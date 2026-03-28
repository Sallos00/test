"""

processes.py -- P1(화면캡처), P2(오디오캡처), P3(싱크분석) 프로세스

"""

import os

import json

import time

import ctypes

import ctypes.wintypes

import collections

import numpy as np

import psutil

from multiprocessing import Queue, Value

from win32_utils import (

    CFG, find_potplayer_hwnd, post_key_to_potplayer,

    queue_put, VK_OEM_PERIOD, VK_OEM_COMMA, VK_OEM_2,

    _user32, WM_USER, POT_SET_CURRENT_TIME,

)


# ── settings.json 직접 읽기 (폴백용) ──────────────────────────────────────────

def _load_saved_setting(key, default):

    try:

        path = os.path.join(os.environ.get("APPDATA", ""), "AutoSync", "settings.json")

        with open(path, "r") as f:

            return json.load(f).get(key, default)

    except Exception:

        return default


# P1: 화면 캡처 + 애니메이션 얼굴 감지 프로세스

def proc_lip_capture(lip_queue: Queue, stop_flag: Value, cfg: dict):

    import cv2

    import mss

    import sys

    if getattr(sys, 'frozen', False):

        base = sys._MEIPASS

    else:

        base = os.path.dirname(os.path.abspath(__file__))

    cascade_path = os.path.join(base, 'lbpcascade_animeface.xml')

    cascade = cv2.CascadeClassifier(cascade_path)

    sct = mss.mss()

    interval = 1.0 / cfg["CAPTURE_FPS"]

    DETECT_EVERY_N = 5

    prev = None

    last_roi = None

    frame_count = 0

    def get_potplayer_monitor():

        try:

            hwnd = find_potplayer_hwnd()

            if hwnd:

                rect = ctypes.wintypes.RECT()

                ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect))

                pt = ctypes.wintypes.POINT(0, 0)

                ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt))

                w = rect.right - rect.left

                h = rect.bottom - rect.top

                if w > 100 and h > 100:

                    # 상하좌우 10% 제외 (자막/레터박스/UI 제거)

                    margin_x = int(w * 0.10)

                    margin_y = int(h * 0.10)

                    return {

                        "left":   pt.x + margin_x,

                        "top":    pt.y + margin_y,

                        "width":  w - margin_x * 2,

                        "height": h - margin_y * 2,

                    }

        except Exception:

            pass

        return sct.monitors[1]

    capture_region   = get_potplayer_monitor()

    region_refresh_t = time.time()

    while not stop_flag.value:

        t0 = time.perf_counter()

        if time.time() - region_refresh_t > 5.0:

            capture_region   = get_potplayer_monitor()

            region_refresh_t = time.time()

        raw  = np.array(sct.grab(capture_region))

        gray = cv2.cvtColor(raw, cv2.COLOR_BGRA2GRAY)

        frame_count += 1

        motion = 0.0

        if frame_count % DETECT_EVERY_N == 1 or last_roi is None:

            faces = cascade.detectMultiScale(

                cv2.equalizeHist(gray),

                scaleFactor=1.1,

                minNeighbors=7,

                minSize=(60, 60),

            )

            if len(faces) > 0:

                x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])

                last_roi = (x, y, fw, fh)

            else:

                last_roi = None

                prev     = None

        if last_roi is not None:

            x, y, fw, fh = last_roi

            h_img, w_img  = gray.shape

            lip_y1 = min(y + int(fh * 0.6), h_img - 1)

            lip_y2 = min(y + fh, h_img)

            lip_x2 = min(x + fw, w_img)

            lip_roi = gray[lip_y1:lip_y2, x:lip_x2]

            if lip_roi.size > 0:

                small = cv2.resize(lip_roi, (64, 20))

                if prev is not None:

                    diff   = cv2.absdiff(small, prev)

                    motion = float(diff.mean())

                prev = small

        queue_put(lip_queue, (time.time(), motion))

        elapsed = time.perf_counter() - t0

        sleep_t = interval - elapsed

        if sleep_t > 0:

            time.sleep(sleep_t)


# P2: 팟플레이어 전용 오디오 캡처 프로세스

