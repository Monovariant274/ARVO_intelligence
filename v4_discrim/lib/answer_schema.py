#!/usr/bin/env python3
"""ARVO Phase-3 agent answer format (step 3g).

Defines what the prediction agent must write to sandbox_contract.ANSWER_FILE,
and validates a submitted answer.

Deliberately ONE predicted crash site (function/filename/line/type), not a
full stack: exec-rl's own CrashPrediction (models/entities.py) is shaped the
same way, and its reward.py scores a single predicted site against every
ground-truth frame with depth-decay -- so a correct-but-not-topmost guess
still earns partial credit. Field names here follow this repo's existing
snake_case convention (crash_parser.Frame / ground_truth.json), not exec-rl's
camelCase -- Phase 5 already needs a crash-taxonomy remap adapter (see
PROGRESS.md known issues), so aligning field *names* now buys nothing; the
shape (one site, not a list) is what actually matters for reusing reward.py
without a rewrite. Note reward.py's scoring math doesn't even use crash type
today -- it's kept here for review/judging value, not because it's scored.
"""

from __future__ import annotations

import json
from pathlib import Path

from crash_parser import COARSE_TYPES
from sandbox_contract import ANSWER_FILE

REQUIRED_FIELDS = {"crash_type_coarse", "filename", "function", "line"}


class InvalidPrediction(ValueError):
    pass


def prompt_instructions(answer_file: str = ANSWER_FILE) -> str:
    return f"""Write your final answer to {answer_file} as a single JSON object \
(no markdown, no extra text) with exactly these fields:

  crash_type_coarse : string, your best guess at the crash class. Use one of \
{sorted(COARSE_TYPES)} if it fits, else "other".
  filename           : string, path to the file you believe crashes, relative \
to the source root (e.g. "codec/SkSwizzler.cpp").
  function           : string, the function you believe crashes.
  line               : integer, the line number you believe crashes.
  reasoning          : string, 1-3 sentences on why. Not scored, but required \
for review.

Example:
{{"crash_type_coarse": "heap-buffer-overflow", "filename": "codec/SkSwizzler.cpp", \
"function": "SkSwizzler::SwizzleWidth", "line": 233, "reasoning": "..."}}
"""


def validate_prediction_data(data: object) -> dict:
    """Sanity-checks an already-loaded prediction dict. Raises InvalidPrediction.

    The field-level gate, split out from validate_prediction so the on-disk
    prediction.json path AND the prediction dict stored in result.json (4b) go
    through the *same* checks -- Phase-5 reward wiring (score.py) validates the
    stored dict, run_prediction validates the file, and they must not diverge.
    """
    if not isinstance(data, dict):
        raise InvalidPrediction(f"prediction is not a JSON object: {type(data).__name__}")
    missing = REQUIRED_FIELDS - data.keys()
    if missing:
        raise InvalidPrediction(f"missing required fields: {sorted(missing)}")
    if not isinstance(data["line"], int) or isinstance(data["line"], bool) or data["line"] < 1:
        raise InvalidPrediction(f"'line' must be a positive integer, got {data['line']!r}")
    if not isinstance(data["filename"], str) or not data["filename"]:
        raise InvalidPrediction("'filename' must be a non-empty string")
    if not isinstance(data["function"], str) or not data["function"]:
        raise InvalidPrediction("'function' must be a non-empty string")
    return data


def validate_prediction(path: Path) -> dict:
    """Loads and sanity-checks a submitted prediction.json. Raises InvalidPrediction."""
    if not path.exists():
        raise InvalidPrediction(f"{path} does not exist -- agent never wrote an answer")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise InvalidPrediction(f"{path} is not valid JSON: {e}") from e
    try:
        return validate_prediction_data(data)
    except InvalidPrediction as e:
        raise InvalidPrediction(f"{path}: {e}") from e


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Validate a submitted prediction.json.")
    ap.add_argument("path", type=Path)
    a = ap.parse_args()
    data = validate_prediction(a.path)
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
