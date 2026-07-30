#!/usr/bin/env python3
"""Phase 6e: the verl agent loop for ARVO crash-site RL.

Analog of exec-rl's ``kAgentLoop`` (``exec_rl/rl/agent.py``). exec-rl's
``MiniSweAgentLoop`` is the generic verl<->mini-swe-agent adapter (builds the
``VerlServerModel`` for in-cluster generation + token bookkeeping, drives the
shared ``KernelAgent``, assembles the ``AgentLoopOutput``). We subclass it and
override only the two methods that carry the ARVO delta -- exactly the shape of
exec-rl's own ``kAgentLoop`` -- so all the verl coupling stays in the tested
upstream adapter:

  * ``_make_env``  -- exec-rl mounts a *prebuilt per-bug kernel image* and drops a
    text reproducer beside it. We have neither: we BUILD the locked sandbox at
    rollout time from the row's ``extra_info.environment`` block (6a) -- fetch the
    repo, check out ``vuln_commit``, mask ``.git`` (the next commit is the fix),
    decode the base64 PoC *bytes*, and mount src/poc read-only + an empty writable
    answer dir under ``sandbox_contract.lockdown_flags()`` on ``arvo-sandbox:base``.
    This is the exact sandbox Phase-3/eval (``run_prediction.py``) already builds,
    so a trajectory sees the identical box in training and eval.

  * ``compute_reward`` -- read the prediction the agent wrote to ``ANSWER_FILE``
    back out of the still-live container and stash it on the row's ``extra_info``
    (in-place, the same dict verl hands the reward manager). No scoring lives here:
    returning ``None`` leaves ``reward_score`` unset so the script-configured
    ``custom_reward_function`` (6b -> ``score.compute_score``) scores it, keeping
    train and eval on the one scoring path (5d).

The class is registered ``arvo_crash`` -- matching ``AGENT_LOOP_NAME`` in 6a and
the ``name`` in ``phase6/config/verl_arvo_agent_loop.yaml`` (6d), so verl routes
our rows here.

CANNOT run on this box (no GPU / no verl / no vLLM). Written against the pinned
exec-rl + vendored mini-swe-agent APIs; it comes alive on the 6g training host.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from verl.experimental.agent_loop.agent_loop import register  # noqa: F401  (verl-only import)

from minisweagent import Environment
from minisweagent.environments import get_environment

# exec-rl's generic verl<->mini-swe-agent adapter; we override only the ARVO bits.
from exec_rl.rl.agent import MiniSweAgentLoop

# Repo-root modules (sandbox contract + source fetch); the 6f launch script puts
# the repo root on PYTHONPATH, but insert it here too so a bare import works.
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fetch_source import materialize_source  # noqa: E402
from sandbox_contract import (  # noqa: E402
    ANSWER_DIR,
    ANSWER_FILE,
    BASE_IMAGE,
    EMPTY_MASK_DIR,
    POC_MOUNT,
    SRC_MOUNT,
    lockdown_flags,
)

# Optional per-rollout cgroup memory cap, mirroring exec-rl's EXEC_RL_ROLLOUT_MEMORY_LIMIT
# (e.g. "2g"). Empty = no cap. Bounds each rollout container's RSS so many concurrent
# rollouts on the agent-loop node can't OOM the host.
_ROLLOUT_MEMORY_LIMIT = os.environ.get("ARVO_ROLLOUT_MEMORY_LIMIT", "").strip()

# Host dir for per-rollout scratch (fetched source + poc + answer). Override when the
# training container's fs is small; must be a path the *host* docker daemon can bind.
_WORKDIR_ROOT = os.environ.get("ARVO_ROLLOUT_WORKDIR", "").strip() or None


@register("arvo_crash")
class ArvoCrashAgentLoop(MiniSweAgentLoop):
    """verl agent loop that builds the ARVO locked sandbox per rollout and reads
    back the agent's crash-site prediction for the shared scorer."""

    def _make_env(self, kwargs: dict) -> Environment:
        extra_info = kwargs.get("extra_info") or {}
        env_block = extra_info.get("environment") or {}

        # One scratch dir per rollout; wiped after the container is cleaned up.
        workdir = Path(tempfile.mkdtemp(prefix="arvo-rollout-", dir=_WORKDIR_ROOT))
        try:
            src_dir = workdir / "src"
            answer_dir = workdir / "answer"
            answer_dir.mkdir(parents=True, exist_ok=True)
            poc_file = self._write_poc(extra_info, workdir)

            # Fetch + checkout the vulnerable tree (blocking git; the base loop runs
            # _make_env in an executor, so this doesn't stall the event loop).
            materialize_source(env_block["repo_addr"], env_block["vuln_commit"], src_dir)

            env_config = {
                "image": env_block.get("image", BASE_IMAGE),
                "cwd": SRC_MOUNT,
                "run_args": self._run_args(),
                "mounts": self._mounts(src_dir, poc_file, answer_dir),
                "container_timeout": "30m",
            }
            # Build the DockerEnvironment directly (NOT super()._make_env): the base
            # adds a /skills mount our prompts never use and re-merges self.env_config,
            # neither of which applies to our fully-specified locked box.
            env = get_environment(env_config, default_type="docker")
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise

        self._wipe_workdir_after_cleanup(env, workdir)
        return env

    def _write_poc(self, extra_info: dict, workdir: Path) -> Path:
        """Decode the row's base64 PoC into a host file to mount read-only. ARVO PoCs
        are arbitrary *bytes* (a fuzzer input), never text -- so no encoding."""
        import base64

        poc_file = workdir / "poc"
        poc_file.write_bytes(base64.b64decode(extra_info["reproducer_b64"]))
        return poc_file

    def _run_args(self) -> list[str]:
        args = ["--rm", *lockdown_flags()]
        if _ROLLOUT_MEMORY_LIMIT:
            args += [f"--memory={_ROLLOUT_MEMORY_LIMIT}", f"--memory-swap={_ROLLOUT_MEMORY_LIMIT}"]
        return args

    def _mounts(self, src_dir: Path, poc_file: Path, answer_dir: Path) -> list[dict]:
        """Same mount set as eval's build_mounts (run_prediction.py): src + poc
        read-only, answer dir writable, and an empty dir masking .git so the fix
        commit that sits right after vuln_commit can't leak. Dict form; the
        DockerEnvironment config coerces each into a DockerMount."""
        mounts = [
            {"source": str(src_dir.resolve()), "target": SRC_MOUNT, "readonly": True},
            {"source": str(poc_file.resolve()), "target": POC_MOUNT, "readonly": True},
            {"source": str(answer_dir.resolve()), "target": ANSWER_DIR, "readonly": False},
        ]
        if (src_dir / ".git").exists():
            EMPTY_MASK_DIR.mkdir(exist_ok=True)
            mounts.append(
                {"source": str(EMPTY_MASK_DIR.resolve()), "target": f"{SRC_MOUNT}/.git", "readonly": True}
            )
        return mounts

    @staticmethod
    def _wipe_workdir_after_cleanup(env: Environment, workdir: Path) -> None:
        """Wrap env.cleanup so the per-rollout scratch dir is removed after the
        container is torn down (mirrors exec-rl's temp-reproducer cleanup)."""
        original = getattr(env, "cleanup", None)

        def cleanup_and_wipe() -> None:
            try:
                if original is not None:
                    original()
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

        env.cleanup = cleanup_and_wipe

    async def _read(self, env: Environment, command: str) -> str:
        """Run a read-only command in the live container, returning stdout (or ""
        on nonzero exit). Same helper as exec-rl's kAgentLoop._read."""
        result = await self.loop.run_in_executor(None, lambda: env.execute({"command": command}))
        return result.get("output", "") if result.get("returncode") == 0 else ""

    async def compute_reward(self, env: Environment, messages: list[dict], output, kwargs: dict) -> float | None:
        """Read the prediction the agent wrote to ANSWER_FILE and expose it on the
        row's extra_info under "context_output" (raw JSON text) -- score.compute_score
        (6b) parses/validates it there. Missing/empty file -> "" -> parse_prediction
        returns None -> INVALID_PREDICTION_REWARD (-1), which is exactly what a
        no-answer rollout should score. Returns None so the custom_reward_function
        does the scoring; no reward math lives here."""
        raw = await self._read(env, f"cat {ANSWER_FILE}")
        output.extra_fields["context_output"] = raw
        if isinstance(kwargs.get("extra_info"), dict):
            kwargs["extra_info"]["context_output"] = raw
        return None
