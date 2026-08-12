#!/usr/bin/env python3
"""Diagnostic for item 2b's crash-base risk: how many commits separate ARVO's verified
vuln_commit (crashes) from its fix_commit (clean)?

Our items-2/3 crash base is fix_commit^, which is only safe if fix^ still crashes. That holds
when vuln_commit == fix^ (distance 1). When the fix is the TAIL of a multi-commit series
(distance > 1), fix^ may already be patched and our base assumption breaks. This measures the
distance distribution across dataset.json so we can decide whether a crash-at-fix^ verification
gate (or a distance==1 filter) is needed.

Per bug: shallow-fetch fix_commit deepening until vuln_commit is reachable (cap --max-depth),
then `git rev-list --count vuln..fix`. Sequential, one scratch repo deleted per bug.

  cd v4_discrim && python3 measure_fix_distance.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _run(a, cwd=None, timeout=600):
    return subprocess.run(a, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def distance(repo: str, vuln: str, fix: str, scratch: Path, depths: list[int]) -> dict:
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], cwd=scratch)
    _run(["git", "remote", "add", "origin", repo], cwd=scratch)
    method = "shallow"
    for d in depths:
        r = _run(["git", "fetch", "-q", "--depth", str(d), "origin", fix], cwd=scratch)
        if r.returncode != 0:  # host rejects sha-fetch -> one full clone reaches everything
            shutil.rmtree(scratch, ignore_errors=True)
            if _run(["git", "clone", "-q", repo, str(scratch)]).returncode != 0:
                return {"error": "clone failed"}
            _run(["git", "fetch", "-q", "origin", fix], cwd=scratch)
            method = "full"
            break
        if _run(["git", "cat-file", "-e", vuln], cwd=scratch).returncode == 0:
            method = f"shallow<= {d}"
            break
    have_vuln = _run(["git", "cat-file", "-e", vuln], cwd=scratch).returncode == 0
    if not have_vuln:
        return {"method": method, "vuln_reachable": False, "distance": None}
    anc = _run(["git", "merge-base", "--is-ancestor", vuln, fix], cwd=scratch).returncode == 0
    cnt = _run(["git", "rev-list", "--count", f"{vuln}..{fix}"], cwd=scratch)
    return {"method": method, "vuln_reachable": True, "vuln_is_ancestor": anc,
            "distance": int(cnt.stdout.strip()) if cnt.returncode == 0 else None}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=_HERE / "data")
    ap.add_argument("--dataset", type=Path, default=_HERE / "dataset.json")
    ap.add_argument("--scratch", type=Path, default=_HERE / "_distcheck")
    ap.add_argument("--out", type=Path, default=_HERE / "discrim-env-images" / "fix_distance.json")
    ap.add_argument("--depths", type=int, nargs="+", default=[5, 50, 400, 3000])
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()

    bugs = json.loads(a.dataset.read_text())["bugs"]
    if a.limit:
        bugs = bugs[: a.limit]
    rows = []
    for i, bug in enumerate(map(str, bugs), 1):
        meta = json.loads((a.data / bug / "meta.json").read_text())
        try:
            d = distance(meta["repo_addr"], meta["vuln_commit"], meta["fix_commit"], a.scratch, a.depths)
        except Exception as e:
            d = {"error": f"{type(e).__name__}: {e}"}
        d.update({"bug_id": bug, "project": meta.get("project")})
        rows.append(d)
        print(f"[{i}/{len(bugs)}] {bug} ({meta.get('project')}): dist={d.get('distance')} "
              f"ancestor={d.get('vuln_is_ancestor')} [{d.get('method', d.get('error'))}]")
    shutil.rmtree(a.scratch, ignore_errors=True)

    dists = [r["distance"] for r in rows if r.get("distance") is not None]
    hist = Counter(min(x, 6) if x is not None else None for x in dists)  # bucket 6+ together
    n1 = sum(1 for x in dists if x == 1)
    a.out.write_text(json.dumps({"n": len(rows), "n_distance_measured": len(dists),
                                 "n_distance_eq_1": n1, "bugs": rows}, indent=2) + "\n")
    print(f"\ndistance measured for {len(dists)}/{len(rows)} bugs")
    print(f"  distance==1 (fix^ == vuln, our base is safe): {n1}")
    print("  histogram (dist -> #bugs, 6=6+):",
          {k: hist[k] for k in sorted(x for x in hist if x is not None)})
    unreach = [r["bug_id"] for r in rows if r.get("distance") is None]
    if unreach:
        print("  distance UNKNOWN (vuln not reachable / error):", ", ".join(unreach))
    print(f"  wrote {a.out}")


if __name__ == "__main__":
    main()
