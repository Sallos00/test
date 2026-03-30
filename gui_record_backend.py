"""gui_record_backend.py -- 오디오/화면 녹화 백엔드"""
import os, time, threading, subprocess
try:
    import cv2
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

"""
gui_record.py -- 녹화 및 캡처 팝업 (설정 → 녹화 및 캡처)

기능:
  - 저장 위치 선택 (폴더 선택 + 열기 버튼)
  - 녹화 탭: 구간녹화 체크, MM:SS~MM:SS 입력, 녹화/정지 버튼
    * 팟플레이어 오디오만 ProcessLoopback(WASAPI)으로 캡처
    * 동영상 + 오디오 → MP4 저장 (Video/ 서브폴더)
    * 녹화 중 / 녹화 종료 팝업 (팟플레이어 좌상단 오버레이)
  - 캡처 탭: 화면 캡처 버튼 → PNG 저장 (Screenshot/ 서브폴더)
    * 캡처 완료 팝업 (팟플레이어 좌상단 오버레이)
"""

import os
import time
import threading
import tkinter as tk
from tkinter import filedialog

# ── 선택적 임포트 (없으면 해당 기능 비활성) ───────────────────────────────
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
# 팟플레이어 창 영역 획득
# ─────────────────────────────────────────────────────────────────────────────
def _get_potplayer_video_hwnd(parent_hwnd):
    """팟플레이어 내 영상 렌더러 자식 창 hwnd 반환. 없으면 None."""
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
    """팟플레이어 영상 영역 (x, y, w, h) 반환. 실패 시 None."""
    if not _WIN_OK:
        return None
    try:
        from win32_utils import find_potplayer_hwnd
        hwnd = find_potplayer_hwnd()
        if not hwnd:
            return None
        # 영상 렌더러 자식 창 찾기 → 없으면 클라이언트 영역 fallback
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
# 오버레이 팝업 (팟플레이어 좌상단)
# ─────────────────────────────────────────────────────────────────────────────

# 현재 화면에 표시 중인 오버레이 창 목록 (캡처/녹화 시 숨김용)
_active_overlays: list = []

def _show_overlay(root, message: str, duration_ms: int = 3000):
    """팟플레이어 클라이언트 영역 좌상단에 반투명 팝업을 띄운다."""
    rect = _get_potplayer_rect()
    if rect is None:
        return  # 팟플레이어가 없으면 조용히 무시

    px, py, pw, ph = rect
    ox = px + 12
    oy = py + 12

    try:
        ov = tk.Toplevel(root)
        ov.overrideredirect(True)
        ov.attributes("-topmost", True)
        ov.attributes("-alpha", 0.88)
        ov.configure(bg="#101010")
        ov.geometry(f"+{ox}+{oy}")

        tk.Label(
            ov, text=message,
            font=("Segoe UI", 11, "bold"),
            bg="#101010", fg="#00c8e0",
            padx=14, pady=8,
        ).pack()

        ov.update_idletasks()
        _active_overlays.append(ov)

        def _close():
            try:
                ov.destroy()
            except Exception:
                pass
            try:
                _active_overlays.remove(ov)
            except ValueError:
                pass

        root.after(duration_ms, _close)
    except Exception:
        pass


def _hide_all_overlays():
    """캡처/녹화 프레임 획득 직전에 모든 오버레이를 숨긴다."""
    for ov in list(_active_overlays):
        try:
            ov.withdraw()
        except Exception:
            pass


def _show_all_overlays():
    """캡처/녹화 프레임 획득 직후 숨긴 오버레이를 다시 표시한다."""
    for ov in list(_active_overlays):
        try:
            ov.deiconify()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 오디오 캡처 (ProcessLoopback, 별도 스레드)
