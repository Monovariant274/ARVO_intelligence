#!/usr/bin/env python3
"""Item 1e: measure crash-vs-fix DISCRIMINABILITY from a v4 verdict manifest.

Discriminability = how cleanly the agent separates the crash arm (source at the vulnerable
commit, the poc crashes) from the fix arm (source at the fix commit, same poc runs clean).
The agent never knows which arm it is in, so a good separation is real signal that it read
the code, not the environment.

Reads batch_verdict.py's manifest.jsonl (one row per (bug, arm, sample) carrying `valid`,
`crashes`, `reward`) and reports, treating "says crash" as the positive call:

  * Pooled confusion over VALID rows:
      TPR  = P(says crash | crash arm)      -- crash-arm sensitivity
      FPR  = P(says crash | fix arm)        -- fix-arm false-alarm rate
      Youden J = TPR - FPR                  -- the headline discriminability, in [-1, 1]
      accuracy = (TP + TN) / valid
  * Mean v4 reward per arm (both incl. the -1 invalid penalty and over valid-only), and the
    reward separation crash-mean - fix-mean.
  * PAIRED per-bug discrimination (the difficulty-controlled view): per bug, p_crash =
    mean(says crash | crash arm) and p_fix = mean(says crash | fix arm); delta = p_crash -
    p_fix. Reports mean delta and the share of bugs with delta >0 / =0 / <0. Pairing cancels
    per-bug difficulty, so a positive mean delta is the strongest evidence of true reading.
  * Invalid (off-schema / no-verdict) rate per arm -- a sanity guard: high invalid rates make
    the pooled rates unreliable.

  python3 discriminability.py runs/v4disc/manifest.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def load_rows(manifest: Path) -> list[dict]:
    rows = []
    for line in manifest.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _rate(num: int, den: int) -> float | None:
    return (num / den) if den else None


def analyze(rows: list[dict]) -> dict:
    arms = {"crash": [r for r in rows if r.get("arm") == "crash"],
            "fix": [r for r in rows if r.get("arm") == "fix"]}

    per_arm = {}
    for arm, rs in arms.items():
        valid = [r for r in rs if r.get("valid")]
        says_crash = sum(1 for r in valid if r.get("crashes") is True)
        rewards_all = [r.get("reward", -1.0) for r in rs]
        rewards_valid = [r.get("reward", -1.0) for r in valid]
        per_arm[arm] = {
            "n": len(rs),
            "n_valid": len(valid),
            "invalid_rate": _rate(len(rs) - len(valid), len(rs)),
            "says_crash": says_crash,
            "says_crash_rate": _rate(says_crash, len(valid)),
            "mean_reward_all": statistics.fmean(rewards_all) if rewards_all else None,
            "mean_reward_valid": statistics.fmean(rewards_valid) if rewards_valid else None,
        }

    tpr = per_arm["crash"]["says_crash_rate"]           # P(says crash | crash arm)
    fpr = per_arm["fix"]["says_crash_rate"]             # P(says crash | fix arm)
    youden_j = (tpr - fpr) if (tpr is not None and fpr is not None) else None

    v_crash, v_fix = per_arm["crash"]["n_valid"], per_arm["fix"]["n_valid"]
    tp = per_arm["crash"]["says_crash"]
    tn = v_fix - per_arm["fix"]["says_crash"]
    accuracy = _rate(tp + tn, v_crash + v_fix)

    reward_sep = None
    if per_arm["crash"]["mean_reward_valid"] is not None and per_arm["fix"]["mean_reward_valid"] is not None:
        reward_sep = per_arm["crash"]["mean_reward_valid"] - per_arm["fix"]["mean_reward_valid"]

    # --- paired per-bug discrimination (difficulty-controlled) ---
    by_bug: dict[str, dict[str, list[dict]]] = defaultdict(lambda: {"crash": [], "fix": []})
    for r in rows:
        if r.get("arm") in ("crash", "fix"):
            by_bug[str(r.get("bug_id"))][r["arm"]].append(r)

    per_bug = []
    for bug_id, sides in sorted(by_bug.items()):
        cv = [r for r in sides["crash"] if r.get("valid")]
        fv = [r for r in sides["fix"] if r.get("valid")]
        p_crash = _rate(sum(1 for r in cv if r.get("crashes") is True), len(cv))
        p_fix = _rate(sum(1 for r in fv if r.get("crashes") is True), len(fv))
        delta = (p_crash - p_fix) if (p_crash is not None and p_fix is not None) else None
        per_bug.append({
            "bug_id": bug_id,
            "project": (sides["crash"] or sides["fix"] or [{}])[0].get("project"),
            "p_crash_says_crash": p_crash,
            "p_fix_says_crash": p_fix,
            "delta": delta,
            "n_valid_crash": len(cv),
            "n_valid_fix": len(fv),
        })

    paired = [b for b in per_bug if b["delta"] is not None]
    deltas = [b["delta"] for b in paired]
    paired_summary = {
        "n_bugs_paired": len(paired),
        "mean_delta": statistics.fmean(deltas) if deltas else None,
        "frac_delta_positive": _rate(sum(1 for d in deltas if d > 0), len(deltas)),
        "frac_delta_zero": _rate(sum(1 for d in deltas if d == 0), len(deltas)),
        "frac_delta_negative": _rate(sum(1 for d in deltas if d < 0), len(deltas)),
    }

    return {
        "n_rows": len(rows),
        "per_arm": per_arm,
        "pooled": {"TPR_crash_says_crash": tpr, "FPR_fix_says_crash": fpr,
                   "youden_j": youden_j, "accuracy": accuracy,
                   "mean_reward_separation_valid": reward_sep},
        "paired": paired_summary,
        "per_bug": sorted(per_bug, key=lambda b: (b["delta"] is None, -(b["delta"] or 0))),
    }


def _fmt(x: float | None, pct: bool = False) -> str:
    if x is None:
        return "  n/a"
    return f"{100 * x:5.1f}%" if pct else f"{x:+.3f}"


def print_report(res: dict) -> None:
    pa, pool, pair = res["per_arm"], res["pooled"], res["paired"]
    print(f"discriminability over {res['n_rows']} rows\n")
    print(f"{'arm':>6} | {'n':>4} {'valid':>6} {'invalid':>8} | {'says-crash':>11} | {'mean R(valid)':>13} {'mean R(all)':>11}")
    for arm in ("crash", "fix"):
        a = pa[arm]
        print(f"{arm:>6} | {a['n']:>4} {a['n_valid']:>6} {_fmt(a['invalid_rate'], True):>8} | "
              f"{_fmt(a['says_crash_rate'], True):>11} | {_fmt(a['mean_reward_valid']):>13} {_fmt(a['mean_reward_all']):>11}")
    print()
    print("POOLED (says-crash = positive call):")
    print(f"  TPR (crash arm says crash) : {_fmt(pool['TPR_crash_says_crash'], True)}")
    print(f"  FPR (fix arm says crash)   : {_fmt(pool['FPR_fix_says_crash'], True)}")
    print(f"  Youden J  = TPR - FPR      : {_fmt(pool['youden_j'])}   <- discriminability [-1..1]")
    print(f"  accuracy                   : {_fmt(pool['accuracy'], True)}")
    print(f"  mean-reward separation     : {_fmt(pool['mean_reward_separation_valid'])}  (crash - fix, valid)")
    print()
    print("PAIRED per-bug (difficulty-controlled):")
    print(f"  bugs with both arms valid  : {pair['n_bugs_paired']}")
    print(f"  mean delta (p_crash-p_fix) : {_fmt(pair['mean_delta'])}")
    print(f"  delta >0 / =0 / <0         : {_fmt(pair['frac_delta_positive'], True)} / "
          f"{_fmt(pair['frac_delta_zero'], True)} / {_fmt(pair['frac_delta_negative'], True)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", type=Path, help="manifest.jsonl from batch_verdict.py")
    ap.add_argument("--out", type=Path, help="write full JSON here (default: manifest dir/discriminability.json)")
    a = ap.parse_args()
    if not a.manifest.exists():
        raise SystemExit(f"{a.manifest}: not found")
    rows = load_rows(a.manifest)
    if not rows:
        raise SystemExit(f"{a.manifest}: no rows")
    res = analyze(rows)
    print_report(res)
    out = a.out or a.manifest.resolve().parent / "discriminability.json"
    out.write_text(json.dumps(res, indent=2) + "\n")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
