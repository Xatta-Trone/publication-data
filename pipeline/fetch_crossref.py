"""Fetch Crossref metadata for every DOI in data/works.json, once per DOI.

Each record is stored as data/crossref/<url-encoded-doi>.json:
    {"doi": ..., "fetched_at": ..., "message": <Crossref /works/{doi} message>}

Before requesting a DOI the script checks whether its file already exists and
skips it if so. DOIs Crossref doesn't know (404, e.g. DataCite DOIs) are listed
in data/crossref/_not_found.json and are also skipped on later runs; pass
--retry-missing to try them again.

Optional env: CROSSREF_MAILTO=you@example.org puts requests in Crossref's
"polite" pool (faster, more reliable). Not required.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)
WORKS = ROOT / "data" / "works.json"
OUT = ROOT / "data" / "crossref"
NOT_FOUND = OUT / "_not_found.json"
API = "https://api.crossref.org/works/"


def norm_doi(doi: str) -> str:
    d = doi.strip().lower()
    for p in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if d.startswith(p):
            d = d[len(p):]
    return d


def path_for(doi: str) -> Path:
    return OUT / f"{quote(doi, safe='')}.json"


def load_json(p: Path, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def fetch(session: requests.Session, doi: str, params: dict) -> dict | None:
    """Return the Crossref message, or None if Crossref has no such DOI."""
    url = API + quote(doi, safe="")
    for attempt in range(5):
        r = session.get(url, params=params, timeout=60)
        if r.status_code == 404:
            return None
        if r.status_code in (429, 500, 502, 503, 504):
            wait = int(r.headers.get("Retry-After", 0)) or 2**attempt
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()["message"]
    r.raise_for_status()
    return None


def main() -> None:
    load_dotenv(ROOT / ".env")
    retry_missing = "--retry-missing" in sys.argv[1:]

    if not WORKS.exists():
        sys.exit("data/works.json not found; run pipeline/fetch_orcid.py first")
    works = json.loads(WORKS.read_text(encoding="utf-8"))
    dois = sorted({norm_doi(w["external_ids"]["doi"]) for w in works if w.get("external_ids", {}).get("doi")})

    OUT.mkdir(parents=True, exist_ok=True)
    not_found: dict[str, str] = load_json(NOT_FOUND, {})

    session = requests.Session()
    session.headers["User-Agent"] = "orcid-records-fetcher/1.0 (python-requests)"
    params = {}
    if mailto := (os.getenv("CROSSREF_MAILTO") or "").strip():
        params["mailto"] = mailto

    fetched = skipped = missing = 0
    for doi in dois:
        if path_for(doi).exists() or (doi in not_found and not retry_missing):
            skipped += 1
            continue
        msg = fetch(session, doi, params)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if msg is None:
            not_found[doi] = now
            missing += 1
            print(f"  not in Crossref: {doi}")
        else:
            not_found.pop(doi, None)
            path_for(doi).write_text(
                json.dumps({"doi": doi, "fetched_at": now, "message": msg}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            fetched += 1
            print(f"  fetched: {doi}")
        time.sleep(0.2)  # stay well under Crossref's public rate limit

    NOT_FOUND.write_text(json.dumps(not_found, indent=2), encoding="utf-8")
    print(json.dumps({"dois": len(dois), "fetched": fetched, "skipped_existing": skipped,
                      "not_found": missing}, indent=2))


if __name__ == "__main__":
    main()
