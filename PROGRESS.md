# ARVO Intelligence — Progress & Resume Notes

Handoff doc. Read this to resume without re-deriving context.

---

## LIVE STATUS (as of 2026-07-29 10:43) — two long jobs running, safe to leave

- **sweep1 (Phase 5g pass@k)** — tmux session `sweep1`, launched 10:37. 100 bugs × k=8 = 800 runs,
  ~128s each → **~28h ETA**, ~$264 (cap $300). First rows landing. Resumable; survives disconnect.
  - Check:   `wc -l runs/sweep1/manifest.jsonl`  (target 800)  ·  `tail -f runs/sweep1.log`
  - **When it finishes → LAST STEP OF PHASE 5:** `python difficulty.py runs/sweep1/manifest.jsonl`
    (writes `runs/sweep1/split.json`). If it died, re-run the same `batch_predict.py` cmd (see 5g).
  - **Early-stop OK (~50 fully-done bugs) for a PILOT Phase 6:** stop the sweep (`pkill -f 'batch_predict.*sweep1'`,
    safe to kill mid-bug), then band only complete bugs: `python difficulty.py runs/sweep1/manifest.jsonl --min-samples 8`.
    Resumable later to grow the set before a serious run. NOTE: early runs are slower/pricier than the
    128s shakeout est (~200s, ~$0.40/run on bug 42470348), so the $300 cap may bind before 800 runs.
- **harvest (Phase 2)** — tmux session `harvest`. At ~[478/6138], ~73 candidates/hr → **~3 days ETA**.
  Independent of sweep1 (sweep locked its 100 bugs at launch). Re-run `python3 frame_clean.py` after.
  - Check:   `wc -l data/manifest.jsonl`  ·  `tail -f harvest.log`  ·  `tmux ls`

---

## 1. Goal (one sentence)

Replicate the **`exec-rl`** crash-prediction RL system from the `system-intelligence`
repo, but on **user-space ARVO bugs** (OSS-Fuzz) instead of Linux-kernel Syzbot bugs.

## 2. Background

- **`system-intelligence`** (`~/sample_repo/system-intelligence`, branch `sysintel`) =
  the "Live-kBench" project. Its main product is a benchmark for AI agents that *fix*
  Linux-kernel crashes (heavy infra: `kGym`, QEMU, kernel VMs).
- **`exec-rl`** (`~/sample_repo/system-intelligence/exec-rl/exec_rl/`) is a newer,
  different piece inside it. **Key finding: exec-rl is NOT patch generation — it trains an
  agent to PREDICT a crash.** Given source + a triggering input (crash report hidden), the
  agent predicts *where* it crashes (stack frames: function/file/line) and *what type*.
  A reward scores the prediction; RL (verl trainer) improves the agent over rounds.
  - `reward.py` → `score_crash_prediction(...)`: depth-decayed scoring of predicted
    frame vs. ordered ground-truth frames.
  - `models/entities.py` → `CrashGroundTruthFrame(depth, function, filename, line)` is the
    exact ground-truth shape we must produce.
  - `crash_taxonomy.py` → crash-type label set (kernel-flavored; we remap to sanitizer types).
- **ARVO** = dataset of **6,138 reproducible OSS-Fuzz crashes across 311 projects**.
  DB at `~/ARVO/arvo.db` (SQLite, table `arvo`). Its win: each bug reproduces via a Docker
  image `n132/arvo:<localId>-vul` — no kernel VMs needed.

## 3. The task we are building

Give the agent: **vulnerable source tree + the PoC input file** (in a sandboxed env).
Agent reads code, runs light Python heuristics, and **predicts the crash** (type + frames).
Crash report is the hidden label. **Critical design rule for later:** the sandbox must NOT
let the agent build-and-run the instrumented target — otherwise it just runs the PoC and
reads the answer (reward hacking). ARVO makes running trivial, so we must actively remove it.

The training tuple = `(PoC input, vulnerable source, crash ground-truth)`.

## 4. Data viability — CONFIRMED GOOD (make-or-break passed)

Measured on the full ARVO DB:

- **6,067 / 6,138 (98.8%) bugs yield a usable symbolized crash location** (ordered
  function/file:line frames). ASAN subset: 4,253 / 4,293 (99.1%).
- Coarse crash types (usable bugs): heap-buffer-overflow 2,290 · use-of-uninit 1,176 ·
  other 1,002 · heap-use-after-free 477 · stack-buffer-overflow 397 · index-out-of-bounds
  237 · global-buffer-overflow 185 · (long tail).
- Sanitizers in DB: asan 4,293 · msan 1,293 · ubsan 552.
- Every metadata field (crash_type, sanitizer, fix_commit, repo_addr, crash_output) is 100% populated.

**Harvesting mechanic (proven on real bugs):**
- PoC input lives inside the image at `/tmp/poc` (tiny: 22–236 bytes).
- Vulnerable source lives at `/src/<project>/` with `.git` checked out at the
  **parent-of-fix commit** (that commit is what we record as `vuln_commit`).
