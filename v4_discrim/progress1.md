# v4 Discrimination Reward — Items 1–3 (raw baseline → leak-normalized images)

**Status: COMPLETE and audited (2026-08-03).** This document is the standalone, detailed record of
the item 1–3 work in `v4_discrim/`. Item 4 (RL training) is explicitly OUT OF SCOPE here.

---

## 1. What we were trying to measure, and why

The v4 task asks a harder question than "where does it crash": it asks **"does this input crash this
exact revision of the code, at all?"** Each bug is presented in **two arms that share one PoC**:

- **crash arm** — source at the vulnerable commit → the PoC crashes.
- **fix arm** — source at the fix commit → the *same* PoC runs clean.

The agent is **never told which arm it is in**. It reads the source + PoC and emits a crash/no-crash
verdict. **Discriminability** = how cleanly the agent separates the two arms. High separation is
evidence it actually *read and reasoned about the code*; low separation means it can't tell a buggy
revision from a patched one.

The reason this matters: an agent could "succeed" at bug-finding by exploiting **environment leaks**
(patch comment text, file mtimes, git metadata, build artifacts) rather than understanding the code.
The two-arm design is a discriminability probe; items 2–3 exist to **remove those leaks** and see how
much of the measured skill survives.

**Headline framing:**
- **Item 1** = raw fetched source in a generic sandbox → a deliberately *leaky UPPER-BAND baseline*.
- **Item 3** = the SAME sweep but the agent runs inside a *leak-normalized per-bug image*.
- **Item 1 vs Item 3** = the clean raw-vs-normalized delta = "how much was the agent leaning on leaks."

---

## 2. Dataset (item 1a) — the frozen 50-bug set, and the "adjacent" refinement

`build_dataset.py` → `dataset.json` / `dataset_adjacent.json` (50 bugs each). A bug is eligible iff:

- `poc` + `meta.json` + `ground_truth.json` present and parse (`select_bugs.runnable_bugs`),
- `meta.json` carries **both** `vuln_commit` and `fix_commit` (the two arms),
- source host is fetchable (heptapod/graphicsmagick excluded),
- `gt_adapter` yields ≥1 usable **gold frame** — reward_v4 needs ≥1 gold frame to score the crash arm,
  so un-scorable bugs are dropped at selection.

Selection is deterministic (seeded shuffle) with a per-project cap (≤3) so the set spans many projects
instead of piling onto skia/ffmpeg. `meta.json` example fields: `project`, `sanitizer`, `crash_type`,
`repo_addr`, `vuln_commit`, `fix_commit`.

**The "adjacent" set (`dataset_adjacent.json`) — critical soundness fix.** A hazard: if `fix_commit^`
(the parent of the fix) is used as the crash base but the fix is a *multi-commit series*, the base tree
might already be partially safe, muddying the arms. Resolved by **restricting to bugs where
`vuln_commit == fix_commit^`** (crash and fix revisions are distance-1 adjacent). Then `fix^` is
simultaneously (a) the ARVO-verified-crashing tree and (b) the minimal-diff base for the fix. Built via
`build_dataset.py --adjacent` (git-checks `fix^ == vuln`, cached in `adjacency_cache.json`).
**All item 1–3 sweeps run on this adjacent set.**

---

## 3. The task contract (shared by items 1 and 3)

### 3.1 Sandbox lockdown (`lib/sandbox_contract.py`)
Fixed in-container layout: `/workspace/src` (source, ro), `/workspace/poc` (input, ro),
`/workspace/answer` (the ONLY writable path) with the verdict at `/workspace/answer/prediction.json`.
`lockdown_flags()` = `--network none --read-only --cap-drop ALL --security-opt no-new-privileges
--user <uid>:<gid>`. In item 1 the source's `.git` is masked with an empty read-only mount (so the
next-commit fix message can't leak). No compiler / build system / network — the agent **cannot build or
run** the target; it must reason statically.

### 3.2 Verdict schema (`verdict_schema.py`)
The agent writes one JSON object:
- `crashes` : bool
- `bugClass` : string (crash class if `crashes`, else exactly `"none"`)
- `reason` : non-empty string (1–3 sentences)
- `crashFrames` : crash-site-first frames `{filename, function, line}`; ≥1 iff `crashes`, else `[]`
- `allocFrames` / `freeFrames` : optional, for heap/UAF alloc & free sites

`validate_verdict()` is a strict **subset** of the authoritative vendor parser
(`vendor/exec_rl/reward_v4.parse_crash_verdict`) — it never accepts what the parser would reject. A
verdict that is off-schema / missing → **invalid** (reward −1). Rule mirror: `crashes:false` REQUIRES
`bugClass=="none"` and empty `crashFrames`; `crashes:true` REQUIRES ≥1 well-formed frame.

### 3.3 Arm-neutral prompt
The instance prompt tells the agent only that "an input was run against `<project>` under `<sanitizer>`"
and that it must **decide** crash-or-not by reading the code — "a bug may be present in this tree, or it
may have been fixed here. Do not assume a crash exists." **Nothing reveals the arm.** (Contrast with the
single-site prediction task, which says "a fuzzer found a crash" — a dead giveaway.) The prompt forces a
verdict on the very first tool call (so a usable answer exists even if the step budget runs out) and
requires the run to end with a `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` bash call.

### 3.4 Scoring (`score_verdict.py` → `vendor/exec_rl/reward_v4.py`)
Single scoring path used identically by the live run and the metric, so they can't diverge.
- **fix arm:** no-crash → **1.0**, crash → **0.0** (clean binary).
- **crash arm:** no-crash → **0.0**, crash → **S ∈ [0,1]**, where
  `S = 0.80·stack + 0.15·bugClass + 0.05·allocfree` (weights of unavailable gold components
  renormalized away). `stack` is a depth-weighted subsequence match (crash-site frame counts most,
  refined by line proximity), normalized so a prompt-perfect prediction = 1.0.
- **off-schema / missing verdict (either arm):** **−1.0** (invalid).

> **Note the asymmetry** (relevant only to item 4 RL, inert for item 1–3 metrics): a *correct fix* pays
> a flat 1.0, a *correct crash* pays only S (which averaged **~0.09** empirically because the model names
> the wrong frames). The item 1–3 discriminability metric uses only the **binary `crashes` verdict**, so
> reward magnitude does not affect J / accuracy / TPR / FPR at all.

### 3.5 The metric (`discriminability.py`)
Reads the manifest and reports, treating "says crash" as the positive call, over **VALID rows only**:
- **TPR** = P(says crash | crash arm) — crash-arm sensitivity
- **FPR** = P(says crash | fix arm) — fix-arm false-alarm rate
- **Youden J** = TPR − FPR — the headline discriminability, in [−1, 1]
- **accuracy** = (TP + TN) / valid
- **mean reward per arm** (valid-only and incl. the −1 invalid penalty)
- **PAIRED per-bug**: per bug, `p_crash` = mean(says-crash | crash arm), `p_fix` = mean(says-crash | fix
  arm), `delta = p_crash − p_fix`; reports mean delta and the >0/=0/<0 split. Pairing cancels per-bug
  difficulty, so a positive mean delta is the strongest single evidence of real reading.
- **invalid rate per arm** — a sanity guard; high invalid rates make the pooled rates unreliable.

---

## 4. Item 1 — the raw-source baseline (`run_verdict.py`, `batch_verdict.py`)

`run_verdict.py` runs one `(bug, arm, sample)` rollout in the generic `arvo-sandbox:base` image with the
fetched source bind-mounted read-only and `.git` masked. `_ARM_COMMIT = {"crash":"vuln_commit",
"fix":"fix_commit"}`; separate `src_crash`/`src_fix` dirs so both arms coexist. Uses the minisweagent
`DefaultAgent` + `DockerEnvironment`, `SafeLitellmModel` wrapper (Gemini safety settings + truncated
observations). Result row records `crashes`, `reward`, `valid`, `invalid_reason`, `gold_available`,
cost, cache, duration, etc.