def proc_audio_capture(audio_queue: Queue, stop_flag: Value, cfg: dict):

    SR       = cfg["AUDIO_SR"]

    chunk_ms = 50

    CLSID_MMDeviceEnumerator  = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"

    IID_IAudioSessionManager2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"

    IID_IAudioSessionControl2 = "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"

    eRender = 0

    _pid_cache = [None, 0.0]   # [pid, last_check_time]

    def find_potplayer_pid():
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

    _com_meter = [None]

    _com_pid   = [None]

    def get_potplayer_rms():

        try:

            import comtypes

            import comtypes.client

            pot_pid = find_potplayer_pid()

            if pot_pid is None:

                _com_meter[0] = None

                return None, "PotPlayer PID 없음"

            if _com_meter[0] is not None and _com_pid[0] == pot_pid:

                peak = ctypes.c_float(0)

                try:

                    _com_meter[0]._comobj.GetPeakValue(ctypes.byref(peak))

                    return float(peak.value), ""

                except Exception:

                    _com_meter[0] = None

            comtypes.CoInitialize()

            enumerator = comtypes.CoCreateInstance(

                comtypes.GUID(CLSID_MMDeviceEnumerator),

                interface=comtypes.IUnknown,

                clsctx=comtypes.CLSCTX_ALL)

            ppDevice = comtypes.POINTER(comtypes.IUnknown)()

            hr = enumerator._comobj.GetDefaultAudioEndpoint(eRender, 1, ctypes.byref(ppDevice))

            if hr != 0: return None, "GetDefaultAudioEndpoint 실패"

            iid_asm2 = comtypes.GUID(IID_IAudioSessionManager2)

            ppMgr    = comtypes.POINTER(comtypes.IUnknown)()

            hr = ppDevice._comobj.Activate(ctypes.byref(iid_asm2), 23, None, ctypes.byref(ppMgr))

            if hr != 0: return None, "IAudioSessionManager2 Activate 실패"

            ppEnum = comtypes.POINTER(comtypes.IUnknown)()

            hr = ppMgr._comobj.GetSessionEnumerator(ctypes.byref(ppEnum))

            if hr != 0: return None, "GetSessionEnumerator 실패"

            count = ctypes.c_int(0)

            ppEnum._comobj.GetCount(ctypes.byref(count))

            iid_ctrl2 = comtypes.GUID(IID_IAudioSessionControl2)

            iid_meter = comtypes.GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")

            for i in range(count.value):

                ppSession = comtypes.POINTER(comtypes.IUnknown)()

                ppEnum._comobj.GetSession(i, ctypes.byref(ppSession))

                ppCtrl2 = comtypes.POINTER(comtypes.IUnknown)()

                if ppSession._comobj.QueryInterface(ctypes.byref(iid_ctrl2), ctypes.byref(ppCtrl2)) != 0:

                    continue

                pid = ctypes.c_uint(0)

                if ppCtrl2._comobj.GetProcessId(ctypes.byref(pid)) != 0:

                    continue

                if pid.value == pot_pid:

                    ppMeter = comtypes.POINTER(comtypes.IUnknown)()

                    hr = ppSession._comobj.QueryInterface(ctypes.byref(iid_meter), ctypes.byref(ppMeter))

                    if hr != 0: return None, "IAudioMeterInformation QI 실패"

                    _com_meter[0] = ppMeter

                    _com_pid[0]   = pot_pid

                    peak = ctypes.c_float(0)

                    ppMeter._comobj.GetPeakValue(ctypes.byref(peak))

                    return float(peak.value), ""

            return None, f"팟플레이어 세션 없음 ({count.value}개 중)"

        except Exception as e:

            _com_meter[0] = None

            return None, f"COM 예외: {e}"

    def _find_loopback_device(p, pot_pid, log_devices=False):
        """팟플레이어 전용 루프백 우선, 없으면 시스템 전체 루프백 반환.
        반환: (device_index, is_exclusive) or (None, False)

        탐색 우선순위:
          1) loopbackProcessId == pot_pid  (PID 직접 매칭)
          2) 장치 이름에 'potplayer' 포함  (PID를 채워주지 않는 환경 대응)
          3) loopbackProcessId == None 인 첫 번째 루프백  (시스템 전체)
        """
        POT_NAMES = {"potplayer", "potplayermini", "potplayermini64",
                     "pot player", "daumpotplayer"}

        by_pid      = None   # 우선순위 1
        by_name     = None   # 우선순위 2
        by_fallback = None   # 우선순위 3

        for i in range(p.get_device_count()):
            info     = p.get_device_info_by_index(i)
            if not info.get("isLoopbackDevice"):
                continue
            lpid     = info.get("loopbackProcessId")
            dev_name = info.get("name", "")

            if log_devices:
                queue_put(audio_queue, ("LOG",
                    f"🔍 루프백 장치 [{i}] loopbackProcessId={lpid!r} "
                    f"(type={type(lpid).__name__}) name={dev_name[:40]}"))

            # 우선순위 1: PID 직접 매칭
            try:
                lpid_int = int(lpid) if lpid is not None else None
            except Exception:
                lpid_int = None

            if pot_pid and lpid_int is not None and lpid_int == int(pot_pid):
                return i, True   # 가장 확실, 즉시 반환

            # 우선순위 2: 장치 이름에 potplayer 포함
            if by_name is None:
                if any(n in dev_name.lower() for n in POT_NAMES):
                    by_name = i

            # 우선순위 3: PID 없는 일반 루프백 (시스템 전체)
            if by_fallback is None and lpid_int is None:
                by_fallback = i

        if by_pid is not None:
            return by_pid, True
        if by_name is not None:
            return by_name, True
        return by_fallback, False

    def _open_stream(p, device_idx, sr, pyaudio):
        """주어진 장치로 스트림 열기. 반환: (stream, ch, native_sr, sos, sosfilt)"""
        dev_info  = p.get_device_info_by_index(device_idx)
        ch        = int(dev_info.get("maxInputChannels", 1)) or 1
        native_sr = int(dev_info.get("defaultSampleRate", sr))
        stream = p.open(
            format=pyaudio.paFloat32,
            channels=ch,
            rate=native_sr,
            input=True,
            input_device_index=device_idx,
            frames_per_buffer=int(native_sr * 0.05),
        )
        try:
            from scipy.signal import butter, sosfilt as _sosfilt
            sos = butter(4, [300, 3400], btype="bandpass", fs=native_sr, output="sos")
        except Exception:
            sos, _sosfilt = None, None
        return stream, ch, native_sr, sos, _sosfilt

    def capture_via_pyaudiowpatch():
        try:
            import pyaudiowpatch as pyaudio
        except Exception as e:
            return False, f"pyaudiowpatch import 실패: {e}"

        try:
            p = pyaudio.PyAudio()
        except Exception as e:
            return False, f"PyAudio 초기화 실패: {e}"

        # 초기 장치 선택
        pot_pid = find_potplayer_pid()
        dev_idx, is_excl = _find_loopback_device(p, pot_pid, log_devices=True)

        if dev_idx is None:
            p.terminate()
            return False, "loopback 장치 없음"

        label = "팟플레이어 전용" if is_excl else "시스템 전체"
        queue_put(audio_queue, ("LOG", f"🎙 루프백 연결: {label} ({dev_idx})"))

        try:
            stream, ch, native_sr, sos, sosfilt = _open_stream(p, dev_idx, SR, pyaudio)
        except Exception as e:
            p.terminate()
            return False, f"스트림 열기 실패: {e}"

        # 재연결 체크 주기 (초)
        RECHECK_INTERVAL = 3.0
        last_recheck = time.time()
        cur_excl     = is_excl
        cur_pid      = pot_pid

        while not stop_flag.value:
            # 팟플레이어 전용 루프백 재연결 체크
            now = time.time()
            if now - last_recheck >= RECHECK_INTERVAL:
                last_recheck = now
                new_pid = find_potplayer_pid()
                # 현재 시스템 전체 루프백이고, 팟플레이어 전용이 생겼으면 재연결
                if not cur_excl:
                    new_idx, new_excl = _find_loopback_device(p, new_pid)
                    if new_excl and new_idx is not None:
                        try:
                            stream.stop_stream()
                            stream.close()
                            stream, ch, native_sr, sos, sosfilt = _open_stream(p, new_idx, SR, pyaudio)
                            dev_idx  = new_idx
                            cur_excl = True
                            cur_pid  = new_pid
                            queue_put(audio_queue, ("LOG", f"🎙 루프백 전환: 시스템 전체 → 팟플레이어 전용 ({new_idx})"))
                        except Exception as e:
                            queue_put(audio_queue, ("LOG", f"⚠ 루프백 전환 실패: {e}"))
                # 현재 전용 루프백인데 팟플레이어 PID가 바뀌었거나 장치가 사라진 경우
                elif cur_excl and new_pid != cur_pid:
                    new_idx, new_excl = _find_loopback_device(p, new_pid)
                    fallback_label = "팟플레이어 전용" if new_excl else "시스템 전체"
                    if new_idx is not None:
                        try:
                            stream.stop_stream()
                            stream.close()
                            stream, ch, native_sr, sos, sosfilt = _open_stream(p, new_idx, SR, pyaudio)
                            dev_idx  = new_idx
                            cur_excl = new_excl
                            cur_pid  = new_pid
                            queue_put(audio_queue, ("LOG", f"🎙 루프백 재연결: {fallback_label} ({new_idx})"))
                        except Exception as e:
                            queue_put(audio_queue, ("LOG", f"⚠ 루프백 재연결 실패: {e}"))

            try:
                data = stream.read(int(native_sr * 0.05), exception_on_overflow=False)
            except Exception:
                # 스트림 오류 → 시스템 전체 루프백으로 폴백 재시도
                try: stream.stop_stream(); stream.close()
                except Exception: pass
                fallback_idx, _ = _find_loopback_device(p, None)
                if fallback_idx is not None:
                    try:
                        stream, ch, native_sr, sos, sosfilt = _open_stream(p, fallback_idx, SR, pyaudio)
                        cur_excl = False
                        queue_put(audio_queue, ("LOG", f"🎙 스트림 오류 → 시스템 전체 루프백으로 재연결 ({fallback_idx})"))
                        continue
                    except Exception:
                        pass
                time.sleep(0.1)
                continue

            arr = np.frombuffer(data, dtype=np.float32)
            if ch > 1:
                arr = arr.reshape(-1, ch).mean(axis=1)
            if sos is not None and sosfilt is not None:
                try:
                    arr = sosfilt(sos, arr)
                except Exception:
                    pass
            rms = float(np.sqrt(np.mean(arr ** 2)))
            queue_put(audio_queue, (time.time(), rms))

        try:
            stream.stop_stream()
            stream.close()
            p.terminate()
        except Exception:
            pass
        return True, ""

    # ── ActivateAudioInterfaceAsync 방식 (팟플레이어 PID 지정 캡처) ──────────
    def capture_via_activate_audio_interface():
        """
        Windows 10 build 20348+ / Windows 11 전용.
        ActivateAudioInterfaceAsync + AUDIOCLIENT_ACTIVATION_PARAMS 를 사용해
        팟플레이어 PID(및 자식 프로세스)의 오디오만 정확하게 캡처한다.
        pyaudiowpatch의 loopbackProcessId 문제와 무관하게 동작한다.

        구조:
          1) 팟플레이어 PID 확인
          2) AUDIOCLIENT_ACTIVATION_PARAMS 구조체 설정 (ProcessLoopback 모드)
          3) ActivateAudioInterfaceAsync 호출 → IAudioClient 획득
          4) IAudioClient.Initialize (공유모드, 이벤트 구동)
          5) IAudioCaptureClient 루프로 PCM 읽기 → RMS 계산 → queue_put
        """
        import threading as _threading

        # ── Windows API 상수 ────────────────────────────────────────────────
        AUDCLNT_SHAREMODE_SHARED          = 0
        AUDCLNT_STREAMFLAGS_LOOPBACK      = 0x00020000
        AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
        AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
        AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000

        # AUDIOCLIENT_ACTIVATION_TYPE
        AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1

        # PROCESS_LOOPBACK_MODE
        PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0

        # WAVEFORMATEX 태그
        WAVE_FORMAT_IEEE_FLOAT = 3

        # IID / CLSID (문자열 → GUID 변환은 ctypes로)
        IID_IAudioClient   = "{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}"
        IID_IAudioCaptureClient = "{C8ADBD64-E71E-48a0-A4DE-185C395CD317}"


        # ── 구조체 정의 ─────────────────────────────────────────────────────
        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [
                ("wFormatTag",      ctypes.c_ushort),
                ("nChannels",       ctypes.c_ushort),
                ("nSamplesPerSec",  ctypes.c_ulong),
                ("nAvgBytesPerSec", ctypes.c_ulong),
                ("nBlockAlign",     ctypes.c_ushort),
                ("wBitsPerSample",  ctypes.c_ushort),
                ("cbSize",          ctypes.c_ushort),
            ]

        class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
            _fields_ = [
                ("ActivationType",    ctypes.c_uint),   # AUDIOCLIENT_ACTIVATION_TYPE
                ("ProcessLoopbackMode", ctypes.c_uint), # PROCESS_LOOPBACK_MODE
                ("TargetProcessId",   ctypes.c_ulong),
            ]

        class PROPVARIANT(ctypes.Structure):
            # 최소한의 PROPVARIANT: vt + 8바이트 값
            class _U(ctypes.Union):
                _fields_ = [("blob_cbSize", ctypes.c_ulong),
                            ("blob_pBlobData", ctypes.c_void_p),
                            ("ptr",  ctypes.c_void_p),
                            ("pad",  ctypes.c_uint64)]
            _fields_ = [("vt",       ctypes.c_ushort),
                        ("wReserved1", ctypes.c_ushort),
                        ("wReserved2", ctypes.c_ushort),
                        ("wReserved3", ctypes.c_ushort),
                        ("u",        _U)]

        # ── COM 초기화 ──────────────────────────────────────────────────────
        ole32 = ctypes.windll.ole32
        try:
            ole32.CoInitializeEx(None, 0)  # COINIT_APARTMENTTHREADED
        except Exception:
            pass

        # ── 팟플레이어 PID 확인 ─────────────────────────────────────────────
        pot_pid = find_potplayer_pid()
        if pot_pid is None:
            return False, "팟플레이어 프로세스 없음"

        queue_put(audio_queue, ("LOG",
            f"🎙 ActivateAudioInterface 시도 (PID={pot_pid})"))

        try:
            # ── AUDIOCLIENT_ACTIVATION_PARAMS 설정 ─────────────────────────
            act_params                  = AUDIOCLIENT_ACTIVATION_PARAMS()
            act_params.ActivationType   = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
            act_params.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
            act_params.TargetProcessId  = pot_pid

            # PROPVARIANT 에 blob 포인터 박기 (vt=VT_BLOB=0x41)
            pv = PROPVARIANT()
            pv.vt = 0x41   # VT_BLOB
            pv.u.blob_cbSize    = ctypes.sizeof(act_params)
            pv.u.blob_pBlobData = ctypes.cast(
                ctypes.addressof(act_params), ctypes.c_void_p)

            # ── IActivateAudioInterfaceCompletionHandler 구현 ───────────────
            # COM 콜백을 Python에서 구현하기 위해 vtable 수동 구성
            activated_event = _threading.Event()
            result_iface    = [None]
            result_hr       = [0]

            CALLBACK_FUNCTYPE = ctypes.WINFUNCTYPE(
                ctypes.HRESULT,           # 반환
                ctypes.c_void_p,          # this
                ctypes.c_void_p,          # pActivateOperation
            )

            def _activate_completed(this, pOp):
                try:
                    # IActivateAudioInterfaceOperation::GetActivateResult
                    # vtable offset 3 (QueryInterface=0, AddRef=1, Release=2, GetActivateResult=3)
                    get_result = ctypes.WINFUNCTYPE(
                        ctypes.HRESULT,
                        ctypes.c_void_p,
                        ctypes.POINTER(ctypes.HRESULT),
                        ctypes.POINTER(ctypes.c_void_p),
                    )(ctypes.cast(pOp, ctypes.POINTER(ctypes.c_void_p))[0][3])
                    inner_hr  = ctypes.HRESULT(0)
                    inner_iface = ctypes.c_void_p(0)
                    get_result(pOp,
                               ctypes.byref(inner_hr),
                               ctypes.byref(inner_iface))
                    result_hr[0]    = inner_hr.value
                    result_iface[0] = inner_iface
                except Exception as e:
                    result_hr[0] = -1
                finally:
                    activated_event.set()
                return 0

            cb_func = CALLBACK_FUNCTYPE(_activate_completed)

            # vtable: QI, AddRef, Release, ActivateCompleted
            QI_FUNC      = ctypes.WINFUNCTYPE(ctypes.HRESULT,
                ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
            ULONG_FUNC   = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)

            def _qi(this, riid, ppv): return 0
            def _addref(this):        return 1
            def _release(this):       return 1

            qi_func   = QI_FUNC(_qi)
            ar_func   = ULONG_FUNC(_addref)
            rel_func  = ULONG_FUNC(_release)

            vtable_arr = (ctypes.c_void_p * 4)(
                ctypes.cast(qi_func,  ctypes.c_void_p).value,
                ctypes.cast(ar_func,  ctypes.c_void_p).value,
                ctypes.cast(rel_func, ctypes.c_void_p).value,
                ctypes.cast(cb_func,  ctypes.c_void_p).value,
            )
            vtable_ptr  = ctypes.cast(vtable_arr, ctypes.c_void_p)
            handler_obj = ctypes.c_void_p(ctypes.addressof(vtable_ptr))
            handler_ptr = ctypes.addressof(handler_obj)

            # ── ActivateAudioInterfaceAsync 호출 ───────────────────────────
            mmdevapi = ctypes.windll.mmdevapi
            iid_ac   = ctypes.create_unicode_buffer(IID_IAudioClient)

            # 장치 경로: 기본 렌더 장치 (DEVINTERFACE_AUDIO_RENDER)

            # Windows는 특수 경로 대신 빈 문자열로 기본 장치 선택 가능
            dev_path = ctypes.create_unicode_buffer(
                r"\\?\SWD#MMDEVAPI#{0.0.1.00000000}.{b3f8fa53-0004-438e-9003-51a46e139bfc}")

            IID_IAC_struct = (ctypes.c_byte * 16)()
            ole32.CLSIDFromString(iid_ac, IID_IAC_struct)

            op_ptr = ctypes.c_void_p(0)
            hr = mmdevapi.ActivateAudioInterfaceAsync(
                dev_path,
                IID_IAC_struct,
                ctypes.byref(pv),
                ctypes.c_void_p(handler_ptr),
                ctypes.byref(op_ptr),
            )
            if hr != 0:
                return False, f"ActivateAudioInterfaceAsync 실패: hr=0x{hr & 0xFFFFFFFF:08X}"

            # 콜백 완료 대기 (최대 5초)
            activated_event.wait(timeout=5.0)
            if not activated_event.is_set():
                return False, "ActivateAudioInterface 타임아웃"
            if result_hr[0] != 0:
                return False, f"GetActivateResult 실패: hr=0x{result_hr[0] & 0xFFFFFFFF:08X}"
            if result_iface[0] is None:
                return False, "IAudioClient 획득 실패"

            audio_client = result_iface[0]   # IAudioClient COM 포인터

            # ── IAudioClient vtable 헬퍼 ────────────────────────────────────
            def _vt(iface, idx):
                """iface의 vtable에서 idx번째 함수 포인터 반환."""
                vt = ctypes.cast(iface, ctypes.POINTER(ctypes.c_void_p))[0]
                return ctypes.cast(
                    ctypes.cast(vt, ctypes.POINTER(ctypes.c_void_p))[idx],
                    ctypes.c_void_p).value

            # IAudioClient::GetMixFormat (vtable idx 8)
            GetMixFormat = ctypes.WINFUNCTYPE(
                ctypes.HRESULT,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.POINTER(WAVEFORMATEX)),
            )(_vt(audio_client, 8))

            pwfx_ptr = ctypes.POINTER(WAVEFORMATEX)()
            hr = GetMixFormat(audio_client, ctypes.byref(pwfx_ptr))
            if hr != 0:
                return False, f"GetMixFormat 실패: hr=0x{hr & 0xFFFFFFFF:08X}"

            wfx        = pwfx_ptr.contents
            native_sr  = wfx.nSamplesPerSec
            ch         = wfx.nChannels
            bits       = wfx.wBitsPerSample

            # 캡처용 포맷: float32 강제
            wfx_cap               = WAVEFORMATEX()
            wfx_cap.wFormatTag    = WAVE_FORMAT_IEEE_FLOAT
            wfx_cap.nChannels     = min(ch, 2)
            wfx_cap.nSamplesPerSec = native_sr
            wfx_cap.wBitsPerSample = 32
            wfx_cap.nBlockAlign   = wfx_cap.nChannels * 4
            wfx_cap.nAvgBytesPerSec = wfx_cap.nSamplesPerSec * wfx_cap.nBlockAlign
            wfx_cap.cbSize        = 0

            # IAudioClient::Initialize (vtable idx 3)
            Initialize = ctypes.WINFUNCTYPE(
                ctypes.HRESULT,
                ctypes.c_void_p,   # this
                ctypes.c_uint,     # ShareMode
                ctypes.c_ulong,    # StreamFlags
                ctypes.c_longlong, # hnsBufferDuration (100ns 단위)
                ctypes.c_longlong, # hnsPeriodicity
                ctypes.c_void_p,   # pFormat
                ctypes.c_void_p,   # AudioSessionGuid
            )(_vt(audio_client, 3))

            BUFFER_DURATION = 10_000_000   # 1초 (100ns 단위)
            hr = Initialize(
                audio_client,
                AUDCLNT_SHAREMODE_SHARED,
                AUDCLNT_STREAMFLAGS_LOOPBACK |
                AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM |
                AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                BUFFER_DURATION,
                0,
                ctypes.addressof(wfx_cap),
                None,
            )
            if hr != 0:
                return False, f"IAudioClient::Initialize 실패: hr=0x{hr & 0xFFFFFFFF:08X}"

            # IAudioClient::GetService → IAudioCaptureClient (vtable idx 14)
            GetService = ctypes.WINFUNCTYPE(
                ctypes.HRESULT,
                ctypes.c_void_p,
                ctypes.c_void_p,   # riid
                ctypes.POINTER(ctypes.c_void_p),
            )(_vt(audio_client, 14))

            iid_cap_buf  = (ctypes.c_byte * 16)()
            iid_cap_str  = ctypes.create_unicode_buffer(IID_IAudioCaptureClient)
            ole32.CLSIDFromString(iid_cap_str, iid_cap_buf)

            cap_client = ctypes.c_void_p(0)
            hr = GetService(audio_client, iid_cap_buf, ctypes.byref(cap_client))
            if hr != 0:
                return False, f"GetService(IAudioCaptureClient) 실패: hr=0x{hr & 0xFFFFFFFF:08X}"

            # IAudioClient::Start (vtable idx 11)
            Start = ctypes.WINFUNCTYPE(
                ctypes.HRESULT, ctypes.c_void_p)(_vt(audio_client, 11))
            hr = Start(audio_client)
            if hr != 0:
                return False, f"IAudioClient::Start 실패: hr=0x{hr & 0xFFFFFFFF:08X}"

            queue_put(audio_queue, ("LOG",
                f"🎙 ProcessLoopback 연결 성공 (PID={pot_pid}, "
                f"sr={native_sr}, ch={wfx_cap.nChannels})"))

            # IAudioCaptureClient::GetBuffer  (vtable idx 3)
            # IAudioCaptureClient::ReleaseBuffer (vtable idx 4)
            # IAudioCaptureClient::GetNextPacketSize (vtable idx 5)
            GetBuffer = ctypes.WINFUNCTYPE(
                ctypes.HRESULT,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),   # ppData
                ctypes.POINTER(ctypes.c_ulong),    # pNumFramesAvailable
                ctypes.POINTER(ctypes.c_ulong),    # pdwFlags
                ctypes.POINTER(ctypes.c_ulonglong),# pu64DevicePosition
                ctypes.POINTER(ctypes.c_ulonglong),# pu64QPCPosition
            )(_vt(cap_client, 3))

            ReleaseBuffer = ctypes.WINFUNCTYPE(
                ctypes.HRESULT,
                ctypes.c_void_p,
                ctypes.c_ulong,
            )(_vt(cap_client, 4))

            GetNextPacketSize = ctypes.WINFUNCTYPE(
                ctypes.HRESULT,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_ulong),
            )(_vt(cap_client, 5))

            # 밴드패스 필터
            try:
                from scipy.signal import butter, sosfilt
                sos = butter(4, [300, 3400], btype="bandpass",
                             fs=native_sr, output="sos")
            except Exception:
                sos = None

            CHUNK_INTERVAL = chunk_ms / 1000.0
            _ch = wfx_cap.nChannels

            # PID 재확인 주기
            RECHECK_INTERVAL = 3.0
            last_recheck     = time.time()
            cur_pid          = pot_pid

            # ── 캡처 루프 ───────────────────────────────────────────────────
            while not stop_flag.value:
                now = time.time()

                # 3초마다 PID 재확인 (팟플레이어 재시작 감지)
                if now - last_recheck >= RECHECK_INTERVAL:
                    last_recheck = now
                    new_pid = find_potplayer_pid()
                    if new_pid != cur_pid:
                        # PID 바뀌면 재시작 (재귀 호출 대신 False 반환 → 상위에서 재시도)
                        queue_put(audio_queue, ("LOG",
                            f"🔄 팟플레이어 PID 변경 ({cur_pid}→{new_pid}) → 재연결"))
                        # Stop
                        Stop = ctypes.WINFUNCTYPE(
                            ctypes.HRESULT, ctypes.c_void_p)(_vt(audio_client, 12))
                        Stop(audio_client)
                        return False, "PID 변경으로 재연결 필요"

                pkt_size = ctypes.c_ulong(0)
                hr = GetNextPacketSize(cap_client, ctypes.byref(pkt_size))
                if hr != 0 or pkt_size.value == 0:
                    time.sleep(CHUNK_INTERVAL)
                    continue

                pData    = ctypes.c_void_p(0)
                nFrames  = ctypes.c_ulong(0)
                dwFlags  = ctypes.c_ulong(0)
                hr = GetBuffer(cap_client,
                               ctypes.byref(pData),
                               ctypes.byref(nFrames),
                               ctypes.byref(dwFlags),
                               None, None)
                if hr != 0:
                    time.sleep(CHUNK_INTERVAL)
                    continue

                n = nFrames.value
                if n > 0 and pData.value:
                    raw = (ctypes.c_float * (n * _ch)).from_address(pData.value)
                    arr = np.frombuffer(raw, dtype=np.float32).copy()
                    if _ch > 1:
                        arr = arr.reshape(-1, _ch).mean(axis=1)
                    if sos is not None:
                        try:
                            arr = sosfilt(sos, arr)
                        except Exception:
                            pass
                    rms = float(np.sqrt(np.mean(arr ** 2)))
                    queue_put(audio_queue, (time.time(), rms))

                ReleaseBuffer(cap_client, n)

            # 정리
            try:
                Stop = ctypes.WINFUNCTYPE(
                    ctypes.HRESULT, ctypes.c_void_p)(_vt(audio_client, 12))
                Stop(audio_client)
            except Exception:
                pass
            return True, ""

        except Exception as e:
            return False, f"ActivateAudioInterface 예외: {e}"

    # ── 캡처 시도 순서 ────────────────────────────────────────────────────────
    # 1) ActivateAudioInterfaceAsync (팟플레이어 PID 전용, 가장 정확)
    # 2) pyaudiowpatch (전용 루프백 or 시스템 전체 루프백)
    # 3) IAudioMeterInformation COM (피크값만, 최후 폴백)

    while not stop_flag.value:
        ok, reason = capture_via_activate_audio_interface()
        if ok:
            break
        queue_put(audio_queue, ("LOG", f"ActivateAudioInterface 실패: {reason}"))

        ok, reason = capture_via_pyaudiowpatch()
        if ok:
            break
        queue_put(audio_queue, ("LOG", f"pyaudiowpatch 실패: {reason}"))

        # 둘 다 실패 → IAudioMeter 폴백
        queue_put(audio_queue, ("LOG", "IAudioMeter 폴백 시작"))
        fallback_logged = False
        while not stop_flag.value:
            rms, err = get_potplayer_rms()
            if rms is not None:
                if not fallback_logged:
                    queue_put(audio_queue, ("LOG", "IAudioMeter 세션 캡처 시작"))
                    fallback_logged = True
                queue_put(audio_queue, (time.time(), rms))
            else:
                if not fallback_logged:
                    queue_put(audio_queue, ("LOG", f"IAudioMeter 실패: {err}"))
                    fallback_logged = True
                # 팟플레이어 없으면 잠깐 대기 후 1번부터 재시도
                if "PID 없음" in err:
                    break
            time.sleep(chunk_ms / 1000)




