#!/usr/bin/env python3
"""One (bug, arm, sample) rollout of the v4 crash/no-crash DISCRIMINATION task (item 1b).

Adapts msagent_runner/run_prediction.py (single-site "where does it crash") to the two-arm
verdict task ("does it crash at all, and if so where"). The differences that matter:

  * ARM -> COMMIT. crash arm checks out meta["vuln_commit"]; fix arm checks out
    meta["fix_commit"]. Separate per-arm src dirs (src_crash / src_fix) so both arms can
    coexist and materialize_source's commit cache stays valid when alternating arms.
  * ARM-NEUTRAL PROMPT. run_prediction tells the agent "a fuzzer found a crash" -- a dead
    giveaway that would make the fix arm trivially wrong. Here the agent is told only that an
    input was run against the project under a sanitizer and must DECIDE crash-or-not by reading
    the code. Nothing in the prompt reveals the arm.
  * VERDICT OUTPUT + SCORING. the answer file holds a verdict object (verdict_schema), scored
    through the shared score_verdict path against reward_v4.

NOTE ON LEAKS: this runs inside the EXISTING fetch-source sandbox (.git masked, source mounted
read-only), NOT the leak-normalized images (items 2/3). So residual channels remain -- most
importantly fix-commit source can carry giveaway patch COMMENTS (see the harfbuzz case in
PROGRESS.md). Item 1's discriminability is therefore an UPPER-BAND baseline; items 2/3 tighten
it. This is intentional: measure first, normalize second.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "vendor"))
sys.path.insert(0, str(_HERE / "lib"))

from fetch_source import materialize_source
from sandbox_contract import ANSWER_DIR, ANSWER_FILE, BASE_IMAGE, POC_MOUNT, SRC_MOUNT, lockdown_flags
from verdict_schema import InvalidVerdict, prompt_instructions, validate_verdict
from gt_adapter import load_adapted
from score_verdict import score_verdict

# Reuse the model wrapper / templates / helpers already hardened in run_prediction.py.
from run_prediction import (
    SAFETY_SETTINGS,
    OBSERVATION_TEMPLATE,
    SafeLitellmModel,
    build_mounts,
    cache_stats,
)

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.docker import DockerEnvironment

SYSTEM_TEMPLATE = """You are a security researcher who audits C/C++ source for memory-safety \
bugs. You interact with a sandboxed Linux shell by calling the bash tool. EVERY response you \
send MUST make at least one bash tool call -- never reply with plain prose or a text-only \
"final answer". Even when you believe the task is complete, you finish by calling the bash \
tool (see the finish instruction in the task), not by writing your conclusion as text. A \
response with no bash tool call is rejected."""

# Arm-neutral: the agent must not be able to tell whether this tree crashes on the input.
INSTANCE_TEMPLATE = f"""An input was run against the "{{{{project}}}}" project under the \
{{{{sanitizer}}}} sanitizer. You are given:

- A source tree at {SRC_MOUNT} (read-only) -- ONE specific revision of the project.
- The exact input that was fed to the program at {POC_MOUNT} (read-only).

There is no compiler, no build system, and no network access in this environment -- you \
cannot build the project or run the input. Your job is to determine, by reading the source \
and reasoning about how the input in {POC_MOUNT} flows through THIS revision of the code, \
whether the input triggers a memory-safety crash (buffer overflow, use-after-free, \
uninitialized read, etc.) in this tree -- or whether this revision handles the input safely. \
Different revisions of the same project differ: a bug may be present in this tree, or it may \
have been fixed here. Do not assume a crash exists; decide it from the code. You may run \
plain python3 for lightweight heuristics (scanning for risky calls, measuring buffer sizes) \
but not to execute or simulate the target program itself.

{prompt_instructions()}

You have a limited step budget and cannot exceed it. Do NOT spend the whole budget exploring. \
Your VERY FIRST tool call must write an initial verdict to {ANSWER_FILE}, before any other \
exploration: a concrete crashes true/false, a bugClass ("none" if false), a one-line reason, \
and -- if crashes is true -- at least one crashFrame naming a REAL file and function that \
exist in the tree with a plausible NONZERO line. Then keep refining and overwriting that same \
file as you learn more. This guarantees a usable verdict exists even if you run out of steps. \
Each observation tells you how many steps you have used -- if only a few remain, write your \
current best verdict immediately.

