#!/usr/bin/env python3
"""Phase 6a: build the verl GRPO parquet dataset from a Phase-5g banded split.

Analog of exec-rl's ``exec_rl/data.py``, but sourced from our on-disk harvest
(``data/<id>/``) instead of a kArena SQLite DB. Given ``runs/sweep3fp/split.json``
(the std>0 train/test bug lists 5g produces) it emits ``train.parquet`` /
``val.parquet`` in the row shape verl's data loader + our agent loop (6e) read:

    id            bug id (the ARVO localId)
    data_source   "arvo/crash_prediction"
    ability       "crash_prediction"
    agent_name    "arvo_crash"           -> selects our verl agent loop (6d/6e)
    prompt        frozen [system, user] messages (verl seeds these into rollout)
    reward_model  {"style": "rule", "ground_truth": {...}}  -> what 6b scores
    extra_info    split/index/bug_id, project, sanitizer, the base64 PoC bytes,
                  and an `environment` block carrying everything the agent loop
                  needs to BUILD the locked sandbox at rollout time (repo_addr,
                  vuln_commit, mount paths) -- our per-bug analog of exec-rl's
                  prebuilt kernel image name.

Two ARVO deviations from exec-rl's data.py:
  * No prebuilt per-bug image. exec-rl bakes a kernel image per bug; we ship the
    repo+commit and let 6e fetch/checkout/lock the sandbox from arvo-sandbox:base.
  * `reproducer` is arbitrary PoC *bytes* (base64), not a syzbot text repro.

The prompt carries ONLY project + sanitizer (never crash_type/fix_commit/frames)
-- same leakage rule as the sandbox. `_assert_no_leak` enforces it per row.

  # once the sweep + difficulty.py have written split.json:
  python phase6/build_dataset.py --split runs/sweep3fp/split.json --name sweep3fp
  python phase6/build_dataset.py --split runs/sweep3fp/split.json --dry-run
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from jinja2 import StrictUndefined, Template

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sandbox_contract import ANSWER_FILE, BASE_IMAGE, POC_MOUNT, SRC_MOUNT  # noqa: E402
from score import ground_truth_frames  # noqa: E402

DATA_SOURCE = "arvo/crash_prediction"
ABILITY = "crash_prediction"
AGENT_LOOP_NAME = "arvo_crash"

# 6c: the SHARED prompt. This same yaml is rendered here (training rows) AND by
# the eval agent (mini-swe-agent), so the two prompts are byte-identical --
# exec-rl's key property (data.py:load_prompt). Rendered with jinja2 directly,
# NOT by importing mini-swe-agent, so dataset generation stays free of the agent
# runtime's deps (also exec-rl's choice); render_template is literally
# Template(t, StrictUndefined).render(**vars), so the output is identical.
PROMPT_YAML = Path(__file__).resolve().parent / "prompts" / "crash-predictor.yaml"


def load_prompt_config(prompt_yaml: Path) -> dict[str, str]:
    """The agent-config yaml's system/instance templates (un-rendered jinja)."""
    return yaml.safe_load(prompt_yaml.read_text())["agent"]


def render_prompt(prompt_cfg: dict[str, str], template_vars: dict[str, str]) -> list[dict[str, str]]:
    """The frozen [system, user] messages for one bug. Only project + sanitizer
    are exposed as template vars -- crash_type / fix_commit / frames are the
    hidden label. StrictUndefined means an unfilled template var fails loudly."""
    def r(template: str) -> str:
        return Template(template, undefined=StrictUndefined).render(**template_vars)

    return [
        {"role": "system", "content": r(prompt_cfg["system_template"])},
        {"role": "user", "content": r(prompt_cfg["instance_template"])},
    ]


def _assert_no_leak(template_vars: dict[str, str], gt: dict, meta: dict) -> None:
    """Guard: the SCORED crash label must never enter the prompt through the
    per-bug data we interpolate into it. Cheap defence for the project's central
    reward-hacking risk -- a leaked frame or fix commit would let the policy read
    the answer from the prompt instead of the source.

    The check is scoped to `template_vars` (project + sanitizer), NOT the whole
    rendered prompt: everything else is FIXED template prose that is identical
    across every bug, so a ground-truth token appearing there is a coincidental
    word (e.g. the crash function `main` inside "remain", or `execute` inside the
    literal instruction "not to execute the target"), never a bug-specific leak.
    Guarding the injected values is the true attack surface and avoids dropping
    ~40% of bugs whose crash function is a common English word. Crash *type* is
    deliberately NOT guarded -- reward.py scores only file/function/line."""
    injected = "\n".join(str(v) for v in template_vars.values())
    forbidden = {gt.get("fix_commit"), meta.get("fix_commit")}
    for frame in gt.get("frames", []) + gt.get("frames_clean", []):
        forbidden.add(frame.get("function"))
        forbidden.add(frame.get("filename"))
    for token in forbidden:
        if token and str(token) in injected:
            raise ValueError(f"prompt leaks ground-truth token {token!r} for bug {meta.get('localId')}")


