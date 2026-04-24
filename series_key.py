"""
series_key.py  ─  시리즈 식별 키 생성 & OP/ED 구간 분류
=========================================================

make_path_key():
    영상 경로 또는 PotPlayer 창 제목에서 에피소드 번호를 제거하고
    소문자 정규화된 시리즈 키를 반환한다.

    예시:
        "Demon Slayer S01E03.mkv - PotPlayer 64bit"
            → "demon slayer s"
        "귀멸의 칼날 3화.mkv"
            → "귀멸의 칼날"
        "Anime - 03 - Episode Title.mkv"
            → "anime episode title"

classify_segment():
    현재 재생 위치(pos_ms)와 전체 길이(dur_ms)를 기반으로
    OP / ED 구간 여부를 판별한다.

    기준 (pos_ratio 미사용):
        - 영상 시작 후 120초 이내 AND 전반부 → "오프닝"
        - 영상 종료 전 120초 이내 AND 후반부 → "엔딩"
        - 그 외                              → None
"""
from __future__ import annotations

import os
import re

# ── OP/ED 구간 경계 ────────────────────────────────────────────────────────────
_OP_ZONE_SEC = 120   # 시작 후 120초 이내 = OP 후보 영역
_ED_ZONE_SEC = 120   # 종료 전 120초 이내 = ED 후보 영역

# ── 정규식 패턴 ───────────────────────────────────────────────────────────────

# PotPlayer 창 제목 접미사 제거
#   "... - PotPlayer 64bit", "... - 팟플레이어" 등
_RE_POT_SUFFIX = re.compile(
    r"\s*[-–]\s*(?:PotPlayer|팟플레이어)\b.*$",
    re.IGNORECASE,
)

# 에피소드 번호 패턴 (한국어 · 영어 · 일본어 통합)
#   S01E03, E03, EP3, 3화, 3話, 제3화, 第3話, part3, 파트3 등
_RE_EPISODE = re.compile(
    r"""
    (?:
        [\s_\-\[\(]*
        (?:
            S\d{1,2}E\d{1,4}   |   # S01E03 (시즌+에피소드)
            E\d{1,4}           |   # E03
            EP\.?\d{1,4}       |   # EP3, EP.3
            Episode\.?\s*\d{1,4}|  # Episode 3
            에피소드\s*\d{1,4}  |  # 에피소드 3
            제\s*\d{1,4}\s*[화편회話] |  # 제3화
            第\s*\d{1,4}\s*[話回] |  # 第3話
            \d{1,4}\s*[화편회話回]|  # 3화, 3話
            (?:part|파트)\s*\d{1,4}  # part3, 파트3
        )
        [\s_\-\]\)]*
    )
    |
    (?:                            # 독립형 숫자 (구분자 포함)
        (?<=[\s_\-\[\(])
        \d{1,4}
        (?=[\s_\-\]\)])
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# 특수문자 → 공백 (한글·알파벳·숫자·공백 외 제거)
_RE_SPECIAL = re.compile(r"[^\w\s가-힣]")
# 연속 공백 압축
_RE_SPACES  = re.compile(r"\s+")


# ── 공개 API ──────────────────────────────────────────────────────────────────

def make_path_key(video_path: str) -> str:
    """
    영상 경로 또는 창 제목 → 에피소드 번호 제거 → 시리즈 식별 키.

    Args:
        video_path : 파일 경로 또는 PotPlayer 창 제목

    Returns:
        소문자 정규화된 시리즈 키 (최소 2글자, 실패 시 "unknown")
    """
    if not video_path:
        return "unknown"

    # 1. PotPlayer 창 제목 접미사 제거
    name = _RE_POT_SUFFIX.sub("", video_path).strip()

    # 2. 파일명 추출 (경로인 경우)
    name = os.path.basename(name)

    # 3. 확장자 제거
    name = os.path.splitext(name)[0]

    # 4. 에피소드 번호 제거
    name = _RE_EPISODE.sub(" ", name)

    # 5. 특수문자 → 공백, 연속 공백 압축, 양쪽 공백 제거
    name = _RE_SPECIAL.sub(" ", name)
    name = _RE_SPACES.sub(" ", name).strip()

    # 6. 소문자 변환
    name = name.lower()

    return name if len(name) >= 2 else "unknown"


def classify_segment(pos_ms: int, dur_ms: int) -> str | None:
    """
    현재 재생 위치가 OP 또는 ED 구간에 해당하는지 분류.

    ⚠️ pos_ratio 기반 분류 미사용 — 절대 시간(초) 기준만 적용.

    Args:
        pos_ms : 현재 재생 위치 (밀리초)
        dur_ms : 전체 영상 길이 (밀리초)

    Returns:
        "오프닝"  — 시작 후 120초 이내이고 전반부
        "엔딩"    — 종료 전 120초 이내이고 후반부
        None      — 해당 없음
    """
    if dur_ms <= 0 or pos_ms < 0:
        return None

    mid_ms       = dur_ms // 2
    op_end_ms    = _OP_ZONE_SEC * 1000           # 120,000 ms
    ed_start_ms  = dur_ms - _ED_ZONE_SEC * 1000  # dur - 120,000 ms

    if pos_ms <= op_end_ms and pos_ms <= mid_ms:
        return "오프닝"

    if pos_ms >= ed_start_ms and pos_ms >= mid_ms:
        return "엔딩"

    return None
