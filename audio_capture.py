"""
audio_capture.py — pyaudiowpatch 기반 WASAPI ProcessLoopback 캡처

팟플레이어 프로세스의 오디오만 캡처한다.
전체 루프백(기본 출력장치 캡처)은 제거됨.

요구사항:
  - Windows 10 20H1 (빌드 19041) 이상
  - pyaudiowpatch >= 0.2.12
"""

import time
import platform
import numpy as np
import psutil
from multiprocessing import Queue, Value
from win32_utils import CFG, queue_put, find_potplayer_hwnd, is_potplayer_playing


def _windows_build() -> int:
    try:
        return int(platform.version().split(".")[-1])
    except Exception:
        return 0

_WIN_BUILD                = _windows_build()
_SUPPORT_PROCESS_LOOPBACK = (_WIN_BUILD >= 19041)


def proc_audio_capture(audio_queue: Queue, stop_flag: Value, cfg: dict, log_queue=None):
    import sys, os
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    SR       = cfg["AUDIO_SR"]
    chunk_ms = 50
    CHUNK    = int(SR * chunk_ms / 1000)

    def send_log(msg: str):
        ts   = time.strftime("%H:%M:%S")
        full = f"[{ts}] {msg}"
        if log_queue is not None:
            try:
                log_queue.put_nowait(full)
                return
            except Exception:
                pass
        queue_put(audio_queue, ("LOG", full))

    send_log(f"ℹ Win build={_WIN_BUILD} | ProcessLoopback={_SUPPORT_PROCESS_LOOPBACK}")

    # Win 빌드 체크 — 19041 미만이면 즉시 종료
    if not _SUPPORT_PROCESS_LOOPBACK:
        send_log(f"✖ ProcessLoopback 미지원 (빌드 {_WIN_BUILD} < 19041). Windows 10 20H1 이상이 필요합니다.")
        return

    _pc = [None, 0.0]
    def find_potplayer_pid() -> "int | None":
        now = time.time()
        if _pc[0] is not None and now - _pc[1] < 0.5:
            return _pc[0]
        for p in psutil.process_iter(["pid", "name"]):
            n = p.info["name"].lower()
            if "potplayer" in n or "pot player" in n:
                _pc[0] = p.info["pid"]
                _pc[1] = now
                return _pc[0]
        _pc[0] = None
        _pc[1] = now
        return None

    def make_bandpass(sr: int):
        try:
            from scipy.signal import butter, sosfilt as _sf
            sos = butter(4, [300, 3400], btype="bandpass", fs=sr, output="sos")
            return sos, _sf
        except Exception:
            return None, None

    def apply_filter(arr, sos, sosfilt):
        if sos is not None and sosfilt is not None:
            try:
                return sosfilt(sos, arr)
            except Exception:
                pass
        return arr

    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        send_log("✖ pyaudiowpatch 없음 — pip install pyaudiowpatch")
        return

    # ──────────────────────────────────────────────────────────────────────────
    # 팟플레이어 ProcessLoopback 캡처
    # ──────────────────────────────────────────────────────────────────────────
    def capture_via_process_loopback() -> "tuple[bool, str]":
        pot_pid = find_potplayer_pid()
        if pot_pid is None:
            return False, "팟플레이어를 찾을 수 없음 — 실행 후 다시 시도하세요"

        try:
            pa = pyaudio.PyAudio()
        except Exception as e:
            return False, f"PyAudio 초기화 실패: {e}"

        device_info = None
        try:
            device_info = pa.get_process_loopback_device(pot_pid)
        except AttributeError:
            pa.terminate()
            return False, "get_process_loopback_device 미지원 — pyaudiowpatch >= 0.2.12 필요"
        except Exception as e:
            pa.terminate()
            return False, f"get_process_loopback_device 오류: {e}"

        if device_info is None:
            pa.terminate()
            return False, "ProcessLoopback 장치를 가져오지 못함 (팟플레이어가 오디오를 출력 중인지 확인)"

        sr  = int(device_info["defaultSampleRate"])
        ch  = max(int(device_info["maxInputChannels"]), 1)
        idx = int(device_info["index"])
        sos, sosfilt = make_bandpass(sr)

        try:
            stream = pa.open(
                format=pyaudio.paFloat32,
                channels=ch,
                rate=sr,
                input=True,
                input_device_index=idx,
                frames_per_buffer=CHUNK,
            )
        except Exception as e:
            pa.terminate()
            return False, f"스트림 오픈 실패: {e}"

        send_log(f"🎙 [ProcessLoopback] PID={pot_pid} sr={sr} ch={ch}")
        cur_pid    = pot_pid
        RECHECK    = 3.0
        last_check = time.time()

        try:
            while not stop_flag.value:
                now = time.time()
                if now - last_check >= RECHECK:
                    last_check = now
                    new_pid = find_potplayer_pid()
                    if new_pid is None:
                        return False, "팟플레이어 종료됨"
                    if new_pid != cur_pid:
                        return False, f"PID 변경 ({cur_pid} → {new_pid}) — 재연결"

                try:
                    raw = stream.read(CHUNK, exception_on_overflow=False)
                except Exception as e:
                    return False, f"read 오류: {e}"

                arr = np.frombuffer(raw, dtype=np.float32)
                if ch > 1:
                    arr = arr.reshape(-1, ch).mean(axis=1)
                arr = apply_filter(arr, sos, sosfilt)
                queue_put(audio_queue, (time.time(), float(np.sqrt(np.mean(arr ** 2)))))
        finally:
            try: stream.stop_stream(); stream.close()
            except Exception: pass
            try: pa.terminate()
            except Exception: pass

        return True, ""

    # ──────────────────────────────────────────────────────────────────────────
    # 메인 루프 — ProcessLoopback 전용
    # ──────────────────────────────────────────────────────────────────────────
    _retry = 0
    while not stop_flag.value:
        try:
            ok, reason = capture_via_process_loopback()
        except Exception as e:
            ok, reason = False, f"예외: {e}"

        if ok:
            _retry = 0
            continue

        send_log(f"⚠ ProcessLoopback 실패: {reason}")
        _retry += 1
        send_log(f"🔄 {_retry}회 재시도 대기 중 (5초)...")
        for _ in range(50):
            if stop_flag.value:
                break
            time.sleep(0.1)
