"""
audio_capture.py — Windows WASAPI ProcessLoopback 직접 구현 (ctypes + COM)

pyaudiowpatch 의 get_process_loopback_device() 에 의존하지 않고,
Windows 10 20H1(빌드 19041)+ 의 IAudioClient3 ProcessLoopback 을 직접 사용한다.

흐름:
  CoCreateInstance(MMDeviceEnumerator)
    → GetDefaultAudioEndpoint(eRender)
      → Activate(IAudioClient3)
        → InitializeSharedAudioStream(AUDCLNT_STREAMFLAGS_LOOPBACK_PROCESSLOOPBACK, PID)
          → IAudioCaptureClient.GetBuffer() 루프
"""

import ctypes
import ctypes.wintypes
import time
import platform
import numpy as np
import psutil
from multiprocessing import Queue, Value
from win32_utils import CFG, queue_put

# ─────────────────────────────────────────────────────────────────────────────
# Windows 빌드 확인
# ─────────────────────────────────────────────────────────────────────────────
def _windows_build() -> int:
    try:
        return int(platform.version().split(".")[-1])
    except Exception:
        return 0

_WIN_BUILD                = _windows_build()
_SUPPORT_PROCESS_LOOPBACK = (_WIN_BUILD >= 19041)

# ─────────────────────────────────────────────────────────────────────────────
# COM / WASAPI 상수
# ─────────────────────────────────────────────────────────────────────────────
CLSCTX_ALL                             = 0x17
COINIT_MULTITHREADED                   = 0x0
COINIT_APARTMENTTHREADED               = 0x2

AUDCLNT_STREAMFLAGS_LOOPBACK           = 0x00020000
AUDCLNT_STREAMFLAGS_LOOPBACK_PROCESSLOOPBACK = 0x01000000
AUDCLNT_SHAREMODE_SHARED               = 0

WAVE_FORMAT_IEEE_FLOAT                 = 0x0003
WAVE_FORMAT_EXTENSIBLE                 = 0xFFFE
SPEAKER_FRONT_LEFT                     = 0x1
SPEAKER_FRONT_RIGHT                    = 0x2

AUDCLNT_S_BUFFER_EMPTY                 = 0x08890001

eRender                                = 0
eConsole                               = 0

# CLSID / IID (바이트열)
CLSID_MMDeviceEnumerator = ctypes.c_byte * 16
_CLSID_MMDeviceEnumerator = (ctypes.c_byte * 16)(
    0x86, 0x2F, 0xE5, 0xBC, 0xD8, 0xAF, 0x67, 0x4D,
    0xB5, 0x11, 0x8B, 0x6D, 0x75, 0xC2, 0x41, 0x00,
)
_IID_IMMDeviceEnumerator = (ctypes.c_byte * 16)(
    0xA9, 0x5E, 0x64, 0xF4, 0xD2, 0x8D, 0xF9, 0x4E,
    0xAF, 0xED, 0x08, 0x00, 0x20, 0x0C, 0x9A, 0x66,
)
_IID_IAudioClient3 = (ctypes.c_byte * 16)(
    0x7D, 0xB4, 0x2E, 0x7B, 0x7F, 0xAE, 0x93, 0x4A,
    0xAA, 0x01, 0x8B, 0x3E, 0xAD, 0xEE, 0x51, 0x90,
)
_IID_IAudioCaptureClient = (ctypes.c_byte * 16)(
    0xC8, 0xAD, 0xBD, 0xC5, 0x5F, 0x35, 0xD9, 0x4E,
    0x97, 0x24, 0x29, 0x11, 0x23, 0x3D, 0x97, 0x74,
)

