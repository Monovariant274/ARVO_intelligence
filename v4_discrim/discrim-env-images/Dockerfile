# Discrimination env images: sysintel-{bug}-{parent|fix}-commit
# parent-commit = bug present (unpatched); fix-commit = commentless oracle patch applied.
# Both from the same parent-commit kernel tree; every metadata leak channel normalized
# identically (git history pruned to 1 pinned commit, mtimes at COPYING epoch, git index
# stat-cache zeroed). The patch lives only in a discarded build stage, outside /linux.
ARG BUG

FROM kenv-base-${BUG}-parent-commit:latest AS kenvsrc

FROM python:3.13-bookworm AS base
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

FROM base AS pristine
COPY --from=kenvsrc /linux /linux
RUN rm -rf /linux/.git

FROM pristine AS parent-prep
RUN set -e; cd /linux; EPOCH=$(stat -c %Y COPYING); \
    git init -q && git config user.email a@a && git config user.name a && \
    git config gc.auto 0 && git add -A && \
    GIT_AUTHOR_DATE="@$EPOCH" GIT_COMMITTER_DATE="@$EPOCH" git commit -qm base >/dev/null && \
    rm -f .git/index && git read-tree HEAD && \
    for i in 1 2 3 4 5; do \
      find . -print0 | xargs -0 -r -P 16 touch -h -d @$EPOCH; \
      [ "$(find . -newermt @$((EPOCH+5)) -not -path './.git/*' | wc -l)" = "0" ] && break; \
      sleep 1; \
    done && \
    [ "$(git rev-list --count HEAD)" = "1" ] && \
    [ "$(find . -newermt @$((EPOCH+5)) -not -path './.git/*' | wc -l)" = "0" ]

FROM pristine AS fix-prep
ARG BUG
COPY ${BUG}.patch /tmp/f.patch
RUN set -e; cd /linux; \
    git apply --check /tmp/f.patch && \
    git apply /tmp/f.patch && rm /tmp/f.patch && \
    EPOCH=$(stat -c %Y COPYING); \
    git init -q && git config user.email a@a && git config user.name a && \
    git config gc.auto 0 && git add -A && \
    GIT_AUTHOR_DATE="@$EPOCH" GIT_COMMITTER_DATE="@$EPOCH" git commit -qm base >/dev/null && \
    rm -f .git/index && git read-tree HEAD && \
    for i in 1 2 3 4 5; do \
      find . -print0 | xargs -0 -r -P 16 touch -h -d @$EPOCH; \
      [ "$(find . -newermt @$((EPOCH+5)) -not -path './.git/*' | wc -l)" = "0" ] && break; \
      sleep 1; \
    done && \
    [ "$(git rev-list --count HEAD)" = "1" ] && \
    [ "$(find . -newermt @$((EPOCH+5)) -not -path './.git/*' | wc -l)" = "0" ] && \
    ! test -e /tmp/f.patch

FROM base AS parent-commit
COPY --from=parent-prep /linux /linux
# Warm git's index stat-cache. Without this the FIRST `git diff HEAD` in every
# container must re-hash the whole 1.5GB tree (9.7s idle, 143-234s under load) —
# which silently timed out the resolve-phase patch capture and lost real patches.
# checkStat=minimal + trustctime=false make git ignore ino/dev/ctime, which are
# reassigned when the layer is extracted, so this warm SURVIVES the pull
# (verified on a second host: 0.15s vs 9.8s). The recorded mtimes are all the
# COPYING epoch, so the index stays arm-uniform and leaks nothing.
RUN cd /linux && git config core.checkStat minimal && git config core.trustctime false \
    && git update-index -q --refresh && git diff HEAD >/dev/null
WORKDIR /linux

FROM base AS fix-commit
COPY --from=fix-prep /linux /linux
# Same index warm as the parent arm — see comment there. Must be identical in
# both arms so the two images stay indistinguishable in everything but content.
RUN cd /linux && git config core.checkStat minimal && git config core.trustctime false \
    && git update-index -q --refresh && git diff HEAD >/dev/null
WORKDIR /linux
