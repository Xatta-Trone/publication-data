# AGENTS.md

Instructions for AI agents (and humans) working on this repository. Read this
first, then `README.md` for setup. Keep this file current when you change how
the pipeline works.

## What this repo is

A daily pipeline that collects one researcher's scholarly record and publishes
it as JSON plus a viewer page on GitHub Pages:

- **Live site:** https://xatta-trone.github.io/publication-data/
- **Repo:** https://github.com/Xatta-Trone/publication-data
- **Owner's identifiers:** ORCID iD in `data/meta.json`, Scholar id in secrets.

ORCID is the source of truth for *what is published*. Crossref, OpenAlex and
Google Scholar enrich it. Preprints are shown but **never counted**.

## Layout

```
run_all.py            entry point: runs pipeline/ in order, writes change report, builds public/
pipeline/             one script per step (each runnable on its own)
  fetch_orcid.py        ORCID works + peer reviews          -> data/works.json, data/peer_reviews.json, data/raw/
  fetch_crossref.py     Crossref record per DOI             -> data/crossref/<urlencoded-doi>.json
  fetch_openalex.py     OpenAlex record per DOI             -> data/openalex/<urlencoded-doi>.json
  fetch_journals.py     journal name per peer-review ISSN   -> data/journals/<ISSN>.json
  fetch_scholar.py      Scholar profile page (1 request)    -> data/scholar/profile.json
  summarize.py          joins everything                    -> data/publications.json, preprints.json,
                                                               peer_reviews_public.json, summary.json,
                                                               other_works.json, authorship.csv
  fetch_other_works.py  full authors for Scholar-only works -> data/other_works_meta/<scholar_id>.json
site/index.html       viewer page (plain HTML/CSS/JS, no build, no dependencies)
public/               what GitHub Pages serves (generated; do not hand-edit)
data/                 cache + intermediate outputs, committed so runs are incremental
.github/workflows/update.yml   daily 06:23 UTC + manual "Run workflow"
```

Every script finds the repo root with `Path(__file__).resolve().parent.parent`
and loads `.env` from there. Run them from anywhere.

## Run order and why

`run_all.py` runs: orcid → crossref → openalex → journals → scholar →
**summarize → other_works → summarize**. Summarize runs twice because the first
pass writes `data/other_works.json` (Scholar entries not on ORCID), which
`fetch_other_works.py` reads; the second pass merges the looked-up authors.

Critical steps (`fetch_orcid`, `fetch_crossref`, `summarize`) fail the run.
Optional steps (`openalex`, `journals`, `scholar`, `other_works`) are marked
"skipped" and the previous cached data is used. Keep that split: a flaky
enrichment source must never block publishing.

## Caching rules (do not break these)

| Data | Refetched when | Key |
|---|---|---|
| ORCID full records | put-code is new, or its `last-modified-date` changed (`--full` forces all) | put-code |
| Crossref | never once saved (delete the file to refetch); 404s listed in `_not_found.json` | DOI, lowercased, URL-encoded |
| OpenAlex | older than `OPENALEX_MAX_AGE_DAYS` (workflow: 0.8); misses retried daily | same as Crossref |
| Journal names | never once saved | ISSN, uppercased |
| Scholar | older than `SCHOLAR_MAX_AGE_HOURS` (workflow: 20) | single file |
| Other-works metadata | never once found; misses retried after 7 days | Scholar `citation_for_view` id |

The workflow commits `data/` back to the repo after each run; that is what makes
the next run incremental on a fresh runner.

## Rules that must hold

- **Secrets:** never write `.env` values (client secret, API key, email) into
  `data/`, `public/`, logs or commits. `CROSSREF_MAILTO` is sent to Crossref
  only as a request parameter and is never stored. Check after changes:
  scan `data/` and `public/` for the values in `.env`.
- **Google Scholar:** exactly one request per run, to
  `/citations?user=<id>&pagesize=100`, which `robots.txt` allows. Do not add
  paging (`cstart=`), per-article pages (`view_op=view_citation`), proxies,
  rotating user agents, or CAPTCHA handling. On a CAPTCHA/429 the script exits
  and keeps the old data. Full author lists for Scholar-only works come from
  arXiv / OpenAlex / Crossref instead (`fetch_other_works.py`).
- **Preprints are not counted.** `summarize.is_preprint()` (Crossref type
  `posted-content`/`preprint`, or venue mentions arXiv/SSRN/Research Square/…)
  moves a work to `preprints.json`. All counts, journal lists and citation
  totals in `summary.json` use non-preprints only. Everything on Scholar but not
  on ORCID also goes to `preprints.json` (`kind`: `preprint` or `other`).
