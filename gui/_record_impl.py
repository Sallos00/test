"""gui/_record_impl.py -- record_backend.py 의 순수 헬퍼 함수 모음

Pyarmor 8 trial 제한(스크립트당 코드 객체 ~30개)을 넘지 않도록
record_backend.py 에서 분리한 파일이다.
외부에서 직접 import 하지 말 것 — gui.record_backend 를 통해 사용한다.
"""
import os, time, ctypes, ctypes.wintypes as wt, struct

try:
    _user32 = ctypes.windll.user32; _WIN_OK = True
except Exception:
    _WIN_OK = False

# ── 디버그 로그 ───────────────────────────────────────────────────────────────
_debug_log = []
def _log(msg):
    _debug_log.append(msg)
    if len(_debug_log) > 200:
        _debug_log.pop(0)

# ── GDI 구조체 ────────────────────────────────────────────────────────────────
class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize",ctypes.c_uint32),("biWidth",ctypes.c_int32),
        ("biHeight",ctypes.c_int32),("biPlanes",ctypes.c_uint16),
        ("biBitCount",ctypes.c_uint16),("biCompression",ctypes.c_uint32),
        ("biSizeImage",ctypes.c_uint32),("biXPelsPerMeter",ctypes.c_int32),
        ("biYPelsPerMeter",ctypes.c_int32),("biClrUsed",ctypes.c_uint32),
        ("biClrImportant",ctypes.c_uint32),
    ]

# ── 팟플레이어 창 탐색 ────────────────────────────────────────────────────────
def _get_potplayer_video_hwnd(parent_hwnd):
    best_area, best_hwnd = 0, None
    child = _user32.FindWindowExW(parent_hwnd, None, None, None)
    while child:
        rc = wt.RECT()
        _user32.GetClientRect(child, ctypes.byref(rc))
        w = rc.right - rc.left
        h = rc.bottom - rc.top
        if w > 100 and h > 100 and w * h > best_area:
            best_area, best_hwnd = w * h, child
        child = _user32.FindWindowExW(parent_hwnd, child, None, None)
    return best_hwnd

def _get_potplayer_rect():
    if not _WIN_OK:
        return None
    try:
        from win32_utils import find_potplayer_hwnd
        hwnd = find_potplayer_hwnd()
        if not hwnd:
            return None
        target = _get_potplayer_video_hwnd(hwnd) or hwnd
        rc = wt.RECT()
        _user32.GetClientRect(target, ctypes.byref(rc))
        pt = wt.POINT(0, 0)
        _user32.ClientToScreen(target, ctypes.byref(pt))
        w, h = rc.right - rc.left, rc.bottom - rc.top
        return (pt.x, pt.y, w, h) if w > 0 and h > 0 else None
    except Exception:
        return None

# ── ffmpeg 탐색 ───────────────────────────────────────────────────────────────
def _find_ffmpeg():
    import shutil, sys
    meipass = getattr(sys, "_MEIPASS", None)
    cands = ([os.path.join(meipass, "ffmpeg.exe")] if meipass else []) + [
        os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    ]
    for p in cands:
        try:
            if os.path.isfile(p): return p
        except Exception: pass
    return shutil.which("ffmpeg") or ""

# ── 화면 캡처 ─────────────────────────────────────────────────────────────────
def _printwindow_capture(target, gdi32, user32, cw, ch):
    import numpy as np
    bmi = _BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.biWidth, bmi.biHeight = cw, -ch
    bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
    hdc_win = user32.GetDC(target)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    pBits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), 0, ctypes.byref(pBits), None, 0)
    old = gdi32.SelectObject(hdc_mem, hbmp)
    if not user32.PrintWindow(target, hdc_mem, 0x00000002):
        user32.PrintWindow(target, hdc_mem, 0)
    raw = (ctypes.c_uint8 * (cw * ch * 4)).from_address(pBits.value)
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(ch, cw, 4).copy()
    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(target, hdc_win)
    return arr

