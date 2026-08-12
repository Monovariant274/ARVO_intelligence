#!/usr/bin/env python3
"""Item 2e: ARVO discrimination-env build worker -- builds BOTH arms for a shard of bugs and
(optionally) pushes them to the sysintel-env registry.

This is the ARVO analogue of Chenxi's build_shard.sh (README §7). The one structural deviation
(README §5.1): the kernel worker `docker pull`s a per-bug source image (kenv-base-<bug>-
parent-commit) that already sits at the crash base. ARVO has no such image -- the ARVO OSS-Fuzz
images sit at vuln_commit (not our fix^ base) and carry build-artifact leaks. So this worker
MATERIALIZES the crash-base tree itself, into the docker build context:

  per bug (manifest = discrim-env-images/patch_corpus.json, one row each):
    1. fetch crash_base_commit (= fix_commit^) from repo_addr into a scratch git dir
       (shallow depth-1 sha fetch; full clone only if the host rejects sha-fetch),
    2. `git archive <base> | tar -x` the tree into ctx/tree  -- a clean, .git-free snapshot,
    3. EPOCH = `git show -s --format=%ct <base>` (fix^ committer time -- the ARG the Dockerfile
       uses instead of the kernel's `stat -c %Y COPYING`),
    4. drop ctx/{bug}.patch (the commentless oracle patch) and ctx/Dockerfile (= Dockerfile.arvo),
    5. `docker build --target vul-commit` and `--target fix-commit`, both from the SAME ctx/tree,
       passing --build-arg BUG / TREE=/src/<project> / EPOCH,
    6. (--push) push sysintel-user-arvo-<bug>-{vul,fix}:latest, then rmi to reclaim space.

Idempotent under --push (skips bugs whose BOTH arms already exist in the registry). Without
--push it is a pure local build check -- safe to run with no GCP access (verifies the whole
tree-materialize -> build path end to end, which is what item 2f acceptance needs).

  cd v4_discrim && python3 build_shard_arvo.py --limit 1            # local build, no push
  python3 build_shard_arvo.py --shard shard0.txt --push            # fleet box, build + push
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_IMGDIR = _HERE / "discrim-env-images"
_DST_REG = "us-docker.pkg.dev/triangulate-396717/sysintel-env"

# Flaky hosts with identical-sha mirrors (see build_patch_corpus.py / fetch_source.py).
_MIRRORS = {"git.ffmpeg.org/ffmpeg.git": "https://github.com/FFmpeg/FFmpeg.git"}

# Dockerfile.arvo build target  ->  registry image-name arm suffix.
_ARMS = [("vul-commit", "vul"), ("fix-commit", "fix")]


def _mirror(repo: str) -> str:
    for needle, m in _MIRRORS.items():
        if needle in repo:
            return m
    return repo


def _run(args, cwd=None, timeout=1800, stream_to=None):
    if stream_to is not None:  # docker build/push: append raw output to a log, don't buffer in RAM
        with open(stream_to, "ab") as fh:
            return subprocess.run(args, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout)
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _fetch_base(repo: str, base: str, scratch: Path) -> str:
    """Get exactly the crash-base commit into scratch. Returns 'shallow' or 'full'."""
    repo = _mirror(repo)
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], cwd=scratch)
    _run(["git", "remote", "add", "origin", repo], cwd=scratch)
    r = _run(["git", "fetch", "-q", "--depth", "1", "origin", base], cwd=scratch, timeout=600)
    if r.returncode == 0 and _run(["git", "cat-file", "-e", base], cwd=scratch).returncode == 0:
        return "shallow"
    shutil.rmtree(scratch, ignore_errors=True)  # host rejected sha-fetch -> full clone
    if _run(["git", "clone", "-q", repo, str(scratch)], timeout=1800).returncode != 0:
        raise RuntimeError("clone failed")
    if _run(["git", "cat-file", "-e", base], cwd=scratch).returncode != 0:
        _run(["git", "fetch", "-q", "origin", base], cwd=scratch, timeout=600)
    return "full"


def _materialize_tree(scratch: Path, base: str, tree_dir: Path):
    """Extract base's tree (no .git) into tree_dir via `git archive | tar -x`."""
    tree_dir.mkdir(parents=True, exist_ok=True)
    ar = subprocess.Popen(["git", "archive", "--format=tar", base], cwd=scratch, stdout=subprocess.PIPE)
    tar = subprocess.Popen(["tar", "-x", "-C", str(tree_dir)], stdin=ar.stdout)
    ar.stdout.close()
    tar.communicate()
    if tar.returncode != 0 or ar.wait() != 0:
        raise RuntimeError("git archive | tar extract failed")
    if not any(tree_dir.iterdir()):
        raise RuntimeError("materialized tree is empty")


def _epoch(scratch: Path, base: str) -> str:
    r = _run(["git", "show", "-s", "--format=%ct", base], cwd=scratch)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError("cannot read committer epoch")
    return r.stdout.strip()


def _image_exists(name: str) -> bool:
    return _run(["gcloud", "artifacts", "docker", "images", "describe", name], timeout=120).returncode == 0


