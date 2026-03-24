"""
win32_utils.py -- Win32 상수, 팟플레이어 제어, 공통 유틸
[수정 보고] 64비트 환경 대응을 위한 SendMessageW 프로토콜 정의 및 재생 위치 감지 로직 강화
"""

import ctypes
import ctypes.wintypes
import psutil

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

# ── 64비트 호환을 위한 함수 프로토콜 명시적 정의 ──
# restype과 argtypes를 지정하지 않으면 64비트 주소값이 잘려 오류가 발생할 수 있습니다.
_user32.SendMessageW.restype = ctypes.wintypes.LPARAM
_user32.SendMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
_user32.PostMessageW.restype = ctypes.wintypes.BOOL
_user32.PostMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
_user32.FindWindowW.restype = ctypes.wintypes.HWND
_user32.FindWindowW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR]
_user32.GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]

# 팟플레이어 IPC 메시지 상수
WM_USER = 0x0400
POT_GET_CURRENT_TIME = 0x5000
POT_GET_TOTAL_TIME    = 0x5004
POT_SET_CURRENT_TIME = 0x5001
POT_COMMAND          = 0x0400
POT_SEND_VIRTUAL_KEY = 0x5010

# 가상 키 상수
VK_SHIFT      = 0x10
VK_OEM_PERIOD = 0xBE # '>' key
VK_OEM_COMMA  = 0xBC # '<' key
VK_OEM_2      = 0xBF # '/' key

def find_potplayer_hwnd():
    """32비트와 64비트 팟플레이어 윈도우 핸들을 모두 검색한다."""
    # 64비트 버전 클래스명 'PotPlayer64' 우선 검색
    hwnd = _user32.FindWindowW("PotPlayer64", None)
    if not hwnd:
        # 32비트 버전 클래스명 'PotPlayer' 검색
        hwnd = _user32.FindWindowW("PotPlayer", None)
    return hwnd

def post_key_to_potplayer(hwnd, vk, shift=False):
    """팟플레이어에 가상 키 메시지를 전송한다 (싱크 조절 등)."""
    if not hwnd: return
    if shift:
        _user32.PostMessageW(hwnd, POT_COMMAND, POT_SEND_VIRTUAL_KEY, VK_SHIFT)
    _user32.PostMessageW(hwnd, POT_COMMAND, POT_SEND_VIRTUAL_KEY, vk)

def is_potplayer_playing(hwnd):
    """영상이 실제로 재생 중인지 창 제목을 통해 확인한다."""
    if not hwnd: return False
    try:
        buf = ctypes.create_unicode_buffer(512)
        _user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value.strip()
        # 재생 중일 때는 제목에 ' - ' 구분자가 포함되는 특성을 이용
        return bool(title) and " - " in title
    except Exception:
        return False

def is_potplayer_running():
    """프로세스 목록에서 팟플레이어의 존재 여부를 확인한다."""
    try:
        for proc in psutil.process_iter(["name"]):
            name = proc.info["name"].lower()
            if "potplayer" in name:
                return True
    except Exception:
        pass
    return False

def get_playback_info(hwnd):
    """
    팟플레이어 IPC를 통해 현재 재생 위치(ms)와 전체 길이를 읽어온다.
    [수정] 반환 데이터 유실을 방지하기 위해 LPARAM 처리를 강화함.
    """
    if not hwnd:
        return None, None
    try:
        # 팟플레이어 API 호출 (wParam=0, lParam=명령코드)
        pos_ms = _user32.SendMessageW(hwnd, WM_USER, 0, POT_GET_CURRENT_TIME)
        dur_ms = _user32.SendMessageW(hwnd, WM_USER, 0, POT_GET_TOTAL_TIME)
        
        # 팟플레이어 응답이 없거나 영상이 없는 경우
        if dur_ms is None or dur_ms <= 0:
            return None, None
            
        return int(pos_ms), int(dur_ms)
    except Exception:
        return None, None

def do_oped_skip(hwnd, pos_ms, dur_ms, skip_sec=90):
    """
    현재 위치에서 지정된 시간(초)만큼 앞으로 이동한다.
    [수정] 0x5001 명령을 사용하여 정확한 밀리초 단위 이동을 수행함.
    """
    if not hwnd or pos_ms is None:
        return pos_ms, False
        
    skip_ms = skip_sec * 1000
    new_pos = pos_ms + skip_ms
    
    # 영상 끝 도달 방지 (종료 2초 전으로 제한)
    if new_pos > dur_ms - 2000:
        new_pos = max(0, dur_ms - 2000)
    
    # POT_SET_CURRENT_TIME(0x5001)은 lParam에 새 위치(ms)를 담아 보냄
    _user32.SendMessageW(hwnd, WM_USER, 0, POT_SET_CURRENT_TIME) # 명령 초기화/준비
    _user32.SendMessageW(hwnd, WM_USER, new_pos, POT_SET_CURRENT_TIME)
    
    return new_pos, True
