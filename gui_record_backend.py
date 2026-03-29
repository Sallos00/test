"""gui_record_backend.py -- 오디오/화면 녹화 백엔드"""
import os, time, threading
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

                # 1. ProcessLoopback 클라이언트 활성화
                #    (내부에서 별도 스레드로 ActivateAudioInterfaceAsync 호출 후 join)
                client = _activate_process_loopback(pid)

                # 2. Initialize (렌더 디바이스 MixFormat 사용)
                sr, ch = _audio_client_initialize(client)
                recorder._sr = sr
                recorder._ch = ch

                # 3. 이벤트 핸들 + CaptureClient
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

        # MTA 전용 스레드 — 이 스레드 안에서만 COM 포인터를 사용
        self._thread = threading.Thread(target=_session_mta, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        import numpy as np
        if self._frames:
            return np.concatenate(self._frames), self._sr, self._ch
        return None, self._sr, self._ch


class _ScreenRecorder:
    """
    팟플레이어 창을 Windows Graphics Capture API(WGC)로 캡처.
    WGC는 HWND 단위로 캡처하므로 다른 HWND인 tkinter 오버레이는
    녹화본에 찍히지 않는다. (팟플레이어 화면에는 정상 표시됨)

    우선순위:
      1. pywinrt  — WGC HWND 캡처 (오버레이 완전 제외)
      2. mss      — 스크린 캡처 fallback (오버레이 hide/show 방식)

    pywinrt 설치:  pip install pywinrt
    """
    def __init__(self):
        self._running = False
        self._thread  = None
        self._frames  = []
        self._fps     = 30
        self._size    = (1280, 720)
        self._hwnd    = None
        self._root    = None

    def start(self, fps: int = 30, root=None):
        from win32_utils import find_potplayer_hwnd
        hwnd = find_potplayer_hwnd()
        if hwnd is None:
            raise RuntimeError("팟플레이어 창을 찾을 수 없습니다.")
        video_hwnd = _get_potplayer_video_hwnd(hwnd)
        self._hwnd = video_hwnd if video_hwnd else hwnd
        self._root = root  # mss fallback의 after() 호출용

        rect = _get_potplayer_rect()
        if rect is None:
            raise RuntimeError("팟플레이어 창 영역을 구할 수 없습니다.")
        px, py, pw, ph = rect
        self._fps     = fps
        self._size    = (pw, ph)
        self._frames  = []
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ── 1순위: pywinrt WGC (HWND 기반 — 오버레이 미포함) ────────────────
    def _try_wgc_loop(self) -> bool:
        import numpy as np
        try:
            import cv2 as _cv2
        except ImportError:
            return False
        try:
            # pywinrt 네임스페이스 import
            from winsdk.windows.graphics.capture import (
                GraphicsCaptureItem,
                Direct3D11CaptureFramePool,
            )
            from winsdk.windows.graphics.directx import DirectXPixelFormat
            from winsdk.windows.graphics.directx.direct3d11 import (
                create_direct3d_device,
            )
            from winsdk.windows.graphics.imaging import (
                BitmapBufferAccessMode,
                SoftwareBitmap,
            )
            from winsdk.windows.ui import UIContext  # noqa — 사용 안 함, import 확인용
        except ImportError:
            try:
                # 구버전 pywinrt 네임스페이스
                from winrt.windows.graphics.capture import (
                    GraphicsCaptureItem,
                    Direct3D11CaptureFramePool,
                )
                from winrt.windows.graphics.directx import DirectXPixelFormat
                from winrt.windows.graphics.directx.direct3d11 import (
                    create_direct3d_device,
                )
                from winrt.windows.graphics.imaging import (
                    BitmapBufferAccessMode,
                    SoftwareBitmap,
                )
            except ImportError:
                return False

        try:
            import ctypes, ctypes.wintypes

            # ── HWND → IGraphicsCaptureItem (win32 interop) ──────────────
            # GraphicsCaptureItem.CreateForWindow 은 WinRT interop 함수.
            # pywinrt 에서는 create_for_window(hwnd) 로 호출한다.
            item = GraphicsCaptureItem.create_for_window(self._hwnd)
            if item is None:
                return False

            d3d = create_direct3d_device()

            BGRA8 = DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED
            item_size = item.size
            pool = Direct3D11CaptureFramePool.create(
                d3d, BGRA8, 2, item_size
            )
            session = pool.create_capture_session(item)
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
                    sb = SoftwareBitmap.create_copy_from_surface_async(
                        surface
                    ).get()
                    buf = sb.lock_buffer(BitmapBufferAccessMode.READ)
                    plane = buf.get_plane_description(0)
                    ref   = buf.create_reference()
                    raw   = bytes(ref)
                    h, w  = plane.height, plane.width
                    arr   = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
                    bgr   = _cv2.cvtColor(arr, _cv2.COLOR_BGRA2BGR)
                    if (w, h) != self._size:
                        bgr = _cv2.resize(bgr, self._size)
                    self._frames.append(bgr)
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

    # ── 2순위: mss fallback (hide/show 방식) ─────────────────────────────
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
        monitor  = {"left": px, "top": py, "width": pw, "height": ph}
        interval = 1.0 / self._fps

        # tkinter는 메인 스레드에서만 호출 가능.
        # hide_done 이벤트로 메인 스레드의 withdraw 완료를 기다린 뒤 grab.
        hide_done = _th.Event()
        show_done = _th.Event()

        def _do_hide():
            _hide_all_overlays()
            # update_idletasks()로 withdraw가 실제 화면에 반영될 때까지 대기
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

        root = self._root  # start()에서 저장

        with _mss.mss() as sct:
            while self._running:
                t0 = time.time()
                try:
                    hide_done.clear()
                    root.after(0, _do_hide)
                    hide_done.wait(timeout=0.1)    # update_idletasks 포함이므로 100ms

                    # 추가 여유: OS가 창 숨김을 화면에 실제 반영하도록 잠깐 대기
                    time.sleep(0.02)

                    shot  = sct.grab(monitor)

                    show_done.clear()
                    root.after(0, _do_show)
                    show_done.wait(timeout=0.1)

                    frame = np.array(shot)
                    frame = _cv2.cvtColor(frame, _cv2.COLOR_BGRA2BGR)
                    self._frames.append(frame)
                except Exception:
                    try:
                        root.after(0, _do_show)
                    except Exception:
                        pass
                sl = interval - (time.time() - t0)
                if sl > 0:
                    time.sleep(sl)

    def _loop(self):
        # WGC 시도 → 실패 시 mss fallback
        if not self._try_wgc_loop():
            self._mss_loop()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        return self._frames, self._fps, self._size


# ─────────────────────────────────────────────────────────────────────────────
# MP4 저장 (비디오 + 오디오 병합)
# ─────────────────────────────────────────────────────────────────────────────
def _find_ffmpeg() -> str:
    """ffmpeg 실행 파일 경로 반환. 없으면 빈 문자열."""
    import shutil
    # 1. PATH에서 찾기
    p = shutil.which("ffmpeg")
    if p:
        return p
    # 2. 프로그램 동작 폴더 옆에 ffmpeg.exe 가 있는 경우 (번들 배포)
    here = os.path.dirname(os.path.abspath(__file__))
    local = os.path.join(here, "ffmpeg.exe")
    if os.path.isfile(local):
        return local
    # 3. 일반적인 설치 위치
    for candidate in [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    ]:
        if os.path.isfile(candidate):
            return candidate
    return ""


def _save_mp4(video_frames, fps, size, audio_arr, audio_sr, audio_ch, out_path):
    """ffmpeg로 H.264 비디오 + AAC 오디오 MP4 저장. ffmpeg 없으면 OpenCV fallback."""
    import subprocess, numpy as np, shutil, tempfile

    # 한글/특수문자 경로 문제를 피하기 위해 임시 파일은 TEMP 폴더에 생성
    tmp_dir   = tempfile.gettempdir()
    tmp_video = os.path.join(tmp_dir, "autosinc_tmp_video.avi")
    tmp_audio = os.path.join(tmp_dir, "autosinc_tmp_audio.wav")

    # 1. 비디오 임시 저장 (MJPG AVI — ffmpeg 입력용)
    fourcc_avi = cv2.VideoWriter_fourcc(*"MJPG")
    vw = cv2.VideoWriter(tmp_video, fourcc_avi, fps, size)
    for f in video_frames:
        h, w = f.shape[:2]
        if (w, h) != size:
            f = cv2.resize(f, size)
        vw.write(f)
    vw.release()

    # 2. 오디오 wav 저장
    has_audio = False
    if audio_arr is not None and len(audio_arr) > 0:
        try:
            import wave
            audio_data = audio_arr.reshape(-1, audio_ch) if audio_ch > 1 else audio_arr.reshape(-1, 1)
            pcm = (audio_data * 32767).clip(-32768, 32767).astype(np.int16)
            with wave.open(tmp_audio, "wb") as wf:
                wf.setnchannels(audio_ch)
                wf.setsampwidth(2)
                wf.setframerate(audio_sr)
                wf.writeframes(pcm.tobytes())
            has_audio = True
        except Exception:
            pass

    # 3. ffmpeg로 H.264 + AAC 인코딩
    #    -c:v libx264      → 범용 H.264
    #    -pix_fmt yuv420p  → Windows Media Player / QuickTime 호환
    #    -movflags +faststart → moov 박스 앞배치 (빠른 열기)
    merged    = False
    ffmpeg_bin = _find_ffmpeg()

    if ffmpeg_bin:
        ffmpeg_inputs = ["-i", tmp_video]
        ffmpeg_audio  = ["-an"]
        if has_audio:
            ffmpeg_inputs += ["-i", tmp_audio]
            ffmpeg_audio   = ["-c:a", "aac", "-b:a", "192k"]

        # out_path에 한글이 있어도 ffmpeg가 처리할 수 있도록
        # 임시 출력 파일을 TEMP에 먼저 쓴 뒤 shutil.move로 이동
        tmp_out = os.path.join(tmp_dir, "autosinc_tmp_out.mp4")
        try:
            cmd = (
                [ffmpeg_bin, "-y"]
                + ffmpeg_inputs
                + [
                    "-c:v", "libx264",
                    "-preset", "fast",
                    "-crf", "18",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                ]
                + ffmpeg_audio
                + (["-shortest"] if has_audio else [])
                + [tmp_out]
            )
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode == 0 and os.path.isfile(tmp_out):
                shutil.move(tmp_out, out_path)
                merged = True
            else:
                # 디버깅용: 에러 로그를 out_path 옆에 남김
                try:
                    log_path = out_path + "_ffmpeg_error.txt"
                    with open(log_path, "wb") as lf:
                        lf.write(result.stderr)
                except Exception:
                    pass
        except Exception:
            pass

    # 4. ffmpeg 없는 환경 fallback: OpenCV로 직접 MP4 저장
    if not merged:
        # avc1(H.264) 시도 → 안 되면 mp4v
        for fourcc_str in ("avc1", "mp4v"):
            fourcc_mp4 = cv2.VideoWriter_fourcc(*fourcc_str)
            vw2 = cv2.VideoWriter(out_path, fourcc_mp4, fps, size)
            if vw2.isOpened():
                for f in video_frames:
                    h, w = f.shape[:2]
                    if (w, h) != size:
                        f = cv2.resize(f, size)
                    vw2.write(f)
                vw2.release()
                merged = True
                break
            vw2.release()

    for p in [tmp_video, tmp_audio]:
        try:
            os.remove(p)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 메인 팝업 클래스
# ─────────────────────────────────────────────────────────────────────────────
