"""Frame-pair similarity: symmetric path LCS + normalized function equality.

Path similarity normalizes by max(len(pred), len(gold)) path components -- unlike v1's
gold-length-only normalization, a superset path (``fs/extra/buffer.c`` vs gold ``fs/buffer.c``)
scores < 1, so hallucinated path segments cost rather than pay.
"""

from __future__ import annotations

from exec_rl.reward import _lcs_len, _normalize_function, _normalize_path, _path_splits


def path_similarity(pred_path: str, gold_path: str) -> float:
    pred = _path_splits(_normalize_path(pred_path))
    gold = _path_splits(_normalize_path(gold_path))
    if not pred or not gold:
        return 0.0
    return _lcs_len(pred, gold) / max(len(pred), len(gold))


def function_match(pred_function: str, gold_function: str) -> float:
    return float(_normalize_function(pred_function) == _normalize_function(gold_function))


def frame_similarity(
    pred_filename: str,
    pred_function: str,
    gold_filename: str,
    gold_function: str,
    *,
    path_weight: float = 0.4,
    func_weight: float = 0.6,
) -> float:
    """Similarity in [0,1]. Function correctness pays even with a partially wrong path (v1's
    exact-file gate dropped semantically right predictions that missed symbolization details)."""
    return path_weight * path_similarity(pred_filename, gold_filename) + func_weight * function_match(
        pred_function, gold_function
    )
