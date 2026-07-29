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
  errors. **Still running** in tmux session `harvest` → `./data` (asan-only, `--limit 2000`).
  Rate-capped by Docker Hub's anonymous 100 pulls/hr (~30 bugs/hr serial) — 2000 will take days,
  not one night; ~250-300/night was the original (too optimistic) estimate.
  **Decision:** let this run finish untouched (no mid-run edits). Once it hits 2000, kick off a
  **second pull with `--sanitizer` filter removed** (asan-only caps at ~4,253 usable; dropping the
  filter is required to reach the full ~6,067 usable pool across asan+msan+ubsan). That second run
  is also resumable/idempotent, so it can just be re-run with a bigger `--limit` later if needed.
  **Not a blocker for Phase 3** — 485 harvested bugs is plenty to build and test the sandbox against;
  growing to 2000/6067 keeps happening passively in the background.
- [ ] **Phase 3 — Sandboxed prediction env** (Docker: source + PoC + python, but NO build/run of
  target). **← Starting now.** Reuse note: exec-rl's Docker/agent-loop *plumbing* (via
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
  - [ ] 3h. Superseded by the mentor's plan: run task 3 (crash prediction) for real through
    mini-swe-agent with an actual model instead of a hand-simulated dry run.
- [ ] **Phase 4 — Agent loop** (reuse mini-swe-agent `KernelAgent`; new prompt + mount contract).
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
