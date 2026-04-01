"""
audio_capture.py -- Windows WASAPI ProcessLoopback 캡처 진입점
저수준 COM 코드는 audio_com.py 에 분리됨
"""
import ctypes
import time
import platform
import numpy as np
import psutil
from multiprocessing import Queue, Value
from win32_utils import CFG, queue_put
from audio_com import (
    activate_process_loopback, audio_client_initialize,
    audio_client_set_event, audio_client_start, audio_client_stop,
    get_capture_client, get_next_packet_size, get_buffer, release_buffer,
    _com_release, _kernel32, _ole32, AUDCLNT_BUFFERFLAGS_SILENT,
    activate_global_loopback, audio_client_initialize_loopback,
)

# 하위 호환 별칭 (gui_record_backend 에서 직접 import 하는 이름)
_activate_process_loopback = activate_process_loopback
_audio_client_initialize   = audio_client_initialize
_audio_client_set_event    = audio_client_set_event
_audio_client_start        = audio_client_start
_audio_client_stop         = audio_client_stop
_get_capture_client        = get_capture_client
_get_next_packet_size      = get_next_packet_size
_get_buffer                = get_buffer
_release_buffer            = release_buffer

# ── Windows 빌드 확인 ─────────────────────────────────────────────────────────
def _windows_build() -> int:
    try:
        return int(platform.version().split(".")[-1])
    except Exception:
        return 0

_WIN_BUILD                = _windows_build()
_SUPPORT_PROCESS_LOOPBACK = (_WIN_BUILD >= 19041)

# ── 팟플레이어 PID 탐색 ────────────────────────────────────────────────────────
_pid_cache = [None, 0.0]

def _find_potplayer_pid():
    now = time.time()
    if _pid_cache[0] is not None and now - _pid_cache[1] < 0.5:
        return _pid_cache[0]
    for p in psutil.process_iter(["pid", "name"]):
        n = p.info["name"].lower()
        if "potplayer" in n or "pot player" in n:
            _pid_cache[0] = p.info["pid"]
            _pid_cache[1] = now
            return _pid_cache[0]
    _pid_cache[0] = None
    _pid_cache[1] = now
    return None

# ── 밴드패스 필터 ─────────────────────────────────────────────────────────────
def _make_bandpass(sr: int):
    try:
        from scipy.signal import butter, sosfilt as _sf
        sos = butter(4, [300, 3400], btype="bandpass", fs=sr, output="sos")
        return sos, _sf
    except Exception:
        return None, None

def _apply_filter(arr, sos, sosfilt):
    if sos is not None and sosfilt is not None:
        try:
            return sosfilt(sos, arr)
        except Exception:
            pass
    return arr

# ── 캡처 세션 ─────────────────────────────────────────────────────────────────
def _run_capture_session(pid: int, audio_queue: Queue,
                         stop_flag: Value, send_log):
    """MTA 스레드 래퍼."""
    import threading
    result_box = [None]

    def _session_mta():
        hr_co = _ole32.CoInitializeEx(None, 0x0)
        co_ok = hr_co in (0, 1)
        try:
            result_box[0] = _run_capture_impl(pid, audio_queue, stop_flag, send_log)
        except Exception as e:
            result_box[0] = (False, f"예외(MTA): {e}")
        finally:
            if co_ok:
                _ole32.CoUninitialize()

    t = threading.Thread(target=_session_mta, daemon=True)
    t.start()
    t.join()
    return result_box[0] if result_box[0] is not None else (False, "MTA 스레드 비정상 종료")


def _run_capture_impl(pid: int, audio_queue: Queue,
                      stop_flag: Value, send_log):
    """실제 캡처 루프."""
    client  = None
    cap     = None
    h_event = None
    try:
        client  = activate_process_loopback(pid)
        sr, ch  = audio_client_initialize(client)
        h_event = _kernel32.CreateEventW(None, False, False, None)
        audio_client_set_event(client, h_event)
        cap = get_capture_client(client)
        audio_client_start(client)

        sos, sosfilt  = _make_bandpass(sr)
        send_log(f"🎙 [ProcessLoopback] PID={pid} sr={sr} ch={ch}")

        RECHECK       = 3.0
        last_check    = time.time()
        cur_pid       = pid
        WAIT_MS       = 10
        first_packet  = True
        last_stat     = time.time()
        total_packets = 0

        while not stop_flag.value:
            now = time.time()
            if now - last_check >= RECHECK:
                last_check = now
                new_pid = _find_potplayer_pid()
                if new_pid is None:
                    return False, "팟플레이어 종료됨"
                if new_pid != cur_pid:
                    return False, f"PID 변경 ({cur_pid} → {new_pid}) — 재연결"

            if now - last_stat >= 10.0:
                last_stat = now
                try:
                    pkt_peek = get_next_packet_size(cap)
                except OSError as _e:
                    send_log(f"⚠ GetNextPacketSize 오류: {_e}")
                    pkt_peek = -1
                send_log(f"🔍 캡처 상태: total={total_packets} next={pkt_peek}")

            _kernel32.WaitForSingleObject(h_event, WAIT_MS)

            while not stop_flag.value:
                try:
                    pkt = get_next_packet_size(cap)
                except OSError as _e:
                    send_log(f"⚠ GetNextPacketSize: {_e}")
                    break
                if pkt == 0:
                    break

                data, num_frames, flg, _qpc = get_buffer(cap)
                total_packets += 1

                if first_packet:
                    first_packet = False
                    send_log(f"✅ 첫 패킷 수신! frames={num_frames}")

                if num_frames > 0:
                    if flg & AUDCLNT_BUFFERFLAGS_SILENT:
                        rms = 0.0
                    else:
                        if not data.value:
                            release_buffer(cap, num_frames)
                            continue
                        buf = (ctypes.c_float * (num_frames * ch)).from_address(data.value)
                        arr = np.frombuffer(buf, dtype=np.float32).copy()
                        if ch > 1:
                            arr = arr.reshape(-1, ch).mean(axis=1)
                        arr = _apply_filter(arr, sos, sosfilt)
                        rms = float(np.sqrt(np.mean(arr ** 2)))
                    queue_put(audio_queue, (time.time(), rms))

                release_buffer(cap, num_frames)

        return True, ""

    except OSError as e:
        return False, str(e)
    finally:
        if cap:
            try: audio_client_stop(client)
            except Exception: pass
            _com_release(cap)
        if client:
            _com_release(client)
        if h_event:
            _kernel32.CloseHandle(h_event)


