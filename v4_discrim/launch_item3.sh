#!/usr/bin/env bash
# Item 3: normalized-image discrimination sweep. MUST run in the v4disc tmux shell (that is the
# only place GEMINI_API_KEY is exported; it is deliberately never written to a file). Waits for
# the 2g build report so all per-bug images exist, then runs the same batch driver with --images.
set -u
cd /home/jinghezhang/ARVO_intelligence/v4_discrim || exit 1

REPORT=discrim-env-images/build_report_adjacent.json
echo "[launch_item3] $(date): waiting for 2g build report ($REPORT)..."
until [ -f "$REPORT" ]; do sleep 30; done
echo "[launch_item3] $(date): 2g report present -> starting item-3 normalized-image sweep"

/home/jinghezhang/ARVO_intelligence/.venv/bin/python batch_verdict.py --images \
  --dataset dataset_adjacent.json --k 5 --concurrency 4 --step-limit 30 \
  --cost-limit 1.0 --max-cost 150 --name v4disc_norm 2>&1 | tee v4disc_norm_stdout.log
echo "NORM_EXIT=${PIPESTATUS[0]}"

echo "[launch_item3] $(date): sweep finished -> metrics"
/home/jinghezhang/ARVO_intelligence/.venv/bin/python discriminability.py runs/v4disc_norm/manifest.jsonl 2>&1 | tee v4disc_norm_metrics.log
echo "[launch_item3] DONE"
