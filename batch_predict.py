#!/usr/bin/env python3
"""Phase 4d: run the single-bug crash-prediction loop (run_prediction.run) over
many harvested bugs, with batch bookkeeping.

3h's run_prediction.py already IS the working agent loop (prompt, locked sandbox,
validation, mid-run crash guards). This just drives it across a bug list and
records the outcome of each, producing the (prediction, ground-truth) pairs that
Phase 5 will score.

Design points (match the harvester's operational contract):
  * One bug's failure never kills the batch -- run() is wrapped, and a setup
    error (fetch/docker) is recorded as a failed manifest row, then we move on.
  * Resumable -- for k=1, skips bugs that already have a result.json (4b). For
    k>1 (pass@k sweeps, 5g) resume is sample-level: we count existing manifest
    rows per bug_id and run only the remaining k-done samples. --redo disables
    both.
  * pass@k -- --k N runs each bug N times (5g difficulty banding needs reward
    VARIANCE across rollouts of the SAME bug). Each manifest row carries a
    `sample` index; src cleanup waits for a bug's last sample.
  * Batch manifest at runs/<name>/manifest.jsonl (append + flush, like
    harvest.py) -- one JSON line per (bug, sample), live-tailable with `wc -l`.
  * Running cost total with --max-cost: abort cleanly before starting a bug that
    would push the batch over budget (model calls are billed).
  * --concurrency N runs N bugs in parallel (default 1 = serial). Parallelism is
    at the BUG level, never the sample level: run_prediction.run uses fixed
    per-bug dirs (data/<id>/src, .../answer/prediction.json), so two samples of
    one bug at once would collide -- a worker owns a whole bug and does its k
    samples in sequence. Worth it only with a high-RPM key (a paid Gemini
    Developer API key, `gemini/...` + GEMINI_API_KEY); Vertex's low RPM ceiling
    just turns concurrency into 429s.

Requires the msagent venv (run_prediction imports mini-swe-agent). Model access:
either a paid Gemini Developer API key (`--model gemini/gemini-3-flash-preview`,
GEMINI_API_KEY exported) or a Vertex model (`--model vertex_ai/gemini-3.5-flash`,
VERTEXAI_LOCATION + Vertex creds exported). The live baseline sweep (sweep3fp)
uses the paid key with gemini/gemini-3-flash-preview; a paid key is needed
because concurrency without a high per-minute (RPM/TPM) ceiling just 429s.

  cd ~/ARVO_intelligence && source .venv/bin/activate
  # paid Gemini Developer API key, 4 bugs in parallel:
  export GEMINI_API_KEY=...
  python batch_predict.py --limit 25 --shuffle --concurrency 4 \
      --model gemini/gemini-3-flash-preview --name shakeout
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "msagent_runner"))

from select_bugs import runnable_bugs
from fetch_source import cleanup_source
from run_prediction import run


class BatchState:
    """Shared, lock-guarded batch bookkeeping for concurrent bug workers.

    Concurrency is at the BUG level (one worker owns a whole bug and runs its k
    samples sequentially), because run_prediction.run uses fixed per-bug dirs
    (data/<id>/src, data/<id>/answer/prediction.json) -- two samples of the SAME
    bug in parallel would collide. Distinct bugs never share a dir, so the only
    cross-thread state is the append-only manifest + the running cost/count
    totals, all serialized through one lock here."""

    def __init__(self, mf, max_cost: float | None):
        self.mf = mf
        self.max_cost = max_cost
        self.lock = threading.Lock()
        self.total_cost = 0.0
        self.counts = {"submitted": 0, "predicted": 0, "no_prediction": 0, "errored": 0}
        self.stop = False
        self.started = time.time()

    def should_stop(self) -> bool:
        """True once the running cost has crossed --max-cost. Concurrency makes the
        guard approximate: bugs already in flight finish their current sample, so
        the batch can overshoot by up to (concurrency) samples -- acceptable, and
        the point of --max-cost is a soft ceiling, not an exact cutoff."""
        with self.lock:
            if self.max_cost is not None and self.total_cost >= self.max_cost:
                self.stop = True
            return self.stop

    def record(self, result: dict) -> tuple[float, dict]:
        """Append one (bug, sample) row and fold its cost/status into the totals.
        Under the lock so the manifest line, flush, and counters stay consistent
        across workers (and `wc -l manifest.jsonl` tracks live progress)."""
        with self.lock:
            self.mf.write(json.dumps(result) + "\n")
            self.mf.flush()
            self.total_cost += result.get("cost") or 0.0
            status = result.get("exit_status") or ""
            if status.startswith("batch-error") or status.startswith("crashed"):
                self.counts["errored"] += 1
            if status == "Submitted":
                self.counts["submitted"] += 1
            if result.get("prediction") is not None:
                self.counts["predicted"] += 1
            else:
                self.counts["no_prediction"] += 1
            return self.total_cost, dict(self.counts)


def _run_one_sample(bug: dict, sample: int, a: argparse.Namespace) -> dict:
    """One rollout of one bug. Mirrors the serial path's try/except: a setup-stage
    failure (fetch/docker) is recorded as a failed row so the batch continues."""
    try:
        result = run(bug["path"], model_name=a.model, step_limit=a.step_limit, cost_limit=a.cost_limit)
    except Exception as e:
        result = {
            "bug_id": bug["id"],
            "project": bug["project"],
            "sanitizer": bug["sanitizer"],
            "model": a.model,
            "exit_status": f"batch-error: {type(e).__name__}: {e}",
            "cost": 0.0,
            "prediction": None,
            "invalid_reason": f"{type(e).__name__}: {e}",
        }
        print(f"    [{bug['id']}] ERROR (recorded, batch continues): {type(e).__name__}: {e}", flush=True)
    result["sample"] = sample
    return result


def process_bug(i: int, n_bugs: int, bug: dict, already: int, a: argparse.Namespace, state: BatchState) -> None:
    """Run a single bug's remaining samples (already..k-1) sequentially. Owns the
    bug's data/<id>/ dir exclusively for its lifetime, then cleans up src."""
    if state.should_stop():
        return
    print(f"[{i}/{n_bugs}] {bug['id']} ({bug['project']}, {bug['sanitizer']}) "
          f"samples {already}..{a.k - 1}", flush=True)
    for s in range(already, a.k):
        if state.should_stop():
            print(f"    [{bug['id']}] stopping: running cost >= --max-cost ${a.max_cost:.2f}", flush=True)
            break
        total_cost, counts = state.record(_run_one_sample(bug, s, a))
        mins = (time.time() - state.started) / 60
        print(f"    [{bug['id']}] sample {s}: cost ${total_cost:.2f} | {counts} | elapsed {mins:.1f}m", flush=True)

    # cleanup only after a bug's last sample (all k rollouts reuse one src tree)
    if a.cleanup_src and cleanup_source(bug["path"] / "src"):
        print(f"    [{bug['id']}] cleaned up src/", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # --- bug selection (forwarded to select_bugs.runnable_bugs) ---
    ap.add_argument("--data", type=Path, default=REPO_ROOT / "data", help="harvest output dir (default ./data)")
    ap.add_argument("--sanitizer", choices=["asan", "msan", "ubsan"])
    ap.add_argument("--project")
    ap.add_argument("--limit", type=int, help="cap how many bugs to run")
    ap.add_argument("--shuffle", action="store_true", help="shuffle before --limit (diverse sample)")
    ap.add_argument("--seed", type=int, default=0, help="shuffle seed (default 0, reproducible)")
    ap.add_argument("--redo", action="store_true", help="re-run bugs that already have result.json (default: skip them)")
    ap.add_argument("--k", type=int, default=1, help="samples per bug for pass@k / difficulty banding (default 1)")
    # --- per-bug agent knobs (forwarded to run_prediction.run) ---
    ap.add_argument("--model", default="gemini/gemini-3-flash-preview", help="litellm model id (Phase-4 sweep standard; sweep3fp)")
    ap.add_argument("--step-limit", type=int, default=45)
    ap.add_argument("--cost-limit", type=float, default=3.0, help="per-bug cost cap")
    # --- batch controls ---
    ap.add_argument("--max-cost", type=float, help="abort the batch before a bug that would exceed this running total")
    ap.add_argument("--cleanup-src", action="store_true", help="delete data/<id>/src after each bug (disk hygiene; re-fetch is ~5s)")
    ap.add_argument("--concurrency", type=int, default=1, help="bugs to run in parallel (default 1 = serial; needs a high-RPM key, e.g. paid Gemini Developer API)")
    ap.add_argument("--name", default=None, help="run name -> runs/<name>/ (default: timestamp)")
    a = ap.parse_args()

    if not a.data.is_dir():
        raise SystemExit(f"{a.data}: not a directory")
    if a.k < 1:
        raise SystemExit(f"--k must be >= 1, got {a.k}")
    if a.concurrency < 1:
        raise SystemExit(f"--concurrency must be >= 1, got {a.concurrency}")

    # For k=1 keep the result.json skip; for k>1 resume is sample-level (below),
    # so select ALL matching bugs and let done_counts decide how many to run.
    skip_done = not a.redo and a.k == 1
    bugs = runnable_bugs(a.data, sanitizer=a.sanitizer, project=a.project, skip_done=skip_done)
    if a.shuffle:
        import random

        random.Random(a.seed).shuffle(bugs)
    if a.limit is not None:
        bugs = bugs[: a.limit]
    if not bugs:
        raise SystemExit("no runnable bugs match the selection (all done? try --redo)")

    run_name = a.name or time.strftime("%Y%m%d-%H%M%S")
    run_dir = REPO_ROOT / "runs" / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = run_dir / "manifest.jsonl"

    # Sample-level resume: count existing manifest rows per bug_id so a resumed
    # sweep runs only the remaining k-done samples. --redo ignores prior rows.
    done_counts: dict[str, int] = {}
    if manifest.exists() and not a.redo:
        for line in manifest.read_text().splitlines():
            if not line.strip():
                continue
            try:
                bid = str(json.loads(line).get("bug_id"))
            except json.JSONDecodeError:
                continue
            done_counts[bid] = done_counts.get(bid, 0) + 1

    print(f"batch '{run_name}': {len(bugs)} bugs x k={a.k} -> {manifest}")
    print(f"model={a.model} step_limit={a.step_limit} cost_limit=${a.cost_limit} "
          f"max_cost={a.max_cost} concurrency={a.concurrency}\n", flush=True)

    # Build the work list up front (skip bugs already at k samples), so the pool
    # only ever sees bugs that need work.
    work: list[tuple[int, dict, int]] = []
    for i, bug in enumerate(bugs, 1):
        already = 0 if a.redo else done_counts.get(str(bug["id"]), 0)
        if already >= a.k:
            print(f"[{i}/{len(bugs)}] {bug['id']} has {already}/{a.k} samples, skipping", flush=True)
            continue
        work.append((i, bug, already))

    with open(manifest, "a") as mf:  # append: keep history across resumed batches
        state = BatchState(mf, a.max_cost)
        # One task per bug; the pool caps how many run at once. concurrency=1 is
        # exactly the old serial behavior. Each worker owns its bug's dir, so the
        # only shared state is BatchState (lock-guarded). --max-cost is enforced
        # via state.should_stop(): queued bugs that haven't started yet return
        # immediately once the ceiling is crossed.
        with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
            futures = [ex.submit(process_bug, i, len(bugs), bug, already, a, state) for (i, bug, already) in work]
            for fut in as_completed(futures):
                fut.result()  # surface any unexpected worker exception

    counts, total_cost = state.counts, state.total_cost
    print(f"\nbatch '{run_name}' done. samples run: {counts['predicted'] + counts['no_prediction']}")
    print(f"  valid predictions: {counts['predicted']}   no prediction: {counts['no_prediction']}   errored: {counts['errored']}")
    print(f"  clean Submitted: {counts['submitted']}")
    print(f"  total cost: ${total_cost:.2f}")
    print(f"  manifest: {manifest}")


if __name__ == "__main__":
    main()
