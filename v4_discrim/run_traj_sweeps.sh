#!/usr/bin/env bash
# Re-run the item-3 (normalized-image) v4 discrimination sweep for BOTH models, this time
# CAPTURING EVERY ROLLOUT'S TRAJECTORY (runs/<name>/trajectories/<bug>_<arm>_s<sample>.json).
#
# Conditions are identical to the prior norm sweeps (§6/§7/§11): same dataset_adjacent.json,
# --images, k=5, step_limit 30, cost_limit $1/rollout, max_cost $150, arm-neutral prompt, schema.
# Only difference vs before: trajectories are now persisted (harness fix §12).
#
# SEQUENTIAL by design: the two runs must never overlap, because both write the same scratch
# dirs data/<bug>/answer_img_<arm>/ (prediction.json + the mounted answer volume). Running them
# at once would race that path. So flash-3-preview finishes fully, THEN gemini-3.5-flash starts.
#
# Concurrency matches each model's prior run: flash-3-preview=4, gemini-3.5-flash=1 (5 RPM cap).
# RESUMABLE: re-launching this script resumes each batch by run name (batch_verdict counts
# existing manifest rows per (bug,arm) and runs only the remaining k). Safe after any crash.
# DISCONNECT-PROOF: launch with `setsid ... &` from the v4disc shell so it inherits GEMINI_API_KEY,
# has no controlling TTY, and is reparented to init -> SIGHUP on disconnect never reaches it.
set -uo pipefail
cd /home/jinghezhang/ARVO_intelligence/v4_discrim
PY=/home/jinghezhang/ARVO_intelligence/.venv/bin/python
COMMON="--images --dataset dataset_adjacent.json --k 5 --step-limit 30 --cost-limit 1.0 --max-cost 150"

echo "=== [$(date '+%F %T')] START flash-3-preview sweep (concurrency 4) ==="
$PY batch_verdict.py $COMMON --concurrency 4 --model gemini/gemini-3-flash-preview \
    --name v4disc_norm_flash3_traj >> v4disc_norm_flash3_traj.log 2>&1
echo "=== [$(date '+%F %T')] flash-3-preview batch exit=$? ==="

echo "=== [$(date '+%F %T')] START gemini-3.5-flash sweep (concurrency 1) ==="
$PY batch_verdict.py $COMMON --concurrency 1 --model gemini/gemini-3.5-flash \
    --name v4disc_norm_g35_traj >> v4disc_norm_g35_traj.log 2>&1
echo "=== [$(date '+%F %T')] gemini-3.5-flash batch exit=$? ==="

echo "=== [$(date '+%F %T')] METRICS ==="
$PY discriminability.py runs/v4disc_norm_flash3_traj/manifest.jsonl || true
$PY discriminability.py runs/v4disc_norm_g35_traj/manifest.jsonl || true
echo "=== [$(date '+%F %T')] ALL DONE ==="
