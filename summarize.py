"""Summarize authorship position and venues across your publications.

Inputs : data/works.json, data/meta.json (from fetch_orcid.py)
         data/crossref/*.json         (from fetch_crossref.py)
Outputs: data/summary.json            counts by author position and by venue
         data/authorship.csv          one row per paper, for checking matches

How "you" are found in each author list (Crossref list, else ORCID's):
  1. an author whose ORCID iD equals ORCID_ID, else
  2. an author whose normalized name matches a name variant learned from
     papers where step 1 matched (so no name is hard-coded).
Papers where neither works are reported as "unresolved" and not guessed.
"""

from __future__ import annotations

import csv
import html
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CROSSREF = DATA / "crossref"


def norm_name(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z ]", " ", s.lower()).split())


def clean_title(s: str | None) -> str | None:
    return " ".join(html.unescape(s).split()) if s else None


def crossref_record(doi: str) -> dict | None:
    p = CROSSREF / f"{quote(doi.lower(), safe='')}.json"
    return json.loads(p.read_text(encoding="utf-8"))["message"] if p.exists() else None


def openalex_record(doi: str) -> dict | None:
    p = DATA / "openalex" / f"{quote(doi.lower(), safe='')}.json"
    return json.loads(p.read_text(encoding="utf-8"))["work"] if p.exists() else None


def cr_date(cr: dict, key: str) -> str | None:
    parts = ((cr.get(key) or {}).get("date-parts") or [[None]])[0]
    if not parts or parts[0] is None:
        return None
    return "-".join(f"{x:02d}" if i else str(x) for i, x in enumerate(parts) if x)


