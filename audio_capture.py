import time
import ctypes
import ctypes.wintypes
import platform
import numpy as np
import psutil
from multiprocessing import Queue, Value
from win32_utils import CFG, queue_put

# ──────────────────────────────────────────────────────────────────────────────
# WASAPI COM GUID 상수
# ──────────────────────────────────────────────────────────────────────────────
CLSID_MMDeviceEnumerator  = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator   = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAudioSessionManager2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
IID_IAudioSessionControl2 = "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"
IID_IAudioMeterInformation = "{C02216F6-8C67-4B5B-9D00-D008E73E0064}"
IID_IAudioClient          = "{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"
IID_IAudioCaptureClient   = "{C8ADBD64-E71E-48A0-A4DE-185C395CD317}"

AUDCLNT_SHAREMODE_SHARED    = 0
AUDCLNT_SHAREMODE_EXCLUSIVE = 1
AUDCLNT_STREAMFLAGS_LOOPBACK           = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK      = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM     = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000

# ProcessLoopback (Win 10 20H1+ build 19041)
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0

CLSCTX_ALL = 23
eRender     = 0
eConsole    = 1

# ──────────────────────────────────────────────────────────────────────────────
# Windows 빌드 번호 확인
# ──────────────────────────────────────────────────────────────────────────────
def _windows_build() -> int:
    try:
        ver = platform.version()          # e.g. "10.0.19041"
        return int(ver.split(".")[-1])
    except Exception:
        return 0

_WIN_BUILD = _windows_build()
_SUPPORT_PROCESS_LOOPBACK = (_WIN_BUILD >= 19041)  # 20H1+


