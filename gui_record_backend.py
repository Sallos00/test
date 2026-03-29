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
    pyaudiowpatch WASAPI loopback으로 팟플레이어 오디오 캡처.
    포커스 무관, Windows 10/11 모두 지원.
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
            try:
                import pyaudiowpatch as pyaudio
            except ImportError:
                self._running = False
                return

            pa = pyaudio.PyAudio()
            try:
                # 팟플레이어 프로세스가 사용하는 오디오 세션 찾기
                # pyaudiowpatch는 GetProcessLoopback을 지원 — pid로 특정 프로세스만 캡처
                loopback_device = None
                try:
                    wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
                    default_out_idx = wasapi_info["defaultOutputDevice"]
                    device_info = pa.get_device_info_by_index(default_out_idx)
                    # loopback 디바이스 탐색
                    for i in range(pa.get_device_count()):
                        d = pa.get_device_info_by_index(i)
                        if d.get("isLoopbackDevice") and d["name"] == device_info["name"]:
                            loopback_device = d
                            loopback_device["index"] = i
                            break
                except Exception:
                    pass

                if loopback_device is None:
                    self._running = False
                    return

                sr = int(loopback_device["defaultSampleRate"])
                ch = min(loopback_device["maxInputChannels"], 2)
                self._sr = sr
                self._ch = ch

                def _cb(in_data, frame_count, time_info, status):
                    if self._running and in_data:
                        arr = np.frombuffer(in_data, dtype=np.float32).copy()
                        self._frames.append(arr)
                    return (None, pyaudio.paContinue)

                stream = pa.open(
                    format=pyaudio.paFloat32,
                    channels=ch,
                    rate=sr,
                    input=True,
                    input_device_index=loopback_device["index"],
                    frames_per_buffer=1024,
                    stream_callback=_cb,
                    as_loopback=True,
                    loopback_process_id=pid,
                )
                stream.start_stream()
                while self._running:
                    import time as _t
                    _t.sleep(0.05)
                stream.stop_stream()
                stream.close()
            except Exception:
                # loopback_process_id 미지원 버전이면 전체 루프백으로 fallback
                try:
                    self._frames = []
                    wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
                    default_out_idx = wasapi_info["defaultOutputDevice"]
                    device_info = pa.get_device_info_by_index(default_out_idx)
                    loopback_device = None
                    for i in range(pa.get_device_count()):
                        d = pa.get_device_info_by_index(i)
                        if d.get("isLoopbackDevice") and d["name"] == device_info["name"]:
                            loopback_device = d
                            loopback_device["index"] = i
                            break
                    if loopback_device:
                        sr = int(loopback_device["defaultSampleRate"])
                        ch = min(loopback_device["maxInputChannels"], 2)
                        self._sr = sr
                        self._ch = ch
                        def _cb2(in_data, frame_count, time_info, status):
                            if self._running and in_data:
                                arr = np.frombuffer(in_data, dtype=np.float32).copy()
                                self._frames.append(arr)
                            return (None, pyaudio.paContinue)
                        stream2 = pa.open(
                            format=pyaudio.paFloat32,
                            channels=ch,
                            rate=sr,
                            input=True,
                            input_device_index=loopback_device["index"],
                            frames_per_buffer=1024,
                            stream_callback=_cb2,
                        )
                        stream2.start_stream()
                        while self._running:
                            import time as _t
                            _t.sleep(0.05)
                        stream2.stop_stream()
                        stream2.close()
                except Exception:
                    pass
            finally:
                pa.terminate()

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
