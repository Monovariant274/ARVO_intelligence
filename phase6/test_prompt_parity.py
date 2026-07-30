#!/usr/bin/env python3
"""Phase 6c guard: the frozen dataset prompt == the live eval prompt, byte-for-byte.

exec-rl's signature property: ONE prompt seeds the training rows (build_dataset.py,
via phase6/prompts/crash-predictor.yaml) AND is rendered by the eval agent at
rollout/eval time (msagent_runner/run_prediction.py, via mini-swe-agent). If the
two ever drift, the policy is trained against a prompt it never sees at eval and
the reward numbers stop meaning anything.

Both sides render through the SAME jinja entry point -- mini-swe-agent's
render_template is literally ``Template(t, StrictUndefined).render(**vars)`` and
build_dataset.render_prompt uses the identical call -- so parity reduces to a
single claim this test pins: the yaml's system/instance templates are byte-equal
to run_prediction.py's live SYSTEM_TEMPLATE / INSTANCE_TEMPLATE after rendering.

Run:  .venv/bin/python phase6/test_prompt_parity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "msagent_runner"))

from minisweagent.utils.jinja import render_template  # noqa: E402

import run_prediction  # noqa: E402  (the live eval templates)
from build_dataset import PROMPT_YAML, load_prompt_config, render_prompt  # noqa: E402

# A spread of project/sanitizer values, including punctuation/space that jinja
# passes through verbatim, so any accidental escaping would show up as a diff.
CASES = [
    ("libpng", "AddressSanitizer"),
    ("openssl", "MemorySanitizer"),
    ("harfbuzz", "UndefinedBehaviorSanitizer"),
    ("re2", "LeakSanitizer"),
    ('weird "quoted" proj', "Address; Sanitizer"),
]


def eval_side(project: str, sanitizer: str) -> list[str]:
    """What the eval agent actually feeds the model (run_prediction templates)."""
    return [
        render_template(run_prediction.SYSTEM_TEMPLATE, project=project, sanitizer=sanitizer),
        render_template(run_prediction.INSTANCE_TEMPLATE, project=project, sanitizer=sanitizer),
    ]


def dataset_side(prompt_cfg: dict, project: str, sanitizer: str) -> list[str]:
    """What build_dataset.py freezes into the training parquet rows."""
    msgs = render_prompt(prompt_cfg, {"project": project, "sanitizer": sanitizer})
    return [m["content"] for m in msgs]


def main() -> None:
    prompt_cfg = load_prompt_config(PROMPT_YAML)
    failures = 0
    for project, sanitizer in CASES:
        want = eval_side(project, sanitizer)
        got = dataset_side(prompt_cfg, project, sanitizer)
        for role, w, g in zip(("system", "instance"), want, got):
            if w != g:
                failures += 1
                print(f"DRIFT [{role}] for ({project!r}, {sanitizer!r}):")
                for i, (a, b) in enumerate(zip(w, g)):
                    if a != b:
                        print(f"  first diff at char {i}: eval={a!r} dataset={b!r}")
                        break
                if len(w) != len(g):
                    print(f"  length differs: eval={len(w)} dataset={len(g)}")
    if failures:
        raise SystemExit(f"{failures} prompt(s) drifted -- regenerate {PROMPT_YAML} from run_prediction.py")
    print(f"OK: dataset prompt == eval prompt for all {len(CASES)} cases (system + instance)")


if __name__ == "__main__":
    main()
