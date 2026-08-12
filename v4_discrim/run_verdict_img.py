#!/usr/bin/env python3
"""One (bug, arm, sample) rollout of the v4 discrimination task INSIDE the leak-normalized
per-bug image (item 3).

Item 1 (run_verdict.py) runs the agent in the generic arvo-sandbox:base image with raw fetched
source mounted read-only and .git masked -- an UPPER-BAND baseline that still leaks (fix-arm
patch comments, mtimes, etc., see run_verdict.py NOTE ON LEAKS). Item 3 swaps the environment for
the normalized image built by build_shard_arvo.py:

  crash arm -> sysintel-user-arvo-<bug>-vul:latest   (fix^ tree, bug present, unpatched)
  fix   arm -> sysintel-user-arvo-<bug>-fix:latest   (SAME fix^ tree + commentless oracle patch)

Both arms derive from the SAME fix^ tree and every metadata leak channel (README 2) is normalized
identically, so the ONLY signal separating the arms is the code itself. Concretely this harness:

  * uses the per-bug arm IMAGE as the container base (source baked at /src/<project>) instead of
    arvo-sandbox:base + a source bind-mount,
  * mounts ONLY poc (ro) + answer (rw) -- no source mount, and no .git mask (the image's .git is
    a single synthetic commit at EPOCH, arm-neutral by construction),
  * sets cwd to the tree path /src/<project> (the image WORKDIR),
  * keeps EVERYTHING else identical to item 1: same arm-neutral prompt, same verdict schema, same
    score_verdict path -- so item-1-vs-item-3 discriminability is a clean raw-vs-normalized delta.

  python3 run_verdict_img.py data/42481822 crash --model gemini/gemini-3-flash-preview
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "vendor"))
sys.path.insert(0, str(_HERE / "lib"))

from sandbox_contract import ANSWER_DIR, ANSWER_FILE, POC_MOUNT, lockdown_flags
from verdict_schema import InvalidVerdict, prompt_instructions, validate_verdict
from gt_adapter import load_adapted
from score_verdict import score_verdict

from run_prediction import (
    SAFETY_SETTINGS,
    OBSERVATION_TEMPLATE,
    SafeLitellmModel,
    cache_stats,
)
from run_verdict import SYSTEM_TEMPLATE

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.docker import DockerEnvironment
from minisweagent.environments.docker import DockerMount

# Registry + naming must match build_shard_arvo.py exactly.
_DST_REG = "us-docker.pkg.dev/triangulate-396717/sysintel-env"
_ARM_IMG = {"crash": "vul", "fix": "fix"}

# Arm-neutral instance prompt (identical intent to run_verdict.INSTANCE_TEMPLATE) but the source
# lives at the image's baked tree path, not the item-1 bind-mount. TREE is filled in per bug.
INSTANCE_TEMPLATE = f"""An input was run against the "{{{{project}}}}" project under the \
{{{{sanitizer}}}} sanitizer. You are given:

- A source tree at {{{{tree}}}} (your current working directory) -- ONE specific revision of the \
project.
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


def _image_local(name: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", name],
                          capture_output=True).returncode == 0


def image_name(bug: str, arm: str) -> str:
    return f"{_DST_REG}/sysintel-user-arvo-{bug}-{_ARM_IMG[arm]}:latest"


def run(bug_dir: Path, arm: str, *, model_name: str, step_limit: int = 30,
        cost_limit: float = 1.0, sample: int = 0, traj_dir: Path | None = None) -> dict:
    if arm not in _ARM_IMG:
        raise ValueError(f"unknown arm {arm!r} (expected 'crash' or 'fix')")
    meta = json.loads((bug_dir / "meta.json").read_text())
    bug = bug_dir.name
    project = meta["project"]
    commit = meta.get("vuln_commit" if arm == "crash" else "fix_commit")
    tree = f"/src/{project}"
    img = image_name(bug, arm)

    poc_file = bug_dir / "poc"
    if not poc_file.exists():
        raise SystemExit(f"{bug_dir}: no poc file")

    if not _image_local(img):  # --keep leaves them local; pull on demand if this box lacks it
        if subprocess.run(["docker", "pull", img], timeout=1800).returncode != 0:
            raise SystemExit(f"{bug}: image {img} not local and pull failed")

    gold = load_adapted(bug_dir)  # None => no usable gold frames; crash-arm scoring impossible
    answer_dir = bug_dir / f"answer_img_{arm}"
    answer_dir.mkdir(parents=True, exist_ok=True)
    verdict_path = answer_dir / "prediction.json"
    verdict_path.unlink(missing_ok=True)  # never read a stale verdict from a prior run

    # Per-rollout trajectory path, unique across (bug, arm, sample) so no sample clobbers another.
    # With a run-level traj_dir (batch driver) every rollout lands under runs/<name>/trajectories/.
    if traj_dir is not None:
        traj_dir.mkdir(parents=True, exist_ok=True)
        traj_path = traj_dir / f"{bug}_{arm}_s{sample}.json"
    else:
        traj_path = answer_dir / f"trajectory_s{sample}.json"

    print(f"{bug} [{arm} s{sample}] ({project}): image {img.split('/')[-1]} tree={tree}")

    env = DockerEnvironment(
        image=img,
        cwd=tree,
        run_args=["--rm", *lockdown_flags()],
        mounts=[  # source is baked into the image; only poc(ro) + answer(rw) are bound
            DockerMount(source=str(poc_file.resolve()), target=POC_MOUNT, readonly=True),
            DockerMount(source=str(answer_dir.resolve()), target=ANSWER_DIR, readonly=False),
        ],
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
        agent_result = agent.run(project=project, sanitizer=meta["sanitizer"], tree=tree)
        exit_status = agent_result.get("exit_status")
        print(f"exit_status={exit_status!r} n_calls={agent.n_calls} cost=${agent.cost:.4f}")
    except Exception as e:  # a single rollout crashing must not lose the partial verdict on disk
        exit_status = f"crashed: {type(e).__name__}: {e}"
        print(f"run crashed after {agent.n_calls} calls (cost=${agent.cost:.4f}): {type(e).__name__}: {e}")
    cache = cache_stats(agent.messages)

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
        "bug_id": bug,
        "project": project,
        "sanitizer": meta["sanitizer"],
        "arm": arm,
        "sample": sample,
        "expected_crashes": (arm == "crash"),
        "model": model_name,
        "commit": commit,
        "image": img,
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
    ap.add_argument("bug_dir", type=Path, help="harvested bug folder, e.g. data/42481822")
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