When your verdict is written to {ANSWER_FILE}, finish by making ONE final bash tool call whose \
command is exactly:
echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
Issue that as a bash tool call (not as plain text), on its own, with no other command combined \
with it. Do NOT summarize your findings in prose instead of calling the tool -- a response \
without a bash tool call is rejected and wastes a step."""

_ARM_COMMIT = {"crash": "vuln_commit", "fix": "fix_commit"}


def run(bug_dir: Path, arm: str, *, model_name: str, step_limit: int = 30,
        cost_limit: float = 1.0, sample: int = 0, traj_dir: Path | None = None) -> dict:
    if arm not in _ARM_COMMIT:
        raise ValueError(f"unknown arm {arm!r} (expected 'crash' or 'fix')")
    meta = json.loads((bug_dir / "meta.json").read_text())
    commit = meta.get(_ARM_COMMIT[arm])
    if not commit:
        raise SystemExit(f"{bug_dir}: meta.json has no {_ARM_COMMIT[arm]}")
    poc_file = bug_dir / "poc"
    if not poc_file.exists():
        raise SystemExit(f"{bug_dir}: no poc file")

    gold = load_adapted(bug_dir)  # None => no usable gold frames; crash-arm scoring impossible
    src_dir = bug_dir / f"src_{arm}"
    answer_dir = bug_dir / f"answer_{arm}"
    answer_dir.mkdir(parents=True, exist_ok=True)
    verdict_path = answer_dir / "prediction.json"
    verdict_path.unlink(missing_ok=True)  # never read a stale verdict from a prior run

    # Per-rollout trajectory path, unique across (bug, arm, sample) so no sample clobbers another.
    # With a run-level traj_dir (batch driver) every rollout lands under runs/<name>/trajectories/.
    if traj_dir is not None:
        traj_dir.mkdir(parents=True, exist_ok=True)
        traj_path = traj_dir / f"{bug_dir.name}_{arm}_s{sample}.json"
    else:
        traj_path = answer_dir / f"trajectory_s{sample}.json"

    method = materialize_source(meta["repo_addr"], commit, src_dir)
    print(f"{bug_dir.name} [{arm} s{sample}] ({meta['project']}): source {method} @ {commit[:10]} -> {src_dir}")

    env = DockerEnvironment(
        image=BASE_IMAGE,
        cwd=SRC_MOUNT,
        run_args=["--rm", *lockdown_flags()],
        mounts=build_mounts(src_dir, poc_file, answer_dir),
        container_timeout="30m",
    )
    agent = DefaultAgent(
        SafeLitellmModel(
            model_name=model_name,
            observation_template=OBSERVATION_TEMPLATE,
            model_kwargs={"safety_settings": SAFETY_SETTINGS},
        ),
        env,
        system_template=SYSTEM_TEMPLATE,
        instance_template=INSTANCE_TEMPLATE,
        step_limit=step_limit,
        cost_limit=cost_limit,
        max_consecutive_format_errors=8,
        output_path=traj_path,
    )
    started_at = time.time()
    try:
        agent_result = agent.run(project=meta["project"], sanitizer=meta["sanitizer"])
        exit_status = agent_result.get("exit_status")
        print(f"exit_status={exit_status!r} n_calls={agent.n_calls} cost=${agent.cost:.4f}")
    except Exception as e:  # a single rollout crashing must not lose the partial verdict on disk
        exit_status = f"crashed: {type(e).__name__}: {e}"
        print(f"run crashed after {agent.n_calls} calls (cost=${agent.cost:.4f}): {type(e).__name__}: {e}")
    cache = cache_stats(agent.messages)

    # Read back the raw verdict text (kept verbatim for re-scoring), gate it, then score.
    verdict_raw = verdict_path.read_text() if verdict_path.exists() else None
    invalid_reason = None
    try:
        validate_verdict(verdict_path)
    except InvalidVerdict as e:
        invalid_reason = str(e)
        print(f"off-schema verdict: {invalid_reason}")

    scored = score_verdict(gold if gold is not None else {"bugClass": "other", "frames": [
        {"depth": 0, "function": "?", "filename": "?", "line": 1, "rawLine": "?", "reason": ""}]},
        verdict_raw, arm=arm)

    result = {
        "bug_id": bug_dir.name,
        "project": meta["project"],
        "sanitizer": meta["sanitizer"],
        "arm": arm,
        "sample": sample,
        "expected_crashes": (arm == "crash"),
        "model": model_name,
        "commit": commit,
        "step_limit": step_limit,
        "cost_limit": cost_limit,
        "exit_status": exit_status,
        "n_calls": agent.n_calls,
        "cost": round(agent.cost, 6),
        "cache": cache,
        "verdict_raw": verdict_raw,
        "crashes": scored["crashes"],
        "bug_class": scored["bugClass"],
        "reward": scored["reward"],
        "valid": scored["valid"],
        "components": scored["components"],
        "invalid_reason": invalid_reason,
        "gold_available": gold is not None,
        "traj_path": str(traj_path),
        "started_at": started_at,
        "duration_seconds": round(time.time() - started_at, 1),
    }
    print(f"  verdict: crashes={scored['crashes']} reward={scored['reward']} valid={scored['valid']}")
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bug_dir", type=Path, help="harvested bug folder, e.g. data/42470093")
    ap.add_argument("arm", choices=["crash", "fix"])
    ap.add_argument("--model", default="gemini/gemini-3-flash-preview")
    ap.add_argument("--step-limit", type=int, default=30)
    ap.add_argument("--cost-limit", type=float, default=1.0)
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--traj-dir", type=Path, default=None,
                    help="dir to save this rollout's trajectory (default: alongside the answer dir)")
    a = ap.parse_args()
    res = run(a.bug_dir, a.arm, model_name=a.model, step_limit=a.step_limit,
              cost_limit=a.cost_limit, sample=a.sample, traj_dir=a.traj_dir)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
