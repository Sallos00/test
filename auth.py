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

# ── 설정 ─────────────────────────────────────────────────────────
SERVER_URL   = os.environ.get("AUTH_SERVER_URL", "")
AUTH_FILE    = os.path.join(
    os.environ.get("APPDATA", ""), "LipSyncMonitor", "settings.json"
)
POLL_INTERVAL = 10   # 인증 확인 주기 (초)

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
    """Apps Script API 호출. 실패 시 빈 dict 반환."""
    try:
        url  = SERVER_URL + "?" + urllib.parse.urlencode(params)
        req  = urllib.request.Request(url, headers={"User-Agent": "AutoSync/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}

def request_auth(pc_id: str, username: str = "") -> dict:
    """서버에 인증 요청 전송."""
    return _api({"action": "request", "pc_id": pc_id, "username": username})

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