# ── 진입점 ────────────────────────────────────────────────────────────────────
def _run_global_loopback_session(audio_queue: Queue, stop_flag, send_log):
    """
    전체 루프백 캡처 세션 (빌드 19041 미만 폴백).
    IMMDevice 기본 렌더 디바이스 loopback — 시스템 전체 오디오 캡처.
    """
    result_box = [None]

    def _mta():
        hr_co = _ole32.CoInitializeEx(None, 0x0)
        co_ok = hr_co in (0, 1)
        client  = None
        cap     = None
        h_event = None
        try:
            client  = activate_global_loopback()
            sr, ch  = audio_client_initialize_loopback(client)
            h_event = _kernel32.CreateEventW(None, False, False, None)
            audio_client_set_event(client, h_event)
            cap = get_capture_client(client)
            audio_client_start(client)

            sos, sosfilt = _make_bandpass(sr)
            send_log(f"🎙 [GlobalLoopback] sr={sr} ch={ch} (전체 루프백)")

            first_packet = True
            while not stop_flag.value:
                _kernel32.WaitForSingleObject(h_event, 10)
                while not stop_flag.value:
                    try:
                        pkt = get_next_packet_size(cap)
                    except OSError as e:
                        send_log(f"⚠ GetNextPacketSize: {e}")
                        result_box[0] = (False, str(e))
                        return
                    if pkt == 0:
                        break
                    data, num_frames, flg, _qpc = get_buffer(cap)
                    if first_packet:
                        first_packet = False
                        send_log("✅ 첫 패킷 수신! (전체 루프백)")
                    if num_frames > 0:
                        if flg & AUDCLNT_BUFFERFLAGS_SILENT:
                            rms = 0.0
                        else:
                            if not data.value:
                                release_buffer(cap, num_frames)
                                continue
                            buf = (ctypes.c_float * (num_frames * ch)).from_address(data.value)
                            arr = np.frombuffer(buf, dtype=np.float32).copy()
                            if ch > 1:
                                arr = arr.reshape(-1, ch).mean(axis=1)
                            arr = _apply_filter(arr, sos, sosfilt)
                            rms = float(np.sqrt(np.mean(arr ** 2)))
                        queue_put(audio_queue, (time.time(), rms))
                    release_buffer(cap, num_frames)
            result_box[0] = (True, "")
        except Exception as e:
            result_box[0] = (False, str(e))
        finally:
            if cap:
                try: audio_client_stop(client)
                except Exception: pass
                _com_release(cap)
            if client:
                _com_release(client)
            if h_event:
                _kernel32.CloseHandle(h_event)
            if co_ok:
                _ole32.CoUninitialize()

    import threading
    t = threading.Thread(target=_mta, daemon=True)
    t.start()
    t.join()
    return result_box[0] if result_box[0] is not None else (False, "MTA 스레드 비정상 종료")


def proc_audio_capture(audio_queue: Queue, stop_flag: Value, cfg: dict, log_queue=None):
    import sys, os
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

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

    if not _SUPPORT_PROCESS_LOOPBACK:
        send_log(f"⚠ ProcessLoopback 미지원 (빌드 {_WIN_BUILD} < 19041) — 전체 루프백으로 전환.")
        _retry = 0
        while not stop_flag.value:
            try:
                ok, reason = _run_global_loopback_session(audio_queue, stop_flag, send_log)
            except Exception as e:
                ok, reason = False, f"예외: {e}"
            if ok:
                _retry = 0
                continue
            send_log(f"⚠ 전체 루프백 실패: {reason}")
            _retry += 1
            send_log(f"🔄 {_retry}회 재시도 대기 중 (5초)...")
            for _ in range(50):
                if stop_flag.value:
                    break
                time.sleep(0.1)
        return

    _retry = 0
    while not stop_flag.value:
        pid = _find_potplayer_pid()
        if pid is None:
            send_log("⏳ 팟플레이어 실행 대기 중...")
            for _ in range(50):
                if stop_flag.value:
                    return
                time.sleep(0.1)
            continue

        try:
            ok, reason = _run_capture_session(pid, audio_queue, stop_flag, send_log)
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
