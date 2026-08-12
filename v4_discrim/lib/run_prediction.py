#!/usr/bin/env python3
"""Runs one ARVO bug's crash-prediction task through mini-swe-agent, inside the
Phase-3 locked-down sandbox (sandbox_contract.py / Dockerfile.sandbox).

mini-swe-agent's DockerEnvironment always starts its own container from an
image (it won't attach to one we launch ourselves), so this script hands it
arvo-sandbox:base plus sandbox_contract.lockdown_flags() and the same mounts
launch_sandbox.py uses -- same security posture, different launcher. The LLM
call itself happens on the host (litellm -> Anthropic API); the container
only ever executes the agent's bash commands, and it has no network anyway.

Usage:
  ANTHROPIC_API_KEY=... .venv/bin/python run_prediction.py ../data_smoke/40096184
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from answer_schema import InvalidPrediction, prompt_instructions, validate_prediction
from fetch_source import materialize_source
from sandbox_contract import ANSWER_DIR, ANSWER_FILE, BASE_IMAGE, EMPTY_MASK_DIR, POC_MOUNT, SRC_MOUNT, lockdown_flags

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.docker import DockerEnvironment, DockerMount
from minisweagent.exceptions import FormatError
from minisweagent.models.litellm_model import LitellmModel

SYSTEM_TEMPLATE = """You are a security researcher analyzing C/C++ source code for memory-safety bugs.
You interact with a sandboxed Linux shell by calling the bash tool. EVERY response you send MUST make
at least one bash tool call -- never reply with plain prose or a text-only "final answer". Even when
you believe the task is complete, you finish by calling the bash tool (see the finish instruction in
the task), not by writing your conclusion as text. A response with no bash tool call is rejected."""

INSTANCE_TEMPLATE = f"""A fuzzer ({{{{sanitizer}}}}) found a crash in the "{{{{project}}}}" project. \
You have:

- The vulnerable source tree at {SRC_MOUNT} (read-only).
- The exact input that triggers the crash at {POC_MOUNT} (read-only).

There is no compiler, no build system, and no network access in this environment \
-- you cannot build the project or run the crashing input. Your job is to find the \
crash site by reading the source code and reasoning about how the input in {POC_MOUNT} \
would flow through it. You may run plain python3 for lightweight heuristics (e.g. \
scanning for risky calls, measuring buffer sizes) but not to execute or simulate the \
target program itself.

{prompt_instructions()}

You have a limited step budget and cannot exceed it. Do NOT spend the whole budget exploring.
Your VERY FIRST tool call must write an initial prediction to {ANSWER_FILE}, before you do any
other exploration. That initial prediction must already be concrete: name a REAL file and a REAL
function that actually exist in the source tree (open one or two files first in that same step if
needed) and a plausible NONZERO line number. Never write "unknown" for filename/function and never
write 0 (or a negative) for line -- such an answer is rejected and counts as no prediction at all.
Then keep refining and overwriting that same file as you learn more. This guarantees a usable
prediction exists even if you run out of steps. Each observation tells you how many steps you have
used -- if only a few remain, write your current best answer immediately.

