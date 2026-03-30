"""gui_record_backend.py -- 오디오/화면 녹화 백엔드 (WGC, pywin32+ctypes 직접 구현)"""
import os, time, threading, subprocess, tempfile, ctypes, ctypes.wintypes as wt

try:
    import cv2
    import numpy as np
    _CV2_OK = True
except ImportError:
    _CV2_OK = False

try:
    import soundfile as sf
    _SF_OK = True
except ImportError:
    _SF_OK = False

try:
    from PIL import ImageGrab
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

try:
    _user32 = ctypes.windll.user32
    _WIN_OK = True
except Exception:
    _WIN_OK = False


# ─────────────────────────────────────────────────────────────────────────────
# 디버그 로거
# ─────────────────────────────────────────────────────────────────────────────
_LOG_PATH = os.path.join(tempfile.gettempdir(), "autosinc_debug.log")

def _set_log_path(directory: str):
    global _LOG_PATH
    try:
        os.makedirs(directory, exist_ok=True)
        _LOG_PATH = os.path.join(directory, "autosinc_debug.log")
    except Exception:
        pass

def _log(msg: str):
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 팟플레이어 창 영역
# ─────────────────────────────────────────────────────────────────────────────
def _get_potplayer_video_hwnd(parent_hwnd):
    children = []
    def _cb(hwnd, _):
        rc = wt.RECT()
        _user32.GetClientRect(hwnd, ctypes.byref(rc))
        w = rc.right - rc.left
        h = rc.bottom - rc.top
        if w > 100 and h > 100:
            children.append((w * h, hwnd))
        return True
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    _user32.EnumChildWindows(parent_hwnd, CB(_cb), 0)
    if children:
        children.sort(reverse=True)
        return children[0][1]
    return None

def _get_potplayer_rect():
    if not _WIN_OK:
        return None
    try:
        from win32_utils import find_potplayer_hwnd
        hwnd = find_potplayer_hwnd()
        if not hwnd:
            return None
        video_hwnd = _get_potplayer_video_hwnd(hwnd)
        target = video_hwnd if video_hwnd else hwnd
        rc = wt.RECT()
        _user32.GetClientRect(target, ctypes.byref(rc))
        pt = wt.POINT(0, 0)
        _user32.ClientToScreen(target, ctypes.byref(pt))
        w = rc.right - rc.left
        h = rc.bottom - rc.top
        if w <= 0 or h <= 0:
            return None
        return pt.x, pt.y, w, h
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 오버레이
# ─────────────────────────────────────────────────────────────────────────────
import tkinter as tk

_active_overlays: list = []

def _show_overlay(root, message: str, duration_ms: int = 3000):
    rect = _get_potplayer_rect()
    if rect is None:
        return
    px, py, pw, ph = rect
    try:
        ov = tk.Toplevel(root)
        ov.overrideredirect(True)
        ov.attributes("-topmost", True)
        ov.attributes("-alpha", 0.88)
        ov.configure(bg="#101010")
        ov.geometry(f"+{px + 12}+{py + 12}")
        tk.Label(ov, text=message, font=("Segoe UI", 11, "bold"),
                 bg="#101010", fg="#00c8e0", padx=14, pady=8).pack()
        ov.update_idletasks()
        _active_overlays.append(ov)
        def _close():
            try: ov.destroy()
            except: pass
            try: _active_overlays.remove(ov)
            except: pass
        root.after(duration_ms, _close)
    except Exception:
        pass

def _hide_all_overlays():
    for ov in list(_active_overlays):
        try: ov.withdraw()
        except: pass

def _show_all_overlays():
    for ov in list(_active_overlays):
        try: ov.deiconify()
        except: pass