**This is the LEAKY upper-band baseline by design** (see the NOTE ON LEAKS in the file): fix-commit
source can still carry giveaway **patch comments**, **mtimes**, and other residual channels. Item 1
measures first; items 2–3 normalize.

The batch driver `batch_verdict.py` runs `N bugs × 2 arms × k samples`:
- one rollout failing never kills the batch (wrapped; setup/fetch errors recorded as `batch-error` rows),
- **resumable** at the `(bug, arm)` grain (counts existing manifest rows, runs only the remaining k),
- **concurrency at the BUG level only** (a worker owns a whole bug; the two arms share fixed per-bug
  dirs so parallelizing within a bug would collide),
- `--max-cost` soft ceiling; `--cleanup-src` deletes `src_<arm>` after each arm for disk hygiene.

---

## 5. Item 2 — the leak-normalization pipeline (a–g)

Goal: build, per bug, **two images from the SAME `fix^` tree** where every metadata leak channel is
normalized *identically*, so the ONLY signal separating the arms is the code itself.

- **crash arm** → `sysintel-user-arvo-<bug>-vul:latest` (fix^ tree, bug present, unpatched)
- **fix arm** → `sysintel-user-arvo-<bug>-fix:latest` (SAME fix^ tree + commentless oracle patch)

### 5.1 Patch corpus (item 2b, `build_patch_corpus.py`)
`patch_corpus_adjacent.json` + `patches_adjacent/{bug}.patch` — the **commentless oracle patch** for
each bug (comments stripped so the patch itself can't leak intent), rebuilt on the adjacent set
(50/50 clean, median ~2 files). `repo_addr` recorded per row (the build worker needs it).

### 5.2 The normalization Dockerfile (items 2c/2d, `discrim-env-images/Dockerfile.arvo`)
An ARVO adaptation of Chenxi's kernel Dockerfile. Structural deviation: ARVO has no per-bug source image
at `fix^`, so **the tree enters via the build context** (`build_shard_arvo` git-checks out `fix^` into
`ctx/tree`, no `.git`). Stages:
1. `pristine` — one clean, history-free copy of the `fix^` tree, shared by both prep stages (so both
   arms derive from the SAME tree object).
2. `vul-prep` — normalize the unpatched tree: `git init` → 1 synthetic commit at `@EPOCH`; zero the
   index stat-cache (`rm .git/index; read-tree`); `touch -h @EPOCH` every mtime (retried until nothing
   is newer than EPOCH+5); assert exactly 1 commit + clean tree.
3. `fix-prep` — apply the commentless oracle patch **FIRST**, then the identical normalization. The
   patch lives ONLY in this discarded stage.
4. `vul-commit` / `fix-commit` — final pushed stages: base + ONE tree layer + identical git index warm
   (`checkStat=minimal`, `trustctime=false`, `update-index --refresh`, `git diff HEAD`), then re-anchor
   EVERY mtime to `@EPOCH`.

`EPOCH` = `git show -s --format=%ct <fix^>` (fix^ committer timestamp) — deterministic per bug,
identical across arms, **not** build-time-derived.

### 5.3 ⚠ Leak found and fixed during acceptance (item 2f)
Content files were `@EPOCH`, but the **tree-root dir, `.git`, and `.git/index` kept BUILD-TIME mtimes**,
which differ between the two arms' builds by the few seconds between them → a `stat()`-able channel
telling the arms apart. Fixed by re-anchoring **every** mtime (root, `.git`, `.git/index` included) to
`@EPOCH` at the end of each final stage. `checkStat=minimal` makes git ignore the index file's own mtime
(it compares the mtimes *recorded in* the index against the tracked files, all already `@EPOCH`), so
re-touching `.git/index` doesn't cost the warm — the first `git diff HEAD` still returns without
re-hashing. **This bug also affected Chenxi's kernel Dockerfile and was flagged upstream.**

### 5.4 Build worker (item 2e, `build_shard_arvo.py`)
Per bug: fetch `fix^` (shallow sha-fetch; full clone fallback) → `git archive | tar -x` into `ctx/tree`
→ compute EPOCH → drop `ctx/{bug}.patch` + `ctx/Dockerfile` → `docker build --target vul-commit` and
`--target fix-commit` from the SAME context → (`--push`) push both arms, `rmi` to reclaim space.
Idempotent under `--push`. `--keep` leaves images local so item 3 reuses them without a re-pull.

### 5.5 Item 2g — the actual build+push run
Built + pushed all **100 images** (50 adjacent bugs × 2 arms) to
`us-docker.pkg.dev/triangulate-396717/sysintel-env/sysintel-user-arvo-<bug>-{vul,fix}:latest`.
`build_report_adjacent.json`: `pushed: True`, 50/50 bugs ok.

---

## 6. Item 3 — the normalized-image sweep (`run_verdict_img.py`, `batch_verdict.py --images`)

`run_verdict_img.py` mirrors item 1 **exactly** except the environment:
- the per-bug **arm image is the container base** (source baked at `/src/<project>`, cwd = tree),
- mounts ONLY `poc` (ro) + `answer` (rw) — **no source bind-mount, no `.git` mask** (the image's `.git`
  is one synthetic arm-neutral commit at EPOCH),
- SAME arm-neutral prompt, SAME verdict schema, SAME `score_verdict` path.

So item-1-vs-item-3 is a clean raw-vs-normalized delta. `batch_verdict.py --images` flips `run_src` →
`run_img`; everything else (resume, concurrency, max-cost) is identical. Launched via `launch_item3.sh`,
which waits for the 2g build report before starting (so all per-bug images exist).

---

## 7. Results (audited 2026-08-03)

Both sweeps: **same 50 adjacent bugs, k=5, `gemini/gemini-3-flash-preview`, step_limit 30,
cost_limit $1/rollout, concurrency 4.**

### 7.1 Per-arm detail (valid = on-schema; rates/rewards over VALID rows)

| run | arm | n | valid | invalid | says-crash | mean R(valid) |
|---|---|---|---|---|---|---|
| adj (raw) | crash | 250 | 248 | 0.8% | 35.9% | +0.034 |
| adj (raw) | fix   | 250 | 222 | 11.2% | 26.6% | +0.734 |
| norm      | crash | 250 | 249 | 0.4% | 35.3% | +0.032 |
| norm      | fix   | 250 | 248 | 0.8% | 31.9% | +0.681 |

### 7.2 Pooled discriminability + paired + best-of-5

| metric | adj (raw, leaky) | norm (leak-normalized) |
|---|---|---|
| Youden J (TPR−FPR) | **+0.093** | **+0.035** |
| accuracy | 53.6% | 51.7% |
| TPR (crash says-crash) | 35.9% | 35.3% |
| FPR (fix says-crash) | 26.6% | 31.9% |
| mean-reward separation (crash−fix, valid) | −0.700 | −0.649 |
| paired bugs (both arms valid) | 45 | 50 |
| paired mean Δ (p_crash−p_fix) | +0.088 | +0.032 |
| paired Δ >0 / =0 / <0 | 42.2% / 31.1% / 26.7% | 38.0% / 26.0% / 36.0% |
| pass@1 / pass@5 crash | 35.6% / 82.0% | 35.2% / 78.0% |
| pass@1 / pass@5 fix | 65.2% / 86.0% | 67.6% / 94.0% |
| JOINT pass@5 (both arms right ≥once) | 70.0% | 72.0% |
| total cost | $48.38 | $51.70 |
| errored / invalid rows | 25 / 30 | 0 / 3 |

### 7.3 What the results say (in plain terms)

1. **Both regimes are only WEAKLY discriminating.** J sits near 0 and accuracy near the 50% coin-flip
   in *both* cases. At this model/budget the agent barely separates a buggy revision from a patched one
   by code-reading alone. This is the robust, headline finding.
2. **Normalization nudges it further toward chance**, driven almost entirely by **FPR rising**
   (26.6→31.9%) while TPR is flat — i.e. once fix-arm metadata leaks are gone, the agent calls "crash"
   on clean fix trees about as often as on real bugs. Directionally consistent with the agent having
   read *some* leak signal in raw source, not purely the code.
3. **Best-of-5 reveals substantial LATENT ability the average hides.** The correct answer is in the
   model's sample distribution far more often than pass@1 suggests: crash pass@5 ≈ 78–82%, fix pass@5 ≈
   86–94%, and on **70–72% of bugs the model produces a fully-correct crash+fix pair at least once in 5
   tries.** (This is the main signal that makes item-4 RL worth considering — separate doc.)

---

## 8. Soundness & code audit

**Code audit (2026-08-03): the item 1–3 path is clean; no bugs found.**
- Independent re-derivation of TPR/FPR/J/accuracy from both manifests **matches the stored
  `discriminability.json` to the digit.**
- Both runs: 500 rows, 50 bugs, 100 `(bug,arm)` cells, **all exactly 5 samples, zero duplicate
  `(bug,arm,sample)`, zero integrity anomalies** (every valid row has a bool `crashes` + reward∈[0,1];
  every invalid row reward = −1).
- The two runs cover the **identical 50-bug set** → the adj-vs-norm comparison is not confounded.
- All 50 bugs had gold frames available, so the low crash-arm reward is genuinely poor frame
  localization, not un-scorable bugs.

**⚠ Caveats — do NOT over-claim the leak-exploitation story:**
1. **Within noise.** n=50 bugs, ~250 valid rows/arm. The FPR gap of +5.3pt is ≈1.3 standard errors
   (SE≈4pt) — NOT statistically significant; the J gap is small vs sampling error. Treat "leaks were
   exploited" as *suggestive*, not proven. The defensible statement is "**both regimes ≈ chance.**"
2. **Censoring is apples-to-oranges.** The raw run's fix arm had 11.2% invalid (vs 0.8% norm), so its
   FPR is measured over a smaller, censored valid sample (222 vs 248). If off-schema verdicts correlate
   with the model being confused on fix trees, the raw FPR is biased — part of the adj→norm FPR move is
   a validity-rate artifact, not pure leak removal.
3. **Negative reward-separation is EXPECTED, not a metric inversion.** mean-reward-separation is
   negative because the crash arm's reward is localization-weighted (mean S ≈ 0.09 even when says-crash
   is correct) while the fix arm pays a flat 1.0 for a correct no-crash. This is the reward_v4 magnitude
   asymmetry (matters only for RL/item 4), NOT the arms being swapped.

