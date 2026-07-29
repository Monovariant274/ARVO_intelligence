#!/usr/bin/env python3
"""ARVO Phase-2 dataset harvester.

Two subcommands:

  stats  -- DB-only. Parse every crash report and report how many bugs yield a
            usable (symbolized) crash location. No Docker, runs in seconds. This
            is the "is the dataset viable?" answer.

  pull   -- For each selected bug: pull its ARVO vuln image, extract the PoC
            input and the vulnerable source commit, parse the crash report into
            ground truth, and write one tuple folder. Deletes the (multi-GB)
            image afterwards to reclaim disk. Resumable.

Examples:
  python3 harvest.py stats
  python3 harvest.py pull --sanitizer asan --limit 5 --out ./data
  python3 harvest.py pull --sanitizer asan --limit 200 --source tar --out ./data
"""

from __future__ import annotations

import argparse
import collections
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from crash_parser import build_ground_truth, coarse_type

DEFAULT_DB = "/home/jinghezhang/ARVO/arvo.db"
_COLS = ["localId", "project", "crash_type", "sanitizer", "crash_output", "fix_commit", "repo_addr", "language"]


def _rows(db: str, sanitizer: str | None, project: str | None, limit: int | None):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    q = f"SELECT {','.join(_COLS)} FROM arvo"
    where, args = [], []
    if sanitizer:
        where.append("sanitizer=?"); args.append(sanitizer)
    if project:
        where.append("project=?"); args.append(project)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY localId"
    if limit:
        q += f" LIMIT {int(limit)}"
    return [dict(r) for r in con.execute(q, args)]


# --------------------------------------------------------------------------- stats
def cmd_stats(a):
    rows = _rows(a.db, a.sanitizer, a.project, None)
    usable = 0
    by_type = collections.Counter()
    frame_hist = collections.Counter()
    for r in rows:
        gt = build_ground_truth(r, max_frames=a.max_frames)
        if gt["usable"]:
            usable += 1
            by_type[gt["crash_type_coarse"]] += 1
            n = len(gt["frames"])
            frame_hist["1" if n == 1 else "2-4" if n < 5 else "5-10"] += 1
    N = len(rows)
    print(f"bugs considered:      {N}")
    print(f"usable (has frames):  {usable}  ({100*usable/max(N,1):.1f}%)")
    print(f"frame-count buckets:  {dict(frame_hist)}")
    print("\nusable bugs by coarse crash type:")
    for k, v in by_type.most_common():
        print(f"  {v:5}  {k}")


