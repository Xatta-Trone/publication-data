"""Resolve the ISSNs of peer-review venues to journal names, once per ISSN.

ORCID peer reviews identify the venue only as "issn:XXXX-XXXX". For each ISSN in
data/peer_reviews.json this looks it up in Crossref (/journals/{issn}), falling
back to OpenAlex (/sources/issn:{issn}) if Crossref doesn't have it, and saves:

    data/journals/<issn>.json   {"issn", "title", "publisher", "issns", "source", "fetched_at", "record"}

Existing files are never re-requested. ISSNs that neither service knows are
saved with "title": null, so they aren't retried either; delete the file to retry.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
REVIEWS = ROOT / "data" / "peer_reviews.json"
OUT = ROOT / "data" / "journals"


def get_json(session: requests.Session, url: str, params: dict) -> dict | None:
    for attempt in range(5):
        r = session.get(url, params=params, timeout=60)
        if r.status_code == 404:
            return None
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(int(r.headers.get("Retry-After", 0)) or 2**attempt)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return None


def lookup(session: requests.Session, issn: str, params: dict) -> dict:
    cr = get_json(session, f"https://api.crossref.org/journals/{issn}", params)
    if cr and cr.get("message", {}).get("title"):
        m = cr["message"]
        return {"title": m["title"], "publisher": m.get("publisher"),
                "issns": m.get("ISSN", []), "source": "crossref", "record": m}
    oa = get_json(session, f"https://api.openalex.org/sources/issn:{issn}", params)
    if oa and oa.get("display_name"):
        return {"title": oa["display_name"], "publisher": oa.get("host_organization_name"),
                "issns": oa.get("issn") or [], "source": "openalex", "record": oa}
    return {"title": None, "publisher": None, "issns": [], "source": None, "record": None}


def main() -> None:
    load_dotenv(ROOT / ".env")
    reviews = json.loads(REVIEWS.read_text(encoding="utf-8"))
    issns = sorted({r["group_id"].split(":", 1)[1].upper()
                    for r in reviews if (r.get("group_id") or "").lower().startswith("issn:")})

    OUT.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "orcid-records-fetcher/1.0 (python-requests)"
    params = {"mailto": m} if (m := (os.getenv("CROSSREF_MAILTO") or "").strip()) else {}

    fetched = skipped = unknown = 0
    for issn in issns:
        path = OUT / f"{issn}.json"
        if path.exists():
            skipped += 1
            continue
        info = lookup(session, issn, params)
        info = {"issn": issn, **info, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        path.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
        if info["title"]:
            fetched += 1
            print(f"  {issn}: {info['title']} ({info['source']})")
        else:
            unknown += 1
            print(f"  {issn}: not found")
        time.sleep(0.2)

    print(json.dumps({"issns": len(issns), "fetched": fetched, "skipped_existing": skipped,
                      "not_found": unknown}, indent=2))


if __name__ == "__main__":
    main()
