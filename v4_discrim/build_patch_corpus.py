#!/usr/bin/env python3
"""Item 2b: build the COMMENTLESS oracle-patch corpus for the discrimination set.

Chenxi's ARVO adaptation (docs/discrim-env-images/README.md §5) says the fix arm must be
the *commentless fix patch applied to the crash-base tree*, NOT the fix-commit tree taken
wholesale. That removes the giveaway patch-comment leak (the harfbuzz case): a diff hunk whose
added lines still carry "// fix out-of-bounds read" tells the agent the answer for free.

CRASH BASE = fix_commit^ (the fix commit's PARENT), matching Chenxi's pipeline, which bases its
images on `kenv-base-<bug>-parent-commit`. We deliberately do NOT use the harvested vuln_commit:
in ARVO it is generally NOT the parent of fix_commit and can be an entire release behind it
(e.g. gnutls: vuln..fix = 347 files vs fix^..fix = 4 files). Basing the crash arm on fix^ makes
the two arms differ by EXACTLY the fix, so arm identity can't leak through unrelated changes.
fix^ is by definition the last pre-fix revision, so the poc is presumed to still crash there.

So per bug in dataset.json this:
  1. materializes repo_addr with fix_commit AND its parent present (shallow depth-2 fetch of the
     fix sha; full clone only if the host rejects sha-fetch -- gitiles/cgit often do),
  2. diffs fix_commit^ -> fix_commit (the fix commit's own minimal patch),
  3. strips C comments from the diff's ADDED lines (lib/strip_comments.strip_diff),
  4. writes patches/<bug>.patch (build_shard.sh's $PATCHES/$BUG.patch convention),
  5. git-apply --check's the commentless patch onto the fix^ tree -- a clean apply is the
     guarantee the Dockerfile's fix-prep stage will succeed,
  6. records stats: files touched, added-line count before/after strip, binary files excluded,
     and whether the harvested vuln_commit happens to equal fix^ (informational only).

Sequential by design: one bug is fetched into a single scratch dir that is deleted before the
next, so peak disk stays at one repo even while item 1's sweep is running. Resumable: skips
bugs that already have a patch + a summary row (unless --redo).

  cd v4_discrim && python3 build_patch_corpus.py
  python3 build_patch_corpus.py --limit 3        # smoke test on the first 3 bugs
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "lib"))

from strip_comments import strip_diff


# Some flaky/slow git hosts have reliable mirrors with identical commit shas; fetch from those.
_MIRRORS = {
    "git.ffmpeg.org/ffmpeg.git": "https://github.com/FFmpeg/FFmpeg.git",
}


def _mirror(repo_addr: str) -> str:
    for needle, mirror in _MIRRORS.items():
        if needle in repo_addr:
            return mirror
    return repo_addr


def _run(args: list[str], cwd: Path | None = None, timeout: int = 1800) -> subprocess.CompletedProcess:
    # Capture bytes and decode with surrogateescape: git diffs of latin-1/binary-ish source carry
    # non-UTF-8 bytes that text=True would crash on; surrogateescape round-trips them losslessly.
    r = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout)
    return subprocess.CompletedProcess(
        r.args, r.returncode,
        r.stdout.decode("utf-8", "surrogateescape"),
        r.stderr.decode("utf-8", "surrogateescape"))


def _fetch_span(repo_addr: str, fix_head: str, fix_tip: str, scratch: Path) -> str:
    """Get fix_head's parent (the crash base) and fix_tip into scratch. For a single-commit fix
    fix_head == fix_tip and this is just fix + fix^. For a multi-commit fix (ARVO sometimes
    records two shas), fix_head is the FIRST fix commit and fix_tip the LAST, so the base is
    fix_head^ and the patch spans the whole fix. Returns 'shallow' or 'full'."""
    repo_addr = _mirror(repo_addr)
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], cwd=scratch)
    _run(["git", "remote", "add", "origin", repo_addr], cwd=scratch)
    # depth 2 of fix_head brings fix_head and its parent (the base tree); fetch fix_tip too.
    r1 = _run(["git", "fetch", "-q", "--depth", "2", "origin", fix_head], cwd=scratch, timeout=600)
    r2 = _run(["git", "fetch", "-q", "--depth", "1", "origin", fix_tip], cwd=scratch, timeout=600) if fix_tip != fix_head else r1
    have_base = _run(["git", "rev-parse", "-q", "--verify", f"{fix_head}^^{{commit}}"], cwd=scratch).returncode == 0
    have_tip = _run(["git", "cat-file", "-e", fix_tip], cwd=scratch).returncode == 0
    if r1.returncode == 0 and r2.returncode == 0 and have_base and have_tip:
        return "shallow"
    # Host rejected sha-fetch (gitiles/cgit) or the parent/tip fell outside the shallow slice.
    shutil.rmtree(scratch, ignore_errors=True)
    r = _run(["git", "clone", "-q", repo_addr, str(scratch)], timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"clone failed: {r.stderr.strip()[:200]}")
    for sha in {fix_head, fix_tip}:
        if _run(["git", "cat-file", "-e", sha], cwd=scratch).returncode != 0:
            _run(["git", "fetch", "-q", "origin", sha], cwd=scratch, timeout=600)
    return "full"


def _added_lines(diff_text: str) -> int:
    return sum(1 for ln in diff_text.splitlines() if ln.startswith("+") and not ln.startswith("+++"))


def _changed_files(scratch: Path, vuln: str, fix: str) -> tuple[list[str], list[str]]:
    """Return (text_paths, binary_paths) changed between vuln and fix. numstat marks a binary
    file with '-' in its add/del columns; binary blobs (fuzz seeds, test corpora) are noise for
    the source-level oracle patch AND can't be git-apply'd under a shallow fetch, so we drop
    them from the patch entirely."""
    r = _run(["git", "diff", "--numstat", "-z", vuln, fix], cwd=scratch)
    text, binary = [], []
    # -z numstat records are NUL-terminated: "adds\tdels\tpath\0" (binary: "-\t-\t\0path\0path\0").
    fields = r.stdout.split("\0")
    i = 0
    while i < len(fields):
        rec = fields[i]
        if not rec:
            i += 1
            continue
        parts = rec.split("\t")
        if len(parts) < 3:
            i += 1
            continue
        adds, dels, path = parts[0], parts[1], parts[2]
        if path == "" and i + 2 < len(fields):  # rename under -z: path spans next two fields
            path = fields[i + 2]
            i += 2
        (binary if adds == "-" and dels == "-" else text).append(path)
        i += 1
    return text, binary


def build_one(bug: str, meta: dict, out_dir: Path, scratch: Path) -> dict:
    repo, vuln, fix = meta["repo_addr"], meta.get("vuln_commit"), meta["fix_commit"]
    # ARVO sometimes records a multi-commit fix as whitespace-joined shas; first = head, last = tip.
    fix_shas = fix.split()
    fix_head, fix_tip = fix_shas[0], fix_shas[-1]
    row: dict = {"bug_id": bug, "project": meta.get("project"), "repo_addr": repo,
                 "fix_commit": fix, "fix_shas": fix_shas, "vuln_commit": vuln, "ok": False}
    row["fetch"] = _fetch_span(repo, fix_head, fix_tip, scratch)

    pr = _run(["git", "rev-parse", f"{fix_head}^"], cwd=scratch)
    if pr.returncode != 0:
        row["error"] = f"cannot resolve fix parent: {pr.stderr.strip()[:200]}"
        return row
    base = pr.stdout.strip()                    # crash base = (first) fix_commit^
    fix = fix_tip                               # diff target = last fix commit
    row["crash_base_commit"] = base
    row["vuln_equals_fix_parent"] = (base == vuln)  # informational: does ARVO's vuln == fix^?

    text_files, binary_files = _changed_files(scratch, base, fix)
    row["binary_files_excluded"] = len(binary_files)
    if not text_files:
        row["error"] = f"no text files changed (all {len(binary_files)} changed files binary)"
        return row
    dr = _run(["git", "diff", base, fix, "--", *text_files], cwd=scratch)
    if dr.returncode != 0:
        row["error"] = f"git diff failed: {dr.stderr.strip()[:200]}"
        return row
    raw = dr.stdout
    if not raw.strip():
        row["error"] = "empty diff (fix^ == fix tree?)"
        return row
    clean = strip_diff(raw)
    row["files_changed"] = raw.count("\ndiff --git ") + (1 if raw.startswith("diff --git ") else 0)
    row["added_lines_raw"] = _added_lines(raw)
    row["added_lines_clean_nonblank"] = sum(
        1 for ln in clean.splitlines() if ln.startswith("+") and not ln.startswith("+++") and ln.strip("+ ") != "")
    row["comment_lines_blanked"] = row["added_lines_raw"] - row["added_lines_clean_nonblank"]

    patch_path = out_dir / f"{bug}.patch"
    patch_path.write_text(clean, errors="surrogateescape")  # preserve non-UTF-8 source bytes
    row["patch"] = str(patch_path.relative_to(_HERE))

    # apply-check the commentless patch onto the exact fix^ tree (what fix-prep will do).
    co = _run(["git", "checkout", "-q", "-f", base], cwd=scratch)
    if co.returncode != 0:
        row["error"] = f"checkout fix^ failed: {co.stderr.strip()[:200]}"
        return row
    ac = _run(["git", "apply", "--check", str(patch_path)], cwd=scratch)
    row["apply_check_ok"] = (ac.returncode == 0)
    if ac.returncode != 0:
        row["apply_check_err"] = ac.stderr.strip()[:300]
    row["ok"] = row["apply_check_ok"]
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=_HERE / "data")
    ap.add_argument("--dataset", type=Path, default=_HERE / "dataset.json")
    ap.add_argument("--out", type=Path, default=_HERE / "discrim-env-images" / "patches")
    ap.add_argument("--scratch", type=Path, default=_HERE / "_patchwork")
    ap.add_argument("--summary", type=Path, default=_HERE / "discrim-env-images" / "patch_corpus.json")
    ap.add_argument("--limit", type=int, help="only the first N bugs (smoke test)")
    ap.add_argument("--redo", action="store_true", help="rebuild patches that already exist")
    a = ap.parse_args()

    a.out = a.out.resolve()  # patch paths are recorded relative to _HERE; a relative --out breaks that
    if not a.dataset.exists():
        raise SystemExit(f"{a.dataset}: not found (run build_dataset.py first)")
    bugs = json.loads(a.dataset.read_text())["bugs"]
    if a.limit is not None:
        bugs = bugs[: a.limit]
    a.out.mkdir(parents=True, exist_ok=True)

    prior = {}
    if a.summary.exists() and not a.redo:
        try:
            for r in json.loads(a.summary.read_text()).get("bugs", []):
                prior[str(r["bug_id"])] = r
        except (json.JSONDecodeError, KeyError):
            pass

    rows: list[dict] = []
    print(f"item 2b: commentless patch corpus for {len(bugs)} bugs -> {a.out}\n")
    for i, bug in enumerate(map(str, bugs), 1):
        patch_exists = (a.out / f"{bug}.patch").exists()
        if not a.redo and patch_exists and bug in prior:
            rows.append(prior[bug])
            print(f"[{i}/{len(bugs)}] {bug}: cached")
            continue
        meta_path = a.data / bug / "meta.json"
        if not meta_path.exists():
            rows.append({"bug_id": bug, "ok": False, "error": "no meta.json"})
            print(f"[{i}/{len(bugs)}] {bug}: WARN no meta.json")
            continue
        meta = json.loads(meta_path.read_text())
        t0 = time.time()
        try:
            row = build_one(bug, meta, a.out, a.scratch)
        except Exception as e:
            row = {"bug_id": bug, "project": meta.get("project"), "ok": False,
                   "error": f"{type(e).__name__}: {e}"}
        rows.append(row)
        dt = time.time() - t0
        tag = "OK " if row.get("ok") else "FAIL"
        if row.get("ok"):
            extra = (f"files={row.get('files_changed')} +lines={row.get('added_lines_raw')} "
                     f"blanked={row.get('comment_lines_blanked')} binexcl={row.get('binary_files_excluded')}")
        else:
            extra = row.get("error") or row.get("apply_check_err", "")
        print(f"[{i}/{len(bugs)}] {bug} ({row.get('project')}): {tag} [{row.get('fetch','-')}] {extra} ({dt:.0f}s)")

    shutil.rmtree(a.scratch, ignore_errors=True)

    ok = [r for r in rows if r.get("ok")]
    fails = [r for r in rows if not r.get("ok")]
    sizes = sorted(r.get("files_changed", 0) for r in ok)
    med = sizes[len(sizes) // 2] if sizes else 0
    big = [r for r in ok if r.get("files_changed", 0) > 20]  # still-large fix commits, worth a look
    vuln_ne_base = [r for r in ok if not r.get("vuln_equals_fix_parent")]
    summary = {
        "n": len(rows), "n_ok": len(ok), "n_failed": len(fails),
        "crash_base": "fix_commit^", "median_files_changed": med,
        "n_files_changed_gt20": len(big),
        "n_vuln_ne_fix_parent": len(vuln_ne_base),
        "params": {"dataset": str(a.dataset), "data": str(a.data)},
        "bugs": rows,
    }
    a.summary.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\ndone: {len(ok)}/{len(rows)} clean patches, {len(fails)} failed")
    print(f"  files-changed per patch: median {med}, {len(big)} with >20 files")
    print(f"  harvested vuln_commit != fix^ in {len(vuln_ne_base)}/{len(ok)} bugs (expected; fix^ is our base)")
    if fails:
        print("  failed bugs:", ", ".join(str(r["bug_id"]) for r in fails))
    print(f"  patches: {a.out}\n  summary: {a.summary}")


if __name__ == "__main__":
    main()