def parse_loose_date(s: str | None) -> str | None:
    """'24 August 2022' or '2025-09-10' -> '2022-08-24'."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def online_date(cr: dict | None) -> tuple[str | None, str | None]:
    """Best full (YYYY-MM-DD) date the paper went online, and where it came from.

    1. Crossref published-online, if it has a day
    2. publisher's article-history assertion (first_online / published)
    3. Crossref 'created' = DOI registration date. Elsevier registers DOIs when the
       article goes online, so this is a close stand-in where (1) and (2) are missing.
    """
    if not cr:
        return None, None
    d = cr_date(cr, "published-online")
    if d and len(d) == 10:
        return d, "crossref:published-online"
    for a in cr.get("assertion", []):
        if a.get("name") in ("first_online", "published") and (d := parse_loose_date(str(a.get("value")))):
            return d, f"crossref:assertion:{a['name']}"
    created = cr_date(cr, "created")
    printed = cr_date(cr, "published-print")
    # Conference papers often get their DOI after the event; a full print date that
    # is earlier than the DOI registration is the better date then.
    if printed and len(printed) == 10 and (not created or printed < created):
        return printed, "crossref:published-print"
    return (created, "crossref:created (DOI registered)") if created else (None, None)


def abstract_text(cr: dict | None, oa: dict | None) -> tuple[str | None, str | None]:
    if cr and cr.get("abstract"):
        txt = re.sub(r"<[^>]+>", " ", cr["abstract"])
        txt = re.sub(r"^\s*(Abstract|ABSTRACT)\s*", "", " ".join(html.unescape(txt).split()))
        return txt, "crossref"
    inv = (oa or {}).get("abstract_inverted_index")
    if inv:
        words = sorted((i, w) for w, idx in inv.items() for i in idx)
        return " ".join(w for _, w in words), "openalex"
    return None, None


def title_key(s: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", s or "").lower()).split())


def load_scholar() -> dict | None:
    p = DATA / "scholar" / "profile.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def match_scholar(titles: list[str], articles: list[dict]) -> dict[int, dict]:
    """Map index in `titles` -> Scholar article, by near-identical normalized title.

    Scholar recases titles and sometimes swaps punctuation, so a ratio >= 0.92
    counts as the same paper. Each Scholar article is used at most once.
    """
    from difflib import SequenceMatcher
    keys = [title_key(a["title"]) for a in articles]
    used: set[int] = set()
    out: dict[int, dict] = {}
    for i, t in enumerate(titles):
        tk = title_key(t)
        best, score = None, 0.0
        for j, k in enumerate(keys):
            if j in used:
                continue
            s = 1.0 if k == tk else SequenceMatcher(None, tk, k).ratio()
            if s > score:
                best, score = j, s
        if best is not None and score >= 0.92:
            used.add(best)
            out[i] = articles[best]
    return out


def other_work_meta(a: dict) -> dict | None:
    """Cached full metadata from fetch_other_works.py, if any (same key scheme)."""
    key = a.get("scholar_id") or title_key(a["title"])[:80]
    p = DATA / "other_works_meta" / (re.sub(r"[^A-Za-z0-9_-]", "_", key) + ".json")
    if not p.exists():
        return None
    m = json.loads(p.read_text(encoding="utf-8"))
    return m if m.get("found") else None


def is_my_name(name: str | None, variants: set[str]) -> bool:
    """Exact variant, or one is a word-subset of the other with >= 2 words
    (e.g. 'monzurul islam' vs 'md monzurul islam')."""
    n = norm_name(name)
    if n in variants:
        return True
    words = set(n.split())
    return any(len(words & set(v.split())) >= 2 and (words <= set(v.split()) or set(v.split()) <= words)
               for v in variants)


PREPRINT_HINTS = ("arxiv", "ssrn", "preprint", "research square", "techrxiv", "medrxiv", "biorxiv", "osf")


PREPRINT_TYPES = ("posted-content", "preprint")


def is_preprint(work_type: str | None, venue: str | None) -> bool:
    return (work_type or "").lower() in PREPRINT_TYPES or any(h in (venue or "").lower() for h in PREPRINT_HINTS)


def h_index(cites: list[int]) -> int:
    return sum(1 for i, c in enumerate(sorted(cites, reverse=True), 1) if c >= i)


def author_list(work: dict, cr: dict | None) -> tuple[list[dict], str]:
    """Return [{name, orcid}] in byline order, and where it came from."""
    if cr and cr.get("author"):
        return [
            {"name": " ".join(filter(None, [a.get("given"), a.get("family")])) or a.get("name"),
             "orcid": (a.get("ORCID") or "").rsplit("/", 1)[-1] or None}
            for a in cr["author"]
        ], "crossref"
    return [{"name": c.get("name"), "orcid": c.get("orcid")} for c in work.get("contributors", [])
            if (c.get("role") or "author") == "author"], "orcid"


def peer_review_venues() -> tuple[dict, list[str]]:
    """Count peer reviews per journal name (from data/journals/<issn>.json).

    Journals with separate print/online ISSNs resolve to the same name, so they
    are counted together. Returns ({journal: count}, [unresolved group ids]).
    """
    p = DATA / "peer_reviews.json"
    if not p.exists():
        return {}, []
    counts: Counter = Counter()
    unresolved: list[str] = []
    for r in json.loads(p.read_text(encoding="utf-8")):
        gid = r.get("group_id") or ""
        title = None
        if gid.lower().startswith("issn:"):
            jp = DATA / "journals" / f"{gid.split(':', 1)[1].upper()}.json"
            if jp.exists():
                title = clean_title(json.loads(jp.read_text(encoding="utf-8")).get("title"))
        if title:
            counts[title] += 1
        else:
            counts[f"[unresolved] {gid}"] += 1
            unresolved.append(gid)
    return dict(counts.most_common()), sorted(set(unresolved))


def citation_summary(rows: list[dict]) -> dict:
    out = {}
    for src in ("scholar", "openalex", "crossref"):
        c = [r[f"citations_{src}"] for r in rows if r[f"citations_{src}"] is not None]
        out[src] = {"papers_with_data": len(c), "total": sum(c), "h_index": h_index(c),
                    "i10_index": sum(x >= 10 for x in c)}
    return out


def peer_reviews_public() -> dict:
    """Site-ready peer reviews: one entry per journal (all ISSNs merged by name)
    and one per review. Internal review identifiers are left out."""
    p = DATA / "peer_reviews.json"
    reviews = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    journals: dict[str, dict] = {}
    items = []
    for r in reviews:
        gid = r.get("group_id") or ""
        issn = gid.split(":", 1)[1].upper() if gid.lower().startswith("issn:") else None
        info = {}
        if issn and (jp := DATA / "journals" / f"{issn}.json").exists():
            info = json.loads(jp.read_text(encoding="utf-8"))
        name = clean_title(info.get("title")) or gid or "Unknown venue"
        date = r.get("completion_date")
        org = r.get("convening_organization") or {}
        items.append({
            "journal": name,
            "issn": issn,
            "publisher": info.get("publisher") or org.get("name"),
            "date": date,
            "year": int(date[:4]) if date else None,
            "role": r.get("reviewer_role"),
            "type": r.get("review_type"),
            "verified_by": (r.get("source") or "").strip() or None,
        })
        j = journals.setdefault(name, {"journal": name, "issns": set(), "publisher": items[-1]["publisher"],
                                       "reviews": 0, "by_year": Counter()})
        j["reviews"] += 1
        if issn:
            j["issns"].add(issn)
        if items[-1]["year"]:
            j["by_year"][items[-1]["year"]] += 1
    by_journal = sorted(journals.values(), key=lambda j: (-j["reviews"], j["journal"]))
    for j in by_journal:
        j["issns"] = sorted(j["issns"])
        j["by_year"] = dict(sorted(j["by_year"].items()))
    items.sort(key=lambda x: (x["date"] or "", x["journal"]), reverse=True)
    return {
        "total": len(items),
        "unique_journals": len(by_journal),
        "by_year": dict(sorted(Counter(x["year"] for x in items if x["year"]).items())),
        "by_journal": by_journal,
        "reviews": items,
    }


def position_bucket(pos: int | None) -> str:
    return {None: "unresolved", 1: "first", 2: "second"}.get(pos, "third_or_later")


def main() -> None:
    orcid_id = json.loads((DATA / "meta.json").read_text(encoding="utf-8"))["orcid_id"]
    works = json.loads((DATA / "works.json").read_text(encoding="utf-8"))

    papers = []
    for w in works:
        doi = (w.get("external_ids") or {}).get("doi")
        cr = crossref_record(doi) if doi else None
        authors, src = author_list(w, cr)
        papers.append({"work": w, "doi": doi, "cr": cr, "authors": authors, "author_source": src})

    # Pass 1: ORCID iD match, and learn the name spellings used for you.
    my_names: set[str] = set()
    for p in papers:
        p["position"], p["match"] = None, None
        for i, a in enumerate(p["authors"], 1):
            if a["orcid"] == orcid_id:
                p["position"], p["match"] = i, "orcid"
                my_names.add(norm_name(a["name"]))
                break

    # Pass 2: name match where the iD wasn't attached. Require exactly one hit.
    for p in papers:
        if p["position"]:
            continue
        hits = [i for i, a in enumerate(p["authors"], 1) if norm_name(a["name"]) in my_names]
        if len(hits) == 1:
            p["position"], p["match"] = hits[0], "name"

    rows, pubs = [], []
    for p in papers:
        w, cr = p["work"], p["cr"]
        oa = openalex_record(p["doi"]) if p["doi"] else None
        venue = clean_title((cr.get("container-title") or [None])[0] if cr else None) or clean_title(w.get("journal"))
        d_online, d_online_src = online_date(cr)
        d_issue = (cr_date(cr, "published-print") or cr_date(cr, "issued")) if cr else w.get("publication_date")
        abstract, abstract_src = abstract_text(cr, oa)
        oa_info = (oa or {}).get("open_access") or {}
        row = {
            "doi": p["doi"],
            "title": clean_title((cr.get("title") or [None])[0] if cr else None) or w.get("title"),
            "type": (cr or {}).get("type") or w.get("type"),
            "venue": venue,
            "year": int((d_online or d_issue or "0")[:4]) or None,
            "date_online": d_online,
            "date_online_source": d_online_src,
            "date_issue": d_issue,
            "date_created": cr_date(cr, "created") if cr else None,
            "n_authors": len(p["authors"]),
            "my_position": p["position"],
            "position_bucket": position_bucket(p["position"]),
            "matched_by": p["match"],
            "author_source": p["author_source"],
            "citations_openalex": (oa or {}).get("cited_by_count"),
            "citations_crossref": (cr or {}).get("is-referenced-by-count"),
            "is_open_access": oa_info.get("is_oa"),
            "oa_url": oa_info.get("oa_url"),
            "has_abstract": bool(abstract),
        }
        rows.append(row)
        pubs.append({
            **{k: row[k] for k in ("doi", "title", "type", "venue", "year", "date_online",
                                   "date_online_source", "date_issue", "date_created", "my_position",
                                   "citations_openalex", "citations_crossref", "is_open_access", "oa_url")},
            "url": f"https://doi.org/{p['doi']}" if p["doi"] else w.get("url"),
            "authors": [{"name": a["name"], "orcid": a["orcid"], "is_me": i == p["position"]}
                        for i, a in enumerate(p["authors"], 1)],
            "volume": (cr or {}).get("volume"),
            "issue": (cr or {}).get("issue"),
            "pages": (cr or {}).get("page") or (cr or {}).get("article-number"),
            "publisher": (cr or {}).get("publisher"),
            "abstract": abstract,
            "abstract_source": abstract_src,
            "openalex_id": (oa or {}).get("id"),
            "fwci": (oa or {}).get("fwci"),
            "primary_topic": ((oa or {}).get("primary_topic") or {}).get("display_name"),
            "keywords": [k.get("display_name") for k in (oa or {}).get("keywords", [])],
            "citations_by_year": {c["year"]: c["cited_by_count"] for c in (oa or {}).get("counts_by_year", [])},
        })

    # Google Scholar: citation counts for matched papers, and entries ORCID lacks.
    scholar = load_scholar()
    scholar_only = []
    if scholar:
        matched = match_scholar([r["title"] for r in rows], scholar["articles"])
        for i, (row, pub) in enumerate(zip(rows, pubs)):
            sa = matched.get(i)
            row["citations_scholar"] = (sa["citations"] or 0) if sa else None
            pub["citations_scholar"] = row["citations_scholar"]
            pub["scholar_url"] = sa["scholar_url"] if sa else None
        matched_ids = {id(a) for a in matched.values()}
        for a in scholar["articles"]:
            if id(a) not in matched_ids:
                venue = a["venue"] or ""
                rec = {**a, "kind": "preprint" if any(h in venue.lower() for h in PREPRINT_HINTS) else "other"}
                meta = other_work_meta(a)
                if meta:
                    authors = [{**au, "is_me": au.get("orcid") == orcid_id or is_my_name(au["name"], my_names)}
                               for au in meta["authors"]]
                    pos = next((i for i, au in enumerate(authors, 1) if au["is_me"]), None)
                    rec.update({
                        "authors_full": authors,
                        "my_position": pos,
                        "position_bucket": position_bucket(pos),
                        "doi": meta.get("doi"),
                        "url": meta.get("url") or a["scholar_url"],
                        "date": meta.get("date"),
                        "venue_resolved": meta.get("venue"),
                        "work_type": meta.get("type"),
                        "metadata_source": meta["source"],
                    })
                    if meta.get("type") in ("preprint", "posted-content"):
                        rec["kind"] = "preprint"
                else:
                    rec.update({"authors_full": None, "my_position": None, "position_bucket": "unresolved",
                                "metadata_source": None})
                scholar_only.append(rec)
        (DATA / "other_works.json").write_text(json.dumps(scholar_only, indent=2, ensure_ascii=False),
                                               encoding="utf-8")
    else:
        for row in rows:
            row["citations_scholar"] = None

    # Preprints (on ORCID or only on Scholar) are listed separately and are not
    # part of any count below.
    for row, pub in zip(rows, pubs):
        row["is_preprint"] = pub["is_preprint"] = is_preprint(row["type"], row["venue"])
    all_rows = rows
    rows = [r for r in all_rows if not r["is_preprint"]]
    # Everything only on Scholar goes here too (kind "other" = not recognisably a
    # preprint, e.g. a garbled venue); add it to ORCID to have it counted.
    preprints = [{**p, "listed_on": "orcid", "kind": "preprint"} for p in pubs if p["is_preprint"]]
    pubs = [p for p in pubs if not p["is_preprint"]]
    for a in scholar_only:
        if any(p["doi"] and p["doi"].lower() == (a.get("doi") or "").lower() for p in preprints):
            continue
        preprints.append({
            "doi": a.get("doi"), "title": a["title"], "type": a.get("work_type") or "preprint",
            "venue": a.get("venue_resolved") or a["venue"], "year": a["year"], "date_online": a.get("date"),
            "my_position": a.get("my_position"), "citations_scholar": a.get("citations"),
            "url": a.get("url") or a["scholar_url"], "scholar_url": a["scholar_url"],
            "authors": a.get("authors_full"), "authors_text": a["authors"], "listed_on": "scholar",
            "kind": a["kind"],
        })
    preprints.sort(key=lambda r: r.get("date_online") or str(r.get("year") or ""), reverse=True)
    (DATA / "preprints.json").write_text(json.dumps(preprints, indent=2, ensure_ascii=False), encoding="utf-8")

    journals = [r for r in rows if r["type"] == "journal-article"]
    other = [r for r in rows if r["type"] != "journal-article"]
    pr_counts, pr_unresolved = peer_review_venues()
    summary = {
        "orcid_id": orcid_id,
        "total_publications": len(rows),
        "author_position": dict(Counter(r["position_bucket"] for r in rows)),
        "author_position_journal_articles": dict(Counter(r["position_bucket"] for r in journals)),
        "by_type": dict(Counter(r["type"] for r in rows)),
        "unique_journals": len({r["venue"] for r in journals if r["venue"]}),
        "journals": dict(Counter(r["venue"] for r in journals).most_common()),
        "other_venues": dict(Counter(f'{r["venue"]} ({r["type"]})' for r in other).most_common()),
        "citations": citation_summary(rows),
        "scholar_profile": {k: scholar[k] for k in ("name", "profile_url", "metrics", "citations_per_year",
                                                    "article_count", "fetched_at")} if scholar else None,
        "scholar_only_works": {"total": len(scholar_only),
                               "preprints": sum(a["kind"] == "preprint" for a in scholar_only)},
        "preprints_excluded": len(preprints),
        "name_variants_matched": sorted(my_names),
        "unresolved": [r["doi"] or r["title"] for r in rows if r["position_bucket"] == "unresolved"],
        "peer_reviews": {
            "total": sum(pr_counts.values()),
            "unique_journals": len([k for k in pr_counts if not k.startswith("[unresolved]")]),
            "by_journal": pr_counts,
            "unresolved_venues": pr_unresolved,
        },
    }
    for k in ("author_position", "author_position_journal_articles"):
        summary[k] = {b: summary[k].get(b, 0) for b in ("first", "second", "third_or_later", "unresolved")}

    (DATA / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    with open(DATA / "authorship.csv", "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=list(all_rows[0]))
        wr.writeheader()
        wr.writerows(sorted(all_rows, key=lambda r: (r["is_preprint"], r["my_position"] or 99, str(r["year"]))))
    # Newest first by Crossref 'created' (DOI registration): always a full date and
    # present on every Crossref record. date_online remains the date to display.
    pubs.sort(key=lambda r: r["date_created"] or r["date_online"] or r["date_issue"] or "", reverse=True)
    (DATA / "publications.json").write_text(json.dumps(pubs, indent=2, ensure_ascii=False), encoding="utf-8")
    (DATA / "peer_reviews_public.json").write_text(json.dumps(peer_reviews_public(), indent=2, ensure_ascii=False),
                                                   encoding="utf-8")

    ap = summary["author_position"]
    print(f"Publications: {len(rows)}  ({', '.join(f'{v} {k}' for k, v in summary['by_type'].items())})"
          f"  [+{len(preprints)} preprints, not counted]")
    print(f"  First author:     {ap['first']}")
    print(f"  Second author:    {ap['second']}")
    print(f"  Third or later:   {ap['third_or_later']}")
    if ap["unresolved"]:
        print(f"  Unresolved:       {ap['unresolved']}  -> {summary['unresolved']}")
    print(f"\nJournals ({summary['unique_journals']} unique, {len(journals)} articles):")
    for name, n in summary["journals"].items():
        print(f"  {n:>3}  {name}")
    if other:
        print("\nOther venues:")
        for name, n in summary["other_venues"].items():
            print(f"  {n:>3}  {name}")
    print("\nCitations:")
    for src, c in summary["citations"].items():
        if c["papers_with_data"]:
            print(f"  {src:9} total {c['total']:>4}  h-index {c['h_index']}  i10 {c['i10_index']}"
                  f"  ({c['papers_with_data']} papers)")
    if scholar:
        m = scholar["metrics"]
        print(f"  scholar profile (all {scholar['article_count']} entries): citations "
              f"{m['citations']['all']}  h-index {m['h_index']['all']}  i10 {m['i10_index']['all']}")
        print(f"\nOn Scholar but not ORCID ({len(scholar_only)}; saved to data/other_works.json):")
        for a in scholar_only:
            n = len(a["authors_full"]) if a.get("authors_full") else "?"
            print(f"  [{a['kind']:8}] {a['year']}  pos {a['my_position'] or '?'}/{n}  {a['title'][:60]}"
                  f"  ({(a.get('venue_resolved') or a['venue'] or 'no venue')[:30]}; via {a['metadata_source'] or 'scholar only'})")
    pr = summary["peer_reviews"]
    if pr["total"]:
        print(f"\nPeer reviews ({pr['total']} reviews, {pr['unique_journals']} unique journals):")
        for name, n in pr["by_journal"].items():
            print(f"  {n:>3}  {name}")
        if pr["unresolved_venues"]:
            print("  (run fetch_journals.py to resolve the [unresolved] ISSNs)")


if __name__ == "__main__":
    main()
