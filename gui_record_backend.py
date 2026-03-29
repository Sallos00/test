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
def _get_potplayer_rect():
    """팟플레이어 클라이언트 영역 (x, y, w, h) 반환. 실패 시 None."""
    if not _WIN_OK:
        return None
    try:
        from win32_utils import find_potplayer_hwnd
        hwnd = find_potplayer_hwnd()
        if not hwnd:
            return None
        rc = _wt.RECT()
        _user32.GetClientRect(hwnd, _ct.byref(rc))
        pt = _wt.POINT(0, 0)
        _user32.ClientToScreen(hwnd, _ct.byref(pt))
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

        def _close():
            try:
                ov.destroy()
            except Exception:
                pass

        root.after(duration_ms, _close)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 오디오 캡처 (ProcessLoopback, 별도 스레드)
# ─────────────────────────────────────────────────────────────────────────────
class _AudioRecorder:
    """
    audio_capture.py 의 WASAPI ProcessLoopback 을 직접 이용해
    팟플레이어 오디오를 float32 raw 버퍼로 수집한다.
    """
    def __init__(self):
        self._frames   = []
        self._sr       = 48000
        self._ch       = 2
        self._running  = False
        self._thread   = None

    def start(self, pid: int):
        from multiprocessing import Value as _Value
        import audio_capture as _ac
        self._frames  = []
        self._running = True
        self._stop_v  = _Value("b", False)

        def _loop():
            import ctypes as _c
            import numpy as np

            COINIT_MULTITHREADED = 0x0
            _ole32 = _c.windll.ole32
            hr_co  = _ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
            co_ok  = hr_co in (0, 1)
            client = cap = h_event = None
            try:
                client  = _ac._activate_process_loopback(pid)
                sr, ch  = _ac._audio_client_initialize(client)
                self._sr = sr
                self._ch = ch
                h_event  = _ac._kernel32.CreateEventW(None, False, False, None)
                _ac._audio_client_set_event(client, h_event)
                cap = _ac._get_capture_client(client)
                _ac._audio_client_start(client)

                WAIT_MS = 10
                while self._running:
                    _ac._kernel32.WaitForSingleObject(h_event, WAIT_MS)
                    while True:
                        try:
                            pkt = _ac._get_next_packet_size(cap)
                        except OSError:
                            break
                        if pkt == 0:
                            break
                        data, num_frames, flg = _ac._get_buffer(cap)
                        if num_frames > 0:
                            if flg & _ac.AUDCLNT_BUFFERFLAGS_SILENT:
                                arr = np.zeros(num_frames * ch, dtype=np.float32)
                            else:
                                import ctypes
                                buf = (ctypes.c_float * (num_frames * ch)).from_address(data.value)
                                arr = np.frombuffer(buf, dtype=np.float32).copy()
                            self._frames.append(arr)
                        _ac._release_buffer(cap, num_frames)
            except Exception:
                pass
            finally:
                if cap and client:
                    try: _ac._audio_client_stop(client)
                    except Exception: pass
                    _ac._com_release(cap)
                if client:
                    _ac._com_release(client)
                if h_event:
                    _ac._kernel32.CloseHandle(h_event)
                if co_ok:
                    _ole32.CoUninitialize()

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        import numpy as np
        if self._frames:
            return np.concatenate(self._frames), self._sr, self._ch
        return None, self._sr, self._ch


# ─────────────────────────────────────────────────────────────────────────────
# 화면 녹화 스레드
# ─────────────────────────────────────────────────────────────────────────────
class _ScreenRecorder:
    """팟플레이어 클라이언트 영역을 mss로 캡처 (GPU 렌더링 지원)."""
    def __init__(self):
        self._running = False
        self._thread  = None
        self._frames  = []
        self._fps     = 30
        self._size    = (1280, 720)

    def start(self, fps: int = 30):
        rect = _get_potplayer_rect()
        if rect is None:
            raise RuntimeError("팟플레이어 창을 찾을 수 없습니다.")
        px, py, pw, ph = rect
        self._monitor = {"left": px, "top": py, "width": pw, "height": ph}
        self._fps     = fps
        self._size    = (pw, ph)
        self._frames  = []
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        import numpy as np
        try:
            import mss as _mss
            import cv2 as _cv2
        except ImportError:
            self._running = False
            return
        interval = 1.0 / self._fps
        with _mss.mss() as sct:
            while self._running:
                t0 = time.time()
                try:
                    shot  = sct.grab(self._monitor)
                    frame = np.array(shot)              # BGRA
                    frame = _cv2.cvtColor(frame, _cv2.COLOR_BGRA2BGR)
                    self._frames.append(frame)
                except Exception:
                    pass
                elapsed = time.time() - t0
                sleep_t = interval - elapsed
                if sleep_t > 0:
                    time.sleep(sleep_t)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        return self._frames, self._fps, self._size


# ─────────────────────────────────────────────────────────────────────────────
# MP4 저장 (비디오 + 오디오 병합)
# ─────────────────────────────────────────────────────────────────────────────
def _save_mp4(video_frames, fps, size, audio_arr, audio_sr, audio_ch, out_path):
    """OpenCV 로 비디오 저장 후 ffmpeg 로 오디오 병합."""
    import subprocess, tempfile, numpy as np

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

    # 2. 오디오 임시 저장
    if audio_arr is not None and len(audio_arr) > 0:
        audio_data = audio_arr.reshape(-1, audio_ch) if audio_ch > 1 else audio_arr
        sf.write(tmp_audio, audio_data, audio_sr, subtype="PCM_16")
        has_audio = True
    else:
        has_audio = False

    # 3. ffmpeg 병합
    if has_audio:
        cmd = [
            "ffmpeg", "-y",
            "-i", tmp_video,
            "-i", tmp_audio,
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            out_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", tmp_video,
            "-c:v", "copy",
            out_path,
        ]
    try:
        subprocess.run(cmd, check=True,
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except Exception:
        # ffmpeg 없으면 비디오만이라도 저장
        import shutil
        shutil.copy(tmp_video, out_path)
    finally:
        for p in [tmp_video, tmp_audio]:
            try: os.remove(p)
            except: pass


# ─────────────────────────────────────────────────────────────────────────────
# 메인 팝업 클래스
# ─────────────────────────────────────────────────────────────────────────────
