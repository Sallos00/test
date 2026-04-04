"""
audio_capture.py -- Windows WASAPI ProcessLoopback 캡처 진입점

audio_queue에 (t_hw, rms, vad) 튜플을 전송한다.
  t_hw : QPC 하드웨어 타임스탬프 (초) — proc_analyzer와 동일한 클럭 기준
  rms  : 원본 신호 RMS — OP/ED 음악 감지에 사용
  vad  : 음성 감지 이진값 (0.0 / 1.0) — 싱크 보정에 사용
         ZCR + RMS 조합으로 BGM과 대사를 구분
           · ZCR 1000~3500 /sec + RMS > 5e-3 → 음성(1)
           · 그 외 (BGM, 무음) → 비음성(0)
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
    qpc_freq, qpc_now,
)

# Windows 빌드 확인 — ProcessLoopback은 빌드 19041(20H1) 이상에서만 지원
def _windows_build() -> int:
    try:
        return int(platform.version().split(".")[-1])
    except Exception:
        return 0

_WIN_BUILD                = _windows_build()
_SUPPORT_PROCESS_LOOPBACK = (_WIN_BUILD >= 19041)

# 팟플레이어 PID 탐색 (0.5초 캐시)
_pid_cache = [None, 0.0]

def _find_potplayer_pid():
    now = time.time()
    if _pid_cache[0] is not None and now - _pid_cache[1] < 0.5:
        return _pid_cache[0]
    for p in psutil.process_iter(["pid", "name"]):
        if "potplayer" in p.info["name"].lower():
            _pid_cache[0] = p.info["pid"]
            _pid_cache[1] = now
            return _pid_cache[0]
    _pid_cache[0] = None
    _pid_cache[1] = now
    return None


# ── VAD (Voice Activity Detection) ────────────────────────────────────────────
# ZCR(Zero Crossing Rate) + RMS 조합으로 판별.
# 애니 BGM도 300~3400Hz 대역에 에너지가 집중되므로 주파수 비율만으로는 구분 불가.
# 사람 목소리는 성대 진동 특성상 ZCR이 1000~3500 /sec 범위에 집중되는 반면
# BGM은 악기가 혼합되어 ZCR 범위가 넓고 불규칙하다.

_VAD_ZCR_LOW  = 1000    # 음성 ZCR 하한 (crosses/sec)
_VAD_ZCR_HIGH = 3500    # 음성 ZCR 상한 (crosses/sec)
_VAD_MIN_RMS  = 5e-3    # 이 이하는 무음으로 판정

def _compute_vad(arr: np.ndarray, sr: int) -> float:
    """
    모노 float32 PCM 배열에서 음성 여부를 판별.
    반환: 1.0 (음성) / 0.0 (BGM·무음)
    """
    if len(arr) < 16:
        return 0.0

    rms = float(np.sqrt(np.mean(arr ** 2)))
    if rms < _VAD_MIN_RMS:
        return 0.0

    zcr = float(np.sum(np.abs(np.diff(np.sign(arr)))) / 2) / (len(arr) / sr)
    return 1.0 if _VAD_ZCR_LOW <= zcr <= _VAD_ZCR_HIGH else 0.0


# ── PCM 버퍼 처리 공통 로직 ───────────────────────────────────────────────────

def _process_buffer(data, num_frames, flg, qp, ch, sr, _freq):
    """버퍼에서 (t_hw, rms, vad) 튜플을 계산해 반환."""
    t_hw = (qp / _freq) if qp > 0 else (qpc_now() / _freq)

    if flg & AUDCLNT_BUFFERFLAGS_SILENT or not data.value:
        return t_hw, 0.0, 0.0

    buf = (ctypes.c_float * (num_frames * ch)).from_address(data.value)
    arr = np.frombuffer(buf, dtype=np.float32).copy()
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)

    rms = float(np.sqrt(np.mean(arr ** 2)))
    vad = _compute_vad(arr, sr)
    return t_hw, rms, vad


# ── ProcessLoopback 캡처 세션 ─────────────────────────────────────────────────

def _run_capture_session(pid: int, audio_queue: Queue, stop_flag: Value, send_log):
    """MTA 스레드에서 ProcessLoopback 캡처를 실행하는 래퍼."""
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


def _run_capture_impl(pid: int, audio_queue: Queue, stop_flag: Value, send_log):
    client  = None
    cap     = None
    h_event = None
    _freq   = qpc_freq()

    try:
        client  = activate_process_loopback(pid)
        sr, ch  = audio_client_initialize(client)
        h_event = _kernel32.CreateEventW(None, False, False, None)
        audio_client_set_event(client, h_event)
        cap = get_capture_client(client)
        audio_client_start(client)
        send_log(f"🎙 [ProcessLoopback] PID={pid} sr={sr} ch={ch}")

        RECHECK       = 3.0
        WAIT_MS       = 10
        last_check    = time.time()
        last_stat     = time.time()
        cur_pid       = pid
        first_packet  = True
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
                except OSError as e:
                    send_log(f"⚠ GetNextPacketSize 오류: {e}")
                    pkt_peek = -1
                send_log(f"🔍 캡처 상태: total={total_packets} next={pkt_peek}")

            _kernel32.WaitForSingleObject(h_event, WAIT_MS)

            while not stop_flag.value:
                try:
                    pkt = get_next_packet_size(cap)
                except OSError as e:
                    send_log(f"⚠ GetNextPacketSize: {e}")
                    break
                if pkt == 0:
                    break

                data, num_frames, flg, qp = get_buffer(cap)
                total_packets += 1

                if first_packet:
                    first_packet = False
                    send_log(f"✅ 첫 패킷 수신! frames={num_frames}")

                if num_frames > 0:
                    t_hw, rms, vad = _process_buffer(data, num_frames, flg, qp, ch, sr, _freq)
                    queue_put(audio_queue, (t_hw, rms, vad))

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


# ── GlobalLoopback 캡처 세션 (빌드 19041 미만 폴백) ──────────────────────────

def _run_global_loopback_session(audio_queue: Queue, stop_flag, send_log):
    """MTA 스레드에서 전체 루프백 캡처를 실행하는 래퍼."""
    result_box = [None]

    def _mta():
        hr_co = _ole32.CoInitializeEx(None, 0x0)
        co_ok = hr_co in (0, 1)
        client  = None
        cap     = None
        h_event = None
        _freq   = qpc_freq()
        try:
            client  = activate_global_loopback()
            sr, ch  = audio_client_initialize_loopback(client)
            h_event = _kernel32.CreateEventW(None, False, False, None)
            audio_client_set_event(client, h_event)
            cap = get_capture_client(client)
            audio_client_start(client)
            send_log(f"🎙 [GlobalLoopback] sr={sr} ch={ch}")

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

                    data, num_frames, flg, qp = get_buffer(cap)
                    if first_packet:
                        first_packet = False
                        send_log("✅ 첫 패킷 수신! (전체 루프백)")

                    if num_frames > 0:
                        t_hw, rms, vad = _process_buffer(data, num_frames, flg, qp, ch, sr, _freq)
                        queue_put(audio_queue, (t_hw, rms, vad))

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


# ── 진입점 ────────────────────────────────────────────────────────────────────

def proc_audio_capture(audio_queue: Queue, stop_flag: Value, cfg: dict, log_queue=None):
    import sys, os
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    def send_log(msg: str):
        full = f"[{time.strftime('%H:%M:%S')}] {msg}"
        if log_queue is not None:
            try:
                log_queue.put_nowait(full)
                return
            except Exception:
                pass
        queue_put(audio_queue, ("LOG", full))

    send_log(f"ℹ Win build={_WIN_BUILD} | ProcessLoopback={_SUPPORT_PROCESS_LOOPBACK}")

    if not _SUPPORT_PROCESS_LOOPBACK:
        send_log(f"⚠ ProcessLoopback 미지원 (빌드 {_WIN_BUILD} < 19041) — 전체 루프백으로 전환")
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
