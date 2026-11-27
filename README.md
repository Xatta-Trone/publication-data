# Publication data

Daily pipeline that collects my publications, preprints, peer reviews and
citation counts, and publishes them as JSON for my website.

## What runs (`python run_all.py`)

| Step | Source | Caching |
|---|---|---|
| `fetch_orcid.py` | ORCID works + peer reviews | re-read every run |
| `fetch_crossref.py` | Crossref metadata per DOI | once per DOI |
| `fetch_openalex.py` | OpenAlex citations, abstracts, topics | refreshed when stale |
| `fetch_journals.py` | Journal names for peer-review ISSNs | once per ISSN |
| `fetch_scholar.py` | Google Scholar profile (1 request) | at most once per ~day |
| `summarize.py` | Builds the outputs below | — |
| `fetch_other_works.py` | Full authors for Scholar-only works (arXiv/OpenAlex/Crossref) | once per work |

If an optional step fails (e.g. Scholar blocks the request), the run continues
with the previous data for that source.

## Published files (`public/`, served by GitHub Pages)

- `publications.json` – ORCID works (no preprints), newest first, with authors, dates, citations
- `preprints.json` – preprints from ORCID or Scholar, and other Scholar-only works; **not counted**
- `peer_reviews.json` – reviews per journal (with per-year counts) and each review's journal, date, role, publisher
- `summary.json` – author-position counts, journals, citation metrics, peer reviews by journal
- `last_run.json` – step status and what changed in the last run

## Setup on GitHub

1. Push this folder to a GitHub repository (`.env` is git-ignored).
2. Settings → Secrets and variables → Actions → add secrets:
   `ORCID_CLIENT_ID`, `ORCID_CLIENT_SECRET`, `ORCID_ID`, `OPENALEX_API_KEY`,
   `CROSSREF_MAILTO`, `SCHOLAR_USER_ID`.
3. Settings → Pages → Source: **GitHub Actions**.
4. Actions → "Update publication data" → **Run workflow** for the first run.
   After that it runs daily at 06:23 UTC.

Files are then at `https://<user>.github.io/<repo>/publications.json` etc.

## Local run

Put the same variables in `.env`, then `python run_all.py`.
