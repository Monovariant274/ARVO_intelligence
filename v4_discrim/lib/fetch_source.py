#!/usr/bin/env python3
"""ARVO Phase-3 source materializer (step 3b).

Given one harvested bug's meta.json (repo_addr + vuln_commit), checks out the
real source tree at that exact commit, so it can be mounted into the sandbox
(sandbox_contract.py's src_dir). Only fetches a ref (step 2 of Phase 2) had
been recorded so far -- this is what turns that reference into actual files.

Two strategies, tried in order:
  1. shallow fetch-by-commit -- `git fetch --depth 1 <repo> <commit>` then
     checkout. Cheap (one commit's worth of objects), but not every git host
     allows fetching an arbitrary sha (works on github/gitlab, often rejected
     by gitiles hosts like *.googlesource.com).
  2. full clone -- `git clone <repo>` then `git checkout <commit>`. Always
     works, costs the whole repo history.

Resumable: skips repos already checked out at the target commit.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path


# Flaky hosts with identical-sha mirrors (see build_patch_corpus.py / build_dataset.py).
# git.ffmpeg.org intermittently 502s / resets mid-fetch; the GitHub mirror is sha-identical.
_MIRRORS = {"git.ffmpeg.org/ffmpeg.git": "https://github.com/FFmpeg/FFmpeg.git"}


def _mirror(repo: str) -> str:
    for needle, m in _MIRRORS.items():
        if needle in repo:
            return m
    return repo


def _run(args: list[str], cwd: Path | None = None, timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _already_at_commit(dest: Path, commit: str) -> bool:
    if not (dest / ".git").exists():
        return False
    r = _run(["git", "rev-parse", "HEAD"], cwd=dest)
    return r.returncode == 0 and r.stdout.strip() == commit


def _try_shallow(repo_addr: str, commit: str, dest: Path) -> bool:
    dest.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], cwd=dest)
    _run(["git", "remote", "add", "origin", repo_addr], cwd=dest)
    r = _run(["git", "fetch", "--depth", "1", "origin", commit], cwd=dest, timeout=600)
    if r.returncode != 0:
        return False
    r2 = _run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=dest)
    return r2.returncode == 0


def _full_clone(repo_addr: str, commit: str, dest: Path) -> bool:
    shutil.rmtree(dest, ignore_errors=True)
    r = _run(["git", "clone", "-q", repo_addr, str(dest)], timeout=1800)
    if r.returncode != 0:
        return False
    r2 = _run(["git", "checkout", "-q", commit], cwd=dest)
    return r2.returncode == 0


def materialize_source(repo_addr: str, commit: str, dest: Path) -> str:
    """Checks out repo_addr@commit into dest. Returns 'cached', 'shallow', or 'full'."""
    if _already_at_commit(dest, commit):
        return "cached"
    repo_addr = _mirror(repo_addr)
    shutil.rmtree(dest, ignore_errors=True)
    if _try_shallow(repo_addr, commit, dest):
        return "shallow"
    shutil.rmtree(dest, ignore_errors=True)
    if _full_clone(repo_addr, commit, dest):
        return "full"
    raise RuntimeError(f"failed to materialize {repo_addr}@{commit} into {dest}")


def cleanup_source(dest: Path) -> bool:
    """Deletes a materialized source tree (e.g. data/<id>/src). Returns True if
    it existed and was removed. Safe to call when it's absent (returns False).
    Re-fetching is a ~5s shallow fetch, so this is cheap to undo."""
    if not dest.exists():
        return False
    shutil.rmtree(dest, ignore_errors=True)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bug_dir", type=Path, help="harvested bug folder, e.g. data/40096184")
    a = ap.parse_args()

    meta = json.loads((a.bug_dir / "meta.json").read_text())
    dest = a.bug_dir / "src"
    t0 = time.time()
    method = materialize_source(meta["repo_addr"], meta["vuln_commit"], dest)
    print(f"{a.bug_dir.name} ({meta['project']}): {method} in {time.time()-t0:.0f}s -> {dest}")


if __name__ == "__main__":
    main()