**Operational win (independent of the discrimination story):** normalization slashed fix-arm invalid
11.2%→0.8% and errored 25→0 (baked images need no git-fetch), and lifted paired-valid bugs 45→50. The
normalized images are the right substrate going forward regardless of the (weak) J delta.

---

## 9. File map & reproduction

**Datasets:** `dataset_adjacent.json` (50 bugs), `adjacency_cache.json`, `data/<bug>/{meta.json,poc,
ground_truth.json}`.
**Task contract:** `lib/sandbox_contract.py`, `verdict_schema.py`, `gt_adapter.py`, `score_verdict.py`,
`vendor/exec_rl/reward_v4.py`.
**Item 1:** `run_verdict.py` (harness), `batch_verdict.py` (driver), `lib/fetch_source.py`.
**Item 2:** `build_dataset.py`, `build_patch_corpus.py`, `build_shard_arvo.py`,
`discrim-env-images/Dockerfile.arvo`, `discrim-env-images/patch_corpus_adjacent.json`,
`discrim-env-images/build_report_adjacent.json`.
**Item 3:** `run_verdict_img.py` (harness), `batch_verdict.py --images`, `launch_item3.sh`.
**Metric:** `discriminability.py`.
**Outputs:** `runs/v4disc_adj/{manifest.jsonl,discriminability.json}` (item 1 baseline),
`runs/v4disc_norm/{manifest.jsonl,discriminability.json}` (item 3).

**Reproduce (must run in the `v4disc` tmux shell — `GEMINI_API_KEY` lives ONLY there, never in a file;
use `/home/jinghezhang/ARVO_intelligence/.venv/bin/python`, which has `minisweagent`):**
```bash
# item 1 (raw baseline)
python batch_verdict.py --dataset dataset_adjacent.json --k 5 --concurrency 4 \
  --step-limit 30 --cost-limit 1.0 --max-cost 150 --cleanup-src --name v4disc_adj
# item 2g (build+push 100 images)
python build_shard_arvo.py --manifest discrim-env-images/patch_corpus_adjacent.json \
  --push --keep --summary-out discrim-env-images/build_report_adjacent.json
# item 3 (normalized), after 2g report exists
python batch_verdict.py --images --dataset dataset_adjacent.json --k 5 --concurrency 4 \
  --step-limit 30 --cost-limit 1.0 --max-cost 150 --name v4disc_norm
# metrics
python discriminability.py runs/v4disc_adj/manifest.jsonl
python discriminability.py runs/v4disc_norm/manifest.jsonl
```

**Total spend for items 1–3 sweeps:** ~$100 (adj $48.38 + norm $51.70), plus 2g build/push compute.

---

## 10. Full-corpus usable-image build (2026-08-04, COMPLETE — 576/577 bugs, 1,152 images)

Items 1–3 above used the frozen **50-bug adjacent set**. This section covers scaling the
leak-normalized two-arm image build to the **entire harvested corpus** so every *soundly buildable*
bug becomes a persistent training env (crash + fix images in the shared registry), for item-4 RL and
for sharing with Chenxi.

### 10.1 What "usable" means at corpus scale
A bug can become a sound two-arm env only if it passes **all six gates** (`scan_usable.py`):
1. `poc` present **and non-empty** (23 harvested bugs have a 0-byte poc — `runnable_bugs` only checks
   existence, so `scan_usable.py` adds an explicit `st_size == 0` drop),
2. `meta.json` + `ground_truth.json` parse,
3. source host fetchable (heptapod/graphicsmagick excluded),
4. `meta` carries **both** `vuln_commit` and `fix_commit`,
5. `gt_adapter` yields ≥1 usable gold frame (crash arm must be scorable),
6. **ADJACENT**: `vuln_commit == fix_commit^` — the ~9% gate. This is the binding constraint: only when
   `fix^` *is* the crashing commit can the crash arm be based on `fix^` soundly.

**Projected yield.** Local eligibility (gates 1–5): **5957** bugs (drops `empty_poc:23,
missing_commit:8, no_gold_frame:1`). Adjacency (gate 6) historically runs **~9%** (54/594 in the prior
cache) → **~530 usable bugs projected** (~1060 images). "Build all 6000" really means "build the ~530
that can form a *sound* two-arm env"; the other 91% are non-adjacent and cannot without an added
crash-at-`fix^` verification step (a design decision left for Chenxi).

### 10.2 Tooling (new this section)
- **`scan_usable.py`** — full-corpus scanner. Certifies gates 1–5 locally, then does one concurrent
  git fetch per survivor for gate 6. Resumable via `adjacency_cache.json` (bug_id → bool|None); writes
  `discrim-env-images/usable_scan.json` (with `usable_bugs`), `usable_scan_progress.txt`. Run:
  `python scan_usable.py --concurrency 8`.
- **`build_all_usable.sh`** — resumable 4-stage orchestration wrapper (`nohup ./build_all_usable.sh >
  build_all_usable.log 2>&1 &`). Ledger → `build_all_progress.txt`:
  1. wait for `usable_scan.json`,
  2. derive `dataset_usable.json` (bug-id list) from the scan's `usable_bugs`,
  3. `build_patch_corpus.py --dataset dataset_usable.json --out discrim-env-images/patches_usable
     --summary discrim-env-images/patch_corpus_usable.json` (commentless oracle patch per bug),
  4. `build_shard_arvo.py --manifest … --push --summary-out
     discrim-env-images/build_report_usable.json` (build + push **both** arms).

### 10.3 Resumability (end-to-end, safe to re-run after any interruption)
- **scan** — reuses/extends `adjacency_cache.json`; interrupted fetches never repeated.
- **stage 3** — `build_patch_corpus` skips bugs that already have a patch + summary row.
- **stage 4** — `build_shard_arvo --push` is idempotent: skips bugs whose **both** arms already exist
  in the registry (`gcloud describe`). The 50 adjacent bugs already pushed are therefore skipped.