def build_bug(row: dict, *, push: bool, keep: bool, log: Path, scratch: Path) -> dict:
    bug, project, repo = str(row["bug_id"]), row["project"], row["repo_addr"]
    base = row["crash_base_commit"]
    tree_path = f"/src/{project}"
    # Patch path comes from the manifest row (points at whichever patches dir built it), so the
    # adjacent corpus (patches_adjacent/) works without hardcoding the default patches/ dir.
    patch = (_HERE / row["patch"]) if row.get("patch") else (_IMGDIR / "patches" / f"{bug}.patch")
    dockerfile = _IMGDIR / "Dockerfile.arvo"
    names = {arm: f"{_DST_REG}/sysintel-user-arvo-{bug}-{arm}:latest" for _, arm in _ARMS}
    res = {"bug_id": bug, "project": project, "ok": False, "pushed": False}

    if not patch.exists():
        res["error"] = "no patch"
        return res
    if push and all(_image_exists(n) for n in names.values()):
        res.update(ok=True, pushed=True, skipped="already in registry")
        return res

    ctx = Path(tempfile.mkdtemp(prefix=f"arvobuild-{bug}-"))
    try:
        res["fetch"] = _fetch_base(repo, base, scratch)
        _materialize_tree(scratch, base, ctx / "tree")
        res["epoch"] = _epoch(scratch, base)
        shutil.copy(dockerfile, ctx / "Dockerfile")
        shutil.copy(patch, ctx / f"{bug}.patch")
        common = ["--build-arg", f"BUG={bug}", "--build-arg", f"TREE={tree_path}",
                  "--build-arg", f"EPOCH={res['epoch']}"]
        for target, arm in _ARMS:
            img = names[arm]
            b = _run(["docker", "build", *common, "--target", target, "-t", img, str(ctx)],
                     timeout=3600, stream_to=log)
            if b.returncode != 0:
                res["error"] = f"{arm} build failed (see {log})"
                return res
            if push:
                if _run(["docker", "push", img], timeout=1800, stream_to=log).returncode != 0:
                    res["error"] = f"{arm} push failed (see {log})"
                    return res
                if not keep:  # --keep leaves images on disk so item 3 reuses them without re-pull
                    _run(["docker", "rmi", img], timeout=120)
        res["ok"] = True
        res["pushed"] = push
        res["images"] = names
        return res
    finally:
        shutil.rmtree(ctx, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)
        if push:  # drop prep-stage build cache regardless of --keep (only the final images are kept)
            _run(["docker", "builder", "prune", "-f"], timeout=300)


def _load_manifest(summary: Path) -> dict[str, dict]:
    rows = json.loads(summary.read_text()).get("bugs", [])
    out = {}
    for r in rows:
        if r.get("ok") and r.get("crash_base_commit") and r.get("repo_addr"):
            out[str(r["bug_id"])] = r
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, default=_IMGDIR / "patch_corpus.json",
                    help="per-bug build inputs (build_patch_corpus.py output)")
    ap.add_argument("--shard", type=Path, help="file of bug ids (one per line); default = all in manifest")
    ap.add_argument("--limit", type=int, help="only the first N bugs (local smoke test)")
    ap.add_argument("--push", action="store_true", help="push both arms to the registry (fleet mode)")
    ap.add_argument("--keep", action="store_true", help="keep built images on disk (skip rmi) so item 3 can reuse them")
    ap.add_argument("--log", type=Path, default=Path("/tmp/discrim_build_arvo.log"))
    ap.add_argument("--scratch", type=Path, default=_HERE / "_buildwork")
    ap.add_argument("--summary-out", type=Path, default=_IMGDIR / "build_report.json")
    a = ap.parse_args()

    manifest = _load_manifest(a.manifest)
    if a.shard:
        ids = [ln.strip() for ln in a.shard.read_text().splitlines() if ln.strip()]
    else:
        ids = list(manifest)
    if a.limit is not None:
        ids = ids[: a.limit]

    if a.push and shutil.which("gcloud"):
        _run(["gcloud", "auth", "configure-docker", "us-docker.pkg.dev", "-q"], timeout=120)

    a.log.write_text("")  # fresh build log
    print(f"item 2e: building {len(ids)} bugs x2 arms  (push={a.push})  log={a.log}\n")
    results = []
    for i, bug in enumerate(ids, 1):
        row = manifest.get(bug)
        if row is None:
            results.append({"bug_id": bug, "ok": False, "error": "not in manifest (or patch failed)"})
            print(f"[{i}/{len(ids)}] {bug}: SKIP not in manifest")
            continue
        t0 = time.time()
        try:
            r = build_bug(row, push=a.push, keep=a.keep, log=a.log, scratch=a.scratch)
        except Exception as e:
            r = {"bug_id": bug, "project": row.get("project"), "ok": False,
                 "error": f"{type(e).__name__}: {e}"}
        results.append(r)
        tag = "OK  " if r.get("ok") else "FAIL"
        note = r.get("skipped") or r.get("error") or f"[{r.get('fetch','-')}] epoch={r.get('epoch')}"
        print(f"[{i}/{len(ids)}] {bug} ({r.get('project')}): {tag} {note} ({time.time()-t0:.0f}s)", flush=True)

    ok = [r for r in results if r.get("ok")]
    a.summary_out.write_text(json.dumps(
        {"n": len(results), "n_ok": len(ok), "n_failed": len(results) - len(ok),
         "pushed": a.push, "bugs": results}, indent=2) + "\n")
    print(f"\ndone: {len(ok)}/{len(results)} bugs built" + (" + pushed" if a.push else " (local, no push)"))
    fails = [r for r in results if not r.get("ok")]
    if fails:
        print("  failed:", ", ".join(f"{r['bug_id']}({r.get('error','?')[:40]})" for r in fails))
    print(f"  report: {a.summary_out}")


if __name__ == "__main__":
    main()
