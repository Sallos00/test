"""
audio_capture.py -- Windows WASAPI ProcessLoopback 캡처 진입점

audio_queue에 (t_stream, rms, vad) 튜플을 전송한다.
  t_stream : 오디오 스트림 기준 위치(초) — 립 캡처와 공통 기준점
             첫 패킷의 (qp_origin, dp_origin, sr)로 확립한 선형 관계로 변환
             lip_capture도 qpc_now()를 동일 공식으로 변환해 기준 통일
  rms      : 원본 신호 RMS — OP/ED 음악 감지에 사용
  vad      : 음성 감지 이진값 (0.0 / 1.0) — 싱크 보정에 사용
             ZCR + RMS 조합으로 BGM과 대사를 구분
"""
import ctypes
import time
import threading
import platform
# multiprocessing Queue/Value 불필요 (스레드 전환)
from win32_utils import CFG, queue_put
from audio_com import (
    activate_process_loopback, audio_client_initialize,
    audio_client_set_event, audio_client_start, audio_client_stop,
    get_capture_client, get_next_packet_size, get_buffer, release_buffer,
    _com_release, _kernel32, _ole32, AUDCLNT_BUFFERFLAGS_SILENT,
    activate_global_loopback, audio_client_initialize_loopback,
    qpc_freq, qpc_now,
)
from log_utils import make_send_log

# Windows 빌드 확인 — ProcessLoopback은 빌드 19041(20H1) 이상에서만 지원
def _windows_build() -> int:
    try:
        return int(platform.version().split(".")[-1])
    except Exception:
        return 0

_WIN_BUILD                = _windows_build()
_SUPPORT_PROCESS_LOOPBACK = (_WIN_BUILD >= 19041)

# 팟플레이어 PID 탐색 (5초 캐시)
# ── [재연결 수정] threading.Lock으로 Race Condition 방지 ──────────────────────
# _pid_cache는 T2(proc_audio_capture)와 oped 모니터 스레드 등 복수 스레드에서
# 동시에 읽고 쓸 수 있으므로 Lock으로 보호한다.
_pid_lock  = threading.Lock()
_pid_cache = [None, 0.0]

def invalidate_pid_cache():
    """PID 캐시를 즉시 무효화한다.
    팟플레이어 종료/재시작 시 _stop_processes() 또는 새 캡처 세션 시작 시 호출해
    종료된 팟플레이어의 stale PID가 새 T2에서 재사용되지 않도록 한다.
    """
    with _pid_lock:
        _pid_cache[0] = None
        _pid_cache[1] = 0.0

def _find_potplayer_pid():
    import psutil
    now = time.time()
    # ── 캐시 유효 여부 확인 (Lock 안에서만 읽기) ───────────────────────────
    with _pid_lock:
        if now - _pid_cache[1] < 5.0:
            return _pid_cache[0]
    # 캐시 만료 → 전체 프로세스 재탐색 (Lock 밖에서 수행 — psutil 블로킹 방지)
    found_pid = None
    try:
        for p in psutil.process_iter(["pid", "name"]):
            if "potplayer" in p.info["name"].lower():
                found_pid = p.info["pid"]
                break
    except Exception:
        pass
    # 탐색 결과를 Lock 안에서 캐시에 기록
    with _pid_lock:
        _pid_cache[0] = found_pid
        _pid_cache[1] = now
    return found_pid


# ── VAD (Voice Activity Detection) ────────────────────────────────────────────
# ZCR(Zero Crossing Rate) + RMS 조합으로 판별.
# 애니 BGM도 300~3400Hz 대역에 에너지가 집중되므로 주파수 비율만으로는 구분 불가.
# 사람 목소리는 성대 진동 특성상 ZCR이 1000~3500 /sec 범위에 집중되는 반면
# BGM은 악기가 혼합되어 ZCR 범위가 넓고 불규칙하다.

_VAD_ZCR_LOW  = 1000    # 음성 ZCR 하한 (crosses/sec)
_VAD_ZCR_HIGH = 3500    # 음성 ZCR 상한 (crosses/sec)
_VAD_MIN_RMS  = 5e-3    # 이 이하는 무음으로 판정

def _compute_vad(arr, sr: int) -> float:
    """모노 float32 PCM 배열에서 음성 여부를 판별. 반환: 1.0 (음성) / 0.0 (BGM·무음)"""
    import numpy as np
    if len(arr) < 16:
        return 0.0
    rms = float(np.sqrt(np.mean(arr ** 2)))
    if rms < _VAD_MIN_RMS:
        return 0.0
    zcr = float(np.sum(np.abs(np.diff(np.sign(arr)))) / 2) / (len(arr) / sr)
    return 1.0 if _VAD_ZCR_LOW <= zcr <= _VAD_ZCR_HIGH else 0.0


