"""
gui/record_backend.py -- 오디오/화면 녹화 백엔드

변경사항:
  [기능2] 오버레이를 팟플레이어의 owned window로 설정 → z-order 자동 연동
  [기능3] OBS 방식 싱크 (화면+오디오 동시 시작, ffmpeg로 후합산),
          autosinc_ffmpeg.log 는 실패 시에만 저장
"""
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

import tkinter as tk

# ── 디버그 로거 (내부용, 파일 출력 안 함) ─────────────────────────────────────
_debug_log = []

def _log(msg: str):
    """내부 디버그용 (파일에 쓰지 않음 — 실패 시에만 ffmpeg 로그 보존)."""
    _debug_log.append(msg)
    if len(_debug_log) > 200:
        _debug_log.pop(0)


# ── 팟플레이어 창 영역 ─────────────────────────────────────────────────────────
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


# ── 오버레이 (기능2: 팟플레이어 owned window → z-order 자동 연동) ──────────────
# SetWindowLongPtrW(ov_hwnd, GWLP_HWNDPARENT, pot_hwnd) 로 오버레이의 owner를
# 팟플레이어로 설정하면, OS가 z-order를 자동으로 연동한다.
# → 팟플레이어가 다른 창 뒤로 가면 오버레이도 같이 뒤로 감 (-topmost 불필요)
_active_overlays: list = []

_GWLP_HWNDPARENT = -8   # SetWindowLongPtr 로 owner 설정


def _set_owner(ov_hwnd: int, pot_hwnd: int):
    """오버레이의 Win32 owner를 팟플레이어로 설정."""
    try:
        # 64bit: SetWindowLongPtrW / 32bit: SetWindowLongW — 둘 다 시도
        try:
            ctypes.windll.user32.SetWindowLongPtrW(ov_hwnd, _GWLP_HWNDPARENT, pot_hwnd)
        except AttributeError:
            ctypes.windll.user32.SetWindowLongW(ov_hwnd, _GWLP_HWNDPARENT, pot_hwnd)
    except Exception:
        pass


def _show_overlay(root, message: str, duration_ms: int = 3000):
    """
    팟플레이어 좌상단에 오버레이 표시.
    owner를 팟플레이어로 설정하여 z-order를 OS가 자동 연동.
    팟플레이어가 다른 창 뒤로 가면 오버레이도 같이 뒤로 감.
    """
    rect = _get_potplayer_rect()
    if rect is None:
        return
    px, py, pw, ph = rect
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

        # owner 설정 — Tk 창이 화면에 나타난 뒤 hwnd를 얻어야 함
        if pot_hwnd and _WIN_OK:
            try:
                ov_hwnd = int(ov.wm_frame(), 16)
                if ov_hwnd:
                    _set_owner(ov_hwnd, pot_hwnd)
            except Exception:
                pass

        _active_overlays.append(ov)

        # 팟플레이어 이동 시 오버레이 위치 동기화
        def _track():
            if not _try_exists(ov):
                return
            r = _get_potplayer_rect()
            if r:
                try: ov.geometry(f"+{r[0] + 12}+{r[1] + 12}")
                except Exception: pass
            try: root.after(150, _track)
            except Exception: pass

        root.after(150, _track)

        def _close():
            try: ov.destroy()
            except: pass
            try: _active_overlays.remove(ov)
            except: pass
        root.after(duration_ms, _close)
    except Exception:
        pass
def _try_exists(widget) -> bool:
    try:
        return widget.winfo_exists()
    except Exception:
        return False


def _hide_all_overlays():
    for ov in list(_active_overlays):
        try: ov.withdraw()
        except: pass

def _show_all_overlays():
    for ov in list(_active_overlays):
        try: ov.deiconify()
        except: pass


