"""gui/record_backend.py -- 오디오/화면 녹화 백엔드 (OBS 방식 싱크)"""
import os, time, threading, subprocess, tempfile, ctypes, ctypes.wintypes as wt
from mem_utils import run_gc

try:
    import cv2, numpy as np; _CV2_OK = True
except ImportError:
    _CV2_OK = False

try:
    import soundfile as sf; _SF_OK = True
except ImportError:
    _SF_OK = False

try:
    from PIL import ImageGrab; _PIL_OK = True
except ImportError:
    _PIL_OK = False

try:
    _user32 = ctypes.windll.user32; _WIN_OK = True
except Exception:
    _WIN_OK = False

import tkinter as tk

# 디버그 로거
_debug_log = []
def _log(msg: str):
    _debug_log.append(msg)
    if len(_debug_log) > 200:
        _debug_log.pop(0)

# BITMAPINFO 공용 구조체
class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]

class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]

# 팟플레이어 창 영역
def _get_potplayer_video_hwnd(parent_hwnd):
    children = []
    def _cb(hwnd, _):
        rc = wt.RECT()
        _user32.GetClientRect(hwnd, ctypes.byref(rc))
        w, h = rc.right - rc.left, rc.bottom - rc.top
        if w > 100 and h > 100:
            children.append((w * h, hwnd))
        return True
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    _user32.EnumChildWindows(parent_hwnd, CB(_cb), 0)
    return children[0][1] if children else None

def _get_potplayer_rect():
    if not _WIN_OK:
        return None
    try:
        from win32_utils import find_potplayer_hwnd
        hwnd = find_potplayer_hwnd()
        if not hwnd:
            return None
        target = _get_potplayer_video_hwnd(hwnd) or hwnd
        rc = wt.RECT()
        _user32.GetClientRect(target, ctypes.byref(rc))
        pt = wt.POINT(0, 0)
        _user32.ClientToScreen(target, ctypes.byref(pt))
        w, h = rc.right - rc.left, rc.bottom - rc.top
        return (pt.x, pt.y, w, h) if w > 0 and h > 0 else None
    except Exception:
        return None

# 오버레이
_active_overlays: list = []
_GWLP_HWNDPARENT = -8

def _show_overlay(root, message: str, duration_ms: int = 3000):
    rect = _get_potplayer_rect()
    if rect is None:
        return None
    px, py = rect[0], rect[1]
    try:
        from win32_utils import find_potplayer_hwnd
        pot_hwnd = find_potplayer_hwnd() if _WIN_OK else None
    except Exception:
        pot_hwnd = None

    try:
        ov = tk.Toplevel(root)
        ov.overrideredirect(True)
        ov.attributes("-topmost", False)
        ov.attributes("-alpha", 0.88)
        ov.configure(bg="#101010")
        ov.geometry(f"+{px + 12}+{py + 12}")
        tk.Label(ov, text=message, font=("Segoe UI", 11, "bold"),
                 bg="#101010", fg="#00c8e0", padx=14, pady=8).pack()
        ov.update_idletasks()

        if pot_hwnd and _WIN_OK:
            try:
                tk_hwnd = int(ov.winfo_id())
                ov_hwnd = ctypes.windll.user32.GetAncestor(tk_hwnd, 2) or tk_hwnd
                try:
                    ctypes.windll.user32.SetWindowLongPtrW(ov_hwnd, _GWLP_HWNDPARENT, pot_hwnd)
                except AttributeError:
                    ctypes.windll.user32.SetWindowLongW(ov_hwnd, _GWLP_HWNDPARENT, pot_hwnd)
                F = 0x0002 | 0x0001 | 0x0004 | 0x0020
                ctypes.windll.user32.SetWindowPos(ov_hwnd, 0, 0, 0, 0, 0, F)
            except Exception:
                pass

        _active_overlays.append(ov)
        _closed = [False]

        def _track():
            if _closed[0] or not _try_exists(ov):
                return
            r = _get_potplayer_rect()
            if r:
                try: ov.geometry(f"+{r[0] + 12}+{r[1] + 12}")
                except Exception: pass
            try: root.after(150, _track)
            except Exception: pass
        root.after(150, _track)

        def _close():
            _closed[0] = True
            try: ov.destroy()
            except: pass
            try: _active_overlays.remove(ov)
            except ValueError: pass
        if duration_ms > 0:
            root.after(duration_ms, _close)
        return ov
    except Exception:
        return None