# ── 스트림 기준 타임스탬프 변환 ───────────────────────────────────────────────
# 오디오의 qp(DAC 출력 QPC틱)와 립의 qpc_now()는 같은 QPC 클럭이지만
# 가리키는 사건이 달라 계통 오차가 발생한다.
# 첫 패킷의 (qp_origin, dp_origin)으로 선형 변환식을 확립하면
# 임의의 QPC 틱 → 스트림 위치(초)로 통일할 수 있다:
#   t_stream = (qp - qp_origin) / freq + dp_origin / sr
# 립도 동일 공식으로 변환하면 두 신호가 완전히 같은 기준축을 갖는다.

def _make_stream_converter(dp_origin: int, qp_origin: int, sr: int, freq: int):
    """QPC 틱 → qp_origin 기준 경과시간(초) 변환 함수.

    [Bug A 수정] dp_origin/sr 오프셋 제거.
    dp_origin = WASAPI 렌더 스트림의 절대 샘플 위치 (재생 시작부터 누적 프레임).
    영상 2분 재생 중 싱크 시작 시 dp_origin/sr ≈ 120s → 오디오 ts가 120s부터 시작.
    proc_lip_capture의 t_hw는 qp_origin 기준 0s부터 시작, stream_anchor에
    dp_origin이 없어 보정 불가 → resample_aligned 겹침 구간 항상 음수 → None.
    수정: 두 신호 모두 qp_origin 기준 경과시간(0s~)으로 통일.
    dp_origin은 하위호환 서명 유지를 위해 인자로 받되 사용하지 않음.
    """
    # dp_origin: 미사용 (하위 호환 서명 유지)
    def convert(qp: int) -> float:
        return (qp - qp_origin) / freq
    return convert


# ── PCM 버퍼 처리 ─────────────────────────────────────────────────────────────

def _process_buffer(data, num_frames, flg, qp, ch, sr, freq, to_stream_t):
    """버퍼에서 (t_stream, rms, vad) 튜플을 계산해 반환."""
    import numpy as np
    t_stream = to_stream_t(qp) if qp > 0 else to_stream_t(qpc_now())

    if flg & AUDCLNT_BUFFERFLAGS_SILENT or not data.value:
        return t_stream, 0.0, 0.0

    buf = (ctypes.c_float * (num_frames * ch)).from_address(data.value)
    arr = np.frombuffer(buf, dtype=np.float32).copy()
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)

    rms = float(np.sqrt(np.mean(arr ** 2)))
    vad = _compute_vad(arr, sr)
    del arr  # WASAPI 버퍼 참조 해제 — GC 대기 없이 즉시 반환
    return t_stream, rms, vad


# ── ProcessLoopback 캡처 세션 ─────────────────────────────────────────────────

def _run_capture_session(pid, audio_queue, stop_flag, send_log, stream_anchor):
    """MTA 스레드에서 ProcessLoopback 캡처를 실행하는 래퍼."""
    result_box = [None]

    def _session_mta():
        # ── [버그 수정] CoInitializeEx를 try 블록 안으로 이동 ─────────────────
        # 이전: try 블록 밖에 있어서 CoInitializeEx 자체가 예외를 던지면
        #   result_box[0]가 None인 채로 스레드가 종료되었음.
        co_ok = False
        try:
            hr_co = _ole32.CoInitializeEx(None, 0x0)
            # S_OK(0) 또는 S_FALSE(1, 이미 초기화됨)일 때만 CoUninitialize 호출
            co_ok = hr_co in (0, 1)
            result_box[0] = _run_capture_impl(pid, audio_queue, stop_flag, send_log, stream_anchor)
        except BaseException as e:
            # ── [버그 수정] Exception → BaseException ─────────────────────────
            # MemoryError, SystemExit, KeyboardInterrupt 등 Exception에 포함되지
            # 않는 예외까지 포획하여 result_box[0]가 항상 설정되도록 보장.
            result_box[0] = (False, f"예외(MTA): {type(e).__name__}: {e}")
        finally:
            if co_ok:
                _ole32.CoUninitialize()

    t = threading.Thread(target=_session_mta, daemon=True)
    t.start()
    # ── [버그 수정] t.join(timeout=30.0) → t.join() ──────────────────────────
    # 이전(버그): 30초 타임아웃 → 정상 캡처가 30초를 초과하는 순간 result_box[0]가
    #   None 상태로 반환되어 "MTA 스레드 비정상 종료" 로그가 반복 출력됐고,
    #   동시에 이전 MTA 스레드가 살아있는 상태에서 새 스레드가 생성되어 장치 충돌.
    # 수정: 타임아웃 제거. stop_flag가 세워지면 _run_capture_impl의 루프는
    #   WaitForSingleObject 최대 대기(WAIT_MS=10ms) 이내에 정상 종료되므로
    #   블로킹 우려가 없다. 진짜 비정상 블로킹 시에는 daemon 스레드로 자연 소멸.
    t.join()
    return result_box[0] if result_box[0] is not None else (False, "MTA 스레드 비정상 종료")