# ══════════════════════════════════════════════════════════════════════════════
def proc_audio_capture(audio_queue: Queue, stop_flag: Value, cfg: dict, log_queue=None):
    SR       = cfg["AUDIO_SR"]
    chunk_ms = 50

    # ── LOG 헬퍼 ──────────────────────────────────────────────────────────────
    def send_log(msg: str):
        if log_queue is not None:
            try:
                log_queue.put_nowait(msg)
            except Exception:
                pass
        else:
            queue_put(audio_queue, ("LOG", msg))

    # ── PID 캐시 ──────────────────────────────────────────────────────────────
    _pc = [None, 0.0]

    def find_potplayer_pid() -> int | None:
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

    # ── scipy 밴드패스 필터 ───────────────────────────────────────────────────
    def make_bandpass(native_sr: int):
        try:
            from scipy.signal import butter, sosfilt as _sf
            sos = butter(4, [300, 3400], btype="bandpass", fs=native_sr, output="sos")
            return sos, _sf
        except Exception:
            return None, None

    def apply_filter(arr: np.ndarray, sos, sosfilt) -> np.ndarray:
        if sos is not None and sosfilt is not None:
            try:
                return sosfilt(sos, arr)
            except Exception:
                pass
        return arr

    # ── COM 초기화 헬퍼 ───────────────────────────────────────────────────────
    def _coinit():
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass

    # ── WASAPI 공통: IMMDevice 획득 ───────────────────────────────────────────
    def _get_default_device():
        """
        IMMDeviceEnumerator → IMMDevice (default render endpoint) 반환.
        반환: (enumerator, device) comtypes IUnknown 포인터 쌍, 실패 시 (None, None)
        """
        import comtypes
        try:
            en = comtypes.CoCreateInstance(
                comtypes.GUID(CLSID_MMDeviceEnumerator),
                interface=comtypes.IUnknown,
                clsctx=CLSCTX_ALL,
            )
            pd = comtypes.POINTER(comtypes.IUnknown)()
            hr = en._comobj.GetDefaultAudioEndpoint(eRender, eConsole, ctypes.byref(pd))
            if hr != 0:
                return None, None
            return en, pd
        except Exception:
            return None, None

    # ── WASAPI 공통: 세션 PID 목록 순회 헬퍼 ─────────────────────────────────
    def _iter_sessions(pd):
        """
        IMMDevice → 각 세션의 (IAudioSessionControl IUnknown, pid) 를 yield.
        """
        import comtypes
        g_asm2 = comtypes.GUID(IID_IAudioSessionManager2)
        g_ctl2 = comtypes.GUID(IID_IAudioSessionControl2)

        pm = comtypes.POINTER(comtypes.IUnknown)()
        if pd._comobj.Activate(ctypes.byref(g_asm2), CLSCTX_ALL, None, ctypes.byref(pm)) != 0:
            return
        pe = comtypes.POINTER(comtypes.IUnknown)()
        if pm._comobj.GetSessionEnumerator(ctypes.byref(pe)) != 0:
            return
        cnt = ctypes.c_int(0)
        pe._comobj.GetCount(ctypes.byref(cnt))

        for i in range(cnt.value):
            ps = comtypes.POINTER(comtypes.IUnknown)()
            if pe._comobj.GetSession(i, ctypes.byref(ps)) != 0:
                continue
            pc = comtypes.POINTER(comtypes.IUnknown)()
            if ps._comobj.QueryInterface(ctypes.byref(g_ctl2), ctypes.byref(pc)) != 0:
                continue
            pid = ctypes.c_uint(0)
            if pc._comobj.GetProcessId(ctypes.byref(pid)) != 0:
                continue
            yield ps, pid.value

    # ══════════════════════════════════════════════════════════════════════════
    # 방법 1: ProcessLoopback (Win 10 20H1+ / Win 11)
    #   AUDIOCLIENT_ACTIVATION_PARAMS 에 PID 지정 → 해당 프로세스 오디오만 캡처
    # ══════════════════════════════════════════════════════════════════════════

    # ctypes 구조체 정의 (한 번만)
    class _AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
        _fields_ = [
            ("TargetProcessId",    ctypes.c_uint32),
            ("ProcessLoopbackMode", ctypes.c_uint32),
        ]

    class _AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
        _fields_ = [
            ("ActivationType",        ctypes.c_uint32),
            ("ProcessLoopbackParams", _AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
        ]

    class _PROPVARIANT(ctypes.Structure):
        """PROPVARIANT 최소 구현 (blob 용)"""
        _fields_ = [
            ("vt",       ctypes.c_ushort),
            ("wReserved1", ctypes.c_ushort),
            ("wReserved2", ctypes.c_ushort),
            ("wReserved3", ctypes.c_ushort),
            ("blob_cbSize",  ctypes.c_ulong),
            ("blob_pBlobData", ctypes.c_void_p),
        ]

    class _WAVEFORMATEX(ctypes.Structure):
        _fields_ = [
            ("wFormatTag",      ctypes.c_ushort),
            ("nChannels",       ctypes.c_ushort),
            ("nSamplesPerSec",  ctypes.c_uint32),
            ("nAvgBytesPerSec", ctypes.c_uint32),
            ("nBlockAlign",     ctypes.c_ushort),
            ("wBitsPerSample",  ctypes.c_ushort),
            ("cbSize",          ctypes.c_ushort),
        ]

    WAVE_FORMAT_IEEE_FLOAT = 3

    def _make_wfx(sr: int, ch: int = 2) -> _WAVEFORMATEX:
        wfx = _WAVEFORMATEX()
        wfx.wFormatTag     = WAVE_FORMAT_IEEE_FLOAT
        wfx.nChannels      = ch
        wfx.nSamplesPerSec = sr
        wfx.wBitsPerSample = 32
        wfx.nBlockAlign    = ch * 4
        wfx.nAvgBytesPerSec = sr * ch * 4
        wfx.cbSize         = 0
        return wfx

    def capture_via_process_loopback() -> tuple[bool, str]:
        """
        Win 10 20H1+ 전용: ActivateAudioInterfaceAsync 없이
        IMMDevice::Activate + PROPVARIANT(blob) 방식으로 ProcessLoopback 스트림 오픈.

        [동작]
        - PID 지정 → 팟플레이어 오디오만 캡처
        - 3초마다 PID 변경 체크 → 재연결
        - stop_flag 시 정상 종료 → True 반환
        """
        if not _SUPPORT_PROCESS_LOOPBACK:
            return False, f"ProcessLoopback 미지원 빌드 ({_WIN_BUILD} < 19041)"

        import comtypes

        _coinit()

        def _open_process_stream(pid: int):
            """
            특정 PID 에 대한 IAudioCaptureClient 스트림 오픈.
            반환: (audio_client_ptr, capture_client_ptr, wfx, hr_msg)
            """
            _, pd = _get_default_device()
            if pd is None:
                return None, None, None, "IMMDevice 획득 실패"

            # AUDIOCLIENT_ACTIVATION_PARAMS 준비
            params = _AUDIOCLIENT_ACTIVATION_PARAMS()
            params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
            params.ProcessLoopbackParams.TargetProcessId    = pid
            params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE

            # PROPVARIANT(VT_BLOB = 0x41)
            pv = _PROPVARIANT()
            pv.vt           = 0x41   # VT_BLOB
            pv.blob_cbSize  = ctypes.sizeof(params)
            pv.blob_pBlobData = ctypes.cast(ctypes.addressof(params), ctypes.c_void_p)

            g_ac = comtypes.GUID(IID_IAudioClient)
            pac  = comtypes.POINTER(comtypes.IUnknown)()

            # IMMDevice::Activate with PROPVARIANT
            hr = pd._comobj.Activate(
                ctypes.byref(g_ac),
                CLSCTX_ALL,
                ctypes.byref(pv),
                ctypes.byref(pac),
            )
            if hr != 0:
                return None, None, None, f"IAudioClient Activate 실패 hr=0x{hr & 0xFFFFFFFF:08X}"

            # 포맷 협상: 먼저 MixFormat 쿼리
            pwfx = ctypes.POINTER(_WAVEFORMATEX)()
            hr = pac._comobj.GetMixFormat(ctypes.byref(pwfx))
            if hr != 0 or not pwfx:
                wfx = _make_wfx(SR)
            else:
                wfx = pwfx.contents

            native_sr = int(wfx.nSamplesPerSec)
            ch        = int(wfx.nChannels)

            # Initialize: Loopback 모드
            flags = AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
            hr = pac._comobj.Initialize(
                AUDCLNT_SHAREMODE_SHARED,
                flags,
                10_000_000,  # 1초 버퍼 (100ns 단위)
                0,
                ctypes.byref(wfx),
                None,
            )
            if hr != 0:
                return None, None, None, f"IAudioClient::Initialize 실패 hr=0x{hr & 0xFFFFFFFF:08X}"

            # IAudioCaptureClient 획득
            g_cc = comtypes.GUID(IID_IAudioCaptureClient)
            pcc  = comtypes.POINTER(comtypes.IUnknown)()
            hr = pac._comobj.GetService(ctypes.byref(g_cc), ctypes.byref(pcc))
            if hr != 0:
                return None, None, None, f"GetService(CaptureClient) 실패 hr=0x{hr & 0xFFFFFFFF:08X}"

            hr = pac._comobj.Start()
            if hr != 0:
                return None, None, None, f"IAudioClient::Start 실패 hr=0x{hr & 0xFFFFFFFF:08X}"

            return pac, pcc, (native_sr, ch), ""

        # ── 최초 PID 확인 ─────────────────────────────────────────────────
        pot_pid = find_potplayer_pid()
        if pot_pid is None:
            return False, "팟플레이어 PID 없음 (ProcessLoopback)"

        pac, pcc, sr_ch, err = _open_process_stream(pot_pid)
        if pcc is None:
            return False, f"ProcessLoopback 스트림 오픈 실패: {err}"

        native_sr, ch = sr_ch
        sos, sosfilt  = make_bandpass(native_sr)
        send_log(f"🎙 [ProcessLoopback] PID={pot_pid} sr={native_sr} ch={ch}")

        RECHECK    = 3.0
        last_check = time.time()
        cur_pid    = pot_pid
        chunk_frames = int(native_sr * chunk_ms / 1000)

        def _stop_stream(pac):
            try:
                pac._comobj.Stop()
            except Exception:
                pass

        while not stop_flag.value:
            now = time.time()

            # ── 3초마다 PID 재확인 → PID 변경 시 재연결 ──────────────────
            if now - last_check >= RECHECK:
                last_check = now
                new_pid = find_potplayer_pid()
                if new_pid is None:
                    # 팟플레이어 종료 → 상위 폴백으로
                    _stop_stream(pac)
                    return False, "팟플레이어 종료 감지 (ProcessLoopback)"
                if new_pid != cur_pid:
                    _stop_stream(pac)
                    pac2, pcc2, sr_ch2, err2 = _open_process_stream(new_pid)
                    if pcc2 is not None:
                        pac, pcc = pac2, pcc2
                        native_sr, ch = sr_ch2
                        sos, sosfilt  = make_bandpass(native_sr)
                        cur_pid = new_pid
                        chunk_frames = int(native_sr * chunk_ms / 1000)
                        send_log(f"🎙 [ProcessLoopback] PID 변경 재연결: {new_pid} sr={native_sr}")
                    else:
                        send_log(f"⚠ [ProcessLoopback] PID 변경 재연결 실패: {err2}")

            # ── 버퍼 읽기 ─────────────────────────────────────────────────
            try:
                num_frames = ctypes.c_uint32(0)
                hr = pcc._comobj.GetNextPacketSize(ctypes.byref(num_frames))
                if hr != 0 or num_frames.value == 0:
                    time.sleep(chunk_ms / 1000)
                    continue

                buf_ptr   = ctypes.c_void_p()
                frames_rd = ctypes.c_uint32(0)
                flags     = ctypes.c_uint32(0)
                hr = pcc._comobj.GetBuffer(
                    ctypes.byref(buf_ptr),
                    ctypes.byref(frames_rd),
                    ctypes.byref(flags),
                    None, None,
                )
                if hr != 0 or frames_rd.value == 0:
                    time.sleep(chunk_ms / 1000)
                    continue

                byte_size = frames_rd.value * ch * 4  # float32
                raw = (ctypes.c_byte * byte_size).from_address(buf_ptr.value)
                arr = np.frombuffer(raw, dtype=np.float32).copy()
                pcc._comobj.ReleaseBuffer(frames_rd.value)

                if ch > 1:
                    arr = arr.reshape(-1, ch).mean(axis=1)
                arr = apply_filter(arr, sos, sosfilt)
                queue_put(audio_queue, (time.time(), float(np.sqrt(np.mean(arr ** 2)))))

            except Exception as e:
                send_log(f"⚠ [ProcessLoopback] 읽기 오류: {e}")
                time.sleep(0.1)

        _stop_stream(pac)
        return True, ""

    # ══════════════════════════════════════════════════════════════════════════
    # 방법 2: WASAPI Session Loopback (Win 10 이하 / ProcessLoopback 불가 시)
    #   전체 루프백 캡처 + IAudioSessionControl2::GetProcessId() 로 PID 검증
    #   → pyaudiowpatch 의 loopbackProcessId 대신 세션 API로 PID 매칭
    # ══════════════════════════════════════════════════════════════════════════

    def capture_via_session_loopback() -> tuple[bool, str]:
        """
        WASAPI 전체 루프백 캡처 + IAudioSessionControl2 로 팟플레이어 세션 존재 확인.

        [동작]
        - 전체 루프백 스트림으로 PCM 수신
        - 3초마다 세션 열거로 팟플레이어 PID 세션이 살아있는지 검증
        - 세션 없으면 사일런트 구간으로 처리 (RMS=0) 하고 계속 대기
        - stop_flag 시 정상 종료 → True 반환
        """
        import comtypes

        _coinit()

        _, pd = _get_default_device()
        if pd is None:
            return False, "IMMDevice 획득 실패 (SessionLoopback)"

        # IAudioClient 활성화 (일반 루프백)
        g_ac = comtypes.GUID(IID_IAudioClient)
        pac  = comtypes.POINTER(comtypes.IUnknown)()
        hr   = pd._comobj.Activate(ctypes.byref(g_ac), CLSCTX_ALL, None, ctypes.byref(pac))
        if hr != 0:
            return False, f"IAudioClient Activate 실패 hr=0x{hr & 0xFFFFFFFF:08X}"

        # MixFormat 쿼리
        pwfx = ctypes.POINTER(_WAVEFORMATEX)()
        pac._comobj.GetMixFormat(ctypes.byref(pwfx))
        wfx       = pwfx.contents if pwfx else _make_wfx(SR)
        native_sr = int(wfx.nSamplesPerSec)
        ch        = int(wfx.nChannels)

        flags = AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
        hr = pac._comobj.Initialize(
            AUDCLNT_SHAREMODE_SHARED, flags,
            10_000_000, 0,
            ctypes.byref(wfx), None,
        )
        if hr != 0:
            return False, f"IAudioClient::Initialize 실패 hr=0x{hr & 0xFFFFFFFF:08X}"

        g_cc = comtypes.GUID(IID_IAudioCaptureClient)
        pcc  = comtypes.POINTER(comtypes.IUnknown)()
        if pac._comobj.GetService(ctypes.byref(g_cc), ctypes.byref(pcc)) != 0:
            return False, "GetService(CaptureClient) 실패"

        pac._comobj.Start()
        sos, sosfilt = make_bandpass(native_sr)
        send_log(f"🎙 [SessionLoopback] 전체 루프백 sr={native_sr} ch={ch}")

        RECHECK    = 3.0
        last_check = time.time()
        pot_active = False   # 세션에서 팟플레이어가 확인됐는지

        def _check_pot_session() -> bool:
            """IAudioSessionControl2 기반 세션 열거로 팟플레이어 PID 확인"""
            pid = find_potplayer_pid()
            if pid is None:
                return False
            try:
                _, pd2 = _get_default_device()
                if pd2 is None:
                    return False
                for _, sess_pid in _iter_sessions(pd2):
                    if sess_pid == pid:
                        return True
            except Exception:
                pass
            return False

        while not stop_flag.value:
            now = time.time()

            # ── 3초마다 세션 PID 검증 ────────────────────────────────────
            if now - last_check >= RECHECK:
                last_check = now
                was_active = pot_active
                pot_active = _check_pot_session()
                if pot_active and not was_active:
                    send_log("🎙 [SessionLoopback] 팟플레이어 세션 감지 → 캡처 활성")
                elif not pot_active and was_active:
                    send_log("⚠ [SessionLoopback] 팟플레이어 세션 소멸 → RMS=0 대기")

            # ── 버퍼 읽기 ─────────────────────────────────────────────────
            try:
                num_frames = ctypes.c_uint32(0)
                hr = pcc._comobj.GetNextPacketSize(ctypes.byref(num_frames))
                if hr != 0 or num_frames.value == 0:
                    time.sleep(chunk_ms / 1000)
                    continue

                buf_ptr   = ctypes.c_void_p()
                frames_rd = ctypes.c_uint32(0)
                flags_rd  = ctypes.c_uint32(0)
                hr = pcc._comobj.GetBuffer(
                    ctypes.byref(buf_ptr),
                    ctypes.byref(frames_rd),
                    ctypes.byref(flags_rd),
                    None, None,
                )
                if hr != 0 or frames_rd.value == 0:
                    time.sleep(chunk_ms / 1000)
                    continue

                byte_size = frames_rd.value * ch * 4
                raw = (ctypes.c_byte * byte_size).from_address(buf_ptr.value)
                arr = np.frombuffer(raw, dtype=np.float32).copy()
                pcc._comobj.ReleaseBuffer(frames_rd.value)

                # 팟플레이어 세션 없으면 무음 처리
                if not pot_active:
                    queue_put(audio_queue, (time.time(), 0.0))
                    continue

                if ch > 1:
                    arr = arr.reshape(-1, ch).mean(axis=1)
                arr = apply_filter(arr, sos, sosfilt)
                queue_put(audio_queue, (time.time(), float(np.sqrt(np.mean(arr ** 2)))))

            except Exception as e:
                send_log(f"⚠ [SessionLoopback] 읽기 오류: {e}")
                time.sleep(0.1)

        try:
            pac._comobj.Stop()
        except Exception:
            pass
        return True, ""

    # ══════════════════════════════════════════════════════════════════════════
    # 방법 3: IAudioMeterInformation 폴백 (WASAPI 스트림 불가 시)
    #   IAudioSessionControl2::GetProcessId() 로 PID 매칭 후 Peak 값 반환
    # ══════════════════════════════════════════════════════════════════════════

    _meter_cache = [None, None]   # [comobj, pid]

    def get_potplayer_rms() -> tuple[float | None, str]:
        """
        IAudioMeterInformation::GetPeakValue() 로 팟플레이어 세션 피크 읽기.
        세션 PID 매칭은 IAudioSessionControl2::GetProcessId() 사용.
        """
        import comtypes
        _coinit()

        pp = find_potplayer_pid()
        if pp is None:
            _meter_cache[0] = None
            return None, "PID없음"

        # 캐시 히트
        if _meter_cache[0] is not None and _meter_cache[1] == pp:
            pk = ctypes.c_float(0)
            try:
                _meter_cache[0]._comobj.GetPeakValue(ctypes.byref(pk))
                return float(pk.value), ""
            except Exception:
                _meter_cache[0] = None

        # 세션 재탐색
        _, pd = _get_default_device()
        if pd is None:
            return None, "IMMDevice 획득 실패"

        g_meter = comtypes.GUID(IID_IAudioMeterInformation)
        for ps, sess_pid in _iter_sessions(pd):
            if sess_pid != pp:
                continue
            mt = comtypes.POINTER(comtypes.IUnknown)()
            if ps._comobj.QueryInterface(ctypes.byref(g_meter), ctypes.byref(mt)) != 0:
                return None, "IAudioMeter QI 실패"
            _meter_cache[0] = mt
            _meter_cache[1] = pp
            pk = ctypes.c_float(0)
            mt._comobj.GetPeakValue(ctypes.byref(pk))
            return float(pk.value), ""

        return None, f"팟플레이어 세션 없음 (PID={pp})"

    # ══════════════════════════════════════════════════════════════════════════
    # 메인 루프: ProcessLoopback → SessionLoopback → IAudioMeter
    # ══════════════════════════════════════════════════════════════════════════

    send_log(f"ℹ Windows 빌드 {_WIN_BUILD} / ProcessLoopback 지원: {_SUPPORT_PROCESS_LOOPBACK}")

    while not stop_flag.value:

        # ── 1순위: ProcessLoopback (Win 10 20H1+) ────────────────────────────
        if _SUPPORT_PROCESS_LOOPBACK:
            try:
                ok, reason = capture_via_process_loopback()
            except Exception as e:
                ok, reason = False, f"예외: {e}"

            if ok:
                continue   # stop_flag 정상 종료 → 루프 탈출 대기

            send_log(f"⚠ ProcessLoopback 종료: {reason}")

        # ── 2순위: WASAPI Session Loopback (Win 10 이하 or 위 실패 시) ────────
        try:
            ok, reason = capture_via_session_loopback()
        except Exception as e:
            ok, reason = False, f"예외: {e}"

        if ok:
            continue

        send_log(f"⚠ SessionLoopback 실패: {reason}")
        send_log("IAudioMeter 폴백 시작")

        # ── 3순위: IAudioMeter 폴백 ──────────────────────────────────────────
        fallback_logged = False
        while not stop_flag.value:
            rms, err = get_potplayer_rms()
            if rms is not None:
                if not fallback_logged:
                    send_log("🎙 IAudioMeter 세션 캡처 시작")
                    fallback_logged = True
                queue_put(audio_queue, (time.time(), rms))
            else:
                if not fallback_logged:
                    send_log(f"IAudioMeter 실패: {err}")
                    fallback_logged = True
                if "PID없음" in err:
                    break   # 팟플레이어 없음 → 상위 재시도
            time.sleep(chunk_ms / 1000)

        time.sleep(1.0)