# P3: 싱크 분석 + 팟플레이어 보정 프로세스
# shared_pos, shared_dur: GUI 메인스레드가 1초마다 갱신하는 재생위치/전체길이 (ms)
# OP/ED 판단은 이 값을 읽어서 수행 → 별도 프로세스에서 SendMessageW 불필요

def proc_analyzer(lip_queue: Queue, audio_queue: Queue,
                  state_queue: Queue, cmd_queue: Queue,
                  stop_flag: Value, cfg: dict,
                  shared_pos=None, shared_dur=None):

    from scipy.signal import correlate

    # ── 싱크 분석 설정 ────────────────────────────────────────────────────────
    BUF_SEC      = cfg["BUFFER_SEC"]
    FPS          = cfg["CAPTURE_FPS"]
    THRESH       = cfg["SYNC_THRESHOLD_MS"]
    STEP         = cfg["POTPLAYER_STEP_MS"]
    MAX_STEPS    = cfg["MAX_CORRECT_STEP"]
    INTERVAL     = cfg["ANALYSIS_INTERVAL"]
    MAX_TOTAL_MS = cfg["MAX_TOTAL_SYNC_MS"]

    # ── OP/ED 설정 ────────────────────────────────────────────────────────────
    OPED_AUTO_SKIP = bool(cfg.get("OPED_AUTO_SKIP", False))
    OPED_SKIP_SEC  = int(cfg.get("OPED_SKIP_SEC", 90))
    OPED_ZONE_MS   = 180 * 1000   # 앞뒤 3분 구간 (ms)
    COOLDOWN_SEC   = 180          # 쿨다운 3분
    MUSIC_WINDOW   = 15.0         # 음악 판별에 사용할 오디오 버퍼 길이 (초)
    MUSIC_MIN_RMS  = 0.03
    MUSIC_MAX_CV   = 0.8
    MUSIC_MIN_FILL = 0.70
    MUSIC_CONFIRM  = 2            # 연속 N회 감지 시 동작

    # ── 버퍼 ─────────────────────────────────────────────────────────────────
    lip_buf   = collections.deque()
    aud_buf   = collections.deque()   # MUSIC_WINDOW 초치 보관
    total_ms  = 0
    log_lines = collections.deque(maxlen=100)
    prev_title = ""

    # ── OP/ED 상태 ────────────────────────────────────────────────────────────
    music_confirm = 0
    last_action_t = 0.0    # 마지막 스킵/닫기 시각 (쿨다운 기준)
    prompt_sent   = False  # 팝업이 이미 떠 있는 상태

    def add_log(msg):
        import time as _t
        log_lines.append(f"[{_t.strftime('%H:%M:%S')}] {msg}")

    # ── 음악 감지 ─────────────────────────────────────────────────────────────
    def is_music_playing():
        """최근 MUSIC_WINDOW 초 오디오 RMS로 음악 여부 판별."""
        if len(aud_buf) < 10:
            return False
        now    = time.time()
        cutoff = now - MUSIC_WINDOW
        vals   = [v for t, v in aud_buf if t >= cutoff]
        if len(vals) < 10:
            return False
        arr      = np.array(vals, dtype=np.float32)
        mean_rms = float(arr.mean())
        if mean_rms < MUSIC_MIN_RMS:
            return False
        cv   = float(arr.std() / mean_rms) if mean_rms > 1e-9 else 999.0
        fill = float((arr > 0.02).sum()) / len(arr)
        return cv < MUSIC_MAX_CV and fill > MUSIC_MIN_FILL

    # ── 큐 drain ─────────────────────────────────────────────────────────────
    def drain_queues():
        for q, buf, tag in [(lip_queue, lip_buf, "👁"), (audio_queue, aud_buf, "🔊")]:
            while True:
                try:
                    item = q.get_nowait()
                    if isinstance(item, tuple) and len(item) == 2 and item[0] == "LOG":
                        add_log(f"{tag} {item[1]}")
                    else:
                        buf.append(item)
                except Exception:
                    break
        now = time.time()
        while lip_buf and now - lip_buf[0][0] > BUF_SEC:
            lip_buf.popleft()
        while aud_buf and now - aud_buf[0][0] > MUSIC_WINDOW:
            aud_buf.popleft()

    # ── 싱크 관련 헬퍼 ───────────────────────────────────────────────────────
    def resample(tvs, fps=15):
        if len(tvs) < 2: return None
        ts = np.array([x[0] for x in tvs])
        vs = np.array([x[1] for x in tvs])
        t_grid = np.linspace(ts[-1] - BUF_SEC, ts[-1], int(BUF_SEC * fps))
        return np.interp(t_grid, ts, vs)

    def to_binary(signal, ratio=0.3):
        median = np.median(signal)
        thresh = median + ratio * signal.std()
        return (signal > thresh).astype(np.float32)

    def compute_offset(lip, aud):
        def norm(x):
            x = x - x.mean()
            s = x.std()
            return x / s if s > 1e-9 else x
        lip_bin = to_binary(lip, ratio=0.5)
        lip_sig = norm(lip_bin) if lip_bin.std() >= 1e-9 else norm(lip)
        aud_diff = np.abs(np.diff(aud, prepend=aud[0]))
        aud_bin  = to_binary(aud_diff, ratio=0.5)
        aud_sig  = norm(aud_bin) if aud_bin.std() >= 1e-9 else norm(aud_diff)
        corr = correlate(lip_sig, aud_sig, mode="full")
        lag  = np.argmax(corr) - (len(aud_sig) - 1)
        return lag / FPS * 1000, lip_bin.std(), aud_bin.std(), lip.mean(), aud.mean()

    _last_log_snapshot = [None]

    def push_state(status, offset, correction, logs, pot_ok, lip_n, aud_n,
                   notify=None, oped_prompt=None):
        # 로그가 바뀐 경우에만 list 변환 (직렬화 비용 절감)
        snap = _last_log_snapshot[0]
        if snap is None or len(snap) != len(logs) or (logs and snap[-1] != logs[-1]):
            snap = list(logs)
            _last_log_snapshot[0] = snap
        queue_put(state_queue, dict(
            status=status, offset_ms=offset, correction_ms=correction,
            log_lines=snap, potplayer_ok=pot_ok,
            lip_samples=lip_n, audio_samples=aud_n,
            notify=notify,
            oped_prompt=oped_prompt,
        ))

    # ── 스킵 실행 (PostMessageW 사용 — 프로세스 간 안전) ─────────────────────
    def execute_skip():
        """shared_pos 기준으로 OPED_SKIP_SEC 초 앞으로 이동."""
        hwnd = find_potplayer_hwnd()
        if not hwnd:
            add_log("⚠ 스킵 실패: 팟플레이어 미감지")
            return False
        try:
            pos = shared_pos.value if shared_pos else 0
            dur = shared_dur.value if shared_dur else 0
            if dur <= 0:
                add_log("⚠ 스킵 실패: 전체 길이 미확인")
                return False
            new_pos = min(pos + OPED_SKIP_SEC * 1000, dur - 2000)
            _user32.SendMessageW(hwnd, WM_USER, POT_SET_CURRENT_TIME, int(new_pos))
            def fmt(ms): s = ms // 1000; return f"{s // 60}:{s % 60:02d}"
            add_log(f"⏭ 스킵 ({OPED_SKIP_SEC}초): {fmt(pos)} → {fmt(new_pos)}")
            return True
        except Exception as e:
            add_log(f"⚠ 스킵 실패: {e}")
            return False

    time.sleep(BUF_SEC)

    audio_detected   = False
    audio_warn_shown = False
    diag_count       = 0

    # ── 메인 루프 ─────────────────────────────────────────────────────────────
    while not stop_flag.value:
        t0 = time.perf_counter()

        # ── 커맨드 처리 ──────────────────────────────────────────────────────
        while True:
            try:
                cmd = cmd_queue.get_nowait()
                if cmd == "reset":
                    hwnd = find_potplayer_hwnd()
                    if hwnd:
                        post_key_to_potplayer(hwnd, VK_OEM_2, shift=True)
                        time.sleep(0.05)
                        post_key_to_potplayer(hwnd, 0x6F, shift=True)
                        total_ms = 0
                        add_log("↺ 싱크 초기화")
                    else:
                        add_log("⚠ 팟플레이어 미감지")

                elif cmd == "oped_skip":
                    # 팝업에서 [스킵] 클릭
                    execute_skip()
                    music_confirm = 0
                    last_action_t = time.time()
                    prompt_sent   = False
                    add_log("⏭ 스킵 완료 → 쿨다운 3분")

                elif cmd == "oped_no_skip":
                    # 팝업 [닫기] 또는 10초 타임아웃
                    music_confirm = 0
                    last_action_t = time.time()
                    prompt_sent   = False
                    add_log("✖ 스킵 건너뜀 → 쿨다운 3분")

                elif cmd == "oped_reset":
                    # 초기화 버튼 → OP/ED 쿨다운·카운터 전부 초기화
                    music_confirm = 0
                    last_action_t = 0.0
                    prompt_sent   = False
                    add_log("↺ OP/ED 상태 초기화")

                elif cmd == "stop":
                    stop_flag.value = True
                    return

            except Exception:
                break

        drain_queues()

        hwnd   = find_potplayer_hwnd()
        pot_ok = bool(hwnd)
        lip_n  = len(lip_buf)
        aud_n  = len(aud_buf)

        # ── 영상 제목 변경 감지 → 상태 초기화 ───────────────────────────────
        if hwnd:
            try:
                tbuf = ctypes.create_unicode_buffer(512)
                _user32.GetWindowTextW(hwnd, tbuf, 512)
                cur_title = tbuf.value.strip()
                if cur_title and cur_title != prev_title and prev_title:
                    post_key_to_potplayer(hwnd, VK_OEM_2, shift=True)
                    time.sleep(0.05)
                    post_key_to_potplayer(hwnd, 0x6F, shift=True)
                    total_ms      = 0
                    lip_buf.clear()
                    aud_buf.clear()
                    music_confirm = 0
                    last_action_t = 0.0
                    prompt_sent   = False
                    add_log("🔄 영상 변경 → 싱크 + OP/ED 상태 초기화")
                prev_title = cur_title
            except Exception:
                pass

        if aud_n == 0 and lip_n > 10 and not audio_warn_shown:
            audio_warn_shown = True
            add_log("⚠ 오디오 미감지 — 팟플레이어 재생 중인지 확인하세요")

        notify      = None
        oped_prompt = None

        if not audio_detected and aud_n > 5:
            audio_detected = True
            notify = ("🎬 동영상 재생 감지",
                      "팟플레이어에서 동영상 재생이 감지되었습니다.\n싱크 분석을 시작합니다.")
            add_log("🎬 동영상 재생 감지")

        # ── OP/ED 감지 ────────────────────────────────────────────────────────
        #
        # 조건 체크 순서:
        #   1) shared_pos / shared_dur 유효한가?
        #   2) OP 구간 (pos < 3분) 또는 ED 구간 (pos > dur - 3분) 인가?
        #   3) 쿨다운(3분) 이 지났는가?
        #   4) is_music_playing() 인가?
        #   5) music_confirm >= MUSIC_CONFIRM(2) 인가?
        #      → ON:  즉시 스킵 + music_confirm 리셋 + 쿨다운 시작
        #      → OFF: 팝업 전송 (팝업 응답이 oped_skip / oped_no_skip 커맨드로 돌아옴)

        pos = shared_pos.value if shared_pos else -1
        dur = shared_dur.value if shared_dur else -1

        if pos >= 0 and dur > 0:
            in_op      = pos < OPED_ZONE_MS
            in_ed      = pos > (dur - OPED_ZONE_MS)
            in_zone    = in_op or in_ed
            zone_label = "오프닝" if in_op else "엔딩"
            cooled     = (time.time() - last_action_t) > COOLDOWN_SEC

            if in_zone and cooled:
                if is_music_playing():
                    music_confirm += 1
                    add_log(f"🎵 {zone_label} 음악 감지 ({music_confirm}/{MUSIC_CONFIRM}회)")

                    if music_confirm >= MUSIC_CONFIRM:
                        if OPED_AUTO_SKIP:
                            # ON → 즉시 스킵
                            if execute_skip():
                                music_confirm = 0
                                last_action_t = time.time()
                                add_log(f"⏭ {zone_label} 자동스킵 완료 → 쿨다운 3분")
                        else:
                            # OFF → 팝업 요청 (중복 방지)
                            if not prompt_sent:
                                oped_prompt = {"zone": zone_label, "skip_sec": OPED_SKIP_SEC}
                                prompt_sent = True
                                add_log(f"🎵 {zone_label} 팝업 전송")
                else:
                    if music_confirm > 0:
                        music_confirm -= 1
            elif in_zone and not cooled:
                remain = int(COOLDOWN_SEC - (time.time() - last_action_t))
                add_log(f"⏳ {zone_label} 쿨다운 {remain}초 남음")

        # ── 싱크 분석 (lip_n, aud_n 충분할 때만) ────────────────────────────
        if lip_n < 10 or aud_n < 10:
            push_state("데이터 수집 중", 0, total_ms, log_lines, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        lip_sig = resample(lip_buf, FPS)
        aud_sig = resample(aud_buf, FPS)

        if lip_sig is None or aud_sig is None:
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        n = min(len(lip_sig), len(aud_sig))
        offset_ms, _, _, lip_mean, aud_mean = compute_offset(lip_sig[-n:], aud_sig[-n:])

        if diag_count < 3:
            diag_count += 1
            add_log(f"📊 offset={offset_ms:.0f}ms lip={lip_mean:.3f} aud={aud_mean:.3f}")

        if lip_mean < 1e-6:
            push_state("미감지", 0, total_ms, log_lines, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(1.0)
            continue

        if abs(offset_ms) >= THRESH and hwnd:
            steps = min(int(abs(offset_ms) / STEP), MAX_STEPS)
            sign  = 1 if offset_ms > 0 else -1
            if abs(total_ms + steps * STEP * sign) > MAX_TOTAL_MS:
                allowed = MAX_TOTAL_MS - abs(total_ms)
                steps   = max(0, int(allowed / STEP))
            if steps == 0:
                add_log(f"⚠ 싱크 상한 도달 (±{MAX_TOTAL_MS}ms)")
                push_state("상한 도달", offset_ms, total_ms, log_lines, pot_ok,
                           lip_n, aud_n, notify, oped_prompt)
                time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
                continue
            direction = "빠르게" if offset_ms > 0 else "느리게"
            add_log(f"보정: {direction} ×{steps} ({steps * STEP}ms)")
            for _ in range(steps):
                vk = VK_OEM_PERIOD if offset_ms > 0 else VK_OEM_COMMA
                post_key_to_potplayer(hwnd, vk, shift=True)
                time.sleep(0.05)
            total_ms += steps * STEP * sign
            status = "보정 완료"
        elif not hwnd:
            status = "팟플레이어 미감지"
        else:
            status = "정상"

        push_state(status, offset_ms, total_ms, log_lines, pot_ok,
                   lip_n, aud_n, notify, oped_prompt)
        time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
