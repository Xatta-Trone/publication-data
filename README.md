# Publication data

Daily pipeline that collects my publications, preprints, peer reviews and
citation counts, and publishes them as JSON with a viewer page.

**Live:** https://xatta-trone.github.io/publication-data/

For how it works internally (caching, matching, rules, next steps) see
[AGENTS.md](AGENTS.md).

## Layout

```
run_all.py     runs everything below, reports changes, builds public/
pipeline/      fetch_orcid, fetch_crossref, fetch_openalex, fetch_journals,
               fetch_scholar, summarize, fetch_other_works
site/          index.html viewer page
data/          cached records (committed, so daily runs only fetch what's new)
public/        published to GitHub Pages
```

## What runs (`python run_all.py`)

| Step | Source | Caching |
|---|---|---|
| `fetch_orcid.py` | ORCID works + peer reviews | only new or edited records downloaded |
| `fetch_crossref.py` | Crossref metadata per DOI | once per DOI |
| `fetch_openalex.py` | OpenAlex citations, abstracts, topics | refreshed daily |
| `fetch_journals.py` | Journal names for peer-review ISSNs | once per ISSN |
| `fetch_scholar.py` | Google Scholar profile (1 request; SerpApi if Scholar blocks it) | at most once per day |
| `summarize.py` | Builds the outputs below | — |
| `fetch_other_works.py` | Full authors for Scholar-only works (arXiv/OpenAlex/Crossref) | once per work |

If an optional step fails (e.g. Scholar blocks the request), the run continues
with the previous data for that source.

Google Scholar usually blocks GitHub's runners (HTTP 403). With `SERPAPI_KEY`
set, `fetch_scholar.py` then reads the same profile once through
[SerpApi](https://serpapi.com/google-scholar-author-api). The daily schedule
uses about 31 of the free plan's 250 searches a month. The script stops at
`SERPAPI_MONTHLY_LIMIT` (default 200, counted in `data/scholar/serpapi_usage.json`),
or when the account has fewer than `SERPAPI_RESERVE` (20) searches left. Run
`python pipeline/fetch_scholar.py --serpapi` to test it (uses 1 search).

## Published files

- `index.html` – viewer page
- `publications.json` – ORCID works (no preprints), newest first, with authors, dates, citations
- `preprints.json` – preprints from ORCID or Scholar, and other Scholar-only works; **not counted**
- `peer_reviews.json` – reviews per journal (with per-year counts) and each review's journal, date, role, publisher
- `summary.json` – author-position counts, journals, citation metrics, peer reviews by journal
- `last_run.json` – step status and what changed in the last run

## Setup

1. Copy `.env.example` to `.env` and fill it in (for local runs).
2. GitHub secrets, same names: `gh secret set -f .env -R <owner>/<repo>`.
3. Settings → Pages → Source: **GitHub Actions**.
4. Actions → "Update publication data" → **Run workflow**, or
   `gh workflow run update.yml`. Afterwards it runs daily at 06:23 UTC.

## Local run

```
pip install -r requirements.txt
python run_all.py
python -m http.server 8765 --directory public   # then open http://localhost:8765
```
