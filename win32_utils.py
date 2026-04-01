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

_hwnd_cache = [0, 0.0]   # [hwnd, last_check_time]

def find_potplayer_hwnd():
    """32/64비트 팟플레이어 핸들 검색 (0.3초 캐시)."""
    import time as _t
    now = _t.time()
    if _hwnd_cache[0] and now - _hwnd_cache[1] < 0.3:
        return _hwnd_cache[0]
    hwnd = _user32.FindWindowW("PotPlayer64", None)
    if not hwnd:
        hwnd = _user32.FindWindowW("PotPlayer", None)
    _hwnd_cache[0] = hwnd
    _hwnd_cache[1] = now
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

def _is_black_frame(arr, threshold=8):
    """
    캡처된 배열이 사실상 검은 화면인지 확인한다.
    RGB 채널 평균이 threshold 미만이면 검은 화면으로 판단.
    """
    import numpy as np
    if arr is None:
        return True
    return float(arr[:, :, :3].mean()) < threshold


def _make_dib(gdi32, user32, width, height):
    """DIB 섹션을 생성하고 (hdc_mem, hbmp, pBits, old_bmp, hdc_screen) 튜플을 반환."""
    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize",          ctypes.c_uint32),
            ("biWidth",         ctypes.c_int32),
            ("biHeight",        ctypes.c_int32),
            ("biPlanes",        ctypes.c_uint16),
            ("biBitCount",      ctypes.c_uint16),
            ("biCompression",   ctypes.c_uint32),
            ("biSizeImage",     ctypes.c_uint32),
            ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32),
            ("biClrUsed",       ctypes.c_uint32),
            ("biClrImportant",  ctypes.c_uint32),
        ]

    bmi           = _BITMAPINFOHEADER()
    bmi.biSize    = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.biWidth   = width
    bmi.biHeight  = -height  # 음수 = top-down
    bmi.biPlanes  = 1
    bmi.biBitCount    = 32
    bmi.biCompression = 0

    hdc_screen = user32.GetDC(None)
    hdc_mem    = gdi32.CreateCompatibleDC(hdc_screen)
    pBits      = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(
        hdc_mem, ctypes.byref(bmi), 0,
        ctypes.byref(pBits), None, 0)

    if not hbmp or not pBits.value:
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)
        return None

    old_bmp = gdi32.SelectObject(hdc_mem, hbmp)
    return hdc_screen, hdc_mem, hbmp, pBits, old_bmp


def _read_dib(hdc_mem, hbmp, pBits, width, height, gdi32, user32,
              hdc_screen, old_bmp):
    """DIB 비트맵을 numpy 배열로 읽고 GDI 리소스를 해제한다."""
    import numpy as np
    try:
        if not pBits.value:
            return None
        buf = (ctypes.c_uint8 * (width * height * 4)).from_address(pBits.value)
        return np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 4).copy()
    except Exception:
        return None
    finally:
        gdi32.SelectObject(hdc_mem, old_bmp)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)


