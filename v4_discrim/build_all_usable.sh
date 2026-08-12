#!/usr/bin/env bash
# Build + push BOTH arms for the entire USABLE ARVO set (item 2, full-corpus).
# Chains the three resumable stages and keeps a single high-level progress ledger:
#   1. wait for scan_usable.py's report (the usable set)         -> discrim-env-images/usable_scan.json
#   2. derive the bug list                                        -> dataset_usable.json
#   3. build the commentless oracle patch per bug (resumable)     -> patch_corpus_usable.json + patches_usable/
#   4. build + push both arms per bug (resumable, idempotent)     -> build_report_usable.json (+ registry)
#
# RESUMABLE end to end: build_patch_corpus skips bugs that already have a patch+row;
# build_shard_arvo --push skips bugs whose BOTH arms already exist in the registry
# (gcloud describe). So re-running this script after any interruption picks up where
# it stopped and never rebuilds/re-pushes finished work.
#
#   nohup ./build_all_usable.sh > build_all_usable.log 2>&1 &
set -u
cd /home/jinghezhang/ARVO_intelligence/v4_discrim || exit 1
PY=/home/jinghezhang/ARVO_intelligence/.venv/bin/python
LEDGER=build_all_progress.txt
log(){ echo "[$(date '+%F %T')] $*" | tee -a "$LEDGER"; }

log "=== build_all_usable start ==="

# 1. wait for the usability scan to finish
if [ ! -f discrim-env-images/usable_scan.json ]; then
  log "waiting for discrim-env-images/usable_scan.json (scan still running)..."
  until [ -f discrim-env-images/usable_scan.json ]; do sleep 30; done
fi
log "usable_scan.json present."

# 2. derive dataset_usable.json (bug-id list) from the scan report
$PY - <<'EOF'
import json
r = json.load(open('discrim-env-images/usable_scan.json'))
ids = [b['bug_id'] for b in r['usable_bugs']]
json.dump({'bugs': ids, 'params': {'source': 'usable_scan.json', 'n': len(ids)}},
          open('dataset_usable.json', 'w'), indent=2)
print(len(ids))
EOF
N=$($PY -c "import json;print(len(json.load(open('dataset_usable.json'))['bugs']))")
log "dataset_usable.json: $N usable bugs"

# 3. commentless patch corpus (resumable; sequential, one repo on disk at a time)
log "STAGE 3: building patch corpus for $N bugs (resumable)..."
$PY build_patch_corpus.py \
    --dataset dataset_usable.json \
    --out discrim-env-images/patches_usable \
    --summary discrim-env-images/patch_corpus_usable.json 2>&1 | tee -a build_all_patchcorpus.log
PC_OK=$($PY -c "import json;d=json.load(open('discrim-env-images/patch_corpus_usable.json'));print(d.get('n_ok'))" 2>/dev/null || echo '?')
log "STAGE 3 done: $PC_OK bugs have a clean patch."

# 4. build + push both arms (resumable via registry idempotency)
log "STAGE 4: build + push both arms (resumable)..."
$PY build_shard_arvo.py \
    --manifest discrim-env-images/patch_corpus_usable.json \
    --push \
    --log /tmp/discrim_build_usable.log \
    --summary-out discrim-env-images/build_report_usable.json 2>&1 | tee -a build_all_build.log
B_OK=$($PY -c "import json;d=json.load(open('discrim-env-images/build_report_usable.json'));print(d.get('n_ok'))" 2>/dev/null || echo '?')
log "STAGE 4 done: $B_OK bugs built + pushed (x2 arms)."

log "=== build_all_usable COMPLETE: patch_ok=$PC_OK build_ok=$B_OK ==="