- Crash ground-truth is parsed from the DB `crash_output` column (no Docker needed for that).
- **Cost:** cold `docker pull` ≈ **137 s for 8.3 GB**; images range 1.6–8.9 GB. We extract the
  few-KB PoC + record repo/commit, then `docker rmi` the image, so **disk stays flat**; the
  real budget is pull bandwidth.

## 5. What's built (this repo, `~/ARVO_intelligence`)

| File | Purpose |
|---|---|
| `harvest.py` | Main runnable script. Subcommands `stats` (DB-only viability) and `pull` (Docker harvest). |
| `crash_parser.py` | Parses `crash_output` → `{crash_type_coarse, frames[]}`. Isolates the first (crash) stack, caps at N frames. |
| `.gitignore` | Excludes `data*/`, `__pycache__`. |

Stdlib only (sqlite3, subprocess, argparse) — no pip install needed. Requires Docker for `pull`.

## 6. How to run

```bash
cd ~/ARVO_intelligence

# --- viability report, no Docker, runs in seconds ---
python3 harvest.py stats                      # whole DB
python3 harvest.py stats --sanitizer asan     # asan only  (choices: asan|msan|ubsan)
python3 harvest.py stats --project curl       # one project
python3 harvest.py stats --max-frames 5       # frames kept per bug (default 10)

# --- harvest tuples (pulls Docker images) ---
python3 harvest.py pull --sanitizer asan --limit 5 --require-frames --out ./data
```

`pull` arguments:

| Flag | Default | Meaning |
|---|---|---|
| `--sanitizer {asan,msan,ubsan}` | (all) | filter by sanitizer (asan = cleanest) |
| `--project NAME` | (all) | filter to one project |
| `--limit N` | 5 | how many bugs to harvest |
| `--out DIR` | ./data | output directory |
| `--source {ref,tar}` | ref | `ref` records repo+commit only (cheap); `tar` also saves the source tree to `source.tar` |
| `--require-frames` | off | skip the ~70 frameless bugs (bare SEGVs) |
| `--keep-images` | off | don't `docker rmi` after extract (faster re-runs, eats disk) |
| `--db PATH` | `~/ARVO/arvo.db` | global flag, before the subcommand |

`pull` is **resumable**: it skips any bug whose `<id>/ground_truth.json` already exists.

### Output layout (one folder per bug)
```
data/<localId>/
  poc                # the crash-triggering input bytes  (agent INPUT)
  ground_truth.json  # crash_type_coarse + ordered frames (HIDDEN label)
  meta.json          # project, sanitizer, repo_addr, vuln_commit, fix_commit
data/manifest.jsonl  # one line per bug: status, poc_bytes, frame count
```

## 7. Phase plan & status

- [x] **Phase 1 — Define task** (predict crash_type + ordered frames). Matches exec-rl reward shape.
- [~] **Phase 2 — Build dataset & prove viable.** Build work DONE: viability confirmed (98.8%),
  harvester built, validated 5/5 against the DB (crash_type/fix_commit match, top frame matches an
  independent re-parse, PoC + vuln_commit captured); integrity re-checked on the full auto-harvested
  batch (all folders structurally clean bar 1 known `ok(no-poc)` case). **Running as a single unified
  pull** in tmux session `harvest` → `./data`: `python3 harvest.py pull --limit 7000 --require-frames`
  (no `--sanitizer` filter, so it covers the full ~6,067-usable pool across asan+msan+ubsan;
  `--limit 7000` > 6,138 DB rows = effectively uncapped). Resume skips by `ground_truth.json`
  existence. Rate-capped by Docker Hub's anonymous 100 pulls/hr (~30 bugs/hr serial) → remaining
  ~5,500 bugs ≈ 7–8 days wall-clock; box must stay up. msan/ubsan images are fine to harvest (we
  parse *recorded* reports, not re-run; the MSAN-flakiness caveat only bites if we regenerate crashes).
  **Not a blocker for Phase 3+** — harvested bugs are plenty to build/test against; the set grows
  passively in the background.
