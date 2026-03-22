"""
processes.py -- P1(화면캡처), P2(오디오캡처), P3(싱크분석) 프로세스
"""
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
    _user32
)


# P1: 화면 캡처 + 애니메이션 얼굴 감지 프로세스
def proc_lip_capture(lip_queue: Queue, stop_flag: Value, cfg: dict):
    import cv2
    import mss
    import os, sys

    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    cascade_path = os.path.join(base, 'lbpcascade_animeface.xml')

    cascade = cv2.CascadeClassifier(cascade_path)
    sct      = mss.mss()
    interval = 1.0 / cfg["CAPTURE_FPS"]

    DETECT_EVERY_N = 5
    prev        = None
    last_roi    = None
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
                    return {"left": pt.x, "top": pt.y, "width": w, "height": h}
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
                minNeighbors=5,
                minSize=(60, 60),
            )
            if len(faces) > 0:
                x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                last_roi = (x, y, fw, fh)
            else:
                last_roi = None
                prev = None

        if last_roi is not None:
            x, y, fw, fh = last_roi
            h_img, w_img = gray.shape
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

    def find_potplayer_pid():
        for p in psutil.process_iter(["pid", "name"]):
            n = p.info["name"].lower()
            if "potplayer" in n or "pot player" in n:
                return p.info["pid"]
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
            ppMgr = comtypes.POINTER(comtypes.IUnknown)()
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

    def capture_via_pyaudiowpatch():
        try:
            import pyaudiowpatch as pyaudio

            p = pyaudio.PyAudio()
            pot_pid = find_potplayer_pid()
            target_device  = None
            fallback_device = None

            for i in range(p.get_device_count()):
                info = p.get_device_info_by_index(i)
                if not info.get("isLoopbackDevice"):
                    continue
                if pot_pid and info.get("loopbackProcessId") == pot_pid:
                    target_device = i
                    break
                if fallback_device is None and info.get("loopbackProcessId") is None:
                    fallback_device = i

            if target_device is None:
                target_device = fallback_device
            if target_device is None:
                p.terminate()
                return False, "loopback 장치 없음"

            dev_info  = p.get_device_info_by_index(target_device)
            ch        = int(dev_info.get("maxInputChannels", 1)) or 1
            native_sr = int(dev_info.get("defaultSampleRate", SR))

            stream = p.open(
                format=pyaudio.paFloat32,
                channels=ch,
                rate=native_sr,
                input=True,
                input_device_index=target_device,
                frames_per_buffer=int(native_sr * 0.05),
            )

            while not stop_flag.value:
                data = stream.read(int(native_sr * 0.05), exception_on_overflow=False)
                arr  = np.frombuffer(data, dtype=np.float32)
                if ch > 1:
                    arr = arr.reshape(-1, ch).mean(axis=1)
                rms = float(np.sqrt(np.mean(arr ** 2)))
                queue_put(audio_queue, (time.time(), rms))

            stream.stop_stream()
            stream.close()
            p.terminate()
            return True, ""

        except Exception as e:
            return False, f"pyaudiowpatch 예외: {e}"

    ok, reason = capture_via_pyaudiowpatch()
    if not ok:
        queue_put(audio_queue, ("LOG", f"pyaudiowpatch 실패: {reason}"))
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
            time.sleep(chunk_ms / 1000)