def _try_exists(widget) -> bool:
    try: return widget.winfo_exists()
    except Exception: return False

def _hide_all_overlays():
    for ov in list(_active_overlays):
        try: ov.withdraw()
        except: pass

def _show_all_overlays():
    for ov in list(_active_overlays):
        try: ov.deiconify()
        except: pass

# ffmpeg 유틸
def _find_ffmpeg() -> str:
    import shutil, sys
    meipass = getattr(sys, "_MEIPASS", None)
    candidates = []
    if meipass:
        candidates.append(os.path.join(meipass, "ffmpeg.exe"))
    candidates += [
        os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    ]
    for p in candidates:
        try:
            if os.path.isfile(p): return p
        except Exception:
            pass
    return shutil.which("ffmpeg") or ""

def _popen_no_window(cmd, **kwargs):
    kwargs.setdefault("creationflags", 0x08000000)
    return subprocess.Popen(cmd, **kwargs)

def _remove_files(*paths):
    for p in paths:
        try: os.remove(p)
        except: pass

# PrintWindow 기반 프레임 캡처 공통 헬퍼
def _printwindow_capture(target, gdi32, user32, cw, ch):
    """target hwnd에서 BGR 배열 캡처. 실패 시 None 반환."""
    import numpy as np
    PW_RENDERFULLCONTENT = 0x00000002
    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = cw
    bmi.bmiHeader.biHeight = -ch
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0

    hdc_win = user32.GetDC(target)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    pBits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), 0, ctypes.byref(pBits), None, 0)
    old = gdi32.SelectObject(hdc_mem, hbmp)
    if not user32.PrintWindow(target, hdc_mem, PW_RENDERFULLCONTENT):
        user32.PrintWindow(target, hdc_mem, 0)

    raw = (ctypes.c_uint8 * (cw * ch * 4)).from_address(pBits.value)
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(ch, cw, 4).copy()

    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(target, hdc_win)
    return arr

def _abs_frame_scheduler(fps, running_flag, capture_fn, write_frame_cb):
    """OBS 방식 절대 시각 기준 프레임 스케줄러."""
    from audio_com import qpc_freq, qpc_now
    _freq = qpc_freq()
    interval = 1.0 / fps
    next_frame_qpc = qpc_now()

    while running_flag.is_set():
        try:
            bgr, frame_qpc = capture_fn()
            if bgr is not None:
                write_frame_cb(bgr, frame_qpc)
                del bgr  # 인코딩 완료 후 즉시 해제 (루프 재진입 전까지 메모리 점유 방지)
        except Exception as e:
            _log(f"캡처 오류: {e}")

        next_frame_qpc += int(interval * _freq)
        now_qpc = qpc_now()
        sl = (next_frame_qpc - now_qpc) / _freq
        if sl > 0:
            time.sleep(sl)
        elif sl < -interval:
            next_frame_qpc = now_qpc
            _log(f"프레임 타이밍 리셋: 지연={-sl*1000:.1f}ms")

# mss 캡처 루프
def _mss_capture_loop(width, height, fps, running_flag, write_frame_cb):
    import cv2 as _cv2
    from win32_utils import find_potplayer_hwnd
    from audio_com import qpc_now

    gdi32  = ctypes.windll.gdi32
    user32 = ctypes.windll.user32

    def _capture():
        hwnd = find_potplayer_hwnd()
        if not hwnd:
            time.sleep(0.1)
            return None, None
        target = _get_potplayer_video_hwnd(hwnd) or hwnd
        rc = wt.RECT()
        user32.GetClientRect(target, ctypes.byref(rc))
        cw = (rc.right - rc.left) & ~1
        ch = (rc.bottom - rc.top) & ~1
        if cw <= 0 or ch <= 0:
            time.sleep(0.05)
            return None, None
        arr = _printwindow_capture(target, gdi32, user32, cw, ch)
        frame_qpc = qpc_now()
        bgr = _cv2.cvtColor(arr, _cv2.COLOR_BGRA2BGR)
        del arr  # BGRA 원본 즉시 해제 (약 8MB/프레임, 30fps 반복)
        if (cw, ch) != (width, height):
            bgr = _cv2.resize(bgr, (width, height))
        return bgr, frame_qpc

    _abs_frame_scheduler(fps, running_flag, _capture, write_frame_cb)

