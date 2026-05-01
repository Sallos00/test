"""gui/record_backend.py -- 오디오/화면 녹화 백엔드 (OBS 방식 싱크)"""
import os, time, threading, subprocess, tempfile, ctypes, ctypes.wintypes as wt, struct
from mem_utils import run_gc

try:
    import cv2, numpy as np; _CV2_OK = True
except ImportError:
    _CV2_OK = False

import tkinter as tk

# 헬퍼 함수 / 캡처 루프 / 오디오 처리는 _record_impl 에서 가져온다
# (Pyarmor 8 trial 코드 객체 한도 대응을 위한 파일 분리)
from gui._record_impl import (
    _log, _debug_log, _WIN_OK,
    _get_potplayer_video_hwnd, _get_potplayer_rect,
    _find_ffmpeg, check_ffmpeg, download_ffmpeg, get_ffmpeg_path,
    _printwindow_capture,
    _mss_capture_loop, _wgc_capture_hwnd, _printwindow_loop,
    _retiming_audio,
    _write_audio_chunk, _read_audio_chunks_from_file,
    _audio_recorder_mta,
)

# ── 오버레이 ──────────────────────────────────────────────────────────────────
_active_overlays = []
_GWLP_HWNDPARENT = -8

def _ov_track(root, ov, closed_flag):
    if closed_flag[0]:
        return
    try:
        if not ov.winfo_exists():
            return
    except Exception:
        return
    r = _get_potplayer_rect()
    if r:
        try: ov.geometry(f"+{r[0]+12}+{r[1]+12}")
        except Exception: pass
    try: root.after(150, _ov_track, root, ov, closed_flag)
    except Exception: pass

def _ov_close(ov, closed_flag):
    closed_flag[0] = True
    try: ov.destroy()
    except Exception: pass
    try: _active_overlays.remove(ov)
    except ValueError: pass

def _show_overlay(root, message, duration_ms=3000):
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
        ov.geometry(f"+{px+12}+{py+12}")
        tk.Label(ov, text=message, font=("Segoe UI",11,"bold"),
                 bg="#101010", fg="#00c8e0", padx=14, pady=8).pack()
        ov.update_idletasks()
        if pot_hwnd and _WIN_OK:
            try:
                tk_hwnd = int(ov.winfo_id())
                ov_hwnd = ctypes.windll.user32.GetAncestor(tk_hwnd, 2) or tk_hwnd
                try: ctypes.windll.user32.SetWindowLongPtrW(ov_hwnd, _GWLP_HWNDPARENT, pot_hwnd)
                except AttributeError: ctypes.windll.user32.SetWindowLongW(ov_hwnd, _GWLP_HWNDPARENT, pot_hwnd)
                ctypes.windll.user32.SetWindowPos(ov_hwnd, 0, 0, 0, 0, 0, 0x0002|0x0001|0x0004|0x0020)
            except Exception: pass
        _active_overlays.append(ov)
        closed_flag = [False]
        root.after(150, _ov_track, root, ov, closed_flag)
        if duration_ms > 0:
            root.after(duration_ms, _ov_close, ov, closed_flag)
        return ov
    except Exception:
        return None

def _toggle_overlays(show):
    for ov in list(_active_overlays):
        try: (ov.deiconify if show else ov.withdraw)()
        except Exception: pass

