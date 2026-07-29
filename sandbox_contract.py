#!/usr/bin/env python3
"""ARVO Phase-3 sandbox contract (step 3a).

Defines the fixed layout and lockdown rules for the box the prediction agent
runs in: source code + crash input + python, no network, no way to write
anywhere except its answer file. This module only assembles the `docker run`
arguments for that contract -- it does not build the base image (step 3c) or
fetch real source code (step 3b), so `main()` here is a dry run: it prints
the command it *would* run.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

# Fixed paths as seen INSIDE the container. Hardcoded so agent prompts and
# later launcher/reward code can rely on them never moving.
SRC_MOUNT = "/workspace/src"        # vulnerable source tree, read-only
POC_MOUNT = "/workspace/poc"        # crash-triggering input file, read-only
ANSWER_DIR = "/workspace/answer"    # empty dir, writable -- agent's prediction goes here
ANSWER_FILE = f"{ANSWER_DIR}/prediction.json"

# Built in step 3c: python only, no compilers/build systems installed.
BASE_IMAGE = "arvo-sandbox:base"

# fetch_source.py checks out real git history to resolve the right commit, but
# `vuln_commit` is defined as the parent of the fix commit -- so the very next
# commit in that history IS the fix, often with a message that gives the crash
# away for free. Mounting an empty dir on top of .git hides all of it from the
# agent regardless of whether the checkout was shallow or a full clone.
EMPTY_MASK_DIR = Path(__file__).parent / ".empty_mount"


def lockdown_flags() -> list[str]:
    """The security flags any docker invocation of BASE_IMAGE must carry, shared
    between docker_run_args() (CLI/manual use) and other launchers (e.g. the
    mini-swe-agent runner, which builds its own docker command)."""
    return [
        "--network", "none",              # can't apt-get a compiler, can't phone home
        "--read-only",                    # container's own filesystem is immutable
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", f"{os.getuid()}:{os.getgid()}",  # write access to answer_dir must match the
                                                     # host uid that owns it, not a fixed image uid
    ]


def docker_run_args(src_dir: Path, poc_file: Path, answer_dir: Path,
                     *, container_name: str | None = None) -> list[str]:
    """Docker args enforcing the contract: src/poc mounted read-only, answer_dir
    is the only writable path, no network, no privilege escalation, no .git
    history (which would leak the fix commit).
    """
    args = [
        "run", "--rm", "-it",
        *lockdown_flags(),
        "-v", f"{src_dir.resolve()}:{SRC_MOUNT}:ro",
        "-v", f"{poc_file.resolve()}:{POC_MOUNT}:ro",
        "-v", f"{answer_dir.resolve()}:{ANSWER_DIR}:rw",
    ]
    if (src_dir / ".git").exists():
        EMPTY_MASK_DIR.mkdir(exist_ok=True)
        args += ["-v", f"{EMPTY_MASK_DIR.resolve()}:{SRC_MOUNT}/.git:ro"]
    if container_name:
        args += ["--name", container_name]
    args.append(BASE_IMAGE)
    return args


def main():
    ap = argparse.ArgumentParser(
        description="Print the docker run command for one bug (dry run; base image not built yet).")
    ap.add_argument("src_dir", type=Path, help="materialized vulnerable source tree (step 3b)")
    ap.add_argument("poc_file", type=Path, help="path to that bug's poc file")
    ap.add_argument("--answer-dir", type=Path, default=Path("./answer"))
    a = ap.parse_args()
    a.answer_dir.mkdir(parents=True, exist_ok=True)
    print("docker", " ".join(docker_run_args(a.src_dir, a.poc_file, a.answer_dir)))


if __name__ == "__main__":
    main()
