# Building Leak-Normalized Two-Arm Environment Images (Discrimination Tasks)

**Audience:** Jinghe — adapting the sysintel discrimination-environment build pipeline to the
ARVO reproducible-bug dataset.

This doc describes how we pre-bake **two-arm discrimination environments** into Docker images:
for every bug, a *buggy* arm and a *fixed* arm that are **byte-identical in every observable
except the actual code content of the fix**. An agent dropped into one of the two containers
must have no metadata side channel that reveals which arm it is in — the only way to answer
"does this environment still contain the bug?" should be to actually reason about the code.

The reference implementation (Linux-kernel / live-kBench bugs, 468 bugs × 2 arms = 936 images,
built 2026-07-28) lives next to this doc:

- [`Dockerfile`](./Dockerfile) — the multi-stage normalization build
- [`build_shard.sh`](./build_shard.sh) — the idempotent per-box fleet worker

Everything below explains *why* each line exists, which parts are kernel-specific, and what to
change for ARVO.

---

## 1. Why pre-baked images (vs. runtime normalization)

Our first harness normalized environments *at container start*: `cp -a` the pristine tree,
re-init git, touch everything. This worked at low concurrency and fell over at scale:

- **PrepError storms at concurrency ≥ 150** — hundreds of containers doing an 80k-file
  `cp -a` + `touch` simultaneously starve I/O and prep steps time out.
- **Disk blow-up** — `cp -a` *doubles* the ~2 GB tree per live container. On standing rollout
  boxes that already carry TBs of images, this drove disks to 98% full and containers churned
  (spawn → ENOSPC mid-normalize → die), throughput collapsing to ~1 record/15 min.

Baking the normalized tree into an image moves all that cost to a one-time build. Rollout
boxes then just `docker run` — no per-container copy, no runtime normalization, no prep race.

---

## 2. Threat model: the leak channels you must close

These are the channels we found (some via adversarial audit of real agent trajectories).
Every one must be identical across the two arms:

| # | Channel | Leak | Fix |
|---|---------|------|-----|
| 1 | **git history** | Real repo history reveals whether the fix commit is present (log, reflog, blame dates). | Nuke `.git`, re-init with exactly **one** synthetic commit, same message/author/date in both arms. |
| 2 | **Commit / author timestamps** | A fresh `git init` stamps *build time*; two arms built minutes apart differ. | Pin `GIT_AUTHOR_DATE` / `GIT_COMMITTER_DATE` to a **content-derived epoch** (we use `stat -c %Y COPYING` — see §5 for the ARVO substitute). |
| 3 | **File mtimes** | Patched files carry newer mtimes than untouched ones — `find -newer` or `ls -lt` instantly reveals the fix's file set. | `touch -h` every path to the same epoch (with the retry loop of §6.1). |
| 4 | **git index stat-cache** | Even with flattened mtimes, `.git/index` records the mtime/size/ino of each file **at `git add` time**. `git ls-files --debug` (or just diffing index timestamps) exposes which files were touched after being added. This was a real audit finding. | `rm -f .git/index && git read-tree HEAD` — rebuilds the index from the tree object with a **zeroed stat-cache**, uniformly for all files. |
| 5 | **Patch residue** | The `.patch` file, `.orig`/`.rej` files, or the patch anywhere on disk. | Patch is COPY'd only into a **discarded build stage**, applied, and `rm`'d in the *same* `RUN`; a guard asserts it's gone. It never exists in the final image's layer history. |
| 6 | **Image structure / layer metadata** | Different layer counts, sizes, or `docker history` entries between arms. | Both final stages are structurally identical: same base + one `COPY --from=prep` tree layer + same warm `RUN` + `WORKDIR`. No labels, no arm-specific env vars. |
| 7 | **Layer birth-time on the host** (deployment-side) | If fleet boxes always pull parent-then-fix, the *btime gap* between base and tree layers statistically fingerprints the arm population. | Randomize per-bug **pull order** on rollout boxes. (This is a deployment TODO in our pipeline — carry it over.) |
| 8 | **gc / maintenance noise** | Background `git gc` could fire in one container and not the other, changing `.git` contents. | `git config gc.auto 0` at prep time. |