# ── 오디오 녹화기 ─────────────────────────────────────────────────────────────
class _AudioRecorder:
    def __init__(self):
        self._chunks = []; self._sr = 48000; self._ch = 2
        self._running = False; self._thread = None
        self._first_audio_qpc_sec = 0.0; self._pid = 0
        # 스트리밍 모드 (녹화 중 청크를 메모리 대신 임시 파일에 기록)
        self._tmp_audio_path = None
        self._tmp_audio_fh   = None
        self._chunk_lock     = threading.Lock()
        self._stream_mode    = False

    def start(self, pid):
        self._chunks = []; self._running = True; self._pid = pid
        self._first_audio_qpc_sec = 0.0
        # 임시 파일 생성 — 실패 시 기존 메모리 방식으로 자동 폴백
        self._stream_mode    = False
        self._tmp_audio_path = None
        self._tmp_audio_fh   = None
        try:
            fd, path = tempfile.mkstemp(suffix='.raw', prefix='autosinc_audio_')
            os.close(fd)
            self._tmp_audio_fh   = open(path, 'wb')
            self._tmp_audio_path = path
            self._stream_mode    = True
        except Exception:
            pass
        self._thread = threading.Thread(target=_audio_recorder_mta,
                                        args=(self,), daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

        # 임시 파일 닫기 (쓰기 스레드 종료 후)
        fh = self._tmp_audio_fh
        self._tmp_audio_fh = None
        if fh is not None:
            try: fh.flush(); fh.close()
            except Exception: pass

        # 스트리밍 모드: 파일에서 청크 복원
        file_chunks = []
        if self._stream_mode and self._tmp_audio_path:
            try:
                file_chunks = _read_audio_chunks_from_file(self._tmp_audio_path)
            except Exception:
                pass
            finally:
                try: os.remove(self._tmp_audio_path)
                except Exception: pass
                self._tmp_audio_path = None
                self._stream_mode = False

        # 멤버 chunks 즉시 비움 -> GC 가능 상태로 전환
        mem_chunks = self._chunks
        self._chunks = []

        # 파일 청크 우선, 없으면 메모리 청크(폴백) 사용
        chunks = file_chunks if file_chunks else mem_chunks

        if chunks:
            try:
                arr, start_qpc = _retiming_audio(chunks, self._sr, self._ch)
                self._first_audio_qpc_sec = start_qpc
                run_gc()
                return arr, self._sr, self._ch
            finally:
                del chunks
                del mem_chunks
        return None, self._sr, self._ch

# ── 화면 녹화기 ───────────────────────────────────────────────────────────────
class _ScreenRecorder:
    def __init__(self):
        self._running_flag = threading.Event()
        self._thread = None
        self._fps = 30; self._size = (1280, 720); self._hwnd = None
        self._ffmpeg_proc = None; self._ffmpeg_log_path = None
        self._ffmpeg_log_fh = None; self._tmp_video = None
        self._first_frame_qpc_sec = 0.0; self._frame_count = 0
        self._lock = threading.Lock()

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
        for p in (self._tmp_video, self._ffmpeg_log_path):
            try: os.remove(p)
            except Exception: pass
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
            self._ffmpeg_proc   = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=self._ffmpeg_log_fh, creationflags=0x08000000)
        except Exception as e:
            raise RuntimeError(f"ffmpeg 실행 실패: {e}")
        self._running_flag.set()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _write_frame(self, bgr_frame, frame_qpc):
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
            _mss_capture_loop(self._size[0], self._size[1], self._fps,
                               self._running_flag, self._write_frame)
        except Exception as e:
            _log(f"캡처 루프 예외: {e}")
        finally:
            if self._ffmpeg_proc and self._ffmpeg_proc.stdin:
                try: self._ffmpeg_proc.stdin.close()
                except Exception: pass

    def stop(self):
        self._running_flag.clear()
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive() and self._ffmpeg_proc and self._ffmpeg_proc.stdin:
                try: self._ffmpeg_proc.stdin.close()
                except Exception: pass
        if self._ffmpeg_proc:
            try:
                if self._ffmpeg_proc.stdin:
                    self._ffmpeg_proc.stdin.close()
            except Exception: pass
            try:
                self._ffmpeg_proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._ffmpeg_proc.kill(); self._ffmpeg_proc.wait()
            finally:
                try:
                    if self._ffmpeg_log_fh: self._ffmpeg_log_fh.close()
                except Exception: pass
            rc = self._ffmpeg_proc.returncode
            if rc != 0:
                try:
                    import shutil as _sh
                    if self._ffmpeg_log_path and os.path.isfile(self._ffmpeg_log_path):
                        dst = os.path.join(tempfile.gettempdir(), "autosinc_ffmpeg.log")
                        _sh.copy2(self._ffmpeg_log_path, dst)
                except Exception: pass
                raise RuntimeError(f"ffmpeg 인코딩 실패 (code={rc})\n로그: {self._ffmpeg_log_path}")
            else:
                try: os.remove(self._ffmpeg_log_path)
                except Exception: pass
        _log(f"[OBS싱크] 영상 총 프레임: {self._frame_count}")
        tmp = self._tmp_video
        if not tmp or not os.path.isfile(tmp):
            raise RuntimeError("녹화 파일 없음")
        if os.path.getsize(tmp) < 1024:
            raise RuntimeError(f"녹화 파일 너무 작음({os.path.getsize(tmp)}B)")
        self._thread = None; self._ffmpeg_proc = None
        self._ffmpeg_log_fh = None; self._ffmpeg_log_path = None
        self._hwnd = None
        self._tmp_video = None
        return tmp