def _mss_capture_loop(width, height, fps, running_flag, write_frame_cb):
    import cv2 as _cv2
    from win32_utils import find_potplayer_hwnd
    from audio_com import qpc_freq, qpc_now
    gdi32 = ctypes.windll.gdi32
    user32 = ctypes.windll.user32
    _freq = qpc_freq()
    interval = 1.0 / fps
    next_qpc = qpc_now()
    while running_flag.is_set():
        bgr = None
        try:
            hwnd = find_potplayer_hwnd()
            if hwnd:
                target = _get_potplayer_video_hwnd(hwnd) or hwnd
                rc = wt.RECT()
                user32.GetClientRect(target, ctypes.byref(rc))
                cw = (rc.right - rc.left) & ~1
                ch = (rc.bottom - rc.top) & ~1
                if cw > 0 and ch > 0:
                    arr = _printwindow_capture(target, gdi32, user32, cw, ch)
                    frame_qpc = qpc_now()
                    bgr = _cv2.cvtColor(arr, _cv2.COLOR_BGRA2BGR)
                    del arr
                    if (cw, ch) != (width, height):
                        bgr = _cv2.resize(bgr, (width, height))
                else:
                    time.sleep(0.05)
            else:
                time.sleep(0.1)
        except Exception as e:
            _log(f"캡처 오류: {e}")
        if bgr is not None:
            write_frame_cb(bgr, frame_qpc)
            del bgr
        next_qpc += int(interval * _freq)
        sl = (next_qpc - qpc_now()) / _freq
        if sl > 0:
            time.sleep(sl)
        elif sl < -interval:
            next_qpc = qpc_now()
            _log(f"프레임 타이밍 리셋: 지연={-sl*1000:.1f}ms")

def _wgc_capture_hwnd(hwnd, width, height, fps, running_flag, write_frame_cb):
    import numpy as np
    import cv2 as _cv2
    try:
        import winrt.windows.graphics.capture as wgc
        import winrt.windows.graphics.directx as wgdx
        import winrt.windows.graphics.directx.direct3d11 as d3d11
        import winrt.windows.graphics.imaging as wgi
        _use_winrt = True
    except ImportError:
        _use_winrt = False
    if not _use_winrt:
        _printwindow_loop(hwnd, width, height, fps, running_flag, write_frame_cb)
        return
    item = wgc.GraphicsCaptureItem.create_for_window(hwnd)
    if item is None:
        _printwindow_loop(hwnd, width, height, fps, running_flag, write_frame_cb)
        return
    from audio_com import qpc_now, qpc_freq
    d3d   = d3d11.create_direct3d_device()
    BGRA8 = wgdx.DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED
    pool  = wgc.Direct3D11CaptureFramePool.create(d3d, BGRA8, 2, item.size)
    session = pool.create_capture_session(item)
    try: session.is_cursor_capture_enabled = False
    except Exception: pass
    session.start_capture()
    _freq = qpc_freq()
    interval = 1.0 / fps
    next_qpc = qpc_now()
    try:
        while running_flag.is_set():
            sb = buf = ref = arr = bgr = f = None
            try:
                f = pool.try_get_next_frame()
                if f is not None:
                    frame_qpc = qpc_now()
                    sb    = wgi.SoftwareBitmap.create_copy_from_surface_async(f.surface).get()
                    buf   = sb.lock_buffer(wgi.BitmapBufferAccessMode.READ)
                    plane = buf.get_plane_description(0)
                    ref   = buf.create_reference()
                    fh, fw = plane.height, plane.width
                    arr   = np.frombuffer(bytes(ref), dtype=np.uint8).reshape(fh, fw, 4)
                    bgr   = _cv2.cvtColor(arr, _cv2.COLOR_BGRA2BGR)
                    del arr
                    if (fw, fh) != (width, height):
                        bgr = _cv2.resize(bgr, (width, height))
                    write_frame_cb(bgr, frame_qpc)
                    del bgr
            except Exception as e:
                _log(f"WGC 프레임 오류: {e}")
            finally:
                if ref is not None:
                    try: del ref
                    except Exception: pass
                if buf is not None:
                    try: del buf
                    except Exception: pass
                if sb is not None:
                    try: del sb
                    except Exception: pass
                if f is not None:
                    try: f.close()
                    except Exception: pass
            next_qpc += int(interval * _freq)
            sl = (next_qpc - qpc_now()) / _freq
            if sl > 0:
                time.sleep(sl)
            elif sl < -interval:
                next_qpc = qpc_now()
    finally:
        try: session.close()
        except Exception: pass
        try: pool.close()
        except Exception: pass

