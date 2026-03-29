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
AUDCLNT_STREAMFLAGS_NOPERSIST     = 0x00080000
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

# IID_IMMDeviceEnumerator {A95664D2-9614-4F35-A746-DE8DB63617E6}
_IID_IMMDeviceEnumerator = (ctypes.c_byte * 16)(
    0xD2, 0x64, 0x56, 0xA9,
    0x14, 0x96,
    0x35, 0x4F,
    0xA7, 0x46, 0xDE, 0x8D, 0xB6, 0x36, 0x17, 0xE6,
)
# CLSID_MMDeviceEnumerator {BCDE0395-E52F-467C-8E3D-C4579291692E}
_CLSID_MMDeviceEnumerator = (ctypes.c_byte * 16)(
    0x95, 0x03, 0xDE, 0xBC,
    0x2F, 0xE5,
    0x7C, 0x46,
    0x8E, 0x3D, 0xC4, 0x57, 0x92, 0x91, 0x69, 0x2E,
)
# IID_IAudioClient (재사용)
# eRender=0, eConsole=0
_EDataFlow_eRender  = 0
_ERole_eConsole     = 0

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

    ActivateAudioInterfaceAsync 는 반드시 MTA(COINIT_MULTITHREADED) 스레드에서
    호출해야 한다. GUI(STA) 스레드에서 호출하면 ActivateCompleted 콜백이
    같은 스레드의 메시지 루프를 기다리면서 데드락된다.
    별도 daemon 스레드를 생성해 MTA 컨텍스트를 보장한다.
    """
    import threading

    result_box = [None, None]  # [client_or_None, exception_or_None]

    def _do_activate():
        COINIT_MULTITHREADED = 0x0
        hr_co = _ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        # S_OK(0)/S_FALSE(1) → OK, 0x80010106 → 이 스레드는 새로 생성했으므로 발생 안 함
        _co_initialized = (hr_co == 0)
        try:
            mmdevapi = ctypes.windll.LoadLibrary("Mmdevapi.dll")
            fn_activate = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_wchar_p,                # deviceInterfacePath
                ctypes.c_byte * 16,              # riid
                ctypes.POINTER(PROPVARIANT),     # activationParams
                ctypes.c_void_p,                 # completionHandler
                ctypes.POINTER(ctypes.c_void_p), # activationOperation
            )(("ActivateAudioInterfaceAsync", mmdevapi))

            act_params = AUDIOCLIENT_ACTIVATION_PARAMS()
            act_params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
            act_params.ProcessLoopbackParams.TargetProcessId     = pid
            act_params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE

            pv = PROPVARIANT()
            pv.vt = VT_BLOB
            pv.u.blob.cbSize    = ctypes.sizeof(act_params)
            pv.u.blob.pBlobData = ctypes.addressof(act_params)

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
                result_box[0] = handler.wait_and_get_client(timeout_ms=5000)
            except Exception as e:
                result_box[1] = e
            finally:
                handler.close()
        finally:
            if _co_initialized:
                _ole32.CoUninitialize()

    t = threading.Thread(target=_do_activate, daemon=True)
    t.start()
    t.join(timeout=8)
    if t.is_alive():
        raise OSError("ActivateAudioInterfaceAsync: MTA 스레드 타임아웃")
    if result_box[1] is not None:
        raise result_box[1]
    return result_box[0]


def _audio_client_get_mix_format(client) -> "tuple[WAVEFORMATEX, ctypes.c_void_p]":
    """IAudioClient::GetMixFormat — vtable[10] (IUnknown 3 + IAudioClient 메서드)"""
    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    fn = fn_type(_vtbl(client, 8))
    ptr = ctypes.c_void_p()
    _hcheck(fn(client, ctypes.byref(ptr)), "GetMixFormat")
    fmt = ctypes.cast(ptr, ctypes.POINTER(WAVEFORMATEX)).contents
    return fmt, ptr


def _get_render_mix_format() -> "tuple[ctypes.c_void_p, int, int]":
    """
    기본 렌더 디바이스(스피커)에서 믹스 포맷을 가져온다.
    ProcessLoopback IAudioClient 는 GetMixFormat 을 지원하지 않으므로(E_NOTIMPL),
    Microsoft 공식 샘플과 동일하게 렌더 디바이스 포맷을 사용한다.
    반환: (fmt_ptr, sample_rate, channels)  — 호출자가 CoTaskMemFree 해야 함
    """
    CLSCTX_ALL = 0x17

    # CoCreateInstance(CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator)
    enumerator = ctypes.c_void_p()
    hr = _ole32.CoCreateInstance(
        ctypes.byref(_CLSID_MMDeviceEnumerator),
        None, CLSCTX_ALL,
        ctypes.byref(_IID_IMMDeviceEnumerator),
        ctypes.byref(enumerator),
    )
    _hcheck(hr, "CoCreateInstance(MMDeviceEnumerator)")

    try:
        # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender, eConsole) — vtable[4]
        fn_gde = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p,
            ctypes.c_uint, ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p),
        )(_vtbl(enumerator, 4))
        device = ctypes.c_void_p()
        _hcheck(fn_gde(enumerator, _EDataFlow_eRender, _ERole_eConsole,
                       ctypes.byref(device)), "GetDefaultAudioEndpoint")

        try:
            # IMMDevice::Activate(IID_IAudioClient, CLSCTX_ALL, NULL, ppInterface) — vtable[3]
            fn_act = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p,
                ctypes.c_byte * 16, ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            )(_vtbl(device, 3))
            render_client = ctypes.c_void_p()
            _hcheck(fn_act(device, _IID_IAudioClient, CLSCTX_ALL,
                           None, ctypes.byref(render_client)),
                    "IMMDevice::Activate(IAudioClient)")

            try:
                # IAudioClient::GetMixFormat — vtable[8]
                fn_gmf = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p),
                )(_vtbl(render_client, 8))
                fmt_ptr = ctypes.c_void_p()
                _hcheck(fn_gmf(render_client, ctypes.byref(fmt_ptr)), "GetMixFormat")

                wfx = ctypes.cast(fmt_ptr, ctypes.POINTER(WAVEFORMATEX)).contents
                return fmt_ptr, int(wfx.nSamplesPerSec), int(wfx.nChannels)
            finally:
                _com_release(render_client)
        finally:
            _com_release(device)
    finally:
        _com_release(enumerator)


def _audio_client_initialize(client) -> "tuple[int, int]":
    """
    IAudioClient::Initialize — vtable[3]

    Microsoft ApplicationLoopback 공식 샘플 기준:
      1. 기본 렌더 디바이스에서 GetMixFormat 으로 포맷 획득
         (ProcessLoopback 클라이언트는 GetMixFormat 미지원 → E_NOTIMPL)
      2. Initialize(SHARED, LOOPBACK|EVENTCALLBACK, 1초, 0, pRenderMixFormat)
    반환: (sample_rate, channels)
    """
    REFTIMES_PER_SEC = 10_000_000  # 1초

    fmt_ptr, sr, ch = _get_render_mix_format()
    try:
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
        fn    = fn_type(_vtbl(client, 3))
        flags = AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
        hr    = fn(client, AUDCLNT_SHAREMODE_SHARED, flags,
                   REFTIMES_PER_SEC, 0, fmt_ptr, None)
        _hcheck(hr, "IAudioClient::Initialize")
    finally:
        _ole32.CoTaskMemFree(fmt_ptr)

    return sr, ch


def _audio_client_set_event(client, h_event):
    """IAudioClient::SetEventHandle — vtable[15]"""
    fn_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)
    fn = fn_type(_vtbl(client, 13))
    _hcheck(fn(client, h_event), "SetEventHandle")


def _audio_client_start(client):
    fn_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
    _hcheck(fn_type(_vtbl(client, 10))(client), "IAudioClient::Start")


def _audio_client_stop(client):
    fn_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
    fn_type(_vtbl(client, 11))(client)


def _get_capture_client(client) -> ctypes.c_void_p:
    """IAudioClient::GetService(IID_IAudioCaptureClient) — vtable[16]"""
    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p,
        ctypes.c_byte * 16,
        ctypes.POINTER(ctypes.c_void_p),
    )
    fn = fn_type(_vtbl(client, 14))
    cap = ctypes.c_void_p()
    _hcheck(fn(client, _IID_IAudioCaptureClient, ctypes.byref(cap)), "GetService(CaptureClient)")
    return cap


def _get_next_packet_size(cap) -> int:
    """IAudioCaptureClient::GetNextPacketSize — vtable[5]"""
    fn_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint))
    n = ctypes.c_uint(0)
    _hcheck(fn_type(_vtbl(cap, 5))(cap, ctypes.byref(n)), "GetNextPacketSize")
    return n.value


def _get_buffer(cap):
    """IAudioCaptureClient::GetBuffer — vtable[3]"""
    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
    )
    fn = fn_type(_vtbl(cap, 3))
    data = ctypes.c_void_p(); frames = ctypes.c_uint(); flags = ctypes.c_uint()
    dp = ctypes.c_ulonglong(); qp = ctypes.c_ulonglong()
    hr = fn(cap, ctypes.byref(data), ctypes.byref(frames),
            ctypes.byref(flags), ctypes.byref(dp), ctypes.byref(qp))
    _hcheck(hr, "GetBuffer")
    return data, frames.value, flags.value


def _release_buffer(cap, n: int):
    """IAudioCaptureClient::ReleaseBuffer — vtable[4]"""
    fn_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint)
    fn_type(_vtbl(cap, 4))(cap, n)


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

        # 2. Initialize (pFormat=NULL, Microsoft 공식 샘플 방식)
        sr, ch = _audio_client_initialize(client)
        wfe_ref = None

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
        # ProcessLoopback + EVENTCALLBACK 조합에서 이벤트가 오지 않는 경우가 있음.
        # 타임아웃(10ms) 후에도 GetNextPacketSize 로 직접 폴링하도록 한다.
        WAIT_MS    = 10

        def _drain_packets():
            """패킷 큐를 비울 때까지 읽어 RMS 를 audio_queue 에 넣는다."""
            while not stop_flag.value:
                try:
                    pkt = _get_next_packet_size(cap)
                except OSError:
                    break
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

            # 이벤트 대기 (타임아웃 시에도 폴링 — ProcessLoopback 이벤트 미발생 대비)
            _kernel32.WaitForSingleObject(h_event, WAIT_MS)
            _drain_packets()

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
        # wfe_ref: pFormat=NULL 방식 사용으로 해제 불필요

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
