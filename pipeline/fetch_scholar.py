"""Read your public Google Scholar profile page: metrics and the list of articles.

Polite by design, following scholar.google.com/robots.txt:
  * one request per run: /citations?user=<id>&pagesize=100 (an allowed path).
    No paging (cstart= is disallowed) and no per-article pages (disallowed).
  * the result is cached; no request is made if the saved copy is younger than
    SCHOLAR_MAX_AGE_HOURS (default 24). --refresh ignores the cache.
  * if Scholar answers with a CAPTCHA / "unusual traffic" page, HTTP 403 or 429,
    it never tries to get around it. If SERPAPI_KEY is set, the same profile is
    read once from SerpApi's google_scholar_author engine instead (a paid API);
    otherwise the script stops and keeps the previous data.

SerpApi budget (the free plan is 250 searches/month):
  * at most one SerpApi search per run, only when the direct request failed, and
    only when the cache is stale (so a daily schedule uses ~31/month);
  * a local counter in data/scholar/serpapi_usage.json caps it at
    SERPAPI_MONTHLY_LIMIT per calendar month (UTC, default 200);
  * SerpApi's account endpoint (free, not counted) is checked first and the
    search is skipped if fewer than SERPAPI_RESERVE searches are left (default 20).

Env: SCHOLAR_USER_ID (the user= value of your profile URL), SCHOLAR_MAX_AGE_HOURS,
     SERPAPI_KEY, SERPAPI_MONTHLY_LIMIT, SERPAPI_RESERVE
Output: data/scholar/profile.json, data/scholar/profile.html (last direct page),
        data/scholar/serpapi.json (last SerpApi answer, metadata stripped)
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


def parse_serpapi(j: dict, user: str) -> dict:
    """Turn a SerpApi google_scholar_author answer into the same shape as parse()."""
    metrics = {}
    for row in (j.get("cited_by") or {}).get("table") or []:
        for key, vals in row.items():  # {"citations": {"all": 818, "since_2021": 816}}
            recent_key = next((k for k in vals if k.startswith("since_")), None)
            metrics[key] = {"all": vals.get("all"), "recent": vals.get(recent_key) if recent_key else None}
            if recent_key:
                metrics["recent_label"] = "Since " + recent_key.split("_", 1)[1]
    per_year = {g["year"]: g.get("citations") or 0
                for g in (j.get("cited_by") or {}).get("graph") or [] if g.get("year")}

    articles = []
    for a in j.get("articles") or []:
        cb = a.get("cited_by") or {}
        articles.append({
            "title": a.get("title"),
            "authors": a.get("authors"),
            "venue": a.get("publication"),
            "year": to_int(str(a.get("year") or "")),
            "citations": cb.get("value"),
            "cited_by_url": cb.get("link"),
            "scholar_id": a.get("citation_id"),
            "scholar_url": a.get("link"),
        })
    return {
        "user": user,
        "profile_url": f"{BASE}/citations?user={user}&hl=en",
        "name": (j.get("author") or {}).get("name"),
        "metrics": metrics,
        "citations_per_year": per_year,
        "articles": articles,
        "article_count": len(articles),
        "maybe_truncated": len(articles) >= PAGE_SIZE,
    }


def fetch_serpapi(user: str, key: str) -> tuple[dict, dict]:
    """One SerpApi search, guarded by the monthly budget. Exits (keeping old data) if over it.

    The key is sent only as a request parameter and never printed or stored:
    errors are reported by status code, not by URL.
    """
    usage_path = OUT / "serpapi_usage.json"
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    usage = json.loads(usage_path.read_text(encoding="utf-8")) if usage_path.exists() else {}
    used = usage.get(month, 0)
    limit = int(os.getenv("SERPAPI_MONTHLY_LIMIT") or 200)
    reserve = int(os.getenv("SERPAPI_RESERVE") or 20)
    if used >= limit:
        sys.exit(f"SerpApi: {used} searches used in {month} (limit {limit}). Kept the previous data.")

    # Free call: does not count against the plan.
    try:
        acc = requests.get("https://serpapi.com/account.json", params={"api_key": key}, timeout=30)
    except requests.RequestException as e:
        sys.exit(f"SerpApi account check failed ({type(e).__name__}). Kept the previous data.")
    if acc.status_code != 200:
        sys.exit(f"SerpApi account check returned HTTP {acc.status_code} (bad key?). Kept the previous data.")
    left = acc.json().get("total_searches_left")
    if left is not None and left < reserve:
        sys.exit(f"SerpApi: only {left} searches left this month (reserve {reserve}). Kept the previous data.")

    try:
        r = requests.get("https://serpapi.com/search.json",
                         params={"engine": "google_scholar_author", "author_id": user, "hl": "en",
                                 "num": PAGE_SIZE, "api_key": key}, timeout=60)
    except requests.RequestException as e:
        sys.exit(f"SerpApi request failed ({type(e).__name__}). Kept the previous data.")
    # Count the attempt whatever the outcome: SerpApi may have billed it.
    usage[month] = used + 1
    usage_path.write_text(json.dumps(dict(sorted(usage.items())[-12:]), indent=2), encoding="utf-8")
    if r.status_code != 200:
        sys.exit(f"SerpApi returned HTTP {r.status_code}. Kept the previous data.")
    j = r.json()
    if j.get("error"):
        sys.exit(f"SerpApi error: {j['error']}. Kept the previous data.")
    left_after = left - 1 if isinstance(left, int) else None
    print(f"SerpApi: 1 search used ({usage[month]} this month by this pipeline; "
          f"{left_after if left_after is not None else '?'} left on the account).")
    return parse_serpapi(j, user), {k: v for k, v in j.items() if k not in ("search_metadata", "search_parameters")}


def main() -> None:
    load_dotenv(ROOT / ".env")
    user = (os.getenv("SCHOLAR_USER_ID") or "").strip()
    if not user:
        sys.exit("Missing SCHOLAR_USER_ID in .env (the user=... part of your Scholar profile URL)")
    max_age = timedelta(hours=float(os.getenv("SCHOLAR_MAX_AGE_HOURS") or 24))
    serpapi_key = (os.getenv("SERPAPI_KEY") or "").strip()
    out_json = OUT / "profile.json"
    args = sys.argv[1:]

    if out_json.exists() and "--refresh" not in args and "--serpapi" not in args:
        prev = json.loads(out_json.read_text(encoding="utf-8"))
        age = datetime.now(timezone.utc) - datetime.fromisoformat(prev["fetched_at"])
        if age < max_age:
            print(f"Cached copy is {age.total_seconds() / 3600:.1f} h old (< {max_age.total_seconds() / 3600:.0f} h); "
                  "not contacting Scholar. Use --refresh to force.")
            return

    OUT.mkdir(parents=True, exist_ok=True)
    data = body = blocked = None
    if "--serpapi" in args:  # skip the direct request (testing / known-blocked network)
        blocked = "skipped (--serpapi)"
    else:
        try:
            r = requests.get(f"{BASE}/citations",
                             params={"user": user, "hl": "en", "pagesize": PAGE_SIZE},
                             headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=30)
        except requests.RequestException as e:
            r, blocked = None, f"request failed ({type(e).__name__})"
        if r is not None:
            body = r.text
            if r.status_code in (403, 429) or "gs_captcha" in body or "unusual traffic" in body.lower():
                blocked = f"blocked (HTTP {r.status_code})"
            elif r.status_code == 404:
                sys.exit(f"Scholar profile {user!r} not found (is it public?)")
            elif r.status_code != 200:
                blocked = f"HTTP {r.status_code}"
            else:
                data = parse(body, user)
                if not data["articles"] and not data["metrics"]:
                    sys.exit("Page parsed to nothing; Scholar may have changed its layout. Saved page not overwritten.")
                data["source"] = "scholar"

    if data is None:
        if not serpapi_key:
            sys.exit(f"Google Scholar {blocked}; no SERPAPI_KEY set. "
                     "Kept the previous data; try again later (hours, not minutes).")
        print(f"Google Scholar {blocked}; reading the profile via SerpApi instead.")
        data, raw = fetch_serpapi(user, serpapi_key)
        if not data["articles"] and not data["metrics"]:
            sys.exit("SerpApi answer parsed to nothing. Kept the previous data.")
        data["source"] = "serpapi"
        (OUT / "serpapi.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        (OUT / "profile.html").write_text(body, encoding="utf-8")

    data["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out_json.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    m = data["metrics"]
    print(json.dumps({"source": data["source"], "name": data["name"], "articles": data["article_count"],
                      "citations": m.get("citations"), "h_index": m.get("h_index"),
                      "i10_index": m.get("i10_index"), "maybe_truncated": data["maybe_truncated"]}, indent=2))


if __name__ == "__main__":
    main()
