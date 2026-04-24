"""
db_manager.py  ─  OP/ED 영상 해시 JSON DB 관리
================================================

저장 경로: ~/. autosinc_oped_db.json

DB 구조:
    {
      "시리즈키": {
        "오프닝": [
          {"video_hash": [0,1,...], "match_count": 3},
          ...
        ],
        "엔딩": [...]
      },
      ...
    }

경량 유지 원칙:
    * 시리즈 / 존 당 최대 _MAX_ITEMS_PER_ZONE 항목
    * match_count 내림차순으로 하위 항목 자동 정리
    * 직렬화: separators=(",", ":") (최소 공백)
"""
from __future__ import annotations

import json
import os
import threading

# ── 설정 ──────────────────────────────────────────────────────────────────────
def _resolve_db_path() -> str:
    """
    DB 파일 경로 결정.

    settings.json과 동일한 디렉토리(%APPDATA%\\AutoSync\\)에 저장한다.
    해당 경로를 사용할 수 없는 경우 사용자 홈 디렉토리로 폴백.
    """
    import sys
    # settings.json 저장 경로: %APPDATA%\AutoSync\
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        candidate = os.path.join(appdata, "AutoSync", "oped_db.json")
        try:
            os.makedirs(os.path.dirname(candidate), exist_ok=True)
            return candidate
        except Exception:
            pass
    # 폴백: 사용자 홈
    return os.path.join(os.path.expanduser("~"), ".autosinc_oped_db.json")


_DB_PATH           = _resolve_db_path()
_DB_LOCK           = threading.Lock()
_MAX_ITEMS_PER_ZONE = 10   # 존 당 최대 저장 항목 수


# ── 공개 API ──────────────────────────────────────────────────────────────────

def load_db() -> dict:
    """
    JSON DB 로드.

    Returns:
        DB dict, 파일 없음·파싱 실패 시 빈 dict
    """
    with _DB_LOCK:
        if not os.path.exists(_DB_PATH):
            return {}
        try:
            with open(_DB_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def save_db(db: dict) -> bool:
    """
    JSON DB 저장.

    Returns:
        성공 시 True
    """
    with _DB_LOCK:
        try:
            with open(_DB_PATH, "w", encoding="utf-8") as f:
                json.dump(db, f, ensure_ascii=False,
                          separators=(",", ":"))
            return True
        except Exception:
            return False


def get_series(db: dict, path_key: str) -> dict:
    """
    path_key에 해당하는 시리즈 데이터를 반환.
    없으면 빈 구조를 생성하고 db에 삽입 후 반환.

    Args:
        db       : load_db()가 반환한 dict (in-place 수정)
        path_key : make_path_key()로 생성한 시리즈 키

    Returns:
        {"오프닝": [...], "엔딩": [...]}
    """
    if path_key not in db:
        db[path_key] = {"오프닝": [], "엔딩": []}
    series = db[path_key]
    # 구조 보정 (DB 파손 대비)
    for zone in ("오프닝", "엔딩"):
        if not isinstance(series.get(zone), list):
            series[zone] = []
    return series


def prune_series(series: dict, zone: str) -> None:
    """
    항목 수가 _MAX_ITEMS_PER_ZONE 초과 시
    match_count 오름차순으로 하위 항목 제거 (인기 항목 보존).

    Args:
        series : get_series() 반환 dict (in-place 수정)
        zone   : "오프닝" 또는 "엔딩"
    """
    items = series.get(zone)
    if not isinstance(items, list):
        return
    if len(items) <= _MAX_ITEMS_PER_ZONE:
        return
    items.sort(key=lambda x: x.get("match_count", 1), reverse=True)
    series[zone] = items[:_MAX_ITEMS_PER_ZONE]
