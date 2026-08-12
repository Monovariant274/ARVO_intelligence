#!/bin/bash
# Build both discrimination-env arms for every bug in this box's shard and push
# to us-docker.pkg.dev/triangulate-396717/sysintel-env. Runs ON a cpu box.
# Usage: build_shard.sh <shard-file>   (one bugId per line)
set -u
SHARD="$1"
WORK=~/discrim-build
DF="$WORK/Dockerfile"
SRC_REG=us-docker.pkg.dev/triangulate-396717/live-kbench
DST_REG=us-docker.pkg.dev/triangulate-396717/sysintel-env
PATCHES="$WORK/patches"
LOG=/tmp/discrim_build.log
export DOCKER_BUILDKIT=1
gcloud auth configure-docker us-docker.pkg.dev -q >/dev/null 2>&1

done_ct=0; fail_ct=0; total=$(grep -c . "$SHARD")
echo "SHARD_START $(date -u +%FT%TZ) bugs=$total" >> $LOG
while read BUG; do
  [ -z "$BUG" ] && continue
  # skip if both arms already pushed (idempotent resume)
  if gcloud artifacts docker images describe "$DST_REG/sysintel-$BUG-parent-commit:latest" >/dev/null 2>&1 && \
     gcloud artifacts docker images describe "$DST_REG/sysintel-$BUG-fix-commit:latest" >/dev/null 2>&1; then
    echo "SKIP $BUG (already in registry)" >> $LOG; done_ct=$((done_ct+1)); continue
  fi
  # ensure kenv-base source image present locally
  if ! docker image inspect "kenv-base-$BUG-parent-commit:latest" >/dev/null 2>&1; then
    docker pull -q "$SRC_REG/kenv-base-$BUG-parent-commit:latest" >/dev/null 2>&1 && \
      docker tag "$SRC_REG/kenv-base-$BUG-parent-commit:latest" "kenv-base-$BUG-parent-commit:latest" || {
        echo "FAIL $BUG (kenv-base pull)" >> $LOG; fail_ct=$((fail_ct+1)); continue; }
  fi
  # per-bug context: just the patch
  CTX=$(mktemp -d)
  cp "$DF" "$CTX/Dockerfile"
  cp "$PATCHES/$BUG.patch" "$CTX/$BUG.patch" || { echo "FAIL $BUG (no patch)" >> $LOG; fail_ct=$((fail_ct+1)); rm -rf "$CTX"; continue; }
  ok=1
  for arm in parent fix; do
    IMG="$DST_REG/sysintel-$BUG-$arm-commit:latest"
    if docker buildx build --load -q --build-arg BUG=$BUG --target ${arm}-commit -t "$IMG" "$CTX" >>$LOG 2>&1; then
      docker push -q "$IMG" >>$LOG 2>&1 || { echo "FAIL $BUG ($arm push)" >> $LOG; ok=0; }
    else
      echo "FAIL $BUG ($arm build)" >> $LOG; ok=0
    fi
    docker rmi "$IMG" >/dev/null 2>&1
  done
  docker rmi "kenv-base-$BUG-parent-commit:latest" >/dev/null 2>&1   # reclaim ~4GB
  docker builder prune -f >/dev/null 2>&1                            # drop prep-stage cache
  rm -rf "$CTX"
  if [ "$ok" = 1 ]; then done_ct=$((done_ct+1)); echo "OK $BUG ($done_ct/$total)" >> $LOG
  else fail_ct=$((fail_ct+1)); fi
done < <(grep . "$SHARD")
echo "SHARD_DONE $(date -u +%FT%TZ) ok=$done_ct fail=$fail_ct total=$total" >> $LOG
