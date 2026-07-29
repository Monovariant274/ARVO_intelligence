#!/usr/bin/env python3
"""ARVO Phase-3 sandbox verification (steps 3e + 3f).

Two automated pass/fail checks against a real bug, so "the sandbox is locked
down" and "the sandbox is still usable for analysis" are provable facts --
re-checkable any time the base image or contract changes, not one-off manual
pokes that get forgotten.

  3e lockdown  -- actually attempt the target project's OWN build (its real
                  CMakeLists/Makefile), confirm it fails.
  3f usability -- run a small pure-python heuristic (walk source, grep for a
                  pattern, inspect the poc bytes) -- the kind of thing the
                  prediction agent will actually need to do -- confirm it
                  succeeds and returns sane output.

Usage:
  python3 verify_sandbox.py data/42474758
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from launch_sandbox import launch

LOCKDOWN_CMD = "cd /workspace/src && cmake . ; echo CMAKE_EXIT=$? ; make ; echo MAKE_EXIT=$?"

USABILITY_CMD = """python3 - <<'PY'
import pathlib
src = pathlib.Path('/workspace/src')
files = list(src.rglob('*.c')) + list(src.rglob('*.h'))
hits = [f for f in files if 'memcpy' in f.read_text(errors='ignore')]
poc = pathlib.Path('/workspace/poc').read_bytes()
print('SRC_FILES=%d' % len(files))
print('MEMCPY_HITS=%d' % len(hits))
print('POC_BYTES=%d' % len(poc))
print('POC_HEAD=%s' % poc[:8].hex())
PY
"""


def check_lockdown(bug_dir: Path) -> bool:
    r = launch(bug_dir, cmd=LOCKDOWN_CMD, capture=True)
    out = r.stdout + r.stderr
    cmake_failed = "CMAKE_EXIT=0" not in out
    make_failed = "MAKE_EXIT=0" not in out
    ok = cmake_failed and make_failed
    print("=== 3e lockdown ===")
    print(out.strip())
    print(f"-> cmake failed: {cmake_failed}, make failed: {make_failed}  =>  {'PASS' if ok else 'FAIL'}\n")
    return ok


def check_usability(bug_dir: Path) -> bool:
    r = launch(bug_dir, cmd=USABILITY_CMD, capture=True)
    out = r.stdout
    ok = r.returncode == 0 and "SRC_FILES=" in out and "POC_BYTES=" in out
    print("=== 3f usability ===")
    print(out.strip())
    print(f"-> python heuristic ran cleanly  =>  {'PASS' if ok else 'FAIL'}\n")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bug_dir", type=Path, help="harvested bug folder, e.g. data/42474758")
    a = ap.parse_args()
    ok = check_lockdown(a.bug_dir) & check_usability(a.bug_dir)
    print("ALL PASS" if ok else "SOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