Rule of thumb: after building both arms, **any command an agent could run** (`git log`,
`git status`, `git diff`, `find -newer`, `ls -la`, `stat`, `git ls-files --debug`) must produce
output that is identical across arms *except where file content genuinely differs*.

---

## 3. Naming & registry conventions

All images go to the shared Artifact Registry repo:

```
us-docker.pkg.dev/triangulate-396717/sysintel-env/
```

**For your ARVO images, use the `sysintel-user-` prefix.** The exact scheme after the prefix
is your call, but keep it flat, lowercase, and *symmetric between arms* (identical except for
the arm token, so nothing about the name length/shape hints at the arm). A reasonable scheme:

```
us-docker.pkg.dev/triangulate-396717/sysintel-env/sysintel-user-arvo-{bugId}-{vul|fix}:latest
```

where `{bugId}` is the ARVO local ID. Whatever you pick, document it once and never mix schemes
— the fleet workers and verification scripts key off exact-name `gcloud artifacts docker images
describe` lookups (§7).

For comparison, the existing kernel images are `sysintel-{bug}-parent-commit:latest`
(bug present) and `sysintel-{bug}-fix-commit:latest` (patched).

One-time auth on every build box:

```bash
gcloud auth configure-docker us-docker.pkg.dev -q
```

---

## 4. Anatomy of the Dockerfile

Open [`Dockerfile`](./Dockerfile) alongside this. Six stages:

```
kenvsrc ──┐
          ▼
base ──► pristine ──► parent-prep ──► parent-commit   (final, pushed)
                 └──► fix-prep ─────► fix-commit      (final, pushed)
```

1. **`kenvsrc`** — `FROM <per-bug source image>`. Only used as a `COPY --from` source for the
   code tree. For ARVO this becomes the ARVO bug image (§5).

2. **`base`** — `python:3.13-bookworm` + `git`. This must be **byte-identical across every bug
   and both arms** so the base layers are stored/pulled exactly once fleet-wide (~1.5 GB shared;
   the per-image marginal cost is only the tree layer, ~2.9 GB for a kernel tree).

