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
    def _get_appdata() -> str:
        """환경변수 없이도 Win32 API / 레지스트리로 APPDATA 경로를 반환한다."""
        # 1. 환경변수 (일반 실행)
        v = os.environ.get("APPDATA", "")
        if v:
            return v
        # 2. Win32 SHGetFolderPathW — 환경변수가 전혀 없어도 동작
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            ok  = ctypes.windll.shell32.SHGetFolderPathW(0, 0x1a, 0, 0, buf)
            if ok == 0 and buf.value:
                return buf.value
        except Exception:
            pass
        # 3. Volatile Environment 레지스트리
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Volatile Environment") as k:
                v = winreg.QueryValueEx(k, "APPDATA")[0]
                if v:
                    return v
        except Exception:
            pass
        # 4. Shell Folders 레지스트리
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
            ) as k:
                v = winreg.QueryValueEx(k, "AppData")[0]
                if v:
                    return v
        except Exception:
            pass
        return ""
    appdata = _get_appdata()
    if appdata:
        candidate = os.path.join(appdata, "AutoSync", "oped_db.json")
        try:
            os.makedirs(os.path.dirname(candidate), exist_ok=True)
            _ensure_db_file(candidate)
            return candidate
        except Exception:
            pass
    # 폴백: 사용자 홈
    fallback = os.path.join(os.path.expanduser("~"), ".autosinc_oped_db.json")
    _ensure_db_file(fallback)
    return fallback


def _ensure_db_file(path: str) -> None:
    """
    DB 파일이 존재하지 않을 경우 빈 JSON 오브젝트({})로 초기화하여 생성한다.

    Args:
        path: 생성할 파일의 절대 경로
    """
    if not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
        except Exception:
            pass


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
    후보(candidate)와 확정후보(confirmed)를 각각 개별 상한으로 정리.

    - 확정후보(confirmed=True 또는 confirmed 키 없는 기존 항목): 최대 3개
      match_count 높은 순 보존.
    - 후보(confirmed=False): 최대 5개
      최근 추가된 순 보존 (리스트 뒤쪽 = 최신).

    Args:
        series : get_series() 반환 dict (in-place 수정)
        zone   : "오프닝" 또는 "엔딩"
    """
    items = series.get(zone)
    if not isinstance(items, list):
        return

    _MAX_CONFIRMED  = 5
    _MAX_CANDIDATES = 17

    confirmed  = [i for i in items if     i.get("confirmed", True)]
    candidates = [i for i in items if not i.get("confirmed", True)]

    # 확정후보: match_count 높은 순 상위 3개 유지
    if len(confirmed) > _MAX_CONFIRMED:
        confirmed.sort(key=lambda x: x.get("match_count", 1), reverse=True)
        confirmed = confirmed[:_MAX_CONFIRMED]

    # 후보: 최신 5개 유지 (오래된 것부터 제거)
    if len(candidates) > _MAX_CANDIDATES:
        candidates = candidates[-_MAX_CANDIDATES:]

    series[zone] = confirmed + candidates
