"""Explicit admin-supplied lab links without a claimed Telegram source."""
import time
from tegridy_coa_links import lab_url
from tegridy_coa_store import normalized

HELP = ("Supply a lab link privately:\n/coa_supplied\nGB | product | exact lab URL | test type\n"
        "Up to 25 lines. These entries have admin-supplied provenance and no Telegram source. "
        "Results are not fetched or verified. Remove with /coa_supplied_remove ID.")

def register_supplied(db, text, admin):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not 1 <= len(lines) <= 25:
        raise ValueError("Supply between 1 and 25 report lines")
    prepared, seen = [], set()
    for number, line in enumerate(lines, 1):
        try:
            fields = [part.strip() for part in line.split("|")]
            if len(fields) != 4:
                raise ValueError("Use GB | product | exact lab URL | test type")
            gb, product, url, kind = fields
            if not normalized(gb) or not normalized(product) or not kind or len(gb) > 100 or len(product) > 200 or len(kind) > 200:
                raise ValueError("GB, product and test type are required")
            url = lab_url(url)
            key = (normalized(gb), url)
            if key in seen:
                raise ValueError("Duplicate supplied report")
            seen.add(key)
            prepared.append((normalized(gb), product, url, kind))
        except ValueError as exc:
            raise ValueError(f"Line {number}: {exc}. No links were registered.") from exc
    results = []
    with db:
        for gb, product, url, kind in prepared:
            db.execute("""INSERT INTO admin_report_links(url,gb,product,kind,admin,received_at)
                VALUES (?,?,?,?,?,?) ON CONFLICT(gb,url) DO UPDATE SET
                product=excluded.product,kind=excluded.kind,admin=excluded.admin,
                received_at=excluded.received_at,enabled=1""",
                (url, gb, product, kind, admin, int(time.time())))
            rid = db.execute("SELECT id FROM admin_report_links WHERE gb=? AND url=?", (gb, url)).fetchone()[0]
            results.append({"id": rid, "product": product, "kind": kind})
    return results
