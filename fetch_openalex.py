"""Fetch OpenAlex records for every DOI in data/works.json.

Unlike Crossref metadata, OpenAlex citation counts change over time, so each
record is cached with its fetch time and re-fetched only when older than
OPENALEX_MAX_AGE_DAYS (default 7). Pass --refresh to re-fetch everything now.

    data/openalex/<url-encoded-doi>.json  {"doi", "fetched_at", "work": <OpenAlex work>}
    data/openalex/_not_found.json         DOIs OpenAlex doesn't have yet (retried daily)

Env: OPENALEX_API_KEY (required), OPENALEX_MAX_AGE_DAYS (optional)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
WORKS = ROOT / "data" / "works.json"
OUT = ROOT / "data" / "openalex"
NOT_FOUND = OUT / "_not_found.json"
API = "https://api.openalex.org/works"
BATCH = 50  # DOIs per filter request


def norm_doi(doi: str) -> str:
    d = doi.strip().lower()
    for p in ("https://doi.org/", "http://doi.org/", "doi:"):
        d = d.removeprefix(p)
    return d


def path_for(doi: str) -> Path:
    return OUT / f"{quote(doi, safe='')}.json"


def is_fresh(stamp: str | None, max_age: timedelta) -> bool:
    if not stamp:
        return False
    return datetime.now(timezone.utc) - datetime.fromisoformat(stamp) < max_age


def get(session: requests.Session, params: dict) -> dict:
    for attempt in range(5):
        r = session.get(API, params=params, timeout=60)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(int(r.headers.get("Retry-After", 0)) or 2**attempt)
            continue
        if r.status_code in (401, 403):
            sys.exit(f"OpenAlex rejected the API key ({r.status_code}): {r.text[:200]}")
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


def main() -> None:
    load_dotenv(ROOT / ".env")
    key = (os.getenv("OPENALEX_API_KEY") or "").strip()
    if not key:
        sys.exit("Missing OPENALEX_API_KEY in .env")
    max_age = timedelta(days=float(os.getenv("OPENALEX_MAX_AGE_DAYS") or 7))
    refresh = "--refresh" in sys.argv[1:]

    works = json.loads(WORKS.read_text(encoding="utf-8"))
    dois = sorted({norm_doi(w["external_ids"]["doi"]) for w in works if (w.get("external_ids") or {}).get("doi")})

    OUT.mkdir(parents=True, exist_ok=True)
    not_found: dict[str, str] = json.loads(NOT_FOUND.read_text(encoding="utf-8")) if NOT_FOUND.exists() else {}

    def needs_fetch(doi: str) -> bool:
        if refresh:
            return True
        p = path_for(doi)
        if p.exists():
            return not is_fresh(json.loads(p.read_text(encoding="utf-8")).get("fetched_at"), max_age)
        # New papers take days to reach OpenAlex, so retry missing ones daily.
        return not is_fresh(not_found.get(doi), min(max_age, timedelta(days=1)))

    todo = [d for d in dois if needs_fetch(d)]
    session = requests.Session()
    session.headers["User-Agent"] = "orcid-records-fetcher/1.0 (python-requests)"

    fetched = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i : i + BATCH]
        data = get(session, {"filter": "doi:" + "|".join(chunk), "per-page": BATCH, "api_key": key})
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        found = set()
        for w in data.get("results", []):
            doi = norm_doi(w.get("doi") or "")
            if doi not in chunk:
                continue
            found.add(doi)
            not_found.pop(doi, None)
            path_for(doi).write_text(json.dumps({"doi": doi, "fetched_at": now, "work": w},
                                                indent=2, ensure_ascii=False), encoding="utf-8")
            fetched += 1
        for doi in set(chunk) - found:
            not_found[doi] = now
            print(f"  not in OpenAlex: {doi}")
        time.sleep(0.2)

    NOT_FOUND.write_text(json.dumps(not_found, indent=2), encoding="utf-8")
    print(json.dumps({"dois": len(dois), "fetched": fetched, "fresh_cached": len(dois) - len(todo),
                      "not_found": len(set(todo) & set(not_found)),
                      "max_age_days": max_age.days}, indent=2))


if __name__ == "__main__":
    main()
