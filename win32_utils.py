"""

win32_utils.py -- Win32 상수, 팟플레이어 제어, 공통 유틸

"""

import ctypes

import ctypes.wintypes

import psutil

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

    # ── OP/ED 자동 스킵 설정 (gui_base._build_cfg 에서 런타임 값으로 덮어씀) ──
    OPED_AUTO_SKIP      = False,   # 자동 스킵 활성 여부
    OPED_SKIP_SEC       = 90,      # 스킵할 초

)

_user32 = ctypes.windll.user32

# 팟플레이어 IPC 메시지 베이스
WM_USER = 0x400

VK_SHIFT      = 0x10

VK_OEM_PERIOD = 0xBE

VK_OEM_COMMA  = 0xBC

VK_OEM_2      = 0xBF

def post_key_to_potplayer(hwnd, vk, shift=False):

    POT_COMMAND          = 0x0400

    POT_SEND_VIRTUAL_KEY = 0x5010

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

def get_playback_info(hwnd):

    """
    팟플레이어에서 현재 재생 위치(ms)와 전체 길이(ms)를 읽어 반환한다.
    팟플레이어 미감지 또는 IPC 실패 시 (None, None) 반환.
    """

    try:

        pos_ms = _user32.SendMessageW(hwnd, WM_USER, 0, 0x5000)

        dur_ms = _user32.SendMessageW(hwnd, WM_USER, 0, 0x5004)

        if dur_ms > 0:

            return int(pos_ms), int(dur_ms)

    except Exception:

        pass

    return None, None

def do_oped_skip(hwnd, pos_ms, dur_ms, skip_sec=90):

    """
    현재 재생 위치에서 skip_sec 초 앞으로 이동한다.
    영상 끝(dur_ms - 2초)을 초과하지 않도록 상한을 적용한다.
    성공 시 (new_pos_ms, True), 실패 시 (None, False) 반환.
    """

    try:

        new_pos = min(pos_ms + skip_sec * 1000, dur_ms - 2000)

        _user32.SendMessageW(hwnd, WM_USER, new_pos, 0x5001)

        return new_pos, True

    except Exception:

        return None, False

def queue_put(q, data):

    if q.full():

        try: q.get_nowait()

        except Exception: pass

    try: q.put_nowait(data)

    except Exception: pass
