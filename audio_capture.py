import time
import ctypes
import ctypes.wintypes
import platform
import numpy as np
import psutil
from multiprocessing import Queue, Value
from win32_utils import CFG, queue_put

# ──────────────────────────────────────────────────────────────────────────────
# WASAPI COM GUID / 상수
# ──────────────────────────────────────────────────────────────────────────────
CLSID_MMDeviceEnumerator   = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator    = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAudioSessionManager2  = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
IID_IAudioSessionControl2  = "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"
IID_IAudioMeterInformation = "{C02216F6-8C67-4B5B-9D00-D008E73E0064}"
IID_IAudioClient           = "{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"
IID_IAudioCaptureClient    = "{C8ADBD64-E71E-48A0-A4DE-185C395CD317}"

AUDCLNT_SHAREMODE_SHARED              = 0
AUDCLNT_STREAMFLAGS_LOOPBACK          = 0x00020000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM    = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000

AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK         = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE    = 0

CLSCTX_ALL = 23
eRender    = 0
eConsole   = 1
WAVE_FORMAT_IEEE_FLOAT = 3

# ──────────────────────────────────────────────────────────────────────────────
# Windows 빌드 번호
# ──────────────────────────────────────────────────────────────────────────────
def _windows_build() -> int:
    try:
        return int(platform.version().split(".")[-1])
    except Exception:
        return 0

_WIN_BUILD                = _windows_build()
_SUPPORT_PROCESS_LOOPBACK = (_WIN_BUILD >= 19041)

# ──────────────────────────────────────────────────────────────────────────────
# 순수 ctypes COM 헬퍼
#   comtypes 를 쓰지 않으므로 multiprocessing 자식 프로세스에서도 안전.
# ──────────────────────────────────────────────────────────────────────────────
_ole32 = ctypes.windll.ole32
_GUID  = ctypes.c_byte * 16

def _make_guid(guid_str: str) -> _GUID:
    import uuid
    return (_GUID)(*uuid.UUID(guid_str).bytes_le)

def _coinit() -> int:
    """
    COM 초기화. 우선순위:
      1) COINIT_APARTMENTTHREADED(0x2) → S_OK(0) 이면 완료
      2) S_FALSE(1): 이미 같은 타입으로 초기화됨 → 그대로 사용
      3) RPC_E_CHANGED_MODE(0x80010106): 다른 타입으로 이미 초기화됨
         → CoUninitialize 후 COINIT_MULTITHREADED(0x0) 재시도
    WASAPI IMMDeviceEnumerator 는 MTA 에서도 동작하므로 MTA 폴백이 안전.
    """
    RPC_E_CHANGED_MODE = 0x80010106
    hr = _ole32.CoInitializeEx(None, 2)  # STA 시도
    if hr == 0:
        return hr  # S_OK: 정상 초기화
    if hr == 1:
        return hr  # S_FALSE: 이미 STA 로 초기화됨, 그대로 사용
    if (hr & 0xFFFFFFFF) == RPC_E_CHANGED_MODE:
        # PyInstaller/multiprocessing 이 MTA 로 먼저 초기화한 경우
        try:
            _ole32.CoUninitialize()
        except Exception:
            pass
        hr2 = _ole32.CoInitializeEx(None, 0)  # MTA 재시도
        return hr2
    return hr

def _couninit():
    try:
        _ole32.CoUninitialize()
    except Exception:
        pass

def _co_create(clsid_str: str, iid_str: str) -> int:
    """CoCreateInstance → 인터페이스 포인터 value, 실패 시 0"""
    clsid = _make_guid(clsid_str)
    iid   = _make_guid(iid_str)
    out   = ctypes.c_void_p(0)
    hr    = _ole32.CoCreateInstance(
        ctypes.byref(clsid), None, CLSCTX_ALL,
        ctypes.byref(iid), ctypes.byref(out),
    )
    return out.value if hr == 0 and out.value else 0

def _vtbl_fn(ptr_val: int, idx: int, restype, *argtypes):
    """vtable[idx] 주소로부터 WINFUNCTYPE 함수 반환"""
    if not ptr_val:
        raise ValueError(f"_vtbl_fn: null ptr_val (idx={idx})")
    vtbl_ptr = ctypes.cast(ptr_val, ctypes.POINTER(ctypes.c_void_p)).contents.value
    if not vtbl_ptr:
        raise ValueError(f"_vtbl_fn: null vtbl_ptr (ptr_val={ptr_val:#x}, idx={idx})")
    fn_addr = ctypes.cast(vtbl_ptr, ctypes.POINTER(ctypes.c_void_p))[idx].value
    if not fn_addr:
        raise ValueError(f"_vtbl_fn: null fn_addr (vtbl_ptr={vtbl_ptr:#x}, idx={idx})")
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(fn_addr)