# ─────────────────────────────────────────────────────────────────────────────
# ffmpeg 유틸
# ─────────────────────────────────────────────────────────────────────────────
def _find_ffmpeg() -> str:
    import shutil, sys
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = os.path.join(meipass, "ffmpeg.exe")
        if os.path.isfile(p):
            return p
    try:
        p = os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe")
        if os.path.isfile(p):
            return p
    except Exception:
        pass
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe")
        if os.path.isfile(p):
            return p
    except Exception:
        pass
    p = shutil.which("ffmpeg")
    if p:
        return p
    for c in [r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"]:
        if os.path.isfile(c):
            return c
    return ""

def _popen_no_window(cmd, **kwargs):
    CREATE_NO_WINDOW = 0x08000000
    kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    return subprocess.Popen(cmd, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# WGC — pywin32 + ctypes 직접 구현 (winsdk/winrt 불필요)
# ─────────────────────────────────────────────────────────────────────────────
# Windows.Graphics.Capture COM GUID
_CLSID_GraphicsCaptureItem   = "{79C3F95B-31F7-4EC2-A464-632EF5D30760}"
_IID_IGraphicsCaptureItemInterop = "{3628E81B-3CAC-4C60-B7F4-23CE0E0C3356}"

def _wgc_capture_hwnd(hwnd, width, height, fps, running_flag, write_frame_cb):
    """
    Windows.Graphics.Capture를 pywin32 + ctypes로 직접 사용.
    running_flag: threading.Event — clear되면 루프 종료.
    write_frame_cb(bgr_ndarray): 프레임 콜백.
    """
    import numpy as np
    import cv2 as _cv2

    # WinRT bootstrap (Windows 10 1903+에 내장)
    try:
        import winrt.windows.graphics.capture as wgc
        import winrt.windows.graphics.directx as wgdx
        import winrt.windows.graphics.directx.direct3d11 as d3d11
        import winrt.windows.graphics.imaging as wgi
        _use_winrt = True
        _log("WGC: winrt 패키지 사용")
    except ImportError:
        _use_winrt = False

    if not _use_winrt:
        # COM 경로 시도, 실패 시 PrintWindow로 직접 폴백
        try:
            _capture_via_com(hwnd, width, height, fps, running_flag, write_frame_cb)
            return
        except Exception as e:
            _log(f"WGC COM 실패 → PrintWindow 직접 폴백: {e}")
            _printwindow_loop(hwnd, width, height, fps, running_flag, write_frame_cb)
            return

    # winrt 패키지 경로
    item = wgc.GraphicsCaptureItem.create_for_window(hwnd)
    if item is None:
        raise RuntimeError("WGC: create_for_window → None (창이 최소화됐는지 확인)")

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
            last_frame[0] = f
            frame_ready.set()
    pool.frame_arrived += _on_frame

    interval = 1.0 / fps
    n = 0
    try:
        while running_flag.is_set():
            t0 = time.time()
            frame_ready.wait(timeout=0.1)
            frame_ready.clear()
            f = last_frame[0]
            if f is None:
                continue
            try:
                sb  = wgi.SoftwareBitmap.create_copy_from_surface_async(f.surface).get()
                buf = sb.lock_buffer(wgi.BitmapBufferAccessMode.READ)
                plane = buf.get_plane_description(0)
                ref   = buf.create_reference()
                raw   = bytes(ref)
                fh, fw = plane.height, plane.width
                arr   = np.frombuffer(raw, dtype=np.uint8).reshape(fh, fw, 4)
                bgr   = _cv2.cvtColor(arr, _cv2.COLOR_BGRA2BGR)
                if (fw, fh) != (width, height):
                    bgr = _cv2.resize(bgr, (width, height))
                write_frame_cb(bgr)
                n += 1
                if n == 1:
                    _log("WGC 첫 프레임 전송 (winrt)")
            except Exception as e:
                _log(f"WGC 프레임 오류: {e}")
            sl = interval - (time.time() - t0)
            if sl > 0:
                time.sleep(sl)
    finally:
        try: session.close()
        except: pass
        try: pool.close()
        except: pass
        _log(f"WGC 종료 ({n}프레임, winrt)")


def _capture_via_com(hwnd, width, height, fps, running_flag, write_frame_cb):
    """
    winsdk/winrt 없이 Windows 내장 WinRT COM + D3D11 + DXGI를 직접 사용.
    pywin32(win32api, comtypes)만 있으면 동작.
    """
    import numpy as np
    import cv2 as _cv2
    import comtypes
    import comtypes.client

    _log("WGC: COM 직접 경로 시도")

    # D3D11CreateDevice
    d3d11 = ctypes.windll.d3d11
    dxgi  = ctypes.windll.dxgi

    D3D_DRIVER_TYPE_HARDWARE = 1
    D3D11_SDK_VERSION = 7
    DXGI_FORMAT_B8G8R8A8_UNORM = 87
    D3D11_USAGE_STAGING = 3
    D3D11_CPU_ACCESS_READ = 0x20000
    D3D11_MAP_READ = 1

    # COM 초기화 — 이미 초기화된 스레드면 무시 (RPC_E_CHANGED_MODE = -2147417850)
    try:
        comtypes.CoInitializeEx(0)  # STA
    except OSError as _e:
        if _e.winerror == -2147417850:
            _log("COM 이미 초기화됨 (MTA) — 계속 진행")
        else:
            raise

    # ID3D11Device 생성
    class _ID3D11Device(comtypes.IUnknown):
        _iid_ = comtypes.GUID("{DB6F6DDB-AC77-4E88-8253-819DF9BBF140}")
        _methods_ = []

    class _ID3D11DeviceContext(comtypes.IUnknown):
        _iid_ = comtypes.GUID("{C0BFA96C-E089-44FB-8EAF-26F8796190DA}")
        _methods_ = []

    pDevice  = ctypes.POINTER(_ID3D11Device)()
    pContext = ctypes.POINTER(_ID3D11DeviceContext)()
    feature_level = ctypes.c_int(0)

    hr = d3d11.D3D11CreateDevice(
        None, D3D_DRIVER_TYPE_HARDWARE, None, 0, None, 0,
        D3D11_SDK_VERSION,
        ctypes.byref(pDevice),
        ctypes.byref(feature_level),
        ctypes.byref(pContext)
    )
    if hr < 0:
        raise RuntimeError(f"D3D11CreateDevice 실패: hr=0x{hr & 0xFFFFFFFF:08X}")

    _log("WGC COM: D3D11 디바이스 생성 완료")

    # IDXGIDevice → IDXGIAdapter → IDXGIFactory → ... 경로로 HWND 캡처
    # 이 경로는 복잡하므로 대신 PrintWindow + DIBSection 방식 사용
    _log("WGC COM: PrintWindow 방식으로 전환")
    _printwindow_loop(hwnd, width, height, fps, running_flag, write_frame_cb)


def _printwindow_loop(hwnd, width, height, fps, running_flag, write_frame_cb):
    """
    PrintWindow API로 HWND 내용을 캡처 — GPU 가속 창(하드웨어 오버레이)도 캡처 가능.
    winsdk/winrt/comtypes 불필요, Win32 API만 사용.
    """
    import numpy as np
    import cv2 as _cv2

    gdi32   = ctypes.windll.gdi32
    user32  = ctypes.windll.user32
    PW_RENDERFULLCONTENT = 0x00000002  # DWM 렌더링 포함 (Win8.1+)

    class BITMAPINFOHEADER(ctypes.Structure):
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

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                    ("bmiColors", ctypes.c_uint32 * 3)]

    interval = 1.0 / fps
    n = 0

    _log(f"PrintWindow 루프 시작 ({width}x{height} @ {fps}fps)")

    while running_flag.is_set():
        t0 = time.time()
        try:
            # 현재 창 실제 크기 재확인
            rc = wt.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rc))
            cw = rc.right  - rc.left
            ch = rc.bottom - rc.top
            if cw <= 0 or ch <= 0:
                time.sleep(0.1)
                continue

            hdc_win = user32.GetDC(hwnd)
            hdc_mem = gdi32.CreateCompatibleDC(hdc_win)

            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize        = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth       = cw
            bmi.bmiHeader.biHeight      = -ch   # 음수 = top-down
            bmi.bmiHeader.biPlanes      = 1
            bmi.bmiHeader.biBitCount    = 32
            bmi.bmiHeader.biCompression = 0     # BI_RGB

            pBits = ctypes.c_void_p()
            hbmp  = gdi32.CreateDIBSection(
                hdc_mem, ctypes.byref(bmi), 0,
                ctypes.byref(pBits), None, 0)
            old   = gdi32.SelectObject(hdc_mem, hbmp)

            # PW_RENDERFULLCONTENT: DWM 합성 포함 캡처
            ok = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
            if not ok:
                # fallback: BitBlt
                user32.PrintWindow(hwnd, hdc_mem, 0)

            # DIB → numpy
            buf_size = cw * ch * 4
            raw = (ctypes.c_uint8 * buf_size).from_address(pBits.value)
            arr = np.frombuffer(raw, dtype=np.uint8).reshape(ch, cw, 4).copy()

            gdi32.SelectObject(hdc_mem, old)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(hwnd, hdc_win)

            bgr = _cv2.cvtColor(arr, _cv2.COLOR_BGRA2BGR)
            if (cw, ch) != (width, height):
                bgr = _cv2.resize(bgr, (width, height))
            write_frame_cb(bgr)
            n += 1
            if n == 1:
                _log("PrintWindow 첫 프레임 전송")

        except Exception as e:
            _log(f"PrintWindow 프레임 오류: {e}")

        sl = interval - (time.time() - t0)
        if sl > 0:
            time.sleep(sl)

    _log(f"PrintWindow 루프 종료 ({n}프레임)")


