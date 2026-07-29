"""Turn an ARVO crash report into structured ground truth.

The ground truth is what we HIDE from the agent and later score its prediction against:
a coarse crash type plus the ordered top stack frames (function / file / line).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# A symbolized frame: "#0 0xADDR in FUNCTION /path/to/file.cc:LINE[:COL]"
_FRAME_RE = re.compile(
    r"#(?P<depth>\d+)\s+0x[0-9a-fA-F]+\s+in\s+(?P<func>.+?)\s+(?P<file>/\S+?):(?P<line>\d+)(?::\d+)?"
)

# OSS-Fuzz / sanitizer crash classes, matched as substrings of ARVO's crash_type.
# Public (not _-prefixed): also used by answer_schema.py to tell the agent the vocabulary.
COARSE_TYPES = [
    "heap-buffer-overflow",
    "stack-buffer-overflow",
    "global-buffer-overflow",
    "stack-buffer-underflow",
    "heap-use-after-free",
    "use-after-free",
    "use-of-uninitialized-value",
    "index-out-of-bounds",
    "container-overflow",
    "null-dereference",
    "integer-overflow",
    "stack-overflow",
    "memory-leak",
    "double-free",
    "invalid-free",
    "bad-cast",
    "negative-size",
    "divide",
    "object-size",
]


@dataclass
class Frame:
    depth: int
    function: str
    filename: str  # normalized: the part after /src/<proj>/ when present
    line: int
    raw_file: str  # full path as it appeared in the report


def coarse_type(crash_type: str) -> str:
    c = (crash_type or "").lower()
    for k in COARSE_TYPES:
        if k in c:
            return k
    return "other"


def _normalize_path(path: str) -> str:
    if "/src/" in path:
        return path.split("/src/", 1)[1]
    return path.lstrip("/")


def parse_frames(crash_output: str, max_frames: int = 10) -> list[Frame]:
    """Extract the FIRST symbolized stack (the crash stack) as ordered frames.

    A sanitizer report can contain several stacks (crash, then "freed by",
    "allocated by", ...). Each restarts its depth counter, so we stop collecting
    once the depth stops increasing -- that isolates the crash stack.
    """
    frames: list[Frame] = []
    prev_depth = -1
    for m in _FRAME_RE.finditer(crash_output or ""):
        depth = int(m.group("depth"))
        if frames and depth <= prev_depth:
            break
        raw_file = m.group("file")
        frames.append(
            Frame(
                depth=depth,
                function=m.group("func").strip(),
                filename=_normalize_path(raw_file),
                line=int(m.group("line")),
                raw_file=raw_file,
            )
        )
        prev_depth = depth
        if len(frames) >= max_frames:
            break
    return frames


def build_ground_truth(row: dict, max_frames: int = 10) -> dict:
    """row: a dict with ARVO columns. Returns the ground-truth record."""
    frames = parse_frames(row.get("crash_output", ""), max_frames=max_frames)
    return {
        "localId": row["localId"],
        "project": row["project"],
        "crash_type": row["crash_type"],
        "crash_type_coarse": coarse_type(row["crash_type"]),
        "sanitizer": row["sanitizer"],
        "fix_commit": row["fix_commit"],
        "repo_addr": row["repo_addr"],
        "frames": [asdict(f) for f in frames],
        "usable": len(frames) > 0,
    }
