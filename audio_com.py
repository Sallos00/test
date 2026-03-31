"""
audio_com.py -- Windows WASAPI ProcessLoopback COM/WinAPI 저수준 구현
audio_capture.py에서 분리된 COM 인터페이스 코드
"""
import ctypes
import ctypes.wintypes
import threading

_ole32    = ctypes.windll.ole32
_kernel32 = ctypes.windll.kernel32

# ── WASAPI 상수 ──────────────────────────────────────────────────────────────
AUDCLNT_STREAMFLAGS_LOOPBACK      = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_NOPERSIST     = 0x00080000
AUDCLNT_SHAREMODE_SHARED          = 0
AUDCLNT_BUFFERFLAGS_SILENT        = 0x2
AUDCLNT_S_BUFFER_EMPTY            = 0x08890001
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

# ── COM IID/CLSID ─────────────────────────────────────────────────────────────
_IID_IAudioClient = (ctypes.c_byte * 16)(
    0x4C, 0xAD, 0xB9, 0x1C, 0xFA, 0xDB, 0x32, 0x4C,
    0xB1, 0x78, 0xC2, 0xF5, 0x68, 0xA7, 0x03, 0xB2,
)
_IID_IAudioCaptureClient = (ctypes.c_byte * 16)(
    0x64, 0xBD, 0xAD, 0xC8, 0x1E, 0xE7, 0xA0, 0x48,
    0xA4, 0xDE, 0x18, 0x5C, 0x39, 0x5C, 0xD3, 0x17,
)
_IID_IMMDeviceEnumerator = (ctypes.c_byte * 16)(
    0xD2, 0x64, 0x56, 0xA9, 0x14, 0x96, 0x35, 0x4F,
    0xA7, 0x46, 0xDE, 0x8D, 0xB6, 0x36, 0x17, 0xE6,
)
_CLSID_MMDeviceEnumerator = (ctypes.c_byte * 16)(
    0x95, 0x03, 0xDE, 0xBC, 0x2F, 0xE5, 0x7C, 0x46,
    0x8E, 0x3D, 0xC4, 0x57, 0x92, 0x91, 0x69, 0x2E,
)
_IID_IActivateAudioInterfaceCompletionHandler = (ctypes.c_byte * 16)(
    0x94, 0x49, 0xD9, 0x41, 0xAA, 0x97, 0x40, 0x9A,
    0xAB, 0x02, 0xE0, 0xD1, 0x71, 0x10, 0xA9, 0xC4,
)
_EDataFlow_eRender = 0
_ERole_eConsole    = 0

# ── COM 구조체 ────────────────────────────────────────────────────────────────
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

class PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId",     ctypes.c_ulong),
        ("ProcessLoopbackMode", ctypes.c_uint),
    ]

class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType",        ctypes.c_uint),
        ("ProcessLoopbackParams", PROCESS_LOOPBACK_PARAMS),
    ]

VT_BLOB = 0x41

class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("pBlobData", ctypes.c_void_p)]

class PROPVARIANT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("blob", BLOB), ("raw", ctypes.c_byte * 16)]
    _fields_ = [
        ("vt",        ctypes.c_ushort),
        ("reserved1", ctypes.c_ushort),
        ("reserved2", ctypes.c_ushort),
        ("reserved3", ctypes.c_ushort),
        ("u",         _U),
    ]

# ── vtable 헬퍼 ───────────────────────────────────────────────────────────────
def _vtbl(obj, index):
    vt = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
    return ctypes.cast(vt, ctypes.POINTER(ctypes.c_void_p))[index]

def _hcheck(hr, label=""):
    if hr < 0:
        raise OSError(f"{label} HRESULT=0x{hr & 0xFFFFFFFF:08X}")

def _com_release(obj):
    if obj and obj.value:
        fn_type = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        fn_type(_vtbl(obj, 2))(obj)

# ── 콜백 함수 타입 ─────────────────────────────────────────────────────────────
_ACTIVATE_COMPLETED_FN = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)
_ADDREF_FN  = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_RELEASE_FN = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_QI_FN      = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_void_p,
    ctypes.c_byte * 16, ctypes.POINTER(ctypes.c_void_p))