# ── ffmpeg 유틸 ───────────────────────────────────────────────────────────────
def _find_ffmpeg() -> str:
    import shutil, sys
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = os.path.join(meipass, "ffmpeg.exe")
        if os.path.isfile(p): return p
    for p in [
        os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ffmpeg.exe"),
    ]:
        try:
            if os.path.isfile(p): return p
        except Exception:
            pass
    p = shutil.which("ffmpeg")
    if p: return p
    for c in [r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"]:
        if os.path.isfile(c): return c
    return ""

def _popen_no_window(cmd, **kwargs):
    CREATE_NO_WINDOW = 0x08000000
    kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    return subprocess.Popen(cmd, **kwargs)


# ── WGC 캡처 (PrintWindow 폴백 포함) ──────────────────────────────────────────
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
                sb    = wgi.SoftwareBitmap.create_copy_from_surface_async(f.surface).get()
                buf   = sb.lock_buffer(wgi.BitmapBufferAccessMode.READ)
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


def _printwindow_loop(hwnd, width, height, fps, running_flag, write_frame_cb):
    import numpy as np
    import cv2 as _cv2
    gdi32  = ctypes.windll.gdi32
    user32 = ctypes.windll.user32
    PW_RENDERFULLCONTENT = 0x00000002

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize",        ctypes.c_uint32), ("biWidth",  ctypes.c_int32),
            ("biHeight",      ctypes.c_int32),  ("biPlanes", ctypes.c_uint16),
            ("biBitCount",    ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
            ("biSizeImage",   ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]

    interval = 1.0 / fps
    n = 0
    while running_flag.is_set():
        t0 = time.time()
        try:
            rc = wt.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rc))
            cw = rc.right - rc.left
            ch = rc.bottom - rc.top
            if cw <= 0 or ch <= 0:
                time.sleep(0.1); continue

            hdc_win = user32.GetDC(hwnd)
            hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize    = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth   = cw
            bmi.bmiHeader.biHeight  = -ch
            bmi.bmiHeader.biPlanes  = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0

            pBits = ctypes.c_void_p()
            hbmp  = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), 0,
                                           ctypes.byref(pBits), None, 0)
            old   = gdi32.SelectObject(hdc_mem, hbmp)
            ok    = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
            if not ok:
                user32.PrintWindow(hwnd, hdc_mem, 0)

            buf_size = cw * ch * 4
            raw  = (ctypes.c_uint8 * buf_size).from_address(pBits.value)
            arr  = np.frombuffer(raw, dtype=np.uint8).reshape(ch, cw, 4).copy()
            gdi32.SelectObject(hdc_mem, old)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(hwnd, hdc_win)

            bgr = _cv2.cvtColor(arr, _cv2.COLOR_BGRA2BGR)
            if (cw, ch) != (width, height):
                bgr = _cv2.resize(bgr, (width, height))
            write_frame_cb(bgr)
            n += 1
        except Exception as e:
            _log(f"PrintWindow 프레임 오류: {e}")

        sl = interval - (time.time() - t0)
        if sl > 0:
            time.sleep(sl)


# ── 오디오 캡처 ───────────────────────────────────────────────────────────────
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
                from audio_com import (
                    activate_process_loopback, audio_client_initialize,
                    audio_client_set_event, audio_client_start, audio_client_stop,
                    get_capture_client, get_next_packet_size, get_buffer,
                    release_buffer, _com_release, AUDCLNT_BUFFERFLAGS_SILENT,
                )
                client = activate_process_loopback(pid)
                sr, ch = audio_client_initialize(client)
                recorder._sr = sr
                recorder._ch = ch
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
                                recorder._running = False
                                break
                            if pkt == 0:
                                break
                            data, num_frames, flg = get_buffer(cap)
                            if num_frames > 0:
                                from audio_com import AUDCLNT_BUFFERFLAGS_SILENT as _SIL
                                if not (flg & _SIL) and data.value:
                                    buf = (ct.c_float * (num_frames * ch)).from_address(data.value)
                                    arr = np.frombuffer(buf, dtype=np.float32).copy()
                                else:
                                    arr = np.zeros(num_frames * ch, dtype=np.float32)
                                recorder._frames.append(arr)
                            release_buffer(cap, num_frames)
                finally:
                    try: audio_client_stop(client)
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
        if self._frames:
            import numpy as np
            arr = np.concatenate(self._frames)
            return arr, self._sr, self._ch
        return None, self._sr, self._ch