- [x] **Phase 3 — Sandboxed prediction env** (Docker: source + PoC + python, but NO build/run of
  target). **COMPLETE 2026-07-29** — all of 3a–3h done; end-to-end verified with a clean `Submitted`
  run of a real model (`vertex_ai/gemini-3.5-flash`) producing a validated, on-stack prediction inside
  the locked sandbox. Reuse note: exec-rl's Docker/agent-loop *plumbing* (via
  `mini-swe-agent`'s `get_environment(..., default_type="docker")` in `rl/agent.py`) is generic and
  reusable. The container *contents* are not — exec-rl's `kenv`/`kArena` image is kernel-specific
  (QEMU, kernel build tooling) and was never designed to hide build/run capability, since that
  wasn't a cheating risk for kernel bugs. For ARVO it is the central risk (images ship a full build
  toolchain + working repro), so the stripped-down, can't-build container is new work, not a port.
  **Note:** current harvest uses `--source ref` (repo+commit only, no source saved) — Phase 3 must
  `git clone` + checkout the recorded `vuln_commit` itself; the source tree isn't on disk yet.
  - [x] 3a. Sandbox contract written: `sandbox_contract.py`. Fixed paths (`/workspace/src`,
    `/workspace/poc` read-only; `/workspace/answer/prediction.json` the only writable path),
    `--network none`, `--read-only` root fs, capabilities dropped. `BASE_IMAGE` name reserved
    for step 3c (not built yet, so `main()` here is a dry-run command printer).
  - [x] 3b. `fetch_source.py`: turns a bug's `meta.json` (`repo_addr` + `vuln_commit`) into a real
    checked-out source tree at `data/<id>/src/`. Tries a shallow fetch-by-commit first, falls back
    to a full clone if the host rejects it. Tested on lwan (github) and skia (googlesource) — both
    succeeded via the shallow path (5s/1s); the full-clone fallback exists in code but hasn't
    actually been exercised yet since no tested host has rejected the shallow fetch so far. Cached
    re-run confirmed (skips re-fetching if already at the target commit). Output feeds directly
    into `sandbox_contract.py`'s `src_dir` — confirmed end-to-end on the lwan bug.
  - [x] 3c. `Dockerfile.sandbox` → image `arvo-sandbox:base`: `python:3.12-slim` + one non-root
    fallback user, nothing else installed. Verified no gcc/cc/make/cmake/g++ present. Full
    contract test (real lwan bug, via `sandbox_contract.py`'s actual generated command): source
    and poc visible read-only, root fs read-only, writing to `/workspace/src` blocked, outbound
    network blocked (`Network is unreachable`), python3 works. **Bug found + fixed during testing:**
    the image's fixed uid-1000 user couldn't write the host-owned answer dir (uid mismatch) —
    switched to launching with `--user $(id -u):$(id -g)` (host's own uid) instead of a hardcoded
    image user; re-tested, answer dir now writable while src/root stay locked.
  - [x] 3d. `launch_sandbox.py`: given just a bug folder, calls 3b to fetch source (if not already
    cached) then 3c/3a's contract to start the locked container — one command from bug ID to
    running sandbox. Tested end-to-end on a fresh bug (no pre-existing `src/`): source fetched,
    container started, source/poc readable, gcc blocked, and a write to `/workspace/answer/` was
    confirmed to actually land back on the host disk (round-trip verified, not just inside the
    container). Also supports `--cmd` for non-interactive one-off runs (used for this test; will
    double as the shape Phase 4's real agent calls need).
  - [x] 3e+3f. `verify_sandbox.py`: automated pass/fail checks (not manual pokes), re-runnable any
    time the image/contract changes. Lockdown check actually invokes the target project's OWN
    build system (`cmake .` / `make`) inside the sandbox and asserts both fail (exit 127, "command
    not found") — stronger than just checking `which gcc`. Usability check runs a real pure-python
    heuristic (walk source tree, grep for a pattern, read poc bytes) and asserts it completes
    cleanly. Ran on two different bugs (lwan: 111 src files, skia: 1521 src files) — both PASS on
    lockdown and usability.

**Security gap found + fixed (post-3e/3f):** `vuln_commit` is defined as the parent of the fix
commit, so the very next commit in real git history *is* the fix — often with a commit message
that gives the crash away outright. The full-clone fallback in `fetch_source.py` (never actually
triggered so far, but written) would carry that full history into the sandbox; `git log --all`
would leak it even though `--depth 1` shallow fetches happen to be safe on their own. Fixed in
`sandbox_contract.py`: an empty read-only directory is now mounted on top of `{SRC_MOUNT}/.git`,
hiding all git history from the agent regardless of which fetch path was used. Verified: `.git`
appears empty inside the sandbox, `git` isn't even installed in the base image (belt and
suspenders), and `verify_sandbox.py` still PASSes both checks after the change.
  - [x] 3g. `answer_schema.py`: agent writes ONE predicted crash site (not a full stack) to
    `ANSWER_FILE` — `crash_type_coarse`, `filename`, `function`, `line`, `reasoning`. Deliberately
    matches the *shape* of exec-rl's `CrashPrediction` (single site, scored against every
    ground-truth frame with depth-decay in `reward.py`) rather than its camelCase field names —
    Phase 5 needs a taxonomy-remap adapter regardless (see known issues), so name-matching now
    buys nothing; shape-matching is what avoids a reward.py rewrite. Note: `reward.py`'s actual
    scoring math doesn't use crash type at all, only file/function/line — `crash_type_coarse` and
    `reasoning` are for the LLM-judge / review step, not the numeric reward. `prompt_instructions()`
    generates the exact text to give the agent later; `validate_prediction()` checks a submitted
    answer. Tested both ways: accepts a well-formed prediction, rejects (with a specific error) a
    missing file and one missing required fields — confirmed against the incomplete file
    accidentally left over from 3d's test.
  - [x] 3h. **RAN END-TO-END 2026-07-29 against a real model — COMPLETE.** `msagent_runner/`
    (new folder): dedicated venv (`.venv`, `mini-swe-agent` installed editable from the vendored
    `sysintel-msagent` copy) + `run_prediction.py`. That script fetches source (3b), starts
    mini-swe-agent's `DockerEnvironment` on `arvo-sandbox:base` (3c) reusing
    `sandbox_contract.lockdown_flags()` + `.git`-mask mounts (mini-swe-agent starts its own
    container, so `sandbox_contract.py` was refactored to expose `lockdown_flags()` +
    `EMPTY_MASK_DIR`), builds the prompt from `answer_schema.prompt_instructions()` + only
    `project`/`sanitizer` from `meta.json` (never `crash_type`/`fix_commit`), then reads back and
    validates `answer/prediction.json`. LLM call runs on the *host*; container stays `--network none`.
    Verified clean end-to-end: skia bug 40096184 via `vertex_ai/gemini-3.5-flash`, step-limit 45,
    `exit_status='Submitted'`, cost ~$0.34, freshly-written validated prediction landing on the real
    crash frame (`SkGifImageReader.cpp` / `SkGIFLZWContext::doLZW`, type correct).

    **Fixes discovered during 3h, all now living in `run_prediction.py` code** (kept here only as a
    changelog; details are in the source):
    - *RepeatedFormatError* — Gemini emitted a prose "final answer" instead of the bash finish
      sentinel → 3 consecutive format errors aborted the run. Fix: hardened `SYSTEM`/`INSTANCE`
      templates (every response must be a bash tool call; finish only via sentinel) +
      `max_consecutive_format_errors=8`.
    - *Stale answer read-back* — `run()` never cleared a prior `prediction.json`, so a no-write run
      reported the previous run's answer. Fix: `unlink(missing_ok=True)` at run start.
    - *Convergence/budget nudge* — weaker models burned the whole budget without writing. Fix:
      prompt tells the agent to write a best-effort prediction in its first steps and keep overwriting;
      `OBSERVATION_TEMPLATE` appends a live `<budget>used N of LIMIT steps</budget>` line.
    - *Output truncation* — a `cat` of a 492 KB file made one observation ~85% of the resent context
      ($3.41 run). Fix: `truncate(8000)` in `OBSERVATION_TEMPLATE` + a grep/sed/head nudge (harfbuzz
      42470093: $3.41 → $0.40).
    - *Empty-choices IndexError* — Vertex/Gemini returned zero candidates (safety/recitation) →
      `response.choices[0]` crashed the run. Fix: `SAFETY_SETTINGS` all → `BLOCK_NONE`, plus
      `agent.run()` wrapped so a mid-run crash records `exit_status="crashed: …"` and still validates
      any partial answer. (Later fully *handled*, not just contained — see 4f `SafeLitellmModel`.)

    Robustness confirmed across diverse bugs (ffmpeg/gdal/imagemagick/harfbuzz) — all produced valid,
    on-stack predictions post-fix. **Cost note (Phase-6 flag):** cost scales with source size + step
    count (gdal $2.23/44 steps vs $0.34 baseline); `LimitsExceeded` at n_calls=45 is the by-design
    "out of steps but early-write left a valid answer" path, not a failure. **Model decision: standard
    on `vertex_ai/gemini-3.5-flash` for Phase 4+** (user call).

**Model-access: RESOLVED via Vertex AI (2026-07-29).** `vertex_ai/gemini-3.5-flash` through litellm
with `VERTEXAI_LOCATION=global`, authenticated by the VM's built-in service-account ADC (see 4f for
the confirmed auth details) — no interactive login, no exported key.

**How to re-run the 3h smoke test (Vertex creds already exported):**
```bash
cd ~/ARVO_intelligence && source .venv/bin/activate
export VERTEXAI_LOCATION="global"
python msagent_runner/run_prediction.py \
  --model "vertex_ai/gemini-3.5-flash" --step-limit 45 --cost-limit 3 \
  data_smoke/40096184
# success = exit_status='Submitted' + a validated prediction printed
# (2.5-flash also works and is cheaper; see the open model decision above)
```
- [x] **Phase 4 — Batch orchestration. COMPLETE 2026-07-29** (4a–4f done; 4g skipped, serial fine).
  Re-scoped 2026-07-29: 3h's `run_prediction.py` already IS
  the working single-bug agent loop (prompt, mounts, validation, crash guards), so Phase 4 is not
  "build the agent loop" — it's running that loop over many bugs with bookkeeping, producing the
  (prediction, ground-truth) pairs Phase 5 scores. Does NOT wait on the Phase-2 harvest finishing:
  batches draw from whatever `data/` holds (~640 bugs and growing); only Phase 6 needs the full set.
  - [x] **4a. Prompt caching — RESOLVED 2026-07-29, opposite of the planned fix.** Gemini's
    **implicit caching is already active** — 78–86% of prompt tokens were cache hits across the 5
    saved trajectories (so runs already cost ~3–4× less than uncached). **Decision: do NOT enable
    `set_cache_control`** — it's Anthropic-semantics and litellm's Vertex path would mis-carve
    mini-swe-agent's single-marked message into a broken per-step explicit cachedContents call. The
    real cost levers are step-limit tuning + small observations (truncation, landed). Deliverable:
    `cache_stats()` in `run_prediction.py` prints `cache: N/M prompt tokens cached (P%)` per run — if
    a future run shows ~0%, caching regressed and cost jumps 3–4×.
  - [x] **4b. Per-bug result record — DONE 2026-07-29.** `run()` writes `data/<id>/result.json` after
    every run: bug_id, project, sanitizer, model, step/cost limits, exit_status, n_calls, cost, cache
    stats, the validated prediction (or `null` + `invalid_reason`), started_at, duration. Stale
    `result.json` unlinked at run start. `run()`'s return value IS this record dict, so the batch
    runner consumes it directly. Cost/cache recorded for visibility but **de-prioritized as a decision
    driver** (user call). Verified offline (happy + crash paths).
  - [x] **4c. Bug-selection helper — DONE 2026-07-29.** `select_bugs.py` (repo root, stdlib-only).
    `runnable_bugs(data_dir, sanitizer=, project=, skip_done=, skip_unfetchable=)` for library use;
    CLI prints ids/paths or a `--count` summary. Runnable = folder complete (`poc` + valid
    `ground_truth.json` + valid `meta.json`; requiring `meta.json`, written last, makes it safe
    against the live harvest). `--skip-done` excludes bugs with a 4b `result.json`; `--shuffle --seed
    N --limit K` gives a reproducible diverse sample. Tested live (651 runnable across 73 projects).
  - [x] **4d. Batch runner (serial) — BUILT + tested 2026-07-29.** `batch_predict.py` (repo root):
    selects via `runnable_bugs()`, loops `run()`, writes one manifest row per bug to
    `runs/<name>/manifest.jsonl` (append+flush). Failure isolation: `run()`'s own guard covers agent
    crashes, and batch_predict additionally wraps `run()` so a *setup-stage* failure (fetch/docker) is
    recorded as a `batch-error` row and the loop continues. Resumable via `skip_done` (`--redo`
    forces); `--max-cost` aborts cleanly before a bug would exceed budget. Defaults:
    `vertex_ai/gemini-3.5-flash`, step-limit 45, per-bug cost-limit 3. Verified without billing
    (stubbed `run()`: error isolation, manifest rows, cost accounting, `--max-cost` abort). Run with:
    ```bash
    cd ~/ARVO_intelligence && source .venv/bin/activate
    export VERTEXAI_LOCATION=global
    python batch_predict.py --limit 25 --shuffle --name shakeout   # 4f shakeout
    ```
  - [x] **4e. Source-tree disk hygiene — DONE 2026-07-29.** `cleanup_source(dest)` in `fetch_source.py`
    (`shutil.rmtree`, safe when absent). `batch_predict.py --cleanup-src` calls it on `data/<id>/src`
    after *every* bug (fires on the error path too). Only the checkout is deleted; poc/ground_truth/
    meta/result stay, and re-fetch is the ~5s shallow fetch. Verified without billing.
  - [x] **4f. Shakeout batch + review — DONE 2026-07-29. GATE: PASS.** 30-bug billed batch:
    `python batch_predict.py --limit 30 --shuffle --cleanup-src --name shakeout` (model
    `vertex_ai/gemini-3.5-flash`, step-limit 45), manifest `runs/shakeout/manifest.jsonl`, log
    `shakeout.log`. **Vertex auth = the VM's built-in service-account ADC** (default SA, project
    `triangulate-396717`, `cloud-platform` scope after the earlier permission-change+reboot) — no
    interactive login, no exported key; just `export VERTEXAI_LOCATION=global`. Verified before
    spending (`google.auth.default()` mints a token, 1-token litellm call returned "ok").
    **Results:** 30/30 → **25/30 valid (83%)** on the old prompt (10 clean `Submitted`, 18
    `LimitsExceeded`-but-valid, 2 crashed/errored). Cost **$9.89 total, $0.33 mean / $0.29 median /
    $0.84 max per bug** — predictable, no cost blocker.
    **5 no-prediction bugs → 3 root causes, all addressed:**
      1. *Literal-placeholder* (aom, harfbuzz): wrote `filename:"unknown"`/`line:0` and never refined
         → rejected by validator (line must be ≥1).
      2. *Never wrote at all* (binutils-gdb, radare2): burned all 45 steps exploring, 0 answer writes.
      3. *Source-fetch fail* (graphicsmagick): repo on `foss.heptapod.net` (Mercurial) — `git` can't
         fetch. 77 bugs in DB, all graphicsmagick (1.25%). Genuinely unrecoverable (hg changeset
         stripped from live repo, doesn't map to the GitHub mirror's git SHAs, source only existed in
         the discarded Docker image). **Skipped, not recovered:** `select_bugs.py` excludes unfetchable
         hosts by default (`UNFETCHABLE_HOST_MARKERS=("heptapod",)`; `--include-unfetchable` overrides).
    **Fix for causes 1+2:** hardened `run_prediction.py` `INSTANCE_TEMPLATE` — the agent's VERY FIRST
    tool call must write a *concrete* prediction (real file+function, nonzero line; "unknown"/0
    forbidden). Prompt-only. An A/B re-run of the 4 prompt-fixable bugs **converted all 4/4 to valid**,
    so **effective valid-rate = 29/30**, the sole miss being graphicsmagick's Heptapod fetch (a data
    issue, not a pipeline defect). **GATE VERDICT: PASS** — healthy completion, understood+fixed
    failure modes, predictable cost. NOTE: accuracy vs. ground truth is NOT scored yet — that's Phase 5;
    4f gates *pipeline health* only.
    **Phase-5 note:** reward wiring must treat a missing/invalid prediction as a **legitimate bad RL
    outcome, not a pipeline error** — scored `-1` per exec-rl's `INVALID_PREDICTION_REWARD` (valid-but-
    wrong = 0). See Phase 5 for the full policy.
    **Both post-4f loose ends RESOLVED:** (1) Heptapod — skip filter above. (2) Vertex empty-candidates
    `IndexError` — now *handled* (not just contained): `SafeLitellmModel(LitellmModel)` in
    `run_prediction.py` retries an empty (zero-candidate) response a few times, then raises
    `FormatError` instead of `IndexError`, so `DefaultAgent` nudges and the run *continues* (retries
    don't burn step budget; `n_calls` is per-step). The old SAFETY_SETTINGS + try/except stays as a
    last-resort backstop.
  - [x] **4g. (Optional) modest parallelism — SKIPPED 2026-07-29.** Serial throughput (~$0.33 and a
    couple min/bug) is fine and 4f passed on it; Docker + Vertex quotas get riskier concurrent, so not
    worth the added risk. Revisit only if the Phase-5 k-sample difficulty sweep over the full set
    proves too slow serially.
- [ ] **Phase 5 — Reward wiring.** Mirror exec-rl's reward-side pipeline
  (`exec-rl/exec_rl/{reward.py, ground_truth_process.py, rl/rewards.py, prediction_process.py,
  reward_v2/}`). exec-rl's scoring convention we adopt: **invalid/missing prediction → `-1`**
  (`INVALID_PREDICTION_REWARD`), **valid-but-wrong → `0`**, otherwise the score in `[0, 1]`. The
  same scoring fn must serve both Phase-6 training and Phase-7 eval so the two can't diverge
  (exec-rl's `score_context_output`/`compute_score` share one path). Substeps:
  - [x] **5a. Port the core reward — DONE 2026-07-29.** `reward.py` (repo root, stdlib-only, no
    pydantic). `score_crash_prediction(frames, function=, filename=, line=)` / `score_crash_frame` —
    exec-rl's math verbatim: `frameScore = 0.50·file + 0.30·func + 0.20·line`, function gated on
    `fileScore==1.0`, line `exp(-|Δ|/4)` gated on function, depth-decayed by `decay_base**depth` and
    normalized by Σ weights; crash *type* not scored. **Two forced ARVO deviations:** (1) `_normalize_path`
    is light/idempotent (resolve `.`/`..`, strip slashes) — the heavy `/src/<project>/` build-root strip
    lives in 5b, NOT here, so a clean `src/codec/x.cpp` doesn't collapse to `x.cpp` (that would erase
    directory discrimination = a reward-hacking hole); (2) `_normalize_function` drops the C++ arg
    signature `(...)` since ARVO symbols carry it but the agent predicts the bare name. Verified:
    single-frame exact = **1.0**, line-off-by-4 = 0.874, wrong file = 0.0, monotonic ordering. NOTE:
    for a *multi-frame* GT a perfect top-frame hit ceilings ~0.67–0.69 (single site is a depth-weighted
    average over all frames) — exec-rl's exact behavior, matters for reading 5e numbers.
  - [x] **5b. Ground-truth frame cleaning — DONE 2026-07-29.** `frame_clean.py` (repo root). Does
    deterministically (regex) what exec-rl's `ground_truth_process.py` does via an LLM: prune the
    leading sanitizer/reporting frames (`__asan_*`/`__interceptor_*`/... funcs, or files under
    `compiler-rt/`/`sanitizer_common`), re-index `depth` from 0, and canonicalize `filename` to
    repo-relative via `canonical_repo_path` (resolve `..`, drop the ARVO `/src/<project>/` build root:
    `skia/out/Fuzz/../../src/codec/SkSwizzler.cpp` → `src/codec/SkSwizzler.cpp`). **Non-destructive:**
    keeps original `frames`+`raw_file`, ADDS `frames_clean` + `excluded_frames` (mirrors exec-rl's
    `frames`/`excludedFrames` split). Ran over all data/: **153/728 (21%) had leading infra frames
    pruned**. All-infra stacks keep original frames (flagged) rather than emptying the GT. Re-runnable
    as harvest grows (idempotent). End-to-end verified: ffmpeg 42470347 `__asan_memcpy` top frame
    pruned → clean top = `decode_move libavcodec/rasc.c:299`, a correct repo-relative prediction scores
    0.687 vs 0.0 for wrong file. **Re-run `python3 frame_clean.py` once the Phase-2 harvest finishes**
    so late-arriving bugs get cleaned too.
  - [x] **5c. Prediction parse + validity policy — DONE 2026-07-29** (in `score.py` with 5d).
    Refactored `answer_schema.py` to split out `validate_prediction_data(dict)` (the field gate:
    required fields, `line` a positive int — bool rejected, non-empty filename/function) so the
    on-disk `prediction.json` path (`validate_prediction`) and the stored-dict path share ONE gate
    and can't drift. `score.parse_prediction(pred)` accepts the result.json dict / a JSON string /
    None → returns the validated dict or `None`. Policy verified: missing/empty/non-JSON/missing-
    fields/`line=0`/`line=True` all → **−1**; valid-but-wrong → **0**; valid+right → (0,1].
  - [x] **5d. Single scoring entrypoint — DONE 2026-07-29.** `score.py` (repo root), our analog of
    `rl/rewards.py`. `score_prediction(frames, prediction, reward_name) -> float` behind a
    `REWARD_REGISTRY` (`crash_site` now; room for `crash_stack` v2 = 5f): −1 invalid, 0 on a scoring
    error inside a valid pred, else reward in [0,1]. `score_result(bug_dir)` reads a bug's
    `ground_truth.json` (prefers 5b `frames_clean`, falls back to raw `frames`) + `result.json`
    prediction. `compute_score(...)` is the VeRL-shaped Phase-6 shim reading the prediction from
    `extra_info` — routes through the *same* `score_prediction`, so training and eval can't diverge
    (exec-rl's key property). CLI scores one bug / `--data DIR` / `--manifest FILE` (for 5e) and
    prints the −1/0/positive distribution. Smoke-tested on the 29 `data/*/result.json`: machinery
    works end-to-end (mean reward 0.240; proper analysis is 5e).
  - [x] **5e. Score the 4f shakeout offline — DONE 2026-07-29. First real quality numbers.**
    `python3 score.py --manifest runs/shakeout/manifest.jsonl`. Two readings:
      * **As-run (frozen pre-prompt-fix manifest, n=30):** mean reward **0.042** — dragged down by
        5×−1 (the exact no-prediction bugs from 4f). 17% invalid / 20% zero / 63% positive.
      * **Honest post-fix baseline (n=29, graphicsmagick excluded as unfetchable; the 4 fixable bugs'
        hardened-prompt predictions swapped in):** mean reward **0.240**, **0% invalid**, 28% zero,
        72% positive (positive mean **0.331**, max **0.624**). By sanitizer: asan +0.255 (n=25),
        msan +0.144 (n=4, too small to conclude). **This 0.240 is the baseline RL must move.**
    **Reward validated, not just measured:** audited the valid-but-0 bugs — every one is a *genuine*
    wrong-file miss (imagemagick predicted `MagickCore/profile.c`, crash is in LibRaw
    `read_utils.cpp`; libxslt predicted `transform.c`, crash is `xpath.c`), NOT a
    canonicalization/reward artifact. So the 0s are real, the signal is trustworthy.
    **Baseline health:** spread across all three buckets, positive mean ~half the ~0.67 multi-frame
    ceiling → headroom to improve, not saturated, not floored. Good RL starting point. **Caveat:**
    this is k=1 (one sample/bug) — it can't distinguish a *learnable* bug (sometimes hits) from a
    uniformly hard/easy one. Within-bug variance is what drives the gradient; measuring it is 5g.
  - [ ] **5f. (Optional) full-stack reward v2** — only if 5e shows single-site reward is too flat/sparse
    to give RL a gradient. Mirror `reward_v2/` (predict the call chain, alignment + IDF + segment
    bonus). Defer by default; single-site is the exec-rl v1 default.
  - [~] **5g. Difficulty banding (pass@k) — INFRA BUILT 2026-07-29, sweep PENDING user go-ahead.**
    Mirrors exec-rl's `prediction_process.py` (pass@k: run each bug `n_runs` times, `n_runs=32` there).
    Purpose: 5e's k=1 number can't tell a *learnable* bug from a uniformly hard/easy one. RL (GRPO-style)
    gets its gradient from **reward variance across the k rollouts of the same bug** — a bug whose k
    samples all score the same gives zero advantage and teaches nothing. So we measure per-bug reward
    **mean AND std** over k samples and keep the band that has a gradient.
    - **Build — DONE:** `batch_predict.py --k N` writes k manifest rows per bug, each tagged with a
      `sample` index; resume is now **sample-level** (counts existing manifest rows per bug_id, runs
      only the remaining k−done; `src` cleanup waits for a bug's last sample). k=1 keeps the old
      result.json skip for backward compat. New `difficulty.py` reads the manifest, scores every row
      via `score.score_prediction` (5d — same path as train/eval), and emits per-bug
      `{mean, std, min, max, n, n_valid}` (`difficulty.json`) + a bug-level train/test split
      (`split.json`). Smoke-tested: on the k=1 shakeout manifest all 30 bugs correctly drop (std=0,
      19 saturated / 11 dead), and band+split verified on synthetic variance.
    - **Band rule (GRPO-correct):** keep bugs with **reward std > --min-std** (default 0 → there IS a
      within-group gradient); drop *saturated* (no variance, some reward) and *dead* (no variance,
      zero/neg) bugs. The user's earlier "~30–40% pass rate" intuition = the same idea; std>0 is the
      precise version and doesn't need an arbitrary pass threshold.
    - **Train/test split:** `--test-frac` (default 0.2), split BUGS (not samples) with a fixed `--seed`,
      held out *before* training — guards against regression-to-mean / p-hacking in Phase-7 numbers.
    - **DECISION 1 — k = 8** (confirmed 2026-07-29): enough to estimate std, ~$2.6/bug; revisit if noisy.
    - **DECISION 2 — scope = ~100 bugs** (confirmed 2026-07-29): first sweep ~100 × k=8 ≈ 800 runs ≈
      ~$265, shuffled/seeded, to size the band before committing to the full set.
    - **LAUNCHED 2026-07-29 10:37** in tmux session `sweep1` (PID was 368826):
      `python batch_predict.py --k 8 --limit 100 --shuffle --seed 0 --cleanup-src --max-cost 300 --name sweep1 2>&1 | tee runs/sweep1.log`
      Per-run wall time ~128s (shakeout), so 800 runs ≈ **~28h serial**. Resumable (sample-level): if it
      dies, re-run the SAME command and it continues. `--max-cost 300` aborts before overspend (~$264 expected).
    - **WHEN THE SWEEP FINISHES → this is the last step of Phase 5:**
      `python difficulty.py runs/sweep1/manifest.jsonl` → writes `runs/sweep1/difficulty.json` +
      `runs/sweep1/split.json` (the banded train/test split Phase 6 consumes). Then eyeball the band
      size: if <~30 bugs have std>0, widen the sweep (more bugs or higher k) before Phase 6. Peek
      anytime mid-run with `--dry-run` (read-only, no files written).
    - Bridges into Phase 6 (produces its train set); lives in Phase 5 because it's reward-driven
      measurement.
- [ ] **Phase 6 — RL training** (verl trainer; the heavy GPU/infra part).
- [ ] **Phase 7 — Eval** (held-out test set; pre/post-training comparison).

## 7b. Operational notes (throughput / how to run overnight)

- **Docker Hub rate limit (checked live): 100 pulls/hour, anonymous** (`x-ratelimit-limit: 100;w=3600`).
  Not logged in. This caps any night at ~900 pulls even with parallelism.
- **Serial speed ≈ 2 min/bug** (pull 60–140s + extract + rmi). One worker ≈ 30 bugs/hr, so a serial
  run stays safely under the rate limit and yields **~250–300 bugs overnight**. No 429 failures.
- **2,000 in one night is NOT feasible** anonymously. To push toward ~700–900/night we'd need
  rate-aware parallelism (3–4 workers) — deliberately deferred (unattended concurrency = risk).
- **Run it in tmux** (survives disconnect):
  ```bash
  tmux new -s harvest                 # enter session
  cd ~/ARVO_intelligence
  python3 harvest.py pull --sanitizer asan --limit 2000 --require-frames --out ./data 2>&1 | tee harvest.log
  #   detach: Ctrl-b then d      reattach: tmux attach -t harvest
  ```
  Monitor from any terminal: `wc -l data/manifest.jsonl` · `tail -f harvest.log` · `tmux attach -t harvest`.
- Resumable + idempotent: re-running skips bugs whose `data/<id>/ground_truth.json` exists.
- **Safe way to pause a live run:** Ctrl-C while the pane shows `pulling ... (multi-GB, may take a
  few min)...`. There's a short unsafe window during `extracting PoC + source` (between
  `ground_truth.json` being written and `meta.json` being written) — interrupting exactly then
  leaves a bug with `ground_truth.json` but no `meta.json`/`poc`, which the resume-skip check
  would then silently treat as done forever. After any manual pause, sanity-check for that case:
  `python3 -c "import os; [print(d) for d in sorted(os.listdir('data')) if os.path.isdir(f'data/{d}') and os.path.exists(f'data/{d}/ground_truth.json') and not os.path.exists(f'data/{d}/meta.json')]"`
  — delete any folder it prints so it retries cleanly.

## 8. Known issues / TODO before Phase 5

- **Frame filename normalization:** `crash_parser._normalize_path` splits on the first `/src/`,
  so build paths like `skia/out/Fuzz/../../src/codec/SkSwizzler.cpp` keep a prefix. `raw_file` is
  preserved so no data is lost, but reward matching will need clean repo-relative paths
  (e.g. `src/codec/SkSwizzler.cpp`). Fix when wiring the reward.
- **PoC path assumption:** we read `/tmp/poc`; holds for the bugs tested. Harvester records
  `ok(no-poc)` if missing — watch the manifest for these on bigger batches.
- **MSAN reproducibility:** ARVO README warns MSAN is flaky to *re-run* under ASLR. Doesn't affect
  parsing recorded reports, but prefer ASAN if we ever regenerate crashes ourselves.

## 9. Key paths

- ARVO DB: `~/ARVO/arvo.db` (table `arvo`, 19 cols incl. crash_output, fix_commit, repo_addr)
- ARVO code/CLI + Docker recipe: `~/ARVO/` , dataset metadata: `~/ARVO-Meta/archive_data/`
- exec-rl reference impl: `~/sample_repo/system-intelligence/exec-rl/exec_rl/`
  (`reward.py`, `crash_taxonomy.py`, `models/entities.py`, `rl/agent.py`, `data.py`)
- Worked single example: `~/arvo_demo/42470017/` (curl — note: bare SEGV, one of the unusable ~70)
