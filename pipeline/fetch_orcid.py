"""Pull works (publications) and peer-review records from the ORCID Public API.

Each run reads ORCID's lists (one call for works, one for peer reviews) to see
what exists now, then downloads full records only for put-codes that are new or
whose last-modified date changed; the rest are reused from data/raw/. Records
deleted on ORCID drop out. --full re-downloads everything.

Reads from .env (or the process environment):
    ORCID_CLIENT_ID, ORCID_CLIENT_SECRET, ORCID_ID
    ORCID_ENV=production|sandbox   (optional, default production)

Writes to data/:
    raw/works_summary.json          GET /{id}/works
    raw/works_full.json             GET /{id}/works/{put-codes} (bulk, full records)
    raw/peer_reviews_summary.json   GET /{id}/peer-reviews
    raw/peer_reviews_full.json      GET /{id}/peer-review/{put-code} for each
    works.json                      one flattened record per work group
    peer_reviews.json               one flattened record per review
    meta.json                       fetch time, ORCID iD, counts
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)
DATA = ROOT / "data"
RAW = DATA / "raw"

HOSTS = {
    "production": ("https://orcid.org", "https://pub.orcid.org/v3.0"),
    "sandbox": ("https://sandbox.orcid.org", "https://pub.sandbox.orcid.org/v3.0"),
}
BULK_LIMIT = 100  # ORCID max put-codes per bulk works request
ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


def env(name: str) -> str:
    val = (os.getenv(name) or "").strip()
    if not val:
        sys.exit(f"Missing {name} in .env / environment")
    return val


def get_token(oauth_host: str, client_id: str, client_secret: str) -> str:
    r = requests.post(
        f"{oauth_host}/oauth/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
            "scope": "/read-public",
        },
        headers={"Accept": "application/json"},
        timeout=30,
    )
    if r.status_code != 200:
        sys.exit(f"Token request failed ({r.status_code}): {r.text[:300]}")
    return r.json()["access_token"]


class Orcid:
    def __init__(self, api: str, orcid_id: str, token: str):
        self.base = f"{api}/{orcid_id}"
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json", "Authorization": f"Bearer {token}"})

    def get(self, path: str) -> dict:
        url = f"{self.base}/{path}"
        for attempt in range(4):
            r = self.s.get(url, timeout=60)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2**attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}


# ---------- small helpers for flattening ----------

def val(d, *keys):
    """Safe nested get: val(x, 'title', 'title', 'value')."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def fmt_date(d) -> str | None:
    if not isinstance(d, dict):
        return None
    parts = [val(d, p, "value") for p in ("year", "month", "day")]
    parts = [p for p in parts if p]
    return "-".join(parts) or None


def ext_ids(container) -> dict:
    out: dict[str, str] = {}
    for e in val(container, "external-id") or []:
        t, v = e.get("external-id-type"), e.get("external-id-value")
        if t and v and e.get("external-id-relationship", "self") == "self":
            out.setdefault(t, v)
    return out


def org(o) -> dict | None:
    if not o:
        return None
    return {
        "name": o.get("name"),
        "city": val(o, "address", "city"),
        "country": val(o, "address", "country"),
    }


# ---------- incremental cache ----------

def load_prev(name: str) -> dict[int, dict]:
    """Full records saved by the previous run, keyed by put-code."""
    p = RAW / name
    if not p.exists() or "--full" in sys.argv[1:]:
        return {}
    try:
        return {r["put-code"]: r for r in json.loads(p.read_text(encoding="utf-8")) if "put-code" in r}
    except (json.JSONDecodeError, TypeError):
        return {}


def needs_fetch(summary_item: dict, prev: dict[int, dict]) -> bool:
    """New put-code, or ORCID says the record was modified since we saved it."""
    old = prev.get(summary_item["put-code"])
    return old is None or (val(old, "last-modified-date", "value")
                           != val(summary_item, "last-modified-date", "value"))


# ---------- works ----------

def fetch_works(api: Orcid, prev: dict[int, dict]):
    summary = api.get("works")
    items = [ws for g in summary.get("group", []) for ws in g.get("work-summary", [])]
    put_codes = [ws["put-code"] for ws in items if needs_fetch(ws, prev)]

    full: dict[int, dict] = {ws["put-code"]: prev[ws["put-code"]] for ws in items if not needs_fetch(ws, prev)}
    print(f"  {len(items)} work records; fetching {len(put_codes)} new/changed, {len(full)} unchanged")
    for i in range(0, len(put_codes), BULK_LIMIT):
        chunk = put_codes[i : i + BULK_LIMIT]
        bulk = api.get("works/" + ",".join(map(str, chunk)))
        for item in bulk.get("bulk", []):
            w = item.get("work")
            if w:
                full[w["put-code"]] = w
            else:
                print("  work error:", item.get("error", {}).get("developer-message"))

    flat = []
    for g in summary.get("group", []):
        sums = g.get("work-summary", [])
        if not sums:
            continue
        pref = sums[0]  # ORCID's preferred source for this group
        w = full.get(pref["put-code"], {})
        flat.append({
            "put_code": pref["put-code"],
            "all_put_codes": [s["put-code"] for s in sums],
            "sources": [val(s, "source", "source-name", "value") for s in sums],
            "title": val(w, "title", "title", "value") or val(pref, "title", "title", "value"),
            "subtitle": val(w, "title", "subtitle", "value"),
            "type": w.get("type") or pref.get("type"),
            "journal": val(w, "journal-title", "value") or val(pref, "journal-title", "value"),
            "publication_date": fmt_date(w.get("publication-date") or pref.get("publication-date")),
            "external_ids": ext_ids(g.get("external-ids")),
            "url": val(w, "url", "value") or val(pref, "url", "value"),
            "contributors": [
                {
                    "name": val(c, "credit-name", "value"),
                    "orcid": val(c, "contributor-orcid", "path"),
                    "role": val(c, "contributor-attributes", "contributor-role"),
                    "sequence": val(c, "contributor-attributes", "contributor-sequence"),
                }
                for c in val(w, "contributors", "contributor") or []
            ],
            "short_description": w.get("short-description"),
            "citation": val(w, "citation"),
            "language": w.get("language-code"),
            "country": val(w, "country", "value"),
            "last_modified": val(w, "last-modified-date", "value"),
        })
    return summary, list(full.values()), flat


