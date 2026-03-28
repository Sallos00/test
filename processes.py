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
from audio_capture import proc_audio_capture
def _load_saved_setting(key, default):
    try:
        path = os.path.join(os.environ.get("APPDATA", ""), "AutoSync", "settings.json")
        with open(path, "r") as f:
            return json.load(f).get(key, default)
    except Exception:
        return default
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
def proc_analyzer(lip_queue: Queue, audio_queue: Queue,
                  state_queue: Queue, cmd_queue: Queue,
                  stop_flag: Value, cfg: dict,
                  shared_pos=None, shared_dur=None):
    from scipy.signal import correlate
    BUF_SEC      = cfg["BUFFER_SEC"]
    FPS          = cfg["CAPTURE_FPS"]
    THRESH       = cfg["SYNC_THRESHOLD_MS"]
    STEP         = cfg["POTPLAYER_STEP_MS"]
    MAX_STEPS    = cfg["MAX_CORRECT_STEP"]
    INTERVAL     = cfg["ANALYSIS_INTERVAL"]
    MTM = cfg["MAX_TOTAL_SYNC_MS"]
    OAS = bool(cfg.get("OAS", False))
    OSS  = int(cfg.get("OSS", 90))
    OZM   = 180 * 1000
    CDS   = 180
    MWI   = 15.0
    MMR  = 0.03
    MMC   = 0.8
    MMF = 0.70
    MCF  = 2
    lpb   = collections.deque()
    aub   = collections.deque()
    tms  = 0
    lgl = collections.deque(maxlen=100)
    pvt = ""
    mco = 0
    lat = 0.0
    pst   = False
    def add_log(msg):
        import time as _t
        lgl.append(f"[{_t.strftime('%H:%M:%S')}] {msg}")
    def is_music_playing():
        """최근 MWI 초 오디오 RMS로 음악 여부 판별."""
        if len(aub) < 10:
            return False
        now    = time.time()
        cutoff = now - MWI
        vals   = [v for t, v in aub if t >= cutoff]
        if len(vals) < 10:
            return False
        arr      = np.array(vals, dtype=np.float32)
        mean_rms = float(arr.mean())
        if mean_rms < MMR:
            return False
        cv   = float(arr.std() / mean_rms) if mean_rms > 1e-9 else 999.0
        fill = float((arr > 0.02).sum()) / len(arr)
        return cv < MMC and fill > MMF
    def drain_queues():
        for q, buf, tag in [(lip_queue, lpb, "👁"), (audio_queue, aub, "🔊")]:
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
        while lpb and now - lpb[0][0] > BUF_SEC:
            lpb.popleft()
        while aub and now - aub[0][0] > MWI:
            aub.popleft()
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
        snap = _last_log_snapshot[0]
        if snap is None or len(snap) != len(logs) or (logs and snap[-1] != logs[-1]):
            snap = list(logs)
            _last_log_snapshot[0] = snap
        queue_put(state_queue, dict(
            status=status, offset_ms=offset, correction_ms=correction,
            lgl=snap, potplayer_ok=pot_ok,
            lip_samples=lip_n, audio_samples=aud_n,
            notify=notify,
            oped_prompt=oped_prompt,
        ))
    def execute_skip():
        """shared_pos 기준으로 OSS 초 앞으로 이동."""
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
            new_pos = min(pos + OSS * 1000, dur - 2000)
            _user32.SendMessageW(hwnd, WM_USER, POT_SET_CURRENT_TIME, int(new_pos))
            def fmt(ms): s = ms // 1000; return f"{s // 60}:{s % 60:02d}"
            add_log(f"⏭ 스킵 ({OSS}초): {fmt(pos)} → {fmt(new_pos)}")
            return True
        except Exception as e:
            add_log(f"⚠ 스킵 실패: {e}")
            return False
    time.sleep(BUF_SEC)
    adt   = False
    aws = False
    dgc       = 0
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
                        tms = 0
                        add_log("↺ 싱크 초기화")
                    else:
                        add_log("⚠ 팟플레이어 미감지")
                elif cmd == "oped_skip":
                    execute_skip()
                    mco = 0
                    lat = time.time()
                    pst   = False
                    add_log("⏭ 스킵 완료 → 쿨다운 3분")
                elif cmd == "oped_no_skip":
                    mco = 0
                    lat = time.time()
                    pst   = False
                    add_log("✖ 스킵 건너뜀 → 쿨다운 3분")
                elif cmd == "oped_reset":
                    mco = 0
                    lat = 0.0
                    pst   = False
                    add_log("↺ OP/ED 상태 초기화")
                elif cmd == "stop":
                    stop_flag.value = True
                    return
            except Exception:
                break
        drain_queues()
        hwnd   = find_potplayer_hwnd()
        pot_ok = bool(hwnd)
        lip_n  = len(lpb)
        aud_n  = len(aub)
        if hwnd:
            try:
                tbuf = ctypes.create_unicode_buffer(512)
                _user32.GetWindowTextW(hwnd, tbuf, 512)
                cur_title = tbuf.value.strip()
                if cur_title and cur_title != pvt and pvt:
                    post_key_to_potplayer(hwnd, VK_OEM_2, shift=True)
                    time.sleep(0.05)
                    post_key_to_potplayer(hwnd, 0x6F, shift=True)
                    tms      = 0
                    lpb.clear()
                    aub.clear()
                    mco = 0
                    lat = 0.0
                    pst   = False
                    add_log("🔄 영상 변경 → 싱크 + OP/ED 상태 초기화")
                pvt = cur_title
            except Exception:
                pass
        if aud_n == 0 and lip_n > 10 and not aws:
            aws = True
            add_log("⚠ 오디오 미감지 — 팟플레이어 재생 중인지 확인하세요")
        notify      = None
        oped_prompt = None
        if not adt and aud_n > 5:
            adt = True
            notify = ("🎬 동영상 재생 감지",
                      "팟플레이어에서 동영상 재생이 감지되었습니다.\n싱크 분석을 시작합니다.")
            add_log("🎬 동영상 재생 감지")
        pos = shared_pos.value if shared_pos else -1
        dur = shared_dur.value if shared_dur else -1
        if pos >= 0 and dur > 0:
            in_op      = pos < OZM
            in_ed      = pos > (dur - OZM)
            in_zone    = in_op or in_ed
            zone_label = "오프닝" if in_op else "엔딩"
            cooled     = (time.time() - lat) > CDS
            if in_zone and cooled:
                if is_music_playing():
                    mco += 1
                    add_log(f"🎵 {zone_label} 음악 감지 ({mco}/{MCF}회)")
                    if mco >= MCF:
                        if OAS:
                            if execute_skip():
                                mco = 0
                                lat = time.time()
                                add_log(f"⏭ {zone_label} 자동스킵 완료 → 쿨다운 3분")
                        else:
                            if not pst:
                                oped_prompt = {"zone": zone_label, "skip_sec": OSS}
                                pst = True
                                add_log(f"🎵 {zone_label} 팝업 전송")
                else:
                    if mco > 0:
                        mco -= 1
            elif in_zone and not cooled:
                remain = int(CDS - (time.time() - lat))
                add_log(f"⏳ {zone_label} 쿨다운 {remain}초 남음")
        if lip_n < 10 or aud_n < 10:
            push_state("데이터 수집 중", 0, tms, lgl, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue
        lip_sig = resample(lpb, FPS)
        aud_sig = resample(aub, FPS)
        if lip_sig is None or aud_sig is None:
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue
        n = min(len(lip_sig), len(aud_sig))
        offset_ms, _, _, lip_mean, aud_mean = compute_offset(lip_sig[-n:], aud_sig[-n:])
        if dgc < 3:
            dgc += 1
            add_log(f"📊 offset={offset_ms:.0f}ms lip={lip_mean:.3f} aud={aud_mean:.3f}")
        if lip_mean < 1e-6:
            push_state("미감지", 0, tms, lgl, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(1.0)
            continue
        if abs(offset_ms) >= THRESH and hwnd:
            steps = min(int(abs(offset_ms) / STEP), MAX_STEPS)
            sign  = 1 if offset_ms > 0 else -1
            if abs(tms + steps * STEP * sign) > MTM:
                allowed = MTM - abs(tms)
                steps   = max(0, int(allowed / STEP))
            if steps == 0:
                add_log(f"⚠ 싱크 상한 도달 (±{MTM}ms)")
                push_state("상한 도달", offset_ms, tms, lgl, pot_ok,
                           lip_n, aud_n, notify, oped_prompt)
                time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
                continue
            direction = "빠르게" if offset_ms > 0 else "느리게"
            add_log(f"보정: {direction} ×{steps} ({steps * STEP}ms)")
            for _ in range(steps):
                vk = VK_OEM_PERIOD if offset_ms > 0 else VK_OEM_COMMA
                post_key_to_potplayer(hwnd, vk, shift=True)
                time.sleep(0.05)
            tms += steps * STEP * sign
            status = "보정 완료"
        elif not hwnd:
            status = "팟플레이어 미감지"
        else:
            status = "정상"
        push_state(status, offset_ms, tms, lgl, pot_ok,
                   lip_n, aud_n, notify, oped_prompt)
        time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
