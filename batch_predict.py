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
  * Resumable -- by default skips bugs that already have a result.json (4b), so
    re-running continues where it left off. --redo disables that.
  * Batch manifest at runs/<name>/manifest.jsonl (append + flush, like
    harvest.py) -- one JSON line per bug, live-tailable with `wc -l`.
  * Running cost total with --max-cost: abort cleanly before starting a bug that
    would push the batch over budget (Vertex calls are billed).

Requires the msagent venv (run_prediction imports mini-swe-agent) and, for the
standardized vertex_ai model, VERTEXAI_LOCATION + Vertex creds exported.

  cd ~/ARVO_intelligence && source .venv/bin/activate
  export VERTEXAI_LOCATION=global
  python batch_predict.py --limit 25 --shuffle --name shakeout
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "msagent_runner"))

from select_bugs import runnable_bugs
from fetch_source import cleanup_source
from run_prediction import run


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
    # --- per-bug agent knobs (forwarded to run_prediction.run) ---
    ap.add_argument("--model", default="vertex_ai/gemini-3.5-flash", help="litellm model id (Phase-4 standard)")
    ap.add_argument("--step-limit", type=int, default=45)
    ap.add_argument("--cost-limit", type=float, default=3.0, help="per-bug cost cap")
    # --- batch controls ---
    ap.add_argument("--max-cost", type=float, help="abort the batch before a bug that would exceed this running total")
    ap.add_argument("--cleanup-src", action="store_true", help="delete data/<id>/src after each bug (disk hygiene; re-fetch is ~5s)")
    ap.add_argument("--name", default=None, help="run name -> runs/<name>/ (default: timestamp)")
    a = ap.parse_args()

    if not a.data.is_dir():
        raise SystemExit(f"{a.data}: not a directory")

    bugs = runnable_bugs(a.data, sanitizer=a.sanitizer, project=a.project, skip_done=not a.redo)
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

    print(f"batch '{run_name}': {len(bugs)} bugs -> {manifest}")
    print(f"model={a.model} step_limit={a.step_limit} cost_limit=${a.cost_limit} max_cost={a.max_cost}\n", flush=True)

    total_cost = 0.0
    counts = {"submitted": 0, "predicted": 0, "no_prediction": 0, "errored": 0}
    started = time.time()

    with open(manifest, "a") as mf:  # append: keep history across resumed batches
        for i, bug in enumerate(bugs, 1):
            if a.max_cost is not None and total_cost >= a.max_cost:
                print(f"\nstopping: running cost ${total_cost:.2f} >= --max-cost ${a.max_cost:.2f} "
                      f"({i - 1}/{len(bugs)} bugs done)", flush=True)
                break

            print(f"[{i}/{len(bugs)}] {bug['id']} ({bug['project']}, {bug['sanitizer']})", flush=True)
            try:
                result = run(bug["path"], model_name=a.model, step_limit=a.step_limit, cost_limit=a.cost_limit)
            except Exception as e:
                # setup-stage failure (source fetch, docker start) -- run()'s own
                # mid-run guard only covers the agent loop. Record and continue.
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
                print(f"    ERROR (recorded, batch continues): {type(e).__name__}: {e}", flush=True)

            mf.write(json.dumps(result) + "\n")
            mf.flush()  # so `wc -l manifest.jsonl` tracks live progress

            if a.cleanup_src and cleanup_source(bug["path"] / "src"):
                print("    cleaned up src/", flush=True)

            total_cost += result.get("cost") or 0.0
            status = result.get("exit_status") or ""
            if status.startswith("batch-error") or status.startswith("crashed"):
                counts["errored"] += 1
            if status == "Submitted":
                counts["submitted"] += 1
            if result.get("prediction") is not None:
                counts["predicted"] += 1
            else:
                counts["no_prediction"] += 1

            mins = (time.time() - started) / 60
            print(f"    running cost ${total_cost:.2f} | {counts} | elapsed {mins:.1f}m\n", flush=True)

    print(f"\nbatch '{run_name}' done. bugs run: {counts['predicted'] + counts['no_prediction']}")
    print(f"  valid predictions: {counts['predicted']}   no prediction: {counts['no_prediction']}   errored: {counts['errored']}")
    print(f"  clean Submitted: {counts['submitted']}")
    print(f"  total cost: ${total_cost:.2f}")
    print(f"  manifest: {manifest}")


if __name__ == "__main__":
    main()
