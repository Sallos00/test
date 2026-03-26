"""
win32_utils.py -- Win32 상수, 팟플레이어 제어, 공통 유틸
[수정 보고] 64비트 대응, queue_put 복구 및 VK_OEM_2 등 누락된 상수 전원 복구
"""

import ctypes
import ctypes.wintypes
import psutil
import queue

# ── 설정값 ──
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

# ── 64비트 호환 프로토콜 정의 ──
_user32.SendMessageW.restype = ctypes.wintypes.LPARAM
_user32.SendMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
_user32.PostMessageW.restype = ctypes.wintypes.BOOL
_user32.PostMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
_user32.FindWindowW.restype = ctypes.wintypes.HWND
_user32.FindWindowW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR]
_user32.GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
_user32.MapVirtualKeyW.restype  = ctypes.wintypes.UINT
_user32.MapVirtualKeyW.argtypes = [ctypes.wintypes.UINT, ctypes.wintypes.UINT]

# ── 상수 정의 ──
WM_USER              = 0x0400
WM_KEYDOWN           = 0x0100
WM_KEYUP             = 0x0101
POT_COMMAND          = 0x0400
POT_SEND_VIRTUAL_KEY = 0x5010   # wParam for WM_USER (포커스 없이 가상 키 전송)
POT_VIRTUAL_KEY_SHIFT = 0x0100
POT_GET_TOTAL_TIME   = 0x5002   # wParam for WM_USER
POT_GET_CURRENT_TIME = 0x5004   # wParam for WM_USER
POT_SET_CURRENT_TIME = 0x5005   # wParam for WM_USER

VK_SHIFT      = 0x10
VK_Q          = 0x51  # Q key (Shift+Q = PIP 창 열기)
VK_W          = 0x57  # W key (Shift+W = PIP 창 닫기)
VK_OEM_PERIOD = 0xBE  # '.' key  (Shift+. = '>')
VK_OEM_COMMA  = 0xBC  # ',' key  (Shift+, = '<')
VK_OEM_2      = 0xBF  # '/' key

def queue_put(q, item):
    """멀티프로세싱 큐에 안전하게 데이터를 넣는다."""
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
    """POT_SEND_VIRTUAL_KEY로 팟플레이어에 키 전송 (포커스/게임 무관)."""
    if not hwnd: return
    lparam = vk
    if shift:
        lparam = lparam | POT_VIRTUAL_KEY_SHIFT
    _user32.PostMessageW(hwnd, POT_COMMAND, POT_SEND_VIRTUAL_KEY, lparam)

def is_potplayer_playing(hwnd):
    """재생 상태 여부를 확인한다."""
    if not hwnd: return False
    try:
        buf = ctypes.create_unicode_buffer(512)
        _user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value.strip()
        return bool(title) and " - " in title
    except Exception:
        return False

def is_potplayer_running():
    """프로세스 실행 여부를 확인한다."""
    try:
        for proc in psutil.process_iter(["name"]):
            if "potplayer" in proc.info["name"].lower():
                return True
    except Exception:
        pass
    return False

def get_playback_info(hwnd):
    """현재 위치와 전체 길이를 읽어온다."""
    if not hwnd:
        return None, None
    try:
        pos_ms = int(_user32.SendMessageW(hwnd, WM_USER, POT_GET_CURRENT_TIME, 0))
        dur_ms = int(_user32.SendMessageW(hwnd, WM_USER, POT_GET_TOTAL_TIME, 0))
        if pos_ms < 0:
            return None, None
        if dur_ms <= 0:
            return pos_ms, None
        if pos_ms > dur_ms + 2000:
            return None, None
        return pos_ms, dur_ms
    except Exception:
        return None, None

def pip_send(hwnd):
    """기존의 복잡한 로직을 지우고, 검증된 방식으로 교체"""
    if not hwnd: return
    
    import time as _time

    # 1. Shift + Q 전송 (UI 숨기기)
    post_key_to_potplayer(hwnd, VK_Q, shift=True)
    
    # 팟플레이어가 첫 번째 키를 인식할 수 있도록 0.05초 정도 기다려줍니다.
    _time.sleep(0.05)
    
    # 2. Shift + W 전송 (맨위 고정)
    post_key_to_potplayer(hwnd, VK_W, shift=True)

def do_oped_skip(hwnd, pos_ms, dur_ms, skip_sec=90):
    """지정된 초만큼 스킵을 수행한다."""
    if not hwnd or pos_ms is None:
        return pos_ms, False
    skip_ms = skip_sec * 1000
    new_pos = pos_ms + skip_ms
    if dur_ms is not None and new_pos > dur_ms - 2000:
        new_pos = max(0, dur_ms - 2000)
    # wParam = 명령(POT_SET_CURRENT_TIME), lParam = 위치값
    _user32.SendMessageW(hwnd, WM_USER, POT_SET_CURRENT_TIME, int(new_pos))
    return new_pos, True