# --------------------------------------------------------------------------- pull
def _docker(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _docker_stream(args: list[str], timeout: int = 1800) -> int:
    """Run docker with its output shown live (used for `pull` so downloads are visible)."""
    return subprocess.run(["docker", *args], timeout=timeout).returncode


def _image_source(image: str, project: str) -> tuple[str, str] | None:
    """Return (src_dir, commit) of the vulnerable checkout inside the image."""
    script = (
        'for d in /src/*/; do '
        'if [ -d "$d/.git" ]; then echo "$d $(git -C "$d" rev-parse HEAD 2>/dev/null)"; fi; '
        "done"
    )
    p = _docker(["run", "--rm", "--entrypoint", "bash", image, "-lc", script], timeout=120)
    repos = [ln.split() for ln in p.stdout.splitlines() if ln.strip()]
    if not repos:
        return None
    for d, commit in repos:  # prefer the dir named after the project
        if d.rstrip("/").split("/")[-1] == project:
            return d.rstrip("/"), commit
    d, commit = repos[0]
    return d.rstrip("/"), commit


def _harvest_one(r: dict, out: Path, source_mode: str, keep_images: bool, require_frames: bool,
                 idx: int, total: int) -> dict:
    lid = r["localId"]
    dest = out / str(lid)
    tag = f"[{idx}/{total}] {lid} ({r['project']}/{r['sanitizer']})"

    if (dest / "ground_truth.json").exists():
        print(f"{tag} skipped (already harvested)", flush=True)
        return {"localId": lid, "status": "skipped(exists)"}

    gt = build_ground_truth(r, max_frames=10)
    if require_frames and not gt["usable"]:
        print(f"{tag} skipped (no symbolized frames)", flush=True)
        return {"localId": lid, "status": "skipped(no-frames)"}

    image = f"n132/arvo:{lid}-vul"
    t0 = time.time()
    print(f"{tag} pulling {image} (multi-GB, may take a few min)...", flush=True)
    if _docker_stream(["pull", image]) != 0:
        print(f"{tag} FAILED to pull", flush=True)
        return {"localId": lid, "status": "fail(pull)"}
    print(f"{tag} pulled in {time.time()-t0:.0f}s, extracting PoC + source...", flush=True)

    dest.mkdir(parents=True, exist_ok=True)
    (dest / "ground_truth.json").write_text(json.dumps(gt, indent=2))

    create = _docker(["create", image], timeout=120)
    cid = create.stdout.strip()
    src_info = None
    poc_bytes = None
    try:
        cp = _docker(["cp", f"{cid}:/tmp/poc", str(dest / "poc")], timeout=120)
        if cp.returncode == 0 and (dest / "poc").exists():
            poc_bytes = (dest / "poc").stat().st_size
        src_info = _image_source(image, r["project"])
        if source_mode == "tar" and src_info:
            src_dir, _ = src_info
            with open(dest / "source.tar", "wb") as fh:
                subprocess.run(["docker", "cp", f"{cid}:{src_dir}", "-"], stdout=fh, timeout=1800)
    finally:
        _docker(["rm", "-f", cid], timeout=120)

    meta = {
        "localId": lid,
        "project": r["project"],
        "sanitizer": r["sanitizer"],
        "crash_type": r["crash_type"],
        "poc_bytes": poc_bytes,
        "repo_addr": r["repo_addr"],
        "vuln_commit": (src_info[1] if src_info else None),
        "vuln_src_dir_in_image": (src_info[0] if src_info else None),
        "fix_commit": r["fix_commit"],
        "source_mode": source_mode,
    }
    (dest / "meta.json").write_text(json.dumps(meta, indent=2))

    if not keep_images:
        _docker(["rmi", "-f", image], timeout=300)

    elapsed = time.time() - t0
    status = "ok" if poc_bytes else "ok(no-poc)"
    print(f"{tag} {status}  poc={poc_bytes}B frames={len(gt['frames'])}  ({elapsed:.0f}s)", flush=True)
    return {"localId": lid, "status": status, "poc_bytes": poc_bytes,
            "frames": len(gt["frames"]), "seconds": round(elapsed)}


def cmd_pull(a):
    if not shutil.which("docker"):
        sys.exit("docker not found on PATH")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = _rows(a.db, a.sanitizer, a.project, a.limit)
    total = len(rows)
    manifest = out / "manifest.jsonl"
    print(f"harvesting {total} bugs -> {out}\n", flush=True)

    counts = collections.Counter()
    run_start = time.time()
    with open(manifest, "a") as mf:  # append: keeps history across resumed runs
        for i, r in enumerate(rows, 1):
            res = _harvest_one(r, out, a.source, a.keep_images, a.require_frames, i, total)
            mf.write(json.dumps(res) + "\n")
            mf.flush()  # so `wc -l manifest.jsonl` reflects live progress
            counts[res["status"].split("(")[0]] += 1
            done = i
            mins = (time.time() - run_start) / 60
            print(f"    progress: {done}/{total} done  ({dict(counts)})  elapsed {mins:.1f}m\n", flush=True)

    print(f"finished {total} bugs. totals: {dict(counts)}\nmanifest: {manifest}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stats", help="DB-only viability report (no docker)")
    s.add_argument("--sanitizer", choices=["asan", "msan", "ubsan"])
    s.add_argument("--project")
    s.add_argument("--max-frames", type=int, default=10)
    s.set_defaults(func=cmd_stats)

    p = sub.add_parser("pull", help="harvest tuples via docker")
    p.add_argument("--sanitizer", choices=["asan", "msan", "ubsan"])
    p.add_argument("--project")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--out", default="./data")
    p.add_argument("--source", choices=["ref", "tar"], default="ref",
                   help="ref = record repo+commit only (cheap); tar = also save source tree")
    p.add_argument("--require-frames", action="store_true",
                   help="skip bugs with no symbolized crash location")
    p.add_argument("--keep-images", action="store_true", help="do not delete images after extract")
    p.set_defaults(func=cmd_pull)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
