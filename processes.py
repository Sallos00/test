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
    _user32, WM_USER, POT_SET_CURRENT_TIME, capture_window,
)
from audio_capture import proc_audio_capture
from audio_com import qpc_freq, qpc_now


def _load_saved_setting(key, default):
    try:
        path = os.path.join(os.environ.get("APPDATA", ""), "AutoSync", "settings.json")
        with open(path, "r") as f:
            return json.load(f).get(key, default)
    except Exception:
        return default


def proc_lip_capture(lip_queue: Queue, stop_flag: Value, cfg: dict):
    """
    립 캡처 프로세스.

    [개선] QPC 하드웨어 타임스탬프 적용
      - time.time() 대신 qpc_now() / qpc_freq() 사용
      - 오디오 캡처와 동일한 QPC 클럭 기준으로 통일
      → proc_analyzer 에서 resample_aligned()로 공통 시간축 정렬 가능
    """
    import cv2
    import sys
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))

    cascade_path = os.path.join(base, 'lbpcascade_animeface.xml')
    cascade = cv2.CascadeClassifier(cascade_path)

    _freq    = qpc_freq()   # QPC 주파수 (한 번만 조회)
    interval = 1.0 / cfg["CAPTURE_FPS"]
    DETECT_EVERY_N = 5
    prev = None
    last_roi = None
    frame_count = 0
    _null_frame_count  = 0
    _last_diag_time    = 0.0
    _total_frame_count = 0

    while not stop_flag.value:
        t0 = time.perf_counter()

        hwnd = find_potplayer_hwnd()
        raw  = capture_window(hwnd) if hwnd else None

        # 30초마다 진단 로그를 lip_queue에 전송
        _now_diag = time.time()
        if _now_diag - _last_diag_time >= 30.0:
            _last_diag_time = _now_diag
            diag = (f"[P1 진단] hwnd={bool(hwnd)} total={_total_frame_count} "
                    f"null={_null_frame_count} "
                    f"shape={raw.shape if raw is not None else None}")
            queue_put(lip_queue, ("LOG", diag))

        if raw is None:
            _null_frame_count += 1
            time.sleep(interval)
            continue

        _total_frame_count += 1

        # [개선] 캡처 직후 QPC 타임스탬프 기록
        t_hw = qpc_now() / _freq

        h, w = raw.shape[:2]
        margin_x = int(w * 0.10)
        margin_y = int(h * 0.10)
        roi = raw[margin_y:h-margin_y, margin_x:w-margin_x]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGRA2GRAY)
        frame_count += 1
        motion = 0.0

        if frame_count % DETECT_EVERY_N == 1 or last_roi is None:
            faces = cascade.detectMultiScale(
                cv2.equalizeHist(gray),
                scaleFactor=1.1,
                minNeighbors=10,
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

        # [개선] t_hw(QPC 기반) 사용 — time.time() 제거
        queue_put(lip_queue, (t_hw, motion))

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
    OAS = bool(cfg.get("OAS", cfg.get("OPED_AUTO_SKIP", False)))
    OSS  = int(cfg.get("OSS", cfg.get("OPED_SKIP_SEC", 90)))
    OZM   = 180 * 1000
    CDS   = 180
    MWI   = 15.0
    MMR  = 0.002
    MMC   = 0.8
    MMF = 0.70
    MCF  = 2

    # ── [개선] EMA 스무딩 파라미터 ──────────────────────────────────────────
    # OBS 버퍼 보정에서 착안: 측정값을 지수이동평균으로 스무딩한 뒤 보정 결정.
    # alpha가 낮을수록 안정적(느린 반응), 높을수록 빠른 반응.
    EMA_ALPHA       = 0.25
    smoothed_offset = 0.0   # EMA 누적값
    EMA_INIT        = False  # 첫 측정값은 그대로 초기화

    lpb   = collections.deque()
    aub   = collections.deque()
    tms  = 0
    lgl = collections.deque(maxlen=100)
    pvt = ""
    mco = 0
    lat = 0.0
    pst   = False
    lcd   = 0.0
    lrd   = 0.0
    pending_prompt = [None]

    def add_log(msg):
        import time as _t
        lgl.append(f"[{_t.strftime('%H:%M:%S')}] {msg}")

    # QPC 기준 현재 시각 헬퍼 — time.time() 대신 사용
    _freq = qpc_freq()
    def _now_qpc() -> float:
        return qpc_now() / _freq

    def is_music_playing():
        if len(aub) < 10:
            return False
        # [수정] QPC 타임스탬프 기준으로 비교
        now_q  = _now_qpc()
        cutoff = now_q - MWI
        vals   = [v for t, v in aub if t >= cutoff]
        if len(vals) < 10:
            return False
        arr      = np.array(vals, dtype=np.float32)
        mean_rms = float(arr.mean())
        if mean_rms < MMR:
            return False
        cv   = float(arr.std() / mean_rms) if mean_rms > 1e-9 else 999.0
        fill = float((arr > mean_rms * 0.5).sum()) / len(arr)
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
        # [수정] QPC 타임스탬프 기준으로 만료 판정
        # lpb: BUF_SEC*2 여유 — ANALYSIS_INTERVAL==BUF_SEC 이면
        # 들어오자마자 만료 판정되어 샘플이 항상 0이 되는 버그 방지
        now_q = _now_qpc()
        while lpb and now_q - lpb[0][0] > BUF_SEC * 2:
            lpb.popleft()
        while aub and now_q - aub[0][0] > MWI:
            aub.popleft()

    # ── [개선] resample_aligned: 공통 시간축 위에서 리샘플 ───────────────────
    # 기존 resample()은 각 버퍼를 독립된 시간축 기준으로 처리해
    # 두 신호의 시작점이 달라지는 문제가 있었다.
    # OBS의 "video-triggers-audio" 방식에서 착안:
    # 두 버퍼의 공통 시간 구간만 사용해 동일한 t_grid 위에 올린다.
    def resample_aligned(lip_buf, aud_buf, fps=15):
        """
        두 버퍼를 공통 시간축 위에서 리샘플.

        Returns
        -------
        (lip_sig, aud_sig) : 같은 길이의 numpy 배열 또는 (None, None)
        """
        if len(lip_buf) < 2 or len(aud_buf) < 2:
            return None, None

        lip_ts = np.array([x[0] for x in lip_buf])
        aud_ts = np.array([x[0] for x in aud_buf])
        lip_vs = np.array([x[1] for x in lip_buf])
        aud_vs = np.array([x[1] for x in aud_buf])

        # 공통 시간 구간 계산
        t_start = max(lip_ts[0], aud_ts[0])
        t_end   = min(lip_ts[-1], aud_ts[-1])

        if t_end - t_start < 1.0:   # 공통 구간이 1초 미만이면 데이터 부족
            return None, None

        n_samples = int((t_end - t_start) * fps)
        if n_samples < fps:          # 최소 1초치 샘플 필요
            return None, None

        t_grid = np.linspace(t_start, t_end, n_samples)
        lip_sig = np.interp(t_grid, lip_ts, lip_vs)
        aud_sig = np.interp(t_grid, aud_ts, aud_vs)
        return lip_sig, aud_sig

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
        if oped_prompt is not None:
            pending_prompt[0] = oped_prompt
        queue_put(state_queue, dict(
            status=status, offset_ms=offset, correction_ms=correction,
            log_lines=snap, potplayer_ok=pot_ok,
            lip_samples=lip_n, audio_samples=aud_n,
            notify=notify,
            oped_prompt=pending_prompt[0],
        ))

    def execute_skip():
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

        # ── 커맨드 처리 ─────────────────────────────────────────────────────
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
                        # [개선] 리셋 시 EMA도 초기화
                        smoothed_offset = 0.0
                        EMA_INIT        = False
                        add_log("↺ 싱크 초기화 (EMA 리셋)")
                    else:
                        add_log("⚠ 팟플레이어 미감지")
                elif cmd == "oped_skip":
                    execute_skip()
                    mco = 0
                    lat = time.time()
                    lcd = 0.0
                    pst   = False
                    pending_prompt[0] = None
                    add_log("⏭ 스킵 완료 → 쿨다운 3분")
                elif cmd == "oped_no_skip":
                    mco = 0
                    lat = time.time()
                    lcd = 0.0
                    pst   = False
                    pending_prompt[0] = None
                    add_log("✖ 스킵 건너뜀 → 쿨다운 3분")
                elif cmd == "oped_reset":
                    mco = 0
                    lat = 0.0
                    lcd = 0.0
                    pst   = False
                    pending_prompt[0] = None
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
                    lcd = 0.0
                    pst   = False
                    pending_prompt[0] = None
                    # [개선] 영상 변경 시 EMA도 초기화
                    smoothed_offset = 0.0
                    EMA_INIT        = False
                    add_log("🔄 영상 변경 → 싱크 + OP/ED + EMA 초기화")
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
                music = is_music_playing()
                if len(aub) >= 10:
                    _vals = [v for t, v in aub if t >= _now_qpc() - MWI]
                    if _vals:
                        _arr  = np.array(_vals, dtype=np.float32)
                        _mean = float(_arr.mean())
                        _cv   = float(_arr.std() / _mean) if _mean > 1e-9 else 999.0
                        _fill = float((_arr > _mean * 0.5).sum()) / len(_arr)
                        _now_rd = time.time()
                        if _now_rd - lrd >= 30:
                            lrd = _now_rd
                            add_log(f"🔍 {zone_label} rms={_mean:.4f} cv={_cv:.2f} fill={_fill:.2f} music={music} mco={mco} OAS={OAS}")
                if music:
                    mco += 1
                    add_log(f"🎵 {zone_label} 음악 감지 ({mco}/{MCF}회)")
                    if mco >= MCF:
                        if OAS:
                            if execute_skip():
                                mco = 0
                                lat = time.time()
                                lcd = 0.0
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
                _now_cd = time.time()
                if _now_cd - lcd >= 30:
                    lcd = _now_cd
                    remain = int(CDS - (_now_cd - lat))
                    add_log(f"⏳ {zone_label} 쿨다운 {remain}초 남음")

        _has_prompt = oped_prompt is not None or pending_prompt[0] is not None

        if aud_n < 10 or (lip_n < 10 and not _has_prompt):
            push_state("데이터 수집 중", 0, tms, lgl, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        # ── [개선] resample_aligned: 공통 시간축 위에서 리샘플 ───────────────
        # 기존: 각자 독립된 시간축 기준 → 두 신호 시작점 불일치 가능
        # 개선: 공통 구간만 사용 → OBS video-triggers-audio 방식과 동일
        lip_sig, aud_sig = resample_aligned(lpb, aub, FPS)

        if lip_sig is None or aud_sig is None:
            if _has_prompt:
                push_state("데이터 수집 중", 0, tms, lgl, pot_ok,
                           lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        raw_offset_ms, lip_std, aud_std, lip_mean, aud_mean = compute_offset(lip_sig, aud_sig)

        # 매 사이클 진단 로그
        add_log(f"📊 raw={raw_offset_ms:.0f}ms lip_std={lip_std:.3f} aud_std={aud_std:.3f} "
                f"lip_mean={lip_mean:.3f} aud_mean={aud_mean:.3f} "
                f"n={len(lip_sig)}")

        if lip_mean < 0.3:
            push_state("미감지", 0, tms, lgl, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(1.0)
            continue

        # lip_std 또는 aud_std가 너무 낮으면 신호가 flat → correlation 신뢰 불가
        if lip_std < 0.05 or aud_std < 0.05:
            add_log(f"⚠ 신호 불충분 (lip_std={lip_std:.3f} aud_std={aud_std:.3f}) → 건너뜀")
            push_state("신호 부족", 0, tms, lgl, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        # ── [개선] EMA 스무딩 ────────────────────────────────────────────────
        if not EMA_INIT:
            smoothed_offset = raw_offset_ms
            EMA_INIT        = True
        else:
            smoothed_offset = EMA_ALPHA * raw_offset_ms + (1.0 - EMA_ALPHA) * smoothed_offset

        offset_ms = smoothed_offset
        add_log(f"📈 smoothed={offset_ms:.1f}ms (EMA α={EMA_ALPHA})")

        if abs(offset_ms) >= THRESH and hwnd:
            steps = min(int(abs(offset_ms) / STEP), MAX_STEPS)
            # correlate(lip, aud) lag 부호 해석:
            #   lag > 0 : lip 피크가 aud보다 나중에 나타남 → 오디오가 빠름
            #             → 오디오를 늦춰야 함 → Shift+. (VK_OEM_PERIOD)
            #   lag < 0 : lip 피크가 aud보다 먼저 나타남 → 오디오가 늦음
            #             → 오디오를 당겨야 함 → Shift+, (VK_OEM_COMMA)
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
            direction = "빠르게(오디오 늦춤)" if offset_ms > 0 else "느리게(오디오 당김)"
            add_log(f"보정: {direction} ×{steps} ({steps * STEP}ms) [스무딩={offset_ms:.1f}ms]")
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