def _release(ptr_val: int):
    try:
        _vtbl_fn(ptr_val, 2, ctypes.c_ulong)(ptr_val)
    except Exception:
        pass

def _qi(ptr_val: int, iid_str: str) -> int:
    """QueryInterface → 새 인터페이스 포인터 value, 실패 시 0"""
    iid = _make_guid(iid_str)
    out = ctypes.c_void_p(0)
    fn  = _vtbl_fn(ptr_val, 0,
                   ctypes.c_long,
                   ctypes.POINTER(_GUID),
                   ctypes.POINTER(ctypes.c_void_p))
    hr  = fn(ptr_val, ctypes.byref(iid), ctypes.byref(out))
    return out.value if hr == 0 and out.value else 0

# ──────────────────────────────────────────────────────────────────────────────
# ctypes 구조체
# ──────────────────────────────────────────────────────────────────────────────
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

class _AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId",     ctypes.c_uint32),
        ("ProcessLoopbackMode", ctypes.c_uint32),
    ]

class _AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType",        ctypes.c_uint32),
        ("ProcessLoopbackParams", _AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]

class _PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt",            ctypes.c_ushort),
        ("wReserved1",    ctypes.c_ushort),
        ("wReserved2",    ctypes.c_ushort),
        ("wReserved3",    ctypes.c_ushort),
        ("blob_cbSize",   ctypes.c_ulong),
        ("blob_pBlobData", ctypes.c_void_p),
    ]

def _make_wfx(sr: int, ch: int = 2) -> _WAVEFORMATEX:
    wfx = _WAVEFORMATEX()
    wfx.wFormatTag      = WAVE_FORMAT_IEEE_FLOAT
    wfx.nChannels       = ch
    wfx.nSamplesPerSec  = sr
    wfx.wBitsPerSample  = 32
    wfx.nBlockAlign     = ch * 4
    wfx.nAvgBytesPerSec = sr * ch * 4
    wfx.cbSize          = 0
    return wfx

# ──────────────────────────────────────────────────────────────────────────────
# WASAPI 공통 래퍼
# ──────────────────────────────────────────────────────────────────────────────

# vtable 인덱스 상수
_IMDE_GetDefaultAudioEndpoint = 4
_IMMD_Activate                = 3
_IASM2_GetSessionEnumerator   = 5
_IASE_GetCount                = 3
_IASE_GetSession              = 4
_IASC2_GetProcessId           = 14
_IAMI_GetPeakValue            = 3
_IAC_Initialize               = 3
_IAC_GetMixFormat             = 8
_IAC_Start                    = 10   # IAudioClient vtable: 10=Start, 11=Stop
_IAC_Stop                     = 11
_IAC_GetService               = 14
_IACC_GetBuffer               = 3
_IACC_ReleaseBuffer           = 4
_IACC_GetNextPacketSize       = 5


def _get_default_device() -> int:
    """IMMDeviceEnumerator → IMMDevice ptr value, 실패 시 0"""
    en = _co_create(CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator)
    if not en:
        return 0
    dev = ctypes.c_void_p(0)
    fn  = _vtbl_fn(en, _IMDE_GetDefaultAudioEndpoint,
                   ctypes.c_long,
                   ctypes.c_uint, ctypes.c_uint,
                   ctypes.POINTER(ctypes.c_void_p))
    hr  = fn(en, eRender, eConsole, ctypes.byref(dev))
    _release(en)
    return dev.value if hr == 0 and dev.value else 0