- **Finding the owner in author lists:** ORCID iD match first; otherwise a name
  match against spellings learned from the iD-matched papers
  (`summary.json → name_variants_matched`). Never hard-code the name. A name
  match must be unique within a paper, otherwise the position is "unresolved".
- **Matching Scholar to ORCID works:** normalized title, `SequenceMatcher`
  ratio ≥ 0.92, each Scholar entry used once.
- **Peer reviews:** publish journal/date/role/publisher only. The reviewed
  manuscript is confidential and is not in the data anyway.
- **Public output:** only the files in `run_all.PUBLIC_FILES` plus
  `last_run.json`, `index.html`, `.nojekyll`. No raw API dumps.

## Dates

- `date_online` (display): Crossref `published-online` if it has a day →
  publisher history assertion (`first_online`/`published`) → full
  `published-print` if earlier than DOI registration (conference papers) →
  `created` (DOI registration; Elsevier only deposits year-month, and its DOI is
  registered when the article goes online). `date_online_source` says which.
- `date_issue` (citation): `published-print` or `issued`, often year-month only.
- `date_created`: Crossref `created`; `publications.json` is sorted by it, newest first.

## Public JSON contracts (the viewer and any external site depend on these)

- `publications.json`: list of works — `doi, title, type, venue, year,
  date_online, date_issue, date_created, url, authors[{name, orcid, is_me}],
  my_position, volume, issue, pages, citations_scholar, citations_openalex,
  citations_crossref, is_open_access, oa_url, abstract, primary_topic, keywords,
  citations_by_year, scholar_url, is_preprint`.
- `preprints.json`: same core fields plus `listed_on` (`orcid`/`scholar`),
  `kind`, `authors_text` (Scholar's shortened list when full authors unknown).
- `peer_reviews.json`: `total, unique_journals, by_year, by_journal[{journal,
  issns, publisher, reviews, by_year}], reviews[{journal, issn, publisher, date,
  year, role, type, verified_by}]`.
- `summary.json`: counts (`author_position`, `journals`, `by_type`),
  `citations{scholar, openalex, crossref}`, `scholar_profile`, `peer_reviews`.
- `last_run.json`: per-step status and `changes` (new works/preprints/reviews,
  citation changes).

Adding fields is fine. Renaming or removing one breaks `site/index.html` and
any site reading these files — update both together.

## Working on it

```
pip install -r requirements.txt
python run_all.py                      # full run (needs .env, see .env.example)
python pipeline/summarize.py           # rebuild outputs from cache, no network
python -m http.server 8765 --directory public   # view the page at localhost:8765
```

- After changing `site/index.html`, re-run `run_all.py` (or copy it to
  `public/index.html`) and check the page in a browser, light and dark mode and
  at phone width. It must not be opened via `file://` (fetch needs HTTP).
- After changing a pipeline step, run it twice: the second run should fetch
  nothing new (check the "fetched / skipped" counts).
- To trigger the GitHub run: `gh workflow run update.yml -R Xatta-Trone/publication-data`,
  then `gh run watch -R Xatta-Trone/publication-data`. The job summary shows the change report.
- Windows console: set `PYTHONIOENCODING=utf-8` when printing titles.

## Known gaps / next steps

- **Scholar from GitHub Actions** is often blocked (datacenter IPs). The run
  then reuses the last Scholar data. Options if it stays blocked: run
  `pipeline/fetch_scholar.py` locally and push; a self-hosted runner; a paid
  Scholar API. Check `last_run.json` → step `fetch_scholar.py`.
- **Abstracts** are missing for ~half the papers (Elsevier deposits none).
  Could add a hand-maintained overrides file.
- **Manual overrides** (featured papers, code/data links, corresponding
  author, plain-language summaries) are not implemented yet. Suggested: a
  `overrides.json` keyed by DOI, merged in `summarize.py`.
- **PINNs survey** (Scholar-only) is not indexed by arXiv/OpenAlex/Crossref,
  so only Scholar's shortened author list is known; it is retried weekly.
- **Owner's personal website** is not wired up yet; it can fetch the JSON from
  the live site (GitHub Pages sends `Access-Control-Allow-Origin: *`).
- Works missing from ORCID show up in `preprints.json` with `listed_on:
  "scholar"`; adding them to ORCID moves them into the counted list.
