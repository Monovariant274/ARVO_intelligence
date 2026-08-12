#!/usr/bin/env python3
"""v4 discrimination agent answer format (item 1b).

The single-site task (answer_schema.py) asks "WHERE does it crash" and presupposes a
crash exists. The v4 discrimination task asks the harder, prior question: "DOES this
input crash this code AT ALL?" -- because each bug is presented in TWO arms sharing one
poc:

    crash arm : source at the vulnerable (parent) commit  -> the poc crashes
    fix   arm : source at the fix commit                   -> the same poc runs clean

The agent is NOT told which arm it is in. It reads the source + poc and emits a VERDICT
object, which reward_v4.parse_crash_verdict then judges. This module's ONLY job is to (a)
tell the agent exactly the JSON shape parse_crash_verdict will accept, and (b) provide the
same host-side validity gate so a run can report "off-schema" without importing the vendor
reward. The authoritative parser is still vendor/exec_rl/reward_v4.parse_crash_verdict;
validate_verdict here must stay a strict SUBSET of it (never accept what it would reject).

Verdict schema (written to sandbox_contract.ANSWER_FILE as one JSON object, no markdown):

    crashes    : bool     -- true if the poc triggers a memory-safety crash in THIS tree
    bugClass   : string   -- crash class if crashes, else exactly "none"
    reason     : string   -- 1-3 sentences of justification (always required, non-empty)
    crashFrames: [ {filename, function, line} ]  -- crash-site-first; >=1 iff crashes, else []
    allocFrames: [ ... ]  -- optional; the alloc-site stack, for use-after-free / overflow
    freeFrames : [ ... ]  -- optional; the free-site stack, for use-after-free

Mirrors parse_crash_verdict's rules: crashes:false REQUIRES bugClass=="none" and empty
crashFrames; crashes:true REQUIRES >=1 well-formed crashFrame.
"""

from __future__ import annotations

import json
from pathlib import Path

from sandbox_contract import ANSWER_FILE


class InvalidVerdict(ValueError):
    pass


def _frame_ok(f: object) -> bool:
    """A crashFrame parse_crash_verdict/PredictedFrame.from_loose would keep: dict with a
    non-empty string filename AND function (line optional)."""
    if not isinstance(f, dict):
        return False
    filename = f.get("filename") or f.get("crashFilename")
    function = f.get("function") or f.get("crashFunction")
    return isinstance(filename, str) and bool(filename.strip()) and isinstance(function, str) and bool(function.strip())


def prompt_instructions(answer_file: str = ANSWER_FILE) -> str:
    return f"""Decide whether the input at the poc path triggers a memory-safety crash \
in THIS source tree, then write your verdict to {answer_file} as a single JSON object \
(no markdown, no extra text) with exactly these fields:

  crashes     : boolean. true if you conclude the poc triggers a memory-safety crash in \
this exact source tree; false if you conclude this tree handles the input safely (e.g. the \
bug is not present or has been fixed here).
  bugClass    : string. If crashes is true, the crash class (e.g. "heap-buffer-overflow", \
"use-after-free", "stack-overflow", "global-buffer-overflow", "null-dereference"). If \
crashes is false, this MUST be exactly "none".
  reason      : string, 1-3 sentences on WHY -- what in the code makes it crash, or what \
makes it safe here. Always required and non-empty.
  crashFrames : array of stack frames, crash-site FIRST (innermost frame at index 0). Each \
frame is {{"filename": <path relative to the source root>, "function": <name>, "line": \
<positive integer>}}. If crashes is true you MUST give at least one frame. If crashes is \
false this MUST be an empty array [].
  allocFrames : array (optional, same frame shape). For a heap bug, the stack that ALLOCATED \
the object. Omit or [] if unknown.
  freeFrames  : array (optional, same frame shape). For a use-after-free, the stack that \
FREED the object. Omit or [] if unknown.

Example (crash arm):
{{"crashes": true, "bugClass": "heap-buffer-overflow", "reason": "loop reads one past the \
glyph array because the bound uses the wrong count.", "crashFrames": [{{"filename": \
"src/hb-ot-layout.cc", "function": "closure", "line": 778}}], "allocFrames": [], "freeFrames": []}}

Example (safe arm):
{{"crashes": false, "bugClass": "none", "reason": "this revision bounds-checks the index \
before the read, so the poc is handled safely.", "crashFrames": []}}
"""


def validate_verdict_data(data: object) -> dict:
    """Host-side gate; a strict subset of vendor parse_crash_verdict. Raises InvalidVerdict."""
    if not isinstance(data, dict):
        raise InvalidVerdict(f"verdict is not a JSON object: {type(data).__name__}")
    if not isinstance(data.get("crashes"), bool):
        raise InvalidVerdict("'crashes' must be a JSON boolean")
    bug_class, reason = data.get("bugClass"), data.get("reason")
    if not isinstance(bug_class, str) or not bug_class.strip():
        raise InvalidVerdict("'bugClass' must be a non-empty string")
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidVerdict("'reason' must be a non-empty string")
    raw_frames = data.get("crashFrames")
    if raw_frames is None:
        raw_frames = data.get("frames")  # accepted alias, like the vendor parser
    if raw_frames is None:
        raw_frames = []
    if not isinstance(raw_frames, list):
        raise InvalidVerdict("'crashFrames' must be an array")
    frames = [f for f in raw_frames if _frame_ok(f)]
    if data["crashes"] and not frames:
        raise InvalidVerdict("crashes:true requires at least one well-formed crashFrame")
    if not data["crashes"] and (raw_frames or bug_class.strip().casefold() != "none"):
        raise InvalidVerdict("crashes:false requires empty crashFrames and bugClass=='none'")
    return data


def validate_verdict(path: Path) -> dict:
    """Loads + gates a submitted verdict file. Raises InvalidVerdict."""
    if not path.exists():
        raise InvalidVerdict(f"{path} does not exist -- agent never wrote a verdict")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise InvalidVerdict(f"{path} is not valid JSON: {e}") from e
    try:
        return validate_verdict_data(data)
    except InvalidVerdict as e:
        raise InvalidVerdict(f"{path}: {e}") from e


def main():
    import argparse

    ap = argparse.ArgumentParser(description="Validate a submitted v4 verdict JSON.")
    ap.add_argument("path", type=Path)
    a = ap.parse_args()
    print(json.dumps(validate_verdict(a.path), indent=2))


if __name__ == "__main__":
    main()
