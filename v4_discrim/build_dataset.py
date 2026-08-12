#!/usr/bin/env python3
"""Item 1a: freeze the two-arm discrimination set -- the N bugs (default 50) that item 1 runs.

A bug is eligible iff BOTH arms are buildable and it is crash-arm scorable:
  * poc + meta.json + ground_truth.json present and parse (select_bugs.runnable_bugs),
  * meta.json carries BOTH vuln_commit and fix_commit (the two arms),
  * source host is fetchable (heptapod/graphicsmagick excluded, like the main pipeline),
  * gt_adapter yields >=1 usable gold frame (reward_v4 needs >=1 gold frame to score the
    crash arm; a bug with only sanitizer/infra frames can't be scored, so drop it here).

Selection is deterministic (seeded shuffle) with a per-project cap so the set spans many
projects instead of piling onto skia/ffmpeg. Writes dataset.json = the frozen bug-id list +
params, so the sweep and the metric always agree on which 50 bugs are "the experiment".

  python3 build_dataset.py                       # 50 bugs, seed 0, <=3 per project
  python3 build_dataset.py --n 50 --max-per-project 3 --seed 0
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "lib"))

from select_bugs import runnable_bugs
from gt_adapter import load_adapted

# Flaky hosts with identical-sha mirrors (see build_patch_corpus.py).
_MIRRORS = {"git.ffmpeg.org/ffmpeg.git": "https://github.com/FFmpeg/FFmpeg.git"}


def _mirror(repo: str) -> str:
    for needle, m in _MIRRORS.items():
        if needle in repo:
            return m
    return repo


def _run(args, cwd=None, timeout=600):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def is_adjacent(meta: dict, scratch: Path) -> bool | None:
    """True iff ARVO's verified-crash vuln_commit == fix_commit^ (the fix commit's parent), i.e.
    the crash and fix revisions are ADJACENT. Only then is fix^ both the ARVO-verified crashing
    tree AND the minimal-diff base for a clean two-arm normalization (see PROGRESS.md item 2b).
    Returns None on fetch failure (can't decide). Multi-sha fixes use the first sha as the head."""
    vuln = meta.get("vuln_commit")
    fix = meta.get("fix_commit")
    if not vuln or not fix:
        return False
    fix_head = fix.split()[0]
    repo = _mirror(meta["repo_addr"])
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], cwd=scratch)
    _run(["git", "remote", "add", "origin", repo], cwd=scratch)
    r = _run(["git", "fetch", "-q", "--depth", "2", "origin", fix_head], cwd=scratch)
    if r.returncode != 0:  # host rejects sha-fetch -> full clone to resolve the parent
        shutil.rmtree(scratch, ignore_errors=True)
        if _run(["git", "clone", "-q", repo, str(scratch)], timeout=1800).returncode != 0:
            return None
        if _run(["git", "cat-file", "-e", fix_head], cwd=scratch).returncode != 0:
            _run(["git", "fetch", "-q", "origin", fix_head], cwd=scratch)
    pr = _run(["git", "rev-parse", f"{fix_head}^"], cwd=scratch)
    if pr.returncode != 0:
        return None
    return pr.stdout.strip() == vuln


def eligible_bugs(data_dir: Path) -> list[dict]:
    out = []
    for bug in runnable_bugs(data_dir, skip_unfetchable=True):
        meta = json.loads((bug["path"] / "meta.json").read_text())
        if not meta.get("vuln_commit") or not meta.get("fix_commit"):
            continue
        if load_adapted(bug["path"]) is None:  # no usable gold frame -> crash arm unscorable
            continue
        out.append(bug)
    return out


def pick(bugs: list[dict], *, n: int, max_per_project: int, seed: int) -> list[dict]:
    order = list(bugs)
    random.Random(seed).shuffle(order)
    per: dict[str, int] = {}
    chosen: list[dict] = []
    for b in order:
        proj = b["project"] or "?"
        if per.get(proj, 0) >= max_per_project:
            continue
        chosen.append(b)
        per[proj] = per.get(proj, 0) + 1
        if len(chosen) >= n:
            break
    # If the per-project cap starved us below n, backfill ignoring the cap.
    if len(chosen) < n:
        have = {b["id"] for b in chosen}
        for b in order:
            if b["id"] in have:
                continue
            chosen.append(b)
            if len(chosen) >= n:
                break
    return chosen