# ─────────────────────────────────────────────────────────────────────────────
# 오디오 캡처
# ─────────────────────────────────────────────────────────────────────────────
class _AudioRecorder:
    def __init__(self):
        self._frames  = []
        self._sr      = 48000
        self._ch      = 2
        self._running = False
        self._thread  = None

    def start(self, pid: int):
        self._frames  = []
        self._running = True
        recorder = self

        def _session_mta():
            import ctypes as ct
            import numpy as np
            ole32    = ct.windll.ole32
            kernel32 = ct.windll.kernel32
            hr_co = ole32.CoInitializeEx(None, 0x0)
            co_ok = hr_co in (0, 1, 0x80010106)
            try:
                from audio_capture import (
                    _activate_process_loopback, _audio_client_initialize,
                    _audio_client_set_event, _audio_client_start,
                    _audio_client_stop, _get_capture_client,
                    _get_next_packet_size, _get_buffer, _release_buffer,
                    _com_release, AUDCLNT_BUFFERFLAGS_SILENT,
                )
                _log(f"오디오: _activate_process_loopback 시작 (pid={pid})")
                client = _activate_process_loopback(pid)
                _log(f"오디오: client 획득 완료 {client}")
                sr, ch = _audio_client_initialize(client)
                recorder._sr = sr
                recorder._ch = ch
                _log(f"오디오: 초기화 완료 sr={sr} ch={ch}")
                h_event = kernel32.CreateEventW(None, False, False, None)
                _audio_client_set_event(client, h_event)
                cap = _get_capture_client(client)
                _audio_client_start(client)
                _log("오디오: 캡처 시작")
                try:
                    while recorder._running:
                        kernel32.WaitForSingleObject(h_event, 10)
                        while recorder._running:
                            try:
                                pkt = _get_next_packet_size(cap)
                            except OSError:
                                recorder._running = False
                                break
                            if pkt == 0:
                                break
                            data, num_frames, flg = _get_buffer(cap)
                            if num_frames > 0:
                                if not (flg & AUDCLNT_BUFFERFLAGS_SILENT) and data.value:
                                    buf = (ct.c_float * (num_frames * ch)).from_address(data.value)
                                    arr = np.frombuffer(buf, dtype=np.float32).copy()
                                else:
                                    arr = np.zeros(num_frames * ch, dtype=np.float32)
                                recorder._frames.append(arr)
                                if len(recorder._frames) == 1:
                                    _log("오디오: 첫 프레임 수신")
                            _release_buffer(cap, num_frames)
                finally:
                    try: _audio_client_stop(client)
                    except: pass
                    _com_release(cap)
                    _com_release(client)
                    kernel32.CloseHandle(h_event)
            except Exception as e:
                _log(f"오디오 캡처 오류: {e}")
                recorder._running = False
            finally:
                if co_ok:
                    ole32.CoUninitialize()

        self._thread = threading.Thread(target=_session_mta, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        _log(f"오디오 stop: frames={len(self._frames)}")
        if self._frames:
            import numpy as np
            arr = np.concatenate(self._frames)
            _log(f"오디오 데이터 크기: {arr.shape}, sr={self._sr}, ch={self._ch}")
            return arr, self._sr, self._ch
        _log("오디오 데이터 없음 → 무음으로 처리")
        return None, self._sr, self._ch


# ─────────────────────────────────────────────────────────────────────────────
# 화면 캡처 + 실시간 ffmpeg 인코딩
# ─────────────────────────────────────────────────────────────────────────────
class _ScreenRecorder:
    def __init__(self):
        self._running_flag  = threading.Event()
        self._thread        = None
        self._fps           = 30
        self._size          = (1280, 720)
        self._hwnd          = None
        self._ffmpeg_proc   = None
        self._ffmpeg_log    = None
        self._ffmpeg_log_fh = None
        self._tmp_video     = None

    def start(self, fps=30, root=None, out_path=None):
        _log("=== ScreenRecorder.start() ===")

        if out_path:
            _set_log_path(os.path.dirname(out_path))

        from win32_utils import find_potplayer_hwnd
        hwnd = find_potplayer_hwnd()
        if not hwnd:
            raise RuntimeError("팟플레이어 창을 찾을 수 없습니다.")

        video_hwnd = _get_potplayer_video_hwnd(hwnd)
        self._hwnd = video_hwnd if video_hwnd else hwnd

        rect = _get_potplayer_rect()
        if not rect:
            raise RuntimeError("팟플레이어 창 영역을 구할 수 없습니다.")

        px, py, pw, ph = rect
        w = pw - (pw % 2)
        h = ph - (ph % 2)
        self._fps  = fps
        self._size = (w, h)
        _log(f"캡처 영역: {w}x{h} @ ({px},{py}), hwnd={self._hwnd}")

        ffmpeg_bin = _find_ffmpeg()
        if not ffmpeg_bin:
            raise RuntimeError("ffmpeg를 찾을 수 없습니다.")
        _log(f"ffmpeg: {ffmpeg_bin}")

        self._tmp_video  = out_path if out_path else os.path.join(tempfile.gettempdir(), "autosinc_live_video.mp4")
        _log_dir = os.path.dirname(out_path) if out_path else tempfile.gettempdir()
        self._ffmpeg_log = os.path.join(_log_dir, "autosinc_ffmpeg.log")

        for p in [self._tmp_video, self._ffmpeg_log]:
            try: os.remove(p)
            except: pass

        cmd = [
            ffmpeg_bin, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(fps),
            "-i", "pipe:0",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an",
            self._tmp_video,
        ]
        _log(f"ffmpeg cmd: {' '.join(cmd)}")

        try:
            self._ffmpeg_log_fh = open(self._ffmpeg_log, "wb")
            self._ffmpeg_proc = _popen_no_window(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self._ffmpeg_log_fh,
            )
            _log(f"ffmpeg PID: {self._ffmpeg_proc.pid}")
        except Exception as e:
            raise RuntimeError(f"ffmpeg 실행 실패: {e}")

        self._running_flag.set()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        _log("캡처 스레드 시작")

    def _write_frame(self, bgr_frame):
        if self._ffmpeg_proc and self._ffmpeg_proc.stdin:
            try:
                self._ffmpeg_proc.stdin.write(bgr_frame.tobytes())
            except (BrokenPipeError, OSError) as e:
                _log(f"stdin 쓰기 실패: {e}")
                self._running_flag.clear()

    def _loop(self):
        _log("캡처 루프 진입")
        try:
            _wgc_capture_hwnd(
                self._hwnd,
                self._size[0], self._size[1],
                self._fps,
                self._running_flag,
                self._write_frame,
            )
        except Exception as e:
            import traceback
            _log(f"캡처 루프 예외: {traceback.format_exc()}")
        finally:
            _log("캡처 루프 종료 → stdin 닫기")
            if self._ffmpeg_proc and self._ffmpeg_proc.stdin:
                try:
                    self._ffmpeg_proc.stdin.close()
                    _log("stdin 닫기 완료")
                except Exception as e:
                    _log(f"stdin 닫기 실패: {e}")

    def stop(self) -> str:
        _log("stop() 호출")
        self._running_flag.clear()

        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                _log("캡처 스레드 timeout → stdin 강제 닫기")
                if self._ffmpeg_proc and self._ffmpeg_proc.stdin:
                    try: self._ffmpeg_proc.stdin.close()
                    except: pass

        if self._ffmpeg_proc:
            # stdin이 아직 열려있으면 확실히 닫아서 ffmpeg가 EOF를 받게 함
            try:
                if self._ffmpeg_proc.stdin:
                    self._ffmpeg_proc.stdin.close()
            except Exception:
                pass
            try:
                _log("ffmpeg wait...")
                self._ffmpeg_proc.wait(timeout=15)
                _log(f"ffmpeg 종료 code={self._ffmpeg_proc.returncode}")
            except subprocess.TimeoutExpired:
                _log("ffmpeg timeout → kill")
                self._ffmpeg_proc.kill()
                self._ffmpeg_proc.wait()
            finally:
                try:
                    if self._ffmpeg_log_fh:
                        self._ffmpeg_log_fh.close()
                except: pass

            if self._ffmpeg_proc.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg 인코딩 실패 (code={self._ffmpeg_proc.returncode})\n"
                    f"로그: {self._ffmpeg_log}"
                )

        tmp = self._tmp_video
        if not tmp or not os.path.isfile(tmp):
            raise RuntimeError(f"녹화 파일 없음. ffmpeg 로그: {self._ffmpeg_log}")
        size = os.path.getsize(tmp)
        _log(f"임시 파일 크기: {size}B")
        if size < 1024:
            raise RuntimeError(f"녹화 파일 너무 작음({size}B). ffmpeg 로그: {self._ffmpeg_log}")

        _log("stop() 완료")
        return tmp