def _capture_via_dwm_thumbnail(hwnd, width, height):
    """
    DWM 썸네일 API를 이용해 캡처한다.
    GPU 오버레이(Direct3D / DXVA / EVR)를 쓰는 창에서도 동작하는 경우가 많다.
    성공하면 numpy BGRA 배열, 실패하면 None 반환.
    """
    import numpy as np

    # 숨겨진 호스트 창 생성
    user32 = ctypes.windll.user32
    gdi32  = ctypes.windll.gdi32
    dwmapi = ctypes.windll.dwmapi

    # 최소 크기 보호
    cap_w = max(width,  8)
    cap_h = max(height, 8)

    # 투명 더미 창 (WS_EX_TOOLWINDOW | WS_EX_LAYERED, WS_POPUP)
    WS_POPUP        = 0x80000000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_LAYERED   = 0x00080000
    hhost = user32.CreateWindowExW(
        WS_EX_TOOLWINDOW | WS_EX_LAYERED,
        "STATIC", None, WS_POPUP,
        -cap_w - 10, 0, cap_w, cap_h,
        None, None, None, None)
    if not hhost:
        return None

    try:
        # DwmRegisterThumbnail
        thumb = ctypes.wintypes.HANDLE()
        if dwmapi.DwmRegisterThumbnail(hhost, hwnd, ctypes.byref(thumb)) != 0:
            return None

        # DWM_THUMBNAIL_PROPERTIES
        class DWM_THUMBNAIL_PROPERTIES(ctypes.Structure):
            _fields_ = [
                ("dwFlags",               ctypes.c_uint32),
                ("rcDestination",         ctypes.wintypes.RECT),
                ("rcSource",              ctypes.wintypes.RECT),
                ("opacity",               ctypes.c_byte),
                ("fVisible",              ctypes.c_bool),
                ("fSourceClientAreaOnly", ctypes.c_bool),
            ]

        DWM_TNP_RECTDESTINATION  = 0x00000001
        DWM_TNP_RECTSOURCE       = 0x00000002
        DWM_TNP_OPACITY          = 0x00000004
        DWM_TNP_VISIBLE          = 0x00000008
        DWM_TNP_SOURCECLIENTAREAONLY = 0x00000010

        props = DWM_THUMBNAIL_PROPERTIES()
        props.dwFlags = (DWM_TNP_RECTDESTINATION | DWM_TNP_RECTSOURCE |
                         DWM_TNP_OPACITY | DWM_TNP_VISIBLE |
                         DWM_TNP_SOURCECLIENTAREAONLY)
        props.rcDestination.left   = 0
        props.rcDestination.top    = 0
        props.rcDestination.right  = cap_w
        props.rcDestination.bottom = cap_h
        props.rcSource.left        = 0
        props.rcSource.top         = 0
        props.rcSource.right       = width
        props.rcSource.bottom      = height
        props.opacity  = 255
        props.fVisible = True
        props.fSourceClientAreaOnly = False

        dwmapi.DwmUpdateThumbnailProperties(thumb, ctypes.byref(props))

        # 더미 창 표시 (DWM이 렌더링하도록)
        user32.ShowWindow(hhost, 4)  # SW_SHOWNOACTIVATE
        dwmapi.DwmFlush()

        # DIB 캡처
        dib = _make_dib(gdi32, user32, cap_w, cap_h)
        if dib is None:
            return None
        hdc_screen, hdc_mem, hbmp, pBits, old_bmp = dib

        hdc_host = user32.GetWindowDC(hhost)
        ok = False
        if hdc_host:
            gdi32.BitBlt(hdc_mem, 0, 0, cap_w, cap_h, hdc_host, 0, 0, 0x00CC0020)
            user32.ReleaseDC(hhost, hdc_host)
            ok = True

        arr = _read_dib(hdc_mem, hbmp, pBits, cap_w, cap_h,
                        gdi32, user32, hdc_screen, old_bmp) if ok else None
        dwmapi.DwmUnregisterThumbnail(thumb)
        return arr if (arr is not None and not _is_black_frame(arr)) else None

    except Exception:
        return None
    finally:
        user32.DestroyWindow(hhost)


def capture_window(hwnd):
    """
    팟플레이어 창을 캡처하여 numpy BGRA 배열로 반환한다.

    시도 순서:
      1) PrintWindow(PW_RENDERFULLCONTENT=2)  — D3D/GPU 렌더링 지원
         → 검은 화면이면 다음 단계로 진행 (성공 여부만으로는 판단 불가)
      2) PrintWindow(플래그=0)                 — 구형 GDI 폴백
      3) BitBlt                               — 화면에 보이는 창 전용
      4) DWM 썸네일 API                       — GPU 오버레이 우회 (최후 수단)
    """
    if not hwnd:
        return None

    import numpy as np
    gdi32  = ctypes.windll.gdi32
    user32 = ctypes.windll.user32

    # 창 크기
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width  = rect.right  - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None

    PW_RENDERFULLCONTENT = 2

    def _try_printwindow(flags):
        dib = _make_dib(gdi32, user32, width, height)
        if dib is None:
            return None
        hdc_screen, hdc_mem, hbmp, pBits, old_bmp = dib
        ok = user32.PrintWindow(hwnd, hdc_mem, flags)
        arr = _read_dib(hdc_mem, hbmp, pBits, width, height,
                        gdi32, user32, hdc_screen, old_bmp)
        return arr if ok else None

    def _try_bitblt():
        dib = _make_dib(gdi32, user32, width, height)
        if dib is None:
            return None
        hdc_screen, hdc_mem, hbmp, pBits, old_bmp = dib
        hdc_win = user32.GetWindowDC(hwnd)
        if not hdc_win:
            _read_dib(hdc_mem, hbmp, pBits, width, height,
                      gdi32, user32, hdc_screen, old_bmp)
            return None
        gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_win, 0, 0, 0x00CC0020)
        user32.ReleaseDC(hwnd, hdc_win)
        return _read_dib(hdc_mem, hbmp, pBits, width, height,
                         gdi32, user32, hdc_screen, old_bmp)

    # 시도 1: PW_RENDERFULLCONTENT
    arr = _try_printwindow(PW_RENDERFULLCONTENT)
    if arr is not None and not _is_black_frame(arr):
        return arr

    # 시도 2: PrintWindow 플래그=0
    arr = _try_printwindow(0)
    if arr is not None and not _is_black_frame(arr):
        return arr

    # 시도 3: BitBlt
    arr = _try_bitblt()
    if arr is not None and not _is_black_frame(arr):
        return arr

    # 시도 4: DWM 썸네일 (GPU 렌더러 우회)
    arr = _capture_via_dwm_thumbnail(hwnd, width, height)
    if arr is not None:
        return arr

    # 모든 방법이 검은 화면 → None 반환 (캡처 실패로 처리)
    return None