When you have written your prediction to {ANSWER_FILE}, finish by making ONE final bash tool call
whose command is exactly:
echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
Issue that as a bash tool call (not as plain text), on its own, with no other command combined with
it. Do NOT summarize your findings in prose instead of calling the tool -- a response without a bash
tool call is rejected and wastes a step."""


# Gemini/Vertex will occasionally return zero candidates (empty `choices`) when its safety or
# recitation filters trip on the C/C++ source we feed it -- and mini-swe-agent then hits
# `response.choices[0]` and dies with IndexError, taking the whole run down. Relaxing every harm
# category to BLOCK_NONE removes the most common cause; the try/except around agent.run() below is
# the backstop for the residual cases (recitation blocks ignore safety_settings entirely).
SAFETY_SETTINGS = [
    {"category": c, "threshold": "BLOCK_NONE"}
    for c in (
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    )
]


class SafeLitellmModel(LitellmModel):
    """Vertex/Gemini sometimes returns zero candidates (empty `choices`) when a
    recitation/safety filter trips on the C/C++ source. Upstream LitellmModel then
    dies at `response.choices[0]` with IndexError and takes the whole run down
    (its retry() only wraps the API call, not the parse). We retry the call a few
    times -- empty responses are usually transient -- and if it persists, raise
    FormatError, which DefaultAgent appends as a nudge and continues (bounded by
    max_consecutive_format_errors) instead of crashing. Retries don't burn step
    budget: the agent increments n_calls per step, not per underlying API call."""

    empty_response_retries = 3

    def query(self, messages: list[dict], **kwargs) -> dict:
        last_err = None
        for attempt in range(self.empty_response_retries + 1):
            try:
                return super().query(messages, **kwargs)
            except IndexError as e:  # response.choices was empty (no candidates)
                last_err = e
                print(f"    empty model response (no candidates), attempt {attempt + 1}/{self.empty_response_retries + 1}")
                if attempt < self.empty_response_retries:
                    time.sleep(1.5)
        raise FormatError(
            {
                "role": "user",
                "content": (
                    "Your last response was empty -- the model returned no output, likely a "
                    "safety/recitation filter on the source text. Respond again and make a bash "
                    "tool call to continue; avoid pasting large verbatim source blocks."
                ),
                "extra": {},
            }
        ) from last_err


# Default litellm observation template, plus (1) hard output truncation so one `cat` of a large
# file can't balloon context/cost -- the full output lives in msg extra, which is stripped before
# resend, so truncating here caps what actually goes back to the model -- and (2) a live step-budget
# line so the model self-paces and writes its answer before running out of steps.
OBSERVATION_TEMPLATE = (
    "{% if output.exception_info %}<exception>{{output.exception_info}}</exception>\n{% endif %}"
    "<returncode>{{output.returncode}}</returncode>\n<output>\n"
    "{{ output.output | truncate(8000, True, ' ...[TRUNCATED: output too long. Do NOT cat whole "
    "files; use grep/sed/head for targeted reads.]') }}"
    "\n</output>\n"
    "<budget>You have used {{n_model_calls}} of {{step_limit}} steps. If only a few remain, write "
    "your current best-effort prediction to the answer file NOW.</budget>"
)


def cache_stats(messages: list[dict]) -> dict:
    """Summarize Gemini implicit-cache hits across a run (4a). Vertex/Gemini caches repeated
    conversation prefixes automatically and litellm bills cached tokens at a deep discount --
    healthy runs show ~80% of prompt tokens cached. If this drops near 0%, cost jumps ~3-4x;
    do NOT "fix" it with LitellmModel's set_cache_control: that marker is Anthropic-specific,
    and on Vertex litellm reroutes marked messages into per-step explicit cachedContents API
    calls with the wrong message split (see separate_cached_messages in litellm)."""
    prompt = cached = 0
    for m in messages:
        usage = (m.get("extra") or {}).get("response", {}).get("usage") or {}
        prompt += usage.get("prompt_tokens") or 0
        cached += (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    return {
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "cached_pct": round(100 * cached / prompt) if prompt else None,
    }


def build_mounts(src_dir: Path, poc_file: Path, answer_dir: Path) -> list[DockerMount]:
    mounts = [
        DockerMount(source=str(src_dir.resolve()), target=SRC_MOUNT, readonly=True),
        DockerMount(source=str(poc_file.resolve()), target=POC_MOUNT, readonly=True),
        DockerMount(source=str(answer_dir.resolve()), target=ANSWER_DIR, readonly=False),
    ]
    if (src_dir / ".git").exists():
        EMPTY_MASK_DIR.mkdir(exist_ok=True)
        mounts.append(DockerMount(source=str(EMPTY_MASK_DIR.resolve()), target=f"{SRC_MOUNT}/.git", readonly=True))
    return mounts


def run(bug_dir: Path, *, model_name: str, step_limit: int = 15, cost_limit: float = 0.50) -> dict:
    meta = json.loads((bug_dir / "meta.json").read_text())
    poc_file = bug_dir / "poc"
    if not poc_file.exists():
        raise SystemExit(f"{bug_dir}: no poc file")

    src_dir = bug_dir / "src"
    answer_dir = bug_dir / "answer"
    answer_dir.mkdir(parents=True, exist_ok=True)
    prediction_path = answer_dir / "prediction.json"
    prediction_path.unlink(missing_ok=True)  # never read a stale answer from a prior run
    (bug_dir / "result.json").unlink(missing_ok=True)  # same stale-read hazard as prediction.json

    method = materialize_source(meta["repo_addr"], meta["vuln_commit"], src_dir)
    print(f"{bug_dir.name} ({meta['project']}): source {method} -> {src_dir}")

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
        output_path=bug_dir / "trajectory.json",
    )
    started_at = time.time()
    # A single bug crashing mid-run (e.g. IndexError when Vertex returns empty `choices`) must not
    # abort a whole batch or discard the partial answer already written to disk -- fall through to
    # the validate_prediction read-back either way.
    try:
        agent_result = agent.run(project=meta["project"], sanitizer=meta["sanitizer"])
        exit_status = agent_result.get("exit_status")
        print(f"exit_status={exit_status!r} n_calls={agent.n_calls} cost=${agent.cost:.4f}")
    except Exception as e:
        exit_status = f"crashed: {type(e).__name__}: {e}"
        print(f"run crashed after {agent.n_calls} calls (cost=${agent.cost:.4f}): {type(e).__name__}: {e}")
    cache = cache_stats(agent.messages)
    print(f"cache: {cache['cached_tokens']}/{cache['prompt_tokens']} prompt tokens cached ({cache['cached_pct']}%)")

    prediction, invalid_reason = None, None
    try:
        prediction = validate_prediction(prediction_path)
        print("prediction:", json.dumps(prediction, indent=2))
    except InvalidPrediction as e:
        invalid_reason = str(e)
        print(f"no valid prediction: {invalid_reason}")

    result = {
        "bug_id": bug_dir.name,
        "project": meta["project"],
        "sanitizer": meta["sanitizer"],
        "model": model_name,
        "step_limit": step_limit,
        "cost_limit": cost_limit,
        "exit_status": exit_status,
        "n_calls": agent.n_calls,
        "cost": round(agent.cost, 6),
        "cache": cache,
        "prediction": prediction,
        "invalid_reason": invalid_reason,
        "started_at": started_at,
        "duration_seconds": round(time.time() - started_at, 1),
    }
    result_path = bug_dir / "result.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    print(f"result written to {result_path}")
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bug_dir", type=Path, help="harvested bug folder, e.g. ../data_smoke/40096184")
    ap.add_argument("--model", default="anthropic/claude-haiku-4-5-20251001")
    ap.add_argument("--step-limit", type=int, default=15)
    ap.add_argument("--cost-limit", type=float, default=0.50)
    a = ap.parse_args()
    run(a.bug_dir, model_name=a.model, step_limit=a.step_limit, cost_limit=a.cost_limit)


if __name__ == "__main__":
    main()