# ─────────────────────────────────────────────────────────────────────────────
class _AudioRecorder:
    """
    audio_capture.py 의 COM/WinAPI ProcessLoopback 구현을 재사용해
    팟플레이어 오디오를 raw PCM(float32)으로 수집한다.

    핵심 제약:
      _activate_process_loopback → _audio_client_initialize → 캡처 루프
      전체가 반드시 하나의 MTA 스레드 안에서 실행되어야 COM 포인터가 유효.
      → _session_mta() 안에서 모두 처리하고, 바깥에서 CoInitializeEx 하지 않음.
    """
    def __init__(self):
        self._frames  = []
        self._sr      = 48000
        self._ch      = 2
        self._running = False
        self._thread  = None

    def start(self, pid: int):
        self._frames  = []
        self._running = True

        recorder = self  # 클로저용

        def _session_mta():
            import ctypes as ct
            import numpy as np

            ole32    = ct.windll.ole32
            kernel32 = ct.windll.kernel32

            COINIT_MULTITHREADED = 0x0
            hr_co = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
            co_ok = hr_co in (0, 1, 0x80010106)  # S_OK / S_FALSE / RPC_E_CHANGED_MODE(이미 MTA)

            try:
                from audio_capture import (
                    _activate_process_loopback,
                    _audio_client_initialize,
                    _audio_client_set_event,
                    _audio_client_start,
                    _audio_client_stop,
                    _get_capture_client,
                    _get_next_packet_size,
                    _get_buffer,
                    _release_buffer,
                    _com_release,
                    AUDCLNT_BUFFERFLAGS_SILENT,
                )

                client = _activate_process_loopback(pid)
                sr, ch = _audio_client_initialize(client)
                recorder._sr = sr
                recorder._ch = ch

                h_event = kernel32.CreateEventW(None, False, False, None)
                _audio_client_set_event(client, h_event)
                cap = _get_capture_client(client)
                _audio_client_start(client)

                WAIT_MS = 10
                try:
                    while recorder._running:
                        kernel32.WaitForSingleObject(h_event, WAIT_MS)
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
                    try:
                        _audio_client_stop(client)
                    except Exception:
                        pass
                    _com_release(cap)
                    _com_release(client)
                    kernel32.CloseHandle(h_event)

            except Exception:
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
        import numpy as np
        if self._frames:
            return np.concatenate(self._frames), self._sr, self._ch
        return None, self._sr, self._ch