class _CompletionHandlerImpl:
    """IActivateAudioInterfaceCompletionHandler 순수 ctypes vtable 구현."""
    def __init__(self):
        self.event   = _kernel32.CreateEventW(None, True, False, None)
        self.hr_act  = ctypes.c_long(0)
        self.op_ptr  = ctypes.c_void_p(None)

        def _qi(this, riid, ppv):
            ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p))[0] = this
            return 0
        def _addref(this):  return 1
        def _release(this): return 1
        def _activate_completed(this, pOp):
            self.op_ptr.value = pOp
            fn_type = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_long),
                ctypes.POINTER(ctypes.c_void_p))
            fn    = fn_type(_vtbl(ctypes.c_void_p(pOp), 3))
            inner = ctypes.c_void_p()
            fn(pOp, ctypes.byref(self.hr_act), ctypes.byref(inner))
            self.audio_client = inner
            _kernel32.SetEvent(self.event)
            return 0

        self._qi_fn        = _QI_FN(_qi)
        self._addref_fn    = _ADDREF_FN(_addref)
        self._release_fn   = _RELEASE_FN(_release)
        self._completed_fn = _ACTIVATE_COMPLETED_FN(_activate_completed)

        self._vtable = (ctypes.c_void_p * 4)(
            ctypes.cast(self._qi_fn,        ctypes.c_void_p).value,
            ctypes.cast(self._addref_fn,    ctypes.c_void_p).value,
            ctypes.cast(self._release_fn,   ctypes.c_void_p).value,
            ctypes.cast(self._completed_fn, ctypes.c_void_p).value,
        )
        self._vtable_ptr = ctypes.cast(self._vtable, ctypes.c_void_p)
        self._obj_data   = ctypes.c_void_p(self._vtable_ptr.value)
        self.com_ptr     = ctypes.addressof(self._obj_data)
        self.audio_client = ctypes.c_void_p()

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


def activate_process_loopback(pid: int) -> ctypes.c_void_p:
    """ProcessLoopback IAudioClient 활성화 (MTA 스레드에서 실행)."""
    result_box = [None, None]

    def _do_activate():
        hr_co = _ole32.CoInitializeEx(None, 0x0)
        try:
            mmdevapi   = ctypes.windll.LoadLibrary("Mmdevapi.dll")
            fn_activate = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_wchar_p,
                ctypes.c_byte * 16,
                ctypes.POINTER(PROPVARIANT),
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            )(("ActivateAudioInterfaceAsync", mmdevapi))

            act_params = AUDIOCLIENT_ACTIVATION_PARAMS()
            act_params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
            act_params.ProcessLoopbackParams.TargetProcessId     = pid
            act_params.ProcessLoopbackParams.ProcessLoopbackMode = \
                PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE

            pv = PROPVARIANT()
            pv.vt = VT_BLOB
            pv.u.blob.cbSize    = ctypes.sizeof(act_params)
            pv.u.blob.pBlobData = ctypes.addressof(act_params)

            handler  = _CompletionHandlerImpl()
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
        except Exception as e:
            result_box[1] = e

    t = threading.Thread(target=_do_activate, daemon=True)
    t.start()
    t.join(timeout=8)
    if t.is_alive():
        raise OSError("ActivateAudioInterfaceAsync: MTA 스레드 타임아웃")
    if result_box[1] is not None:
        raise result_box[1]
    return result_box[0]


def get_render_mix_format():
    """기본 렌더 디바이스에서 믹스 포맷 획득."""
    CLSCTX_ALL = 0x17
    enumerator = ctypes.c_void_p()
    hr = _ole32.CoCreateInstance(
        ctypes.byref(_CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
        ctypes.byref(_IID_IMMDeviceEnumerator), ctypes.byref(enumerator))
    _hcheck(hr, "CoCreateInstance(MMDeviceEnumerator)")

    try:
        fn_gde = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p,
            ctypes.c_uint, ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p))(_vtbl(enumerator, 4))
        device = ctypes.c_void_p()
        _hcheck(fn_gde(enumerator, _EDataFlow_eRender, _ERole_eConsole,
                       ctypes.byref(device)), "GetDefaultAudioEndpoint")

        try:
            fn_act = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p,
                ctypes.c_byte * 16, ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p))(_vtbl(device, 3))
            render_client = ctypes.c_void_p()
            _hcheck(fn_act(device, _IID_IAudioClient, CLSCTX_ALL,
                           None, ctypes.byref(render_client)),
                    "IMMDevice::Activate(IAudioClient)")

            try:
                fn_gmf = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p))(_vtbl(render_client, 8))
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


def audio_client_initialize(client):
    """IAudioClient::Initialize — vtable[3]. 반환: (sample_rate, channels)"""
    REFTIMES_PER_SEC = 10_000_000
    fmt_ptr, sr, ch = get_render_mix_format()
    try:
        fn_type = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
            ctypes.c_longlong, ctypes.c_longlong, ctypes.c_void_p, ctypes.c_void_p)
        fn    = fn_type(_vtbl(client, 3))
        flags = AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
        hr    = fn(client, AUDCLNT_SHAREMODE_SHARED, flags,
                   REFTIMES_PER_SEC, 0, fmt_ptr, None)
        _hcheck(hr, "IAudioClient::Initialize")
    finally:
        _ole32.CoTaskMemFree(fmt_ptr)
    return sr, ch