# ── 오디오 병합 및 최종 저장 ──────────────────────────────────────────────────
def _merge_audio(tmp_video, audio_arr, audio_sr, audio_ch, out_path, audio_offset_sec=0.0):
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
    if audio_ch > 1:
        rem = len(audio_arr) % audio_ch
        if rem: audio_arr = audio_arr[:-rem]
        audio_data = audio_arr.reshape(-1, audio_ch)
    else:
        audio_data = audio_arr.reshape(-1, 1)
    pcm        = (audio_data * 32767).clip(-32768, 32767).astype(np.int16)
    num_frames = audio_data.shape[0]
    data_size  = num_frames * audio_ch * 2
    wav_hdr = (b"RIFF" + struct.pack("<I", 36+data_size) + b"WAVEfmt " +
               struct.pack("<IHHIIHH", 16, 1, audio_ch, audio_sr,
                           audio_sr*audio_ch*2, audio_ch*2, 16) +
               b"data" + struct.pack("<I", data_size))
    with open(tmp_audio, "wb") as wf:
        wf.write(wav_hdr + pcm.tobytes())
    del pcm, audio_data
    _offset = round(audio_offset_sec, 4)
    _log(f"[OBS싱크] 최종 오프셋 보정: audio_offset={_offset:.4f}s")
    base_cmd = [ffmpeg_bin, "-y", "-i", tmp_video]
    if _offset >= 0.005:   base_cmd += ["-itsoffset", str(_offset)]
    elif _offset <= -0.005: base_cmd += ["-ss", str(-_offset)]
    cmd = base_cmd + ["-i", tmp_audio, "-c:v", "copy", "-c:a", "aac",
                      "-b:a", "192k", "-shortest", "-movflags", "+faststart", tmp_out]
    with open(merge_log, "wb") as lf:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=lf, creationflags=0x08000000)
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()
    if proc.returncode == 0 and os.path.isfile(tmp_out) and os.path.getsize(tmp_out) > 1024:
        shutil.move(tmp_out, out_path)
        try: os.remove(merge_log)
        except Exception: pass
    else:
        try:
            dst = os.path.join(tempfile.gettempdir(), "autosinc_merge.log")
            import shutil as _sh; _sh.copy2(merge_log, dst)
        except Exception: pass
        if tmp_video != out_path:
            shutil.move(tmp_video, out_path)
    for p in (tmp_audio, tmp_out):
        try: os.remove(p)
        except Exception: pass

def _save_mp4(tmp_video, audio_arr, audio_sr, audio_ch, out_path, audio_offset_sec=0.0):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    _merge_audio(tmp_video, audio_arr, audio_sr, audio_ch, out_path, audio_offset_sec)
