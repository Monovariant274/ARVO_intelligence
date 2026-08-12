"""v4 discrimination reward: crash/no-crash verdict + weighted-subsequence stack credit.

The dataset carries two rows per bug (``extra_info["arm"]``): the parent-commit environment where
the reproducer crashes (``"crash"``) and the fix-commit environment where it does not (``"fix"``).

    fix arm:    crashes -> 0.0, no-crash -> 1.0
    crash arm:  no-crash -> 0.0, crashes -> S in [0, 1]

``-1`` (invalid) is decided by the caller (``exec_rl.rl.rewards``) for non-Submitted trajectories
and for context_output that ``parse_crash_verdict`` rejects.

The crash-arm stack score replaces reward_v2's IDF/superlinear chain: sanitizer/reporting and
generic entry/dispatch frames are stripped from BOTH gold and prediction (reward-neutral either
way), then the maximum-weight common subsequence (order-preserving, skips allowed on both sides)
is taken with gold depth d of D carrying linear weight D - d — the most-recent (crash-site) frame
counts most, and the credit of a matched crash-site frame is refined by line proximity. Normalized
against the stripped gold, so a prompt-perfect prediction scores 1.0.

    S = 0.80 * stack + 0.15 * bugClass + 0.05 * alloc/free

with weights of unavailable gold components renormalized away, exactly like reward_v2.
"""

from __future__ import annotations

import json
from math import exp

from pydantic import BaseModel

from exec_rl.reward import _coerce_ground_truth_payload, _normalize_function
from exec_rl.reward_v2.schema import PredictedFrame
from exec_rl.reward_v2.similarity import function_match

_STRIP_EXACT = {
    "die", "die_addr", "panic", "oops_enter", "oops_exit", "report_bug", "dump_stack",
    "dump_stack_lvl", "show_stack", "show_regs", "print_report", "add_taint", "bust_spinlocks",
    "check_memory_region", "ret_from_fork", "x64_sys_call",
}
_STRIP_PREFIXES = (
    "kasan_", "__kasan_", "kmsan_", "__kmsan_", "kcsan_", "__kcsan_", "ubsan_", "__ubsan_",
    "kfence_", "__kfence_", "__asan_", "__msan_", "__tsan_", "warn_slowpath_", "__warn",
    "__report_", "do_syscall_", "entry_syscall_", "entry_sysenter_", "entry_int80_",
    "ret_from_fork_", "syscall_enter_from_", "syscall_exit_to_", "do_int80_",
)


def is_stripped_frame(function: str) -> bool:
    """True for sanitizer/reporting and generic entry/dispatch frames: reward-neutral on both the
    gold and the prediction side (they can neither earn nor cost anything)."""
    name = _normalize_function(function).casefold()
    return name in _STRIP_EXACT or name.startswith(_STRIP_PREFIXES)


class VerdictRewardConfig(BaseModel):
    """Frozen knobs of the v4 reward; serialized into every result for provenance."""

    model_config = {"frozen": True}

    w_stack: float = 0.80
    w_class: float = 0.15
    w_allocfree: float = 0.05
    line_share: float = 0.2
    """Share of the matched crash-site frame's credit paid by line proximity."""
    line_temperature: float = 8.0
    max_pred_frames: int = 32


class CrashVerdict(BaseModel):
    """Parsed v4 ``/context_output.json``: the crash/no-crash verdict plus the predicted chains."""

    crashes: bool
    bugClass: str
    reason: str
    crashFrames: list[PredictedFrame] = []
    allocFrames: list[PredictedFrame] = []
    freeFrames: list[PredictedFrame] = []


class VerdictRewardResult(BaseModel):
    reward: float
    components: dict[str, float]
    weights: dict[str, float]
    config: VerdictRewardConfig


def parse_crash_verdict(context_output: str) -> CrashVerdict | None:
    """Parse raw v4 ``/context_output.json`` text; ``None`` means off-schema (=> reward -1).

    Schema enforcement: ``crashes`` must be a JSON bool, ``bugClass``/``reason`` non-empty strings;
    ``crashes: true`` requires at least one parsable frame in ``crashFrames`` (``frames`` accepted
    as an alias); ``crashes: false`` requires an empty ``crashFrames`` and ``bugClass: "none"``.
    Individually malformed frames are dropped, not fatal.
    """
    if not context_output or not context_output.strip():
        return None
    try:
        raw = json.loads(context_output)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("crashes"), bool):
        return None
    bug_class, reason = raw.get("bugClass"), raw.get("reason")
    if not isinstance(bug_class, str) or not bug_class.strip() or not isinstance(reason, str) or not reason.strip():
        return None
    raw_frames = raw.get("crashFrames") if raw.get("crashFrames") is not None else raw.get("frames")
    if raw_frames is None:
        raw_frames = []
    if not isinstance(raw_frames, list):
        return None
    frames = [f for f in (PredictedFrame.from_loose(x) for x in raw_frames) if f is not None]
    if raw["crashes"] and not frames:
        return None
    if not raw["crashes"] and (raw_frames or bug_class.strip().casefold() != "none"):
        return None
    return CrashVerdict(
        crashes=raw["crashes"],
        bugClass=bug_class.strip(),
        reason=reason.strip(),
        crashFrames=frames,
        allocFrames=[f for f in (PredictedFrame.from_loose(x) for x in raw.get("allocFrames") or []) if f],
        freeFrames=[f for f in (PredictedFrame.from_loose(x) for x in raw.get("freeFrames") or []) if f],
    )