def _printwindow_loop(hwnd, width, height, fps, running_flag, write_frame_cb):
    import cv2 as _cv2
    from audio_com import qpc_freq, qpc_now
    gdi32  = ctypes.windll.gdi32
    user32 = ctypes.windll.user32
    _freq = qpc_freq()
    interval = 1.0 / fps
    next_qpc = qpc_now()
    while running_flag.is_set():
        bgr = None
        try:
            rc = wt.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rc))
            cw, ch = rc.right - rc.left, rc.bottom - rc.top
            if cw > 0 and ch > 0:
                arr = _printwindow_capture(hwnd, gdi32, user32, cw, ch)
                frame_qpc = qpc_now()
                bgr = _cv2.cvtColor(arr, _cv2.COLOR_BGRA2BGR)
                del arr
                if (cw, ch) != (width, height):
                    bgr = _cv2.resize(bgr, (width, height))
            else:
                time.sleep(0.1)
        except Exception as e:
            _log(f"캡처 오류: {e}")
        if bgr is not None:
            write_frame_cb(bgr, frame_qpc)
            del bgr
        next_qpc += int(interval * _freq)
        sl = (next_qpc - qpc_now()) / _freq
        if sl > 0:
            time.sleep(sl)
        elif sl < -interval:
            next_qpc = qpc_now()
            _log(f"프레임 타이밍 리셋: 지연={-sl*1000:.1f}ms")

