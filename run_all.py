"""Daily update: run every step, report what changed, build public/ for the website.

Steps, all in pipeline/ (critical ones stop the run; the others warn and keep
the previous data):
    fetch_orcid.py        critical   works + peer reviews from ORCID
    fetch_crossref.py     critical   metadata for new DOIs (cached once per DOI)
    fetch_openalex.py     optional   citations / abstracts (refetched when stale)
    fetch_journals.py     optional   names for new peer-review ISSNs (cached)
    fetch_scholar.py      optional   Scholar profile: citations + new preprints
    summarize.py          critical   (1st pass writes the Scholar-only list)
    fetch_other_works.py  optional   full authors for new Scholar-only works (cached)
    summarize.py          critical   (2nd pass merges them)

Writes data/last_run.json (step status + changes) and public/ (site files only;
never raw API dumps or anything from .env). Exit code is non-zero only if a
critical step failed. In GitHub Actions the change report also goes to the job
summary.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PIPELINE = ROOT / "pipeline"
DATA = ROOT / "data"
PUBLIC = ROOT / "public"

STEPS = [
    ("fetch_orcid.py", True),
    ("fetch_crossref.py", True),
    ("fetch_openalex.py", False),
    ("fetch_journals.py", False),
    ("fetch_scholar.py", False),
    ("summarize.py", True),
    ("fetch_other_works.py", False),
    ("summarize.py", True),
]
# published name -> file in data/
PUBLIC_FILES = {
    "publications.json": "publications.json",
    "preprints.json": "preprints.json",
    "peer_reviews.json": "peer_reviews_public.json",
    "summary.json": "summary.json",
}


def load(name: str, default):
    p = DATA / name
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except json.JSONDecodeError:
        return default


def snapshot() -> dict:
    pubs = load("publications.json", [])
    pre = load("preprints.json", [])
    return {
        "works": {(p.get("doi") or p["title"]).lower(): p["title"] for p in pubs},
        "preprints": {(p.get("doi") or p["title"]).lower(): p["title"] for p in pre},
        "peer_reviews": {str(r["put_code"]): r.get("group_id") for r in load("peer_reviews.json", [])},
        "citations": {(p.get("doi") or p["title"]).lower(): p.get("citations_scholar") for p in pubs + pre},
        "scholar_total": ((load("summary.json", {}).get("scholar_profile") or {}).get("metrics") or {})
                         .get("citations", {}).get("all"),
    }


def diff(before: dict, after: dict) -> dict:
    new = lambda k: [after[k][x] for x in after[k] if x not in before[k]]  # noqa: E731
    gone = lambda k: [before[k][x] for x in before[k] if x not in after[k]]  # noqa: E731
    cite_changes = {
        after["works"].get(k) or after["preprints"].get(k): {"from": before["citations"].get(k), "to": v}
        for k, v in after["citations"].items()
        if k in before["citations"] and v is not None and v != before["citations"][k]
    }
    return {
        "new_works": new("works"),
        "removed_works": gone("works"),
        "new_preprints": new("preprints"),
        "new_peer_reviews": len(new("peer_reviews")),
        "removed_peer_reviews": len(gone("peer_reviews")),
        "citation_changes": cite_changes,
        "scholar_total": {"from": before["scholar_total"], "to": after["scholar_total"]},
    }


def run(script: str) -> tuple[bool, str]:
    t = time.time()
    p = subprocess.run([sys.executable, str(PIPELINE / script)], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    out = (p.stdout + p.stderr).strip()
    print(f"\n=== {script} ({time.time() - t:.1f}s, exit {p.returncode}) ===\n{out}")
    return p.returncode == 0, out.splitlines()[-1] if out else ""


def build_public(last_run: dict) -> None:
    PUBLIC.mkdir(exist_ok=True)
    for name, src in PUBLIC_FILES.items():
        if (DATA / src).exists():
            shutil.copy2(DATA / src, PUBLIC / name)
    (PUBLIC / "last_run.json").write_text(json.dumps(last_run, indent=2, ensure_ascii=False), encoding="utf-8")
    # The viewer page (site/index.html) reads the JSON files next to it.
    shutil.copy2(ROOT / "site" / "index.html", PUBLIC / "index.html")
    (PUBLIC / ".nojekyll").write_text("", encoding="utf-8")


def report_md(last_run: dict) -> str:
    c = last_run["changes"]
    lines = [f"## Publication data update ({last_run['finished_at']})", "", "| Step | Status |", "|---|---|"]
    lines += [f"| {s['step']} | {'ok' if s['ok'] else ('FAILED' if s['critical'] else 'skipped: ' + s['last_line'][:80])} |"
              for s in last_run["steps"]]
    lines += ["", f"- New works: {len(c['new_works'])}" + "".join(f"\n  - {t}" for t in c["new_works"]),
              f"- New preprints: {len(c['new_preprints'])}" + "".join(f"\n  - {t}" for t in c["new_preprints"]),
              f"- New peer reviews: {c['new_peer_reviews']}",
              f"- Scholar total citations: {c['scholar_total']['from']} -> {c['scholar_total']['to']}",
              f"- Papers with citation changes: {len(c['citation_changes'])}"]
    if c["removed_works"] or c["removed_peer_reviews"]:
        lines.append(f"- Removed: {len(c['removed_works'])} works, {c['removed_peer_reviews']} peer reviews")
    return "\n".join(lines) + "\n"


def main() -> None:
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    before = snapshot()
    steps, failed_critical = [], False
    for script, critical in STEPS:
        if failed_critical:
            break
        ok, last = run(script)
        steps.append({"step": script, "critical": critical, "ok": ok, "last_line": last})
        failed_critical = critical and not ok

    last_run = {
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": not failed_critical,
        "steps": steps,
        "changes": diff(before, snapshot()),
    }
    (DATA / "last_run.json").write_text(json.dumps(last_run, indent=2, ensure_ascii=False), encoding="utf-8")
    if not failed_critical:
        build_public(last_run)

    md = report_md(last_run)
    print("\n" + md)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md)
    sys.exit(1 if failed_critical else 0)


if __name__ == "__main__":
    main()