def pick_adjacent(bugs: list[dict], *, n: int, max_per_project: int, seed: int,
                  scratch: Path, cache_path: Path) -> list[dict]:
    """Like pick(), but only accepts bugs whose crash and fix revisions are ADJACENT
    (vuln_commit == fix^). Checks candidates in shuffled order until n are found, so we pay the
    per-bug git fetch only for as many as needed. Results cached to cache_path (bug_id -> bool|null)
    so re-runs / --seed sweeps are cheap and resumable."""
    cache: dict[str, bool | None] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
    order = list(bugs)
    random.Random(seed).shuffle(order)
    per: dict[str, int] = {}
    chosen: list[dict] = []
    checked = 0
    for b in order:
        if len(chosen) >= n:
            break
        proj = b["project"] or "?"
        if per.get(proj, 0) >= max_per_project:
            continue
        bid = str(b["id"])
        if bid not in cache:
            meta = json.loads((b["path"] / "meta.json").read_text())
            try:
                cache[bid] = is_adjacent(meta, scratch)
            except Exception:
                cache[bid] = None
            checked += 1
            cache_path.write_text(json.dumps(cache, indent=0) + "\n")
            if checked % 10 == 0:
                print(f"  ...checked {checked} candidates, {len(chosen)}/{n} adjacent found", flush=True)
        if cache[bid] is True:
            chosen.append(b)
            per[proj] = per.get(proj, 0) + 1
            print(f"  + {bid} ({proj})  [{len(chosen)}/{n}]", flush=True)
    shutil.rmtree(scratch, ignore_errors=True)
    print(f"  adjacency checks: {checked} fetched, {sum(1 for v in cache.values() if v)} adjacent total in cache")
    return chosen


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=_HERE / "data", help="harvest dir (default ./data symlink)")
    ap.add_argument("--n", type=int, default=50, help="how many bugs in the discrimination set (default 50)")
    ap.add_argument("--max-per-project", type=int, default=3, help="cap bugs per project for diversity (default 3)")
    ap.add_argument("--seed", type=int, default=0, help="selection seed (default 0, reproducible)")
    ap.add_argument("--out", type=Path, default=_HERE / "dataset.json")
    ap.add_argument("--adjacent", action="store_true",
                    help="require vuln_commit == fix^ (adjacent crash/fix revisions) -- needed for a "
                         "sound normalized two-arm set (item 2/3). Costs a git fetch per candidate.")
    ap.add_argument("--adjacency-cache", type=Path, default=_HERE / "adjacency_cache.json")
    ap.add_argument("--scratch", type=Path, default=_HERE / "_adjcheck")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not a.data.is_dir():
        raise SystemExit(f"{a.data}: not a directory")
    pool = eligible_bugs(a.data)
    if len(pool) < a.n:
        print(f"WARNING: only {len(pool)} eligible bugs (< requested {a.n}); using all of them")
    if a.adjacent:
        print(f"selecting {a.n} ADJACENT bugs (vuln == fix^) from {len(pool)} eligible; git-checking as needed...")
        chosen = pick_adjacent(pool, n=a.n, max_per_project=a.max_per_project, seed=a.seed,
                               scratch=a.scratch, cache_path=a.adjacency_cache)
        if len(chosen) < a.n:
            print(f"WARNING: only {len(chosen)} adjacent bugs found (< requested {a.n})")
    else:
        chosen = pick(pool, n=a.n, max_per_project=a.max_per_project, seed=a.seed)

    by_proj: dict[str, int] = {}
    by_san: dict[str, int] = {}
    for b in chosen:
        by_proj[b["project"]] = by_proj.get(b["project"], 0) + 1
        by_san[b["sanitizer"]] = by_san.get(b["sanitizer"], 0) + 1
    print(f"eligible pool: {len(pool)} bugs")
    print(f"selected: {len(chosen)} bugs across {len(by_proj)} projects")
    print(f"  sanitizers: {dict(sorted(by_san.items(), key=lambda kv: -kv[1]))}")
    print(f"  top projects: {dict(sorted(by_proj.items(), key=lambda kv: -kv[1])[:8])}")

    if a.dry_run:
        return
    payload = {
        "bugs": [b["id"] for b in chosen],
        "params": {"n": a.n, "max_per_project": a.max_per_project, "seed": a.seed,
                   "eligible_pool": len(pool), "adjacent": a.adjacent},
        "meta": [{"id": b["id"], "project": b["project"], "sanitizer": b["sanitizer"]} for b in chosen],
    }
    a.out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {a.out} ({len(chosen)} bugs)")


if __name__ == "__main__":
    main()
