"""
win32_utils.py -- Win32 상수, 팟플레이어 제어, 공통 유틸
[최종 보고] 싱크 조절과 동일한 통로를 사용하되, 64비트 리턴값 수신 방식을 보강
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

# ── 64비트 환경 대응 API 정의 ──
# SendMessageW의 반환 타입을 LPARAM(64비트 정수)으로 고정하여 데이터 유실 방지
_user32.SendMessageW.restype = ctypes.wintypes.LPARAM
_user32.SendMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
_user32.PostMessageW.restype = ctypes.wintypes.BOOL
_user32.PostMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
_user32.FindWindowW.restype = ctypes.wintypes.HWND
_user32.FindWindowW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR]
_user32.GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]

# ── 상수 ──
WM_USER              = 0x0400
POT_COMMAND          = 0x0400
POT_GET_CURRENT_TIME = 0x5000
POT_GET_TOTAL_TIME   = 0x5004
POT_SET_CURRENT_TIME = 0x5001
POT_SEND_VIRTUAL_KEY = 0x5010

VK_SHIFT      = 0x10
VK_OEM_PERIOD = 0xBE # '>'
VK_OEM_COMMA  = 0xBC # '<'
VK_OEM_2      = 0xBF # '/'

def queue_put(q, item):
    """멀티프로세싱 큐 안전 삽입 함수 (ImportError 방지)"""
    try:
        q.put_nowait(item)
    except queue.Full:
        try: q.get_nowait()
        except: pass
        try: q.put_nowait(item)
        except: pass

def find_potplayer_hwnd():
    """32/64비트 팟플레이어 핸들 검색"""
    hwnd = _user32.FindWindowW("PotPlayer64", None)
    if not hwnd:
        hwnd = _user32.FindWindowW("PotPlayer", None)
    return hwnd

def post_key_to_potplayer(hwnd, vk, shift=False):
    """가상 키 메시지 전송 (싱크 조절용)"""
    if not hwnd: return
    if shift:
        _user32.PostMessageW(hwnd, POT_COMMAND, POT_SEND_VIRTUAL_KEY, VK_SHIFT)
    _user32.PostMessageW(hwnd, POT_COMMAND, POT_SEND_VIRTUAL_KEY, vk)

def is_potplayer_playing(hwnd):
    """창 제목으로 재생 확인"""
    if not hwnd: return False
    try:
        buf = ctypes.create_unicode_buffer(512)
        _user32.GetWindowTextW(hwnd, buf, 512)
        return bool(buf.value) and " - " in buf.value
    except: return False

def is_potplayer_running():
    """프로세스 실행 여부 확인"""
    try:
        for proc in psutil.process_iter(["name"]):
            if "potplayer" in proc.info["name"].lower(): return True
    except: pass
    return False

def get_playback_info(hwnd):
    """
    [교정] 64비트 정밀도를 유지하며 재생 정보를 읽어옴
    싱크 조절이 된다면 이 메시지도 이론적으로 반드시 도달해야 합니다.
    """
    if not hwnd: return None, None
    try:
        # 64비트 정수형으로 반환값을 직접 받음
        pos_ms = _user32.SendMessageW(hwnd, WM_USER, 0, POT_GET_CURRENT_TIME)
        dur_ms = _user32.SendMessageW(hwnd, WM_USER, 0, POT_GET_TOTAL_TIME)
        
        # 팟플레이어가 정보를 주지 않거나 정지 상태인 경우
        if dur_ms is None or dur_ms <= 0:
            return None, None
            
        return int(pos_ms), int(dur_ms)
    except Exception:
        return None, None

def do_oped_skip(hwnd, pos_ms, dur_ms, skip_sec=90):
    """지정 초만큼 스킵 실행"""
    if not hwnd or pos_ms is None: return pos_ms, False
    new_pos = min(pos_ms + (skip_sec * 1000), dur_ms - 2000)
    
    # 시간 설정은 PostMessage가 아닌 SendMessage로 확실히 전달
    _user32.SendMessageW(hwnd, WM_USER, 0, POT_SET_CURRENT_TIME)
    _user32.SendMessageW(hwnd, WM_USER, new_pos, POT_SET_CURRENT_TIME)
    
    return new_pos, True