# ── 오디오 리타이밍 ───────────────────────────────────────────────────────────
def _retiming_flush_window(wchunks, w_start, out_parts, cursor, start_qpc, sr, ch):
    import numpy as np
    try:
        from scipy.signal import resample_poly as _rsp; _HAS_SCIPY = True
    except ImportError:
        _HAS_SCIPY = False
    if not wchunks: return
    total_frames = sum(len(a) // ch for _, a in wchunks)
    if not total_frames: return
    qpc_elapsed = wchunks[-1][0] - w_start
    raw = np.concatenate([a for _, a in wchunks]).astype(np.float32)

    if qpc_elapsed < 0.5:
        _ep = int((w_start - start_qpc) * sr); _gp = _ep - cursor[0]
        if _gp > 2: _log(f"오디오 갭 패딩: {_gp} frames ({_gp/sr*1000:.1f}ms)"); out_parts.append(np.zeros(_gp * ch, dtype=np.float32)); cursor[0] += _gp
        elif _gp < -2: _sk = min(-_gp, len(raw) // ch); _log(f"오디오 겹침 제거: {_sk} frames ({_sk/sr*1000:.1f}ms)"); raw = raw[_sk * ch:]
        if len(raw) > 0: out_parts.append(raw); cursor[0] += len(raw) // ch
        del raw; return
    clock_ratio = (total_frames / (qpc_elapsed * sr)) if qpc_elapsed > 0 else 1.0
    drift_ppm = abs(clock_ratio - 1.0) * 1e6
    if drift_ppm < 20.0:
        _ep = int((w_start - start_qpc) * sr); _gp = _ep - cursor[0]
        if _gp > 2: _log(f"오디오 갭 패딩: {_gp} frames ({_gp/sr*1000:.1f}ms)"); out_parts.append(np.zeros(_gp * ch, dtype=np.float32)); cursor[0] += _gp
        elif _gp < -2: _sk = min(-_gp, len(raw) // ch); _log(f"오디오 겹침 제거: {_sk} frames ({_sk/sr*1000:.1f}ms)"); raw = raw[_sk * ch:]
        if len(raw) > 0: out_parts.append(raw); cursor[0] += len(raw) // ch
        del raw; return
    _log(f"[ASRC] 드리프트 {drift_ppm:.1f}ppm (ratio={clock_ratio:.8f}) → 리샘플링")
    if _HAS_SCIPY:
        from fractions import Fraction
        frac = Fraction(1.0 / clock_ratio).limit_denominator(10000)
        up, down = frac.numerator, frac.denominator
        try:
            if ch > 1:
                r2d = raw.reshape(-1, ch)
                corrected = np.stack(
                    [_rsp(r2d[:, c], up, down) for c in range(ch)],
                    axis=1).reshape(-1).astype(np.float32)
                del r2d
            else:
                corrected = _rsp(raw, up, down).astype(np.float32)
        except Exception as e:
            _log(f"[ASRC] resample_poly 실패: {e}"); corrected = raw
    else:
        target_frames = int(round(total_frames / clock_ratio))
        if ch > 1:
            r2d = raw.reshape(-1, ch)
            idx = np.linspace(0, len(r2d)-1, target_frames)
            corrected = np.stack(
                [np.interp(idx, np.arange(len(r2d)), r2d[:, c])
                 for c in range(ch)], axis=1).reshape(-1).astype(np.float32)
            del r2d, idx
        else:
            idx = np.linspace(0, len(raw)-1, target_frames)
            corrected = np.interp(idx, np.arange(len(raw)), raw).astype(np.float32)
            del idx
    if corrected is not raw:
        del raw
    _ep2 = int((w_start - start_qpc) * sr); _gp2 = _ep2 - cursor[0]
    if _gp2 > 2: _log(f"오디오 갭 패딩: {_gp2} frames ({_gp2/sr*1000:.1f}ms)"); out_parts.append(np.zeros(_gp2 * ch, dtype=np.float32)); cursor[0] += _gp2
    elif _gp2 < -2: _sk2 = min(-_gp2, len(corrected) // ch); _log(f"오디오 겹침 제거: {_sk2} frames ({_sk2/sr*1000:.1f}ms)"); corrected = corrected[_sk2 * ch:]
    if len(corrected) > 0: out_parts.append(corrected); cursor[0] += len(corrected) // ch
    del corrected

def _retiming_audio(chunks, sr, ch):
    import numpy as np
    if not chunks:
        return np.zeros(0, dtype=np.float32), 0.0
    start_qpc = chunks[0][0]
    out_parts  = []
    cursor     = [0]
    WINDOW_SEC = 10.0
    window_chunks = []
    window_start  = chunks[0][0]
    for qpc_sec, arr in chunks:
        window_chunks.append((qpc_sec, arr))
        if qpc_sec - window_start >= WINDOW_SEC:
            _retiming_flush_window(window_chunks, window_start,
                                   out_parts, cursor, start_qpc, sr, ch)
            window_chunks.clear()
            window_start = qpc_sec
    if window_chunks:
        _retiming_flush_window(window_chunks, window_start,
                               out_parts, cursor, start_qpc, sr, ch)
        window_chunks.clear()
    if not out_parts:
        return np.zeros(0, dtype=np.float32), start_qpc
    try:
        result = np.concatenate(out_parts).astype(np.float32)
    finally:
        out_parts.clear()
    return result, start_qpc

# ── 오디오 청크 스트리밍 I/O ──────────────────────────────────────────────────
# 포맷: [qpc_sec: double 8B][data_bytes: uint32 4B][float32 PCM: data_bytes B] 반복

def _write_audio_chunk(fh, lock, qpc_sec, float32_arr):
    """청크를 임시 파일에 append. 실패 시 조용히 무시."""
    try:
        raw = float32_arr.tobytes()
        header = struct.pack('<dI', qpc_sec, len(raw))
        with lock:
            fh.write(header)
            fh.write(raw)
    except Exception:
        pass

def _read_audio_chunks_from_file(path):
    """임시 파일을 읽어 청크 리스트로 복원. 실패 시 빈 리스트."""
    import numpy as np
    chunks = []
    try:
        with open(path, 'rb') as f:
            while True:
                header = f.read(12)
                if len(header) < 12:
                    break
                qpc_sec, data_len = struct.unpack('<dI', header)
                if data_len == 0:
                    chunks.append((qpc_sec, np.zeros(0, dtype=np.float32)))
                    continue
                raw = f.read(data_len)
                if len(raw) < data_len:
                    break
                chunks.append((qpc_sec, np.frombuffer(raw, dtype=np.float32).copy()))
    except Exception:
        pass
    return chunks

# ── 오디오 캡처 MTA 스레드 ────────────────────────────────────────────────────
def _audio_recorder_mta(recorder):
    import ctypes as ct, numpy as np
    ole32    = ct.windll.ole32
    kernel32 = ct.windll.kernel32
    hr_co = ole32.CoInitializeEx(None, 0x0)
    co_ok = hr_co in (0, 1, 0x80010106)
    client = cap = h_event = None
    try:
        from audio_com import (
            activate_process_loopback, audio_client_initialize,
            audio_client_set_event, audio_client_start, audio_client_stop,
            get_capture_client, get_next_packet_size, get_buffer,
            release_buffer, _com_release, AUDCLNT_BUFFERFLAGS_SILENT,
            qpc_freq, activate_global_loopback, audio_client_initialize_loopback,
        )
        try:
            from audio_capture import _SUPPORT_PROCESS_LOOPBACK as _spl
        except Exception:
            import platform as _p; _spl = int(_p.version().split(".")[-1]) >= 19041
        _freq = qpc_freq()
        if _spl:
            client = activate_process_loopback(recorder._pid)
            sr, ch = audio_client_initialize(client)
        else:
            client = activate_global_loopback()
            sr, ch = audio_client_initialize_loopback(client)
        recorder._sr, recorder._ch = sr, ch
        h_event = kernel32.CreateEventW(None, False, False, None)
        audio_client_set_event(client, h_event)
        cap = get_capture_client(client)
        audio_client_start(client)
        try:
            while recorder._running:
                kernel32.WaitForSingleObject(h_event, 10)
                while recorder._running:
                    try:
                        pkt = get_next_packet_size(cap)
                    except OSError:
                        recorder._running = False; break
                    if pkt == 0: break
                    data, num_frames, flg, dp, qp = get_buffer(cap)
                    if num_frames > 0:
                        if not (flg & AUDCLNT_BUFFERFLAGS_SILENT) and data.value:
                            if qp:
                                chunk_qpc_sec = qp / _freq
                            else:
                                _fallback_q = ct.c_ulonglong()
                                kernel32.QueryPerformanceCounter(ct.byref(_fallback_q))
                                chunk_qpc_sec = _fallback_q.value / _freq
                            if not recorder._first_audio_qpc_sec:
                                recorder._first_audio_qpc_sec = chunk_qpc_sec
                                _log(f"[OBS싱크] 첫 오디오 QPC: {chunk_qpc_sec:.6f}s")
                            buf = (ct.c_float * (num_frames * ch)).from_address(data.value)
                            _arr = np.frombuffer(buf, dtype=np.float32).copy()
                            # ── [메모리 수정] 청크를 메모리 대신 임시 파일에 직접 기록 ──
                            if recorder._stream_mode and recorder._tmp_audio_fh is not None:
                                _write_audio_chunk(recorder._tmp_audio_fh, recorder._chunk_lock,
                                                   chunk_qpc_sec, _arr)
                                del _arr
                            else:
                                recorder._chunks.append((chunk_qpc_sec, _arr))
                        elif recorder._first_audio_qpc_sec and qp:
                            _silent = np.zeros(num_frames * ch, dtype=np.float32)
                            _sqpc  = qp / _freq
                            if recorder._stream_mode and recorder._tmp_audio_fh is not None:
                                _write_audio_chunk(recorder._tmp_audio_fh, recorder._chunk_lock,
                                                   _sqpc, _silent)
                                del _silent
                            else:
                                recorder._chunks.append((_sqpc, _silent))
                    release_buffer(cap, num_frames)
        finally:
            try: audio_client_stop(client)
            except Exception: pass
            _com_release(cap); _com_release(client)
            kernel32.CloseHandle(h_event)
    except Exception as e:
        _log(f"오디오 캡처 오류: {e}"); recorder._running = False
    finally:
        if co_ok: ole32.CoUninitialize()