class _ScreenRecorder:
    """
    OBS 방식: 녹화 시작 시 ffmpeg를 즉시 실행하고,
    캡처한 프레임을 실시간으로 stdin 파이프에 전달한다.
    → 메모리에 프레임을 쌓지 않으므로 장시간 녹화도 안정적.

    우선순위:
      1. pywinrt WGC — HWND 단위 캡처 (오버레이 완전 제외)
      2. mss          — 스크린 캡처 fallback (오버레이 hide/show)
    """
    def __init__(self):
        self._running   = False
        self._thread    = None
        self._fps       = 30
        self._size      = (1280, 720)
        self._hwnd      = None
        self._root      = None
        self._ffmpeg_proc = None   # 실시간 인코딩용 ffmpeg 프로세스
        self._out_path    = None
        self._error       = None   # 녹화 중 발생한 오류

    def start(self, fps: int = 30, root=None, out_path: str = None,
              audio_wav_path: str = None):
        """
        녹화를 시작한다.
        out_path       : 최종 MP4 저장 경로
        audio_wav_path : 나중에 병합할 WAV 파일 경로 (None이면 무음)
        """
        from win32_utils import find_potplayer_hwnd
        hwnd = find_potplayer_hwnd()
        if hwnd is None:
            raise RuntimeError("팟플레이어 창을 찾을 수 없습니다.")
        video_hwnd = _get_potplayer_video_hwnd(hwnd)
        self._hwnd = video_hwnd if video_hwnd else hwnd
        self._root = root

        rect = _get_potplayer_rect()
        if rect is None:
            raise RuntimeError("팟플레이어 창 영역을 구할 수 없습니다.")
        px, py, pw, ph = rect
        self._fps      = fps
        self._size     = (pw, ph)
        self._running  = True
        self._out_path = out_path
        self._error    = None

        # ffmpeg를 즉시 기동 (OBS 방식: 캡처 즉시 인코딩)
        ffmpeg_bin = _find_ffmpeg()
        if not ffmpeg_bin:
            raise RuntimeError(
                "ffmpeg를 찾을 수 없습니다.\n"
                "ffmpeg를 설치하고 PATH에 추가하거나,\n"
                "프로그램 폴더에 ffmpeg.exe를 넣어주세요."
            )

        w, h = pw, ph
        # 홀수 해상도는 yuv420p 인코딩 실패 → 짝수로 내림
        w = w - (w % 2)
        h = h - (h % 2)
        self._size = (w, h)

        import tempfile
        self._tmp_video    = os.path.join(tempfile.gettempdir(), "autosinc_live_video.mp4")
        self._ffmpeg_log   = os.path.join(tempfile.gettempdir(), "autosinc_ffmpeg.log")

        cmd = [
            ffmpeg_bin, "-y",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{w}x{h}",
            "-r", str(fps),
            "-i", "pipe:0",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-an",                    # 오디오는 나중에 별도 병합
            self._tmp_video,
        ]
        # stderr를 파이프 대신 파일에 기록 → 파이프 버퍼 고갈로 인한 deadlock 방지
        self._ffmpeg_log_fh = open(self._ffmpeg_log, "wb")
        self._ffmpeg_proc = _popen_no_window(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=self._ffmpeg_log_fh,
        )

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ── 1순위: pywinrt WGC (HWND 기반 — 오버레이 미포함) ────────────────
    def _try_wgc_loop(self) -> bool:
        import numpy as np
        try:
            import cv2 as _cv2
        except ImportError:
            return False
        try:
            from winsdk.windows.graphics.capture import (
                GraphicsCaptureItem, Direct3D11CaptureFramePool)
            from winsdk.windows.graphics.directx import DirectXPixelFormat
            from winsdk.windows.graphics.directx.direct3d11 import create_direct3d_device
            from winsdk.windows.graphics.imaging import BitmapBufferAccessMode, SoftwareBitmap
        except ImportError:
            try:
                from winrt.windows.graphics.capture import (
                    GraphicsCaptureItem, Direct3D11CaptureFramePool)
                from winrt.windows.graphics.directx import DirectXPixelFormat
                from winrt.windows.graphics.directx.direct3d11 import create_direct3d_device
                from winrt.windows.graphics.imaging import BitmapBufferAccessMode, SoftwareBitmap
            except ImportError:
                return False

        try:
            item = GraphicsCaptureItem.create_for_window(self._hwnd)
            if item is None:
                return False

            d3d      = create_direct3d_device()
            BGRA8    = DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED
            pool     = Direct3D11CaptureFramePool.create(d3d, BGRA8, 2, item.size)
            session  = pool.create_capture_session(item)
            try:
                session.is_cursor_capture_enabled = False
            except Exception:
                pass
            session.start_capture()

            import threading as _th
            frame_ready = _th.Event()
            last_frame  = [None]

            def _on_frame(sender, _):
                f = sender.try_get_next_frame()
                if f is not None:
                    last_frame[0] = f
                    frame_ready.set()

            pool.frame_arrived += _on_frame

            w, h     = self._size
            interval = 1.0 / self._fps

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
                    # 메모리에 쌓지 않고 즉시 ffmpeg로 전송
                    self._write_frame(bgr)
                except Exception:
                    pass
                sl = interval - (time.time() - t0)
                if sl > 0:
                    time.sleep(sl)

            session.close()
            pool.close()
            return True

        except Exception:
            return False

    # ── 2순위: mss fallback ───────────────────────────────────────────────
    def _mss_loop(self):
        import numpy as np
        import threading as _th
        try:
            import mss as _mss
            import cv2 as _cv2
        except ImportError:
            self._running = False
            return

        rect = _get_potplayer_rect()
        if rect is None:
            self._running = False
            return
        px, py, pw, ph = rect
        w, h     = self._size
        monitor  = {"left": px, "top": py, "width": pw, "height": ph}
        interval = 1.0 / self._fps

        hide_done = _th.Event()
        show_done = _th.Event()

        def _do_hide():
            _hide_all_overlays()
            try:
                self._root.update_idletasks()
            except Exception:
                pass
            hide_done.set()

        def _do_show():
            _show_all_overlays()
            try:
                self._root.update_idletasks()
            except Exception:
                pass
            show_done.set()

        root = self._root

        with _mss.mss() as sct:
            while self._running:
                t0 = time.time()
                try:
                    hide_done.clear()
                    root.after(0, _do_hide)
                    hide_done.wait(timeout=0.1)
                    time.sleep(0.02)

                    shot  = sct.grab(monitor)

                    show_done.clear()
                    root.after(0, _do_show)
                    show_done.wait(timeout=0.1)

                    frame = np.array(shot)
                    frame = _cv2.cvtColor(frame, _cv2.COLOR_BGRA2BGR)
                    if (frame.shape[1], frame.shape[0]) != (w, h):
                        frame = _cv2.resize(frame, (w, h))
                    self._write_frame(frame)
                except Exception:
                    try:
                        root.after(0, _do_show)
                    except Exception:
                        pass
                sl = interval - (time.time() - t0)
                if sl > 0:
                    time.sleep(sl)

    def _write_frame(self, bgr_frame):
        """프레임을 ffmpeg stdin 파이프에 즉시 기록."""
        if self._ffmpeg_proc and self._ffmpeg_proc.stdin:
            try:
                self._ffmpeg_proc.stdin.write(bgr_frame.tobytes())
            except (BrokenPipeError, OSError):
                self._running = False

    def _loop(self):
        try:
            if not self._try_wgc_loop():
                self._mss_loop()
        finally:
            # 캡처 루프 종료 → ffmpeg stdin 닫기
            if self._ffmpeg_proc and self._ffmpeg_proc.stdin:
                try:
                    self._ffmpeg_proc.stdin.close()
                except Exception:
                    pass

    def stop(self) -> str:
        """
        녹화를 멈추고, ffmpeg가 완료될 때까지 기다린다.
        반환값: 임시 영상 파일 경로 (tmp_video).
        오류 시 RuntimeError 발생.
        """
        self._running = False

        # 캡처 루프가 현재 프레임 처리를 마치고 빠져나올 시간만 주면 됨
        if self._thread:
            self._thread.join(timeout=2)
            # join이 timeout됐다면 강제로 stdin을 닫아 ffmpeg 종료 유도
            if self._thread.is_alive():
                if self._ffmpeg_proc and self._ffmpeg_proc.stdin:
                    try:
                        self._ffmpeg_proc.stdin.close()
                    except Exception:
                        pass

        # ffmpeg가 남은 프레임을 마저 인코딩하도록 대기
        # stderr는 파일로 리다이렉트했으므로 communicate() 대신 wait() 사용
        if self._ffmpeg_proc:
            try:
                self._ffmpeg_proc.wait(timeout=120)
            except Exception:
                try:
                    self._ffmpeg_proc.kill()
                    self._ffmpeg_proc.wait()
                except Exception:
                    pass
            finally:
                # stderr 파일 핸들 닫기
                try:
                    fh = getattr(self, "_ffmpeg_log_fh", None)
                    if fh:
                        fh.close()
                except Exception:
                    pass

            if self._ffmpeg_proc.returncode != 0:
                log = getattr(self, "_ffmpeg_log", "")
                raise RuntimeError(
                    f"ffmpeg 인코딩 실패 (code={self._ffmpeg_proc.returncode})\n"
                    f"로그: {log}"
                )

        tmp = getattr(self, "_tmp_video", None)
        if not tmp or not os.path.isfile(tmp) or os.path.getsize(tmp) < 1024:
            log = getattr(self, "_ffmpeg_log", "%TEMP%\\autosinc_ffmpeg.log")
            raise RuntimeError(
                f"녹화된 영상 파일이 없습니다.\n"
                f"ffmpeg 로그를 확인하세요: {log}"
            )

        return tmp


