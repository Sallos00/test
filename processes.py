import os
import json
import time
import ctypes
import ctypes.wintypes
import collections
# multiprocessing.Queue 불필요 (스레드 전환)
from win32_utils import (
    CFG, find_potplayer_hwnd, post_key_to_potplayer,
    queue_put, VK_OEM_PERIOD, VK_OEM_COMMA, VK_OEM_2,
    _user32, WM_USER, POT_SET_CURRENT_TIME, capture_window,
    get_video_fps,
)
from audio_capture import proc_audio_capture
from audio_com import qpc_freq, qpc_now
from log_utils import (make_add_log, STATUS_OK, STATUS_CORRECTED, STATUS_COLLECTING,
                       STATUS_NO_SIGNAL, STATUS_LOW_CONF, STATUS_COOLDOWN,
                       STATUS_UNDETECTED, STATUS_CEILING, STATUS_NO_POT, STATUS_BUFFERING)
from mem_utils import full_cleanup, full_cleanup_and_release, trim_working_set

def proc_lip_capture(lip_queue, stop_flag, cfg: dict, stream_anchor=None):
    """팟플레이어 화면 캡처 → 입술 개구 신호 추출 프로세스."""
    import numpy as np
    import cv2
    import sys

    base    = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    cascade = cv2.CascadeClassifier(os.path.join(base, "lbpcascade_animeface.xml"))

    _freq          = qpc_freq()
    interval       = 1.0 / cfg["CAPTURE_FPS"]
    DETECT_EVERY_N    = 8           # N 프레임마다 얼굴 재탐지 (15fps 기준 ~1.9초 주기)
    LIP_ROI_SIZE      = (64, 16)    # 입술 패치 리사이즈 크기
    DETECT_SCALE      = 0.5         # 얼굴 감지용 다운스케일 비율 (감지 비용 75% 절감)

    last_roi           = None
    frame_count        = 0
    null_frame_count   = 0
    total_frame_count  = 0
    last_diag_time     = 0.0
    lip_y_ratio        = 0.70   # 얼굴 높이 대비 입술 시작 위치 (동적 갱신)
    lip_ratio_update_n = 0
    no_face_count      = 0      # 연속 얼굴 미감지 횟수
    _hwnd_cache_t      = 0.0    # hwnd 캐시 갱신 시각 (1초마다 재조회)
    _cached_hwnd       = None   # P1 로컬 hwnd 캐시

    def _estimate_lip_y_ratio(face_gray):  # 얼굴 하위 50~92%에서 가장 어두운 행 → 입술 위치
        h            = face_gray.shape[0]
        search_start = int(h * 0.50)
        search_end   = int(h * 0.92)
        if search_end <= search_start:
            return 0.70
        row_means = face_gray[search_start:search_end].mean(axis=1)
        darkest   = int(np.argmin(row_means))
        return float(np.clip((search_start + darkest) / h - 0.05, 0.50, 0.85))

    while not stop_flag.is_set():
        t0   = time.perf_counter()

        # hwnd는 1초마다만 재조회 (find_potplayer_hwnd 비용 절감)
        _t_now = time.perf_counter()
        if _t_now - _hwnd_cache_t >= 1.0:
            _cached_hwnd = find_potplayer_hwnd()
            _hwnd_cache_t = _t_now
        hwnd = _cached_hwnd

        qp_now_val = qpc_now()
        raw  = capture_window(hwnd) if hwnd else None

        if stream_anchor is not None and stream_anchor[0] > 0:
            qp_origin = stream_anchor[0]
            sr_anc    = stream_anchor[1]
            freq_anc  = stream_anchor[2]
            t_hw = (qp_now_val - qp_origin) / freq_anc
        else:
            t_hw = qp_now_val / _freq

        now = time.time()
        if now - last_diag_time >= 30.0:
            last_diag_time = now
            queue_put(lip_queue, ("LOG",
                f"[립캡처 진단] hwnd={bool(hwnd)} total={total_frame_count} "
                f"null={null_frame_count} "
                f"shape={raw.shape if raw is not None else None} "
                f"lip_y_ratio={lip_y_ratio:.2f}"))

        if raw is None:
            null_frame_count += 1
            time.sleep(interval)
            continue

        total_frame_count += 1

        h, w  = raw.shape[:2]
        mx    = int(w * 0.10)
        my    = int(h * 0.10)
        gray  = cv2.cvtColor(raw[my:h-my, mx:w-mx], cv2.COLOR_BGRA2GRAY)
        del raw  # GDI 비트맵 복사본 즉시 해제 (수 MB 규모)
        raw = None
        frame_count += 1
        motion = 0.0

        if frame_count % DETECT_EVERY_N == 1 or last_roi is None:
            # 다운스케일된 이미지로 얼굴 감지 → 연산량 대폭 감소
            dw = max(1, int(gray.shape[1] * DETECT_SCALE))
            dh = max(1, int(gray.shape[0] * DETECT_SCALE))
            gray_small = cv2.resize(gray, (dw, dh), interpolation=cv2.INTER_LINEAR)
            faces = cascade.detectMultiScale(
                cv2.equalizeHist(gray_small),
                scaleFactor=1.1, minNeighbors=10, minSize=(30, 30),
            )
            if len(faces):
                # 좌표를 원본 해상도로 역변환
                inv = 1.0 / DETECT_SCALE
                x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                x  = int(x  * inv); y  = int(y  * inv)
                fw = int(fw * inv); fh = int(fh * inv)
                last_roi      = (x, y, fw, fh)
                no_face_count = 0
                lip_ratio_update_n += 1
                if lip_ratio_update_n % 30 == 1:
                    h_img, w_img = gray.shape
                    face_crop = gray[y:min(y+fh, h_img), x:min(x+fw, w_img)]
                    if face_crop.size > 0:
                        lip_y_ratio = _estimate_lip_y_ratio(face_crop)
            else:
                no_face_count += 1
                # 연속 2회 탐지 실패 시 즉시 last_roi 초기화
                # 오탐으로 last_roi가 유지돼 버퍼에 쌓이는 문제 방지
                if no_face_count >= 2:
                    last_roi = None

        if last_roi is not None:
            x, y, fw, fh = last_roi
            h_img, w_img = gray.shape
            y1      = min(y + int(fh * lip_y_ratio),          h_img - 1)
            y2      = min(y + int(fh * (lip_y_ratio + 0.25)), h_img)
            lip_roi = gray[y1:y2, x:min(x+fw, w_img)]
            if lip_roi.size > 0:
                small  = cv2.resize(lip_roi, LIP_ROI_SIZE)
                motion = float(small.var(axis=0).mean()) / (255.0 ** 2)
                del small
            del lip_roi
            queue_put(lip_queue, (t_hw, motion))

        del gray  # 그레이스케일 프레임 즉시 해제

        sleep_t = interval - (time.perf_counter() - t0)
        if sleep_t > 0:
            time.sleep(sleep_t)

