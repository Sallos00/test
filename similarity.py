"""
similarity.py  ─  Perceptual Hash 유사도 계산
==============================================

정규화된 Hamming 유사도:
    similarity = 1.0 - (hamming_distance / n_bits)

범위: 0.0 (완전히 다름) ~ 1.0 (완전히 같음)

임계값 가이드:
    >= 0.85  : 동일 OP/ED 구간으로 판정 (권장)
    >= 0.75  : 유사 장면 (주의 필요)
    <  0.75  : 다른 장면
"""
from __future__ import annotations


def compare_video_hash(hash1: list, hash2: list) -> float:
    """
    두 perceptual hash 간 정규화 Hamming 유사도 계산.

    Args:
        hash1 : 64-bit pHash (0/1 정수 리스트)
        hash2 : 64-bit pHash (0/1 정수 리스트)

    Returns:
        0.0 ~ 1.0  (1.0 = 완전 일치)
    """
    if not hash1 or not hash2:
        return 0.0

    n = min(len(hash1), len(hash2))
    if n == 0:
        return 0.0

    hamming = sum(a != b for a, b in zip(hash1[:n], hash2[:n]))
    return 1.0 - (hamming / n)