# ── 화면 녹화 + 실시간 ffmpeg 인코딩 (기능3: OBS 방식) ─────────────────────────
class _ScreenRecorder:
    """
    OBS 방식: ffmpeg를 녹화 시작 시점에 즉시 기동하여 pipe:0으로 실시간 인코딩.
    오디오는 별도 _AudioRecorder로 캡처 후 정지 시 ffmpeg로 합산.
    autosinc_ffmpeg.log는 실패 시에만 보존 (성공 시 삭제).
    """
    def __init__(self):
        self._running_flag  = threading.Event()
        self._thread        = None
        self._fps           = 30
        self._size          = (1280, 720)
        self._hwnd          = None
        self._ffmpeg_proc   = None
        self._ffmpeg_log_path = None
        self._ffmpeg_log_fh = None
        self._tmp_video     = None

    def start(self, fps=30, root=None, out_path=None):
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

        ffmpeg_bin = _find_ffmpeg()
        if not ffmpeg_bin:
            raise RuntimeError("ffmpeg를 찾을 수 없습니다.")

        self._tmp_video = os.path.join(tempfile.gettempdir(), "autosinc_live_video.mp4")
        # 로그는 임시 파일 — 성공 시 삭제
        self._ffmpeg_log_path = os.path.join(tempfile.gettempdir(), "autosinc_ffmpeg.log")

        for p in [self._tmp_video, self._ffmpeg_log_path]:
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

        try:
            self._ffmpeg_log_fh = open(self._ffmpeg_log_path, "wb")
            self._ffmpeg_proc   = _popen_no_window(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self._ffmpeg_log_fh,
            )
        except Exception as e:
            raise RuntimeError(f"ffmpeg 실행 실패: {e}")

        self._running_flag.set()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _write_frame(self, bgr_frame):
        if self._ffmpeg_proc and self._ffmpeg_proc.stdin:
            try:
                self._ffmpeg_proc.stdin.write(bgr_frame.tobytes())
            except (BrokenPipeError, OSError):
                self._running_flag.clear()

    def _loop(self):
        try:
            _wgc_capture_hwnd(
                self._hwnd, self._size[0], self._size[1],
                self._fps, self._running_flag, self._write_frame)
        except Exception as e:
            _log(f"캡처 루프 예외: {e}")
        finally:
            if self._ffmpeg_proc and self._ffmpeg_proc.stdin:
                try: self._ffmpeg_proc.stdin.close()
                except Exception: pass

    def stop(self) -> str:
        """녹화 정지. 성공 시 ffmpeg 로그 삭제. 실패 시 로그 보존."""
        self._running_flag.clear()
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                if self._ffmpeg_proc and self._ffmpeg_proc.stdin:
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
                # 실패 시 로그 보존 (저장 폴더로 복사)
                _save_ffmpeg_log_on_fail(self._ffmpeg_log_path, self._tmp_video)
                raise RuntimeError(
                    f"ffmpeg 인코딩 실패 (code={rc})\n"
                    f"로그: {self._ffmpeg_log_path}")
            else:
                # 성공 시 로그 삭제
                try: os.remove(self._ffmpeg_log_path)
                except: pass

        tmp = self._tmp_video
        if not tmp or not os.path.isfile(tmp):
            raise RuntimeError("녹화 파일 없음")
        if os.path.getsize(tmp) < 1024:
            raise RuntimeError(f"녹화 파일 너무 작음({os.path.getsize(tmp)}B)")
        return tmp


def _save_ffmpeg_log_on_fail(log_path: str, video_path: str):
    """실패 시 ffmpeg 로그를 저장 폴더에 복사."""
    try:
        if not log_path or not os.path.isfile(log_path):
            return
        dst_dir = os.path.dirname(video_path) if video_path else tempfile.gettempdir()
        dst = os.path.join(dst_dir, "autosinc_ffmpeg.log")
        import shutil
        shutil.copy2(log_path, dst)
    except Exception:
        pass


# ── 오디오 병합 (기능3: OBS 방식 — 화면+오디오 병렬 녹화 후 ffmpeg 합산) ────────
def _merge_audio(tmp_video: str, audio_arr, audio_sr: int,
                 audio_ch: int, out_path: str):
    import shutil
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

    import wave
    if audio_ch > 1:
        rem = len(audio_arr) % audio_ch
        if rem: audio_arr = audio_arr[:-rem]
        audio_data = audio_arr.reshape(-1, audio_ch)
    else:
        audio_data = audio_arr.reshape(-1, 1)
    pcm = (audio_data * 32767).clip(-32768, 32767).astype(np.int16)
    with wave.open(tmp_audio, "wb") as wf:
        wf.setnchannels(audio_ch)
        wf.setsampwidth(2)
        wf.setframerate(audio_sr)
        wf.writeframes(pcm.tobytes())

    cmd = [
        ffmpeg_bin, "-y",
        "-i", tmp_video, "-i", tmp_audio,
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart",
        tmp_out,
    ]
    with open(merge_log, "wb") as lf:
        proc = _popen_no_window(cmd, stdout=subprocess.DEVNULL, stderr=lf)
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()

    if proc.returncode == 0 and os.path.isfile(tmp_out) and os.path.getsize(tmp_out) > 1024:
        shutil.move(tmp_out, out_path)
        # 병합 성공 시 로그 삭제
        try: os.remove(merge_log)
        except: pass
    else:
        # 실패 시 로그를 저장 폴더로 복사
        _save_ffmpeg_log_on_fail(merge_log, out_path)
        if tmp_video != out_path:
            shutil.move(tmp_video, out_path)

    for p in [tmp_audio, tmp_out]:
        try: os.remove(p)
        except: pass


def _save_mp4(tmp_video: str, audio_arr, audio_sr: int,
              audio_ch: int, out_path: str):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    _merge_audio(tmp_video, audio_arr, audio_sr, audio_ch, out_path)
