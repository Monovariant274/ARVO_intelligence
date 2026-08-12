#!/usr/bin/env python3
"""Phase 5b: clean harvested ground-truth frames into the shape the reward scores.

Mirrors exec-rl's `ground_truth_process.py`, which asks an LLM to (1) prune the
sanitizer / crash-reporting frames at the top of the stack, (2) re-index depth
from 0 after exclusion, and (3) emit root-relative filenames. ARVO's ASAN/MSAN/
UBSAN reports are far more regular than kernel oops text, so we do the same three
things deterministically with regex instead of an LLM — cheaper, reproducible,
and no extra model dependency. (If a messier report ever needs it, an LLM pass
is the drop-in exec-rl equivalent.)

Why it's needed: `crash_parser.py` records frames verbatim from the report, so
  * the TOP frame is often sanitizer runtime (`__asan_memcpy`,
    `llvm/.../compiler-rt/lib/asan/...`) rather than the faulting app code, and
  * `filename` still carries the ARVO build root + `..` segments
    (`skia/out/Fuzz/../../src/codec/SkSwizzler.cpp`) instead of the repo-relative
    path the agent actually sees (`src/codec/SkSwizzler.cpp`).
Both distort reward.py's LCS/depth scoring, so we canonicalize once, up front.

Non-destructive + re-runnable: original `frames` (with `raw_file`) are kept; we
ADD `frames_clean` (the scored frames) and `excluded_frames` (what we pruned and
why), mirroring exec-rl's `frames` / `excludedFrames` payload split.

  python3 frame_clean.py                 # clean every data/<id>/ground_truth.json
  python3 frame_clean.py --dry-run       # report only, write nothing
  python3 frame_clean.py --data ./data --limit 20
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# Sanitizer / crash-reporting runtime frames. Pruned ONLY while they sit at the
# TOP of the stack (exec-rl: "prune the most recently called sanitizer, oops,
# ... frames at the top") — a match deeper in the stack is left alone.
_INFRA_FUNC_RE = re.compile(
    r"^(?:__(?:asan|msan|tsan|lsan|ubsan|hwasan|sanitizer)|"
    r"__interceptor|asan_report|ubsan_|__gnu_cxx::__verbose)"
)
_INFRA_FILE_MARKERS = (
    "compiler-rt/",
    "/sanitizer_common/",
    "llvm/projects/compiler-rt",
    "/libsanitizer/",
)


def _is_infra_frame(frame: dict) -> bool:
    func = (frame.get("function") or "").strip()
    raw = (frame.get("raw_file") or frame.get("filename") or "")
    if _INFRA_FUNC_RE.match(func):
        return True
    return any(m in raw for m in _INFRA_FILE_MARKERS)


def _resolve_segments(path: str) -> list[str]:
    parts: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            else:
                parts.append(seg)
            continue
        parts.append(seg)
    return parts


def canonical_repo_path(raw_path: str) -> str:
    """Turn an ARVO report path into a repo-relative one.

    ARVO always checks the source out at `/src/<project>/`, so the build root is
    the first `src/` segment plus the one project segment after it. We resolve
    `.`/`..` first (skia's `out/Fuzz/../../src/codec` collapses correctly), then
    drop through that build root:

        /src/skia/out/Fuzz/../../src/codec/SkSwizzler.cpp -> src/codec/SkSwizzler.cpp
        /src/ffmpeg/libavcodec/rasc.c                     -> libavcodec/rasc.c
    """
    parts = _resolve_segments(raw_path or "")
    if "src" in parts:
        i = parts.index("src")
        parts = parts[i + 2:] if len(parts) > i + 1 else parts[i + 1:]
    return "/".join(parts)


def clean_frames(frames: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split raw frames into (scored, excluded).

    Leading infra frames are excluded; the first real app frame onward is kept,
    depth re-indexed from 0, filename canonicalized to repo-relative. If EVERY
    frame is infra (no app code in the stack) we keep them rather than emptying
    the ground truth — better a hard bug than an unscorable one.
    """
    excluded: list[dict] = []
    i = 0
    while i < len(frames) and _is_infra_frame(frames[i]):
        f = frames[i]
        excluded.append({"function": f.get("function"), "reason": "sanitizer/reporting runtime frame"})
        i += 1

    kept = frames[i:] if i < len(frames) else frames  # all-infra -> keep original
    if i >= len(frames):
        excluded = []

    clean: list[dict] = []
    for new_depth, f in enumerate(kept):
        raw = f.get("raw_file") or f.get("filename") or ""
        clean.append(
            {
                "depth": new_depth,
                "function": (f.get("function") or "").strip(),
                "filename": canonical_repo_path(raw),
                "line": int(f.get("line", 0)),
                "raw_file": raw,
            }
        )
    return clean, excluded


def clean_ground_truth(gt: dict) -> dict:
    """Add `frames_clean` + `excluded_frames` to a ground-truth dict (in place)."""
    frames = gt.get("frames") or []
    clean, excluded = clean_frames(frames)
    gt["frames_clean"] = clean
    gt["excluded_frames"] = excluded
    return gt


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=Path("data"), help="harvest output dir (default ./data)")
    ap.add_argument("--limit", type=int, help="cap how many bugs to process")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    a = ap.parse_args()

    if not a.data.is_dir():
        raise SystemExit(f"{a.data}: not a directory")

    gt_files = sorted(a.data.glob("*/ground_truth.json"))
    if a.limit is not None:
        gt_files = gt_files[: a.limit]

    n_written = n_pruned = n_allinfra = 0
    for gt_path in gt_files:
        try:
            gt = json.loads(gt_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        before = len(gt.get("frames") or [])
        clean_ground_truth(gt)
        pruned = len(gt["excluded_frames"])
        if pruned:
            n_pruned += 1
        if before and not gt["frames_clean"]:
            n_allinfra += 1
        if not a.dry_run:
            gt_path.write_text(json.dumps(gt, indent=2))
            n_written += 1

    verb = "would clean" if a.dry_run else "cleaned"
    print(f"{verb} {len(gt_files)} ground_truth.json files")
    print(f"  {n_pruned} had leading sanitizer/infra frames pruned")
    if n_allinfra:
        print(f"  {n_allinfra} were all-infra (kept original frames, flagged)")
    if not a.dry_run:
        print(f"  wrote frames_clean + excluded_frames to {n_written} files")


if __name__ == "__main__":
    main()