def _run_capture_impl(pid, audio_queue, stop_flag, send_log, stream_anchor):
    client  = None
    cap     = None
    h_event = None
    freq    = qpc_freq()

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
        to_stream_t   = None   # 첫 패킷에서 확립

        while not stop_flag.is_set():
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

            while not stop_flag.is_set():
                try:
                    pkt = get_next_packet_size(cap)
                except OSError as e:
                    send_log(f"⚠ GetNextPacketSize: {e}")
                    break
                if pkt == 0:
                    break

                data, num_frames, flg, dp, qp = get_buffer(cap)
                total_packets += 1

                if first_packet:
                    first_packet = False
                    send_log(f"✅ 첫 패킷 수신! frames={num_frames}")

                if num_frames > 0:
                    # 첫 유효 패킷에서 스트림 기준점 확립
                    if to_stream_t is None and qp > 0:
                        to_stream_t = _make_stream_converter(dp, qp, sr, freq)
                        # [Bug C 수정] 쓰기 순서: [1],[2] 먼저, [0] 마지막.
                        # P1은 stream_anchor[0]>0 을 트리거로 앵커 사용 전환.
                        # 이전: [0]=qp 먼저 쓰면 P1이 [2]=1.0(기본값) 상태에서
                        # t_hw = raw_qpc_ticks/1.0 (수백만) 극단값 생성 가능.
                        stream_anchor[1] = sr    # sample rate  (먼저)
                        stream_anchor[2] = freq  # qpc_freq     (먼저)
                        stream_anchor[0] = qp    # qp_origin    (트리거 — 마지막)
                        send_log(f"⚓ 스트림 기준점 확립: qp_origin={qp} sr={sr}")
                    if to_stream_t is None:
                        to_stream_t = lambda q: qpc_now() / freq

                    t_stream, rms, vad = _process_buffer(
                        data, num_frames, flg, qp, ch, sr, freq, to_stream_t)
                    queue_put(audio_queue, (t_stream, rms, vad))

                release_buffer(cap, num_frames)

        return True, ""

    except Exception as e:
        # ── [버그 수정] OSError → Exception ──────────────────────────────────
        # ctypes.ArgumentError, ValueError, OSError 외 모든 예외를 포획하여
        # 스레드가 비정상 종료(Crash)되지 않도록 보장.
        return False, f"{type(e).__name__}: {e}"
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

