# ARVO Intelligence — Progress & Resume Notes

Handoff doc. Read this to resume without re-deriving context.

---

## NEW DIRECTION (2026-08-02) — v4 "discrimination reward" two-arm experiment (Chenxi collab)

**Supersedes the immediate Phase-6 GRPO next-step as the active track.** User is coordinating with
Chenxi (kaloronahuang) to pivot the reward from single-site crash *location* prediction to exec-rl's
**v4 crash/no-crash discrimination reward**. This is NOT a cheap re-score of the existing sweep — it is
a new experiment with a different task shape. Recorded here so it isn't re-derived.

- **Chenxi's newer repo CLONED at `~/sample_repo/system-intelligence-kalorona`** (github
  `kaloronahuang/system-intelligence`, `main` @ `1b4610d` "two-arm-dataset" merge). Kept ALONGSIDE the
  old `~/sample_repo/system-intelligence` (branch `sysintel`, older) — both share the same remote; the
  new clone is just newer `main`. The old copy is what the rest of this doc's exec-rl references point at.
- **What v4 actually is (read from `system-intelligence-kalorona/exec-rl/exec_rl/reward_v4.py`):** a
  crash/no-crash VERDICT reward over **two arms per bug** — `crash` arm = vuln/parent commit (reproducer
  crashes), `fix` arm = fix commit (same reproducer runs clean). Agent is NOT told which arm it's in;
  it must decide crash-or-not by reading code. Scoring: fix arm → no-crash 1.0 / crash 0.0; crash arm →
  no-crash 0.0 / crash = stack-similarity S∈[0,1] (max-weight common subsequence, depth-weighted, line
  proximity on the crash-site frame). Agent output schema is a VERDICT object
  (`{crashes: bool, bugClass, reason, crashFrames[], allocFrames[], freeFrames[]}`), NOT our single
  file/function/line site — so our recorded 800 sweep3fp predictions CANNOT be re-scored under v4.
- **"Discriminability"** (the metric to measure) = how cleanly the agent separates the crash arm from the
  fix arm. Reference probe: `system-intelligence-kalorona/v3_discrim_run.py`
  (5 bugs × {crash, nocrash} × 3 samples, inference-only, captures the verdict). v4 test:
  `exec-rl/tests/test_reward_v4.py`; agent prompt: `exec-rl/agent-configs/crash-predictor.v4`.
- **Environment normalization — DOC IN HAND (2026-08-02).** Chenxi's spec is on the `sysintel` branch of
  the local clone: `docs/discrim-env-images/README.md` (+ `Dockerfile`, `build_shard.sh` beside it). Title:
  "Building Leak-Normalized Two-Arm Environment Images (Discrimination Tasks)," explicitly addressed to
  "Jinghe — adapting the sysintel discrimination-environment build pipeline to the ARVO dataset."
  Assessed SUFFICIENT to build the images. Key contents:
  - **Approach = PRE-BAKED images, not runtime normalization** — normalizing at run time caused PrepError
    storms + disk blow-up at scale, so each arm is baked into its own image ahead of time.
  - **§2 threat model = 8 leak channels, each with a fix:** (1) git history → nuke `.git`, re-init ONE
    synthetic commit; (2) commit/author timestamps → pin to a content-derived epoch; (3) file mtimes →
    `touch -h` every path to that epoch; (4) git index stat-cache → `rm -f .git/index && git read-tree HEAD`;
    (5) patch residue → apply the patch only in a DISCARDED build stage; (6) image structure/layer metadata
    → structurally identical stages for both arms; (7) layer birth-time → randomize pull/build order;
    (8) gc noise → `git config gc.auto 0`.
  - **§4 Dockerfile = 6 stages:** kenvsrc → base → pristine → {parent-prep | fix-prep} → {parent-commit |
    fix-commit}. Both arms descend from the SAME `pristine` source tree.
  - **§5 ARVO adaptations (the load-bearing part):** ⚠️ **DO NOT take the fix tree from ARVO's `-fix` image
    and the vul tree from the `-vul` image** — the two ARVO images differ in more than source (build
    timestamps, layer metadata) = leaks. Instead take source from the `-vul` image ONLY, and produce the
    fix arm by APPLYING THE FIX AS A COMMENTLESS PATCH inside `fix-prep`. Parameterize the tree path
    (`/src/<project>`, vs the kernel's `/linux`). Epoch anchor = the PARENT commit's committer timestamp
    (`ARG EPOCH`). One shared base image. ARVO trees are 10–100× smaller than the kernel tree.
  - **§3 registry:** targets GCP Artifact Registry `us-docker.pkg.dev/triangulate-396717/sysintel-env/`,
    ARVO images under prefix `sysintel-user-`, scheme `sysintel-user-arvo-{bugId}-{vul|fix}:latest`.
    (⚠️ external infra — push access to be confirmed; can build locally otherwise.)
  - **§6 footguns:** touch race → 5× retry; watch shard trailing-newline; disk discipline; idempotence;
    verify btime fingerprint; **nothing after the tree COPY may touch the tree.** §8 gives an acceptance
    checklist; §9: reference fleet = 468 bugs × 2 arms = 936 images.
  - **NEW dependency this surfaces:** we must supply a **commentless oracle patch** per bug at
    `patches/{bugId}.patch`. This is exactly the harfbuzz giveaway-comment leak found in 1a — the patch
    corpus needs a comment-stripping step. Source: diff ARVO's `vuln_commit`→`fix_commit` (both already in
    `meta.json`), strip comments.
- **Encouraging:** our `data/<id>/meta.json` already records BOTH `vuln_commit` and `fix_commit`, so the
  ARVO fix (no-crash) arm is buildable from data already harvested — no re-harvest needed for the source.

- **➡️ DETAILED ITEM 1–3 RECORD LIVES IN `v4_discrim/progress1.md`.** That standalone doc is the
  authoritative, audited write-up of items 1–3 (task contract, dataset, results, soundness audit, full
  directory inventory) PLUS **§10 = the full-corpus usable-image build (2026-08-04)**: scaling the
  two-arm image build/push to every soundly-buildable bug (`scan_usable.py` + `build_all_usable.sh`,
  resumable, ~600–640 usable projected). If you are resuming the v4 work, read `progress1.md` — this
  section here is only a high-level pointer.

