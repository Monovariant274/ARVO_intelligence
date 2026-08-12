#!/usr/bin/env python3
"""Full-corpus usability scan: which harvested ARVO bugs can become a SOUND two-arm
normalized training env (item 2/3)?

A bug is USABLE iff it passes every gate the two-arm design needs:
  local (cheap, no network):
    1. poc present AND non-empty         (23 harvested bugs have a 0-byte poc)
    2. meta.json + ground_truth.json parse
    3. source host is fetchable          (heptapod/graphicsmagick excluded)
    4. meta carries BOTH vuln_commit and fix_commit (the two arms)
    5. gt_adapter yields >=1 usable gold frame (crash arm must be scorable)
  network (one git fetch per survivor, the expensive part):
    6. ADJACENT: vuln_commit == fix_commit^  -- the crash arm can only be based on
       fix^ soundly when fix^ IS the crashing commit. This is the ~9% gate.

Only bugs passing ALL SIX are emitted as the buildable set. (Patch-apply + image-build
is the FINAL usability proof -- that's the next phase, build_patch_corpus + build_shard_arvo.)

Resumable: reuses/extends adjacency_cache.json (bug_id -> bool|None) so prior checks and
interrupted runs are never repeated. Concurrent git fetches, each in its own scratch dir.

  python3 scan_usable.py --concurrency 8
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "lib"))

from select_bugs import runnable_bugs
from gt_adapter import load_adapted
from build_dataset import is_adjacent


def local_eligible(data: Path) -> tuple[list[dict], dict[str, int]]:
    """Gates 1-5 (no network). Returns (eligible rows, drop-reason counts)."""
    drops = {"empty_poc": 0, "missing_commit": 0, "no_gold_frame": 0}
    out = []
    for b in runnable_bugs(data, skip_unfetchable=True):
        p = b["path"]
        poc = p / "poc"
        if not poc.is_file() or poc.stat().st_size == 0:
            drops["empty_poc"] += 1
            continue
        meta = json.loads((p / "meta.json").read_text())
        if not (meta.get("vuln_commit") and meta.get("fix_commit")):
            drops["missing_commit"] += 1
            continue
        if load_adapted(p) is None:
            drops["no_gold_frame"] += 1
            continue
        b["meta"] = meta
        out.append(b)
    return out, drops


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=_HERE / "data")
    ap.add_argument("--cache", type=Path, default=_HERE / "adjacency_cache.json")
    ap.add_argument("--out", type=Path, default=_HERE / "discrim-env-images" / "usable_scan.json")
    ap.add_argument("--progress", type=Path, default=_HERE / "usable_scan_progress.txt")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--scratch-root", type=Path, default=_HERE / "_scanwork")
    a = ap.parse_args()

    t0 = time.time()
    eligible, drops = local_eligible(a.data)
    print(f"local eligibility: {len(eligible)} bugs pass gates 1-5  (dropped: {drops})", flush=True)

    cache: dict[str, bool | None] = {}
    if a.cache.exists():
        cache = json.loads(a.cache.read_text())
    lock = threading.Lock()
    a.scratch_root.mkdir(parents=True, exist_ok=True)

    todo = [b for b in eligible if str(b["id"]) not in cache]
    print(f"adjacency: {len(cache)} cached, {len(todo)} to check "
          f"(concurrency={a.concurrency})\n", flush=True)

    counter = {"done": 0}

    def check(b: dict):
        bid = str(b["id"])
        scratch = a.scratch_root / bid
        try:
            res = is_adjacent(b["meta"], scratch)
        except Exception:
            res = None
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        with lock:
            cache[bid] = res
            counter["done"] += 1
            n = counter["done"]
            if n % 25 == 0 or n == len(todo):
                adj = sum(1 for v in cache.values() if v is True)
                msg = (f"[{n}/{len(todo)}] checked  |  adjacent so far: {adj}  |  "
                       f"{(time.time()-t0)/60:.1f}m")
                print(msg, flush=True)
                a.progress.write_text(msg + "\n")
                a.cache.write_text(json.dumps(cache, indent=0) + "\n")
        return res

    if todo:
        with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
            for fut in as_completed([ex.submit(check, b) for b in todo]):
                fut.result()
    a.cache.write_text(json.dumps(cache, indent=0) + "\n")
    shutil.rmtree(a.scratch_root, ignore_errors=True)

    # Fully-usable = local-eligible AND adjacent.
    elig_ids = {str(b["id"]): b for b in eligible}
    usable = [{"bug_id": bid, "project": elig_ids[bid]["project"],
               "sanitizer": elig_ids[bid]["sanitizer"]}
              for bid in elig_ids if cache.get(bid) is True]
    unknown = [bid for bid in elig_ids if cache.get(bid) is None]

    report = {
        "n_local_eligible": len(eligible),
        "local_drops": drops,
        "n_adjacency_checked": len([b for b in elig_ids if b in cache]),
        "n_usable": len(usable),
        "n_non_adjacent": sum(1 for bid in elig_ids if cache.get(bid) is False),
        "n_unknown_fetch_failed": len(unknown),
        "elapsed_min": round((time.time() - t0) / 60, 1),
        "usable_bugs": sorted(usable, key=lambda r: r["bug_id"]),
        "unknown_bug_ids": sorted(unknown),
    }
    a.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nDONE in {report['elapsed_min']}m")
    print(f"  local-eligible : {report['n_local_eligible']}")
    print(f"  USABLE (adjacent + all gates): {report['n_usable']}")
    print(f"  non-adjacent   : {report['n_non_adjacent']}")
    print(f"  fetch-unknown  : {report['n_unknown_fetch_failed']}")
    print(f"  report -> {a.out}")


if __name__ == "__main__":
    main()