# WGC 캡처
def _wgc_capture_hwnd(hwnd, width, height, fps, running_flag, write_frame_cb):
    import numpy as np
    import cv2 as _cv2

    try:
        import winrt.windows.graphics.capture as wgc
        import winrt.windows.graphics.directx as wgdx
        import winrt.windows.graphics.directx.direct3d11 as d3d11
        import winrt.windows.graphics.imaging as wgi
        _use_winrt = True
    except ImportError:
        _use_winrt = False

    if not _use_winrt:
        _printwindow_loop(hwnd, width, height, fps, running_flag, write_frame_cb)
        return

    item = wgc.GraphicsCaptureItem.create_for_window(hwnd)
    if item is None:
        _printwindow_loop(hwnd, width, height, fps, running_flag, write_frame_cb)
        return

    from audio_com import qpc_now
    d3d   = d3d11.create_direct3d_device()
    BGRA8 = wgdx.DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED
    pool  = wgc.Direct3D11CaptureFramePool.create(d3d, BGRA8, 2, item.size)
    session = pool.create_capture_session(item)
    try: session.is_cursor_capture_enabled = False
    except: pass
    session.start_capture()

    frame_ready = threading.Event()
    last_frame  = [None]
    def _on_frame(sender, _):
        f = sender.try_get_next_frame()
        if f is not None:
            # 이전 프레임이 아직 처리되지 않았다면 교체 전 해제
            old = last_frame[0]
            last_frame[0] = f
            frame_ready.set()
            if old is not None:
                try: old.close()
                except Exception: pass
    pool.frame_arrived += _on_frame

    try:
        while running_flag.is_set():
            frame_ready.wait(timeout=0.1)
            frame_ready.clear()
            f = last_frame[0]
            if f is None:
                continue
            last_frame[0] = None  # 참조 즉시 해제 (다음 _on_frame 콜백 전까지)
            sb = buf = ref = arr = bgr = None  # 예외 경로에서 del 안전하게
            try:
                frame_qpc = qpc_now()
                sb    = wgi.SoftwareBitmap.create_copy_from_surface_async(f.surface).get()
                buf   = sb.lock_buffer(wgi.BitmapBufferAccessMode.READ)
                plane = buf.get_plane_description(0)
                ref   = buf.create_reference()
                fh, fw = plane.height, plane.width
                arr   = np.frombuffer(bytes(ref), dtype=np.uint8).reshape(fh, fw, 4)
                bgr   = _cv2.cvtColor(arr, _cv2.COLOR_BGRA2BGR)
                del arr  # BGRA 원본 즉시 해제
                if (fw, fh) != (width, height):
                    bgr = _cv2.resize(bgr, (width, height))
                write_frame_cb(bgr, frame_qpc)
                del bgr  # 인코딩 완료 후 BGR 해제
            except Exception as e:
                _log(f"WGC 프레임 오류: {e}")
            finally:
                # WinRT 참조 카운트 명시적 해제 (Python GC 의존 불가)
                if ref is not None:
                    try: del ref
                    except Exception: pass
                if buf is not None:
                    try: del buf
                    except Exception: pass
                if sb is not None:
                    try: del sb
                    except Exception: pass
                # 처리 완료된 WGC 프레임 해제
                try: f.close()
                except Exception: pass
    finally:
        try: session.close()
        except: pass
        try: pool.close()
        except: pass

def _printwindow_loop(hwnd, width, height, fps, running_flag, write_frame_cb):
    import cv2 as _cv2
    from audio_com import qpc_now

    gdi32  = ctypes.windll.gdi32
    user32 = ctypes.windll.user32

    def _capture():
        rc = wt.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rc))
        cw, ch = rc.right - rc.left, rc.bottom - rc.top
        if cw <= 0 or ch <= 0:
            time.sleep(0.1)
            return None, None
        arr = _printwindow_capture(hwnd, gdi32, user32, cw, ch)
        frame_qpc = qpc_now()
        bgr = _cv2.cvtColor(arr, _cv2.COLOR_BGRA2BGR)
        del arr  # BGRA 원본 즉시 해제
        if (cw, ch) != (width, height):
            bgr = _cv2.resize(bgr, (width, height))
        return bgr, frame_qpc

    _abs_frame_scheduler(fps, running_flag, _capture, write_frame_cb)

