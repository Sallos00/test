"""
audio_capture.py — pyaudiowpatch 기반 WASAPI 루프백 캡처

pyaudiowpatch 가 ProcessLoopback / 전체 루프백을 이미 안정적으로 구현하고 있으므로
ctypes COM 코드를 전부 제거하고 이 라이브러리에 위임한다.

우선순위:
  1. pyaudiowpatch ProcessLoopback  — 팟플레이어 프로세스만 캡처 (Win10 20H1+)
  2. pyaudiowpatch 전체 루프백      — 기본 출력장치 전체 캡처 (하위 호환)
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
    # 방법 1: ProcessLoopback (팟플레이어 PID 지정)
    # ──────────────────────────────────────────────────────────────────────────
    def capture_via_process_loopback() -> "tuple[bool, str]":
        if not _SUPPORT_PROCESS_LOOPBACK:
            return False, f"미지원 빌드 ({_WIN_BUILD} < 19041)"

        pot_pid = find_potplayer_pid()
        if pot_pid is None:
            return False, "팟플레이어 PID 없음"

        try:
            pa = pyaudio.PyAudio()
        except Exception as e:
            return False, f"PyAudio 초기화 실패: {e}"

        # get_process_loopback_device 는 pyaudiowpatch >= 0.2.12
        device_info = None
        try:
            device_info = pa.get_process_loopback_device(pot_pid)
        except AttributeError:
            pa.terminate()
            return False, "get_process_loopback_device 미지원 (pyaudiowpatch 버전)"
        except Exception as e:
            pa.terminate()
            return False, f"get_process_loopback_device 오류: {e}"

        if device_info is None:
            pa.terminate()
            return False, "ProcessLoopback 장치 없음"

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
                        return False, "팟플레이어 종료"
                    if new_pid != cur_pid:
                        return False, f"PID 변경 → 재연결"

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
    # 방법 2: 전체 루프백 (기본 출력장치)
    # ──────────────────────────────────────────────────────────────────────────
    def capture_via_loopback() -> "tuple[bool, str]":
        try:
            pa = pyaudio.PyAudio()
        except Exception as e:
            return False, f"PyAudio 초기화 실패: {e}"

        device_info = None
        try:
            device_info = pa.get_default_wasapi_loopback()
        except AttributeError:
            # 구버전 fallback
            try:
                wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
                default_idx = wasapi_info["defaultOutputDevice"]
                raw_info    = pa.get_device_info_by_index(default_idx)
                # pyaudiowpatch 는 출력장치를 루프백으로 직접 쓸 수 있음
                device_info = raw_info
            except Exception as e:
                pa.terminate()
                return False, f"WASAPI 장치 탐색 실패: {e}"
        except Exception as e:
            pa.terminate()
            return False, f"get_default_wasapi_loopback 실패: {e}"

        if device_info is None:
            pa.terminate()
            return False, "루프백 장치 없음"

        sr  = int(device_info["defaultSampleRate"])
        # 루프백 장치는 maxInputChannels 로 채널 수를 줌;
        # 0이면 maxOutputChannels 사용
        ch  = int(device_info.get("maxInputChannels") or
                  device_info.get("maxOutputChannels") or 2)
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
            return False, f"루프백 스트림 오픈 실패: {e}"

        send_log(f"🎙 [Loopback] 전체 루프백 sr={sr} ch={ch} idx={idx}")
        RECHECK    = 3.0
        last_check = time.time()
        pot_active = False

        def _check_pot() -> bool:
            hwnd = find_potplayer_hwnd()
            return bool(hwnd) and is_potplayer_playing(hwnd)

        try:
            while not stop_flag.value:
                now = time.time()
                if now - last_check >= RECHECK:
                    last_check = now
                    was        = pot_active
                    pot_active = _check_pot()
                    if pot_active and not was:
                        send_log("🎙 [Loopback] 팟플레이어 재생 감지 → 캡처 활성")
                    elif not pot_active and was:
                        send_log("⚠ [Loopback] 팟플레이어 재생 중지 → 대기")

                try:
                    raw = stream.read(CHUNK, exception_on_overflow=False)
                except Exception as e:
                    return False, f"read 오류: {e}"

                if not pot_active:
                    queue_put(audio_queue, (time.time(), 0.0))
                    continue

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
    # 메인 루프
    # ──────────────────────────────────────────────────────────────────────────
    _retry = 0
    while not stop_flag.value:

        if _SUPPORT_PROCESS_LOOPBACK:
            try:
                ok, reason = capture_via_process_loopback()
            except Exception as e:
                ok, reason = False, f"예외: {e}"
            if ok:
                _retry = 0
                continue
            send_log(f"⚠ ProcessLoopback 실패: {reason}")

        try:
            ok, reason = capture_via_loopback()
        except Exception as e:
            ok, reason = False, f"예외: {e}"
        if ok:
            _retry = 0
            continue

        send_log(f"⚠ 루프백 실패: {reason}")
        _retry += 1
        send_log(f"🔄 {_retry}회 재시도 대기 중 (5초)...")
        for _ in range(50):
            if stop_flag.value:
                break
            time.sleep(0.1)
