"""Register exact lab URLs already present in selected Telegram source posts."""
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

from tegridy_coa_store import gb_matches, telegram_link
from tegridy_state import SRC

HELP = ("Register several lab links in one private message:\n"
        "/coa_links\nGB | product | exact lab URL | test type / note\n"
        "Repeat one line per report (up to 25). Instead of a URL, use chromate:report_id or janoshik:report_id to select the exact URL already stored in Telegram. Each report must already appear in a selected source post. "
        "If it appears in more than one post, add | original_message_id as a fifth field. "
        "This registers links and admin labels only; it does not read or verify the lab results. "
        "Use /coa_unlink registration_id to remove a mistaken registration.")


def lab_url(value):
    value = value.strip()
    url = urlsplit(value)
    if url.scheme != "https" or url.netloc.lower() not in ("chromate.org", "www.chromate.org", "verify.janoshik.com") or url.fragment:
        raise ValueError("Use an exact HTTPS Chromate or Janoshik report URL")
    if len(value) > 500 or any(c.isspace() for c in value):
        raise ValueError("Invalid report URL")
    return value


def reference_matches(url, reference):
    try:
        lab_url(url)
    except ValueError:
        return False
    if reference.startswith("https://"):
        return url == reference
    lab, number = reference.split(":", 1)
    parsed = urlsplit(url)
    if lab == "chromate" and parsed.netloc.lower() in ("chromate.org", "www.chromate.org"):
        code = parse_qs(parsed.query).get("c", [""])[0]
        return code.split("_", 1)[0] == number
    if lab == "janoshik" and parsed.netloc.lower() == "verify.janoshik.com":
        match = re.match(r"/tests/(\d+)-", parsed.path)
        return bool(match and match[1] == number)
    return False


def register_batch(db, text, admin):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not 1 <= len(lines) <= 25:
        raise ValueError("Supply between 1 and 25 report lines")
    prepared = []
    with db:
        db.execute("BEGIN IMMEDIATE")
        for number, line in enumerate(lines, 1):
            try:
                fields = [s.strip() for s in line.split("|")]
                if len(fields) not in (4, 5):
                    raise ValueError("Use GB | product | exact lab URL | test type")
                gb, product, reference, kind = fields[:4]
                if not gb or not product or not kind or len(gb) > 100 or len(product) > 200 or len(kind) > 200:
                    raise ValueError("GB, product and test type are required")
                if not re.fullmatch(r"(?:chromate|janoshik):[1-9][0-9]{0,11}", reference):
                    reference = lab_url(reference)
                chosen = None
                if len(fields) == 5:
                    if not fields[4].isascii() or not fields[4].isdigit() or not 0 < int(fields[4]) <= 2147483647:
                        raise ValueError("Fifth field must be an original Telegram message ID")
                    chosen = int(fields[4])
                candidates = []
                for row in db.execute("""SELECT m.* FROM messages m JOIN sources s ON s.chat=m.chat AND s.topic=m.topic
                     WHERE m.chat=? AND m.deleted=0 AND s.enabled=1""", (SRC,)):
                    if chosen is not None and row["id"] != chosen:
                        continue
                    for stored_url in set(json.loads(row["links"])):
                        if reference_matches(stored_url, reference):
                            candidates.append((row, stored_url))
                if not candidates:
                    raise ValueError("URL not found in the current Telegram catalogue; check transcription or wait for sync")
                if len(candidates) != 1:
                    raise ValueError("URL appears in multiple source posts; append | original_message_id")
                source, url = candidates[0]
                if any(item[0]["id"] == source["id"] and item[3] == url for item in prepared):
                    raise ValueError("Duplicate report URL in this batch")
                prior = db.execute("SELECT gb FROM labels WHERE chat=? AND message=?", (SRC, source["id"])).fetchone()
                if prior and not gb_matches(prior["gb"], gb):
                    raise ValueError("GB conflicts with the source's existing admin label")
                prepared.append((source, gb, product, url, kind))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Line {number}: {exc}. No links were registered.") from exc
        result = []
        for source, gb, product, url, kind in prepared:
            db.execute("""INSERT INTO report_links(chat,message,url,gb,product,kind,admin,updated_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(chat,message,url) DO UPDATE SET
                gb=excluded.gb,product=excluded.product,kind=excluded.kind,admin=excluded.admin,
                updated_at=excluded.updated_at,enabled=1""", (SRC, source["id"], url, gb, product, kind, admin, int(time.time())))
            rid = db.execute("SELECT id FROM report_links WHERE chat=? AND message=? AND url=?", (SRC, source["id"], url)).fetchone()[0]
            result.append({"id": rid, "product": product, "kind": kind,
                           "telegram_url": telegram_link(SRC, source["topic"], source["id"])})
    return result
