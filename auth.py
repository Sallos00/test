"""
Auto Sync — PC 인증 모듈
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Google Apps Script 서버와 통신하여 PC 인증을 처리합니다.

[사용 전 설정]
  SERVER_URL = Apps Script 배포 URL로 변경
"""

import os
import json
import uuid
import hashlib
import platform
import threading
import urllib.request
import urllib.parse

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

# ── 설정 ─────────────────────────────────────────────────────────
SERVER_URL   = os.environ.get("AUTH_SERVER_URL", "")
AUTH_FILE    = os.path.join(_get_appdata(), "AutoSync", "settings.json")
POLL_INTERVAL = 10   # 인증 확인 주기 (초)

# ── [추가] 현재 프로그램 버전 ─────────────────────────────
# 업데이트 시트 B1 값과 비교하는 기준 버전.
# 새 버전 배포 시 이 값을 함께 변경한다.
APP_VERSION   = "1.5"

# ── PC 고유 ID 생성 ───────────────────────────────────────────────
def get_pc_id() -> str:
    """MAC 주소 + 컴퓨터 이름 + OS 정보를 조합한 고유 ID 생성."""
    try:
        mac   = ':'.join(['{:02x}'.format((uuid.getnode() >> i) & 0xff)
                          for i in range(0, 48, 8)][::-1])
        node  = platform.node()
        osver = platform.version()
        raw   = f"{mac}-{node}-{osver}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]
    except Exception:
        # 최후 수단: uuid4 기반 (재실행마다 달라질 수 있으므로 저장 필요)
        return hashlib.sha256(str(uuid.uuid4()).encode()).hexdigest()[:24]

