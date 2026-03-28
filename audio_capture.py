import time
import ctypes
import ctypes.wintypes
import numpy as np
import psutil
from multiprocessing import Queue, Value
from win32_utils import CFG, queue_put


def proc_audio_capture(audio_queue: Queue, stop_flag: Value, cfg: dict, log_queue=None):
    SR       = cfg["AUDIO_SR"]
    chunk_ms = 50

    # ── LOG 헬퍼: log_queue가 있으면 거기로, 없으면 audio_queue로 ──────────────
    def send_log(msg):
        if log_queue is not None:
            try:
                log_queue.put_nowait(msg)
            except Exception:
                pass
        else:
            queue_put(audio_queue, ("LOG", msg))

    # ── PID 캐시 ─────────────────────────────────────────────────────────────
    _pc = [None, 0.0]

    def find_potplayer_pid():
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

    # ── IAudioMeter COM 폴백 ─────────────────────────────────────────────────
    _cm = [None]
    _cp = [None]

    def get_potplayer_rms():
        try:
            import comtypes
            pp = find_potplayer_pid()
            if pp is None:
                _cm[0] = None
                return None, "PID없음"
            if _cm[0] is not None and _cp[0] == pp:
                pk = ctypes.c_float(0)
                try:
                    _cm[0]._comobj.GetPeakValue(ctypes.byref(pk))
                    return float(pk.value), ""
                except Exception:
                    _cm[0] = None
            comtypes.CoInitialize()
            en = comtypes.CoCreateInstance(
                comtypes.GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
                interface=comtypes.IUnknown,
                clsctx=comtypes.CLSCTX_ALL)
            pd = comtypes.POINTER(comtypes.IUnknown)()
            if en._comobj.GetDefaultAudioEndpoint(0, 1, ctypes.byref(pd)) != 0:
                return None, "GetDefaultAudioEndpoint 실패"
            pm = comtypes.POINTER(comtypes.IUnknown)()
            if pd._comobj.Activate(
                ctypes.byref(comtypes.GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")),
                    23, None, ctypes.byref(pm)) != 0:
                return None, "ASM2 Activate 실패"
            pe = comtypes.POINTER(comtypes.IUnknown)()
            if pm._comobj.GetSessionEnumerator(ctypes.byref(pe)) != 0:
                return None, "GetSessionEnumerator 실패"
            cnt = ctypes.c_int(0)
            pe._comobj.GetCount(ctypes.byref(cnt))
            g2 = comtypes.GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
            gm = comtypes.GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
            for i in range(cnt.value):
                ps = comtypes.POINTER(comtypes.IUnknown)()
                pe._comobj.GetSession(i, ctypes.byref(ps))
                pc = comtypes.POINTER(comtypes.IUnknown)()
                if ps._comobj.QueryInterface(ctypes.byref(g2), ctypes.byref(pc)) != 0:
                    continue
                pid = ctypes.c_uint(0)
                if pc._comobj.GetProcessId(ctypes.byref(pid)) != 0:
                    continue
                if pid.value == pp:
                    mt = comtypes.POINTER(comtypes.IUnknown)()
                    if ps._comobj.QueryInterface(ctypes.byref(gm), ctypes.byref(mt)) != 0:
                        return None, "IAudioMeter QI 실패"
                    _cm[0] = mt
                    _cp[0] = pp
                    pk = ctypes.c_float(0)
                    mt._comobj.GetPeakValue(ctypes.byref(pk))
                    return float(pk.value), ""
            return None, f"팟플레이어 세션 없음({cnt.value}개)"
        except Exception as e:
            _cm[0] = None
            return None, f"COM예외:{e}"

    # ── pyaudiowpatch 루프백 ─────────────────────────────────────────────────

    def find_loopback_device(p, pot_pid, log=False):
        """
        팟플레이어 전용 루프백 우선, 없으면 시스템 전체 루프백 반환.
        반환: (device_index, is_exclusive) or (None, False)
        """
        exclusive = None
        fallback  = None
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if not info.get("isLoopbackDevice"):
                continue
            raw_lpid = info.get("loopbackProcessId")
            # PID 타입 통일
            try:
                lpid = int(raw_lpid) if raw_lpid is not None else None
            except Exception:
                lpid = None
            if log:
                queue_put(audio_queue, ("LOG",
                    f"🔍 루프백[{i}] pid={lpid} potPID={pot_pid} "
                    f"name={info.get('name','')[:30]}"))
            # 팟플레이어 PID 일치 → 전용
            if pot_pid is not None and lpid is not None and lpid == pot_pid:
                exclusive = i
                break
            # PID 없음 → 시스템 전체 루프백 후보
            if fallback is None and lpid is None:
                fallback = i
        if exclusive is not None:
            return exclusive, True
        return fallback, False

    def open_stream(p, device_idx, pyaudio):
        """스트림 열기. 반환: (stream, ch, native_sr, sos, sosfilt)"""
        info      = p.get_device_info_by_index(device_idx)
        ch        = int(info.get("maxInputChannels", 1)) or 1
        native_sr = int(info.get("defaultSampleRate", SR))
        stream    = p.open(
            format=pyaudio.paFloat32,
            channels=ch,
            rate=native_sr,
            input=True,
            input_device_index=device_idx,
            frames_per_buffer=int(native_sr * 0.05),
        )
        try:
            from scipy.signal import butter, sosfilt as _sf
            sos = butter(4, [300, 3400], btype="bandpass", fs=native_sr, output="sos")
        except Exception:
            sos, _sf = None, None
        return stream, ch, native_sr, sos, _sf

    def capture_via_pyaudiowpatch():
        try:
            import pyaudiowpatch as pyaudio
        except Exception as e:
            return False, f"import 실패: {e}"
        try:
            p = pyaudio.PyAudio()
        except Exception as e:
            return False, f"PyAudio 초기화 실패: {e}"

        pot_pid           = find_potplayer_pid()
        dev_idx, is_excl  = find_loopback_device(p, pot_pid, log=True)

        if dev_idx is None:
            p.terminate()
            return False, "루프백 장치 없음"

        label = "팟플레이어 전용" if is_excl else "시스템 전체"
        send_log(f"🎙 루프백 연결: {label} (idx={dev_idx})")

        try:
            stream, ch, native_sr, sos, sosfilt = open_stream(p, dev_idx, pyaudio)
        except Exception as e:
            p.terminate()
            return False, f"스트림 열기 실패: {e}"

        RECHECK = 3.0
        last_check = time.time()
        cur_excl   = is_excl
        cur_pid    = pot_pid

        while not stop_flag.value:
            now = time.time()

            # ── 3초마다 재연결 체크 ───────────────────────────────────────
            if now - last_check >= RECHECK:
                last_check = now
                new_pid    = find_potplayer_pid()

                if not cur_excl:
                    # 시스템 전체 → 팟플레이어 전용으로 전환 가능한지 확인
                    new_idx, new_excl = find_loopback_device(p, new_pid)
                    if new_excl and new_idx is not None:
                        try:
                            stream.stop_stream()
                            stream.close()
                            stream, ch, native_sr, sos, sosfilt = open_stream(p, new_idx, pyaudio)
                            dev_idx  = new_idx
                            cur_excl = True
                            cur_pid  = new_pid
                            send_log(f"🎙 루프백 전환: 시스템 전체 → 팟플레이어 전용 (idx={new_idx})")
                        except Exception as e:
                            send_log(f"⚠ 루프백 전환 실패: {e}")

                elif cur_pid != new_pid:
                    # 팟플레이어 PID 변경 → 새 전용 루프백으로 재연결
                    new_idx, new_excl = find_loopback_device(p, new_pid)
                    if new_idx is not None:
                        try:
                            stream.stop_stream()
                            stream.close()
                            stream, ch, native_sr, sos, sosfilt = open_stream(p, new_idx, pyaudio)
                            dev_idx  = new_idx
                            cur_excl = new_excl
                            cur_pid  = new_pid
                            new_label = "팟플레이어 전용" if new_excl else "시스템 전체"
                            send_log(f"🎙 루프백 재연결: {new_label} (idx={new_idx})")
                        except Exception as e:
                            send_log(f"⚠ 루프백 재연결 실패: {e}")

            # ── 오디오 읽기 ───────────────────────────────────────────────
            try:
                data = stream.read(int(native_sr * 0.05), exception_on_overflow=False)
            except Exception:
                # 스트림 오류 → 시스템 전체 루프백으로 폴백
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
                fb_idx, _ = find_loopback_device(p, None)
                if fb_idx is not None:
                    try:
                        stream, ch, native_sr, sos, sosfilt = open_stream(p, fb_idx, pyaudio)
                        cur_excl = False
                        send_log(f"🎙 스트림 오류 → 시스템 전체 루프백 재연결 (idx={fb_idx})")
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
            queue_put(audio_queue, (time.time(), float(np.sqrt(np.mean(arr ** 2)))))

        try:
            stream.stop_stream()
            stream.close()
            p.terminate()
        except Exception:
            pass
        return True, ""

    # ── 메인 루프: pyaudiowpatch → IAudioMeter 폴백 ─────────────────────────
    while not stop_flag.value:
        try:
            ok, reason = capture_via_pyaudiowpatch()
        except Exception as e:
            ok, reason = False, f"예외: {e}"

        if ok:
            # 정상 종료(stop_flag) → 루프 탈출
            continue

        send_log(f"pyaudiowpatch 실패: {reason}")
        send_log("IAudioMeter 폴백 시작")

        fallback_logged = False
        while not stop_flag.value:
            rms, err = get_potplayer_rms()
            if rms is not None:
                if not fallback_logged:
                    send_log("IAudioMeter 세션 캡처 시작")
                    fallback_logged = True
                queue_put(audio_queue, (time.time(), rms))
            else:
                if not fallback_logged:
                    send_log(f"IAudioMeter 실패: {err}")
                    fallback_logged = True
                if "PID없음" in err:
                    break
            time.sleep(chunk_ms / 1000)

        time.sleep(1.0)
