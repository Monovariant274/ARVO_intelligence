# ARVO_intelligence

Replicating `system-intelligence`'s **exec-rl** crash-prediction RL on user-space
**ARVO / OSS-Fuzz** bugs (instead of Linux-kernel Syzbot bugs).

An agent is given a vulnerable source tree + a crash-triggering input in a **locked
sandbox** (no build/run — that prevents reward-hacking by just reproducing the crash)
and must predict *where* it crashes (file / function / line) and *what type*. The
prediction is scored against hidden ground truth; the reward trains the agent.

See `PROGRESS.md` for full status and resume notes.

## Pipeline (by stage)

### Phase 2 — Harvest ground truth
- `harvest.py` — pull `n132/arvo:<id>-vul` Docker images, extract PoC + source, write `data/<id>/`.
- `crash_parser.py` — parse an ARVO crash report into ordered stack frames (the ground truth we hide).
- `fetch_source.py` — materialize a bug's source at its commit (git clone) and clean it up.
- `select_bugs.py` — list the runnable bugs in `data/` (filter by sanitizer / project).

### Phase 3 — Locked sandbox + answer format
- `sandbox_contract.py` — the read-only mount layout and the answer-file path.
- `launch_sandbox.py` — build/run the network-less, read-only Docker sandbox.
- `verify_sandbox.py` — assert the lockdown (no build, no network, no writes outside the answer file).
- `answer_schema.py` — the agent's required answer format (`crash_type_coarse`/`filename`/`function`/`line`) + validation.
- `msagent_runner/run_prediction.py` — the single-bug agent loop (prompt → sandbox → validate).

### Phase 4 — Batch prediction
- `batch_predict.py` — run the agent over many bugs; `--k N` for pass@k sampling; resumable manifest at `runs/<name>/`.

### Phase 5 — Reward + difficulty
- `reward.py` — depth-decayed crash-site reward (file via LCS, function gated on file, line via `exp(-|Δ|/4)`).
- `frame_clean.py` — canonicalize ground-truth frames (prune sanitizer frames, repo-relative paths) before scoring.
- `score.py` — the single scoring entrypoint train & eval share; invalid = −1, wrong = 0, right ∈ (0, 1].
- `difficulty.py` — score a pass@k manifest, band by reward variance (std > 0), emit a bug-level train/test split.

### Phases 6–7 — planned
- RL training (verl) on the banded train set, then eval on the held-out test bugs (pre/post comparison).

## Quickstart

```bash
cd ~/ARVO_intelligence && source .venv/bin/activate && export GEMINI_API_KEY=...   # paid Gemini Developer API key

python3 harvest.py pull --require-frames --out ./data      # Phase 2
python3 frame_clean.py                                      # Phase 5b: clean frames
python  batch_predict.py --model gemini/gemini-3-flash-preview --k 8 --limit 100 \
        --shuffle --seed 0 --cleanup-src --concurrency 4 --max-cost 250 --name sweep3fp   # Phase 4+5g
python  difficulty.py runs/sweep3fp/manifest.jsonl         # Phase 5g: band + split
```

Data lives under `data/` and run outputs under `runs/` (both git-ignored).