# ─────────────────────────────────────────────────────────────────────────────
# WAVEFORMATEX / WAVEFORMATEXTENSIBLE
# ─────────────────────────────────────────────────────────────────────────────
class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag",      ctypes.c_ushort),
        ("nChannels",       ctypes.c_ushort),
        ("nSamplesPerSec",  ctypes.c_uint),
        ("nAvgBytesPerSec", ctypes.c_uint),
        ("nBlockAlign",     ctypes.c_ushort),
        ("wBitsPerSample",  ctypes.c_ushort),
        ("cbSize",          ctypes.c_ushort),
    ]

class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("wValidBitsPerSample", ctypes.c_ushort),
                    ("wSamplesPerBlock",     ctypes.c_ushort),
                    ("wReserved",            ctypes.c_ushort)]
    _fields_ = [
        ("Format",          WAVEFORMATEX),
        ("Samples",         _U),
        ("dwChannelMask",   ctypes.c_uint),
        ("SubFormat",       ctypes.c_byte * 16),
    ]

# IEEE float GUID: {00000003-0000-0010-8000-00aa00389b71}
_KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = (ctypes.c_byte * 16)(
    0x03, 0x00, 0x00, 0x00,
    0x00, 0x00,
    0x10, 0x00,
    0x80, 0x00,
    0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71,
)

# ─────────────────────────────────────────────────────────────────────────────
# AudioClientProperties (ProcessLoopback 용)
# ─────────────────────────────────────────────────────────────────────────────
class AudioClientProperties(ctypes.Structure):
    _fields_ = [
        ("cbSize",          ctypes.c_uint),
        ("bIsOffload",      ctypes.c_int),   # BOOL
        ("eCategory",       ctypes.c_int),   # AUDIO_STREAM_CATEGORY (0=Other)
        ("Options",         ctypes.c_uint),  # AUDCLNT_STREAMOPTIONS
    ]

AUDCLNT_STREAMOPTIONS_NONE             = 0x0
AUDCLNT_STREAMOPTIONS_MATCH_FORMAT     = 0x1
AUDCLNT_STREAMOPTIONS_LOOPBACK         = 0x4   # ProcessLoopback 활성화 옵션

# ─────────────────────────────────────────────────────────────────────────────
# COM 인터페이스 vtable 래퍼 (필요한 메서드만)
# ─────────────────────────────────────────────────────────────────────────────
_ole32   = ctypes.windll.ole32
_kernel32= ctypes.windll.kernel32