def score_crash_verdict(
    ground_truth: dict,
    verdict: CrashVerdict,
    *,
    arm: str,
    config: VerdictRewardConfig | None = None,
) -> VerdictRewardResult:
    cfg = config or VerdictRewardConfig()
    if arm == "fix":
        reward = 0.0 if verdict.crashes else 1.0
        return VerdictRewardResult(reward=reward, components={"verdict": reward}, weights={"verdict": 1.0}, config=cfg)
    if arm != "crash":
        raise ValueError(f"unknown arm: {arm!r} (expected 'crash' or 'fix')")
    if not verdict.crashes:
        return VerdictRewardResult(reward=0.0, components={"verdict": 0.0}, weights={"verdict": 1.0}, config=cfg)

    gold_class = ground_truth.get("bugClass") if isinstance(ground_truth, dict) else None
    payload = _coerce_ground_truth_payload(ground_truth)
    gold = [
        {"filename": f.filename, "function": f.function, "line": f.line}
        for f in sorted(payload.frames, key=lambda f: f.depth)
        if not is_stripped_frame(f.function)
    ]
    pred = [f for f in verdict.crashFrames[: cfg.max_pred_frames] if not is_stripped_frame(f.function)]

    components: dict[str, float] = {}
    if gold:
        components["stack"] = _weighted_subsequence(pred, gold, cfg)
    if gold_class is not None:
        components["class"] = float(verdict.bugClass.casefold() == str(gold_class).casefold())
    gold_alloc = _gold_frames(ground_truth, "allocFrames")
    gold_free = _gold_frames(ground_truth, "freeFrames")
    if gold_alloc or gold_free:
        subs = []
        for gold_side, pred_side in ((gold_alloc, verdict.allocFrames), (gold_free, verdict.freeFrames)):
            if gold_side:
                subs.append(_weighted_subsequence([f for f in pred_side if not is_stripped_frame(f.function)],
                                                  gold_side, cfg))
        components["allocfree"] = sum(subs) / len(subs)

    if not components:
        return VerdictRewardResult(reward=0.0, components={}, weights={}, config=cfg)
    base = {"stack": cfg.w_stack, "class": cfg.w_class, "allocfree": cfg.w_allocfree}
    total = sum(base[k] for k in components)
    weights = {k: base[k] / total for k in components}
    reward = sum(weights[k] * components[k] for k in components)
    return VerdictRewardResult(
        reward=min(1.0, max(0.0, reward)), components=components, weights=weights, config=cfg
    )


def _gold_frames(ground_truth: object, key: str) -> list[dict]:
    if not isinstance(ground_truth, dict):
        return []
    frames = [PredictedFrame.from_loose(x) for x in ground_truth.get(key) or []]
    return [
        {"filename": f.filename, "function": f.function, "line": f.line}
        for f in frames
        if f is not None and not is_stripped_frame(f.function)
    ]


def _weighted_subsequence(pred: list[PredictedFrame], gold: list[dict], cfg: VerdictRewardConfig) -> float:
    """Maximum-weight common subsequence of pred vs gold in [0, 1].

    Order-preserving with skips allowed on both sides; matching is on normalized function name
    alone (the v1 exact-file gate dropped semantically right predictions). Gold depth d of D earns
    weight D - d; the crash-site (depth 0) credit is scaled by line proximity when both sides carry
    a line, by ``1 - line_share`` when only gold does, and left whole when gold has no line.
    """
    if not gold:
        return 0.0
    depth_weight = [float(len(gold) - d) for d in range(len(gold))]
    total = sum(depth_weight)
    if not pred:
        return 0.0

    def credit(i: int, j: int) -> float:
        value = depth_weight[j]
        if j == 0 and gold[0]["line"] is not None:
            proximity = (
                exp(-abs(pred[i].line - gold[0]["line"]) / cfg.line_temperature)
                if pred[i].line is not None
                else 0.0
            )
            value *= (1 - cfg.line_share) + cfg.line_share * proximity
        return value

    rows = len(pred) + 1
    cols = len(gold) + 1
    m = [[0.0] * cols for _ in range(rows)]
    for i in range(1, rows):
        for j in range(1, cols):
            best = max(m[i - 1][j], m[i][j - 1])
            if function_match(pred[i - 1].function, gold[j - 1]["function"]):
                best = max(best, m[i - 1][j - 1] + credit(i - 1, j - 1))
            m[i][j] = best
    return m[-1][-1] / total