def build_row(bug_id: str, data_dir: Path, split: str, index: int, prompt_cfg: dict[str, str]) -> dict[str, Any]:
    """One verl parquet row from a harvested bug folder. Raises if the folder is
    incomplete (missing poc/ground_truth/meta) or has no usable frames."""
    bug = data_dir / bug_id
    gt = json.loads((bug / "ground_truth.json").read_text())
    meta = json.loads((bug / "meta.json").read_text())
    frames = ground_truth_frames(gt)
    if not frames:
        raise ValueError(f"bug {bug_id}: no usable ground-truth frames")
    poc = (bug / "poc").read_bytes()

    template_vars = {"project": meta["project"], "sanitizer": meta["sanitizer"]}
    prompt = render_prompt(prompt_cfg, template_vars)
    _assert_no_leak(template_vars, gt, meta)

    return {
        "id": bug_id,
        "data_source": DATA_SOURCE,
        "ability": ABILITY,
        "agent_name": AGENT_LOOP_NAME,
        "prompt": prompt,
        "reward_model": {
            "style": "rule",
            # ground_truth_frames() prefers frames_clean; storing under "frames"
            # is what 6b's score.compute_score reads. crash_type kept for a v2.
            "ground_truth": {
                "frames": frames,
                "crash_type_coarse": gt.get("crash_type_coarse"),
                "n_frames": len(frames),
            },
        },
        "extra_info": {
            "split": split,
            "index": index,
            "bug_id": bug_id,
            "project": meta["project"],
            "sanitizer": meta["sanitizer"],
            # PoC bytes travel base64 in the row; the agent loop (6e) decodes and
            # mounts them read-only at POC_MOUNT -- arbitrary bytes, not text.
            "reproducer_b64": base64.b64encode(poc).decode("ascii"),
            # Everything 6e needs to build the locked sandbox at rollout time.
            "environment": {
                "image": BASE_IMAGE,
                "repo_addr": meta["repo_addr"],
                "vuln_commit": meta["vuln_commit"],
                "src_subdir": meta.get("vuln_src_dir_in_image"),
                "src_mount": SRC_MOUNT,
                "poc_mount": POC_MOUNT,
                "answer_file": ANSWER_FILE,
            },
        },
    }


def build_rows(bug_ids: list[str], data_dir: Path, split: str, prompt_cfg: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Build every row for a split, collecting (not raising on) unbuildable bugs."""
    rows, skipped = [], []
    for i, bug_id in enumerate(bug_ids):
        try:
            rows.append(build_row(bug_id, data_dir, split, i, prompt_cfg))
        except (FileNotFoundError, ValueError, KeyError) as e:
            skipped.append(f"{bug_id}: {e}")
    return rows, skipped


def write_prompts(out_dir: Path, prompt_yaml: Path) -> str:
    """Copy the shared prompt yaml into the dataset dir (exec-rl write_prompts).
    Training points MSWEA_VERL_CONFIG_PATH at this copy, so a dataset is scored
    with the exact prompt it was built with -- frozen alongside the data."""
    dst_dir = out_dir / "prompts"
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(prompt_yaml, dst_dir / prompt_yaml.name)
    return f"prompts/{prompt_yaml.name}"


def build_dataset(split_path: Path, data_dir: Path, out_dir: Path, name: str,
                  prompt_yaml: Path = PROMPT_YAML, dry_run: bool = False) -> dict[str, Any]:
    split = json.loads(split_path.read_text())
    train_ids = [str(b) for b in split.get("train", [])]
    test_ids = [str(b) for b in split.get("test", [])]
    overlap = sorted(set(train_ids) & set(test_ids))
    if overlap:
        raise ValueError(f"bug(s) in both train and test: {overlap}")

    prompt_cfg = load_prompt_config(prompt_yaml)
    train_rows, train_skip = build_rows(train_ids, data_dir, "train", prompt_cfg)
    val_rows, val_skip = build_rows(test_ids, data_dir, "validation", prompt_cfg)

    manifest = {
        "name": name,
        "source_split": str(split_path),
        "data_dir": str(data_dir),
        "created": datetime.now().isoformat(timespec="seconds"),
        "train_count": len(train_rows),
        "validation_count": len(val_rows),
        "train_bug_ids": [r["id"] for r in train_rows],
        "validation_bug_ids": [r["id"] for r in val_rows],
        "skipped": train_skip + val_skip,
        "reward_name": "crash_site",
        "agent_name": AGENT_LOOP_NAME,
        "data_source": DATA_SOURCE,
        "prompt": str(prompt_yaml),
    }

    if dry_run:
        return manifest

    import pandas as pd

    out_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(train_rows).to_parquet(out_dir / "train.parquet", index=False)
    pd.DataFrame(val_rows).to_parquet(out_dir / "val.parquet", index=False)
    manifest["prompt"] = write_prompts(out_dir, prompt_yaml)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", type=Path, required=True, help="split.json from difficulty.py (train/test bug ids)")
    ap.add_argument("--data", type=Path, default=REPO_ROOT / "data", help="harvest dir with data/<id>/ folders")
    ap.add_argument("--out-dir", type=Path, default=None, help="dataset dir (default phase6/datasets/<name>)")
    ap.add_argument("--name", default=None, help="dataset name (default: split's parent dir name, else timestamp)")
    ap.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    a = ap.parse_args(argv)

    if not a.split.exists():
        raise SystemExit(f"{a.split}: not found (run the sweep + difficulty.py first)")
    if not a.data.is_dir():
        raise SystemExit(f"{a.data}: not a directory (pass --data)")

    name = a.name or (a.split.resolve().parent.name if a.split.resolve().parent.name != "." else datetime.now().strftime("dataset-%Y%m%d-%H%M%S"))
    out_dir = a.out_dir or (Path(__file__).resolve().parent / "datasets" / name)

    manifest = build_dataset(a.split, a.data, out_dir, name, dry_run=a.dry_run)
    print(f"train rows: {manifest['train_count']}   validation rows: {manifest['validation_count']}")
    if manifest["skipped"]:
        print(f"skipped {len(manifest['skipped'])} unbuildable bug(s):")
        for s in manifest["skipped"]:
            print(f"  {s}")
    if a.dry_run:
        print("(dry run -- nothing written)")
    else:
        print(f"wrote {out_dir}/train.parquet, val.parquet, manifest.json")


if __name__ == "__main__":
    main()
