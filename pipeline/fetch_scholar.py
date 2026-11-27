"""Read your public Google Scholar profile page: metrics and the list of articles.

Polite by design, following scholar.google.com/robots.txt:
  * one request per run: /citations?user=<id>&pagesize=100 (an allowed path).
    No paging (cstart= is disallowed) and no per-article pages (disallowed).
  * the result is cached; no request is made if the saved copy is younger than
    SCHOLAR_MAX_AGE_HOURS (default 24). --refresh ignores the cache.
  * if Scholar answers with a CAPTCHA / "unusual traffic" page or HTTP 429, the
    script stops and keeps the previous data. It never tries to get around it.

Env: SCHOLAR_USER_ID (the user= value of your profile URL), SCHOLAR_MAX_AGE_HOURS
Output: data/scholar/profile.json, data/scholar/profile.html (last page, for re-parsing)
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # repo root (scripts live in pipeline/)
OUT = ROOT / "data" / "scholar"
BASE = "https://scholar.google.com"
PAGE_SIZE = 100
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def to_int(s: str | None) -> int | None:
    s = (s or "").strip().replace(",", "")
    return int(s) if s.isdigit() else None


def parse(html: str, user: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    metrics = {}
    for tr in soup.select("#gsc_rsb_st tbody tr"):
        cells = [td.get_text(strip=True) for td in tr.select("td")]
        if len(cells) >= 3:
            key = cells[0].lower().replace("-", "_")  # citations / h_index / i10_index
            metrics[key] = {"all": to_int(cells[1]), "recent": to_int(cells[2])}
    since = soup.select("#gsc_rsb_st thead th")
    if len(since) >= 3:
        metrics["recent_label"] = since[2].get_text(strip=True)  # e.g. "Since 2021"

    years = [to_int(y.get_text()) for y in soup.select(".gsc_g_t")]
    # Each bar's z-index is its position counted from the right end of the chart,
    # so years with no citations (no bar) are handled correctly.
    per_year = {y: 0 for y in years if y}
    for a in soup.select("a.gsc_g_a"):
        m = re.search(r"z-index:\s*(\d+)", a.get("style", ""))
        if m and years:
            idx = len(years) - int(m.group(1))
            if 0 <= idx < len(years) and years[idx]:
                per_year[years[idx]] = to_int(a.get_text()) or 0

    articles = []
    for tr in soup.select("tr.gsc_a_tr"):
        a = tr.select_one("a.gsc_a_at")
        if not a:
            continue
        gray = [g.get_text(" ", strip=True) for g in tr.select("div.gs_gray")]
        href = a.get("href") or ""
        qs = parse_qs(urlparse(href).query)
        cite = tr.select_one("a.gsc_a_ac")
        articles.append({
            "title": a.get_text(" ", strip=True),
            "authors": gray[0] if gray else None,  # Scholar shortens long lists with "..."
            "venue": gray[1] if len(gray) > 1 else None,
            "year": to_int((tr.select_one(".gsc_a_y span") or tr.select_one(".gsc_a_h") or a).get_text()),
            "citations": to_int(cite.get_text()) if cite else None,
            "cited_by_url": cite.get("href") if cite and cite.get("href") else None,
            "scholar_id": (qs.get("citation_for_view") or [None])[0],
            "scholar_url": urljoin(BASE, href) if href else None,
        })

    name = soup.select_one("#gsc_prf_in")
    return {
        "user": user,
        "profile_url": f"{BASE}/citations?user={user}&hl=en",
        "name": name.get_text(strip=True) if name else None,
        "metrics": metrics,
        "citations_per_year": per_year,
        "articles": articles,
        "article_count": len(articles),
        "maybe_truncated": len(articles) >= PAGE_SIZE,
    }


def main() -> None:
    load_dotenv(ROOT / ".env")
    user = (os.getenv("SCHOLAR_USER_ID") or "").strip()
    if not user:
        sys.exit("Missing SCHOLAR_USER_ID in .env (the user=... part of your Scholar profile URL)")
    max_age = timedelta(hours=float(os.getenv("SCHOLAR_MAX_AGE_HOURS") or 24))
    out_json = OUT / "profile.json"

    if out_json.exists() and "--refresh" not in sys.argv[1:]:
        prev = json.loads(out_json.read_text(encoding="utf-8"))
        age = datetime.now(timezone.utc) - datetime.fromisoformat(prev["fetched_at"])
        if age < max_age:
            print(f"Cached copy is {age.total_seconds() / 3600:.1f} h old (< {max_age.total_seconds() / 3600:.0f} h); "
                  "not contacting Scholar. Use --refresh to force.")
            return

    r = requests.get(f"{BASE}/citations",
                     params={"user": user, "hl": "en", "pagesize": PAGE_SIZE},
                     headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=30)
    body = r.text
    if r.status_code == 429 or "gs_captcha" in body or "unusual traffic" in body.lower():
        sys.exit("Google Scholar asked for a CAPTCHA / rate-limited this request. "
                 "Kept the previous data; try again later (hours, not minutes).")
    if r.status_code == 404:
        sys.exit(f"Scholar profile {user!r} not found (is it public?)")
    r.raise_for_status()

    data = parse(body, user)
    if not data["articles"] and not data["metrics"]:
        sys.exit("Page parsed to nothing; Scholar may have changed its layout. Saved page not overwritten.")
    data["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "profile.html").write_text(body, encoding="utf-8")
    out_json.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    m = data["metrics"]
    print(json.dumps({"name": data["name"], "articles": data["article_count"],
                      "citations": m.get("citations"), "h_index": m.get("h_index"),
                      "i10_index": m.get("i10_index"), "maybe_truncated": data["maybe_truncated"]}, indent=2))


if __name__ == "__main__":
    main()
