#!/usr/bin/env python3
"""Lists runnable bugs from a harvest output dir (default ./data).

A bug is *runnable* iff its folder is complete: `poc` + `ground_truth.json` +
`meta.json` all present and the JSON parses. Requiring `meta.json` (written
last by harvest.py) makes this safe to run against a live harvest: an
in-flight, half-written bug folder is simply not listed yet. `ok(no-poc)`
bugs fail the poc check and are excluded the same way.

Library use (batch runner): `runnable_bugs(data_dir, ...)` -> list of dicts.
CLI use: prints one bug id per line (or full paths with --paths).

  python3 select_bugs.py                          # all runnable bug ids
  python3 select_bugs.py --sanitizer asan --limit 20
  python3 select_bugs.py --project skia --paths
  python3 select_bugs.py --skip-done              # exclude bugs with result.json
  python3 select_bugs.py --shuffle --seed 0 --limit 30   # diverse sample
  python3 select_bugs.py --count                  # summary only
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


# Hosts we cannot materialize source from, so their bugs can never run. Heptapod
# (foss.heptapod.net) serves Mercurial: git can't fetch it, AND the recorded
# vuln_commit is an hg changeset that no longer resolves on the live repo (history
# stripped) and doesn't map to the GitHub mirror's git SHAs -- the source only ever
# existed in the discarded ARVO Docker image. Affects all 77 graphicsmagick bugs.
UNFETCHABLE_HOST_MARKERS = ("heptapod",)


def _load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _is_unfetchable(meta: dict) -> bool:
    addr = (meta.get("repo_addr") or "").lower()
    return any(marker in addr for marker in UNFETCHABLE_HOST_MARKERS)


def runnable_bugs(
    data_dir: Path,
    *,
    sanitizer: str | None = None,
    project: str | None = None,
    skip_done: bool = False,
    skip_unfetchable: bool = True,
) -> list[dict]:
    bugs = []
    for bug_dir in sorted(data_dir.iterdir()):
        if not bug_dir.is_dir():
            continue
        if not (bug_dir / "poc").is_file():
            continue
        meta = _load_json(bug_dir / "meta.json")
        gt = _load_json(bug_dir / "ground_truth.json")
        if meta is None or gt is None:
            continue
        if skip_unfetchable and _is_unfetchable(meta):
            continue
        if sanitizer and meta.get("sanitizer") != sanitizer:
            continue
        if project and meta.get("project") != project:
            continue
        if skip_done and (bug_dir / "result.json").is_file():
            continue
        bugs.append(
            {
                "id": bug_dir.name,
                "path": bug_dir,
                "project": meta.get("project"),
                "sanitizer": meta.get("sanitizer"),
            }
        )
    return bugs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=Path("data"), help="harvest output dir (default ./data)")
    ap.add_argument("--sanitizer", choices=["asan", "msan", "ubsan"])
    ap.add_argument("--project")
    ap.add_argument("--skip-done", action="store_true", help="exclude bugs that already have result.json")
    ap.add_argument("--include-unfetchable", action="store_true",
                    help="include bugs whose source can't be fetched (heptapod/graphicsmagick); excluded by default")
    ap.add_argument("--shuffle", action="store_true", help="shuffle before applying --limit (diverse sample)")
    ap.add_argument("--seed", type=int, default=0, help="shuffle seed (default 0, reproducible)")
    ap.add_argument("--limit", type=int, help="cap the list")
    ap.add_argument("--paths", action="store_true", help="print bug folder paths instead of ids")
    ap.add_argument("--count", action="store_true", help="print a summary instead of the list")
    a = ap.parse_args()

    if not a.data.is_dir():
        raise SystemExit(f"{a.data}: not a directory")
    bugs = runnable_bugs(a.data, sanitizer=a.sanitizer, project=a.project, skip_done=a.skip_done,
                         skip_unfetchable=not a.include_unfetchable)
    if a.shuffle:
        random.Random(a.seed).shuffle(bugs)
    if a.limit is not None:
        bugs = bugs[: a.limit]

    if a.count:
        by_san: dict[str, int] = {}
        projects = set()
        for b in bugs:
            by_san[b["sanitizer"]] = by_san.get(b["sanitizer"], 0) + 1
            projects.add(b["project"])
        print(f"{len(bugs)} runnable bugs across {len(projects)} projects")
        for san, n in sorted(by_san.items(), key=lambda kv: -kv[1]):
            print(f"  {san}: {n}")
    else:
        for b in bugs:
            print(b["path"] if a.paths else b["id"])


if __name__ == "__main__":
    main()