# OBS ASRC 방식 오디오 재타이밍
def _retiming_audio(chunks, sr: int, ch: int):
    """
    OBS ASRC 방식 오디오 재타이밍.
    chunks: list of (qpc_sec, pcm_array)
    반환:   (resampled_array, start_qpc_sec)
    """
    import numpy as np

    if not chunks:
        return np.zeros(0, dtype=np.float32), 0.0

    try:
        from scipy.signal import resample_poly
        _HAS_SCIPY = True
    except ImportError:
        _HAS_SCIPY = False

    start_qpc      = chunks[0][0]
    out_parts      = []
    cursor_samples = 0
    WINDOW_SEC     = 10.0
    window_chunks  = []
    window_start   = chunks[0][0]

    def _place_corrected(arr, w_start):
        nonlocal cursor_samples
        if not len(arr): return
        expected = int((w_start - start_qpc) * sr)
        gap = expected - cursor_samples
        if gap > 2:
            _log(f"오디오 갭 패딩: {gap} frames ({gap/sr*1000:.1f}ms)")
            out_parts.append(np.zeros(gap * ch, dtype=np.float32))
            cursor_samples += gap
        elif gap < -2:
            skip = min(-gap, len(arr) // ch)
            _log(f"오디오 겹침 제거: {skip} frames ({skip/sr*1000:.1f}ms)")
            arr = arr[skip * ch:]
        if len(arr) > 0:
            out_parts.append(arr)
            cursor_samples += len(arr) // ch

    def _flush_window(wchunks, w_start):
        if not wchunks: return
        total_frames = sum(len(a) // ch for _, a in wchunks)
        if not total_frames: return
        qpc_elapsed = wchunks[-1][0] - w_start
        raw = np.concatenate([a for _, a in wchunks]).astype(np.float32)
        if qpc_elapsed < 0.5:
            _place_corrected(raw, w_start)
            del raw  # out_parts가 소유 → 로컬 참조 해제
            return
        clock_ratio = (total_frames / (qpc_elapsed * sr)) if qpc_elapsed > 0 else 1.0
        drift_ppm = abs(clock_ratio - 1.0) * 1e6
        if drift_ppm < 20.0:
            _place_corrected(raw, w_start)
            del raw  # out_parts가 소유 → 로컬 참조 해제
            return
        _log(f"[ASRC] 드리프트 {drift_ppm:.1f}ppm (ratio={clock_ratio:.8f}) → 리샘플링")
        if _HAS_SCIPY:
            from fractions import Fraction
            frac = Fraction(1.0 / clock_ratio).limit_denominator(10000)
            up, down = frac.numerator, frac.denominator
            try:
                if ch > 1:
                    r2d = raw.reshape(-1, ch)
                    corrected = np.stack(
                        [resample_poly(r2d[:, c], up, down) for c in range(ch)],
                        axis=1).reshape(-1).astype(np.float32)
                    del r2d
                else:
                    corrected = resample_poly(raw, up, down).astype(np.float32)
            except Exception as e:
                _log(f"[ASRC] resample_poly 실패: {e}"); corrected = raw
        else:
            target_frames = int(round(total_frames / clock_ratio))
            if ch > 1:
                r2d = raw.reshape(-1, ch)
                idx = np.linspace(0, len(r2d) - 1, target_frames)
                corrected = np.stack(
                    [np.interp(idx, np.arange(len(r2d)), r2d[:, c])
                     for c in range(ch)], axis=1).reshape(-1).astype(np.float32)
                del r2d, idx
            else:
                idx = np.linspace(0, len(raw) - 1, target_frames)
                corrected = np.interp(idx, np.arange(len(raw)), raw).astype(np.float32)
                del idx
        # corrected가 raw와 다른 객체인 경우에만 raw 해제 (fallback 시 corrected=raw)
        if corrected is not raw:
            del raw
        _place_corrected(corrected, w_start)
        del corrected  # out_parts가 소유 → 로컬 참조 해제

    for qpc_sec, arr in chunks:
        window_chunks.append((qpc_sec, arr))
        if qpc_sec - window_start >= WINDOW_SEC:
            _flush_window(window_chunks, window_start)
            window_chunks.clear()  # 처리 완료된 윈도우 참조 즉시 해제
            window_start  = qpc_sec
    if window_chunks:
        _flush_window(window_chunks, window_start)
        window_chunks.clear()

    if not out_parts:
        return np.zeros(0, dtype=np.float32), start_qpc
    result = np.concatenate(out_parts).astype(np.float32)
    out_parts.clear()  # concatenate 완료 후 서브 배열 참조 해제 (이중 보유 방지)
    return result, start_qpc

# 오디오 캡처
class _AudioRecorder:
    """WASAPI QPC 타임스탬프 기반 오디오 캡처 + OBS ASRC 드리프트 보정."""
    def __init__(self):
        self._chunks  = []
        self._sr      = 48000
        self._ch      = 2
        self._running = False
        self._thread  = None
        self._first_audio_qpc_sec: float = 0.0

    def start(self, pid: int):
        self._chunks  = []
        self._running = True
        recorder = self

        def _session_mta():
            import ctypes as ct
            import numpy as np
            ole32    = ct.windll.ole32
            kernel32 = ct.windll.kernel32
            hr_co = ole32.CoInitializeEx(None, 0x0)
            co_ok = hr_co in (0, 1, 0x80010106)
            client = None; cap = None; h_event = None
            try:
                from audio_com import (
                    activate_process_loopback, audio_client_initialize,
                    audio_client_set_event, audio_client_start, audio_client_stop,
                    get_capture_client, get_next_packet_size, get_buffer,
                    release_buffer, _com_release, AUDCLNT_BUFFERFLAGS_SILENT,
                    qpc_freq, activate_global_loopback, audio_client_initialize_loopback,
                )
                try:
                    from audio_capture import _SUPPORT_PROCESS_LOOPBACK as _spl
                except Exception:
                    import platform as _p; _spl = int(_p.version().split(".")[-1]) >= 19041
                _freq = qpc_freq()
                if _spl:
                    client = activate_process_loopback(pid)
                    sr, ch = audio_client_initialize(client)
                else:
                    client = activate_global_loopback()
                    sr, ch = audio_client_initialize_loopback(client)
                recorder._sr, recorder._ch = sr, ch
                h_event = kernel32.CreateEventW(None, False, False, None)
                audio_client_set_event(client, h_event)
                cap = get_capture_client(client)
                audio_client_start(client)
                try:
                    while recorder._running:
                        kernel32.WaitForSingleObject(h_event, 10)
                        while recorder._running:
                            try:
                                pkt = get_next_packet_size(cap)
                            except OSError:
                                recorder._running = False; break
                            if pkt == 0: break
                            data, num_frames, flg, dp, qp = get_buffer(cap)
                            if num_frames > 0:
                                if not (flg & AUDCLNT_BUFFERFLAGS_SILENT) and data.value:
                                    if qp:
                                        chunk_qpc_sec = qp / _freq
                                    else:
                                        _q = ct.c_ulonglong()
                                        kernel32.QueryPerformanceCounter(ct.byref(_q))
                                        chunk_qpc_sec = _q.value / _freq
                                    if not recorder._first_audio_qpc_sec:
                                        recorder._first_audio_qpc_sec = chunk_qpc_sec
                                        _log(f"[OBS싱크] 첫 오디오 QPC: {chunk_qpc_sec:.6f}s")
                                    buf = (ct.c_float * (num_frames * ch)).from_address(data.value)
                                    recorder._chunks.append((chunk_qpc_sec, np.frombuffer(buf, dtype=np.float32).copy()))
                                elif recorder._first_audio_qpc_sec and qp:
                                    recorder._chunks.append((qp / _freq, np.zeros(num_frames * ch, dtype=np.float32)))
                            release_buffer(cap, num_frames)
                finally:
                    try: audio_client_stop(client)
                    except: pass
                    _com_release(cap); _com_release(client)
                    kernel32.CloseHandle(h_event)
            except Exception as e:
                _log(f"오디오 캡처 오류: {e}"); recorder._running = False
            finally:
                if co_ok: ole32.CoUninitialize()

        self._thread = threading.Thread(target=_session_mta, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        if self._chunks:
            arr, start_qpc = _retiming_audio(self._chunks, self._sr, self._ch)
            self._first_audio_qpc_sec = start_qpc
            # ── [정리] 재타이밍 완료 후 원본 chunk 버퍼 즉시 해제 ──────────────
            # 녹화 시간에 비례한 float32 numpy 배열(수십~수백 MB)이
            # _retiming_audio() 이후에도 self._chunks에 잔류하면 GC 대상이 안 됨.
            self._chunks = []
            run_gc()
            return arr, self._sr, self._ch
        return None, self._sr, self._ch

# 화면 녹화 + 실시간 ffmpeg 인코딩
class _ScreenRecorder:
    """OBS 비디오 타이밍: wallclock timestamps + vsync passthrough."""
    def __init__(self):
        self._running_flag        = threading.Event()
        self._thread              = None
        self._fps                 = 30
        self._size                = (1280, 720)
        self._hwnd                = None
        self._ffmpeg_proc         = None
        self._ffmpeg_log_path     = None
        self._ffmpeg_log_fh       = None
        self._tmp_video           = None
        self._first_frame_qpc_sec: float = 0.0
        self._frame_count         = 0
        self._lock                = threading.Lock()

    def start(self, fps=30, root=None, out_path=None):
        from win32_utils import find_potplayer_hwnd
        hwnd = find_potplayer_hwnd()
        if not hwnd:
            raise RuntimeError("팟플레이어 창을 찾을 수 없습니다.")
        self._hwnd = _get_potplayer_video_hwnd(hwnd) or hwnd
        rect = _get_potplayer_rect()
        if not rect:
            raise RuntimeError("팟플레이어 창 영역을 구할 수 없습니다.")
        _, _, pw, ph = rect
        w, h = pw & ~1, ph & ~1
        self._fps, self._size, self._frame_count = fps, (w, h), 0

        ffmpeg_bin = _find_ffmpeg()
        if not ffmpeg_bin:
            raise RuntimeError("ffmpeg를 찾을 수 없습니다.")

        self._tmp_video       = os.path.join(tempfile.gettempdir(), "autosinc_live_video.mp4")
        self._ffmpeg_log_path = os.path.join(tempfile.gettempdir(), "autosinc_ffmpeg.log")
        _remove_files(self._tmp_video, self._ffmpeg_log_path)

        cmd = [
            ffmpeg_bin, "-y",
            "-use_wallclock_as_timestamps", "1",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(fps),
            "-i", "pipe:0",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-vsync", "passthrough",
            "-movflags", "+faststart", "-an", self._tmp_video,
        ]
        try:
            self._ffmpeg_log_fh = open(self._ffmpeg_log_path, "wb")
            self._ffmpeg_proc   = _popen_no_window(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._ffmpeg_log_fh)
        except Exception as e:
            raise RuntimeError(f"ffmpeg 실행 실패: {e}")

        self._running_flag.set()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _write_frame(self, bgr_frame, frame_qpc: int):
        if self._ffmpeg_proc and self._ffmpeg_proc.stdin:
            try:
                with self._lock:
                    if not self._first_frame_qpc_sec:
                        from audio_com import qpc_freq
                        self._first_frame_qpc_sec = frame_qpc / qpc_freq()
                        _log(f"[OBS싱크] 첫 프레임 QPC: {self._first_frame_qpc_sec:.6f}s")
                    self._frame_count += 1
                self._ffmpeg_proc.stdin.write(bgr_frame.tobytes())
            except (BrokenPipeError, OSError):
                self._running_flag.clear()

    def _loop(self):
        try:
            _mss_capture_loop(self._size[0], self._size[1], self._fps, self._running_flag, self._write_frame)
        except Exception as e:
            _log(f"캡처 루프 예외: {e}")
        finally:
            if self._ffmpeg_proc and self._ffmpeg_proc.stdin:
                try: self._ffmpeg_proc.stdin.close()
                except Exception: pass

    def stop(self) -> str:
        self._running_flag.clear()
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive() and self._ffmpeg_proc and self._ffmpeg_proc.stdin:
                try: self._ffmpeg_proc.stdin.close()
                except: pass

        if self._ffmpeg_proc:
            try:
                if self._ffmpeg_proc.stdin:
                    self._ffmpeg_proc.stdin.close()
            except Exception:
                pass
            try:
                self._ffmpeg_proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._ffmpeg_proc.kill()
                self._ffmpeg_proc.wait()
            finally:
                try:
                    if self._ffmpeg_log_fh:
                        self._ffmpeg_log_fh.close()
                except: pass

            rc = self._ffmpeg_proc.returncode
            if rc != 0:
                _save_ffmpeg_log_on_fail(self._ffmpeg_log_path, self._tmp_video)
                raise RuntimeError(f"ffmpeg 인코딩 실패 (code={rc})\n로그: {self._ffmpeg_log_path}")
            else:
                _remove_files(self._ffmpeg_log_path)

        _log(f"[OBS싱크] 영상 총 프레임: {self._frame_count}")
        tmp = self._tmp_video
        if not tmp or not os.path.isfile(tmp):
            raise RuntimeError("녹화 파일 없음")
        if os.path.getsize(tmp) < 1024:
            raise RuntimeError(f"녹화 파일 너무 작음({os.path.getsize(tmp)}B)")
        # ── [정리] stop() 완료 후 내부 참조 해제 ──────────────────────────────
        # ffmpeg 프로세스 핸들, 스레드, 파일 핸들을 명시적으로 None 처리해
        # 참조 카운트를 끊고 GC가 즉시 회수할 수 있게 함.
        self._thread         = None
        self._ffmpeg_proc    = None
        self._ffmpeg_log_fh  = None
        self._ffmpeg_log_path = None
        return tmp

def _save_ffmpeg_log_on_fail(log_path: str, video_path: str):
    try:
        if not log_path or not os.path.isfile(log_path): return
        import shutil
        dst = os.path.join(os.path.dirname(video_path) if video_path else tempfile.gettempdir(), "autosinc_ffmpeg.log")
        shutil.copy2(log_path, dst)
    except Exception:
        pass

# 오디오 병합
def _merge_audio(tmp_video: str, audio_arr, audio_sr: int,
                 audio_ch: int, out_path: str, audio_offset_sec: float = 0.0):
    import shutil, struct
    import numpy as np

    ffmpeg_bin = _find_ffmpeg()
    has_audio  = audio_arr is not None and len(audio_arr) > 0
    if not has_audio or not ffmpeg_bin:
        if tmp_video != out_path:
            shutil.move(tmp_video, out_path)
        return

    tmp_audio = os.path.join(tempfile.gettempdir(), "autosinc_tmp_audio.wav")
    tmp_out   = os.path.join(tempfile.gettempdir(), "autosinc_merge_out.mp4")
    merge_log = os.path.join(tempfile.gettempdir(), "autosinc_merge.log")

    if audio_ch > 1:
        rem = len(audio_arr) % audio_ch
        if rem: audio_arr = audio_arr[:-rem]
        audio_data = audio_arr.reshape(-1, audio_ch)
    else:
        audio_data = audio_arr.reshape(-1, 1)

    pcm       = (audio_data * 32767).clip(-32768, 32767).astype(np.int16)
    num_frames = audio_data.shape[0]
    data_size  = num_frames * audio_ch * 2
    wav_hdr = (b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVEfmt " +
               struct.pack("<IHHIIHH", 16, 1, audio_ch, audio_sr,
                           audio_sr * audio_ch * 2, audio_ch * 2, 16) +
               b"data" + struct.pack("<I", data_size))
    with open(tmp_audio, "wb") as wf:
        wf.write(wav_hdr + pcm.tobytes())
    del pcm, audio_data  # WAV 파일 작성 완료 → int16/float32 배열 즉시 해제

    _offset = round(audio_offset_sec, 4)
    _log(f"[OBS싱크] 최종 오프셋 보정: audio_offset={_offset:.4f}s")
    base_cmd = [ffmpeg_bin, "-y", "-i", tmp_video]
    if _offset >= 0.005:
        base_cmd += ["-itsoffset", str(_offset)]
    elif _offset <= -0.005:
        base_cmd += ["-ss", str(-_offset)]
    cmd = base_cmd + ["-i", tmp_audio, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                      "-shortest", "-movflags", "+faststart", tmp_out]

    with open(merge_log, "wb") as lf:
        proc = _popen_no_window(cmd, stdout=subprocess.DEVNULL, stderr=lf)
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()

    if proc.returncode == 0 and os.path.isfile(tmp_out) and os.path.getsize(tmp_out) > 1024:
        shutil.move(tmp_out, out_path)
        _remove_files(merge_log)
    else:
        _save_ffmpeg_log_on_fail(merge_log, out_path)
        if tmp_video != out_path:
            shutil.move(tmp_video, out_path)

    _remove_files(tmp_audio, tmp_out)

def _save_mp4(tmp_video: str, audio_arr, audio_sr: int,
              audio_ch: int, out_path: str, audio_offset_sec: float = 0.0):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    _merge_audio(tmp_video, audio_arr, audio_sr, audio_ch, out_path, audio_offset_sec)
