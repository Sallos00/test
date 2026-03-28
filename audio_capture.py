import time
import ctypes
import ctypes.wintypes
import numpy as np
import psutil
from multiprocessing import Queue, Value
from win32_utils import CFG, queue_put

def proc_audio_capture(audio_queue: Queue, stop_flag: Value, cfg: dict):
    try:
        _proc_audio_capture_inner(audio_queue, stop_flag, cfg)
    except Exception as e:
        try:
            from win32_utils import queue_put as _qp
            _qp(audio_queue, ("LOG", f"⚠ proc_audio_capture 크래시: {e}"))
        except Exception:
            pass

def _proc_audio_capture_inner(audio_queue: Queue, stop_flag: Value, cfg: dict):
    SR       = cfg["AUDIO_SR"]
    chunk_ms = 50
    CLSID_MMDeviceEnumerator  = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
    IID_IAudioSessionManager2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
    IID_IAudioSessionControl2 = "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"
    eRender = 0
    _pc=[None,0.0]
    def find_potplayer_pid():
        now=time.time()
        if _pc[0] is not None and now-_pc[1]<0.5: return _pc[0]
        for p in psutil.process_iter(["pid","name"]):
            n=p.info["name"].lower()
            if "potplayer" in n or "pot player" in n:
                _pc[0]=p.info["pid"]; _pc[1]=now; return _pc[0]
        _pc[0]=None; _pc[1]=now; return None
    _cm=[None]; _cp=[None]
    def get_potplayer_rms():
        try:
            import comtypes
            pp=find_potplayer_pid()
            if pp is None: _cm[0]=None; return None,"PID없음"
            if _cm[0] is not None and _cp[0]==pp:
                pk=ctypes.c_float(0)
                try: _cm[0]._comobj.GetPeakValue(ctypes.byref(pk)); return float(pk.value),""
                except: _cm[0]=None
            comtypes.CoInitialize()
            en=comtypes.CoCreateInstance(comtypes.GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),interface=comtypes.IUnknown,clsctx=comtypes.CLSCTX_ALL)
            pd=comtypes.POINTER(comtypes.IUnknown)()
            if en._comobj.GetDefaultAudioEndpoint(0,1,ctypes.byref(pd))!=0: return None,"GetDefaultAudioEndpoint 실패"
            pm=comtypes.POINTER(comtypes.IUnknown)()
            if pd._comobj.Activate(ctypes.byref(comtypes.GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")),23,None,ctypes.byref(pm))!=0: return None,"ASM2 Activate 실패"
            pe=comtypes.POINTER(comtypes.IUnknown)()
            if pm._comobj.GetSessionEnumerator(ctypes.byref(pe))!=0: return None,"GetSessionEnumerator 실패"
            cnt=ctypes.c_int(0); pe._comobj.GetCount(ctypes.byref(cnt))
            g2=comtypes.GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
            gm=comtypes.GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
            for i in range(cnt.value):
                ps=comtypes.POINTER(comtypes.IUnknown)(); pe._comobj.GetSession(i,ctypes.byref(ps))
                pc=comtypes.POINTER(comtypes.IUnknown)()
                if ps._comobj.QueryInterface(ctypes.byref(g2),ctypes.byref(pc))!=0: continue
                pid=ctypes.c_uint(0)
                if pc._comobj.GetProcessId(ctypes.byref(pid))!=0: continue
                if pid.value==pp:
                    mt=comtypes.POINTER(comtypes.IUnknown)()
                    if ps._comobj.QueryInterface(ctypes.byref(gm),ctypes.byref(mt))!=0: return None,"IAudioMeter QI 실패"
                    _cm[0]=mt; _cp[0]=pp
                    pk=ctypes.c_float(0); mt._comobj.GetPeakValue(ctypes.byref(pk))
                    return float(pk.value),""
            return None,f"팟플레이어 세션 없음({cnt.value}개)"
        except Exception as e: _cm[0]=None; return None,f"COM예외:{e}"

    def _find_loopback_device(p, pot_pid, log_devices=False):
        POT_NAMES = {"potplayer", "potplayermini", "potplayermini64",
                     "pot player", "daumpotplayer"}
        by_pid      = None
        by_name     = None
        by_fallback = None
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
            try:
                lpid_int = int(lpid) if lpid is not None else None
            except Exception:
                lpid_int = None
            if pot_pid and lpid_int is not None and lpid_int == int(pot_pid):
                return i, True
            if by_name is None:
                if any(n in dev_name.lower() for n in POT_NAMES):
                    by_name = i
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
        RECHECK_INTERVAL = 3.0
        last_recheck = time.time()
        cur_excl     = is_excl
        cur_pid      = pot_pid
        while not stop_flag.value:
            now = time.time()
            if now - last_recheck >= RECHECK_INTERVAL:
                last_recheck = now
                new_pid = find_potplayer_pid()
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
    def capture_via_activate_audio_interface():
        import threading as _th
        cv = ctypes.c_void_p
        cu = ctypes.c_ulong
        HR = ctypes.HRESULT
        WF = ctypes.WINFUNCTYPE
        ole32 = ctypes.windll.ole32
        try: ole32.CoInitializeEx(None, 0)
        except: pass
        pot_pid = find_potplayer_pid()
        if pot_pid is None:
            return False, "팟플레이어 없음"
        queue_put(audio_queue, ("LOG", f"🎙 ProcessLoopback 시도 (PID={pot_pid})"))
        try:
            class WFX(ctypes.Structure):
                _fields_ = [("wFormatTag",ctypes.c_ushort),("nChannels",ctypes.c_ushort),
                    ("nSamplesPerSec",cu),("nAvgBytesPerSec",cu),
                    ("nBlockAlign",ctypes.c_ushort),("wBitsPerSample",ctypes.c_ushort),("cbSize",ctypes.c_ushort)]
            class AAP(ctypes.Structure):
                _fields_ = [("ActivationType",ctypes.c_uint),("ProcessLoopbackMode",ctypes.c_uint),("TargetProcessId",cu)]
            class PV(ctypes.Structure):
                class _U(ctypes.Union):
                    _fields_ = [("sz",cu),("pb",cv),("p",cv),("pad",ctypes.c_uint64)]
                _fields_ = [("vt",ctypes.c_ushort),("r1",ctypes.c_ushort),("r2",ctypes.c_ushort),("r3",ctypes.c_ushort),("u",_U)]
            ap = AAP(); ap.ActivationType=1; ap.ProcessLoopbackMode=0; ap.TargetProcessId=pot_pid
            pv = PV(); pv.vt=0x41; pv.u.sz=ctypes.sizeof(ap); pv.u.pb=ctypes.cast(ctypes.addressof(ap),cv)
            ev = _th.Event(); ri=[None]; rh=[0]
            CF = WF(HR,cv,cv)
            def _done(this,pOp):
                try:
                    gr=WF(HR,cv,ctypes.POINTER(HR),ctypes.POINTER(cv))(ctypes.cast(pOp,ctypes.POINTER(cv))[0][3])
                    ih=HR(0); ii=cv(0)
                    gr(pOp,ctypes.byref(ih),ctypes.byref(ii))
                    rh[0]=ih.value; ri[0]=ii
                except: rh[0]=-1
                finally: ev.set()
                return 0
            cb=CF(_done)
            QF=WF(HR,cv,cv,ctypes.POINTER(cv)); UF=WF(ctypes.c_ulong,cv)
            qf=QF(lambda t,r,p:0); af=UF(lambda t:1); rf=UF(lambda t:1)
            vt=(cv*4)(ctypes.cast(qf,cv).value,ctypes.cast(af,cv).value,ctypes.cast(rf,cv).value,ctypes.cast(cb,cv).value)
            vp=ctypes.cast(vt,cv); ho=cv(ctypes.addressof(vp)); hp=ctypes.addressof(ho)
            mmdev=ctypes.windll.mmdevapi
            ia=ctypes.create_unicode_buffer("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
            dp=ctypes.create_unicode_buffer(r"\?\SWD#MMDEVAPI#{0.0.1.00000000}.{b3f8fa53-0004-438e-9003-51a46e139bfc}")
            gs=(ctypes.c_byte*16)(); ole32.CLSIDFromString(ia,gs)
            op=cv(0)
            hr=mmdev.ActivateAudioInterfaceAsync(dp,gs,ctypes.byref(pv),cv(hp),ctypes.byref(op))
            if hr!=0: return False,f"Activate 실패:0x{hr&0xFFFFFFFF:08X}"
            ev.wait(timeout=5.0)
            if not ev.is_set(): return False,"타임아웃"
            if rh[0]!=0: return False,f"GetResult 실패:0x{rh[0]&0xFFFFFFFF:08X}"
            if ri[0] is None: return False,"IAudioClient 없음"
            ac=ri[0]
            def vt_(i,idx):
                v=ctypes.cast(i,ctypes.POINTER(cv))[0]
                return ctypes.cast(ctypes.cast(v,ctypes.POINTER(cv))[idx],cv).value
            GMF=WF(HR,cv,ctypes.POINTER(ctypes.POINTER(WFX)))(vt_(ac,8))
            pp=ctypes.POINTER(WFX)()
            hr=GMF(ac,ctypes.byref(pp))
            if hr!=0: return False,f"GetMixFormat 실패:0x{hr&0xFFFFFFFF:08X}"
            w=pp.contents; sr=w.nSamplesPerSec; ch=min(w.nChannels,2)
            wc=WFX(); wc.wFormatTag=3; wc.nChannels=ch; wc.nSamplesPerSec=sr
            wc.wBitsPerSample=32; wc.nBlockAlign=ch*4; wc.nAvgBytesPerSec=sr*ch*4; wc.cbSize=0
            Init=WF(HR,cv,ctypes.c_uint,cu,ctypes.c_longlong,ctypes.c_longlong,cv,cv)(vt_(ac,3))
            hr=Init(ac,0,0x00020000|0x80000000|0x08000000,10_000_000,0,ctypes.addressof(wc),None)
            if hr!=0: return False,f"Initialize 실패:0x{hr&0xFFFFFFFF:08X}"
            GS=WF(HR,cv,cv,ctypes.POINTER(cv))(vt_(ac,14))
            cb2=(ctypes.c_byte*16)(); cs=ctypes.create_unicode_buffer("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
            ole32.CLSIDFromString(cs,cb2); cc=cv(0)
            hr=GS(ac,cb2,ctypes.byref(cc))
            if hr!=0: return False,f"GetService 실패:0x{hr&0xFFFFFFFF:08X}"
            St=WF(HR,cv)(vt_(ac,11)); hr=St(ac)
            if hr!=0: return False,f"Start 실패:0x{hr&0xFFFFFFFF:08X}"
            queue_put(audio_queue,("LOG",f"🎙 ProcessLoopback 성공 (PID={pot_pid},sr={sr},ch={ch})"))
            GB=WF(HR,cv,ctypes.POINTER(cv),ctypes.POINTER(cu),ctypes.POINTER(cu),ctypes.POINTER(ctypes.c_ulonglong),ctypes.POINTER(ctypes.c_ulonglong))(vt_(cc,3))
            RB=WF(HR,cv,cu)(vt_(cc,4))
            NP=WF(HR,cv,ctypes.POINTER(cu))(vt_(cc,5))
            try:
                from scipy.signal import butter,sosfilt
                sos=butter(4,[300,3400],btype="bandpass",fs=sr,output="sos")
            except: sos=None
            CI=chunk_ms/1000.0; RI=3.0; lr=time.time(); cp=pot_pid
            while not stop_flag.value:
                now=time.time()
                if now-lr>=RI:
                    lr=now; np2=find_potplayer_pid()
                    if np2!=cp:
                        queue_put(audio_queue,("LOG",f"🔄 PID변경({cp}→{np2})→재연결"))
                        try: WF(HR,cv)(vt_(ac,12))(ac)
                        except: pass
                        return False,"PID변경"
                ps=cu(0)
                if NP(cc,ctypes.byref(ps))!=0 or ps.value==0:
                    time.sleep(CI); continue
                pd=cv(0); nf=cu(0); df=cu(0)
                if GB(cc,ctypes.byref(pd),ctypes.byref(nf),ctypes.byref(df),None,None)!=0:
                    time.sleep(CI); continue
                n=nf.value
                if n>0 and pd.value:
                    raw=(ctypes.c_float*(n*ch)).from_address(pd.value)
                    arr=np.frombuffer(raw,dtype=np.float32).copy()
                    if ch>1: arr=arr.reshape(-1,ch).mean(axis=1)
                    if sos is not None:
                        try: arr=sosfilt(sos,arr)
                        except: pass
                    queue_put(audio_queue,(time.time(),float(np.sqrt(np.mean(arr**2)))))
                RB(cc,n)
            try: WF(HR,cv)(vt_(ac,12))(ac)
            except: pass
            return True,""
        except Exception as e:
            return False,f"예외:{e}"
    while not stop_flag.value:
        ok, reason = capture_via_activate_audio_interface()
        if ok:
            break
        queue_put(audio_queue, ("LOG", f"ActivateAudioInterface 실패: {reason}"))
        ok, reason = capture_via_pyaudiowpatch()
        if ok:
            break
        queue_put(audio_queue, ("LOG", f"pyaudiowpatch 실패: {reason}"))
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
                if "PID 없음" in err:
                    break
            time.sleep(chunk_ms / 1000)
