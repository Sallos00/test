"""
win32_utils.py -- Win32 상수, 팟플레이어 제어, 공통 유틸
[최종 통합본] 64비트 호환성, 누락된 상수(VK_OEM_2 등) 및 queue_put 전원 복구
"""

import ctypes
import ctypes.wintypes
import psutil
import queue

# ── 설정값 (기본값) ──
CFG = dict(
    CAPTURE_FPS         = 15,
    AUDIO_SR            = 16000,
    BUFFER_SEC          = 3.0,
    ANALYSIS_INTERVAL   = 3.0,
    SYNC_THRESHOLD_MS   = 80,
    POTPLAYER_STEP_MS   = 50,
    MAX_CORRECT_STEP    = 10,
    MAX_TOTAL_SYNC_MS   = 500,
    QUEUE_MAXSIZE       = 200,
    OPED_AUTO_SKIP      = False,   
    OPED_SKIP_SEC       = 90,      
)

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# ── 64비트 호환을 위한 Win32 API 프로토콜 정의 ──
_user32.SendMessageW.restype = ctypes.wintypes.LPARAM
_user32.SendMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
_user32.PostMessageW.restype = ctypes.wintypes.BOOL
_user32.PostMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
_user32.FindWindowW.restype = ctypes.wintypes.HWND
_user32.FindWindowW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR]
_user32.GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]

# ── 팟플레이어 IPC 및 가상 키 상수 ──
WM_USER              = 0x0400
POT_COMMAND          = 0x0400
POT_GET_CURRENT_TIME = 0x5000
POT_GET_TOTAL_TIME   = 0x5004
POT_SET_CURRENT_TIME = 0x5001
POT_SEND_VIRTUAL_KEY = 0x5010

VK_SHIFT      = 0x10
VK_OEM_PERIOD = 0xBE # '>' key
VK_OEM_COMMA  = 0xBC # '<' key
VK_OEM_2      = 0xBF # '/' key (ImportError 해결용)

def queue_put(q, item):
    """멀티프로세싱 큐에 안전하게 데이터를 넣는다 (ImportError 해결용)."""
    try:
        q.put_nowait(item)
    except queue.Full:
        try: q.get_nowait()
        except: pass
        try: q.put_nowait(item)
        except: pass

def find_potplayer_hwnd():
    """32/64비트 팟플레이어 핸들을 검색한다."""
    hwnd = _user32.FindWindowW("PotPlayer64", None)
    if not hwnd:
        hwnd = _user32.FindWindowW("PotPlayer", None)
    return hwnd

def post_key_to_potplayer(hwnd, vk, shift=False):
    """팟플레이어에 단축키 메시지를 전송한다."""
    if not hwnd: return
    if shift:
        _user32.PostMessageW(hwnd, POT_COMMAND, POT_SEND_VIRTUAL_KEY, VK_SHIFT)
    _user32.PostMessageW(hwnd, POT_COMMAND, POT_SEND_VIRTUAL_KEY, vk)

def is_potplayer_playing(hwnd):
    """창 제목을 분석하여 영상이 재생 중인지 확인한다."""
    if not hwnd: return False
    try:
        buf = ctypes.create_unicode_buffer(512)
        _user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value.strip()
        return bool(title) and " - " in title
    except Exception:
        return False

def is_potplayer_running():
    """팟플레이어 프로세스가 실행 중인지 확인한다."""
    try:
        for proc in psutil.process_iter(["name"]):
            if "potplayer" in proc.info["name"].lower():
                return True
    except Exception:
        pass
    return False

def get_playback_info(hwnd):
    """
    팟플레이어로부터 현재 재생 시간과 전체 길이를 읽어온다.
    [보강] 데이터 타입을 명시적으로 처리하여 0 혹은 None 반환 문제를 최소화함.
    """
    if not hwnd:
        return None, None
    try:
        # 메시지를 보내서 응답값을 직접 LPARAM으로 받음
        pos_ms = _user32.SendMessageW(hwnd, WM_USER, 0, POT_GET_CURRENT_TIME)
        dur_ms = _user32.SendMessageW(hwnd, WM_USER, 0, POT_GET_TOTAL_TIME)
        
        # 전체 길이가 0 이하일 경우 정지 상태로 간주
        if dur_ms is None or dur_ms <= 0:
            return None, None
            
        return int(pos_ms), int(dur_ms)
    except Exception:
        return None, None

def do_oped_skip(hwnd, pos_ms, dur_ms, skip_sec=90):
    """현재 위치에서 지정된 초만큼 앞으로 점프한다."""
    if not hwnd or pos_ms is None:
        return pos_ms, False
        
    skip_ms = skip_sec * 1000
    new_pos = pos_ms + skip_ms
    
    # 영상 종료 방지 (끝나기 2초 전까지만 이동)
    if new_pos > dur_ms - 2000:
        new_pos = max(0, dur_ms - 2000)
    
    # POT_SET_CURRENT_TIME 명령 실행
    _user32.SendMessageW(hwnd, WM_USER, 0, POT_SET_CURRENT_TIME)
    _user32.SendMessageW(hwnd, WM_USER, new_pos, POT_SET_CURRENT_TIME)
    
    return new_pos, True