So re-launching `build_all_usable.sh` after a crash/reboot picks up exactly where it stopped and never
rebuilds or re-pushes finished work.

### 10.4 Status — COMPLETE (2026-08-04 11:53)

Ran fully unattended end-to-end (through a client disconnect; both processes were reparented to
systemd with no controlling TTY, so SIGHUP never reached them). Final ledger line:
`=== build_all_usable COMPLETE: patch_ok=577 build_ok=576 ===`.

**Pipeline funnel (actual):**
- **scan** (03:35–05:50, 139 min): 5365/5365 checked → **582 usable** (adjacent). Adjacency ran
  ~10.8% here vs the ~9% projection, so yield beat the ~530 estimate.
- **stage 3** patch corpus (05:50–06:12): 582 → **577** clean commentless patches (5 dropped).
- **stage 4** build+push (06:12–11:53, ~5.7 h): 577 → **576 built + pushed** (both arms) →
  **1,152 images** live in `us-docker.pkg.dev/triangulate-396717/sysintel-env`, naming
  `sysintel-user-arvo-<bug>-{vul,fix}:latest` (same namespace as the original 100; verified 47/50 of the
  original bug-ids were skipped as "already in registry", proving co-location).
- Final report: `discrim-env-images/build_report_usable.json` (`n=577, n_ok=576, n_failed=1`).

**6 bugs did not produce an env — 3 buckets:**
- *Bucket A — malformed generated patch (4, likely a real bug in `build_patch_corpus.py`'s comment
  strip):* `42490006` + `42490091` (imagemagick, both `patch fragment without header at line 343`,
  identical MagickBlueShiftImage hunk), `42502182` (libpsl, `corrupt patch at line 241`), `42515438`
  (kimageformats, `corrupt patch at line 98`). Shared signature ⇒ `@@` hunk header line-counts not
  recomputed after stripping lines inside a hunk. **Fixable — should recover all 4.**
- *Bucket B — transient fetch (1, just retry):* `42476093` (icu, `git archive | tar extract failed` at
  stage 4). Idempotent single-bug re-run should recover it.
- *Bucket C — genuinely unusable (1, leave it):* `42497751` (fuzzing) — fix touches only a **binary**
  file, no text patch possible.

**Recoverable ceiling:** 576 → up to **581** (fix Bucket A + retry icu); 1 legitimately out.

**Open follow-ups:** (1) retry icu; (2) fix the hunk-header recomputation in `build_patch_corpus.py`
for Bucket A; (3) share `training_envs.json` + the corpus manifest with Chenxi for item-4 RL.

---

## 11. Model swap — Gemini 3.5 Flash on the normalized images (2026-08-05, COMPLETE)

A clean **model-only** swap of the item-3 (normalized) sweep: same 50 adjacent bugs, same normalized
per-bug images, same arm-neutral prompt, same verdict schema, same `score_verdict` path, same k=5,
`step_limit 30`, `cost_limit $1`. **Only the `--model` flag changed** to `gemini/gemini-3.5-flash`
(prior runs used `gemini/gemini-3-flash-preview`). Run at **concurrency 1** (deliberately, no rush — the
Vertex quota caps gemini-3.5-flash at ~5 RPM). Output: `runs/v4disc_norm_g35/{manifest.jsonl,
discriminability.json}`; stdout `v4disc_norm_g35.log`. Ran ~Aug 4 12:36 → Aug 5 08:31 (~20 h, serial).

### 11.1 Result vs the two Gemini-3-flash baselines (all: same 50 bugs, k=5, normalized images)

| metric | **g35 norm (new)** | g3-flash norm | g3-flash raw (leaky) |
|---|---|---|---|
| Youden J (TPR−FPR) | **+0.096** | +0.035 | +0.093 |
| accuracy | 54.8% | 51.7% | 53.6% |
| TPR (crash says-crash) | **16.1%** (40/249) | 35.3% | 35.9% |
| FPR (fix says-crash) | **6.4%** (16/249) | 31.9% | 26.6% |
| paired mean Δ (p_crash−p_fix) | +0.096 | +0.032 | +0.088 |
| paired >0 / =0 / <0 | 34% / 58% / 8% | 38% / 26% / 36% | 42% / 31% / 27% |
| invalid rate (each arm) | 0.4% | 0.8% | — |
| mean R(valid) crash / fix | +0.018 / +0.936 | +0.032 / +0.681 | +0.034 / +0.734 |

500/500 rows, 100/100 (bug,arm) cells at k=5, 498/500 valid (1 invalid per arm). J re-derived by hand
from the manifest = +0.0964 (matches the script to the digit).

### 11.2 ⚠ The critical caveat — the J gain is a NO-CRASH BIAS, not better crash-detection

**Both TPR and FPR fell sharply because Gemini 3.5 says "no crash" the overwhelming majority of the
time.** Verified directly from the manifest (not just the metric):
- **88.8% of ALL valid rollouts (442/498) are "no-crash" verdicts.**
- On **26 of 50 bugs the agent said no-crash on *every* valid rollout** (all 10, both arms) — it never
  even attempts a crash call on half the corpus.
- Accuracy 54.8% is only **~5 pt above the trivial "always say no-crash" baseline (50.0% exactly**, since
  the set is 50/50 crash/fix and no-crash is the correct fix-arm answer).

So the honest reading is **NOT** "Gemini 3.5 discriminates ~3× better." It is: **Gemini 3.5 is far more
conservative** — it false-alarms much less on patched trees (FPR 31.9%→6.4%, the large, likely-real move)
**at the cost of catching far fewer real crashes** (TPR 35.3%→16.1%). The positive J means that *in the
rare cases it does call crash*, it is ~2.5× more likely to be on a real crash arm (16.1%) than a fix arm
(6.4%) — a genuine but very low-recall signal. The 58% paired-Δ ties are mostly "no-crash on both arms."

### 11.3 Soundness caveats (same as §8)
- **Within noise on J.** n=50 bugs, ~249 valid/arm. The J difference vs g3-flash-norm (+0.035 vs +0.096)
  is ≈1–2 SE — *suggestive, not significant*. The FPR drop (31.9%→6.4%, SE≈3–4 pt) is the one move large
  enough to be **likely real**.
- **Reward-separation −0.917** is the expected reward_v4 magnitude asymmetry (crash arm pays
  localization-weighted S ≈ 0.02 here; fix arm pays flat ~0.94), NOT arm inversion — inert for the
  discriminability metric, matters only for item-4 RL.

**Bottom line:** on leak-normalized images, Gemini 3.5's discrimination edge over Gemini-3-flash is
almost entirely a precision/abstention shift, not improved crash-reading. Both models remain weak
discriminators (accuracy ≈ chance); Gemini 3.5 just moves the operating point toward "rarely cry wolf."

---

## 12. Trajectory persistence fix (2026-08-05)

**Problem found while trying to inspect rollouts:** the full agent trajectories (`info` + `messages`,
the complete step-by-step tool-call history) *were* being saved by both harnesses, but to a path keyed
by **(bug, arm) only** — `data/<bug>/answer_img_<arm>/trajectory.json` (and `answer_<arm>/` for item 1).
Two clobbers resulted:
1. **sample clobber** — within a (bug,arm) cell, each of the k=5 samples overwrote the previous → only
   the *last* sample's trajectory survived;
2. **run/model clobber** — the g35 sweep reused the same `data/.../answer_img_<arm>/` dirs and overwrote
   the earlier gemini-3-flash-preview trajectories.

Net effect: **no per-rollout trajectories survive for either completed sweep** — only 100 last-sample
g35 files remain on disk, and **zero** flash-3-preview trajectories (overwritten). The manifests still
hold every rollout's final verdict + metadata (`verdict_raw`, reward, valid, n_calls, cost, exit_status),
just not the intermediate reasoning. Recovering trajectories for the done runs is **impossible without
re-running** (sampled rollouts, temp>0 → a re-run is statistically comparable, not identical, and costs
quota again).