# P3: 싱크 분석 + 팟플레이어 보정 프로세스
def proc_analyzer(lip_queue: Queue, audio_queue: Queue,
                  state_queue: Queue, cmd_queue: Queue,
                  stop_flag: Value, cfg: dict):
    from scipy.signal import correlate

    BUF_SEC      = cfg["BUFFER_SEC"]
    FPS          = cfg["CAPTURE_FPS"]
    THRESH       = cfg["SYNC_THRESHOLD_MS"]
    STEP         = cfg["POTPLAYER_STEP_MS"]
    MAX_STEPS    = cfg["MAX_CORRECT_STEP"]
    INTERVAL     = cfg["ANALYSIS_INTERVAL"]
    MAX_TOTAL_MS = cfg["MAX_TOTAL_SYNC_MS"]

    lip_buf    = collections.deque()
    aud_buf    = collections.deque()
    total_ms   = 0
    log_lines  = collections.deque(maxlen=100)
    prev_title = ""

    def add_log(msg):
        import time as _t
        log_lines.append(f"[{_t.strftime('%H:%M:%S')}] {msg}")

    def drain_queues():
        while True:
            try:
                item = lip_queue.get_nowait()
                if isinstance(item, tuple) and len(item) == 2 and item[0] == "LOG":
                    add_log(f"👁 {item[1]}")
                else:
                    lip_buf.append(item)
            except Exception:
                break
        while True:
            try:
                item = audio_queue.get_nowait()
                if isinstance(item, tuple) and len(item) == 2 and item[0] == "LOG":
                    add_log(f"🔊 {item[1]}")
                else:
                    aud_buf.append(item)
            except Exception:
                break
        now = time.time()
        while lip_buf and now - lip_buf[0][0] > BUF_SEC:
            lip_buf.popleft()
        while aud_buf and now - aud_buf[0][0] > BUF_SEC:
            aud_buf.popleft()

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

    def push_state(status, offset, correction, logs, pot_ok, lip_n, aud_n, notify=None):
        queue_put(state_queue, dict(
            status=status, offset_ms=offset, correction_ms=correction,
            log_lines=list(logs), potplayer_ok=pot_ok,
            lip_samples=lip_n, audio_samples=aud_n, notify=notify
        ))

    time.sleep(BUF_SEC)

    audio_detected   = False
    audio_warn_shown = False
    diag_count       = 0

    while not stop_flag.value:
        t0 = time.perf_counter()

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

        if hwnd:
            try:
                tbuf = ctypes.create_unicode_buffer(512)
                _user32.GetWindowTextW(hwnd, tbuf, 512)
                cur_title = tbuf.value.strip()
                if cur_title and cur_title != prev_title and prev_title:
                    post_key_to_potplayer(hwnd, VK_OEM_2, shift=True)
                    time.sleep(0.05)
                    post_key_to_potplayer(hwnd, 0x6F, shift=True)
                    total_ms = 0
                    lip_buf.clear()
                    aud_buf.clear()
                    add_log("🔄 영상 변경 감지 → 싱크 초기화")
                prev_title = cur_title
            except Exception:
                pass

        if aud_n == 0 and lip_n > 10 and not audio_warn_shown:
            audio_warn_shown = True
            add_log("⚠ 오디오 미감지 — 팟플레이어가 소리를 재생 중인지 확인하세요")

        notify = None
        if not audio_detected and aud_n > 5:
            audio_detected = True
            notify = ("🎬 동영상 재생 감지",
                      "팟플레이어에서 동영상 재생이 감지되었습니다.\n싱크 분석을 시작합니다.")
            add_log("🎬 동영상 재생 감지")

        if lip_n < 10 or aud_n < 10:
            push_state("데이터 수집 중", 0, total_ms, log_lines, pot_ok, lip_n, aud_n, notify)
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
            add_log(f"📊 offset={offset_ms:.0f}ms lip_mean={lip_mean:.3f} aud_mean={aud_mean:.3f}")

        if lip_mean < 1e-6:
            push_state("미감지", 0, total_ms, log_lines, pot_ok, lip_n, aud_n, notify)
            time.sleep(1.0)
            continue

        if abs(offset_ms) >= THRESH and hwnd:
            steps = min(int(abs(offset_ms) / STEP), MAX_STEPS)
            sign  = 1 if offset_ms > 0 else -1
            if abs(total_ms + steps * STEP * sign) > MAX_TOTAL_MS:
                allowed = MAX_TOTAL_MS - abs(total_ms)
                steps   = max(0, int(allowed / STEP))
            if steps == 0:
                add_log(f"⚠ 영상 싱크 상한 도달 (±{MAX_TOTAL_MS}ms)")
                push_state("상한 도달", offset_ms, total_ms, log_lines, pot_ok, lip_n, aud_n, notify)
                time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
                continue
            direction = "빠르게" if offset_ms > 0 else "느리게"
            add_log(f"보정: {direction} ×{steps} ({steps*STEP}ms)")
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

        push_state(status, offset_ms, total_ms, log_lines, pot_ok, lip_n, aud_n, notify)
        time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
