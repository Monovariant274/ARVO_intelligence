#!/usr/bin/env python3
"""ARVO Phase-3 sandbox launcher (step 3d).

Ties 3b (source fetch) and 3c (locked-down image) together: given one
harvested bug folder, makes sure its source is checked out, then starts the
sandbox container for it under sandbox_contract.py's rules.

Usage:
  python3 launch_sandbox.py data/42474687                # interactive shell
  python3 launch_sandbox.py data/42474687 --cmd 'ls /workspace/src'   # one-off, non-interactive
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from fetch_source import materialize_source
from sandbox_contract import BASE_IMAGE, docker_run_args


def launch(bug_dir: Path, *, cmd: str | None = None, capture: bool = False):
    """Runs the sandbox for one bug. With capture=True, returns the
    CompletedProcess (stdout/stderr captured) instead of streaming to the
    terminal and returning an exit code -- used by verify_sandbox.py.
    """
    meta = json.loads((bug_dir / "meta.json").read_text())
    poc_file = bug_dir / "poc"
    if not poc_file.exists():
        raise SystemExit(f"{bug_dir}: no poc file (harvested as ok(no-poc), can't sandbox this one)")

    src_dir = bug_dir / "src"
    answer_dir = bug_dir / "answer"
    answer_dir.mkdir(parents=True, exist_ok=True)

    method = materialize_source(meta["repo_addr"], meta["vuln_commit"], src_dir)
    print(f"{bug_dir.name} ({meta['project']}): source {method} -> {src_dir}")

    args = docker_run_args(src_dir, poc_file, answer_dir,
                            container_name=f"arvo-sandbox-{bug_dir.name}")
    if cmd:
        # non-interactive one-off command, for testing/automation (Phase 4 will need this shape)
        args = [a for a in args if a != "-it"]
        idx = args.index(BASE_IMAGE)
        args = args[:idx] + ["--entrypoint", "bash", BASE_IMAGE, "-c", cmd]

    print("docker", " ".join(args))
    if capture:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    return subprocess.run(["docker", *args]).returncode


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bug_dir", type=Path, help="harvested bug folder, e.g. data/42474687")
    ap.add_argument("--cmd", help="run this instead of an interactive shell")
    a = ap.parse_args()
    raise SystemExit(launch(a.bug_dir, cmd=a.cmd))


if __name__ == "__main__":
    main()