**Fix (code only, no re-run):** `run()` in `run_verdict.py` + `run_verdict_img.py` now takes an optional
`traj_dir`; the trajectory saves to a path unique per **(bug, arm, sample)**:
- batch driver → `runs/<name>/trajectories/<bug>_<arm>_s<sample>.json` (co-located with that run's
  manifest, so each run name / model gets its own folder — no cross-run clobber),
- direct CLI (no `traj_dir`) → `answer_<arm>/trajectory_s<sample>.json` (sample in the name, so ad-hoc
  runs don't clobber either).
Each manifest row now also records its `traj_path`, linking every rollout to its trajectory file.
`batch_verdict.py` creates `runs/<name>/trajectories/` per run and threads `a.traj_dir` through.
Resume-safe (writes only new-sample files, never touches existing ones). All three files compile;
verified a smoke rollout lands a file in the new location before any full re-run.

**⚠ Security incident during this work (2026-08-05):** while checking whether the `v4disc` tmux shell
had `GEMINI_API_KEY`, a malformed shell test (`${GEMINI_API_KEY:-NO}` returns the *value* when set)
printed the key into the session output. Pane + scrollback were cleared, but **the key should be treated
as compromised and rotated.** (User elected to keep using the leaked key for now — rotation deferred.)
Reinforces the standing rule (§A.7): the key lives only in the `v4disc` shell and must never be written
to a file — and never echoed, even indirectly.

---

## 13. Trajectory-capturing re-run — BOTH models (2026-08-05 → 2026-08-06, COMPLETE)

With the §12 fix in place, both normalized (item-3) sweeps are being **re-run from scratch to capture
every rollout's trajectory** (the prior `v4disc_norm` and `v4disc_norm_g35` runs kept verdicts but their
trajectories were clobbered/unrecoverable). Orchestrated by **`run_traj_sweeps.sh`** (launched under
`setsid` from the `v4disc` shell → reparented off the TTY, survives disconnect; SIGHUP-proof, same
mechanism as §10.4). Launched 2026-08-05 ~08:58.

**Conditions identical to the prior norm sweeps** (§6/§7/§11): same `dataset_adjacent.json`, `--images`,
k=5, `step_limit 30`, `cost_limit $1`, `max_cost $150`, arm-neutral prompt, verdict schema, scoring. The
**only** change is that trajectories now persist to `runs/<name>/trajectories/<bug>_<arm>_s<sample>.json`
(each manifest row records its `traj_path`).

**SEQUENTIAL by design** (never concurrent): both runs write the shared scratch dirs
`data/<bug>/answer_img_<arm>/` (prediction.json + the mounted answer volume), so overlapping them would
race that path. Order + per-model concurrency (each matches that model's prior run):
1. `gemini/gemini-3-flash-preview` → `runs/v4disc_norm_flash3_traj/`, **concurrency 4**, log
   `v4disc_norm_flash3_traj.log`. ETA ~3–4 h.
2. `gemini/gemini-3.5-flash` → `runs/v4disc_norm_g35_traj/`, **concurrency 1** (5 RPM Vertex cap), log
   `v4disc_norm_g35_traj.log`. ETA ~20 h. Auto-starts when #1 finishes.

Master log `run_traj_sweeps.log` prints START/exit per stage, then runs `discriminability.py` on both
manifests at the end. **Resumable:** re-launching the script resumes each batch by run name (counts
manifest rows per (bug,arm)); safe after any crash/reboot (but a reboot needs a manual re-launch — the
process is not a service). Total est ~24 h wall / ~$100.

**Smoke test before launch:** one rollout (`bug 42510353 crash+fix, k=1`) confirmed both trajectory files
land in the new run-scoped dir and `traj_path` is recorded ($0.50).

### 13.1 Completion + trajectory coverage (2026-08-06 07:09)

Both sweeps ran to completion, orchestrated end-to-end with no manual intervention.
- **flash-3-preview** finished 2026-08-05 13:41 (500/500, exit 0); **gemini-3.5-flash** finished
  2026-08-06 ~07:09 (500/500). Master log closed `=== ALL DONE ===` after both `discriminability.py`
  metrics ran.
- **Trajectories now fully persisted** (the whole point of this re-run): 500/500 files each,
  1:1 with manifest rows, keyed `(bug,arm,sample)` as `runs/<name>/trajectories/<bug>_<arm>_s<sample>.json`;
  every manifest row records its `traj_path`. (The prior `v4disc_norm` / `v4disc_norm_g35` runs still have
  **zero** trajectories — clobbered per §12, unrecoverable without re-running.)
- **Audit:** both manifests = 500 rows, balanced 250 crash / 250 fix, **zero duplicate (bug,arm,sample)**,
  3–4 invalid rows each. TPR/FPR/J re-derived by hand from the raw manifests match `discriminability.py`
  to the digit.

### 13.2 Pooled results vs the pre-trajectory norm runs (all: same 50 adjacent bugs, k=5, normalized images)

| metric | **NEW flash3-traj** | prior flash3-norm | **NEW g35-traj** | prior g35-norm |
|---|---|---|---|---|
| Youden J (TPR−FPR) | **+0.086** | +0.035 | **+0.028** | +0.096 |
| accuracy | 54.3% | 51.7% | 51.4% | 54.8% |
| TPR (crash says-crash) | 31.0% | 35.3% | 8.9% | 16.1% |
| FPR (fix says-crash) | 22.5% | 31.9% | 6.0% | 6.4% |
| paired mean Δ | +0.087 | +0.032 | +0.031 | +0.096 |

**⚠ The cross-model J ranking FLIPPED between the two run-pairs** (prior: g35 +0.096 > flash3 +0.035;
new: flash3 +0.086 > g35 +0.028). This is **not** a bug and **not** an environment difference — both
pairs are normalized (`--images`). It is the §8/§11 caveat made concrete: these are `temp>0` sampled
rollouts (§12: a re-run is "statistically comparable, not identical"), and on n=50 near-chance
discriminators the per-model J moves ~1–2 SE between resamples, so *which model's J is nominally higher is
not stable*. What DID reproduce is each model's **regime**: flash3 stays the moderate caller (~30%
says-crash), g35 stays the ultra-conservative abstainer (~9% says-crash, very low FPR). The honest
statement remains **both ≈ chance; ranking them by J on 50 bugs is not meaningful.**

### 13.3 Per-bug tables — p_crash / p_fix / delta, ranked high→low

Per bug over its **k=5 samples per arm** (VALID rows only): **p_crash** = fraction that say *crash* on the
buggy arm (higher = better; this is per-bug TPR / "correct when a bug exists"); **p_fix** = fraction that
say *crash* on the patched arm (lower = better; this is per-bug FPR / "false-alarm when no bug"); the
per-bug "correct-no-bug" rate is `1 − p_fix`. **delta = p_crash − p_fix** (paired discriminability;
delta > 0 ⇒ separates the arms the right way). Counts shown as (says-crash / valid-samples).

> ⚠ **This is a MEAN RATE over 5 samples, NOT pass@5.** p_crash = the *average* says-crash rate across the
> 5 crash-arm rollouts (e.g. 0.60 = 3 of 5 said crash). pass@5 would be the more lenient "≥1 of 5 correct"
> and is reported separately in §7.2 — do not read these p-values as pass@5.

**flash-3-preview** (`runs/v4disc_norm_flash3_traj`) — mean delta **+0.087**, delta >0/=0/<0 = **21/15/14**