def _iter_session_pids(dev: int):
    """
    IMMDevice → 세션 열거 → (session_ctl_ptr_value, pid) yield.
    IAudioSessionControl2::GetProcessId() 기반 → 드라이버 독립적.
    """
    iid_asm2 = _make_guid(IID_IAudioSessionManager2)
    asm2     = ctypes.c_void_p(0)
    fn_act   = _vtbl_fn(dev, _IMMD_Activate,
                        ctypes.c_long,
                        ctypes.POINTER(_GUID),
                        ctypes.c_uint,
                        ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_void_p))
    if fn_act(dev, ctypes.byref(iid_asm2), CLSCTX_ALL, None, ctypes.byref(asm2)) != 0:
        return
    if not asm2.value:
        return

    enum = ctypes.c_void_p(0)
    fn_ge = _vtbl_fn(asm2.value, _IASM2_GetSessionEnumerator,
                     ctypes.c_long,
                     ctypes.POINTER(ctypes.c_void_p))
    hr = fn_ge(asm2.value, ctypes.byref(enum))
    _release(asm2.value)
    if hr != 0 or not enum.value:
        return

    cnt = ctypes.c_int(0)
    _vtbl_fn(enum.value, _IASE_GetCount,
             ctypes.c_long,
             ctypes.POINTER(ctypes.c_int))(enum.value, ctypes.byref(cnt))

    iid_ctl2 = _make_guid(IID_IAudioSessionControl2)

    for i in range(cnt.value):
        sess = ctypes.c_void_p(0)
        fn_gs = _vtbl_fn(enum.value, _IASE_GetSession,
                         ctypes.c_long,
                         ctypes.c_int,
                         ctypes.POINTER(ctypes.c_void_p))
        if fn_gs(enum.value, i, ctypes.byref(sess)) != 0 or not sess.value:
            continue

        ctl2 = ctypes.c_void_p(0)
        fn_qi = _vtbl_fn(sess.value, 0,
                         ctypes.c_long,
                         ctypes.POINTER(_GUID),
                         ctypes.POINTER(ctypes.c_void_p))
        if fn_qi(sess.value, ctypes.byref(iid_ctl2), ctypes.byref(ctl2)) != 0:
            continue
        if not ctl2.value:
            continue

        pid = ctypes.c_uint(0)
        fn_pid = _vtbl_fn(ctl2.value, _IASC2_GetProcessId,
                          ctypes.c_long,
                          ctypes.POINTER(ctypes.c_uint))
        if fn_pid(ctl2.value, ctypes.byref(pid)) != 0:
            continue

        yield sess.value, pid.value
        _release(ctl2.value)

    _release(enum.value)


def _activate_audio_client(dev: int, pv_ptr=None) -> int:
    """IMMDevice::Activate → IAudioClient ptr value"""
    iid_ac = _make_guid(IID_IAudioClient)
    ac     = ctypes.c_void_p(0)
    fn     = _vtbl_fn(dev, _IMMD_Activate,
                      ctypes.c_long,
                      ctypes.POINTER(_GUID),
                      ctypes.c_uint,
                      ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_void_p))
    hr = fn(dev, ctypes.byref(iid_ac), CLSCTX_ALL,
            pv_ptr if pv_ptr is not None else None,
            ctypes.byref(ac))
    return ac.value if hr == 0 and ac.value else 0


def _get_mix_format(ac: int) -> tuple[int, int]:
    """IAudioClient::GetMixFormat → (sample_rate, channels)"""
    pwfx = ctypes.c_void_p(0)
    fn   = _vtbl_fn(ac, _IAC_GetMixFormat,
                    ctypes.c_long,
                    ctypes.POINTER(ctypes.c_void_p))
    hr   = fn(ac, ctypes.byref(pwfx))
    if hr == 0 and pwfx.value:
        wfx = ctypes.cast(pwfx.value, ctypes.POINTER(_WAVEFORMATEX)).contents
        return int(wfx.nSamplesPerSec), int(wfx.nChannels)
    return 48000, 2


def _initialize_client(ac: int, sr: int, ch: int, flags: int) -> bool:
    wfx = _make_wfx(sr, ch)
    fn  = _vtbl_fn(ac, _IAC_Initialize,
                   ctypes.c_long,
                   ctypes.c_int,
                   ctypes.c_uint,
                   ctypes.c_longlong,
                   ctypes.c_longlong,
                   ctypes.POINTER(_WAVEFORMATEX),
                   ctypes.c_void_p)
    hr  = fn(ac, AUDCLNT_SHAREMODE_SHARED, flags, 10_000_000, 0, ctypes.byref(wfx), None)
    return hr == 0


def _get_capture_client(ac: int) -> int:
    iid_cc = _make_guid(IID_IAudioCaptureClient)
    cc     = ctypes.c_void_p(0)
    fn     = _vtbl_fn(ac, _IAC_GetService,
                      ctypes.c_long,
                      ctypes.POINTER(_GUID),
                      ctypes.POINTER(ctypes.c_void_p))
    hr     = fn(ac, ctypes.byref(iid_cc), ctypes.byref(cc))
    return cc.value if hr == 0 and cc.value else 0


