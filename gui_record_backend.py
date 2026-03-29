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
    audio_capture.py 와 동일한 COM/WinAPI ProcessLoopback 방식으로
    팟플레이어 오디오를 캡처하여 raw PCM(float32) 프레임을 수집한다.
    Windows 10 20H1(빌드 19041) 이상에서 동작.
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

        def _loop():
            import numpy as np
            import ctypes as ct
            import ctypes.wintypes as wt

            ole32    = ct.windll.ole32
            kernel32 = ct.windll.kernel32
            COINIT_MULTITHREADED = 0x0

            # ── 같은 MTA 스레드 안에서 COM 초기화 + 캡처 세션 실행 ──────────
            hr_co = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
            co_ok = hr_co in (0, 1)
            try:
                # audio_capture 모듈의 헬퍼 함수 재사용
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

                client  = _activate_process_loopback(pid)
                sr, ch  = _audio_client_initialize(client)
                self._sr = sr
                self._ch = ch

                h_event = kernel32.CreateEventW(None, False, False, None)
                _audio_client_set_event(client, h_event)
                cap = _get_capture_client(client)
                _audio_client_start(client)

                WAIT_MS = 10
                try:
                    while self._running:
                        kernel32.WaitForSingleObject(h_event, WAIT_MS)
                        # 패킷 드레인
                        while self._running:
                            try:
                                pkt = _get_next_packet_size(cap)
                            except OSError:
                                self._running = False
                                break
                            if pkt == 0:
                                break
                            data, num_frames, flg = _get_buffer(cap)
                            if num_frames > 0:
                                if not (flg & AUDCLNT_BUFFERFLAGS_SILENT) and data.value:
                                    buf = (ct.c_float * (num_frames * ch)).from_address(data.value)
                                    arr = np.frombuffer(buf, dtype=np.float32).copy()
                                    self._frames.append(arr)
                                else:
                                    # 무음 구간 — 0으로 채워 타임라인 유지
                                    self._frames.append(np.zeros(num_frames * ch, dtype=np.float32))
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
                # ProcessLoopback 실패 시 조용히 종료 (오디오 없이 영상만 저장)
                self._running = False
            finally:
                if co_ok:
                    ole32.CoUninitialize()

        self._thread = threading.Thread(target=_loop, daemon=True)
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

    def start(self, fps: int = 30):
        from win32_utils import find_potplayer_hwnd
        hwnd = find_potplayer_hwnd()
        if hwnd is None:
            raise RuntimeError("팟플레이어 창을 찾을 수 없습니다.")
        video_hwnd = _get_potplayer_video_hwnd(hwnd)
        self._hwnd = video_hwnd if video_hwnd else hwnd

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

        with _mss.mss() as sct:
            while self._running:
                t0 = time.time()
                try:
                    _hide_all_overlays()
                    shot  = sct.grab(monitor)
                    _show_all_overlays()
                    frame = np.array(shot)
                    frame = _cv2.cvtColor(frame, _cv2.COLOR_BGRA2BGR)
                    self._frames.append(frame)
                except Exception:
                    _show_all_overlays()
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
def _save_mp4(video_frames, fps, size, audio_arr, audio_sr, audio_ch, out_path):
    """OpenCV로 비디오 저장 후 ffmpeg로 오디오 병합. ffmpeg 없으면 영상만 저장."""
    import subprocess, numpy as np

    tmp_video = out_path + "_tmp_video.mp4"
    tmp_audio = out_path + "_tmp_audio.wav"

    # 1. 비디오 임시 저장
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(tmp_video, fourcc, fps, size)
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
            import wave, struct
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

    # 3. ffmpeg로 병합
    merged = False
    if has_audio:
        try:
            cmd = [
                "ffmpeg", "-y",
                "-i", tmp_video,
                "-i", tmp_audio,
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                out_path,
            ]
            subprocess.run(cmd, check=True,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            merged = True
        except Exception:
            pass

    if not merged:
        import shutil
        shutil.copy(tmp_video, out_path)

    for p in [tmp_video, tmp_audio]:
        try: os.remove(p)
        except: pass


# ─────────────────────────────────────────────────────────────────────────────
# 메인 팝업 클래스
# ─────────────────────────────────────────────────────────────────────────────