3. **`pristine`** — base + `COPY --from=kenvsrc /linux /linux` + `rm -rf /linux/.git`. One
   clean, history-free copy of the *buggy* tree, shared by both prep stages. Both arms derive
   from the **same** tree object — never from two different source images (§5, ARVO caveat #1).

4. **`parent-prep` / `fix-prep`** — the normalization stages, **discarded** (never pushed).
   `fix-prep` additionally applies the patch first: `git apply --check` (fast-fail before any
   work) → `git apply` → `rm /tmp/f.patch`, all before normalization so the patched files'
   mtimes get flattened like everything else. Then, identically in both:

   ```
   EPOCH=$(stat -c %Y COPYING)                      # content-derived timestamp anchor
   git init + user config + gc.auto 0 + git add -A
   git commit with GIT_AUTHOR_DATE/GIT_COMMITTER_DATE=@$EPOCH
   rm -f .git/index && git read-tree HEAD           # zero the index stat-cache (leak #4)
   5× retry: touch -h everything to @$EPOCH; break when 0 files newer than EPOCH+5
   ```

   followed by **hard guards** (the `&&`-chain makes the build FAIL, not silently mis-build):
   - exactly 1 commit (`git rev-list --count HEAD` = 1)
   - 0 files newer than `EPOCH+5` outside `.git`
   - (fix arm) the patch file no longer exists

5. **`parent-commit` / `fix-commit`** — the final, pushed stages. `base` + a **single**
   `COPY --from=*-prep /linux /linux`, so the whole tree is exactly one layer and nothing
   mutates it afterwards (any later `RUN` that touched `/linux` would add a whole-tree diff
   layer *and* re-perturb mtimes). Then the **git index warm**:

   ```dockerfile
   RUN cd /linux && git config core.checkStat minimal && git config core.trustctime false \
       && git update-index -q --refresh && git diff HEAD >/dev/null
   ```

   Why: without it, the *first* `git diff HEAD` in every container re-hashes the entire tree
   (9.7 s idle, 143–234 s under load on a kernel tree) — this silently timed out our patch
   capture and lost real agent patches. `checkStat=minimal` + `trustctime=false` make git
   ignore inode/dev/ctime, which get reassigned on layer extraction, so the warm **survives
   `docker pull` onto a different host** (verified: 0.15 s vs 9.8 s cold). The warm writes
   only the flattened epoch mtimes into the index, so it stays arm-uniform (doesn't reopen
   leak #4). It must be *character-identical* in both final stages.

   Note this warm step post-dates the doc'd 2026-07-28 build design — it was added after the
   first-`git diff` timeout bit us in production. Don't drop it.

---

## 5. Adapting to ARVO — what changes and what to verify

The Dockerfile is kernel-specific in a few places. Checklist:

1. **Source tree provenance — the big one.** In our pipeline, *both* arms come from one source
   image (the buggy tree) and the fix arm is produced by applying a patch we control. ARVO
   ships **paired images** (`-vul` / `-fix`) per bug. **Do not** take the vul tree from the vul
   image and the fix tree from the fix image: the two ARVO images may differ in far more than
   the source tree (build artifacts, logs, package state, timestamps), and every one of those
   differences is a leak. Instead:
   - take the source tree from the **vul** image only, and
   - produce the fix arm by applying the fix **as a patch** inside `fix-prep` (extract the
     patch from the ARVO metadata / the upstream fix commit, or `diff -ruN` the two ARVO trees
     once, offline, and review it).
   - If the discrimination task needs a *runnable* fuzz/build toolchain (not just the tree),
     the toolchain must come from a stage shared by both arms — same rule.

2. **`/linux` → the ARVO project source path.** ARVO checkouts live under the OSS-Fuzz layout
   (typically `/src/<project>`). Parameterize the tree path (e.g. `ARG TREE=/src/libxml2`) and
   audit that there's only **one** copy of the source in the image — leftover trees elsewhere
   (e.g. seed corpora with source snippets, `/src` siblings) also need normalizing or removing.

3. **The epoch anchor.** `stat -c %Y COPYING` is a kernel-ism: COPYING's mtime in the kenv
   images is a stable, content-associated timestamp present in both arms. For ARVO pick an
   anchor that is (a) deterministic per bug, (b) identical in both arms, (c) not derived from
   build time. Good option: the **parent/vulnerable commit's committer timestamp** from ARVO
   metadata, passed in as `ARG EPOCH` (then guards use `$EPOCH+5` as before). Don't use "some
   file's mtime" unless you've verified that mtime is stable across ARVO image rebuilds.

4. **Base image.** Keep one shared `base` across all your images. `python:3.13-bookworm`+git
   is fine if agents only need to *read* the tree. If they must **build/reproduce the crash**,
   base on the relevant toolchain image instead — but then it must still be the same base for
   both arms and ideally across bugs (per-project bases fragment the layer cache; weigh it).

5. **Tree size / retry tuning.** Kernel trees are ~80k files / 2.9 GB — hence `xargs -P 16`
   and the 5× retry. ARVO project trees are 10–100× smaller; the same code works unchanged,
   builds will just be much faster (our ETA was ~5 min/bug/box on shared CPU boxes; expect far
   less).

6. **Guards to add for ARVO.** Keep all existing guards, and consider adding: assert the tree
   path exists and is non-empty after COPY; assert `git status --porcelain` is empty at the end
   of prep (clean tree == index consistent with worktree).

---

## 6. Footguns (all hit in production — read before your first fleet run)

### 6.1 The touch race → 5× retry loop
Under back-to-back build I/O, a parallel `xargs -P16 touch` over 80k files intermittently
leaves a straggler with a fresh mtime. Our first fleet pass FAILED ~50% of builds, **all** at
the `find -newermt` guard; every failure normalized cleanly when re-run in isolation — a race,
not a real defect. Hence the retry loop *around* touch+check, with the hard guard after.
Keep the loop even for small ARVO trees; it's free when the first pass succeeds.

### 6.2 Shard files must survive `while read`
A shard file without a trailing newline makes `while read` **silently drop the last bug**.
The worker reads `done < <(grep . "$SHARD")` to be immune. Don't "simplify" that line.

### 6.3 Disk discipline on the build boxes
The worker is deliberately space-frugal: after each bug it `docker rmi`s both built images and
the pulled source image (~4 GB reclaimed) and runs `docker builder prune -f` to drop the
prep-stage cache. Without this a 100-bug shard eats hundreds of GB. Never run the build on
standing rollout boxes near disk capacity — and never delete other experiments' image sets to
make room (hours to re-pull; ask first).

### 6.4 Idempotence is the retry mechanism
The worker skips a bug iff **both** arms already `describe` successfully in the registry. So
"retry the failures" == "re-run the same shard command". All 21 residual failures from our
first pass self-healed on a plain resume. Design any modification to preserve this property.

### 6.5 Registry-side btime fingerprint (deployment)
See leak #7 — when pre-pulling images onto rollout boxes, randomize per-bug arm pull order.
Baking can't fix this one; it's a deployment-script concern.

### 6.6 Nothing after the tree COPY may touch the tree
Any `RUN` in the final stage that writes under the tree path creates a second whole-tree layer
and re-perturbs mtimes. The only permitted final-stage `RUN` is the git index warm (§4.5),
which is tree-content-neutral and identical in both arms.

---

## 7. Fleet build procedure

For N build boxes (we used 4):

1. **Inputs per bug:** the source image is pullable, and the fix patch exists locally as
   `patches/{bugId}.patch`. Pre-validate every patch with `git apply --check` against a
   scratch checkout *before* launching the fleet — a bad patch discovered at build time
   wastes a full tree COPY.
2. **Shard** the bug list `index % N` into `shard{0..N-1}.txt`, one bugId per line
   (trailing newline!). Copy `Dockerfile`, `build_shard.sh`, `patches/`, and the shard file
   to `~/discrim-build/` on each box.
3. **Launch** in tmux (survives SSH drops):
   ```bash
   tmux new -s dbuild -d 'bash ~/discrim-build/build_shard.sh ~/discrim-build/shard0.txt'
   ```
4. **Monitor:** `grep -c '^OK' /tmp/discrim_build.log` vs total; `grep '^FAIL'` for failures.
   The log lines are `OK|SKIP|FAIL <bug> (<reason>)` plus `SHARD_START`/`SHARD_DONE` markers.
5. **Resume:** just re-run step 3; idempotence (§6.4) skips completed bugs.
6. **Final verification — do not skip:** list the registry and cross-check against the bug
   list in *both* directions (every bug has both arms; no strays):
   ```bash
   gcloud artifacts docker images list \
     us-docker.pkg.dev/triangulate-396717/sysintel-env \
     --format='value(IMAGE)' | sort > pushed.txt
   # then diff against the expected 2×N name list generated from your bug list
   ```

---

## 8. Acceptance checklist (run before calling the dataset done)

On at least a handful of bugs (we prototyped on 2 before the fleet run):

- [ ] **Bit-reproducibility:** build one arm twice with `--no-cache`; the tree-layer digest
      must be **identical**. (Ours was — this also proves build order/box is irrelevant.)
- [ ] **Pull-back audit** on a *different* host: `docker pull` the image and check inside a
      container: exactly 1 commit; commit date == epoch; `find / -newermt @EPOCH+5` under the
      tree returns 0; no `*.patch|*.orig|*.rej` anywhere; `git status` clean; first
      `git diff HEAD` returns in well under a second (index warm survived the pull).
- [ ] **Arm A/B sweep:** run the *same* battery of metadata commands (§2 rule of thumb) in
      both arms and `diff` the outputs — differences only where file content differs.
- [ ] **Content sanity:** the fix arm actually contains the fix (`grep` for a hunk from the
      patch) and the vul arm doesn't.
- [ ] **Structural identity:** `docker history` shows the same layer count/shape for both arms.

---

## 9. Reference-run stats (kernel dataset, for calibration)

- 468 bugs × 2 arms = 936 images, all verified in registry.
- 4 CPU boxes, ~9.5 h wall total (~5 min/bug/box incl. pull + 2 builds + 2 pushes).
- Shared base ~1.46 GB (stored once); per-arm tree layer ~2.9 GB.
- First pass ~50% flake (touch race, §6.1); post-fix resume self-healed everything.
