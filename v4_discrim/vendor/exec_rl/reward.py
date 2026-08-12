"""Reward scoring for crash-site predictions."""

from __future__ import annotations

from math import exp

from pydantic import BaseModel

from exec_rl.models.entities import (
    CrashGroundTruth,
    CrashGroundTruthFrame,
    CrashGroundTruthPayload,
    CrashPrediction,
)


class CrashFrameScore(BaseModel):
    """Score for a prediction against one ground-truth frame."""

    depth: int
    filename: str
    function: str
    line: int
    fileScore: float
    functionScore: float
    lineScore: float
    frameScore: float
    reward: float


class CrashRewardResult(BaseModel):
    """Aggregated depth-decayed reward over all ground-truth frames."""

    reward: float
    frameScores: list[CrashFrameScore]


def score_crash_prediction(
    ground_truth: CrashGroundTruthPayload | CrashGroundTruth | dict,
    *,
    function: str,
    filename: str,
    line: int,
    decay_base: float = 2.0,
) -> CrashRewardResult:
    """Score a predicted crash site against ordered ground-truth frames.

    Per-frame rewards are divided by ``decay_base**depth``. The final reward is the sum of
    those depth-decayed frame rewards divided by the sum of all depth-decay weights.
    """
    payload = _coerce_ground_truth_payload(ground_truth)
    if decay_base <= 0:
        raise ValueError("decay_base must be positive")

    frame_scores = [
        score_crash_frame(
            frame,
            function=function,
            filename=filename,
            line=line,
            decay_base=decay_base,
        )
        for frame in payload.frames
    ]
    decay_weight_sum = sum(1 / (decay_base ** frame.depth) for frame in payload.frames)
    return CrashRewardResult(
        reward=sum(score.reward for score in frame_scores) / decay_weight_sum if decay_weight_sum else 0.0,
        frameScores=frame_scores,
    )


def score_crash_prediction_model(
    ground_truth: CrashGroundTruthPayload | CrashGroundTruth | dict,
    prediction: CrashPrediction | dict,
    *,
    decay_base: float = 2.0,
) -> CrashRewardResult:
    """Score a stored/model prediction object against ground truth."""
    parsed = (
        prediction
        if isinstance(prediction, CrashPrediction)
        else CrashPrediction.model_validate(prediction)
    )
    return score_crash_prediction(
        ground_truth,
        function=parsed.crashFunction,
        filename=parsed.crashFilename,
        line=parsed.crashLine,
        decay_base=decay_base,
    )


def score_crash_frame(
    frame: CrashGroundTruthFrame,
    *,
    function: str,
    filename: str,
    line: int,
    decay_base: float = 2.0,
) -> CrashFrameScore:
    """Score a predicted crash site against a single ground-truth frame."""
    pred_file = _normalize_path(filename)
    truth_file = _normalize_path(frame.filename)
    file_score = _file_score(pred_file, truth_file)
    function_match = _normalize_function(function) == _normalize_function(frame.function)
    function_score = float(file_score == 1.0 and function_match)
    line_score = exp(-abs(line - frame.line) / 4) if function_score == 1.0 else 0.0
    frame_score = 0.50 * file_score + 0.30 * function_score + 0.20 * line_score
    reward = frame_score / (decay_base ** frame.depth)

    return CrashFrameScore(
        depth=frame.depth,
        filename=frame.filename,
        function=frame.function,
        line=frame.line,
        fileScore=file_score,
        functionScore=function_score,
        lineScore=line_score,
        frameScore=frame_score,
        reward=reward,
    )


def _coerce_ground_truth_payload(
    ground_truth: CrashGroundTruthPayload | CrashGroundTruth | dict,
) -> CrashGroundTruthPayload:
    if isinstance(ground_truth, CrashGroundTruthPayload):
        return ground_truth
    if isinstance(ground_truth, CrashGroundTruth):
        parsed = ground_truth.parsed_ground_truth()
        if parsed is None:
            raise ValueError("CrashGroundTruth does not contain valid groundTruth")
        return parsed
    return CrashGroundTruthPayload.model_validate(ground_truth)


def _file_score(pred_file: str, truth_file: str) -> float:
    pred_splits = _path_splits(pred_file)
    truth_splits = _path_splits(truth_file)
    if not truth_splits:
        return 0.0
    return _lcs_len(pred_splits, truth_splits) / len(truth_splits)


def _normalize_path(path: str) -> str:
    path = path.strip().rstrip("!.,;)")
    marker = "/linux/"
    if marker in path:
        return path.split(marker, 1)[1]
    if path.startswith("linux/"):
        return path[len("linux/") :]
    return path.lstrip("/")


def _path_splits(path: str) -> list[str]:
    return [part for part in path.split("/") if part]


def _lcs_len(left: list[str], right: list[str]) -> int:
    if not left or not right:
        return 0
    prev = [0] * (len(right) + 1)
    for left_part in left:
        curr = [0] * (len(right) + 1)
        for j, right_part in enumerate(right, start=1):
            if left_part == right_part:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev = curr
    return prev[-1]


def _normalize_function(function: str) -> str:
    suffixes = (".cold", ".isra.", ".constprop.", ".llvm.")
    function = function.strip()
    while True:
        for suffix in suffixes:
            idx = function.find(suffix)
            if idx != -1:
                function = function[:idx]
                break
        else:
            return function
