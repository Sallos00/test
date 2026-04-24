"""
similarity.py  ─  Perceptual Hash 유사도 계산
==============================================

비교 방식: sliding alignment (구간 shift에 강인)
  - 저장된 해시 / 신규 해시가 모두 프레임별 해시 리스트 형태
  - 짧은 쪽을 긴 쪽 위로 슬라이드하며 최대 평균 유사도를 반환
  - 구 DB 포맷(flat [0,1,...] 단일 해시)도 단일 프레임으로 취급해 호환

Hamming 유사도 (프레임 단위):
    similarity = 1.0 - (hamming_distance / n_bits)

범위: 0.0 (완전히 다름) ~ 1.0 (완전히 같음)

임계값 가이드:
    >= 0.85  : 동일 OP/ED 구간으로 판정 (권장)
    >= 0.75  : 유사 장면 (주의 필요)
    <  0.75  : 다른 장면
"""
from __future__ import annotations


def _normalize(h: list) -> list[list]:
    """
    해시를 프레임 리스트로 정규화.
    구 포맷(flat int 리스트) → [[...]]  단일 프레임 취급.
    신 포맷(list of list)   → 그대로 반환.
    """
    if not h:
        return []
    if isinstance(h[0], int):
        return [h]   # 구 DB 포맷: 집계된 단일 64-bit 해시
    return h         # 신 포맷: 프레임별 해시 리스트


def _hamming_sim(a: list, b: list) -> float:
    """두 프레임 해시 간 정규화 Hamming 유사도."""
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    return 1.0 - sum(x != y for x, y in zip(a, b)) / n


def compare_video_hash(hash1: list, hash2: list) -> float:
    """
    두 perceptual hash 시퀀스 간 최대 유사도 계산.

    sliding alignment: 짧은 시퀀스를 긴 시퀀스 위로 이동하며
    겹치는 프레임 쌍의 평균 Hamming 유사도가 최대인 정렬을 반환.
    "같은 OP/ED인데 감지 시작 타이밍만 다른 경우"를 동일 그룹으로 처리.

    Args:
        hash1 : 프레임별 해시 리스트 또는 구 포맷 flat 리스트
        hash2 : 동일

    Returns:
        0.0 ~ 1.0  (1.0 = 완전 일치)
    """
    h1 = _normalize(hash1)
    h2 = _normalize(hash2)

    if not h1 or not h2:
        return 0.0

    # 단일 프레임끼리는 직접 비교 (구 DB 포맷 간)
    if len(h1) == 1 and len(h2) == 1:
        return _hamming_sim(h1[0], h2[0])

    # h1이 항상 긴 쪽
    if len(h1) < len(h2):
        h1, h2 = h2, h1

    n, m = len(h1), len(h2)
    best = 0.0

    # shift: h2[0]이 h1[shift]에 정렬 (-m+1 ~ n-1)
    # 최소 1프레임 이상 겹쳐야 유효
    for shift in range(-(m - 1), n):
        sims = []
        for j in range(m):
            i = shift + j
            if 0 <= i < n:
                sims.append(_hamming_sim(h1[i], h2[j]))
        if sims:
            avg = sum(sims) / len(sims)
            if avg > best:
                best = avg

    return best