- **WORKING FOLDER (2026-08-02): `v4_discrim/`** — all of items 1–4 happen here, self-contained.
  Layout: `lib/` = copies of the reusable ARVO pipeline modules (fetch_source, sandbox_contract,
  launch_sandbox, run_prediction, reward/score/difficulty/frame_clean, answer_schema, batch_predict,
  select_bugs, Dockerfile.sandbox); `vendor/exec_rl/` = Chenxi's v4 reward vendored as a minimal
  importable package (`reward_v4` + its closure: `reward`, `reward_v2/{schema,similarity}`,
  `models/entities`, `crash_taxonomy`; `reward_v2/__init__` emptied to avoid dragging idf/scorer) —
  verified `import exec_rl.reward_v4` runs standalone; `vendor/v3_discrim_run.py` = Chenxi's reference
  probe; `discrim-env-images/{README.md,Dockerfile,build_shard.sh}` = the normalization doc, extracted
  from `origin/sysintel`; `data -> ../data` symlink (both arms' commits already in each `meta.json`);
  `patches/` (for the 2b commentless patch corpus) and `runs/` scaffolded empty.
- **ITEM 1 BUILT + RUNNING (2026-08-02).** All of item 1 is implemented in `v4_discrim/` and the
  50-bug sweep is executing in tmux session **`v4disc`** (alongside the `harvest` session).
  - New code (all in `v4_discrim/`): `verdict_schema.py` (1b — arm-neutral verdict output +
    validity gate, strict subset of vendor `parse_crash_verdict`); `gt_adapter.py` (ARVO
    `ground_truth.json` → reward_v4 gold: `raw_file`→`rawLine`, synth `reason`, `bugClass` from
    `crash_type_coarse`, prefers `frames_clean`); `score_verdict.py` (1c — the ONE scoring path,
    wraps `parse_crash_verdict`+`score_crash_verdict`, `-1` for off-schema); `run_verdict.py` (1b —
    one (bug,arm,sample) rollout: arm→commit, per-arm `src_<arm>`/`answer_<arm>` dirs, ARM-NEUTRAL
    prompt with NO "a fuzzer found a crash" giveaway, reuses run_prediction's SafeLitellmModel/
    templates); `build_dataset.py` (1a — froze 50 bugs / 34 projects into `dataset.json`, seed 0,
    ≤3/project, eligibility = both commits + fetchable + ≥1 usable gold frame; eligible pool 4669);
    `batch_verdict.py` (1d — 50×2 arms×k, manifest.jsonl, (bug,arm)-level resume, bug-level
    concurrency); `discriminability.py` (1e — TPR/FPR/**Youden J**, accuracy, paired per-bug delta,
    mean v4 reward per arm; run it on the manifest when the sweep finishes).
  - **Launch (running):** `batch_verdict.py --k 5 --concurrency 4 --step-limit 40 --cost-limit 1.0
    --max-cost 80 --cleanup-src --name v4disc`, model `gemini/gemini-3-flash-preview` (paid key,
    provided by user). 500 rollouts, est ~$40–60 / a few hours. Output: `runs/v4disc/manifest.jsonl`
    (+ `run.log`). Verified end-to-end on 1 bug×2 arms before launch: full lockdown sandbox, both
    arms fetched, crash-arm verdict valid. **Watch item:** step-limit 15 was too tight (fix arm hit
    LimitsExceeded without writing → invalid); raised to 40. If invalid-rate stays high in the
    metric, raise step-limit further and re-run.
  - **When it finishes (1e):** `cd v4_discrim && python3 discriminability.py runs/v4disc/manifest.jsonl`
    → prints separation + writes `runs/v4disc/discriminability.json`. Check the tmux tail for
    `EXITCODE=` / the `batch 'v4disc' done` line; resume a partial run with the SAME command (it's
    (bug,arm)-level resumable). NOTE: item 1 runs on the EXISTING fetch-source sandbox, NOT the
    leak-normalized images (2/3) — so its discriminability is an upper-band baseline that 2/3 tighten.
- **Four-item plan (user's, with phase breakdown):**
  1. Rerun the pipeline with v4 reward, measure discriminability on 50 bugs. → 1a build two-arm dataset
     (crash+fix trees per bug from the two commits); 1b switch agent task/output to the v4 verdict schema;
     1c wire `reward_v4.score_crash_verdict`; 1d run 50 bugs × 2 arms × k samples; 1e measure crash-vs-fix
     separation (Chenxi's discriminability metric).
  2/3. Build the normalized two-arm Docker images per Chenxi's doc (`docs/discrim-env-images/README.md`).
     → 2a ✅ get doc (in hand, assessed sufficient); 2b **patch corpus — BUILT 2026-08-02** (see block
     below); 2c adapt Chenxi's 6-stage Dockerfile to ARVO (§5): source from `-vul` image only, fix arm =
     apply the commentless patch in `fix-prep`, parameterize tree path `/src/<project>`, epoch = parent
     committer timestamp; 2d run the 8-channel normalization (nuke+reinit `.git`, pin timestamps,
     `touch -h` mtimes, `read-tree`, `gc.auto=0`); 2e build both arms via `build_shard.sh`; 2f acceptance
     check per §8 (arms indistinguishable except source — btime fingerprint, no patch residue); 2g push to
     registry (or keep local — GCP access TBD).

- **⤷ NEWER v4 WORK (2026-08-04/05) NOT reflected below — see `v4_discrim/progress1.md`:** §10 full-corpus
  leak-normalized image build (576/577 bugs, 1,152 images); §11 Gemini-3.5-flash normalized sweep result
  (J +0.096 vs g3-flash-norm +0.035 — but a no-crash-bias artifact, not better crash-reading); §12 the
  trajectory-persistence fix (prior runs' per-rollout trajectories were clobbered/unrecoverable); §13 the
  in-progress trajectory-capturing RE-RUN of BOTH models (`v4disc_norm_flash3_traj` + `v4disc_norm_g35_traj`,
  running under `run_traj_sweeps.sh`, ~24 h). The block below is the original 2026-08-02 item-1/3 record.

- **★ LIVE STATUS (2026-08-02, overnight autonomous run) — items 2c–2g + item-3 harness DONE; two
  matched sweeps launched. READ THIS FIRST ON WAKE.**
  - **✅ RUN COMPLETE (~22:02) — BOTH matched sweeps done; item-3 headline below.** 2g build wrote all
    100 images; item-1-adjacent baseline (`v4disc_adj`) 500 rows (errored 25, invalid 30, cost $48.38);
    item-3 normalized (`v4disc_norm`) 500 rows (errored 0, invalid 3, cost $51.70) — auto-fired by
    `launch_item3.sh` as planned. Metrics in `runs/{v4disc_adj,v4disc_norm}/discriminability.json`.
  - **★★ ITEM-3 RESULT (audited 2026-08-03).** Same 50 adjacent bugs, k=5, gemini-3-flash-preview,
    arm-neutral prompt — only the environment differs (raw fetched source vs leak-normalized per-bug
    image). Per-arm detail (valid = on-schema verdict; says-crash rate + mean reward over VALID rows):

    | run | arm | n | valid | invalid | says-crash | mean R(valid) |
    |---|---|---|---|---|---|---|
    | adj (raw) | crash | 250 | 248 | 0.8% | 35.9% | +0.034 |
    | adj (raw) | fix   | 250 | 222 | 11.2% | 26.6% | +0.734 |
    | norm      | crash | 250 | 249 | 0.4% | 35.3% | +0.032 |
    | norm      | fix   | 250 | 248 | 0.8% | 31.9% | +0.681 |

    Pooled discriminability + paired per-bug + best-of-5 (pass@k over the 5 samples of each (bug,arm)):

    | metric | adj (raw, leaky) | norm (leak-normalized) |
    |---|---|---|
    | Youden J (TPR−FPR) | +0.093 | +0.035 |
    | accuracy | 53.6% | 51.7% |
    | TPR (crash says-crash) | 35.9% | 35.3% |
    | FPR (fix says-crash) | 26.6% | 31.9% |
    | paired mean Δ (p_crash−p_fix) | +0.088 | +0.032 |
    | paired both-arms-valid bugs | 45 | 50 |
    | pass@1 / pass@5 crash | 35.6% / 82.0% | 35.2% / 78.0% |
    | pass@1 / pass@5 fix | 65.2% / 86.0% | 67.6% / 94.0% |
    | JOINT pass@5 (both arms right ≥once) | 70.0% | 72.0% |

    **Reading (directional, NOT statistically strong — see caveats):** both regimes are only WEAKLY
    discriminating; J sits near 0 either way and accuracy is near the 50% coin-flip. Normalization nudges
    it further toward chance, driven almost entirely by FPR rising (26.6→31.9%) while TPR is flat — i.e.
    once fix-arm metadata leaks (patch comments, mtimes, index stat-cache) are gone, the agent calls
    "crash" on clean fix trees about as often as on real bugs. Consistent with the agent reading *some*
    leak signal in raw source, not purely the code; normalization strips it, as intended.
  - **⚠ SOUNDNESS CAVEATS on the adj→norm delta (do not over-claim leak-exploitation):**
    1. **Within noise.** n=50 bugs, ~250 valid rows/arm. The FPR gap of +5.3pt is ≈1.3 standard errors
       (SE≈4pt) — NOT significant; the J gap (+0.093 vs +0.035) is small vs sampling error. Treat "leaks
       were exploited" as *suggestive*, not proven. The robust statement is "both regimes ≈ chance."
    2. **Censoring is apples-to-oranges.** The raw run's fix arm had 11.2% invalid (vs 0.8% norm), so its
       FPR is measured over a smaller, censored valid sample (222 vs 248). If off-schema verdicts
       correlate with the model being confused on fix trees, the raw FPR is biased. So part of the
       adj→norm FPR move is a validity-rate artifact, not pure leak removal.
  - **NOTE — negative reward-separation is EXPECTED, not a metric inversion.** mean-reward-separation
    (crash−fix) is −0.70 (adj) / −0.65 (norm) because the crash arm's reward is localization-weighted
    (S=0.80·stack+0.15·class+0.05·allocfree, mean≈0.09 even when says-crash is correct) while the fix arm
    pays a flat 1.0 for a correct no-crash. This is the reward_v4 magnitude asymmetry (matters only for
    RL/item 4, inert for the item 1–3 binary discrimination metrics), NOT the arms being swapped.
  - **CODE AUDIT (2026-08-03) — item 1–3 path is clean.** Independent re-derivation of TPR/FPR/J/accuracy
    from both manifests matches the stored `discriminability.json` to the digit. Both runs: 500 rows, 50
    bugs, 100 (bug,arm) cells, **all exactly 5 samples, zero duplicate (bug,arm,sample), zero integrity
    anomalies** (every valid row has a bool `crashes` + reward∈[0,1]; every invalid row reward=−1). The
    two runs cover the **identical 50-bug set** → the comparison is not confounded. No bugs found.
  - **Operational win (independent of the discrimination story):** normalization slashed fix-arm invalid
    11.2%→0.8% and errored 25→0 (baked images need no git-fetch), and lifted paired-valid bugs 45→50 — so
    the normalized images are the right substrate going forward regardless of the (weak) J delta.
  - **[HISTORICAL — all three below COMPLETED, see RESULT above] What ran (two tmux sessions):**
    1. `dbuild2g` — **item 2g build+push** of the 50-bug ADJACENT set × 2 arms (100 images) to
       `us-docker.pkg.dev/triangulate-396717/sysintel-env/sysintel-user-arvo-<bug>-{vul,fix}:latest`.
       Cmd: `python3 build_shard_arvo.py --manifest discrim-env-images/patch_corpus_adjacent.json
       --push --keep --summary-out discrim-env-images/build_report_adjacent.json`. `--keep` leaves
       images on disk so item 3 reuses them without a re-pull. Check: `tail build2g_stdout.log`;
       done when `BUILD2G_EXIT=` appears + `discrim-env-images/build_report_adjacent.json` is written.
    2. `v4disc` — **item-1-adjacent baseline sweep** (raw fetched source in `arvo-sandbox:base`, the
       upper-band leaky baseline) over the SAME 50 adjacent bugs. Cmd: `python3 batch_verdict.py
       --dataset dataset_adjacent.json --k 5 --concurrency 4 --step-limit 30 --cost-limit 1.0
       --max-cost 150 --cleanup-src --name v4disc_adj`. Output `runs/v4disc_adj/manifest.jsonl`.
       Check: `tail v4disc_adj_stdout.log`; done at `ADJ_EXIT=`. NOTE the venv (`~/ARVO_intelligence/.venv`)
       must be active — `minisweagent` lives there; a bare `python3` fails `ModuleNotFoundError`.
  - **[HISTORICAL — DONE] item-3 normalized-image sweep.** Must run in the
    `v4disc` shell (the GEMINI_API_KEY is exported ONLY there; it is deliberately NOT in any file).
    Cmd (identical driver, `--images` flag): `python3 batch_verdict.py --images --dataset
    dataset_adjacent.json --k 5 --concurrency 4 --step-limit 30 --cost-limit 1.0 --max-cost 150
    --name v4disc_norm`. Then metrics on both: `python3 discriminability.py runs/v4disc_adj/manifest.jsonl`
    and `... runs/v4disc_norm/manifest.jsonl`. The adj-vs-norm J/accuracy delta = the raw-vs-normalized
    leak effect (item 3's headline result). Two matched sweeps on the SAME 50 bugs so the comparison
    isn't confounded by the bug set.
  - **ITEM 2c/2d/2e DONE — `v4_discrim/discrim-env-images/Dockerfile.arvo` + `build_shard_arvo.py`.**
    ARVO adaptation of Chenxi's 6-stage leak-normalization Dockerfile: source enters via the BUILD
    CONTEXT (not a per-bug `-vul` source image — ARVO has none at fix^); `build_shard_arvo.py`
    materializes the fix^ tree itself (`git archive <fix^> | tar -x`, no .git), passes `--build-arg
    BUG/TREE=/src/<project>/EPOCH` (EPOCH = fix^ committer `%ct`), builds `--target vul-commit` and
    `--target fix-commit` from the SAME tree. Both arms: git→1 synthetic commit @EPOCH, all mtimes
    `touch -h @EPOCH`, index stat-cache zeroed (`rm .git/index; read-tree`) then warmed
    (`checkStat=minimal`). Patch applied only in the discarded `fix-prep` stage.
  - **⚠ LEAK FOUND + FIXED during 2f (also affects Chenxi's kernel Dockerfile — flagged to user):**
    content files were @EPOCH but the tree-root dir, `.git`, and `.git/index` kept BUILD-TIME mtimes,
    which differ between the two arms' builds by the few seconds between them → a `stat()`-able channel
    telling the arms apart. Fixed by re-anchoring EVERY mtime to @EPOCH at the end of the final
    `vul-commit`/`fix-commit` RUN (`checkStat=minimal` makes the index file's own mtime irrelevant to
    the warm, so re-touching `.git/index` costs nothing). Final stages now carry `ARG EPOCH`.
  - **★ DISTANCE-1 RISK RESOLVED via ADJACENT SET (supersedes the 2b ⚠RISK block below).** The
    `fix^`-may-already-be-safe / multi-commit-fix-series hazard is eliminated by RESTRICTING the set to
    bugs where `vuln_commit == fix_commit^` (crash and fix revisions ADJACENT). Then fix^ is BOTH the
    ARVO-verified-crashing tree AND the minimal-diff base. Built with `build_dataset.py --adjacent`
    (git-checks `fix^ == vuln`, cached in `adjacency_cache.json`) → `dataset_adjacent.json` (50 bugs,
    `adjacent=true`). Patch corpus rebuilt on this set: `build_patch_corpus.py ... --out
    discrim-env-images/patch_corpus_adjacent.json` → `patches_adjacent/{bug}.patch` (50/50 clean,
    median ~2 files). All matched sweeps run on this adjacent set, NOT the original `dataset.json`.
  - **ITEM 3 HARNESS BUILT — `v4_discrim/run_verdict_img.py` + `batch_verdict.py --images`.** Same
    arm-neutral prompt / verdict schema / `score_verdict` path as item 1; the ONLY change is the
    environment: the per-bug arm IMAGE is the container base (source baked at `/src/<project>`,
    cwd=tree), mounting ONLY poc(ro)+answer(rw) — no source bind-mount, no `.git` mask (the image's
    `.git` is one synthetic arm-neutral commit). Plumbing smoke-tested on a locally-kept `-vul` image
    under the full lockdown contract: tree readable, poc readable, answer writable, python3 works.
    (`git log` fails "dubious ownership" — tree is root-owned vs the `--user` uid; harmless and
    desirable — git history stays unprobed and is arm-neutral regardless.)
  - **Small fixes made along the way:** `lib/fetch_source.py` + `build_shard_arvo.py` +
    `build_dataset.py` all learned the ffmpeg mirror (`git.ffmpeg.org` intermittently 502s →
    sha-identical GitHub mirror) to stop errored sweep rows. `build_patch_corpus.py`: `a.out =
    a.out.resolve()` so a relative `--out` doesn't break the `patch.relative_to(_HERE)` bookkeeping
    (this had silently marked all 50 adjacent rows "failed" though the patch files were written), and
    `repo_addr` is now recorded in each corpus row (the build worker needs it).

- **ITEM 2b BUILT (2026-08-02) — `v4_discrim/build_patch_corpus.py` → `discrim-env-images/patches/{bug}.patch`.**
  Per bug: diff → strip C comments from added lines (`lib/strip_comments.strip_diff`, vendored from
  Chenxi's exec-rl) → `git apply --check` → `discrim-env-images/patch_corpus.json` summary. Binary files
  (fuzz seeds/corpora) excluded from the patch — they're source-irrelevant AND can't be `git apply`-ed
  under a shallow fetch. Sequential (one scratch repo, deleted per bug) so peak disk stays flat next to
  the running sweep. Resumable.
  - **CRASH-BASE DECISION (user, 2026-08-02): base = `fix_commit^`, patch = commentless `fix^..fix`,**
    matching Chenxi's `kenv-base-<bug>-parent-commit`. Rejected the literal §5 reading (diff
    `vuln_commit`→`fix_commit`) because ARVO's `vuln_commit` is generally NOT `fix^` and can be a whole
    release behind fix (gnutls: `vuln..fix` = 347 files vs `fix^..fix` = 2 files) — that would leak arm
    identity via hundreds of unrelated changes. `fix^..fix` makes the two arms differ by EXACTLY the fix.
  - **⚠ RISK found while building (NOT yet resolved):** ARVO's `fix_commit` is sometimes the TAIL of a
    multi-commit fix series. Concrete: harfbuzz 42525070 — `vuln..fix` distance = 3, series is
    `73f2f93 "Fix another leak"` → `3dd1de4 "Fix a few return_trace's"` (=`fix^`, our base) →
    `af3fdf1 "Simplify error handling"` (=`fix_commit`). So `fix^` may ALREADY be safe (real fix in an
    earlier commit) → the poc might not crash at our crash base, and `fix^..fix` may capture a non-security
    refactor. ARVO only *verifies* crash at `vuln_commit` (crashes) and clean at `fix_commit`; intermediate
    commits are unverified. **Next: measure `vuln..fix` commit-distance distribution across all 50 (after
    the corpus build finishes, to avoid network contention). If many bugs have distance >1, we likely need
    the crash-at-`fix^` verification gate (the option the user declined) or to restrict the set to
    distance==1 bugs. NB PROGRESS.md §"Harvesting mechanic" line ~269 claims `vuln_commit` == parent-of-fix;
    the harfbuzz evidence shows that is NOT reliably true — treat that claim as suspect.**
  - PROGRESS note this corrects: line ~269 ("`vuln_commit` = parent-of-fix commit") is empirically false
    for the sampled bugs; ARVO's `vuln_commit` sits 1..N commits before `fix_commit`.
  4. Re-run the experiment with Chenxi's own code (his `KernelAgent`/`v3_discrim_run.py` harness) →
     4a get his agent; 4b adapt to the ARVO two-arm dataset (or adopt his dataset); 4c run + compare.
- **Dependency order:** normalized images (2/3) gate item 1's real run; item 4 comes last. The doc (2a) is
  now unblocked; 2b (patch corpus) is the next concrete build step and needs no external infra.
- **FIRST STEP — ✅ DONE 2026-08-02. ARVO two-arm dataset is buildable, zero new code.** Verified
  `fetch_source.materialize_source()` checks out BOTH arms cleanly on 3 distinct projects (imagemagick
  42470067, harfbuzz 42470093, wireshark 42470183): each arm shallow-fetched in 1–13s, HEAD matched the
  requested commit, full source tree present, arms differ. Just point the existing fetcher at `fix_commit`
  instead of `vuln_commit` — no code change. Confirmed the fix arm carries the REAL fix (harfbuzz
  `src/hb-dsalgs.hh`: template ctor restricted to ptr/ref to stop accidental copies) — a genuine
  crash-vs-no-crash code delta. **Also validated the need for normalization live:** that fix arm ships a
  giveaway code comment ("...causing unwanted copies and bugs that come with it") that flags it as the
  patched version — exactly the leak item 2/3's strip-patch-comments step must remove. So 1a's biggest
  unknown (is the fix arm real/buildable?) is CLEARED.
- **OPEN DECISION to settle with Chenxi:** for the 50 bugs, use ARVO bugs (build fix-arm from our
  `fix_commit`) vs. adopt his kernel two-arm dataset — decides whether item 1 is "extend our harvest" or
  "consume his dataset."

---

## LIVE STATUS (as of 2026-07-31) — PHASE 5 COMPLETE. split.json built. Next = Phase 6 (verl GRPO training)

- **sweep3fp (Phase 5g pass@k) — ✅ DONE 2026-07-31** — `runs/sweep3fp/manifest.jsonl`, **800/800 samples,
  100/100 bugs × k=8**, `gemini/gemini-3-flash-preview` on a paid Gemini Developer key at concurrency 4.
  **$163.02 total, 0 errored.** Fresh `--name sweep3fp` → zero mixing with the 3.5 (sweep1) data.
  - Launch cmd used: `.venv/bin/python batch_predict.py --model gemini/gemini-3-flash-preview --k 8
    --limit 100 --shuffle --seed 0 --cleanup-src --max-cost 250 --concurrency 4 --name sweep3fp`
  - Sample-level resumable if ever re-run (relaunch SAME `--name`; done samples skipped).

- **FINAL REPORT (all 800 samples, scored via `score.py` = exec-rl `[0,1]` formula, −1 = invalid):**
  - **Coverage/reliability:** 726/800 valid (91%); 74 no-prediction (9%), every one a `LimitsExceeded`
    (agent hit the 45-step limit before writing an answer → scored −1). 489 clean `Submitted`, 311
    `LimitsExceeded` (3-flash-preview is verbose, burns steps). 0 pipeline errors.
  - **Valid-reward distribution (0..1):** mean **0.235**, median 0.240, max **0.748**, std 0.191;
    10% valid-but-zero (right format, wrong location). Bulk in 0.1–0.5 → model usually gets file, often
    function, misses exact line; nothing >0.75 (multi-frame depth-decay ceiling).
  - **Per-bug (valid-only, apples-to-apples w/ 3.5):** pass@8>0 = **95/100**; per-bug valid mean **0.231**
    (range 0.00–0.748), valid-sample std **0.077**. vs 3.5 baseline: mean ~0.30, std ~0.11 — same scale/shape,
    3-flash-preview a bit weaker AND more deterministic (thinner per-bug spread).
  - **Trainability (GRPO signal = variance among VALID samples):**
    **54 REAL-gradient (54%)** / 25 invalid-only "variance" (25%, spread is only −1 timeouts) /
    13 saturated flat-positive (13%) / 8 dead ~0 (8%). Among REAL bugs valid-std mean 0.077 / max 0.246.
  - **VERDICT:** trainable, but plan on **~54 genuinely-learnable bugs (not 87)** and weaker than 3.5.

- **✅ PHASE 5 CLOSED (2026-07-31) — both pre-split fixes resolved, split.json written:**
  1. **[DONE] Temperature check — non-issue.** Traced the full chain: `run_prediction.py:195` sets
     `model_kwargs={"safety_settings": ...}` only; `LitellmModel._query` passes no temperature;
     `agents/default.py:147` calls `model.query(messages)` with no kwargs. So **no temperature is set at
     any layer → Gemini API default = 1.0 (high).** The low valid-sample std (0.077) is therefore
     INTRINSIC to 3-flash-preview on this constrained task, NOT a temp artifact — the model converges to
     nearly the same crash-site answer across rollouts even at temp 1.0. No cheap variance lever exists;
     raising temp >1.0 would just add invalids. So ~54 real-gradient bugs is the genuine ceiling; NO
     re-sweep justified.
  2. **[DONE] Banded on valid-sample variance, not raw std.** Rewrote `difficulty.py`: `summarize()` now
     computes `std_valid`/`mean_valid`/`max_valid` over the non-(-1) samples and `band_and_split` keeps
     `std_valid > --min-std`. This drops the 33 timeout-inflated false keeps (raw-std banding kept 87;
     valid-std banding keeps 54 at min-std 0.02). saturated/dead now classified by `max_valid`.
  - **Split built:** `python difficulty.py runs/sweep3fp/manifest.jsonl --min-samples 8 --min-std 0.02`
    → `runs/sweep3fp/{difficulty.json, split.json}`. **54 kept (std_valid>0.02) → 43 train / 11 test**
    (test_frac 0.2, seed 0), 46 dropped (41 saturated / 5 dead). Kept valid-reward: mean 0.269, std mean
    0.077 / max 0.246. Kept set spans **30 projects** (ffmpeg 7, skia 7, imagemagick 4, ... — well diversified,
    no single project dominates). Alt threshold `--min-std 0.0` keeps 75 (60/15) if more (near-saturated)
    bugs are wanted later; 0.02 chosen as the clean real-gradient set.
  - **This split.json is the Phase-6 feeder.** `phase6/build_dataset.py --split runs/sweep3fp/split.json`
    consumes it. Comparable in absolute count to the 3.5 baseline's 46/51 keep, so a healthy train set.

- **Why preview + c=4 (decided 2026-07-30):** the serial `vertex_ai/gemini-3.5-flash` sweep (sweep1)
  was slow (serial + ~43 calls/rollout + Vertex RPM ceiling). Migrated to a paid Gemini Developer key
  for concurrency. Smoke tests then showed the paid key **429s (RESOURCE_EXHAUSTED) at just c=2 on
  3.5-flash** — the throttle is **tokens-per-minute**, not RPM (a tiny-request burst probe passed 24/24
  on every flash model; the big source-reading prompts blow the TPM cap; over-ceiling concurrency gives
  NO throughput gain — extra workers just pile into bounded backoff — and risks degraded data if retries
  exhaust). Model comparison at c=2 (2 real rollouts each):
    - `gemini-3.5-flash` → hit 429 · `gemini-3.6-flash` → clean but ~1.1M tok/rollout, ~$0.62 ·
      `gemini-3-flash-preview` → clean, ~40% fewer tokens, ~$0.18 · `gemini-3-pro-preview` → 404 (not enabled).
  First relaunched on **3.6-flash @ c=6** → **drowned (60 × 429 in 2 min, 0 completions)** — TPM ceiling
  sits between c=2 and c=6 for 3.6. User then chose **3-flash-preview**: its lower token/rollout sustains
  higher usable concurrency (c=4 completes with only mild throttling), cheaper too — accepting the
  preview-model stability risk for speed/cost. **No data mixing** — all 100 bugs reswept fresh on this
  one model. NOTE: Gemini stays SWEEP/EVAL only — the Phase-6 RL policy is open-weights (see
  [[arvo-phase6-training-model]]).
  Done along the way: **[DONE] killed auto-resume watcher AND deleted `resume_sweep1.sh`** (dead Vertex
  re-launcher — removed 2026-07-30 so it can't resurrect the old 3.5 sweep); **[DONE] killed the idle
  `sweep1` tmux session**; **[DONE] added `--concurrency N` to `batch_predict.py`** (ThreadPoolExecutor
  over BUGS not samples — a worker owns a whole bug's dirs `data/<id>/{src,answer}`; `BatchState` lock
  guards manifest append + cost/count; `--max-cost` soft ceiling); **[DONE] stopped sweep1** (pid 368826)
  + cleaned orphaned containers.

- **Consistency audit (2026-07-30):** full anti-cheating + correctness pass — sandbox lockdown, answer
  validation, prompt leak guard, single scoring path (all intact); concurrency + resume logic correct;
  agent-loop name aligned; `py_compile` + prompt-parity green. Fixed stale refs: `batch_predict.py`
  `--model` default → `gemini/gemini-3-flash-preview`; `README`/`build_dataset.py` docstrings → sweep3fp
  workflow; annotated the old sweep1 launch block below as SUPERSEDED.

- **sweep1 (3.5 baseline — KEEP, do NOT feed to Phase-6 split)** — `runs/sweep1/manifest.jsonl`,
  **412 samples / 52 distinct bugs (51 with all 8), 409 valid**, all `vertex_ai/gemini-3.5-flash`.
  Serial Vertex run only reached 52/100 bugs in ~19h before we stopped it. Preserved as a 3.5-flash
  capability baseline; NOT mixed into the gemini-3-flash-preview difficulty split (sweep3fp).
  - **Band quality (dry-run on 34 scored 3.5 bugs) looked healthy:** 28 keep (std>0) / 5 saturated /
    1 dead; kept reward mean 0.313, std mean 0.102 / max 0.472 — on track for plenty of keepable bugs.
- **harvest (Phase 2)** — tmux session `harvest`. At ~[4568/6138] (~74%) as of 2026-08-02, pulling normally.
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
    - **⚠️ SUPERSEDED 2026-07-30 — this whole sub-block describes the ORIGINAL serial Vertex sweep1
      (3.5-flash), which we STOPPED at 52/100 bugs and REPLACED with the concurrent gemini-3-flash-preview
      resweep (`sweep3fp`). See LIVE STATUS at the top. Do NOT re-run the sweep1 command or `difficulty.py
      runs/sweep1/...` for the Phase-6 split — use `runs/sweep3fp/...`. Kept here as dated history only.**
    - **LAUNCHED 2026-07-29 10:37** in tmux session `sweep1` (PID was 368826):
      `python batch_predict.py --k 8 --limit 100 --shuffle --seed 0 --cleanup-src --max-cost 300 --name sweep1 2>&1 | tee runs/sweep1.log`
      Real per-run wall time ~178s (429 backoff, not the 128s shakeout est), so 800 runs ≈ **~40h**, ~$356.
      Resumable (sample-level): if it dies, re-run the SAME command and it continues.
    - **AUTO-RESUME ARMED 2026-07-29 (hands-off, user decision — want all 100, cost not a concern):**
      the `--max-cost 300` on the launched run trips at ~84 bugs; detached watcher `resume_sweep1.sh`
      relaunches the same command (fresh $300 cap, resets `total_cost`, covers the remaining ~$56) into
      the `sweep1` pane so all 100 bugs finish unattended. Don't type in the `sweep1` pane while armed.
    - **Mid-run band health (34 bugs, `difficulty.py --dry-run`):** 28 keep (std>0) / 5 saturated / 1
      dead; kept reward mean 0.313 (≈ half the ~0.67 multi-frame ceiling — competent, not saturated),
      std mean 0.102 / max 0.472. Good gradient. Projects to ~80 keepable bugs at n=100.
    - **WHEN THE SWEEP FINISHES → this is the last step of Phase 5:**
      `python difficulty.py runs/sweep1/manifest.jsonl` → writes `runs/sweep1/difficulty.json` +
      `runs/sweep1/split.json` (the banded train/test split Phase 6 consumes). Then eyeball the band
      size: if <~30 bugs have std>0, widen the sweep (more bugs or higher k) before Phase 6. Peek
      anytime mid-run with `--dry-run` (read-only, no files written).
    - Bridges into Phase 6 (produces its train set); lives in Phase 5 because it's reward-driven
      measurement.
- [~] **Phase 6 — RL training** (verl GRPO trainer; the heavy GPU/infra part). **All Phase-6 code lives
  in a dedicated `phase6/` folder** (user decision — keep the training port self-contained). Broken into
  small substeps mirroring the exec-rl training-side files. **No GPU on this box and verl isn't
  installed**, so the loop + training can be *written* now but only *run* on a GPU box later (6g).
  Port surface (exec-rl → ARVO): `data.py`→6a, `rl/rewards.py`→6b, agent prompt yaml→6c, agent-loop
  config→6d, `rl/agent.py` `kAgentLoop`→6e, `run_qwen3_5_9b_crash_grpo.sh`→6f.
  - [x] **6a. Dataset builder → verl parquet — DONE 2026-07-30.** `phase6/build_dataset.py` (analog of
    exec-rl `data.py`). Reads `runs/sweep1/split.json` (5g's std>0 train/test bug lists) + each
    `data/<id>/` (poc, ground_truth, meta) → `train.parquet`/`val.parquet` + `manifest.json` in verl's
    row shape: `prompt` (frozen [system,user], exposes ONLY project+sanitizer), `reward_model.ground_truth`
    (cleaned frames), `extra_info.reproducer_b64` (PoC bytes b64 — ARVO PoCs are binary, not text),
    `extra_info.environment` (repo_addr/vuln_commit/mounts/base image — our per-bug substitute for
    exec-rl's prebuilt kernel image, since 6e builds the locked sandbox at rollout time), `agent_name=
    "arvo_crash"`. The prompt is NOT inlined here — it's rendered from the shared 6c yaml
    (`phase6/prompts/crash-predictor.yaml`) with jinja2 `StrictUndefined` directly (no mini-swe-agent
    import, so dataset gen stays dep-free — exec-rl's choice), and a copy of the yaml is written into
    `<dataset>/prompts/` so each dataset is frozen with the exact prompt it was built from. Enforces two
    invariants at the source: (1) `_assert_no_leak` fails the build if a scored frame (function/file) or
    fix_commit appears in the **per-bug interpolated values** (project+sanitizer) — scoped to the injected
    vars, NOT the whole rendered prompt, because the rest is FIXED template prose identical across every
    bug, so a common-word crash function like `main`/`execute` matching "remain"/"not to execute" is a
    coincidence, not a leak (the old whole-prompt substring check dropped ~40% of bugs on exactly this);
    crash *type* is deliberately unguarded (prompt must list the type vocabulary; reward.py doesn't score
    type); (2) same prompt+gt shape feeds train and eval so they can't diverge. Verified on all 49 real
    swept bugs (47 train / 2 val, ZERO skipped after the leak-guard fix): parquet round-trips, PoC decodes
    to meta's exact byte count, env block complete, yaml copied into the dataset, stored gt scores 0.50
    perfect-top / 0.0 wrong-file / -1.0 no-answer through the real reward. **Testable now against swept
    bugs; the final real build waits on `split.json` (sweep + `difficulty.py`).**
  - [x] **6b. Reward-shim parity — DONE 2026-07-30.** verl points `custom_reward_function.path` at
    `phase6/rewards.py` (named plural to NOT shadow repo-root `reward.py` on sys.path — the collision
    that first broke `score.py`'s `from reward import`; same trick as exec-rl's `rl/rewards.py`). It's a
    thin re-export of the SAME `score.compute_score` Phase-5 eval uses, so train/eval can't diverge (5d).
    Two hardenings in `score.py`: (1) `ground_truth_frames` now does explicit None/len checks and returns
    a plain `list[dict]` — fixes the **6a finding** (parquet round-trip deserializes frames as a numpy
    array; the old `... or []` AND `reward.py`'s `if not frames` both threw numpy's ambiguous-truth
    error); (2) `compute_score` reads the prediction from `extra_info["prediction"]` (our agent loop's
    validated dict) falling back to `extra_info["context_output"]` (exec-rl's raw-text convention).
    Verified: numpy-array gt straight from parquet scores 0.50 perfect-top / 0.0 wrong / -1.0 missing;
    context_output path scores good→0.50, garbage→-1.0; returns `{score,acc,reward_name}`; Phase-5
    on-disk `score_result` unchanged (regression held).
  - [x] **6c. Agent prompt yaml — DONE 2026-07-30.** `phase6/prompts/crash-predictor.yaml` is the SINGLE
    prompt source: it seeds the training rows (6a renders it) AND is what the eval agent feeds the model
    (`msagent_runner/run_prediction.py` via mini-swe-agent) — exec-rl's signature "one frozen prompt, no
    train/eval drift" property. GENERATED from run_prediction.py's live `SYSTEM_TEMPLATE`/`INSTANCE_TEMPLATE`
    (not hand-copied), preserving the `{{project}}`/`{{sanitizer}}` jinja placeholders and the hardened
    first-write / step-budget / finish instructions; `environment:` carries step_limit + format-error cap.
    Parity is structural: mini-swe-agent's `render_template` IS `Template(t, StrictUndefined).render(**v)`,
    identical to 6a's `render_prompt`, so parity reduces to "yaml templates == run_prediction templates".
    `phase6/test_prompt_parity.py` PINS exactly that: it renders both sides across 5 project/sanitizer
    cases (incl. quotes/punctuation) and asserts byte-equality — a regression guard that fails loudly if
    run_prediction.py's templates are edited without regenerating the yaml. Deliberately did NOT edit
    run_prediction.py (the live sweep imports it; auto-resume is armed) — the yaml was generated FROM it.
  - [x] **6d. verl agent-loop config — DONE 2026-07-30.** `phase6/config/verl_arvo_agent_loop.yaml`
    (analog of exec-rl `verl_crash_agent_loop.yaml`). A one-entry `{name, _target_}` list verl loads via
    `actor_rollout_ref.rollout.agent.agent_loop_config_path` (set in 6f). `name: arvo_crash` MUST equal
    the `agent_name` 6a stamps on every parquet row (asserted against `build_dataset.AGENT_LOOP_NAME`), or
    verl can't match a loop to our rows. `_target_: phase6.agent_loop.ArvoCrashAgentLoop` is the Hydra
    import path to the 6e class — resolves as a namespace package off repo-root PYTHONPATH (no `__init__.py`
    needed). This file is the CONTRACT 6e must satisfy: it names the class 6e has to provide.
  - [x] **6e. verl agent loop — DONE 2026-07-30 (write-only; cannot run here).** `phase6/agent_loop.py`,
    analog of exec-rl `kAgentLoop`. `ArvoCrashAgentLoop` subclasses exec-rl's generic `MiniSweAgentLoop`
    (the tested verl<->mini-swe-agent adapter — VerlServerModel gen + token bookkeeping + AgentLoopOutput
    all stay upstream) and overrides ONLY the two ARVO-delta methods, exactly the shape of kAgentLoop:
    (1) `_make_env` builds the locked sandbox PER ROLLOUT from the row's `extra_info.environment` (6a) —
    `materialize_source(repo_addr, vuln_commit)` into a per-rollout host scratch dir, decode the base64
    PoC to bytes, mount src+poc read-only / answer dir writable / empty-dir mask over `.git`, run under
    `sandbox_contract.lockdown_flags()` on `arvo-sandbox:base`. Same mount set + lockdown as eval's
    `run_prediction.build_mounts`, so a trajectory sees the identical box in train and eval. Builds the
    `DockerEnvironment` directly via `get_environment` (NOT `super()._make_env`, which would add exec-rl's
    `/skills` mount we don't use). Scratch dir is wiped by wrapping `env.cleanup`. (2) `compute_reward`
    reads `ANSWER_FILE` back out of the live container and stashes the raw JSON text on
    `extra_info["context_output"]`; returns None so 6b's `custom_reward_function` scores it (missing file
    -> "" -> -1, the correct no-answer score). Registered `arvo_crash` (== 6a `AGENT_LOOP_NAME` == 6d
    yaml `name`). **Also fixed a 6c yaml bug found here:** `step_limit`/`max_consecutive_format_errors`
    were under `environment:` but the verl loop reads them from `agent:` (and merges `environment:` into
    the Docker config, which rejects unknown keys) — moved under `agent:`, `environment:` block dropped
    (the loop builds Docker config per-bug). Verified: 6e py-compiles; parity still byte-exact; a real
    swept row carries repo_addr/vuln_commit/image + PoC that decodes to the exact disk bytes and mount
    targets that match `sandbox_contract`. **Runs only on the 6g host (needs verl + exec_rl + vLLM + GPU).**
  - [ ] **6f. GRPO launch script** — port `run_qwen3_5_9b_crash_grpo.sh` with ARVO paths (our dataset,
    `phase6/rewards.py` reward, `phase6/config/verl_arvo_agent_loop.yaml`, `MSWEA_VERL_CONFIG_PATH` ->
    the 6c yaml). **Model is a `MODEL` arg/env var, NOT hardcoded** (user decision 2026-07-30): the
    trained policy MUST be open-weights — Gemini 3.5 Flash is the Phase-5 sweep + Phase-7 eval model and
    CANNOT be RL-trained (closed API: no weights/vLLM/logprobs). Default to Qwen3 (exec-rl's proven config,
    least porting), concrete model picked at 6g/6h once the GPU box is known. See
    [[arvo-phase6-training-model]].
  - [ ] **6g. Infra bring-up (not code)** — GPU box + verl + vLLM + vendored mini-swe-agent + base model +
    `docker build arvo-sandbox:base` + Ray. Hard blocker for actually training.
  - [ ] **6h. Smoke train + first curve** — 1-2 steps, val_before_train reproduces 5e's ~0.240 baseline,
    confirm non-zero advantage on std>0 bugs, then the real run. Needs 6g.
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