def audio_client_set_event(client, h_event):
    fn_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)
    _hcheck(fn_type(_vtbl(client, 13))(client, h_event), "SetEventHandle")

def audio_client_start(client):
    fn_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
    _hcheck(fn_type(_vtbl(client, 10))(client), "IAudioClient::Start")

def audio_client_stop(client):
    fn_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
    fn_type(_vtbl(client, 11))(client)

def get_capture_client(client) -> ctypes.c_void_p:
    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p,
        ctypes.c_byte * 16, ctypes.POINTER(ctypes.c_void_p))
    fn = fn_type(_vtbl(client, 14))
    cap = ctypes.c_void_p()
    _hcheck(fn(client, _IID_IAudioCaptureClient, ctypes.byref(cap)),
            "GetService(CaptureClient)")
    return cap

def get_next_packet_size(cap) -> int:
    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint))
    n = ctypes.c_uint(0)
    _hcheck(fn_type(_vtbl(cap, 5))(cap, ctypes.byref(n)), "GetNextPacketSize")
    return n.value

def get_buffer(cap):
    fn_type = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong))
    fn = fn_type(_vtbl(cap, 3))
    data = ctypes.c_void_p(); frames = ctypes.c_uint(); flags = ctypes.c_uint()
    dp = ctypes.c_ulonglong(); qp = ctypes.c_ulonglong()
    hr = fn(cap, ctypes.byref(data), ctypes.byref(frames),
            ctypes.byref(flags), ctypes.byref(dp), ctypes.byref(qp))
    _hcheck(hr, "GetBuffer")
    return data, frames.value, flags.value

def release_buffer(cap, n: int):
    fn_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint)
    fn_type(_vtbl(cap, 4))(cap, n)


# ── 전체 루프백 활성화 (빌드 19041 미만 폴백) ─────────────────────────────────
def activate_global_loopback() -> ctypes.c_void_p:
    """
    기본 렌더 디바이스의 loopback IAudioClient를 반환.
    ProcessLoopback 미지원 환경(빌드 < 19041)에서 폴백으로 사용.
    팟플레이어뿐 아니라 시스템 전체 오디오가 캡처된다.
    """
    CLSCTX_ALL = 0x17
    enumerator = ctypes.c_void_p()
    hr = _ole32.CoCreateInstance(
        ctypes.byref(_CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
        ctypes.byref(_IID_IMMDeviceEnumerator), ctypes.byref(enumerator))
    _hcheck(hr, "CoCreateInstance(MMDeviceEnumerator)")

    try:
        fn_gde = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p,
            ctypes.c_uint, ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p))(_vtbl(enumerator, 4))
        device = ctypes.c_void_p()
        _hcheck(fn_gde(enumerator, _EDataFlow_eRender, _ERole_eConsole,
                       ctypes.byref(device)), "GetDefaultAudioEndpoint")
        try:
            fn_act = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p,
                ctypes.c_byte * 16, ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p))(_vtbl(device, 3))
            client = ctypes.c_void_p()
            _hcheck(fn_act(device, _IID_IAudioClient, CLSCTX_ALL,
                           None, ctypes.byref(client)),
                    "IMMDevice::Activate(IAudioClient)")
            return client
        finally:
            _com_release(device)
    finally:
        _com_release(enumerator)


def audio_client_initialize_loopback(client) -> tuple:
    """
    전체 루프백용 IAudioClient::Initialize.
    AUDCLNT_STREAMFLAGS_LOOPBACK 플래그만 사용 (ProcessLoopback 파라미터 없음).
    반환: (sample_rate, channels)
    """
    REFTIMES_PER_SEC = 10_000_000
    fmt_ptr, sr, ch = get_render_mix_format()
    try:
        fn_type = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
            ctypes.c_longlong, ctypes.c_longlong, ctypes.c_void_p, ctypes.c_void_p)
        fn    = fn_type(_vtbl(client, 3))
        flags = AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
        hr    = fn(client, AUDCLNT_SHAREMODE_SHARED, flags,
                   REFTIMES_PER_SEC, 0, fmt_ptr, None)
        _hcheck(hr, "IAudioClient::Initialize(loopback)")
    finally:
        _ole32.CoTaskMemFree(fmt_ptr)
    return sr, ch
