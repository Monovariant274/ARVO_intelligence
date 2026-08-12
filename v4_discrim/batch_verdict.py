#!/usr/bin/env python3
"""Item 1d: drive the v4 two-arm verdict rollout (run_verdict.run) over the frozen
discrimination set -> one manifest.jsonl the metric (discriminability.py) reads.

Runs, per bug in dataset.json, BOTH arms x k samples:  N bugs x 2 arms x k = the sweep.
Mirrors batch_predict.py's operational contract:

  * one rollout failing never kills the batch (run wrapped; setup errors recorded as rows),
  * RESUMABLE at the (bug, arm) grain -- counts existing manifest rows per (bug_id, arm) and
    runs only the remaining k-done samples; --redo ignores prior rows,
  * CONCURRENCY at the BUG level only. A worker owns a whole bug and runs its two arms'
    samples sequentially, because run_verdict uses fixed per-bug/arm dirs
    (data/<id>/src_<arm>, .../answer_<arm>) -- two rollouts of the same (bug,arm) at once
    would collide. Distinct bugs never share a dir. Needs a high-RPM key for concurrency>1.
  * --max-cost soft ceiling; src_<arm> cleaned after a bug's LAST sample of that arm.

  cd v4_discrim && export GEMINI_API_KEY=...
  python3 batch_verdict.py --k 3 --concurrency 4 --name v4disc \
      --model gemini/gemini-3-flash-preview
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "vendor"))
sys.path.insert(0, str(_HERE / "lib"))

from fetch_source import cleanup_source
from run_verdict import run as run_src
from run_verdict_img import run as run_img

ARMS = ("crash", "fix")


def load_dataset(data_dir: Path, dataset_json: Path) -> list[dict]:
    ids = json.loads(dataset_json.read_text())["bugs"]
    bugs = []
    for bid in ids:
        bug_dir = data_dir / str(bid)
        meta_path = bug_dir / "meta.json"
        if not meta_path.exists():
            print(f"  WARN {bid}: no meta.json under {data_dir}, skipping")
            continue
        meta = json.loads(meta_path.read_text())
        bugs.append({"id": str(bid), "path": bug_dir, "project": meta.get("project"),
                     "sanitizer": meta.get("sanitizer")})
    return bugs


class BatchState:
    def __init__(self, mf, max_cost: float | None):
        self.mf = mf
        self.max_cost = max_cost
        self.lock = threading.Lock()
        self.total_cost = 0.0
        self.counts = {"crash_says_crash": 0, "fix_says_crash": 0, "invalid": 0, "errored": 0, "rows": 0}
        self.stop = False
        self.started = time.time()

    def should_stop(self) -> bool:
        with self.lock:
            if self.max_cost is not None and self.total_cost >= self.max_cost:
                self.stop = True
            return self.stop

    def record(self, result: dict) -> tuple[float, dict]:
        with self.lock:
            self.mf.write(json.dumps(result) + "\n")
            self.mf.flush()
            self.total_cost += result.get("cost") or 0.0
            self.counts["rows"] += 1
            status = result.get("exit_status") or ""
            if status.startswith("batch-error") or status.startswith("crashed"):
                self.counts["errored"] += 1
            if not result.get("valid", False):
                self.counts["invalid"] += 1
            elif result.get("crashes") is True:
                key = "crash_says_crash" if result.get("arm") == "crash" else "fix_says_crash"
                self.counts[key] += 1
            return self.total_cost, dict(self.counts)


def _run_one(bug: dict, arm: str, sample: int, a: argparse.Namespace) -> dict:
    # --images: agent runs INSIDE the normalized per-bug image (item 3); else raw-source item 1.
    run = run_img if a.images else run_src
    try:
        return run(bug["path"], arm, model_name=a.model, step_limit=a.step_limit,
                   cost_limit=a.cost_limit, sample=sample, traj_dir=a.traj_dir)
    except Exception as e:
        print(f"    [{bug['id']} {arm} s{sample}] ERROR (recorded, batch continues): "
              f"{type(e).__name__}: {e}", flush=True)
        return {
            "bug_id": bug["id"], "project": bug["project"], "sanitizer": bug["sanitizer"],
            "arm": arm, "sample": sample, "expected_crashes": (arm == "crash"),
            "model": a.model, "exit_status": f"batch-error: {type(e).__name__}: {e}",
            "cost": 0.0, "verdict_raw": None, "crashes": None, "reward": -1.0, "valid": False,
            "invalid_reason": f"{type(e).__name__}: {e}",
        }


def process_bug(i: int, n: int, bug: dict, done: dict[tuple[str, str], int],
                a: argparse.Namespace, state: BatchState) -> None:
    if state.should_stop():
        return
    print(f"[{i}/{n}] {bug['id']} ({bug['project']}, {bug['sanitizer']})", flush=True)
    for arm in ARMS:
        already = 0 if a.redo else done.get((bug["id"], arm), 0)
        for s in range(already, a.k):
            if state.should_stop():
                print(f"    [{bug['id']}] stopping: running cost >= --max-cost ${a.max_cost:.2f}", flush=True)
                break
            total_cost, counts = state.record(_run_one(bug, arm, s, a))
            mins = (time.time() - state.started) / 60
            print(f"    [{bug['id']} {arm} s{s}] cost ${total_cost:.2f} | {counts} | {mins:.1f}m", flush=True)
        if a.cleanup_src and cleanup_source(bug["path"] / f"src_{arm}"):
            print(f"    [{bug['id']}] cleaned up src_{arm}/", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=_HERE / "data")
    ap.add_argument("--dataset", type=Path, default=_HERE / "dataset.json", help="frozen bug set from build_dataset.py")
    ap.add_argument("--k", type=int, default=3, help="samples per (bug, arm) (default 3)")
    ap.add_argument("--redo", action="store_true", help="re-run even (bug,arm) already at k samples")
    ap.add_argument("--model", default="gemini/gemini-3-flash-preview")
    ap.add_argument("--step-limit", type=int, default=30)
    ap.add_argument("--cost-limit", type=float, default=1.0, help="per-rollout cost cap")
    ap.add_argument("--max-cost", type=float, help="abort before a rollout that would exceed this running total")
    ap.add_argument("--cleanup-src", action="store_true", help="delete data/<id>/src_<arm> after each arm (disk hygiene)")
    ap.add_argument("--images", action="store_true",
                    help="item 3: run each rollout INSIDE the normalized per-bug image "
                         "(run_verdict_img) instead of raw fetched source in arvo-sandbox:base")
    ap.add_argument("--concurrency", type=int, default=1, help="bugs in parallel (default 1; needs a high-RPM key)")
    ap.add_argument("--name", default=None, help="run name -> runs/<name>/ (default: timestamp)")
    ap.add_argument("--limit", type=int, help="cap number of bugs (debug)")
    a = ap.parse_args()

    if not a.data.is_dir():
        raise SystemExit(f"{a.data}: not a directory")
    if not a.dataset.exists():
        raise SystemExit(f"{a.dataset}: not found (run build_dataset.py first)")
    if a.k < 1 or a.concurrency < 1:
        raise SystemExit("--k and --concurrency must be >= 1")

    bugs = load_dataset(a.data, a.dataset)
    if a.limit is not None:
        bugs = bugs[: a.limit]
    if not bugs:
        raise SystemExit("no bugs resolved from dataset.json")

    run_name = a.name or time.strftime("%Y%m%d-%H%M%S")
    run_dir = _HERE / "runs" / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = run_dir / "manifest.jsonl"
    # Every rollout's full trajectory is saved here as <bug>_<arm>_s<sample>.json (per-run, never
    # clobbered across samples/models); each manifest row records its "traj_path".
    a.traj_dir = run_dir / "trajectories"
    a.traj_dir.mkdir(parents=True, exist_ok=True)

    # (bug, arm)-level resume: count existing rows per (bug_id, arm).
    done: dict[tuple[str, str], int] = {}
    if manifest.exists() and not a.redo:
        for line in manifest.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (str(row.get("bug_id")), row.get("arm"))
            done[key] = done.get(key, 0) + 1

    total_rollouts = sum(max(0, a.k - done.get((b["id"], arm), 0)) for b in bugs for arm in ARMS)
    print(f"batch '{run_name}': {len(bugs)} bugs x 2 arms x k={a.k} -> {manifest}")
    print(f"  {total_rollouts} rollouts remaining after resume")
    print(f"  model={a.model} step_limit={a.step_limit} cost_limit=${a.cost_limit} "
          f"max_cost={a.max_cost} concurrency={a.concurrency}\n", flush=True)

    with open(manifest, "a") as mf:
        state = BatchState(mf, a.max_cost)
        with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
            futures = [ex.submit(process_bug, i, len(bugs), bug, done, a, state)
                       for i, bug in enumerate(bugs, 1)]
            for fut in as_completed(futures):
                fut.result()

    c = state.counts
    print(f"\nbatch '{run_name}' done. rows: {c['rows']}  (errored {c['errored']}, invalid {c['invalid']})")
    print(f"  crash-arm says-crash: {c['crash_says_crash']}   fix-arm says-crash: {c['fix_says_crash']}")
    print(f"  total cost: ${state.total_cost:.2f}   manifest: {manifest}")
    print(f"  next: python3 discriminability.py {manifest}")


if __name__ == "__main__":
    main()
