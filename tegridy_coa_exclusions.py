"""Owner-requested report exclusions, applied on every lookup after sync."""
import json
import re
from urllib.parse import urlsplit

# Custom YuanTai Eloralintide certificates excluded by Lou. Keep 250971.
EXCLUDED_JANOSHIK_IDS = frozenset(str(n) for n in range(250973, 251009))
JANOSHIK_HOSTS = frozenset(("janoshik.com", "www.janoshik.com", "verify.janoshik.com"))

def excluded_url(url):
    try:
        parsed = urlsplit(url)
        match = re.match(r"/tests/(\d+)-", parsed.path)
        return parsed.hostname in JANOSHIK_HOSTS and bool(match and match[1] in EXCLUDED_JANOSHIK_IDS)
    except (TypeError, ValueError):
        return False

def scrub_text(text):
    return re.sub(r"https?://[^\s<>]+",
                  lambda match: "" if excluded_url(match[0]) else match[0], text or "")

def filtered_post(row):
    item = dict(row)
    item["links"] = json.dumps([url for url in json.loads(item["links"]) if not excluded_url(url)])
    item["caption"] = scrub_text(item["caption"])
    item["ocr"] = scrub_text(item["ocr"])
    return item