def _com_vtbl(obj, index):
    """COM 객체의 vtable에서 index번째 함수 포인터를 반환."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))
    fn_ptr = ctypes.cast(vtbl[0], ctypes.POINTER(ctypes.c_void_p))[index]
    return fn_ptr

def _hresult_check(hr, label=""):
    if hr < 0:
        raise OSError(f"{label} HRESULT=0x{hr & 0xFFFFFFFF:08X}")

# ─────────────────────────────────────────────────────────────────────────────
# 고수준 래퍼 함수들
# ─────────────────────────────────────────────────────────────────────────────

def _co_initialize():
    """
    CoInitializeEx 를 시도한다.
    RPC_E_CHANGED_MODE(0x80010106): 이미 다른 모드로 초기화된 스레드 → 무시하고 진행.
    ctypes c_long 은 부호 있는 정수이므로 음수 값으로 비교한다.
    """
    _ole32.CoInitializeEx.restype = ctypes.c_long
    hr = _ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    if hr >= 0:
        return  # S_OK(0) or S_FALSE(1)
    # 0x80010106 을 signed int32 로 해석하면 -2147417850
    if hr == ctypes.c_long(0x80010106).value:
        return  # 이미 초기화돼 있음 — COM 준비된 상태이므로 그냥 진행
    _hresult_check(hr, "CoInitializeEx")

def _co_uninitialize():
    _ole32.CoUninitialize()

def _create_device_enumerator():
    """IMMDeviceEnumerator COM 객체 생성."""
    obj = ctypes.c_void_p()
    hr = _ole32.CoCreateInstance(
        _CLSID_MMDeviceEnumerator,
        None,
        CLSCTX_ALL,
        _IID_IMMDeviceEnumerator,
        ctypes.byref(obj),
    )
    _hresult_check(hr, "CoCreateInstance(MMDeviceEnumerator)")
    return obj

def _get_default_render_device(enumerator):
    """기본 렌더(출력) 엔드포인트 IMMDevice 획득."""
    # IMMDeviceEnumerator::GetDefaultAudioEndpoint — vtable[4]
    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,  # this
        ctypes.c_uint,    # dataFlow  (eRender=0)
        ctypes.c_uint,    # role      (eConsole=0)
        ctypes.POINTER(ctypes.c_void_p),  # ppDevice
    )
    fn = fn_type(_com_vtbl(enumerator, 4))
    device = ctypes.c_void_p()
    hr = fn(enumerator, eRender, eConsole, ctypes.byref(device))
    _hresult_check(hr, "GetDefaultAudioEndpoint")
    return device

def _activate_audio_client3(device):
    """IMMDevice::Activate → IAudioClient3."""
    # IMMDevice::Activate — vtable[3]
    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,   # this
        ctypes.c_byte * 16,# riid
        ctypes.c_uint,     # dwClsCtx
        ctypes.c_void_p,   # pActivationParams (NULL)
        ctypes.POINTER(ctypes.c_void_p),  # ppInterface
    )
    fn = fn_type(_com_vtbl(device, 3))
    client = ctypes.c_void_p()
    hr = fn(device, _IID_IAudioClient3, CLSCTX_ALL, None, ctypes.byref(client))
    _hresult_check(hr, "IMMDevice::Activate(IAudioClient3)")
    return client

def _set_client_properties(client, pid: int):
    """
    IAudioClient3::SetClientProperties 로 ProcessLoopback 옵션 설정.
    vtable 인덱스는 IAudioClient3 기준 (IAudioClient 상속).
    IAudioClient  메서드 수: Initialize(5) GetBufferSize(6) GetStreamLatency(7)
                             GetCurrentPadding(8) IsFormatSupported(9)
                             GetMixFormat(10) GetDevicePeriod(11) Start(12)
                             Stop(13) Reset(14) SetEventHandle(15) GetService(16)
    IAudioClient2 추가: IsOffloadCapable(17) SetClientProperties(18)
                        GetBufferSizeLimits(19)
    IAudioClient3 추가: GetSharedModeEnginePeriod(20)
                        GetCurrentSharedModeEnginePeriod(21)
                        InitializeSharedAudioStream(22)
    SetClientProperties = vtable[18]
    """
    props = AudioClientProperties()
    props.cbSize    = ctypes.sizeof(AudioClientProperties)
    props.bIsOffload= 0
    props.eCategory = 0   # AudioCategory_Other
    props.Options   = AUDCLNT_STREAMOPTIONS_LOOPBACK  # ProcessLoopback 활성화

    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,                     # this
        ctypes.POINTER(AudioClientProperties),
    )
    fn = fn_type(_com_vtbl(client, 18))
    hr = fn(client, ctypes.byref(props))
    _hresult_check(hr, "SetClientProperties")

def _get_mix_format(client) -> WAVEFORMATEX:
    """IAudioClient::GetMixFormat — 엔진 내부 포맷 조회."""
    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    fn = fn_type(_com_vtbl(client, 10))
    fmt_ptr = ctypes.c_void_p()
    hr = fn(client, ctypes.byref(fmt_ptr))
    _hresult_check(hr, "GetMixFormat")
    # 반환된 포인터를 WAVEFORMATEX 로 해석
    fmt = ctypes.cast(fmt_ptr, ctypes.POINTER(WAVEFORMATEX)).contents
    return fmt, fmt_ptr

def _initialize_shared_audio_stream(client, flags: int, period_frames: int,
                                     fmt_ptr, pid: int):
    """
    IAudioClient3::InitializeSharedAudioStream — vtable[22]
    AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_LOOPBACK_PROCESSLOOPBACK
    """
    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,   # this
        ctypes.c_uint,     # StreamFlags
        ctypes.c_uint,     # PeriodInFrames
        ctypes.c_void_p,   # pFormat (WAVEFORMATEX*)
        ctypes.c_void_p,   # AudioSessionGuid (NULL)
    )
    fn = fn_type(_com_vtbl(client, 22))
    hr = fn(client, flags, period_frames, fmt_ptr, None)
    _hresult_check(hr, "InitializeSharedAudioStream")

def _get_capture_client(client):
    """IAudioClient::GetService(IID_IAudioCaptureClient)."""
    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_byte * 16,
        ctypes.POINTER(ctypes.c_void_p),
    )
    fn = fn_type(_com_vtbl(client, 16))
    cap = ctypes.c_void_p()
    hr = fn(client, _IID_IAudioCaptureClient, ctypes.byref(cap))
    _hresult_check(hr, "GetService(IAudioCaptureClient)")
    return cap

def _audio_client_start(client):
    fn_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
    fn = fn_type(_com_vtbl(client, 12))
    _hresult_check(fn(client), "IAudioClient::Start")

def _audio_client_stop(client):
    fn_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
    fn = fn_type(_com_vtbl(client, 13))
    fn(client)

def _get_next_packet_size(capture_client) -> int:
    """IAudioCaptureClient::GetNextPacketSize — vtable[3]"""
    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint),
    )
    fn = fn_type(_com_vtbl(capture_client, 3))
    frames = ctypes.c_uint(0)
    hr = fn(capture_client, ctypes.byref(frames))
    _hresult_check(hr, "GetNextPacketSize")
    return frames.value

def _get_buffer(capture_client):
    """
    IAudioCaptureClient::GetBuffer — vtable[1]
    반환: (data_ptr, num_frames, flags, device_position, qpc_position)
    """
    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,                      # this
        ctypes.POINTER(ctypes.c_void_p),       # ppData
        ctypes.POINTER(ctypes.c_uint),         # pNumFramesToRead
        ctypes.POINTER(ctypes.c_uint),         # pdwFlags
        ctypes.POINTER(ctypes.c_ulonglong),    # pu64DevicePosition (opt)
        ctypes.POINTER(ctypes.c_ulonglong),    # pu64QPCPosition    (opt)
    )
    fn = fn_type(_com_vtbl(capture_client, 1))
    data    = ctypes.c_void_p()
    frames  = ctypes.c_uint(0)
    flags   = ctypes.c_uint(0)
    dev_pos = ctypes.c_ulonglong(0)
    qpc_pos = ctypes.c_ulonglong(0)
    hr = fn(capture_client,
            ctypes.byref(data), ctypes.byref(frames),
            ctypes.byref(flags), ctypes.byref(dev_pos), ctypes.byref(qpc_pos))
    _hresult_check(hr, "GetBuffer")
    return data, frames.value, flags.value

def _release_buffer(capture_client, num_frames: int):
    """IAudioCaptureClient::ReleaseBuffer — vtable[2]"""
    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_uint,
    )
    fn = fn_type(_com_vtbl(capture_client, 2))
    fn(capture_client, num_frames)

def _com_release(obj):
    if obj and obj.value:
        fn_type = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        fn = fn_type(_com_vtbl(obj, 2))  # IUnknown::Release = vtable[2]
        fn(obj)

def _co_task_mem_free(ptr):
    if ptr and ptr.value:
        _ole32.CoTaskMemFree(ptr)

# ─────────────────────────────────────────────────────────────────────────────
# 팟플레이어 PID 탐색
# ─────────────────────────────────────────────────────────────────────────────
_pid_cache = [None, 0.0]

def _find_potplayer_pid() -> "int | None":
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

# ─────────────────────────────────────────────────────────────────────────────
# 밴드패스 필터
# ─────────────────────────────────────────────────────────────────────────────
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

# ─────────────────────────────────────────────────────────────────────────────
# ProcessLoopback 세션 1회 실행
# ─────────────────────────────────────────────────────────────────────────────
AUDCLNT_BUFFERFLAGS_SILENT = 0x2

def _run_capture_session(pid: int, audio_queue: Queue,
                          stop_flag: Value, send_log) -> "tuple[bool, str]":
    """
    팟플레이어 PID 에 대해 ProcessLoopback 캡처를 열고
    stop_flag 가 설정될 때까지 RMS 값을 audio_queue 에 넣는다.
    반환: (정상종료여부, 실패이유)
    """
    enumerator    = None
    device        = None
    client        = None
    capture_client= None
    fmt_ptr       = None

    try:
        _co_initialize()

        enumerator = _create_device_enumerator()
        device     = _get_default_render_device(enumerator)
        client     = _activate_audio_client3(device)

        # ProcessLoopback 옵션 설정
        _set_client_properties(client, pid)

        # 엔진 믹스 포맷 조회
        fmt, fmt_ptr = _get_mix_format(client)
        sr = fmt.nSamplesPerSec
        ch = fmt.nChannels

        # 스트림 초기화
        flags = (AUDCLNT_STREAMFLAGS_LOOPBACK |
                 AUDCLNT_STREAMFLAGS_LOOPBACK_PROCESSLOOPBACK)
        # PeriodInFrames: 10ms
        period_frames = sr // 100

        _initialize_shared_audio_stream(client, flags, period_frames, fmt_ptr, pid)

        # IAudioCaptureClient 획득
        capture_client = _get_capture_client(client)

        sos, sosfilt = _make_bandpass(sr)
        send_log(f"🎙 [ProcessLoopback/WinAPI] PID={pid} sr={sr} ch={ch}")

        _audio_client_start(client)

        RECHECK    = 3.0
        last_check = time.time()
        cur_pid    = pid

        while not stop_flag.value:
            # PID 재확인
            now = time.time()
            if now - last_check >= RECHECK:
                last_check = now
                new_pid = _find_potplayer_pid()
                if new_pid is None:
                    return False, "팟플레이어 종료됨"
                if new_pid != cur_pid:
                    return False, f"PID 변경 ({cur_pid} → {new_pid}) — 재연결"

            # 패킷 루프
            packet_frames = _get_next_packet_size(capture_client)
            if packet_frames == 0:
                time.sleep(0.005)
                continue

            data, num_frames, flg = _get_buffer(capture_client)

            if num_frames > 0:
                if flg & AUDCLNT_BUFFERFLAGS_SILENT:
                    rms = 0.0
                else:
                    # float32 샘플로 해석
                    buf = (ctypes.c_float * (num_frames * ch)).from_address(data.value)
                    arr = np.frombuffer(buf, dtype=np.float32).copy()
                    if ch > 1:
                        arr = arr.reshape(-1, ch).mean(axis=1)
                    arr = _apply_filter(arr, sos, sosfilt)
                    rms = float(np.sqrt(np.mean(arr ** 2)))

                queue_put(audio_queue, (time.time(), rms))

            _release_buffer(capture_client, num_frames)

        return True, ""

    except OSError as e:
        return False, str(e)
    finally:
        if capture_client:
            try: _audio_client_stop(client)
            except Exception: pass
            _com_release(capture_client)
        if client:
            _com_release(client)
        if device:
            _com_release(device)
        if enumerator:
            _com_release(enumerator)
        if fmt_ptr and fmt_ptr.value:
            _co_task_mem_free(fmt_ptr)
        try:
            _co_uninitialize()
        except Exception:
            pass

# ─────────────────────────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────────────────────────
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
        send_log(f"✖ ProcessLoopback 미지원 (빌드 {_WIN_BUILD} < 19041). Windows 10 20H1 이상 필요.")
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
