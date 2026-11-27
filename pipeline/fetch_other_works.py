"""Full metadata (complete author list, DOI, date) for works that are on Google
Scholar but not on ORCID, i.e. the entries in data/other_works.json.

Scholar shortens author lists and its per-paper pages are off limits to bots
(robots.txt), so the details come from open APIs instead:
  1. arXiv API, for entries whose Scholar venue names an arXiv id
  2. OpenAlex title search (preprints on SSRN etc., reports, unlisted papers)
  3. Crossref title search, as a last resort
A result is accepted only if its title is a near-exact match.

Cached per work in data/other_works_meta/<scholar_id>.json. Non-preprints are
fetched once. Preprints are re-checked every 7 days, because an arXiv entry
gains the journal DOI once the paper is published (summarize.py then moves it
off the preprint list). Works no source could match are saved with
"found": false and retried after 7 days. --refresh refetches all.

Run summarize.py afterwards; it merges these into data/other_works.json.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)
OTHER = ROOT / "data" / "other_works.json"
OUT = ROOT / "data" / "other_works_meta"
RETRY_NOT_FOUND = timedelta(days=7)
REFRESH_PREPRINTS = timedelta(days=7)  # same cadence; kept separate for clarity
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def title_key(s: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", s or "").lower()).split())


def same_title(a: str | None, b: str | None) -> bool:
    ka, kb = title_key(a), title_key(b)
    return bool(ka and kb) and (ka == kb or SequenceMatcher(None, ka, kb).ratio() >= 0.92)


def cache_path(work: dict) -> Path:
    key = work.get("scholar_id") or title_key(work["title"])[:80]
    return OUT / (re.sub(r"[^A-Za-z0-9_-]", "_", key) + ".json")


def get(session: requests.Session, url: str, params: dict) -> requests.Response | None:
    for attempt in range(4):
        r = session.get(url, params=params, timeout=60)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(int(r.headers.get("Retry-After", 0)) or 3 * 2**attempt)
            continue
        return r if r.ok else None
    return None


def from_arxiv(session, work) -> dict | None:
    m = re.search(r"arxiv[:\s]*(\d{4}\.\d{4,5})", work.get("venue") or "", re.I)
    if not m:
        return None
    arxiv_id = m.group(1)
    r = get(session, "https://export.arxiv.org/api/query", {"id_list": arxiv_id})
    time.sleep(3)  # arXiv asks for 3 s between API calls
    if not r:
        return None
    entry = ET.fromstring(r.content).find("a:entry", ATOM)
    if entry is None or not same_title(entry.findtext("a:title", "", ATOM), work["title"]):
        return None
    return {
        "source": "arxiv",
        "title": " ".join(entry.findtext("a:title", "", ATOM).split()),
        "authors": [{"name": a.findtext("a:name", "", ATOM).strip(), "orcid": None}
                    for a in entry.findall("a:author", ATOM)],
        "date": (entry.findtext("a:published", "", ATOM) or "")[:10] or None,
        "doi": f"10.48550/arxiv.{arxiv_id}",
        # DOI of the published version, once the authors add it on arXiv
        "published_doi": (entry.findtext("arxiv:doi", None, ATOM) or "").lower() or None,
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "venue": "arXiv",
        "type": "preprint",
        "journal_ref": entry.findtext("arxiv:journal_ref", None, ATOM),
    }


def from_openalex(session, work, key) -> dict | None:
    params = {"search": work["title"], "per-page": 5}
    if key:
        params["api_key"] = key
    r = get(session, "https://api.openalex.org/works", params)
    if not r:
        return None
    for w in r.json().get("results", []):
        if same_title(w.get("title"), work["title"]):
            loc = w.get("primary_location") or {}
            return {
                "source": "openalex",
                "title": w.get("title"),
                "authors": [{"name": a["author"].get("display_name"),
                             "orcid": (a["author"].get("orcid") or "").rsplit("/", 1)[-1] or None}
                            for a in w.get("authorships", [])],
                "date": w.get("publication_date"),
                "doi": (w.get("doi") or "").removeprefix("https://doi.org/").lower() or None,
                "url": w.get("doi") or loc.get("landing_page_url"),
                "venue": (loc.get("source") or {}).get("display_name"),
                "type": w.get("type"),
                "openalex_id": w.get("id"),
                "citations_openalex": w.get("cited_by_count"),
            }
    return None


def from_crossref(session, work, mailto) -> dict | None:
    params = {"query.bibliographic": work["title"], "rows": 5}
    if mailto:
        params["mailto"] = mailto
    r = get(session, "https://api.crossref.org/works", params)
    if not r:
        return None
    for m in r.json()["message"].get("items", []):
        if same_title((m.get("title") or [None])[0], work["title"]):
            parts = (m.get("issued") or {}).get("date-parts", [[None]])[0]
            return {
                "source": "crossref",
                "title": m["title"][0],
                "authors": [{"name": " ".join(filter(None, [a.get("given"), a.get("family")])) or a.get("name"),
                             "orcid": (a.get("ORCID") or "").rsplit("/", 1)[-1] or None}
                            for a in m.get("author", [])],
                "date": "-".join(f"{x:02d}" if i else str(x) for i, x in enumerate(parts) if x) or None,
                "doi": m["DOI"].lower(),
                "url": f"https://doi.org/{m['DOI']}",
                "venue": (m.get("container-title") or [None])[0] or m.get("publisher"),
                "type": m.get("type"),
            }
    return None


def main() -> None:
    load_dotenv(ROOT / ".env")
    if not OTHER.exists():
        sys.exit("data/other_works.json not found; run pipeline/fetch_scholar.py and pipeline/summarize.py first")
    works = json.loads(OTHER.read_text(encoding="utf-8"))
    refresh = "--refresh" in sys.argv[1:]
    oa_key = (os.getenv("OPENALEX_API_KEY") or "").strip()
    mailto = (os.getenv("CROSSREF_MAILTO") or "").strip()

    OUT.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "orcid-records-fetcher/1.0 (python-requests)"

    found = missing = skipped = 0
    for w in works:
        p = cache_path(w)
        if p.exists() and not refresh:
            prev = json.loads(p.read_text(encoding="utf-8"))
            age = datetime.now(timezone.utc) - datetime.fromisoformat(prev["fetched_at"])
            # Found records are kept, except preprints: re-check those weekly, since
            # arXiv entries gain a journal DOI once the paper is published.
            is_pre = prev.get("type") in ("preprint", "posted-content")
            if (prev.get("found") and not is_pre) or age < REFRESH_PREPRINTS:
                skipped += 1
                continue
        meta = from_arxiv(session, w) or from_openalex(session, w, oa_key) or from_crossref(session, w, mailto)
        rec = {"scholar_id": w.get("scholar_id"), "scholar_title": w["title"], "found": bool(meta),
               "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **(meta or {})}
        p.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        if meta:
            found += 1
            print(f"  [{meta['source']:8}] {len(meta['authors'])} authors  {w['title'][:60]}")
        else:
            missing += 1
            print(f"  [not found] {w['title'][:60]}")
        time.sleep(0.3)

    print(json.dumps({"works": len(works), "found": found, "not_found": missing,
                      "skipped_cached": skipped}, indent=2))


if __name__ == "__main__":
    main()