# ---------- peer reviews ----------

def fetch_peer_reviews(api: Orcid, prev: dict[int, dict]):
    summary = api.get("peer-reviews")
    summaries = [
        s
        for g in summary.get("group", [])
        for pg in g.get("peer-review-group", [])
        for s in pg.get("peer-review-summary", [])
    ]

    full: dict[int, dict] = {s["put-code"]: prev[s["put-code"]] for s in summaries if not needs_fetch(s, prev)}
    todo = [s for s in summaries if needs_fetch(s, prev)]
    print(f"  {len(summaries)} peer reviews; fetching {len(todo)} new/changed, {len(full)} unchanged")
    for s in todo:
        pc = s["put-code"]
        try:
            full[pc] = api.get(f"peer-review/{pc}")
        except requests.HTTPError as e:
            print(f"  peer-review {pc} error: {e}")

    flat = []
    for g in summary.get("group", []):
        group_ids = ext_ids(g.get("external-ids"))  # e.g. {"peer-review": "issn:1234-5678"}
        for pg in g.get("peer-review-group", []):
            for s in pg.get("peer-review-summary", []):
                d = full.get(s["put-code"], s)
                flat.append({
                    "put_code": s["put-code"],
                    "group_id": d.get("review-group-id") or next(iter(group_ids.values()), None),
                    "reviewer_role": d.get("reviewer-role"),
                    "review_type": d.get("review-type"),
                    "completion_date": fmt_date(d.get("review-completion-date") or s.get("completion-date")),
                    "review_url": val(d, "review-url", "value"),
                    "review_ids": ext_ids(d.get("review-identifiers") or s.get("external-ids")),
                    "subject_type": d.get("subject-type"),
                    "subject_name": val(d, "subject-name", "title", "value"),
                    "subject_container": val(d, "subject-container-name", "value"),
                    "subject_ids": ext_ids({"external-id": [d["subject-external-identifier"]]})
                    if d.get("subject-external-identifier") else {},
                    "subject_url": val(d, "subject-url", "value"),
                    "convening_organization": org(d.get("convening-organization")),
                    "source": val(d, "source", "source-name", "value"),
                    "last_modified": val(d, "last-modified-date", "value"),
                })
    return summary, list(full.values()), flat


def save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    load_dotenv(ROOT / ".env")
    client_id, client_secret = env("ORCID_CLIENT_ID"), env("ORCID_CLIENT_SECRET")
    orcid_id = env("ORCID_ID").removeprefix("https://orcid.org/").removeprefix("http://orcid.org/")
    if not ORCID_RE.match(orcid_id):
        sys.exit(f"ORCID_ID doesn't look like 0000-0000-0000-0000: {orcid_id!r}")
    mode = (os.getenv("ORCID_ENV") or "production").strip().lower()
    if mode not in HOSTS:
        sys.exit(f"ORCID_ENV must be one of {list(HOSTS)}")
    oauth_host, api_host = HOSTS[mode]

    print(f"Getting /read-public token ({mode})...")
    api = Orcid(api_host, orcid_id, get_token(oauth_host, client_id, client_secret))

    print("Fetching works...")
    w_sum, w_full, works = fetch_works(api, load_prev("works_full.json"))
    print("Fetching peer reviews...")
    p_sum, p_full, reviews = fetch_peer_reviews(api, load_prev("peer_reviews_full.json"))

    save(RAW / "works_summary.json", w_sum)
    save(RAW / "works_full.json", w_full)
    save(RAW / "peer_reviews_summary.json", p_sum)
    save(RAW / "peer_reviews_full.json", p_full)
    save(DATA / "works.json", works)
    save(DATA / "peer_reviews.json", reviews)
    meta = {
        "orcid_id": orcid_id,
        "environment": mode,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "works": len(works),
        "work_records_all_sources": len(w_full),
        "peer_reviews": len(reviews),
        "peer_review_venues": len({r["group_id"] for r in reviews}),
    }
    save(DATA / "meta.json", meta)
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