# ─────────────────────────────────────────────────────────────────────────────
# MP4 저장 (비디오 + 오디오 병합)
# ─────────────────────────────────────────────────────────────────────────────
def _find_ffmpeg() -> str:
    """ffmpeg 실행 파일 경로 반환. 없으면 빈 문자열."""
    import shutil, sys

    # 1. PyInstaller --onefile 번들 내부 (_MEIPASS = 압축 해제된 임시 폴더)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = os.path.join(meipass, "ffmpeg.exe")
        if os.path.isfile(bundled):
            return bundled

    # 2. exe 실행 파일과 같은 폴더 (개발 환경 or --onedir 빌드)
    try:
        exe_dir = os.path.dirname(sys.executable)
        local = os.path.join(exe_dir, "ffmpeg.exe")
        if os.path.isfile(local):
            return local
    except Exception:
        pass

    # 3. 소스 실행 시 스크립트 옆
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        local = os.path.join(here, "ffmpeg.exe")
        if os.path.isfile(local):
            return local
    except Exception:
        pass

    # 4. 시스템 PATH
    p = shutil.which("ffmpeg")
    if p:
        return p

    # 5. 일반적인 설치 위치
    for candidate in [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    ]:
        if os.path.isfile(candidate):
            return candidate

    return ""


def _popen_no_window(cmd, **kwargs):
    """
    --noconsole(윈도우 서브시스템) 빌드에서 subprocess가
    콘솔 창을 띄우거나 실패하지 않도록 CREATE_NO_WINDOW 플래그를 추가한다.
    """
    import subprocess
    CREATE_NO_WINDOW = 0x08000000
    kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    return subprocess.Popen(cmd, **kwargs)