def _run_global_loopback_session(audio_queue, stop_flag, send_log, stream_anchor):
    """MTA 스레드에서 전체 루프백 캡처를 실행하는 래퍼."""
    result_box = [None]

    def _mta():
        # ── [버그 수정] CoInitializeEx를 try 블록 안으로 이동 ─────────────────
        # _run_capture_session과 동일한 이유: CoInitializeEx 예외 시
        # result_box[0]가 None으로 남는 문제 방지.
        co_ok = False
        client  = None
        cap     = None
        h_event = None
        freq    = qpc_freq()
        try:
            hr_co   = _ole32.CoInitializeEx(None, 0x0)
            co_ok   = hr_co in (0, 1)
            client  = activate_global_loopback()
            sr, ch  = audio_client_initialize_loopback(client)
            h_event = _kernel32.CreateEventW(None, False, False, None)
            audio_client_set_event(client, h_event)
            cap = get_capture_client(client)
            audio_client_start(client)
            send_log(f"🎙 [GlobalLoopback] sr={sr} ch={ch}")

            first_packet = True
            to_stream_t  = None

            while not stop_flag.is_set():
                _kernel32.WaitForSingleObject(h_event, 10)
                while not stop_flag.is_set():
                    try:
                        pkt = get_next_packet_size(cap)
                    except OSError as e:
                        send_log(f"⚠ GetNextPacketSize: {e}")
                        result_box[0] = (False, str(e))
                        return
                    if pkt == 0:
                        break

                    data, num_frames, flg, dp, qp = get_buffer(cap)
                    if first_packet:
                        first_packet = False
                        send_log("✅ 첫 패킷 수신! (전체 루프백)")

                    if num_frames > 0:
                        if to_stream_t is None and qp > 0:
                            to_stream_t = _make_stream_converter(dp, qp, sr, freq)
                            # [Bug C 수정] GlobalLoopback도 동일: [1],[2] 먼저, [0] 마지막
                            stream_anchor[1] = sr
                            stream_anchor[2] = freq
                            stream_anchor[0] = qp    # 트리거 — 마지막에 쓰기
                            send_log(f"⚓ 스트림 기준점 확립 (GlobalLoopback): qp_origin={qp}")
                        if to_stream_t is None:
                            to_stream_t = lambda q: qpc_now() / freq

                        t_stream, rms, vad = _process_buffer(
                            data, num_frames, flg, qp, ch, sr, freq, to_stream_t)
                        queue_put(audio_queue, (t_stream, rms, vad))

                    release_buffer(cap, num_frames)

            result_box[0] = (True, "")
        except BaseException as e:
            # ── [버그 수정] Exception → BaseException ─────────────────────────
            result_box[0] = (False, f"{type(e).__name__}: {e}")
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

    t = threading.Thread(target=_mta, daemon=True)
    t.start()
    t.join()
    return result_box[0] if result_box[0] is not None else (False, "MTA 스레드 비정상 종료")


# ── 진입점 ────────────────────────────────────────────────────────────────────

def proc_audio_capture(audio_queue, stop_flag, cfg: dict,
                       log_queue=None, stream_anchor=None):
    """
    stream_anchor: [qp_origin, sr, freq] 공유 리스트 (multiprocessing.Manager().list())
                   첫 패킷에서 기준점을 기록, proc_lip_capture가 읽어서 동일 기준 사용.
                   None이면 기준점 공유 없이 동작 (하위 호환).
    """
    import sys, os
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    # ── [재연결 수정] stale PID 캐시 즉시 무효화 ─────────────────────────────
    # 새 캡처 세션 시작마다 캐시를 초기화해 종료된 팟플레이어의 PID가 재사용되는
    # 것을 방지한다. 모듈 레벨 _pid_cache는 스레드 간 공유되므로 반드시 Lock으로
    # 보호된 invalidate_pid_cache()를 통해 초기화한다.
    invalidate_pid_cache()

    if stream_anchor is None:
        stream_anchor = [0, 48000, qpc_freq()]  # 더미 — 공유 안 됨

    send_log = make_send_log(audio_queue, log_queue, queue_put)

    send_log(f"ℹ Win build={_WIN_BUILD} | ProcessLoopback={_SUPPORT_PROCESS_LOOPBACK}")

    if not _SUPPORT_PROCESS_LOOPBACK:
        send_log(f"⚠ ProcessLoopback 미지원 (빌드 {_WIN_BUILD} < 19041) — 전체 루프백으로 전환")
        _retry = 0
        while not stop_flag.is_set():
            try:
                ok, reason = _run_global_loopback_session(
                    audio_queue, stop_flag, send_log, stream_anchor)
            except Exception as e:
                ok, reason = False, f"예외: {e}"
            if ok:
                _retry = 0
                continue
            send_log(f"⚠ 전체 루프백 실패: {reason}")
            _retry += 1
            send_log(f"🔄 {_retry}회 재시도 대기 중 (5초)...")
            for _ in range(50):
                if stop_flag.is_set():
                    break
                time.sleep(0.1)
        return

    _retry = 0
    while not stop_flag.is_set():
        pid = _find_potplayer_pid()
        if pid is None:
            send_log("⏳ 팟플레이어 실행 대기 중...")
            for _ in range(50):
                if stop_flag.is_set():
                    return
                time.sleep(0.1)
            continue

        try:
            ok, reason = _run_capture_session(
                pid, audio_queue, stop_flag, send_log, stream_anchor)
        except Exception as e:
            ok, reason = False, f"예외: {e}"

        if ok:
            _retry = 0
            continue

        send_log(f"⚠ ProcessLoopback 실패: {reason}")
        _retry += 1
        send_log(f"🔄 {_retry}회 재시도 대기 중 (5초)...")
        for _ in range(50):
            if stop_flag.is_set():
                break
            time.sleep(0.1)