def proc_analyzer(lip_queue, audio_queue,
                  state_queue, cmd_queue,
                  stop_flag, cfg: dict,
                  shared_pos=None, shared_dur=None):
    """교차상관으로 싱크 오프셋 추정 및 팟플레이어 자동 보정 프로세스."""
    import numpy as np
    from scipy.signal import correlate

    BUF_SEC   = cfg["BUFFER_SEC"]
    THRESH    = cfg["SYNC_THRESHOLD_MS"]
    STEP      = cfg["POTPLAYER_STEP_MS"]
    MAX_STEPS = cfg["MAX_CORRECT_STEP"]
    INTERVAL  = cfg["ANALYSIS_INTERVAL"]
    MTM       = cfg["MAX_TOTAL_SYNC_MS"]
    OAS       = bool(cfg.get("OAS", cfg.get("OPED_AUTO_SKIP", False)))
    OSS       = int(cfg.get("OSS",  cfg.get("OPED_SKIP_SEC",  90)))
    # ── [버그2&3 수정] 세대 ID — _refresh()에서 좀비 스레드 아이템 필터링에 사용 ──
    _GENERATION = cfg.get("_generation", 0)

    OPED_ZONE_MS      = 90_000   # OP/ED 탐지 구간: 재생 위치 앞뒤 90초 이내
    OPED_COOLDOWN_SEC = 90       # OP/ED 감지 후 재감지 억제 시간
    MUSIC_WINDOW_SEC  = 15.0     # 음악 감지 RMS 슬라이딩 윈도우
    MUSIC_MIN_RMS     = 0.002    # 이 이하는 무음으로 판정
    MUSIC_MAX_CV      = 0.8      # 변동계수 상한 (이 이하면 안정적인 음악)
    MUSIC_MIN_FILL    = 0.70     # RMS > 평균×0.5 비율 하한
    MUSIC_CONFIRM     = 2        # 연속 감지 횟수 기준

    EMA_ALPHA          = 0.5      # EMA 계수 (높을수록 빠른 반응)
    OFFSET_BUF_SIZE    = 3        # 평균 낼 연속 측정 횟수
    SYNC_COOLDOWN_SEC  = 10.0     # 보정 후 재보정 억제 시간
    CONFIDENCE_THRESH  = 0.25     # 교차상관 신뢰도 하한 (이 미만이면 해당 사이클 skip)

    # 버퍼 상한 고정 — 무한 누적 방지 (문제 2 수정)
    _MAX_BUF = int(max(BUF_SEC, MUSIC_WINDOW_SEC) * 25 + 100)
    lpb = collections.deque(maxlen=_MAX_BUF)
    aub = collections.deque(maxlen=_MAX_BUF)

    total_correction_ms = 0       # 누적 보정량 (팟플레이어 딜레이 절대값 추적)
    smoothed_offset     = 0.0
    ema_initialized     = False
    offset_buf          = collections.deque(maxlen=OFFSET_BUF_SIZE)
    last_correction_t   = 0.0
    video_fps           = 30.0    # 현재 영상 fps (재생 감지·영상 변경 시 갱신)

    log_lines    = collections.deque(maxlen=100)
    prev_title   = ""
    audio_warned = False   # 오디오 미감지 경고 1회 플래그
    audio_det    = False   # 오디오 최초 감지 알림 플래그

    oped_confirm  = {"오프닝": 0,     "엔딩": 0}
    oped_last_t   = {"오프닝": 0.0,   "엔딩": 0.0}
    oped_prompted = {"오프닝": False,  "엔딩": False}
    last_cd_log   = 0.0   # 쿨다운 로그 스로틀
    last_rms_log  = 0.0   # RMS 진단 로그 스로틀
    pending_prompt = [None]

    # ── 녹화 중 정리 억제 플래그 ─────────────────────────────────────────────
    # GUI 에서 "recording_start" / "recording_stop" cmd 로 갱신
    _is_recording_active = False

    # ── 팟플레이어 미감지 쿨다운 (조건 1) ────────────────────────────────────
    # 미감지 시 정리 후 60초간 재정리 억제
    NO_POT_CLEANUP_CD  = 60.0
    _no_pot_cleanup_t  = 0.0   # 마지막 미감지 정리 시각
    _prev_pot_ok       = True  # 직전 루프의 pot_ok (감지→미감지 전환 감지용)

    add_log = make_add_log(log_lines)

    def is_music_playing() -> bool:
        if len(aub) < 10:
            return False
        latest_t = aub[-1][0]
        cutoff   = latest_t - MUSIC_WINDOW_SEC
        vals     = [x[1] for x in aub if x[0] >= cutoff]   # rms
        if len(vals) < 10:
            return False
        arr      = np.array(vals, dtype=np.float32)
        mean_rms = float(arr.mean())
        if mean_rms < MUSIC_MIN_RMS:
            return False
        cv   = float(arr.std() / mean_rms) if mean_rms > 1e-9 else 999.0
        fill = float((arr > mean_rms * 0.5).sum()) / len(arr)
        return cv < MUSIC_MAX_CV and fill > MUSIC_MIN_FILL

    def drain_queues():
        """lip_queue(Pipe Connection 또는 mp.Queue)와 audio_queue(queue.Queue)를
        드레인해 lpb / aub 버퍼에 적재한다.
        lip_queue가 Pipe Connection이면 poll()+recv(), 아니면 get_nowait() 사용.

        [수정] 한 번의 drain_queues() 호출에서 처리하는 항목 수를 제한한다.
        큐 전체를 한 번에 소비하면 대량 적체 시 lpb/aub가 순간적으로 폭증하여
        메모리가 급격히 증가한다. 루프당 최대 처리 개수를 제한해 이를 방지한다.
        """
        # [수정] 루프 1회당 최대 처리 개수 상한
        # lip_queue: 영상 FPS 기준(30fps × 약 0.3초 여유) → 최대 10개
        # audio_queue: 오디오 패킷 밀도 고려 → 최대 10개
        _LIP_DRAIN_LIMIT   = 10
        _AUDIO_DRAIN_LIMIT = 10

        # ── lip_queue 드레인 ──────────────────────────────────────────────────
        _lip_is_pipe = hasattr(lip_queue, 'poll') and not hasattr(lip_queue, 'get_nowait')
        _lip_count = 0  # [수정] 처리 개수 카운터
        while _lip_count < _LIP_DRAIN_LIMIT:  # [수정] 상한 초과 시 중단
            try:
                if _lip_is_pipe:
                    if not lip_queue.poll():
                        break
                    item = lip_queue.recv()
                else:
                    try:
                        item = lip_queue.get_nowait()
                    except Exception:
                        break
                if isinstance(item, tuple) and len(item) == 2 and item[0] == "LOG":
                    add_log(f"👁 {item[1]}")
                else:
                    lpb.append(item)
                _lip_count += 1  # [수정] 성공 처리 시에만 카운트
            except (EOFError, OSError):
                # Pipe writer 쪽이 닫혔음 — P1 종료 신호, 조용히 종료
                break
            except Exception:
                break

        # ── audio_queue 드레인 ───────────────────────────────────────────────
        _aud_count = 0  # [수정] 처리 개수 카운터
        while _aud_count < _AUDIO_DRAIN_LIMIT:  # [수정] 상한 초과 시 중단
            try:
                item = audio_queue.get_nowait()
                if isinstance(item, tuple) and len(item) == 2 and item[0] == "LOG":
                    add_log(f"🔊 {item[1]}")
                else:
                    aub.append(item)
                _aud_count += 1  # [수정] 성공 처리 시에만 카운트
            except Exception:
                break

        # BUF_SEC 기준으로 트리밍 (오래된 샘플 제거)
        _trim_win = max(BUF_SEC, MUSIC_WINDOW_SEC)
        if lpb:
            latest_lip = lpb[-1][0]
            while lpb and latest_lip - lpb[0][0] > _trim_win:
                lpb.popleft()
        if aub:
            latest_aud = aub[-1][0]
            while aub and latest_aud - aub[0][0] > _trim_win:
                aub.popleft()

    def resample_aligned(lip_buf, aud_buf, fps):

        if len(lip_buf) < 2 or len(aud_buf) < 2:
            return None, None

        lip_ts = np.fromiter((x[0] for x in lip_buf), dtype=np.float64, count=len(lip_buf))
        aud_ts = np.fromiter((x[0] for x in aud_buf), dtype=np.float64, count=len(aud_buf))
        lip_vs = np.fromiter((x[1] for x in lip_buf), dtype=np.float32, count=len(lip_buf))
        rms_vs = np.fromiter((x[1] for x in aud_buf), dtype=np.float32, count=len(aud_buf))  # rms
        vad_vs = np.fromiter((x[2] for x in aud_buf), dtype=np.float32, count=len(aud_buf))  # vad

        # VAD로 마스킹한 RMS diff
        # - RMS diff: 대사 시작/끝의 변화 시점을 잡음 → 절대 타임스탬프 편향 없음
        # - VAD 마스킹: BGM 구간(VAD=0)을 0으로 제거 → flat 신호 문제 해결
        rms_diff = np.abs(np.diff(rms_vs, prepend=rms_vs[0]))
        aud_vs   = rms_diff * vad_vs

        t_start = max(lip_ts[0], aud_ts[0])
        t_end   = min(lip_ts[-1], aud_ts[-1])

        if t_end - t_start < 1.0:
            return None, None

        n_samples = int((t_end - t_start) * fps)
        if n_samples < fps:
            return None, None

        t_grid  = np.linspace(t_start, t_end, n_samples)
        lip_sig = np.interp(t_grid, lip_ts, lip_vs)
        aud_sig = np.interp(t_grid, aud_ts, aud_vs)
        return lip_sig, aud_sig

    def to_binary(signal, ratio):

        thresh = np.median(signal) + ratio * signal.std()
        return (signal > thresh).astype(np.float32)

    def normalize(x):
        x = x - x.mean()
        s = x.std()
        return x / s if s > 1e-9 else x

    def compute_offset(lip, aud, fps):

        lip_bin = to_binary(lip, ratio=0.5)
        lip_sig = normalize(lip_bin) if lip_bin.std() >= 1e-9 else normalize(lip)

        aud_sig = normalize(aud) if aud.std() >= 1e-9 else aud

        max_lag_samples = int(MTM / 1000.0 * fps)
        corr   = correlate(lip_sig, aud_sig, mode="full")
        center = len(aud_sig) - 1
        lo     = max(0, center - max_lag_samples)
        hi     = min(len(corr), center + max_lag_samples + 1)

        sub_corr = corr[lo:hi]
        peak_rel = int(np.argmax(np.abs(sub_corr)))
        peak_idx = peak_rel + lo

        energy     = np.sqrt(np.sum(lip_sig**2) * np.sum(aud_sig**2))
        confidence = float(abs(corr[peak_idx]) / energy) if energy > 1e-9 else 0.0

        if lo < peak_idx < hi - 1:
            y0, y1, y2 = corr[peak_idx-1], corr[peak_idx], corr[peak_idx+1]
            denom = 2*y1 - y0 - y2
            sub   = 0.5 * (y2 - y0) / denom if abs(denom) > 1e-9 else 0.0
            lag   = (peak_idx + sub) - center
        else:
            lag = peak_idx - center

        return lag / fps * 1000, lip_bin.std(), aud.std(), lip.mean(), aud.mean(), confidence

    _last_log_snapshot = [None]

    def push_state(status, offset, correction, logs, pot_ok, lip_n, aud_n,
                   notify=None, oped_prompt=None):
        snap = _last_log_snapshot[0]
        if snap is None or len(snap) != len(logs) or (logs and snap[-1] != logs[-1]):
            snap = list(logs)
            _last_log_snapshot[0] = snap
        if oped_prompt is not None:
            pending_prompt[0] = oped_prompt
        prompt_to_send    = pending_prompt[0]
        pending_prompt[0] = None
        queue_put(state_queue, dict(
            status=status, offset_ms=offset, correction_ms=correction,
            log_lines=snap, potplayer_ok=pot_ok,
            lip_samples=lip_n, audio_samples=aud_n,
            notify=notify, oped_prompt=prompt_to_send,
            # ── [버그2&3 수정] 세대 ID 포함 — GUI에서 좀비 스레드 필터링 ──
            _generation=_GENERATION,
        ))

    def execute_skip() -> bool:
        hwnd = find_potplayer_hwnd()
        if not hwnd:
            add_log("⚠ 스킵 실패: 팟플레이어 미감지")
            return False
        try:
            pos = shared_pos[0] if shared_pos else 0
            dur = shared_dur[0] if shared_dur else 0
            if dur <= 0:
                add_log("⚠ 스킵 실패: 전체 길이 미확인")
                return False
            new_pos = min(pos + OSS * 1000, dur - 2000)
            _user32.SendMessageW(hwnd, WM_USER, POT_SET_CURRENT_TIME, int(new_pos))
            fmt = lambda ms: f"{ms//1000//60}:{ms//1000%60:02d}"
            add_log(f"⏭ 스킵 ({OSS}초): {fmt(pos)} → {fmt(new_pos)}")
            return True
        except Exception as e:
            add_log(f"⚠ 스킵 실패: {e}")
            return False

    def reset_sync():
        nonlocal total_correction_ms, smoothed_offset, ema_initialized, last_correction_t
        total_correction_ms = 0
        smoothed_offset     = 0.0
        ema_initialized     = False
        last_correction_t   = 0.0
        offset_buf.clear()
        lpb.clear()
        aub.clear()

    def reset_oped():
        nonlocal oped_confirm, oped_last_t, oped_prompted
        oped_confirm  = {"오프닝": 0,     "엔딩": 0}
        oped_last_t   = {"오프닝": 0.0,   "엔딩": 0.0}
        oped_prompted = {"오프닝": False,  "엔딩": False}
        pending_prompt[0] = None

    def _flush_and_gc(label: str):
        """보정·정상 판정 후 샘플 버퍼·큐 드레인 + GC + Working Set 트림.
        녹화 중(_is_recording_active=True)이면 절대 실행하지 않는다 (조건 4).
        mem_utils.full_cleanup 을 사용해 A(드레인)+B(버퍼클리어)+C(GC) 수행.
        GC 직후 trim_working_set()으로 RAM 페이지를 OS에 반환한다.
        큐는 재사용하므로 close()/join_thread()는 호출하지 않는다.
        """
        if _is_recording_active:
            add_log(f"⏸ [{label}] 녹화 중 — 메모리 정리 억제")
            return
        full_cleanup(
            queues=(lip_queue, audio_queue),
            bufs=(offset_buf, lpb, aub),
        )
        trim_working_set()   # GC 완료 직후 — 10초 쿨다운 전 최적 타이밍
        add_log(f"🧹 [{label}] 버퍼·큐 초기화 및 메모리 정리 완료 → {SYNC_COOLDOWN_SEC:.0f}초 쿨다운 시작")

    # BUF_SEC 동안 대기하되 stop_flag가 세워지면 즉시 탈출
    _buf_end = time.perf_counter() + BUF_SEC
    while not stop_flag.is_set() and time.perf_counter() < _buf_end:
        time.sleep(0.05)
    if stop_flag.is_set():
        return

    # ── [버그4 수정] 쿨다운 중 상태를 추적해 루프를 올바르게 제어 ─────────
    in_cooldown = False   # 보정/정상 판정 후 쿨다운 진행 중 플래그

    while not stop_flag.is_set():
        t0 = time.perf_counter()

        while True:
            try:
                cmd = cmd_queue.get_nowait()
            except Exception:
                break
            if cmd == "reset":
                hwnd = find_potplayer_hwnd()
                if hwnd:
                    post_key_to_potplayer(hwnd, VK_OEM_2, shift=True)
                    time.sleep(0.05)
                    post_key_to_potplayer(hwnd, 0x6F, shift=True)
                    # 싱크 시작 시 1·2번 쿨다운 초기화 + 버퍼·큐 정리 (조건 3)
                    # 녹화 중이면 _flush_and_gc 내부에서 억제됨 (조건 4)
                    _no_pot_cleanup_t = 0.0   # 미감지 쿨다운 초기화
                    _flush_and_gc("수동 초기화")
                    reset_sync()
                    in_cooldown = False
                    add_log("↺ 싱크 초기화")
                else:
                    add_log("⚠ 팟플레이어 미감지")
            elif cmd == "recording_start":
                # 조건 4: 녹화 시작 → 정리 억제 플래그 ON
                _is_recording_active = True
                add_log("🔴 녹화 시작 — 메모리 정리 억제 활성화")
            elif cmd == "recording_stop":
                # 조건 4: 녹화 종료 → 정리 억제 플래그 OFF
                _is_recording_active = False
                add_log("⏹ 녹화 종료 — 메모리 정리 억제 해제")
            elif cmd == "oped_skip":
                execute_skip()
                zone = pending_prompt[0].get("zone", "오프닝") if pending_prompt[0] else "오프닝"
                oped_confirm[zone]  = 0
                oped_last_t[zone]   = time.time()
                oped_prompted[zone] = False
                pending_prompt[0]   = None
                add_log(f"⏭ {zone} 스킵 완료 → 쿨다운 {OPED_COOLDOWN_SEC}초")
            elif cmd == "oped_no_skip":
                zone = pending_prompt[0].get("zone", "오프닝") if pending_prompt[0] else "오프닝"
                oped_confirm[zone]  = 0
                oped_last_t[zone]   = time.time()
                oped_prompted[zone] = False
                pending_prompt[0]   = None
                add_log(f"✖ {zone} 스킵 건너뜀 → 쿨다운 {OPED_COOLDOWN_SEC}초")
            elif cmd == "oped_reset":
                reset_oped()
                add_log("↺ OP/ED 상태 초기화")
            elif cmd == "stop":
                stop_flag.set()
                return

        drain_queues()

        hwnd   = find_potplayer_hwnd()
        pot_ok = bool(hwnd)
        lip_n  = len(lpb)
        aud_n  = len(aub)

        # ── 조건 1: 팟플레이어 미감지 → 정리 + 60초 쿨다운 ─────────────────
        # 조건 4: 녹화 중이면 정리하지 않음
        if not pot_ok:
            _now_t = time.time()
            # 미감지 전환 직후 OR 이전 쿨다운이 만료된 경우에만 정리
            _no_pot_cd_expired = (_now_t - _no_pot_cleanup_t) >= NO_POT_CLEANUP_CD
            if (lpb or aub) and _no_pot_cd_expired and not _is_recording_active:
                full_cleanup(queues=(lip_queue, audio_queue), bufs=(offset_buf, lpb, aub))
                _no_pot_cleanup_t = _now_t
                add_log(f"🧹 [팟플레이어 미감지] 버퍼 정리 → {NO_POT_CLEANUP_CD:.0f}초 쿨다운")
            if in_cooldown:
                in_cooldown = False
            _prev_pot_ok = False
            push_state(STATUS_NO_POT, 0, total_correction_ms, log_lines, pot_ok,
                       0, 0)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        # ── 조건 2: 팟플레이어 재감지 (미감지→감지 전환) ───────────────────
        # 미감지 쿨다운 초기화 + 정리 수행 후 새 60초 쿨다운 시작
        # 조건 4: 녹화 중이면 정리하지 않음
        if not _prev_pot_ok and pot_ok:
            if not _is_recording_active:
                _no_pot_cleanup_t = 0.0   # 쿨다운 초기화
                full_cleanup(queues=(lip_queue, audio_queue), bufs=(offset_buf, lpb, aub))
                _no_pot_cleanup_t = time.time()
                add_log(f"🧹 [팟플레이어 재감지] 버퍼 정리 → {NO_POT_CLEANUP_CD:.0f}초 쿨다운")
            else:
                add_log("⏸ [팟플레이어 재감지] 녹화 중 — 정리 억제")
            in_cooldown = False
        _prev_pot_ok = True

        if hwnd:
            try:
                tbuf = ctypes.create_unicode_buffer(512)
                _user32.GetWindowTextW(hwnd, tbuf, 512)
                cur_title = tbuf.value.strip()
                if cur_title and cur_title != prev_title and prev_title:
                    post_key_to_potplayer(hwnd, VK_OEM_2, shift=True)
                    time.sleep(0.05)
                    post_key_to_potplayer(hwnd, 0x6F, shift=True)
                    # 영상 변경 시에도 녹화 중이 아닐 때만 정리
                    if not _is_recording_active:
                        full_cleanup(queues=(lip_queue, audio_queue), bufs=(offset_buf, lpb, aub))
                    else:
                        lpb.clear()
                        aub.clear()
                    reset_sync()
                    reset_oped()
                    in_cooldown = False
                    video_fps = get_video_fps(hwnd)
                    add_log(f"🔄 영상 변경 → 전체 초기화 (fps={video_fps})")
                prev_title = cur_title
            except Exception:
                pass

        if aud_n == 0 and lip_n > 10 and not audio_warned:
            audio_warned = True
            add_log("⚠ 오디오 미감지 — 팟플레이어 재생 중인지 확인하세요")

        notify      = None
        oped_prompt = None

        if not audio_det and aud_n > 5:
            audio_det = True
            video_fps = get_video_fps(hwnd)
            notify = ("🎬 동영상 재생 감지", "팟플레이어에서 동영상 재생이 감지되었습니다.\n싱크 분석을 시작합니다.")
            add_log(f"🎬 동영상 재생 감지 (fps={video_fps})")

        pos = shared_pos[0] if shared_pos else -1
        dur = shared_dur[0] if shared_dur else -1

        if pos >= 0 and dur > 0:
            in_op   = pos < OPED_ZONE_MS and pos <= dur // 2
            in_ed   = pos > (dur - OPED_ZONE_MS) and pos > dur // 2
            in_zone = in_op or in_ed
            zone    = "오프닝" if in_op else "엔딩"
            cooled  = (time.time() - oped_last_t[zone]) > OPED_COOLDOWN_SEC

            if in_zone and cooled:
                music = is_music_playing()

                # RMS 진단 로그 (30초 스로틀)
                if len(aub) >= 10:
                    now_rd = time.time()
                    if now_rd - last_rms_log >= 30:
                        last_rms_log = now_rd
                        vals = [x[1] for x in aub if x[0] >= aub[-1][0] - MUSIC_WINDOW_SEC]
                        if vals:
                            arr  = np.array(vals, dtype=np.float32)
                            mean = float(arr.mean())
                            cv   = float(arr.std() / mean) if mean > 1e-9 else 999.0
                            fill = float((arr > mean * 0.5).sum()) / len(arr)
                            add_log(f"🔍 {zone} rms={mean:.4f} cv={cv:.2f} fill={fill:.2f} "
                                    f"music={music} confirm={oped_confirm[zone]}/{MUSIC_CONFIRM}")

                if music:
                    oped_confirm[zone] += 1
                    add_log(f"🎵 {zone} 음악 감지 ({oped_confirm[zone]}/{MUSIC_CONFIRM}회)")
                    if oped_confirm[zone] >= MUSIC_CONFIRM:
                        if OAS:
                            if execute_skip():
                                oped_confirm[zone] = 0
                                oped_last_t[zone]  = time.time()
                                add_log(f"⏭ {zone} 자동스킵 완료 → 쿨다운 {OPED_COOLDOWN_SEC}초")
                        elif not oped_prompted[zone]:
                            oped_prompt           = {"zone": zone, "skip_sec": OSS}
                            oped_prompted[zone]   = True
                            add_log(f"🎵 {zone} 팝업 전송")
                elif oped_confirm[zone] > 0:
                    oped_confirm[zone] -= 1

            elif in_zone and not cooled:
                now_cd = time.time()
                if now_cd - last_cd_log >= 30:
                    last_cd_log = now_cd
                    remain = int(OPED_COOLDOWN_SEC - (now_cd - oped_last_t[zone]))
                    add_log(f"⏳ {zone} 쿨다운 {remain}초 남음")

        has_prompt = oped_prompt is not None or pending_prompt[0] is not None

        # ── [버그4 수정] 쿨다운 중에는 drain만 하고 분석 건너뜀 ──────────────
        cooldown_remain = SYNC_COOLDOWN_SEC - (time.time() - last_correction_t)
        if in_cooldown and cooldown_remain > 0:
            push_state(STATUS_COOLDOWN, smoothed_offset, total_correction_ms, log_lines, pot_ok,
                       lip_n, aud_n, None, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue
        elif in_cooldown and cooldown_remain <= 0:
            # 쿨다운 종료 → 다음 사이클부터 정상 수집 재개
            in_cooldown = False
            add_log("🔄 쿨다운 종료 → 버퍼 수집 재개")

        if aud_n < 10 or (lip_n < 10 and not has_prompt):
            push_state(STATUS_COLLECTING, 0, total_correction_ms, log_lines, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        lip_sig, aud_sig = resample_aligned(lpb, aub, video_fps)

        if lip_sig is None or aud_sig is None:
            if has_prompt:
                push_state(STATUS_COLLECTING, 0, total_correction_ms, log_lines, pot_ok,
                           lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        pre_lip_std = float(lip_sig.std())
        pre_aud_std = float(aud_sig.std())
        if pre_lip_std < 0.001 or pre_aud_std < 1e-4:
            add_log(f"⚠ 신호 불충분 (lip_std={pre_lip_std:.4f} aud_std={pre_aud_std:.6f}) → 생략")
            push_state(STATUS_NO_SIGNAL, 0, total_correction_ms, log_lines, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        raw_ms, lip_std, aud_std, lip_mean, aud_mean, confidence = compute_offset(lip_sig, aud_sig, video_fps)

        add_log(f"📊 raw={raw_ms:.0f}ms lip_std={lip_std:.3f} aud_std={aud_std:.4f} "
                f"lip_mean={lip_mean:.4f} conf={confidence:.3f} n={len(lip_sig)}")

        if lip_mean < 0.002:
            push_state(STATUS_UNDETECTED, 0, total_correction_ms, log_lines, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(1.0)
            continue

        if lip_std < 0.05 or aud_std < 1e-4:
            add_log(f"⚠ 신호 불충분 (lip_std={lip_std:.3f} aud_std={aud_std:.6f}) → 건너뜀")
            push_state(STATUS_NO_SIGNAL, 0, total_correction_ms, log_lines, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        if confidence < CONFIDENCE_THRESH:
            add_log(f"⚠ 상관 신뢰도 낮음 (conf={confidence:.3f}) → 건너뜀")
            push_state(STATUS_LOW_CONF, 0, total_correction_ms, log_lines, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        # raw → 버퍼 평균 → EMA 순으로 노이즈 내성 확보
        offset_buf.append(raw_ms)
        if len(offset_buf) < OFFSET_BUF_SIZE:
            add_log(f"📦 버퍼 수집 중 ({len(offset_buf)}/{OFFSET_BUF_SIZE}) raw={raw_ms:.0f}ms")
            push_state(STATUS_BUFFERING, raw_ms, total_correction_ms, log_lines, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        buf_avg = float(np.mean(offset_buf))
        if not ema_initialized:
            smoothed_offset = buf_avg
            ema_initialized = True
        else:
            smoothed_offset = EMA_ALPHA * buf_avg + (1.0 - EMA_ALPHA) * smoothed_offset
        add_log(f"📈 buf_avg={buf_avg:.1f}ms smoothed={smoothed_offset:.1f}ms")

        # ── 보정 실행 / 정상 판정 ────────────────────────────────────────────
        if abs(smoothed_offset) >= THRESH and hwnd:
            steps = min(int(abs(smoothed_offset) / STEP), MAX_STEPS)
            sign  = 1 if smoothed_offset > 0 else -1

            # 누적 보정 상한 초과 방지
            if abs(total_correction_ms + steps * STEP * sign) > MTM:
                steps = max(0, int((MTM - abs(total_correction_ms)) / STEP))
            if steps == 0:
                add_log(f"⚠ 싱크 상한 도달 (±{MTM}ms)")
                push_state(STATUS_CEILING, smoothed_offset, total_correction_ms, log_lines, pot_ok,
                           lip_n, aud_n, notify, oped_prompt)
                time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
                continue

            vk        = VK_OEM_PERIOD if smoothed_offset > 0 else VK_OEM_COMMA
            direction = "싱크 빠르게(오디오 늦춤)" if smoothed_offset > 0 else "싱크 느리게(오디오 당김)"
            add_log(f"🔧 보정: {direction} ×{steps} ({steps*STEP}ms) [평균={smoothed_offset:.1f}ms]")
            for _ in range(steps):
                post_key_to_potplayer(hwnd, vk, shift=True)
                time.sleep(0.01)

            total_correction_ms += steps * STEP * sign
            last_correction_t    = time.time()
            status = STATUS_CORRECTED
            # 조건 3: 싱크 보정 완료 → 1·2번 쿨다운 초기화 + 버퍼·큐 정리
            # 조건 4: 녹화 중이면 _flush_and_gc 내부에서 억제됨
            _no_pot_cleanup_t = 0.0
            _flush_and_gc("싱크 보정 완료")
            in_cooldown = True

        elif not hwnd:
            status = STATUS_NO_POT
        else:
            add_log(f"✅ 싱크 정상 (offset={smoothed_offset:.1f}ms, 임계값 ±{THRESH}ms 이내)")
            status = STATUS_OK
            last_correction_t = time.time()   # 정상 판정도 쿨다운 기산점 갱신
            # 조건 3: 싱크 정상 → 1·2번 쿨다운 초기화 + 버퍼·큐 정리
            # 조건 4: 녹화 중이면 _flush_and_gc 내부에서 억제됨
            _no_pot_cleanup_t = 0.0
            _flush_and_gc("싱크 정상 확인")
            in_cooldown = True

        push_state(status, smoothed_offset, total_correction_ms, log_lines, pot_ok,
                   lip_n, aud_n, notify, oped_prompt)
        time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