def _read_capture_buffer(cc: int, ch: int) -> "np.ndarray | None":
    nf = ctypes.c_uint32(0)
    fn_nps = _vtbl_fn(cc, _IACC_GetNextPacketSize,
                      ctypes.c_long,
                      ctypes.POINTER(ctypes.c_uint32))
    if fn_nps(cc, ctypes.byref(nf)) != 0 or nf.value == 0:
        return None

    buf       = ctypes.c_void_p(0)
    frames_rd = ctypes.c_uint32(0)
    flags_rd  = ctypes.c_uint32(0)
    fn_gb = _vtbl_fn(cc, _IACC_GetBuffer,
                     ctypes.c_long,
                     ctypes.POINTER(ctypes.c_void_p),
                     ctypes.POINTER(ctypes.c_uint32),
                     ctypes.POINTER(ctypes.c_uint32),
                     ctypes.c_void_p,
                     ctypes.c_void_p)
    hr = fn_gb(cc, ctypes.byref(buf), ctypes.byref(frames_rd),
               ctypes.byref(flags_rd), None, None)
    if hr != 0 or frames_rd.value == 0 or not buf.value:
        return None

    raw = (ctypes.c_byte * (frames_rd.value * ch * 4)).from_address(buf.value)
    arr = np.frombuffer(raw, dtype=np.float32).copy()

    _vtbl_fn(cc, _IACC_ReleaseBuffer,
             ctypes.c_long,
             ctypes.c_uint32)(cc, frames_rd.value)
    return arr


