"""gui_record_backend.py -- 오디오/화면 녹화 백엔드 (WGC 전용)"""
import os, time, threading, subprocess, tempfile

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
    import ctypes as _ct
    import ctypes.wintypes as _wt
    _user32 = _ct.windll.user32
    _WIN_OK = True
except Exception:
    _WIN_OK = False


# ─────────────────────────────────────────────────────────────────────────────
# 디버그 로거
# ─────────────────────────────────────────────────────────────────────────────
# 기본값: 임시 폴더. ScreenRecorder.start() 호출 시 저장 디렉토리로 변경됨.
_LOG_PATH = os.path.join(tempfile.gettempdir(), "autosinc_debug.log")

def _set_log_path(directory: str):
    """로그 파일 경로를 지정 디렉토리로 변경한다."""
    global _LOG_PATH
    try:
        os.makedirs(directory, exist_ok=True)
        _LOG_PATH = os.path.join(directory, "autosinc_debug.log")
    except Exception:
        pass  # 실패 시 기존 경로 유지

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
        rc = _wt.RECT()
        _user32.GetClientRect(hwnd, _ct.byref(rc))
        w = rc.right - rc.left
        h = rc.bottom - rc.top
        if w > 100 and h > 100:
            children.append((w * h, hwnd))
        return True
    CB = _ct.WINFUNCTYPE(_ct.c_bool, _ct.c_void_p, _ct.c_void_p)
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
        rc = _wt.RECT()
        _user32.GetClientRect(target, _ct.byref(rc))
        pt = _wt.POINT(0, 0)
        _user32.ClientToScreen(target, _ct.byref(pt))
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
    for c in [r"C:\ffmpeg\bin\ffmpeg.exe",
               r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"]:
        if os.path.isfile(c):
            return c
    return ""

def _popen_no_window(cmd, **kwargs):
    CREATE_NO_WINDOW = 0x08000000
    kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    return subprocess.Popen(cmd, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# WGC 임포트 헬퍼 (winsdk 또는 winrt 둘 중 하나 자동 선택)
# ─────────────────────────────────────────────────────────────────────────────
def _import_wgc():
    """
    winsdk 또는 winrt 패키지에서 WGC 관련 모듈을 임포트합니다.
    성공하면 (GraphicsCaptureItem, Direct3D11CaptureFramePool,
               DirectXPixelFormat, create_direct3d_device,
               BitmapBufferAccessMode, SoftwareBitmap) 튜플 반환.
    둘 다 없으면 ImportError 발생.
    """
    try:
        from winsdk.windows.graphics.capture import (
            GraphicsCaptureItem, Direct3D11CaptureFramePool)
        from winsdk.windows.graphics.directx import DirectXPixelFormat
        from winsdk.windows.graphics.directx.direct3d11 import create_direct3d_device
        from winsdk.windows.graphics.imaging import BitmapBufferAccessMode, SoftwareBitmap
        return (GraphicsCaptureItem, Direct3D11CaptureFramePool,
                DirectXPixelFormat, create_direct3d_device,
                BitmapBufferAccessMode, SoftwareBitmap)
    except ImportError:
        pass

    try:
        from winrt.windows.graphics.capture import (
            GraphicsCaptureItem, Direct3D11CaptureFramePool)
        from winrt.windows.graphics.directx import DirectXPixelFormat
        from winrt.windows.graphics.directx.direct3d11 import create_direct3d_device
        from winrt.windows.graphics.imaging import BitmapBufferAccessMode, SoftwareBitmap
        return (GraphicsCaptureItem, Direct3D11CaptureFramePool,
                DirectXPixelFormat, create_direct3d_device,
                BitmapBufferAccessMode, SoftwareBitmap)
    except ImportError:
        pass

    raise ImportError(
        "WGC를 사용하려면 'winsdk' 또는 'winrt' 패키지가 필요합니다.\n"
        "설치: pip install winsdk\n"
        "또는: pip install winrt"
    )


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
                client = _activate_process_loopback(pid)
                sr, ch = _audio_client_initialize(client)
                recorder._sr = sr
                recorder._ch = ch
                h_event = kernel32.CreateEventW(None, False, False, None)
                _audio_client_set_event(client, h_event)
                cap = _get_capture_client(client)
                _audio_client_start(client)
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
            self._thread.join(timeout=10)
        if self._frames:
            import numpy as np
            return np.concatenate(self._frames), self._sr, self._ch
        return None, self._sr, self._ch


# ─────────────────────────────────────────────────────────────────────────────
# 화면 캡처 + 실시간 ffmpeg 인코딩 (WGC 전용)
# ─────────────────────────────────────────────────────────────────────────────
class _ScreenRecorder:
    def __init__(self):
        self._running       = False
        self._thread        = None
        self._fps           = 30
        self._size          = (1280, 720)
        self._hwnd          = None
        self._root          = None
        self._ffmpeg_proc   = None
        self._ffmpeg_log    = None
        self._ffmpeg_log_fh = None
        self._tmp_video     = None
        self._out_path      = None

    def start(self, fps=30, root=None, out_path=None):
        # 로그를 저장 디렉토리에 기록 (out_path가 있을 때)
        if out_path:
            _set_log_path(os.path.dirname(out_path))
        _log("=== ScreenRecorder.start() ===")

        # WGC 패키지 사전 확인 — 없으면 즉시 명확한 오류
        try:
            _import_wgc()
        except ImportError as e:
            _log(f"WGC 패키지 없음: {e}")
            raise RuntimeError(str(e))

        from win32_utils import find_potplayer_hwnd
        hwnd = find_potplayer_hwnd()
        if not hwnd:
            raise RuntimeError("팟플레이어 창을 찾을 수 없습니다.")

        video_hwnd = _get_potplayer_video_hwnd(hwnd)
        self._hwnd = video_hwnd if video_hwnd else hwnd
        self._root = root

        rect = _get_potplayer_rect()
        if not rect:
            raise RuntimeError("팟플레이어 창 영역을 구할 수 없습니다.")

        px, py, pw, ph = rect
        w = pw - (pw % 2)
        h = ph - (ph % 2)
        self._fps    = fps
        self._size   = (w, h)
        self._out_path = out_path
        _log(f"캡처 영역: {w}x{h} @ ({px},{py})")

        ffmpeg_bin = _find_ffmpeg()
        if not ffmpeg_bin:
            raise RuntimeError("ffmpeg를 찾을 수 없습니다.")
        _log(f"ffmpeg: {ffmpeg_bin}")

        self._tmp_video  = os.path.join(tempfile.gettempdir(), "autosinc_live_video.mp4")
        # ffmpeg 로그: out_path 디렉토리 우선, 없으면 임시 폴더
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
            _log(f"ffmpeg 실행 실패: {e}")
            raise RuntimeError(f"ffmpeg 실행 실패: {e}")

        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        _log("캡처 스레드 시작")

    # ── WGC 캡처 루프 ─────────────────────────────────────────────────────────
    def _wgc_loop(self):
        _log("WGC 루프 시작")
        try:
            import numpy as np
            import cv2 as _cv2
        except ImportError as e:
            raise RuntimeError(f"numpy/cv2 없음: {e}")

        (GraphicsCaptureItem, Direct3D11CaptureFramePool,
         DirectXPixelFormat, create_direct3d_device,
         BitmapBufferAccessMode, SoftwareBitmap) = _import_wgc()

        item = GraphicsCaptureItem.create_for_window(self._hwnd)
        if item is None:
            raise RuntimeError(
                "WGC: create_for_window이 None을 반환했습니다.\n"
                "팟플레이어 창이 최소화되어 있지 않은지 확인하세요."
            )

        d3d   = create_direct3d_device()
        BGRA8 = DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED
        pool  = Direct3D11CaptureFramePool.create(d3d, BGRA8, 2, item.size)
        session = pool.create_capture_session(item)

        try:
            session.is_cursor_capture_enabled = False
        except Exception:
            pass

        session.start_capture()
        _log("WGC 세션 시작")

        frame_ready = threading.Event()
        last_frame  = [None]

        def _on_frame(sender, _):
            f = sender.try_get_next_frame()
            if f is not None:
                last_frame[0] = f
                frame_ready.set()

        pool.frame_arrived += _on_frame

        w, h     = self._size
        interval = 1.0 / self._fps
        n        = 0

        try:
            while self._running:
                t0 = time.time()
                frame_ready.wait(timeout=0.1)
                frame_ready.clear()

                f = last_frame[0]
                if f is None:
                    continue

                try:
                    surface = f.surface
                    sb  = SoftwareBitmap.create_copy_from_surface_async(surface).get()
                    buf = sb.lock_buffer(BitmapBufferAccessMode.READ)
                    plane = buf.get_plane_description(0)
                    ref   = buf.create_reference()
                    raw   = bytes(ref)
                    fh, fw = plane.height, plane.width
                    arr   = np.frombuffer(raw, dtype=np.uint8).reshape(fh, fw, 4)
                    bgr   = _cv2.cvtColor(arr, _cv2.COLOR_BGRA2BGR)
                    if (fw, fh) != (w, h):
                        bgr = _cv2.resize(bgr, (w, h))
                    self._write_frame(bgr)
                    n += 1
                    if n == 1:
                        _log("WGC 첫 프레임 전송")
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
            _log(f"WGC 종료 ({n}프레임)")

    def _write_frame(self, bgr_frame):
        if self._ffmpeg_proc and self._ffmpeg_proc.stdin:
            try:
                self._ffmpeg_proc.stdin.write(bgr_frame.tobytes())
            except (BrokenPipeError, OSError) as e:
                _log(f"stdin 쓰기 실패: {e}")
                self._running = False

    def _loop(self):
        _log("캡처 루프 진입")
        try:
            self._wgc_loop()
        except Exception as e:
            _log(f"캡처 루프 예외: {e}")
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
        self._running = False

        if self._thread:
            self._thread.join(timeout=3)
            if self._thread.is_alive():
                _log("캡처 스레드 timeout → stdin 강제 닫기")
                if self._ffmpeg_proc and self._ffmpeg_proc.stdin:
                    try: self._ffmpeg_proc.stdin.close()
                    except: pass

        if self._ffmpeg_proc:
            try:
                _log("ffmpeg wait...")
                self._ffmpeg_proc.wait(timeout=120)
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
    import shutil
    import numpy as np

    ffmpeg_bin = _find_ffmpeg()
    has_audio  = audio_arr is not None and len(audio_arr) > 0

    if not has_audio or not ffmpeg_bin:
        _log(f"오디오 없이 이동 (has_audio={has_audio})")
        shutil.move(tmp_video, out_path)
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
    _log(f"WAV 저장 완료")

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
        _log(f"병합 실패 → 영상만 저장")
        shutil.move(tmp_video, out_path)
        try: shutil.copy(merge_log, out_path + "_merge_error.log")
        except: pass

    for p in [tmp_audio, tmp_out, tmp_video, merge_log]:
        try: os.remove(p)
        except: pass