# ── 로컬 인증 정보 저장/불러오기 ─────────────────────────────────
def _load_settings() -> dict:
    try:
        with open(AUTH_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_settings(data: dict):
    try:
        os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
        existing = _load_settings()
        existing.update(data)
        with open(AUTH_FILE, "w") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def get_local_auth() -> dict | None:
    """로컬에 저장된 인증 정보 반환. 없으면 None."""
    s = _load_settings()
    if s.get("auth_token") and s.get("auth_id"):
        return {"pc_id": s["auth_id"], "token": s["auth_token"]}
    return None

def get_local_status() -> str:
    """로컬에 저장된 마지막 인증 상태 반환."""
    return _load_settings().get("auth_status", "")

def save_local_auth(pc_id: str, token: str):
    """인증 정보 + 상태 로컬 저장."""
    from datetime import datetime
    _save_settings({
        "auth_id":     pc_id,
        "auth_token":  token,
        "auth_date":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "auth_status": AuthStatus.APPROVED,
    })

def save_local_status(status: str):
    """마지막 인증 상태만 로컬 저장."""
    _save_settings({"auth_status": status})

def clear_local_auth():
    """로컬 인증 정보 삭제."""
    _save_settings({"auth_id": "", "auth_token": "", "auth_date": "", "auth_status": ""})

# ── 서버 통신 ─────────────────────────────────────────────────────
def _api(params: dict, timeout: int = 8) -> dict:
    try:
        url = SERVER_URL + "?" + urllib.parse.urlencode(params, encoding="utf-8")
        print("[AUTH API]", url)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "AutoSync/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
            print("[AUTH RESP]", text)
            return json.loads(text)
    except Exception as e:
        print("[AUTH ERROR]", repr(e))
        return {}

def request_auth(pc_id: str, username: str = "") -> dict:
    """서버에 인증 요청 전송."""
    resp = _api({"action": "request", "pc_id": pc_id, "username": username})
    if resp.get("ok"):
        _save_settings({"auth_id": pc_id, "auth_status": AuthStatus.PENDING})
    return resp

def check_auth(pc_id: str, token: str = "") -> dict:
    """서버에 인증 상태 확인."""
    params = {"action": "check", "pc_id": pc_id}
    if token:
        params["token"] = token
    return _api(params)

# ── 인증 상태 ─────────────────────────────────────────────────────
class AuthStatus:
    APPROVED = "approved"   # 인증 완료
    PENDING  = "pending"    # 대기 중
    REVOKED  = "revoked"    # 차단됨
    ERROR    = "error"      # 오류 (네트워크 등)
    UNKNOWN  = "unknown"    # 미등록

# ── 메인 인증 로직 ────────────────────────────────────────────────
def verify(on_approved, on_revoked, on_error, on_pending=None):
    """
    인증 흐름 전체 처리.

    on_approved(token) : 인증 성공
    on_revoked()       : 인증 차단됨
    on_error(msg)      : 오류
    on_pending()       : 대기 중 (아직 허가 안 됨)
    """
    pc_id      = get_pc_id()
    local_auth = get_local_auth()

    def _check():
        token  = local_auth["token"] if local_auth else ""
        resp   = check_auth(pc_id, token)
        status = resp.get("status", "")

        if resp.get("ok") and status == AuthStatus.APPROVED:
            new_token = resp.get("token", token)
            save_local_auth(pc_id, new_token)   # 상태 "approved" 저장
            on_approved(new_token)
        elif status == AuthStatus.REVOKED:
            save_local_status(AuthStatus.REVOKED)  # 상태 "revoked" 저장
            on_revoked()
        elif status == AuthStatus.PENDING:
            save_local_status(AuthStatus.PENDING)  # 상태 "pending" 저장
            if on_pending:
                on_pending()
            else:
                on_error("사용 허가 대기 중입니다.")
        elif not resp:
            # 오프라인 → 로컬에 저장된 마지막 상태로 처리
            last = get_local_status()
            if last == AuthStatus.APPROVED:
                on_approved(local_auth["token"] if local_auth else "")
            elif last == AuthStatus.REVOKED:
                on_revoked()
            elif last == AuthStatus.PENDING:
                if on_pending:
                    on_pending()
                else:
                    on_error("사용 허가 대기 중입니다.")
            else:
                on_error("서버에 연결할 수 없습니다.")
        else:
            on_error(resp.get("msg", "인증 오류가 발생했습니다."))

    threading.Thread(target=_check, daemon=True).start()
    return pc_id


# ── [추가] 업데이트 버전 체크 ─────────────────────────────
def check_version() -> dict:
    """서버 업데이트 시트(B1)의 최신 버전과 현재 버전을 비교한다.

    반환 예시:
        {"ok": True,  "latest": "1.5"}   ← 서버 응답 성공
        {"ok": False, "msg":   "..."}          ← 응답 실패 / 시트 없음

    예외 발생 없이 빈 dict({})를 반환하는 경우도 있으므로
    호출자는 반드시 .get() 으로 키를 접근해야 한다.
    버전 일치 여부는 호출자(gui/auth.py)에서 APP_VERSION 과 비교한다.
    """
    return _api({"action": "version"}, timeout=6)


def check_update_skipped(pc_id: str) -> bool:
    """인증목록 시트 G열이 '차단'인지 확인한다.

    True  → 업데이트 팝업 미표시 (사용자가 건너뛰기를 선택한 상태)
    False → 팝업 정상 표시 (G열 없음 / 빈 값 / 서버 오류 포함)
    """
    try:
        resp = _api({"action": "check_skip", "pc_id": pc_id}, timeout=6)
        return bool(resp.get("skipped", False))
    except Exception:
        return False


def skip_update_version(pc_id: str) -> bool:
    """인증목록 시트 G열을 '차단'으로 설정한다 (업데이트 건너뛰기).

    - 이미 '차단'인 경우 서버에서 중복 처리를 막으므로 재요청해도 무해하다.
    - 네트워크 오류 시 False 반환, 예외는 호출자로 전파하지 않는다.
    """
    try:
        resp = _api({"action": "skip_update", "pc_id": pc_id}, timeout=8)
        return bool(resp.get("ok", False))
    except Exception:
        return False


def get_reg_setup_done() -> bool:
    """settings.json에 레지스트리 셋팅 완료 여부를 반환한다."""
    return bool(_load_settings().get("reg_setup_done", False))


def save_reg_setup_done():
    """settings.json에 레지스트리 셋팅 완료를 기록한다."""
    _save_settings({"reg_setup_done": True})


def get_pot_setting_shown() -> bool:
    """settings.json에 PotPlayer 설정 팝업 표시 여부를 반환한다."""
    return bool(_load_settings().get("pot_setting_shown", False))


def save_pot_setting_shown():
    """settings.json에 PotPlayer 설정 팝업 표시 완료를 기록한다."""
    _save_settings({"pot_setting_shown": True})


def check_download_permission(pc_id: str) -> str:
    """인증목록 시트 H열 다운로드 권한 값을 확인한다.

    반환값:
        "차단"  → 다운 버튼 숨김
        "허가"  → 다운 버튼 표시
        ""      → 서버 오류 / 빈 값 (버튼 상태 변경 없음)
    """
    try:
        resp = _api({"action": "check_download_perm", "pc_id": pc_id}, timeout=6)
        return str(resp.get("perm", "")).strip()
    except Exception:
        return ""


def get_server_exec_url() -> str:
    """업데이트 시트 B4 셀의 실행 파일 URL을 반환한다.

    반환값:
        URL 문자열  → 다운로드 후 실행 대상
        ""          → 서버 오류 / B4 비어있음 (실행하지 않음)
    """
    try:
        resp = _api({"action": "exec_url"}, timeout=6)
        return str(resp.get("exec_url", "")).strip()
    except Exception:
        return ""


def poll_until_approved(pc_id: str, on_approved, on_revoked, on_error, stop_event):
    """
    허가될 때까지 POLL_INTERVAL 초마다 서버 확인.
    stop_event가 set되면 중단.
    """
    def _poll():
        while not stop_event.is_set():
            resp   = check_auth(pc_id)
            status = resp.get("status", "")

            if resp.get("ok") and status == AuthStatus.APPROVED:
                token = resp.get("token", "")
                save_local_auth(pc_id, token)   # 상태 "approved" 저장
                on_approved(token)
                return
            elif status == AuthStatus.REVOKED:
                save_local_status(AuthStatus.REVOKED)  # 상태 "revoked" 저장
                on_revoked()
                return
            elif not resp:
                on_error("서버에 연결할 수 없습니다.")
                return

            stop_event.wait(POLL_INTERVAL)

    threading.Thread(target=_poll, daemon=True).start()
