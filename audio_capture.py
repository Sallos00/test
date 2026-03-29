"""
audio_capture.py — Windows WASAPI ProcessLoopback (OBS 방식 동일 구현)

OBS win-wasapi 플러그인과 동일한 흐름:
  ActivateAudioInterfaceAsync(
      VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
      IAudioClient,
      AUDIOCLIENT_ACTIVATION_PARAMS { TargetProcessId = PID }
  )
  → IAudioClient::Initialize(AUDCLNT_STREAMFLAGS_LOOPBACK)
  → IAudioCaptureClient::GetBuffer() 루프

CoCreateInstance / IAudioClient3 / SetClientProperties 는 사용하지 않는다.
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
# DLL / 상수
# ─────────────────────────────────────────────────────────────────────────────
_ole32    = ctypes.windll.ole32
_kernel32 = ctypes.windll.kernel32

AUDCLNT_STREAMFLAGS_LOOPBACK     = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK= 0x00040000
AUDCLNT_SHAREMODE_SHARED         = 0
AUDCLNT_BUFFERFLAGS_SILENT       = 0x2
AUDCLNT_S_BUFFER_EMPTY           = 0x08890001

# AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
# PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0 (OBS 기본값)
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0

# VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK (고정 문자열 GUID)
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

# IID_IAudioClient {1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}
_IID_IAudioClient = (ctypes.c_byte * 16)(
    0x4C, 0xAD, 0xB9, 0x1C,  # Data1 LE
    0xFA, 0xDB,               # Data2 LE
    0x32, 0x4C,               # Data3 LE
    0xB1, 0x78, 0xC2, 0xF5, 0x68, 0xA7, 0x03, 0xB2,  # Data4
)

# IID_IAudioCaptureClient {C8ADBD64-E71E-48A0-A4DE-185C395CD317}
_IID_IAudioCaptureClient = (ctypes.c_byte * 16)(
    0x64, 0xBD, 0xAD, 0xC8,  # Data1 LE
    0x1E, 0xE7,               # Data2 LE
    0xA0, 0x48,               # Data3 LE
    0xA4, 0xDE, 0x18, 0x5C, 0x39, 0x5C, 0xD3, 0x17,  # Data4
)

# ─────────────────────────────────────────────────────────────────────────────
# WAVEFORMATEX
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

# ─────────────────────────────────────────────────────────────────────────────
# AUDIOCLIENT_ACTIVATION_PARAMS
# ─────────────────────────────────────────────────────────────────────────────
class PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId",    ctypes.c_ulong),
        ("ProcessLoopbackMode",ctypes.c_uint),
    ]

class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType",       ctypes.c_uint),
        ("ProcessLoopbackParams",PROCESS_LOOPBACK_PARAMS),
    ]

# PROPVARIANT (VT_BLOB = 0x41)
VT_BLOB = 0x41
class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("pBlobData", ctypes.c_void_p)]

class PROPVARIANT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("blob", BLOB), ("raw", ctypes.c_byte * 16)]
    _fields_ = [
        ("vt",       ctypes.c_ushort),
        ("reserved1",ctypes.c_ushort),
        ("reserved2",ctypes.c_ushort),
        ("reserved3",ctypes.c_ushort),
        ("u",        _U),
    ]

# ─────────────────────────────────────────────────────────────────────────────
# IActivateAudioInterfaceCompletionHandler (콜백 COM 인터페이스)
# Python 에서는 ctypes COM 콜백 대신 이벤트로 동기 대기한다.
# ─────────────────────────────────────────────────────────────────────────────
# vtable 헬퍼
def _vtbl(obj, index):
    vt = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
    return ctypes.cast(vt, ctypes.POINTER(ctypes.c_void_p))[index]

def _hcheck(hr, label=""):
    if hr < 0:
        raise OSError(f"{label} HRESULT=0x{hr & 0xFFFFFFFF:08X}")

# ─────────────────────────────────────────────────────────────────────────────
# ActivateAudioInterfaceAsync 동기 래퍼
# OBS 는 WRL ComPtr + completion handler 를 씀.
# Python 에서는 Mmdevapi.dll 의 함수를 직접 로드하고,
# completion handler 를 IUnknown + vtable 로 직접 구현한다.
# ─────────────────────────────────────────────────────────────────────────────

# IActivateAudioInterfaceCompletionHandler IID {41D94994-97AA-9A40-AB02-E0D17110A9C4}
_IID_IActivateAudioInterfaceCompletionHandler = (ctypes.c_byte * 16)(
    0x94, 0x49, 0xD9, 0x41,  # Data1 LE
    0xAA, 0x97,               # Data2 LE
    0x40, 0x9A,               # Data3 LE
    0xAB, 0x02, 0xE0, 0xD1, 0x71, 0x10, 0xA9, 0xC4,  # Data4
)

# IActivateAudioInterfaceAsyncOperation IID {72A72B72-5653-4BBD-8608-9FD58F9E2177}
_IID_IActivateAudioInterfaceAsyncOperation = (ctypes.c_byte * 16)(
    0x72, 0x2B, 0xA7, 0x72,  # Data1 LE
    0x53, 0x56,               # Data2 LE
    0xBD, 0x4B,               # Data3 LE
    0x86, 0x08, 0x9F, 0xD5, 0x8F, 0x9E, 0x21, 0x77,  # Data4
)

# 콜백 함수 타입
_ACTIVATE_COMPLETED_FN = ctypes.WINFUNCTYPE(
    ctypes.c_long,
    ctypes.c_void_p,   # this (IActivateAudioInterfaceCompletionHandler*)
    ctypes.c_void_p,   # pActivateOperation
)
_ADDREF_FN   = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_RELEASE_FN  = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_QI_FN       = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_void_p,
    ctypes.c_byte * 16, ctypes.POINTER(ctypes.c_void_p)
)

class _CompletionHandlerImpl:
    """
    IActivateAudioInterfaceCompletionHandler 를 순수 ctypes vtable 로 구현.
    ActivateCompleted 가 호출되면 Win32 이벤트를 신호.
    """
    def __init__(self):
        self.event   = _kernel32.CreateEventW(None, True, False, None)
        self.hr_act  = ctypes.c_long(0)
        self.op_ptr  = ctypes.c_void_p(None)

        # vtable 함수들
        def _qi(this, riid, ppv):
            ppv_ptr = ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p))
            ppv_ptr[0] = this
            return 0
        def _addref(this):  return 1
        def _release(this): return 1
        def _activate_completed(this, pOp):
            self.op_ptr.value = pOp
            # GetActivateResult 호출
            fn_type = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_long),
                ctypes.POINTER(ctypes.c_void_p),
            )
            # IActivateAudioInterfaceAsyncOperation::GetActivateResult = vtable[3]
            fn = fn_type(_vtbl(ctypes.c_void_p(pOp), 3))
            inner = ctypes.c_void_p()
            hr = fn(pOp, ctypes.byref(self.hr_act), ctypes.byref(inner))
            self.audio_client = inner
            _kernel32.SetEvent(self.event)
            return 0

        self._qi_fn       = _QI_FN(_qi)
        self._addref_fn   = _ADDREF_FN(_addref)
        self._release_fn  = _RELEASE_FN(_release)
        self._completed_fn= _ACTIVATE_COMPLETED_FN(_activate_completed)

        # vtable 배열: QueryInterface, AddRef, Release, ActivateCompleted
        self._vtable = (ctypes.c_void_p * 4)(
            ctypes.cast(self._qi_fn,        ctypes.c_void_p).value,
            ctypes.cast(self._addref_fn,    ctypes.c_void_p).value,
            ctypes.cast(self._release_fn,   ctypes.c_void_p).value,
            ctypes.cast(self._completed_fn, ctypes.c_void_p).value,
        )
        self._vtable_ptr = ctypes.cast(self._vtable, ctypes.c_void_p)
        # COM 객체 = vtable 포인터를 가리키는 포인터
        self._obj_data   = ctypes.c_void_p(self._vtable_ptr.value)
        self.com_ptr     = ctypes.addressof(self._obj_data)
        self.audio_client= ctypes.c_void_p()

    def wait_and_get_client(self, timeout_ms=5000):
        ret = _kernel32.WaitForSingleObject(self.event, timeout_ms)
        if ret != 0:
            raise OSError("ActivateAudioInterfaceAsync timeout")
        _hcheck(self.hr_act.value, "ActivateCompleted inner")
        return self.audio_client

    def close(self):
        if self.event:
            _kernel32.CloseHandle(self.event)
            self.event = None


def _activate_process_loopback(pid: int) -> ctypes.c_void_p:
    """
    OBS InitClient(ProcessOutput) 동일 흐름:
      ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, ...)
    반환: IAudioClient COM 포인터
    """
    # Mmdevapi.dll 에서 ActivateAudioInterfaceAsync 로드
    mmdevapi = ctypes.windll.LoadLibrary("Mmdevapi.dll")
    fn_activate = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_wchar_p,   # deviceInterfacePath
        ctypes.c_byte * 16, # riid
        ctypes.POINTER(PROPVARIANT),  # activationParams
        ctypes.c_void_p,    # completionHandler (IActivateAudioInterfaceCompletionHandler*)
        ctypes.POINTER(ctypes.c_void_p),  # activationOperation
    )(("ActivateAudioInterfaceAsync", mmdevapi))

    # AUDIOCLIENT_ACTIVATION_PARAMS 설정
    act_params = AUDIOCLIENT_ACTIVATION_PARAMS()
    act_params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
    act_params.ProcessLoopbackParams.TargetProcessId     = pid
    act_params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE

    # PROPVARIANT{VT_BLOB}
    pv = PROPVARIANT()
    pv.vt = VT_BLOB
    pv.u.blob.cbSize    = ctypes.sizeof(act_params)
    pv.u.blob.pBlobData = ctypes.addressof(act_params)

    # completion handler
    handler = _CompletionHandlerImpl()
    async_op = ctypes.c_void_p()

    try:
        hr = fn_activate(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
            _IID_IAudioClient,
            ctypes.byref(pv),
            ctypes.c_void_p(handler.com_ptr),
            ctypes.byref(async_op),
        )
        _hcheck(hr, "ActivateAudioInterfaceAsync")
        client = handler.wait_and_get_client(timeout_ms=5000)
    finally:
        handler.close()

    return client


def _audio_client_get_mix_format(client) -> "tuple[WAVEFORMATEX, ctypes.c_void_p]":
    """IAudioClient::GetMixFormat — vtable[10] (IUnknown 3 + IAudioClient 메서드)"""
    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    fn = fn_type(_vtbl(client, 10))
    ptr = ctypes.c_void_p()
    _hcheck(fn(client, ctypes.byref(ptr)), "GetMixFormat")
    fmt = ctypes.cast(ptr, ctypes.POINTER(WAVEFORMATEX)).contents
    return fmt, ptr


def _audio_client_initialize(client, sr: int, ch: int):
    """
    IAudioClient::Initialize — vtable[5]
    AUDCLNT_SHAREMODE_SHARED, LOOPBACK | EVENTCALLBACK, 5초 버퍼
    포맷은 float32 로 직접 지정 (OBS 방식).
    """
    WAVE_FORMAT_EXTENSIBLE = 0xFFFE
    WAVE_FORMAT_IEEE_FLOAT = 0x0003

    class WAVEFORMATEXTENSIBLE(ctypes.Structure):
        class _S(ctypes.Union):
            _fields_ = [("wValidBitsPerSample", ctypes.c_ushort)]
        _fields_ = [
            ("Format",        WAVEFORMATEX),
            ("Samples",       _S),
            ("dwChannelMask", ctypes.c_uint),
            ("SubFormat",     ctypes.c_byte * 16),
        ]
    # KSDATAFORMAT_SUBTYPE_IEEE_FLOAT
    _SUBTYPE_FLOAT = (ctypes.c_byte * 16)(
        0x03,0x00,0x00,0x00, 0x00,0x00, 0x10,0x00,
        0x80,0x00, 0x00,0xAA,0x00,0x38,0x9B,0x71,
    )
    wfe = WAVEFORMATEXTENSIBLE()
    wfe.Format.wFormatTag      = WAVE_FORMAT_EXTENSIBLE
    wfe.Format.nChannels       = ch
    wfe.Format.nSamplesPerSec  = sr
    wfe.Format.wBitsPerSample  = 32
    wfe.Format.nBlockAlign     = ch * 4
    wfe.Format.nAvgBytesPerSec = sr * ch * 4
    wfe.Format.cbSize          = ctypes.sizeof(WAVEFORMATEXTENSIBLE) - ctypes.sizeof(WAVEFORMATEX)
    wfe.Samples.wValidBitsPerSample = 32
    # 스테레오 채널 마스크
    wfe.dwChannelMask = 0x3 if ch == 2 else (0x1 if ch == 1 else 0)
    wfe.SubFormat     = _SUBTYPE_FLOAT

    BUFFER_100NS = 5 * 10_000_000  # 5초

    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,   # this
        ctypes.c_uint,     # ShareMode
        ctypes.c_uint,     # StreamFlags
        ctypes.c_longlong, # hnsBufferDuration
        ctypes.c_longlong, # hnsPeriodicity
        ctypes.c_void_p,   # pFormat
        ctypes.c_void_p,   # AudioSessionGuid
    )
    fn = fn_type(_vtbl(client, 5))
    flags = AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
    hr = fn(client, AUDCLNT_SHAREMODE_SHARED, flags,
            BUFFER_100NS, 0, ctypes.addressof(wfe), None)
    _hcheck(hr, "IAudioClient::Initialize")
    return wfe  # 포맷 유지 (GC 방지)


def _audio_client_set_event(client, h_event):
    """IAudioClient::SetEventHandle — vtable[15]"""
    fn_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)
    fn = fn_type(_vtbl(client, 15))
    _hcheck(fn(client, h_event), "SetEventHandle")


def _audio_client_start(client):
    fn_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
    _hcheck(fn_type(_vtbl(client, 12))(client), "IAudioClient::Start")


def _audio_client_stop(client):
    fn_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
    fn_type(_vtbl(client, 13))(client)


def _get_capture_client(client) -> ctypes.c_void_p:
    """IAudioClient::GetService(IID_IAudioCaptureClient) — vtable[16]"""
    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p,
        ctypes.c_byte * 16,
        ctypes.POINTER(ctypes.c_void_p),
    )
    fn = fn_type(_vtbl(client, 16))
    cap = ctypes.c_void_p()
    _hcheck(fn(client, _IID_IAudioCaptureClient, ctypes.byref(cap)), "GetService(CaptureClient)")
    return cap


def _get_next_packet_size(cap) -> int:
    """IAudioCaptureClient::GetNextPacketSize — vtable[3]"""
    fn_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint))
    n = ctypes.c_uint(0)
    _hcheck(fn_type(_vtbl(cap, 3))(cap, ctypes.byref(n)), "GetNextPacketSize")
    return n.value


def _get_buffer(cap):
    """IAudioCaptureClient::GetBuffer — vtable[1]"""
    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
    )
    fn = fn_type(_vtbl(cap, 1))
    data = ctypes.c_void_p(); frames = ctypes.c_uint(); flags = ctypes.c_uint()
    dp = ctypes.c_ulonglong(); qp = ctypes.c_ulonglong()
    hr = fn(cap, ctypes.byref(data), ctypes.byref(frames),
            ctypes.byref(flags), ctypes.byref(dp), ctypes.byref(qp))
    _hcheck(hr, "GetBuffer")
    return data, frames.value, flags.value


def _release_buffer(cap, n: int):
    """IAudioCaptureClient::ReleaseBuffer — vtable[2]"""
    fn_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint)
    fn_type(_vtbl(cap, 2))(cap, n)


def _com_release(obj):
    if obj and obj.value:
        fn_type = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        fn_type(_vtbl(obj, 2))(obj)

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
# 캡처 세션 1회 실행
# ─────────────────────────────────────────────────────────────────────────────
def _run_capture_session(pid: int, audio_queue: Queue,
                          stop_flag: Value, send_log) -> "tuple[bool, str]":
    client  = None
    cap     = None
    h_event = None
    wfe_ref = None  # GC 방지

    try:
        # 1. ProcessLoopback IAudioClient 활성화 (OBS 동일 방식)
        client = _activate_process_loopback(pid)

        # 2. 포맷 결정: float32, 스테레오, 48000Hz 고정 (OBS 기본값)
        sr = 48000
        ch = 2
        wfe_ref = _audio_client_initialize(client, sr, ch)

        # 3. 이벤트 핸들 생성 & 등록
        h_event = _kernel32.CreateEventW(None, False, False, None)
        _audio_client_set_event(client, h_event)

        # 4. CaptureClient 획득 & 시작
        cap = _get_capture_client(client)
        _audio_client_start(client)

        sos, sosfilt = _make_bandpass(sr)
        send_log(f"🎙 [ProcessLoopback/WinAPI] PID={pid} sr={sr} ch={ch}")

        RECHECK    = 3.0
        last_check = time.time()
        cur_pid    = pid
        WAIT_MS    = 100  # 100ms 대기

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

            # 이벤트 대기
            ret = _kernel32.WaitForSingleObject(h_event, WAIT_MS)
            if ret != 0:  # WAIT_TIMEOUT or error
                continue

            # 패킷 수집
            while not stop_flag.value:
                pkt = _get_next_packet_size(cap)
                if pkt == 0:
                    break

                data, num_frames, flg = _get_buffer(cap)

                if num_frames > 0:
                    if flg & AUDCLNT_BUFFERFLAGS_SILENT:
                        rms = 0.0
                    else:
                        buf = (ctypes.c_float * (num_frames * ch)).from_address(data.value)
                        arr = np.frombuffer(buf, dtype=np.float32).copy()
                        if ch > 1:
                            arr = arr.reshape(-1, ch).mean(axis=1)
                        arr = _apply_filter(arr, sos, sosfilt)
                        rms = float(np.sqrt(np.mean(arr ** 2)))
                    queue_put(audio_queue, (time.time(), rms))

                _release_buffer(cap, num_frames)

        return True, ""

    except OSError as e:
        return False, str(e)
    finally:
        if cap:
            try: _audio_client_stop(client)
            except Exception: pass
            _com_release(cap)
        if client:
            _com_release(client)
        if h_event:
            _kernel32.CloseHandle(h_event)

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