| rank | bug | project | san | p_crash (exist) | p_fix (no-bug err) | delta |
|---|---|---|---|---|---|---|
| 1 | 42532755 | gdal | asan | 0.80 (4/5) | 0.20 (1/5) | +0.60 |
| 2 | 42496870 | json | asan | 1.00 (5/5) | 0.40 (2/5) | +0.60 |
| 3 | 42475467 | libspng | asan | 0.60 (3/5) | 0.00 (0/5) | +0.60 |
| 4 | 42481010 | wavpack | msan | 0.40 (2/5) | 0.00 (0/5) | +0.40 |
| 5 | 42529061 | ghostpdl | asan | 0.40 (2/5) | 0.00 (0/5) | +0.40 |
| 6 | 42492491 | libssh2 | asan | 0.40 (2/5) | 0.00 (0/5) | +0.40 |
| 7 | 42474904 | wget2 | asan | 0.40 (2/5) | 0.00 (0/5) | +0.40 |
| 8 | 42509806 | ffmpeg | asan | 0.40 (2/5) | 0.00 (0/5) | +0.40 |
| 9 | 42493031 | ffmpeg | asan | 0.40 (2/5) | 0.00 (0/5) | +0.40 |
| 10 | 42506732 | unrar | msan | 0.60 (3/5) | 0.20 (1/5) | +0.40 |
| 11 | 42479381 | openh264 | asan | 0.60 (3/5) | 0.20 (1/5) | +0.40 |
| 12 | 42513063 | libvpx | msan | 0.60 (3/5) | 0.20 (1/5) | +0.40 |
| 13 | 42520935 | mupdf | asan | 0.60 (3/5) | 0.20 (1/5) | +0.40 |
| 14 | 42487091 | openexr | asan | 0.25 (1/4) | 0.00 (0/5) | +0.25 |
| 15 | 42488096 | openh264 | asan | 0.40 (2/5) | 0.20 (1/5) | +0.20 |
| 16 | 42490854 | kimageformats | msan | 0.40 (2/5) | 0.20 (1/5) | +0.20 |
| 17 | 42510346 | libvips | asan | 0.20 (1/5) | 0.00 (0/5) | +0.20 |
| 18 | 42496296 | wasm3 | msan | 0.40 (2/5) | 0.20 (1/5) | +0.20 |
| 19 | 42492002 | leptonica | asan | 0.40 (2/5) | 0.20 (1/5) | +0.20 |
| 20 | 42474174 | mupdf | msan | 0.75 (3/4) | 0.60 (3/5) | +0.15 |
| 21 | 42475500 | karchive | msan | 0.40 (2/5) | 0.25 (1/4) | +0.15 |
| 22 | 42503478 | imagemagick | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 23 | 42516694 | haproxy | asan | 0.40 (2/5) | 0.40 (2/5) | +0.00 |
| 24 | 42499386 | mruby | asan | 0.20 (1/5) | 0.20 (1/5) | +0.00 |
| 25 | 42485317 | mruby | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 26 | 42490829 | kimageformats | msan | 0.40 (2/5) | 0.40 (2/5) | +0.00 |
| 27 | 42516681 | gpsd | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 28 | 42490490 | glib | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 29 | 42495801 | wasm3 | asan | 0.20 (1/5) | 0.20 (1/5) | +0.00 |
| 30 | 42502085 | file | asan | 0.60 (3/5) | 0.60 (3/5) | +0.00 |
| 31 | 42470928 | librawspeed | msan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 32 | 42522648 | libxml2 | msan | 0.20 (1/5) | 0.20 (1/5) | +0.00 |
| 33 | 42522792 | simdutf | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 34 | 42495278 | skcms | msan | 0.20 (1/5) | 0.20 (1/5) | +0.00 |
| 35 | 42498988 | fmt | asan | 0.20 (1/5) | 0.20 (1/5) | +0.00 |
| 36 | 42474170 | capstonenext | msan | 0.20 (1/5) | 0.20 (1/5) | +0.00 |
| 37 | 42531416 | ndpi | asan | 0.40 (2/5) | 0.60 (3/5) | -0.20 |
| 38 | 42481045 | uWebSockets | asan | 0.80 (4/5) | 1.00 (5/5) | -0.20 |
| 39 | 42506977 | gdal | asan | 0.40 (2/5) | 0.60 (3/5) | -0.20 |
| 40 | 42510353 | libvips | ubsan | 0.20 (1/5) | 0.40 (2/5) | -0.20 |
| 41 | 42475254 | qtbase | msan | 0.20 (1/5) | 0.40 (2/5) | -0.20 |
| 42 | 42481822 | harfbuzz | asan | 0.00 (0/5) | 0.20 (1/5) | -0.20 |
| 43 | 42481120 | harfbuzz | asan | 0.00 (0/5) | 0.20 (1/5) | -0.20 |
| 44 | 42526333 | libraw | asan | 0.20 (1/5) | 0.40 (2/5) | -0.20 |
| 45 | 42488393 | openh264 | asan | 0.00 (0/5) | 0.20 (1/5) | -0.20 |
| 46 | 42498672 | re2 | asan | 0.00 (0/5) | 0.20 (1/5) | -0.20 |
| 47 | 42522642 | libxslt | msan | 0.20 (1/5) | 0.40 (2/5) | -0.20 |
| 48 | 42482052 | pcre2 | asan | 0.00 (0/5) | 0.20 (1/5) | -0.20 |
| 49 | 42499827 | spice-usbredir | asan | 0.20 (1/5) | 0.40 (2/5) | -0.20 |
| 50 | 42481135 | harfbuzz | asan | 0.00 (0/5) | 0.40 (2/5) | -0.40 |

**gemini-3.5-flash** (`runs/v4disc_norm_g35_traj`) — mean delta **+0.031**, delta >0/=0/<0 = **10/32/8**

| rank | bug | project | san | p_crash (exist) | p_fix (no-bug err) | delta |
|---|---|---|---|---|---|---|
| 1 | 42474904 | wget2 | asan | 1.00 (4/4) | 0.20 (1/5) | +0.80 |
| 2 | 42496870 | json | asan | 0.60 (3/5) | 0.00 (0/5) | +0.60 |
| 3 | 42532755 | gdal | asan | 0.40 (2/5) | 0.00 (0/5) | +0.40 |
| 4 | 42490829 | kimageformats | msan | 0.40 (2/5) | 0.00 (0/5) | +0.40 |
| 5 | 42510353 | libvips | ubsan | 0.20 (1/5) | 0.00 (0/5) | +0.20 |
| 6 | 42506732 | unrar | msan | 0.20 (1/5) | 0.00 (0/5) | +0.20 |
| 7 | 42490854 | kimageformats | msan | 0.40 (2/5) | 0.20 (1/5) | +0.20 |
| 8 | 42526333 | libraw | asan | 0.20 (1/5) | 0.00 (0/5) | +0.20 |
| 9 | 42475467 | libspng | asan | 0.20 (1/5) | 0.00 (0/4) | +0.20 |
| 10 | 42522642 | libxslt | msan | 0.20 (1/5) | 0.00 (0/5) | +0.20 |
| 11 | 42503478 | imagemagick | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 12 | 42481010 | wavpack | msan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 13 | 42499386 | mruby | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 14 | 42485317 | mruby | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 15 | 42516681 | gpsd | asan | 0.00 (0/4) | 0.00 (0/5) | +0.00 |
| 16 | 42529061 | ghostpdl | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 17 | 42481822 | harfbuzz | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 18 | 42490490 | glib | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 19 | 42488096 | openh264 | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 20 | 42495801 | wasm3 | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 21 | 42502085 | file | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 22 | 42481120 | harfbuzz | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 23 | 42487091 | openexr | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 24 | 42531416 | ndpi | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 25 | 42492491 | libssh2 | asan | 0.20 (1/5) | 0.20 (1/5) | +0.00 |
| 26 | 42481135 | harfbuzz | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 27 | 42479381 | openh264 | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 28 | 42488393 | openh264 | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 29 | 42470928 | librawspeed | msan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 30 | 42498672 | re2 | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 31 | 42522648 | libxml2 | msan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 32 | 42522792 | simdutf | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 33 | 42496296 | wasm3 | msan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 34 | 42482052 | pcre2 | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 35 | 42492002 | leptonica | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 36 | 42493031 | ffmpeg | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 37 | 42495278 | skcms | msan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 38 | 42506977 | gdal | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 39 | 42498988 | fmt | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 40 | 42499827 | spice-usbredir | asan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 41 | 42520935 | mupdf | asan | 0.20 (1/5) | 0.20 (1/5) | +0.00 |
| 42 | 42474170 | capstonenext | msan | 0.00 (0/5) | 0.00 (0/5) | +0.00 |
| 43 | 42475254 | qtbase | msan | 0.20 (1/5) | 0.40 (2/5) | -0.20 |
| 44 | 42516694 | haproxy | asan | 0.00 (0/5) | 0.20 (1/5) | -0.20 |
| 45 | 42510346 | libvips | asan | 0.00 (0/5) | 0.20 (1/5) | -0.20 |
| 46 | 42474174 | mupdf | msan | 0.20 (1/5) | 0.40 (2/5) | -0.20 |
| 47 | 42509806 | ffmpeg | asan | 0.00 (0/5) | 0.20 (1/5) | -0.20 |
| 48 | 42513063 | libvpx | msan | 0.00 (0/5) | 0.20 (1/5) | -0.20 |
| 49 | 42475500 | karchive | msan | 0.00 (0/5) | 0.25 (1/4) | -0.25 |
| 50 | 42481045 | uWebSockets | asan | 0.00 (0/5) | 0.40 (2/5) | -0.40 |