def _merge_audio(tmp_video: str, audio_arr, audio_sr: int, audio_ch: int,
                 out_path: str):
    """
    이미 H.264로 인코딩된 tmp_video에 오디오를 병합해 out_path에 저장.
    오디오가 없으면 tmp_video를 그냥 out_path로 이동.
    """
    import subprocess, numpy as np, shutil, tempfile

    ffmpeg_bin = _find_ffmpeg()
    has_audio  = (audio_arr is not None and len(audio_arr) > 0)

    if not has_audio:
        shutil.move(tmp_video, out_path)
        return

    tmp_dir   = tempfile.gettempdir()
    tmp_audio = os.path.join(tmp_dir, "autosinc_tmp_audio.wav")
    tmp_out   = os.path.join(tmp_dir, "autosinc_merge_out.mp4")

    # WAV 저장
    import wave
    audio_data = audio_arr.reshape(-1, audio_ch) if audio_ch > 1 else audio_arr.reshape(-1, 1)
    pcm = (audio_data * 32767).clip(-32768, 32767).astype(np.int16)
    with wave.open(tmp_audio, "wb") as wf:
        wf.setnchannels(audio_ch)
        wf.setsampwidth(2)
        wf.setframerate(audio_sr)
        wf.writeframes(pcm.tobytes())

    # ffmpeg로 비디오 + 오디오 병합 (비디오는 스트림 복사, 오디오만 AAC 인코딩)
    merge_log = tmp_out + ".log"
    cmd = [
        ffmpeg_bin, "-y",
        "-i", tmp_video,
        "-i", tmp_audio,
        "-c:v", "copy",         # 이미 H.264이므로 재인코딩 없이 복사
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        tmp_out,
    ]
    with open(merge_log, "wb") as log_fh:
        proc = _popen_no_window(cmd, stdout=subprocess.DEVNULL, stderr=log_fh)
        proc.wait()

    if proc.returncode == 0 and os.path.isfile(tmp_out) and os.path.getsize(tmp_out) > 1024:
        shutil.move(tmp_out, out_path)
    else:
        # 병합 실패 시 영상만이라도 저장
        shutil.move(tmp_video, out_path)
        try:
            shutil.copy(merge_log, out_path + "_merge_error.log")
        except Exception:
            pass

    for p in [tmp_audio, tmp_out, tmp_video, merge_log]:
        try:
            os.remove(p)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 메인 팝업 클래스
# ─────────────────────────────────────────────────────────────────────────────
