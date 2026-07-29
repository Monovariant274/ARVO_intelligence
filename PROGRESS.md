# ARVO Intelligence — Progress & Resume Notes

Handoff doc. Read this to resume without re-deriving context.

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
  harvester built, validated 5/5 against the DB (skia, graphicsmagick, harfbuzz — all PASS:
  crash_type/fix_commit match DB, top frame matches independent re-parse, PoC + vuln_commit
  captured). Re-checked integrity on the full auto-harvested batch (not just the 5 manual
  examples): as of this writing 485 folders on disk, all structurally clean (poc + ground_truth.json
  + meta.json present, valid JSON) except 1 known `ok(no-poc)` case; harvest.log shows no real
  errors. **Restarted 2026-07-29 as a single unified run** in tmux session `harvest` → `./data`:
  `python3 harvest.py pull --limit 7000 --require-frames` (**`--sanitizer` filter dropped**), so one
  run now covers the full ~6,067-usable pool across asan+msan+ubsan instead of capping at asan's
  ~4,253. Resume verified at restart: 557 clean folders on disk, integrity check found 0 half-written
  folders (no `ground_truth.json`-without-`meta.json` cases). `--limit 7000` > total 6,138 DB rows so
  it's effectively uncapped; resume skips by `ground_truth.json` existence, so the 557 already-done
  (asan) bugs are skipped and it harvests forward past them (confirmed manifest ticking up + docker
  pulling the next `localId`s live). Rate-capped by Docker Hub's anonymous 100 pulls/hr (~30 bugs/hr
  serial) — the remaining ~5,500 bugs are ~7–8 days of wall-clock; box must stay up.
  **This supersedes the earlier two-run plan** (finish asan-only at 2000, then a second filter-removed
  pull) — it's now one filter-free run. Also pulls msan/ubsan images now; fine for harvesting (we
  parse *recorded* reports, not re-run) — the MSAN-flakiness caveat only bites if we regenerate crashes.
  **Not a blocker for Phase 3** — the harvested bugs are plenty to build/test the sandbox against;
  growing toward ~6,067 keeps happening passively in the background.
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
  - [x] 3h. **RAN END-TO-END 2026-07-29 against a real model — blocker cleared.** Run task 3 (crash
    prediction) for real through mini-swe-agent instead of a hand-simulated dry run.
    `msagent_runner/` (new folder): dedicated venv (`.venv`, `mini-swe-agent` installed editable
    from the vendored `sysintel-msagent` copy) + `run_prediction.py`. That script: fetches source
    (3b), starts mini-swe-agent's `DockerEnvironment` pointed at `arvo-sandbox:base` (3c) using the
    *same* `sandbox_contract.lockdown_flags()` + `.git`-mask mounts as the manual launcher (3a/3d) —
    mini-swe-agent always starts its own container rather than attaching to one we launch, so this
    reuses the contract's flags rather than re-declaring them (refactored `sandbox_contract.py` to
    expose `lockdown_flags()` + public `EMPTY_MASK_DIR` for this). Prompt built from 3g's
    `answer_schema.prompt_instructions()`, plus only `project`/`sanitizer` from `meta.json` (never
    `crash_type`/`fix_commit` — those stay host-side, same leak class as the `.git` mask fixes).
    After the agent finishes, `run_prediction.py` reads back `answer/prediction.json` and validates it
    (3g). The LLM call happens on the *host* (litellm → provider); the container never needs network,
    so `--network none` holds.

    **First real run (2026-07-29), skia bug 40096184 via `vertex_ai/gemini-2.5-flash`:** sandbox
    enforced exactly as designed (`--network none --read-only --cap-drop ALL --user <hostuid>`, `.git`
    masked), agent wrote a **valid, validated** `prediction.json`. Accuracy vs. the hidden ground
    truth was genuinely good: predicted `filename` = `src/codec/SkSwizzler.cpp` and `function` =
    `SkSwizzler::swizzle` both land on the real crash stack (gt depth-1 frame); `line` (335 vs 237/
    1233) and `crash_type` (heap-buffer-underflow vs -overflow) were off — note `reward.py` scores
    only file/function/line, not crash type, so the type miss won't hurt the numeric reward.

    **Bug found + fixed during that run — `RepeatedFormatError`.** `LitellmModel` uses native
    tool-calls; `parse_toolcall_actions` raises `FormatError` when a response contains **no** bash
    tool call, and `DefaultAgent` aborts after `max_consecutive_format_errors` (default **3**) in a
    row. Gemini, once it thought it was done, kept replying with a plain-text "final answer" instead
    of calling the bash tool to run the finish sentinel → 3 format errors → `RepeatedFormatError`
    (n_calls=22). The prediction survived only because the file-writing tool calls had already run;
    the clean `Submitted` exit (env checks first stdout line == `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
    with rc 0, see `environments/docker.py:_check_finished`) was never reached. Fix in
    `run_prediction.py`: (a) hardened `SYSTEM_TEMPLATE` + `INSTANCE_TEMPLATE` — every response MUST be
    a bash tool call, and the finish sentinel must be issued *as* a bash tool call, never as prose;
    (b) raised `max_consecutive_format_errors=8` so a stray prose turn gets nudged back instead of
    aborting (step_limit/cost_limit still bound runaway).

    **Second bug found + fixed — stale answer read-back.** `run()` did `answer_dir.mkdir(exist_ok=True)`
    but never cleared a prior run's `prediction.json`, so a run that wrote *no* answer would read back
    and print the **previous** run's file as if it were its own (silently poisoning any Phase-4 data).
    Caught it when a `gemini-3.5-flash` run that hit `LimitsExceeded` printed a prediction byte-identical
    to an earlier `2.5-flash` run. Fix: `prediction_path.unlink(missing_ok=True)` at the start of
    `run()`, so a no-write run now honestly reports `no valid prediction: ... agent never wrote an answer`.

    **Third fix — convergence for weaker models (budget nudge).** `gemini-3.5-flash` explores more and
    burned its whole 30- then 45-step budget without ever writing an answer. Fixed without touching the
    sandbox: (a) instance prompt now tells the agent to write an initial best-effort prediction within
    its first few steps and keep overwriting it; (b) a custom `OBSERVATION_TEMPLATE` appends a live
    `<budget>used {{n_model_calls}} of {{step_limit}} steps</budget>` line to every observation so the
    model self-paces. (`step_limit`/`n_model_calls` come from `DefaultAgent.get_template_vars()`.)

    **VERIFIED CLEAN (2026-07-29), skia bug 40096184 via `vertex_ai/gemini-3.5-flash`, step-limit 45:**
    `exit_status='Submitted'` (clean sentinel finish, not `RepeatedFormatError`/`LimitsExceeded`),
    n_calls=45, cost **$0.3426**. Prediction was **freshly written this run** (4 write-to-answer commands
    — the early-write nudge worked) and validated. Accuracy vs. hidden ground truth: `filename` =
    `third_party/gif/SkGifImageReader.cpp` ✓ and `function` = `SkGIFLZWContext::doLZW` ✓ both match the
    real depth-4 crash frame, `crash_type` = heap-buffer-overflow ✓ (correct this time), `line` 213 vs
    299 ✗. **3h is functionally complete** — locked sandbox + real model + clean submit + on-stack
    prediction, all verified.

    **Cost note (not a bug, but a Phase-6 scaling flag):** $0.34/run is expected for 45 flash steps, but
    per-call cost grew ~2.7× across the run ($0.0028 → $0.0075) because mini-swe-agent resends the whole
    growing conversation every step, and **no prompt caching is configured** (`set_cache_control=None`).
    The 3.5 run also submitted on the *very last* allowed step (45/45) — a bigger source tree might not
    finish at 45. At RL scale (~$0.34 × thousands of bugs × many rollouts) this balloons. Levers before
    Phase 6: (1) enable `set_cache_control` on `LitellmModel` **(RETRACTED — see 4a: implicit
    caching already active, flag would break Gemini)**; (2) reconsider `2.5-flash`, which reached
    a comparable on-stack answer in ~22 steps for ~$0.08 (~4× cheaper) — worth a head-to-head re-run
    *with these fixes* before committing a model; (3) tune step-limit/rollout count. **Model decision
    (2026-07-29): standardizing on `vertex_ai/gemini-3.5-flash` for Phase 4+** (user call). It gave the
    correct crash type and a clean `Submitted` finish; the ~4× cost gap vs 2.5-flash is deferred to the
    Phase-6 caching/step-limit tuning above rather than resolved by model downgrade.

    **Robustness check across diverse bugs (2026-07-29) — two more fixes landed in
    `run_prediction.py`:**
    - **Output truncation (fix):** harfbuzz (42470093) blew up to **$3.41** because the agent `cat`'d a
      492 KB source file; with no truncation that single 492 K-char observation was ~85% of the resent
      context and got re-sent every step, so cost exploded and it never refined past its placeholder
      guess. Added `truncate(8000, True, …)` to `OBSERVATION_TEMPLATE` (verified: 500 KB input → ~8.2 K
      chars rendered; small outputs untouched) plus a nudge to use grep/sed/head instead of whole-file
      `cat`.
    - **Empty-choices crash (fix):** imagemagick (42470067) died with `IndexError: list index out of
      range` at `response.choices[0]` (litellm_model.py:129) — Vertex/Gemini returned **zero candidates**
      (safety/recitation filter). litellm's retry wraps `_query`, not `_parse_actions`, so it doesn't
      catch this; it crashed the whole run. Two-part fix, both in our code (vendored mini-swe-agent is
      read-only): (1) `SAFETY_SETTINGS` = all Gemini harm categories → `BLOCK_NONE`, passed via
      `model_kwargs={"safety_settings": …}` on `LitellmModel` (kills the common safety-block cause;
      recitation blocks ignore it); (2) `agent.run()` wrapped in try/except → a mid-run crash records
      `exit_status="crashed: …"`, prints a clean line, and still falls through to `validate_prediction()`
      so any partial answer on disk survives and one bad bug can't abort a batch.
    - **Results by bug (all four now validated post-fix):** ffmpeg (40096184) **PASS** — near-exact
      (`decode_move` rsac.c:299, all fields match). gdal (42470702) **PASS** — clean `Submitted`, 44
      calls, **$2.23**, real heap-buffer-overflow prediction (`ogrgmldatasource.cpp`
      `FindAndParseTopElements` line 2781). imagemagick (42470067) **PASS post-fix** — ran 3× with **no
      traceback** (IndexError fix holds); Submitted once at $0.95, `LimitsExceeded` twice at
      $0.31/$0.44, each with a valid `use-of-uninitialized-value` prediction. harfbuzz (42470093)
      **PASS post-fix** — the headline truncation win: **$0.40** vs the old **$3.41** (~8.5× cheaper),
      real heap-buffer-overflow prediction (`hb-ot-layout-gsub-table.hh` `LigatureSet::sanitize` line
      761) instead of the old placeholder.
    - **On `LimitsExceeded` vs `Submitted`:** several re-runs ended `LimitsExceeded` at n_calls=45 —
      this is **not** a failure. They hit the 45-*step* cap (costs $0.31–$0.44, nowhere near the cost
      limit), and the early-write instruction guarantees a validated prediction was already on disk.
      That graceful "out of steps but answer exists" path is by design; a clean `Submitted` is nicer but
      not required to pass. Model non-determinism drives whether a given run wraps up early or runs to 45.
    - **Cost flag reinforced:** gdal's **$2.23 / 44 steps** (vs the 40096184 baseline of $0.34) confirms
      cost scales hard with source size + step count and with no prompt caching. The Phase-6 levers
      (enable `set_cache_control`, tune step-limit, revisit 2.5-flash) matter more than the single-run
      numbers suggested.

**Model-access blocker: RESOLVED via Vertex AI (2026-07-29).** The mentor's "no plain key" path
worked: calling `vertex_ai/gemini-2.5-flash` through litellm with `VERTEXAI_LOCATION=global` succeeded
end-to-end (real billed run, cost ~$0.08). This supersedes the earlier open question about which auth
route to use — Vertex is the chosen path. (Historical context, now moot: the VM's built-in service
account ADC lacked the `cloud-platform` scope Vertex needs; whatever credential the user exported for
this run — personal ADC login or a service-account key — carries the right scope. The exact
credential/project used wasn't passed through chat.) **3h now fully closed** — the clean `Submitted`
run above confirms it.

**How to re-run the 3h smoke test (from the user's shell, with Vertex creds already exported):**
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
  - [x] **4a. Prompt caching — RESOLVED 2026-07-29, opposite of the planned fix.** Investigated
    before touching code: trajectories from the Phase-3 runs show Gemini's **implicit caching is
    already active** and litellm already bills cached tokens at a deep discount — measured across
    all 5 saved trajectories: **78–86% of prompt tokens were cache hits**, i.e. runs already cost
    ~3–4x less than uncached (gdal $2.23 actual vs ~$9.80 est. uncached; 40096184 $0.34 vs ~$1.03).
    **Decision: do NOT enable `set_cache_control` — it would actively break Gemini runs.** That flag
    is Anthropic-semantics (mark the last message = "cache prefix up to here"); mini-swe-agent marks
    only the final message, and litellm's Vertex path (`separate_cached_messages` in
    `llms/vertex_ai/context_caching/transformation.py`) would instead carve that lone message into a
    per-step **explicit** cachedContents API call with a scrambled message split + min-token
    requirements. This supersedes the Phase-6 "enable set_cache_control" lever from the 3h cost
    notes; the remaining real cost levers are step-limit tuning and keeping observations small
    (truncation, already landed). Deliverable: `cache_stats_line()` in `run_prediction.py` now
    prints `cache: N/M prompt tokens cached (P%)` after every run (tested offline against the saved
    40096184 trajectory: 83%) — if a future run shows ~0%, caching regressed and cost jumps 3–4x.
  - [x] **4b. Per-bug result record — DONE 2026-07-29.** `run()` in `run_prediction.py` now writes
    `data/<id>/result.json` after every run (alongside `trajectory.json`): bug_id, project,
    sanitizer, model, step_limit, cost_limit, exit_status, n_calls, cost, cache stats (4a),
    the validated prediction (or `null` + `invalid_reason`), started_at, duration. Stale
    `result.json` is unlinked at run start (same hazard class as the 3h stale-prediction bug —
    a run that dies early must not leave a prior run's record looking current). Also refactored
    4a's `cache_stats_line` → `cache_stats()` returning a dict so the record and the printed line
    share one computation. **Cost de-prioritized (user call, 2026-07-29):** cost/cache numbers are
    recorded for visibility but are no longer a decision driver unless egregious. Verified offline
    (no billed run) by monkeypatching `DockerEnvironment`/`DefaultAgent` over the real 40096184
    folder + saved trajectory: happy path (Submitted + validated prediction → full record) and
    crash path (IndexError mid-run → `exit_status="crashed: …"`, `prediction: null`,
    `invalid_reason` set, record still written). Test artifacts cleaned up; real prediction.json
    restored. Note for 4d: `run()`'s return value is now this same record dict — the batch runner
    can consume it directly instead of re-reading result.json.
  - [x] **4c. Bug-selection helper — DONE 2026-07-29.** `select_bugs.py` (repo root, stdlib-only
    like the harvester). `runnable_bugs(data_dir, sanitizer=, project=, skip_done=)` for library use
    (4d imports this); CLI prints ids/paths or a `--count` summary. Runnable = folder complete
    (`poc` + valid `ground_truth.json` + valid `meta.json`) — requiring `meta.json` (written *last*
    by harvest.py) makes it safe against the live harvest: half-written folders simply aren't listed
    yet, and `ok(no-poc)` bugs are excluded by the poc check. `--skip-done` excludes bugs that
    already have a 4b `result.json` (resume primitive for 4d). `--shuffle --seed N --limit K` gives
    a reproducible diverse sample (for 4f). Tested live against the in-flight harvest: 651 runnable
    across 73 projects (asan 557 / msan 85 / ubsan 9), filters + reproducible shuffle verified,
    `--skip-done` verified with a planted result.json (651→650, bug absent from list).
  - [x] **4d. Batch runner (serial first) — BUILT + harness-tested 2026-07-29.** `batch_predict.py`
    (repo root): selects bugs via 4c's `runnable_bugs()`, loops 4b's `run()` over them, writes one
    manifest row per bug to `runs/<name>/manifest.jsonl` (append+flush, harvester pattern). One bug's
    failure never kills the batch: `run()`'s own mid-run guard covers agent crashes, and batch_predict
    additionally wraps `run()` so a *setup-stage* failure (source fetch / docker start, which happens
    before run()'s internal try) is recorded as a `batch-error` row and the loop continues. Resumable
    by default via `skip_done` (skips bugs with a 4b `result.json`; `--redo` forces re-run). Running
    cost total + `--max-cost` aborts cleanly *before* starting a bug that would exceed the budget.
    Defaults standardized on `vertex_ai/gemini-3.5-flash`, step-limit 45, per-bug cost-limit 3 (matches
    the 3h smoke command). **Verified without any billed call** by stubbing `run()`: error isolation
    (a raised bug recorded + batch continued), manifest rows written for every bug, cost accounting,
    and `--max-cost` abort all confirmed. **Not yet run against a real billed batch — that IS 4f.**
    Run it with:
    ```bash
    cd ~/ARVO_intelligence && source .venv/bin/activate
    export VERTEXAI_LOCATION=global
    python batch_predict.py --limit 25 --shuffle --name shakeout   # 4f shakeout
    ```
  - [x] **4e. Source-tree disk hygiene — DONE 2026-07-29.** `cleanup_source(dest)` added to
    `fetch_source.py` (the module that owns the src lifecycle) — `shutil.rmtree`, safe when absent,
    returns whether it removed anything. `batch_predict.py --cleanup-src` calls it on `data/<id>/src`
    after *every* bug, right after the manifest write, so it fires on the error path too (a setup
    failure can leave a partial `src/`). Only the checkout is deleted — `poc`/`ground_truth.json`/
    `meta.json`/`result.json` stay; re-fetch is the ~5s shallow fetch. Verified without billing:
    unit-checked the helper (removes existing, returns False when absent) and ran the batch with a
    stubbed `run()` that creates a src tree (and raises on one bug) — all three src dirs gone after,
    manifest + counts intact.
  - [x] **4f. Shakeout batch + review — DONE 2026-07-29. GATE: PASS.** 30-bug billed batch launched:
    `python batch_predict.py --limit 30 --shuffle --cleanup-src --name shakeout` (model
    `vertex_ai/gemini-3.5-flash`, step-limit 45), manifest at `runs/shakeout/manifest.jsonl`, log
    `shakeout.log`. **Vertex access is via the VM's built-in service-account ADC** (default SA,
    project `triangulate-396717`, `cloud-platform` scope) after the earlier permission-change+reboot
    — NO interactive login and NO exported key needed; just `export VERTEXAI_LOCATION=global`. This
    corrects the 3h speculation about "personal ADC login or a service-account key" — it's the VM SA.
    Verified before spending: `google.auth.default()` mints a token, and a 1-token litellm call to
    `vertex_ai/gemini-3.5-flash` returned "ok".
    **Partial results (24/30 done):** ~18/24 valid predictions, 6 clean `Submitted`; the many
    `LimitsExceeded` are the by-design "out of steps but early-write left a valid answer" path (not
    failures). **5 no-prediction bugs analyzed → 3 root causes:**
      1. *Literal-placeholder* (aom 42470283, harfbuzz 42470395): agent wrote a placeholder
         (`filename:"unknown"`, `line:0`) as its early write and never refined it → rejected by
         `answer_schema.validate_prediction` (line must be ≥1).
      2. *Never wrote at all* (binutils-gdb 42479120, radare2 42475544): burned all 45 steps
         exploring, 0 writes to the answer file (confirmed in trajectory).
      3. *Source-fetch fail* (graphicsmagick 42473496): repo on `foss.heptapod.net` (a Mercurial
         host) — `git` can't fetch it. **77 bugs in the full DB, all graphicsmagick (1.25%).**
         **RESOLVED as a skip 2026-07-29** — investigated recovery and it's genuinely impossible: the
         recorded `vuln_commit` is an hg changeset that no longer resolves on the live heptapod repo
         (history stripped) AND doesn't map to the GitHub mirror's git SHAs; the source only ever
         existed in the discarded ARVO Docker image (harvest used `--source ref`). So Mercurial support
         would NOT help (verified: `hg identify -r <commit>` → "unknown revision"). Fix: `select_bugs.py`
         now excludes unfetchable hosts by default (`UNFETCHABLE_HOST_MARKERS=("heptapod",)`,
         `skip_unfetchable=True`; `--include-unfetchable` to override), so these never get selected and
         can't pollute a batch. Recovering them would require re-harvesting with `--source tar` from the
         Docker image + teaching Phase-3 to mount a saved tree — not worth it for 1.25%.
    **Fix applied for causes 1+2:** hardened `run_prediction.py` `INSTANCE_TEMPLATE` — the agent's
    VERY FIRST tool call must write a *concrete* prediction (real file+function from the tree, nonzero
    line; "unknown"/0 explicitly forbidden as "counts as no prediction"), before any exploration.
    Prompt-only; sandbox and validator untouched. Applies to future batches, not the in-flight
    shakeout (separate process, module already loaded). **A/B re-run of the 4 fixable bugs is queued**
    as a background job that waits for the shakeout to exit, then re-runs binutils-gdb/radare2/aom/
    harfbuzz through the new prompt (graphicsmagick excluded — its failure is the heptapod data issue,
    not the prompt). Review/gate verdict pending both jobs finishing.
    **Phase-5 note surfaced here:** reward wiring must treat a missing/invalid prediction as **score
    0**, not an error — "no answer" is a legitimate bad RL outcome, not a pipeline crash.
    **FINAL RESULTS (both jobs landed 2026-07-29):** shakeout 30/30 → **25/30 valid (83%)** on the old
    prompt, 10 clean `Submitted`, 18 `LimitsExceeded`-but-valid, 2 crashed/errored. Cost **$9.89 total,
    $0.33 mean / $0.29 median / $0.84 max per bug** — tame and predictable, no cost blocker. The A/B
    re-run of the 4 prompt-fixable bugs through the hardened `INSTANCE_TEMPLATE` **converted all 4/4
    to valid** (binutils-gdb + harfbuzz clean `Submitted`, radare2 + aom `LimitsExceeded`-but-valid;
    every one wrote a concrete file/function/nonzero-line, no more `unknown`/0). So **effective
    valid-rate = 29/30**, with the sole remaining miss being graphicsmagick's Heptapod fetch (the
    deferred 21-bug data-source issue, not a pipeline defect). **GATE VERDICT: PASS** — healthy
    completion, understood+fixed failure modes, predictable cost. 4g (parallelism) skipped: serial
    throughput is fine. NOTE: accuracy vs. ground truth is NOT scored yet — that is Phase 5; 4f only
    gates *pipeline health*, not prediction quality.
    **Both post-4f loose ends RESOLVED 2026-07-29:** (1) Heptapod/graphicsmagick — skip filter in
    `select_bugs.py` (see 4f cause #3 above). (2) Vertex empty-candidates `IndexError` — this used to
    only be *contained* (SAFETY_SETTINGS + a try/except that recorded `crashed` and stopped the run).
    Now properly *handled*: `run_prediction.py` has a `SafeLitellmModel(LitellmModel)` subclass whose
    `query()` retries an empty (zero-candidate) response a few times, then raises `FormatError` instead
    of `IndexError` — `DefaultAgent` appends a nudge and the run *continues* (bounded by
    `max_consecutive_format_errors`) rather than crashing. Retries don't burn step budget (`n_calls`
    increments per step, not per API call). Verified: simulated empty response → retry → `FormatError`,
    no `IndexError` leak. The old try/except stays as a last-resort backstop.
  - [x] **4g. (Optional) modest parallelism — SKIPPED 2026-07-29.** Serial throughput (~$0.33 and a
    couple min/bug) is fine and 4f passed on it; Docker + Vertex quotas get riskier concurrent, so not
    worth the added risk. Revisit only if the Phase-5 k-sample difficulty sweep over the full set
    proves too slow serially.
- [ ] **Phase 5 — Reward wiring** (reuse `exec-rl/reward.py`; remap crash taxonomy to sanitizer types;
  canonicalize frame filenames — see Known issues).
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