**⚠ These per-bug deltas are NOT stable across resampled runs.** The identical-setup prior `v4disc_norm`
run put bug `42490829` (kimageformats) at the very top with delta **+1.00** (p_crash 1.00 / p_fix 0.00);
in this new flash3 run the same bug is **+0.00** (0.40 / 0.40). n=5 samples/arm means each p-value has a
large binomial error (±~0.2), so the ranking churns run-to-run — read the *distribution* (mean delta, the
>0/=0/<0 split), not any single bug's rank. This is the §13.2 sampling-noise point at the per-bug grain.

### 13.4 Do the trajectories show real reasoning? (spot-check, high-delta bugs)

**Yes — the per-rollout reasoning is captured and legible.** Two layers survive per rollout:
1. the **full action trace** — every `bash` command the agent ran (what it chose to inspect, in what order),
2. the **stated verdict `reason`** — a 1–3 sentence technical justification the agent wrote into
   `prediction.json` (mirrored in each manifest row's `verdict_raw.reason`).

The ONE thing NOT captured is Gemini's *private* chain-of-thought: the thinking tokens come back as an
encrypted `thought_signature` (used for context caching), so `thinking_blocks` is empty and the raw internal
monologue is unreadable. But the model's own explanation of its verdict IS there and is genuinely
high-quality — the trajectories serve their purpose.

**Example — wget2 `42474904`** (g35's top-delta bug: p_crash 1.00 / p_fix 0.20). The `reason` strings show
real root-cause analysis, consistent across the 4 valid crash-arm samples:
- crash-arm (correctly says crash): *"In libwget/iri.c:253, parsing the `&apos;` entity advances the source
  pointer s by 6, but does not use `continue` (unlike `amp;`, `gt;`, `lt;`, `quot;`). This causes the code
  to fall through to `*d++ = *s++`, which increments s past the terminating null byte, resulting in an
  out-of-bounds read/write on the heap."* — the exact file:line, the exact mechanism (missing `continue`),
  and the right crash class. This is code-reading, not guessing.
- fix-arm (correctly says no-crash): *"The input handles XML entities like `&apos;` safely in the IRI
  unescaping code since string boundary checks and null-termination are properly respected."* — it reads the
  patched tree (the added `continue`) and flips its verdict accordingly.

**The action trace also shows the agent TRYING to cheat via git — and the normalization denying it.** On
both arms it ran `git log`, `git blame libwget/iri.c`, `git show <sha>`, and targeted
`git log --grep="apos"` / `--grep="unescape" -p` — reaching for fix-commit messages / blame to tell the arms
apart. On the leak-normalized image the history is one synthetic arm-neutral commit at EPOCH, so these
return nothing useful and it is forced back to the source. Direct evidence the §5 normalization is
load-bearing: the model's instinct is to exploit git metadata, and the environment closes that channel.

**The `reason` field also explains the sampling variance** (why delta isn't +1.00 every time). On the SAME
wget2 crash arm, flash3 split 2/5: samples s3/s4 found the bug (*"Missing continue statement … s incremented
beyond the null terminator"*) while s0–s2 talked themselves out of it (*"the code correctly handles `&apos;`
… buffer has enough space"*). Same model, same code, different reasoning paths per sample — exactly the
temp>0 noise §13.2 describes, now visible at the token level. Likewise on json `42496870`, flash3's two
fix-arm false-alarms come with plausible-but-wrong OOB reasoning about `get_from_vector`/indefinite arrays,
i.e. genuine over-eagerness on bounds-check-heavy code, not random flips.

Caveat: these are high-delta bugs where the agent engaged. On the many delta≈0 bugs (esp. g35's 32 ties) the
`reason` strings are short and default to no-crash after a shallow look. The spot-check shows the capability
is real *when it engages* (consistent with the strong pass@5 latent-ability finding, §7.3), not that it
engages on every bug.

### 13.5 Does the reasoning track the delta? (study across the delta spectrum)

Tested the intuition "high delta ⇒ reasoning makes sense; negative delta ⇒ it shouldn't" by reading the
`reason` + `exit_status` of every sample for bugs at each delta level. The intuition is broadly right, but
the mechanism at the bottom is **failed engagement + noise**, not coherent-but-inverted reasoning.

**HIGH delta (→ +1): reasoning is sound.** e.g. json `42496870` (flash3 p_crash 1.00) — all 5 crash-arm
samples correctly pin the OOB in `from_cbor_internal`/`get_from_vector` (unchecked `idx`/`current_index +
sizeof(T)` past the vector). wget2 `42474904` (g35, §13.4) names the exact `iri.c:253` missing-`continue`
mechanism. The fix arm mostly clears correctly. Even the stray fix-arm false-alarms are *plausible
over-eagerness* on genuinely bounds-check-heavy code, not gibberish.

**ZERO delta: coherent but NON-DISCRIMINATING — two flavors.**
- *both-cold (0.00 / 0.00, the common g35 tie):* the crash-arm `reason` strings are short placeholders
  ("Initial assessment", "no obvious issues") and the runs mostly `LimitsExceeded` — the model never
  actually engaged; the forced first-verdict (no-crash) just survived. Ties by default, not by insight.
- *both-hot (e.g. file `42502085`, flash3 0.60 / 0.60):* the model finds a REAL-looking bug
  (`cdf_read_property_info` heap-overflow on a `CDF_LENGTH32_STRING` loop) and emits essentially the SAME
  root cause on BOTH arms — it cannot see that the patch fixed it. Coherent reasoning, but the fix is too
  subtle for static reading to distinguish. Delta≈0 = "can't tell the fix apart," not "bad reasoning."

**NEGATIVE delta: your intuition holds, but it's UNGROUNDED noise, not backwards logic.** The negative-delta
bugs are ones where the model **failed to find the true bug on the crash arm** and its few "crash" calls are
speculative false-alarms that landed on the fix arm by chance. The `exit_status` is the tell:
- uWebSockets `42481045` (g35, delta −0.40): crash arm = 4/5 `LimitsExceeded` with "Initial safe
  prediction" placeholders (budget ran out, never understood it); the 2 fix-arm "crash" calls are the only
  ones that *submitted*, each a different speculative guess (`getHeaders` lowercase write, empty-topic
  size-0 alloc) on the PATCHED tree.
- karchive `42475500` (g35, −0.25): identical shape — crash arm all placeholder/LimitsExceeded no-crash;
  one fix-arm speculative MSAN "uninitialized read" guess.
- harfbuzz `42481135` (flash3, −0.40): crash arm 0/5 (mostly "Initial assessment"); fix arm's 2 crash calls
  are speculative (`StructAtOffsetOrNull` user offset, `blend_arg_t::set_blends` resize) — not the real bug.

So negative delta ≠ "the model coherently argues the fixed code crashes." It = the model **never grounded a
root cause** (placeholder/step-exhausted no-crash on the buggy arm) and emitted scattered speculative crash
calls that happened to fall more on the fix arm.

**⚠ Statistical caveat:** with k=5, a delta of −0.20 is literally ONE extra fix-arm false-alarm (0/5 vs
1/5) — inside binomial noise (SE ≈ 0.2). The negative tail is the **noise floor**, not a real anti-signal;
the reasoning study just confirms those crash calls are ungrounded speculation, exactly what noise looks
like. Read the *distribution* (mean delta, >0/=0/<0 split), never an individual negative rank.

## Appendix A — Complete `v4_discrim/` directory inventory

Everything under `v4_discrim/`. Note that `lib/` and `vendor/` are **largely inherited** from the
earlier ARVO Phase 3–5 *single-site crash-prediction* pipeline ("where does it crash", presupposing a
crash); the v4 discrimination work **reuses a subset** and adds its own top-level modules. "✓ on v4
path" marks files actually exercised by items 1–3.

### A.1 Top-level modules (the v4 discrimination code)
| file | role |
|---|---|
| `build_dataset.py` ✓ | item 1a — freeze the 50-bug set; `--adjacent` builds `dataset_adjacent.json` |
| `run_verdict.py` ✓ | item 1 harness — one rollout in raw fetched source (`arvo-sandbox:base`) |
| `run_verdict_img.py` ✓ | item 3 harness — one rollout inside the normalized per-bug image |
| `batch_verdict.py` ✓ | the batch driver (both items; `--images` selects item 3) |
| `discriminability.py` ✓ | the metric — TPR/FPR/Youden J/accuracy/paired-Δ from a manifest |
| `score_verdict.py` ✓ | the single scoring path → `vendor/exec_rl/reward_v4` |
| `verdict_schema.py` ✓ | the v4 crash/no-crash verdict format + host-side validity gate |
| `gt_adapter.py` ✓ | adapt ARVO `ground_truth.json` → the gold dict shape reward_v4 expects |
| `build_patch_corpus.py` ✓ | item 2b — build the commentless oracle patches |
| `build_shard_arvo.py` ✓ | item 2e — ARVO build worker (materialize fix^ tree, build+push both arms) |
| `measure_fix_distance.py` | diagnostic — how many commits separate ARVO's verified crash from fix (→ `fix_distance.json`); motivated the adjacent-set decision |
| `launch_item3.sh` ✓ | item-3 launcher/guard — waits for the 2g build report, then runs the sweep + metrics |

### A.2 `lib/` — inherited shared library (Phase 3–5 prediction pipeline)
| file | role | on v4 path? |
|---|---|---|
| `sandbox_contract.py` | sandbox layout + `lockdown_flags()` | ✓ (both harnesses) |
| `fetch_source.py` | materialize source at a commit into a dir | ✓ (item 1) |
| `run_prediction.py` | single-site prediction harness | ✓ (v4 reuses `SafeLitellmModel`, templates, `build_mounts`, `cache_stats`) |
| `select_bugs.py` | list runnable bugs from `data/` | ✓ (build_dataset) |
| `strip_comments.py` | `strip_diff()` — strip patch comments | ✓ (build_patch_corpus) |
| `answer_schema.py` | the single-site "where" answer format | ✗ (v4 uses `verdict_schema` instead) |
| `batch_predict.py` | Phase 4 batch driver for prediction | ✗ (batch_verdict mirrors its contract) |
| `crash_parser.py` | parse sanitizer crash reports | ✗ (upstream harvest/gt) |
| `frame_clean.py` | Phase 5b — clean gold frames | ✗ (feeds gt/reward upstream) |
| `reward.py` | Phase 5a — single-site crash-site reward | ✗ (v4 uses reward_v4) |
| `score.py` | Phase 5c/5d — prediction validity gate + scoring | ✗ (v4 uses score_verdict) |
| `difficulty.py` | Phase 5g — difficulty banding / split | ✗ |
| `launch_sandbox.py` | Phase 3d — manual sandbox launcher | ✗ |
| `Dockerfile.sandbox` | builds `arvo-sandbox:base` (the item-1 base image) | ✓ (indirectly) |

### A.3 `vendor/` — vendored exec-rl reward code
| path | role | on v4 path? |
|---|---|---|
| `exec_rl/reward_v4.py` | **the v4 discrimination reward** (`parse_crash_verdict`, `score_crash_verdict`) | ✓ |
| `exec_rl/reward.py`, `exec_rl/reward_v2/` | earlier reward versions (v2 IDF/superlinear chain) | ✗ (superseded by v4) |
| `exec_rl/crash_taxonomy.py` | crash-class taxonomy | ✗ (indirect) |
| `exec_rl/__init__.py`, `exec_rl/models/` | package glue / model configs | ✗ |
| `v3_discrim_run.py` | a v3 discrimination-run reference script | ✗ |

### A.4 `discrim-env-images/` — the leak-normalization build assets
| path | role |
|---|---|
| `Dockerfile.arvo` ✓ | **the ARVO normalization Dockerfile** used by item 2 (§5.2) |
| `Dockerfile` | Chenxi's ORIGINAL kernel Dockerfile (`sysintel-{bug}-{parent|fix}-commit`) — reference/source |
| `README.md` | Chenxi's build-pipeline guide for adapting the sysintel pipeline to ARVO |
| `build_shard.sh` | Chenxi's original kernel build worker — reference (`build_shard_arvo.py` is the ARVO port) |
| `patch_corpus_adjacent.json` ✓ + `patches_adjacent/` (50) ✓ | **the adjacent corpus actually used** |
| `patch_corpus.json` + `patches/` (50) | the ORIGINAL non-adjacent corpus (superseded) |
| `build_report_adjacent.json` ✓ | 2g build result (`pushed: True`, 50/50 ok) |
| `build_report.json` | original (non-adjacent) build report |
| `fix_distance.json` | output of `measure_fix_distance.py` |
| `ctx/` | scratch docker build context (`tree/` + one `.patch`), left from the last build |

### A.5 Data, runs, and state
| path | role |
|---|---|
| `data/` | **5510** harvested ARVO bug folders (`meta.json`, `poc`, `ground_truth.json`, per-arm `src_*`/`answer_*`). The full corpus; only the 50 adjacent bugs are used for items 1–3 |
| `dataset_adjacent.json` ✓ | the frozen 50-bug adjacent set (the experiment) |
| `dataset.json` | the original non-adjacent 50-bug set (superseded) |
| `adjacency_cache.json` | cached `fix^ == vuln` git checks (built by `build_dataset.py --adjacent`) |
| `runs/v4disc_adj/` ✓ | **item 1 baseline** output (`manifest.jsonl` + `discriminability.json`) |
| `runs/v4disc_norm/` ✓ | **item 3 normalized** output (`manifest.jsonl` + `discriminability.json`) |
| `runs/v4disc/` | the ORIGINAL sweep on the non-adjacent `dataset.json` (has `run.log`) — superseded |
| `patches/` (top-level) | empty stray dir |
| `__pycache__/` | bytecode cache |

### A.6 Logs
| file | what it captured |
|---|---|
| `v4disc_adj_stdout.log` | item 1 (adjacent baseline) sweep stdout |
| `v4disc_norm_stdout.log` / `v4disc_norm_metrics.log` | item 3 sweep stdout / its discriminability output |
| `build2g_stdout.log` | item 2g build+push stdout |
| `patchcorpus_adjacent.log`, `patchcorpus_adjacent2.log`, `_patchcorpus.log` | patch-corpus build runs |
| `reselect_adjacent.log` | adjacent-set reselection run |
| `_distcheck.log` | fix-distance / distance-check run |

### A.7 Security note (standing constraint)
`GEMINI_API_KEY` is exported **only** in the `v4disc` tmux shell and is passed to child processes by
inheritance — it is **never written to any file** in this directory (no `.env`, not in any script). Any
reproduction must re-export it in that shell. All batch/metric commands use
`/home/jinghezhang/ARVO_intelligence/.venv/bin/python` (the root venv, which has `minisweagent`; a bare
`python3` fails `ModuleNotFoundError`).