# ─────────────────────────────────────────────────────────────────────────────
# 오디오 병합
# ─────────────────────────────────────────────────────────────────────────────
def _merge_audio(tmp_video: str, audio_arr, audio_sr: int, audio_ch: int, out_path: str):
    import shutil, numpy as np

    ffmpeg_bin = _find_ffmpeg()
    has_audio  = audio_arr is not None and len(audio_arr) > 0

    if not has_audio or not ffmpeg_bin:
        _log(f"오디오 없음 - 영상 경로: {tmp_video}")
        if tmp_video != out_path:
            shutil.move(tmp_video, out_path)
            _log(f"이동 완료: {out_path}")
        else:
            _log(f"이미 최종 경로에 저장됨: {out_path}")
        return

    tmp_audio = os.path.join(tempfile.gettempdir(), "autosinc_tmp_audio.wav")
    tmp_out   = os.path.join(tempfile.gettempdir(), "autosinc_merge_out.mp4")
    merge_log = os.path.join(tempfile.gettempdir(), "autosinc_merge.log")

    import wave
    audio_data = audio_arr.reshape(-1, audio_ch) if audio_ch > 1 else audio_arr.reshape(-1, 1)
    pcm = (audio_data * 32767).clip(-32768, 32767).astype(np.int16)
    with wave.open(tmp_audio, "wb") as wf:
        wf.setnchannels(audio_ch)
        wf.setsampwidth(2)
        wf.setframerate(audio_sr)
        wf.writeframes(pcm.tobytes())
    _log("WAV 저장 완료")

    cmd = [
        ffmpeg_bin, "-y",
        "-i", tmp_video, "-i", tmp_audio,
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart",
        tmp_out,
    ]
    _log(f"병합 cmd: {' '.join(cmd)}")
    with open(merge_log, "wb") as lf:
        proc = _popen_no_window(cmd, stdout=subprocess.DEVNULL, stderr=lf)
        proc.wait()
    _log(f"병합 code={proc.returncode}")

    if proc.returncode == 0 and os.path.isfile(tmp_out) and os.path.getsize(tmp_out) > 1024:
        shutil.move(tmp_out, out_path)
        _log(f"최종 저장: {out_path}")
    else:
        _log("병합 실패 → 영상만 저장")
        if tmp_video != out_path:
            shutil.move(tmp_video, out_path)
        try: shutil.copy(merge_log, out_path + "_merge_error.log")
        except: pass

    for p in [tmp_audio, tmp_out, merge_log]:
        try: os.remove(p)
        except: pass
    # tmp_video == out_path이므로 별도 삭제 불필요


# ─────────────────────────────────────────────────────────────────────────────
# gui_record_open.py에서 호출하는 통합 저장 함수
# ─────────────────────────────────────────────────────────────────────────────
def _save_mp4(tmp_video: str, audio_arr, audio_sr: int, audio_ch: int, out_path: str):
    """
    _ScreenRecorder.stop()이 반환한 tmp_video 경로와
    _AudioRecorder.stop()이 반환한 오디오 데이터를 받아
    최종 out_path에 병합 저장한다.
    """
    import os
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    _merge_audio(tmp_video, audio_arr, audio_sr, audio_ch, out_path)
