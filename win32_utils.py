"""
win32_utils.py -- Win32 상수, 팟플레이어 제어, 공통 유틸
"""
import ctypes
import ctypes.wintypes
import psutil

CFG = dict(
    CAPTURE_FPS       = 15,
    AUDIO_SR          = 16000,
    BUFFER_SEC        = 3.0,
    ANALYSIS_INTERVAL = 3.0,
    SYNC_THRESHOLD_MS = 80,
    POTPLAYER_STEP_MS = 50,
    MAX_CORRECT_STEP  = 10,
    MAX_TOTAL_SYNC_MS = 500,
    QUEUE_MAXSIZE     = 200,
)

_user32       = ctypes.windll.user32
VK_SHIFT      = 0x10
VK_OEM_PERIOD = 0xBE
VK_OEM_COMMA  = 0xBC
VK_OEM_2      = 0xBF

def post_key_to_potplayer(hwnd, vk, shift=False):
    POT_COMMAND           = 0x0400
    POT_SEND_VIRTUAL_KEY  = 0x5010
    POT_VIRTUAL_KEY_SHIFT = 0x0100
    wparam = POT_SEND_VIRTUAL_KEY
    lparam = vk | POT_VIRTUAL_KEY_SHIFT if shift else vk
    _user32.PostMessageW(hwnd, POT_COMMAND, wparam, lparam)

def find_potplayer_hwnd():
    for cls in ("PotPlayer64", "PotPlayer"):
        hwnd = _user32.FindWindowW(cls, None)
        if hwnd:
            return hwnd
    return None

def is_potplayer_playing(hwnd):
    if not hwnd:
        return False
    try:
        buf = ctypes.create_unicode_buffer(512)
        _user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value.strip()
        return bool(title) and " - " in title
    except Exception:
        return False

def is_potplayer_running():
    try:
        for proc in psutil.process_iter(["name"]):
            if "potplayer" in proc.info["name"].lower():
                return True
    except Exception:
        pass
    return False

def queue_put(q, data):
    if q.full():
        try: q.get_nowait()
        except Exception: pass
    try: q.put_nowait(data)
    except Exception: pass