# ══════════════════════════════════════════════════════════════════════════════
def proc_audio_capture(audio_queue: Queue, stop_flag: Value, cfg: dict, log_queue=None):
    # ── PyInstaller 자식 프로세스에서 sys.stdout/stderr 가 None 이면
    #    예외 traceback 출력 시 'NoneType has no attribute write' 발생.
    #    무해한 null sink 로 교체해 크래시를 방지한다.
    import sys, os
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    SR       = cfg["AUDIO_SR"]
    chunk_ms = 50

    # ── COM 초기화: 자식 프로세스 진입 즉시, comtypes 보다 먼저 ──────────────
    _hr_init = _coinit()

    # ── LOG 헬퍼 ──────────────────────────────────────────────────────────────
    # 싱크 ON/OFF 무관하게 항상 log_queue 우선 → 없으면 audio_queue LOG 태그
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

    _com_status = {0: "S_OK(새 초기화)", 1: "S_FALSE(기존 STA 재사용)"}.get(
        _hr_init & 0xFFFFFFFF,
        f"hr=0x{_hr_init & 0xFFFFFFFF:08X}(MTA 폴백 또는 오류)"
    )
    send_log(f"ℹ COM init {_com_status} | "
             f"Win build={_WIN_BUILD} | ProcessLoopback={_SUPPORT_PROCESS_LOOPBACK}")

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

    # ── 밴드패스 필터 ─────────────────────────────────────────────────────────
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

    # ══════════════════════════════════════════════════════════════════════════
    # 방법 1: ProcessLoopback (Win 10 build 19041+)
    # ══════════════════════════════════════════════════════════════════════════

    def _open_process_loopback(pid: int) -> tuple:
        """반환: (ac, cc, sr, ch, err_str)  실패 시 ac=cc=0"""
        dev = _get_default_device()
        if not dev:
            return 0, 0, 0, 0, "IMMDevice 획득 실패"

        params = _AUDIOCLIENT_ACTIVATION_PARAMS()
        params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        params.ProcessLoopbackParams.TargetProcessId     = pid
        params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE

        pv = _PROPVARIANT()
        pv.vt             = 0x41   # VT_BLOB
        pv.blob_cbSize    = ctypes.sizeof(params)
        pv.blob_pBlobData = ctypes.cast(ctypes.addressof(params), ctypes.c_void_p)

        ac = _activate_audio_client(dev, ctypes.byref(pv))
        _release(dev)
        if not ac:
            return 0, 0, 0, 0, "IAudioClient Activate 실패 (ProcessLoopback)"

        sr, ch = _get_mix_format(ac)
        flags  = (AUDCLNT_STREAMFLAGS_LOOPBACK |
                  AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM |
                  AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
        if not _initialize_client(ac, sr, ch, flags):
            _release(ac)
            return 0, 0, 0, 0, "IAudioClient::Initialize 실패"

        cc = _get_capture_client(ac)
        if not cc:
            _release(ac)
            return 0, 0, 0, 0, "GetService(CaptureClient) 실패"

        _vtbl_fn(ac, _IAC_Start, ctypes.c_long)(ac)
        return ac, cc, sr, ch, ""

    def capture_via_process_loopback() -> tuple[bool, str]:
        if not _SUPPORT_PROCESS_LOOPBACK:
            return False, f"ProcessLoopback 미지원 빌드 ({_WIN_BUILD} < 19041)"

        pot_pid = find_potplayer_pid()
        if pot_pid is None:
            return False, "팟플레이어 PID 없음 (ProcessLoopback)"

        ac, cc, sr, ch, err = _open_process_loopback(pot_pid)
        if not cc:
            return False, f"ProcessLoopback 스트림 오픈 실패: {err}"

        sos, sosfilt = make_bandpass(sr)
        send_log(f"🎙 [ProcessLoopback] PID={pot_pid} sr={sr} ch={ch}")

        RECHECK    = 3.0
        last_check = time.time()
        cur_pid    = pot_pid

        while not stop_flag.value:
            now = time.time()

            if now - last_check >= RECHECK:
                last_check = now
                new_pid = find_potplayer_pid()
                if new_pid is None:
                    _vtbl_fn(ac, _IAC_Stop, ctypes.c_long)(ac)
                    return False, "팟플레이어 종료 감지 (ProcessLoopback)"
                if new_pid != cur_pid:
                    _vtbl_fn(ac, _IAC_Stop, ctypes.c_long)(ac)
                    ac2, cc2, sr2, ch2, err2 = _open_process_loopback(new_pid)
                    if cc2:
                        ac, cc, sr, ch = ac2, cc2, sr2, ch2
                        sos, sosfilt   = make_bandpass(sr)
                        cur_pid        = new_pid
                        send_log(f"🎙 [ProcessLoopback] PID 변경 재연결: {new_pid}")
                    else:
                        send_log(f"⚠ [ProcessLoopback] PID 변경 재연결 실패: {err2}")

            arr = _read_capture_buffer(cc, ch)
            if arr is None:
                time.sleep(chunk_ms / 1000)
                continue

            if ch > 1:
                arr = arr.reshape(-1, ch).mean(axis=1)
            arr = apply_filter(arr, sos, sosfilt)
            queue_put(audio_queue, (time.time(), float(np.sqrt(np.mean(arr ** 2)))))

        _vtbl_fn(ac, _IAC_Stop, ctypes.c_long)(ac)
        return True, ""

    # ══════════════════════════════════════════════════════════════════════════
    # 방법 2: WASAPI Session Loopback (Win 10 이하 / ProcessLoopback 실패)
    # ══════════════════════════════════════════════════════════════════════════

    def capture_via_session_loopback() -> tuple[bool, str]:
        dev = _get_default_device()
        if not dev:
            return False, "IMMDevice 획득 실패 (SessionLoopback)"

        ac = _activate_audio_client(dev)
        _release(dev)
        if not ac:
            return False, "IAudioClient Activate 실패 (SessionLoopback)"

        sr, ch = _get_mix_format(ac)
        flags  = (AUDCLNT_STREAMFLAGS_LOOPBACK |
                  AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM |
                  AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
        if not _initialize_client(ac, sr, ch, flags):
            _release(ac)
            return False, "IAudioClient::Initialize 실패 (SessionLoopback)"

        cc = _get_capture_client(ac)
        if not cc:
            _release(ac)
            return False, "GetService(CaptureClient) 실패 (SessionLoopback)"

        _vtbl_fn(ac, _IAC_Start, ctypes.c_long)(ac)
        sos, sosfilt = make_bandpass(sr)
        send_log(f"🎙 [SessionLoopback] 전체 루프백 sr={sr} ch={ch}")

        RECHECK    = 3.0
        last_check = time.time()
        pot_active = False

        def _check_pot_session() -> bool:
            pid = find_potplayer_pid()
            if pid is None:
                return False
            dev2 = _get_default_device()
            if not dev2:
                return False
            found = any(sp == pid for _, sp in _iter_session_pids(dev2))
            _release(dev2)
            return found

        while not stop_flag.value:
            now = time.time()
            if now - last_check >= RECHECK:
                last_check = now
                was        = pot_active
                pot_active = _check_pot_session()
                if pot_active and not was:
                    send_log("🎙 [SessionLoopback] 팟플레이어 세션 감지 → 캡처 활성")
                elif not pot_active and was:
                    send_log("⚠ [SessionLoopback] 팟플레이어 세션 소멸 → RMS=0 대기")

            arr = _read_capture_buffer(cc, ch)
            if arr is None:
                time.sleep(chunk_ms / 1000)
                continue

            if not pot_active:
                queue_put(audio_queue, (time.time(), 0.0))
                continue

            if ch > 1:
                arr = arr.reshape(-1, ch).mean(axis=1)
            arr = apply_filter(arr, sos, sosfilt)
            queue_put(audio_queue, (time.time(), float(np.sqrt(np.mean(arr ** 2)))))

        _vtbl_fn(ac, _IAC_Stop, ctypes.c_long)(ac)
        return True, ""

    # ══════════════════════════════════════════════════════════════════════════
    # 방법 3: IAudioMeterInformation 폴백
    # ══════════════════════════════════════════════════════════════════════════

    _meter_cache = [0, None]   # [mt_ptr_val, pid]

    def get_potplayer_rms() -> tuple[float | None, str]:
        pp = find_potplayer_pid()
        if pp is None:
            _meter_cache[0] = 0
            return None, "PID없음"

        # 캐시 히트
        if _meter_cache[0] and _meter_cache[1] == pp:
            pk = ctypes.c_float(0)
            try:
                fn = _vtbl_fn(_meter_cache[0], _IAMI_GetPeakValue,
                              ctypes.c_long,
                              ctypes.POINTER(ctypes.c_float))
                fn(_meter_cache[0], ctypes.byref(pk))
                return float(pk.value), ""
            except Exception:
                _meter_cache[0] = 0

        dev = _get_default_device()
        if not dev:
            return None, "IMMDevice 획득 실패"

        iid_meter = _make_guid(IID_IAudioMeterInformation)
        result    = None, f"팟플레이어 세션 없음 (PID={pp})"

        for sess_val, sess_pid in _iter_session_pids(dev):
            if sess_pid != pp:
                continue
            mt = ctypes.c_void_p(0)
            fn_qi = _vtbl_fn(sess_val, 0,
                             ctypes.c_long,
                             ctypes.POINTER(_GUID),
                             ctypes.POINTER(ctypes.c_void_p))
            if fn_qi(sess_val, ctypes.byref(iid_meter), ctypes.byref(mt)) != 0 or not mt.value:
                result = None, "IAudioMeter QI 실패"
                break
            _meter_cache[0] = mt.value
            _meter_cache[1] = pp
            pk = ctypes.c_float(0)
            _vtbl_fn(mt.value, _IAMI_GetPeakValue,
                     ctypes.c_long,
                     ctypes.POINTER(ctypes.c_float))(mt.value, ctypes.byref(pk))
            result = float(pk.value), ""
            break

        _release(dev)
        return result

    # ══════════════════════════════════════════════════════════════════════════
    # 메인 루프: ProcessLoopback → SessionLoopback → IAudioMeter
    # ══════════════════════════════════════════════════════════════════════════

    _retry_count = 0  # 재시도 횟수 (로그에 표시)

    while not stop_flag.value:

        # 1순위: ProcessLoopback (Win 10 20H1+)
        if _SUPPORT_PROCESS_LOOPBACK:
            try:
                ok, reason = capture_via_process_loopback()
            except Exception as e:
                ok, reason = False, f"예외: {e}"
            if ok:
                _retry_count = 0
                continue
            send_log(f"⚠ ProcessLoopback 종료: {reason}")

        # 2순위: WASAPI Session Loopback
        try:
            ok, reason = capture_via_session_loopback()
        except Exception as e:
            ok, reason = False, f"예외: {e}"
        if ok:
            _retry_count = 0
            continue

        send_log(f"⚠ SessionLoopback 실패: {reason}")
        send_log("⏳ IAudioMeter 폴백 시작 (5초 후 재시도 예정)")

        # 3순위: IAudioMeter 폴백
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
                    send_log(f"⚠ IAudioMeter 실패: {err}")
                    fallback_logged = True
                if "PID없음" in err:
                    break
            time.sleep(chunk_ms / 1000)

        # 재시도 전 대기 — 로그를 읽을 수 있도록 5초 유지
        _retry_count += 1
        send_log(f"🔄 {_retry_count}회 재시도 대기 중 (5초)...")
        for _ in range(50):          # 0.1초 × 50 = 5초, stop_flag 반응 유지
            if stop_flag.value:
                break
            time.sleep(0.1)

    _couninit()
