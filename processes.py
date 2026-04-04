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
    get_video_fps,
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

def proc_lip_capture(lip_queue: Queue, stop_flag: Value, cfg: dict, stream_anchor=None):
    """
    팟플레이어 화면을 캡처해 입술 개구(開口) 신호를 추출하는 프로세스.

    타임스탬프: 오디오 스트림 기준 위치(초) — proc_audio_capture와 동일한 기준점.
                stream_anchor[qp_origin, sr, freq]를 읽어 qpc_now()를 변환.
                기준점 미확립 시 qpc_now() / freq 로 폴백.
    입술 위치:  얼굴 ROI 하위 절반에서 수평 평균 밝기가 가장 낮은 행을 동적 추정.
    개구 신호:  입술 ROI를 64×16으로 축소 후 열(列)별 세로 분산의 평균으로 계산.
                입이 열리면 치아(밝음)와 입술(어두움)의 대비로 분산이 커짐.
    """
    import cv2
    import sys

    base    = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    cascade = cv2.CascadeClassifier(os.path.join(base, "lbpcascade_animeface.xml"))

    _freq          = qpc_freq()
    interval       = 1.0 / cfg["CAPTURE_FPS"]
    DETECT_EVERY_N = 5          # N 프레임마다 얼굴 재탐지
    LIP_ROI_SIZE   = (64, 16)   # 입술 패치 리사이즈 크기

    last_roi           = None
    frame_count        = 0
    null_frame_count   = 0
    total_frame_count  = 0
    last_diag_time     = 0.0
    lip_y_ratio        = 0.70   # 얼굴 높이 대비 입술 시작 위치 (동적 갱신)
    lip_ratio_update_n = 0

    def _estimate_lip_y_ratio(face_gray):
        """얼굴 하위 50~92% 구간에서 가장 어두운 수평 띠를 입술 위치로 추정."""
        h            = face_gray.shape[0]
        search_start = int(h * 0.50)
        search_end   = int(h * 0.92)
        if search_end <= search_start:
            return 0.70
        row_means = face_gray[search_start:search_end].mean(axis=1)
        darkest   = int(np.argmin(row_means))
        return float(np.clip((search_start + darkest) / h - 0.05, 0.50, 0.85))

    while not stop_flag.value:
        t0   = time.perf_counter()
        hwnd = find_potplayer_hwnd()

        # 캡처 시작 직전에 QPC 틱 기록
        qp_now_val = qpc_now()
        raw  = capture_window(hwnd) if hwnd else None

        # stream_anchor 기준점이 확립되면 오디오와 동일한 스트림 위치(초)로 변환
        # 미확립 시 qpc_now() / freq 로 폴백 (오디오 첫 패킷 전 구간)
        if stream_anchor is not None and stream_anchor[0] > 0:
            qp_origin = stream_anchor[0]
            sr_anc    = stream_anchor[1]
            freq_anc  = stream_anchor[2]
            t_hw = (qp_now_val - qp_origin) / freq_anc
        else:
            t_hw = qp_now_val / _freq

        # 30초마다 진단 로그 전송
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
        frame_count += 1
        motion = 0.0

        # 얼굴 탐지 (매 DETECT_EVERY_N 프레임)
        if frame_count % DETECT_EVERY_N == 1 or last_roi is None:
            faces = cascade.detectMultiScale(
                cv2.equalizeHist(gray),
                scaleFactor=1.1, minNeighbors=10, minSize=(60, 60),
            )
            if len(faces):
                x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                last_roi = (x, y, fw, fh)
                lip_ratio_update_n += 1
                # 30회마다 입술 위치 재추정
                if lip_ratio_update_n % 30 == 1:
                    h_img, w_img = gray.shape
                    face_crop = gray[y:min(y+fh, h_img), x:min(x+fw, w_img)]
                    if face_crop.size > 0:
                        lip_y_ratio = _estimate_lip_y_ratio(face_crop)
            else:
                last_roi = None

        # 얼굴 감지된 프레임만 큐에 전송
        # 미감지 프레임의 motion=0이 섞이면 상관 신호가 오염되어 싱크 오탐 발생
        if last_roi is not None:
            x, y, fw, fh = last_roi
            h_img, w_img = gray.shape
            y1      = min(y + int(fh * lip_y_ratio),          h_img - 1)
            y2      = min(y + int(fh * (lip_y_ratio + 0.25)), h_img)
            lip_roi = gray[y1:y2, x:min(x+fw, w_img)]
            if lip_roi.size > 0:
                small  = cv2.resize(lip_roi, LIP_ROI_SIZE)
                motion = float(small.var(axis=0).mean()) / 255.0
            queue_put(lip_queue, (t_hw, motion))

        sleep_t = interval - (time.perf_counter() - t0)
        if sleep_t > 0:
            time.sleep(sleep_t)

def proc_analyzer(lip_queue: Queue, audio_queue: Queue,
                  state_queue: Queue, cmd_queue: Queue,
                  stop_flag: Value, cfg: dict,
                  shared_pos=None, shared_dur=None):
    """
    립 모션(lip)과 오디오 VAD를 교차상관으로 비교해 싱크 오프셋을 추정하고
    팟플레이어 오디오 딜레이를 자동 보정하는 프로세스.

    aub 튜플: (t_stream, rms, vad)
      t_stream: 오디오 스트림 기준 위치(초) — lip과 동일 기준축
      rms: OP/ED 음악 감지에 사용
      vad: 싱크 보정에 사용 (ZCR 기반 이진 신호)
    """
    from scipy.signal import correlate

    # ── 설정값 ────────────────────────────────────────────────────────────────
    BUF_SEC   = cfg["BUFFER_SEC"]
    THRESH    = cfg["SYNC_THRESHOLD_MS"]
    STEP      = cfg["POTPLAYER_STEP_MS"]
    MAX_STEPS = cfg["MAX_CORRECT_STEP"]
    INTERVAL  = cfg["ANALYSIS_INTERVAL"]
    MTM       = cfg["MAX_TOTAL_SYNC_MS"]
    OAS       = bool(cfg.get("OAS", cfg.get("OPED_AUTO_SKIP", False)))
    OSS       = int(cfg.get("OSS",  cfg.get("OPED_SKIP_SEC",  90)))

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

    # ── 상태 변수 ─────────────────────────────────────────────────────────────
    lpb = collections.deque()
    aub = collections.deque()

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

    # OP/ED 상태를 오프닝/엔딩 구간별로 독립 관리
    # 단일 변수로 관리하면 오프닝에서 소진 후 엔딩 감지가 차단되는 버그 발생
    oped_confirm  = {"오프닝": 0,     "엔딩": 0}
    oped_last_t   = {"오프닝": 0.0,   "엔딩": 0.0}
    oped_prompted = {"오프닝": False,  "엔딩": False}
    last_cd_log   = 0.0   # 쿨다운 로그 스로틀
    last_rms_log  = 0.0   # RMS 진단 로그 스로틀
    pending_prompt = [None]

    def add_log(msg: str):
        log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    # ── OP/ED: 음악 재생 여부 판단 ────────────────────────────────────────────
    def is_music_playing() -> bool:
        if len(aub) < 10:
            return False
        # aub 타임스탬프는 스트림 위치 기준 — 최신 항목에서 윈도우만큼 이전
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

    # ── 큐 드레인 + 만료 항목 제거 ────────────────────────────────────────────
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
        if lpb:
            latest_lip = lpb[-1][0]
            while lpb and latest_lip - lpb[0][0] > MUSIC_WINDOW_SEC:
                lpb.popleft()
        if aub:
            latest_aud = aub[-1][0]
            while aub and latest_aud - aub[0][0] > MUSIC_WINDOW_SEC:
                aub.popleft()

    # ── 립·오디오 버퍼를 공통 시간축으로 리샘플 ──────────────────────────────
    def resample_aligned(lip_buf, aud_buf, fps):
        """
        두 버퍼를 겹치는 시간 구간에서 fps 간격으로 리샘플.
        반환: (lip_sig, vad_sig) — 겹침 구간이 1초 미만이면 (None, None)
        """
        if len(lip_buf) < 2 or len(aud_buf) < 2:
            return None, None

        lip_ts = np.fromiter((x[0] for x in lip_buf), dtype=np.float64, count=len(lip_buf))
        aud_ts = np.fromiter((x[0] for x in aud_buf), dtype=np.float64, count=len(aud_buf))
        lip_vs = np.fromiter((x[1] for x in lip_buf), dtype=np.float32, count=len(lip_buf))
        aud_vs = np.fromiter((x[2] for x in aud_buf), dtype=np.float32, count=len(aud_buf))  # vad

        t_start = max(lip_ts[0], aud_ts[0])
        t_end   = min(lip_ts[-1], aud_ts[-1])

        if t_end - t_start < 1.0:
            return None, None

        n_samples = int((t_end - t_start) * fps)
        if n_samples < fps:
            return None, None

        t_grid  = np.linspace(t_start, t_end, n_samples)
        lip_sig = np.interp(t_grid, lip_ts, lip_vs)
        vad_sig = np.interp(t_grid, aud_ts, aud_vs)
        return lip_sig, vad_sig

    # ── 교차상관으로 싱크 오프셋 추정 ─────────────────────────────────────────
    def to_binary(signal, ratio):
        """중앙값 + ratio×표준편차를 임계값으로 이진화."""
        thresh = np.median(signal) + ratio * signal.std()
        return (signal > thresh).astype(np.float32)

    def normalize(x):
        x = x - x.mean()
        s = x.std()
        return x / s if s > 1e-9 else x

    def compute_offset(lip, vad, fps):
        """
        lip·vad 신호를 정규화 후 교차상관으로 lag(ms) 추정.
        파라볼라 보간으로 서브샘플 정밀도 확보.

        lip: 연속값 → to_binary로 이진화
        vad: 이미 0/1 이진 신호 → round()로 스냅 (to_binary 재적용 시 신호 왜곡)

        lag > 0: 오디오가 립보다 빠름 → 오디오 늦춰야 함 (Shift+.)
        lag < 0: 오디오가 립보다 느림 → 오디오 당겨야 함 (Shift+,)
        반환: (lag_ms, lip_bin_std, vad_std, lip_mean, vad_mean, confidence)
               confidence: 정규화 상관계수 peak값 (0~1), 낮으면 신뢰 불가
        """
        lip_bin = to_binary(lip, ratio=0.5)
        lip_sig = normalize(lip_bin) if lip_bin.std() >= 1e-9 else normalize(lip)

        # vad는 interp로 생긴 0~1 중간값을 반올림해 이진으로 복원 후 정규화
        vad_snapped = np.round(vad).astype(np.float32)
        vad_sig     = normalize(vad_snapped) if vad_snapped.std() >= 1e-9 else normalize(vad)

        # 탐색 범위를 MAX_TOTAL_SYNC_MS 이내로 제한해 노이즈 peak 방지
        max_lag_samples = int(MTM / 1000.0 * fps)
        corr   = correlate(lip_sig, vad_sig, mode="full")
        center = len(vad_sig) - 1
        lo     = max(0, center - max_lag_samples)
        hi     = min(len(corr), center + max_lag_samples + 1)

        sub_corr = corr[lo:hi]
        peak_idx = int(np.argmax(sub_corr)) + lo

        # 정규화 상관계수로 신뢰도 산출 (신호 에너지 대비 peak 크기)
        energy    = np.sqrt(np.sum(lip_sig**2) * np.sum(vad_sig**2))
        confidence = float(corr[peak_idx] / energy) if energy > 1e-9 else 0.0

        if lo < peak_idx < hi - 1:
            y0, y1, y2 = corr[peak_idx-1], corr[peak_idx], corr[peak_idx+1]
            denom = 2*y1 - y0 - y2
            sub   = 0.5 * (y2 - y0) / denom if abs(denom) > 1e-9 else 0.0
            lag   = (peak_idx + sub) - center
        else:
            lag = peak_idx - center

        return lag / fps * 1000, lip_bin.std(), vad_snapped.std(), lip.mean(), vad.mean(), confidence

    # ── 상태 큐 전송 ──────────────────────────────────────────────────────────
    _last_log_snapshot = [None]

    def push_state(status, offset, correction, logs, pot_ok, lip_n, aud_n,
                   notify=None, oped_prompt=None):
        snap = _last_log_snapshot[0]
        if snap is None or len(snap) != len(logs) or (logs and snap[-1] != logs[-1]):
            snap = list(logs)
            _last_log_snapshot[0] = snap
        if oped_prompt is not None:
            pending_prompt[0] = oped_prompt
        # 전송 후 즉시 클리어 — 큐 잔류로 팝업 중복 호출 방지
        prompt_to_send    = pending_prompt[0]
        pending_prompt[0] = None
        queue_put(state_queue, dict(
            status=status, offset_ms=offset, correction_ms=correction,
            log_lines=snap, potplayer_ok=pot_ok,
            lip_samples=lip_n, audio_samples=aud_n,
            notify=notify, oped_prompt=prompt_to_send,
        ))

    # ── OP/ED 스킵 실행 ───────────────────────────────────────────────────────
    def execute_skip() -> bool:
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
            fmt = lambda ms: f"{ms//1000//60}:{ms//1000%60:02d}"
            add_log(f"⏭ 스킵 ({OSS}초): {fmt(pos)} → {fmt(new_pos)}")
            return True
        except Exception as e:
            add_log(f"⚠ 스킵 실패: {e}")
            return False

    # ── 상태 리셋 헬퍼 ────────────────────────────────────────────────────────
    def reset_sync():
        nonlocal total_correction_ms, smoothed_offset, ema_initialized, last_correction_t
        total_correction_ms = 0
        smoothed_offset     = 0.0
        ema_initialized     = False
        last_correction_t   = 0.0
        offset_buf.clear()

    def reset_oped():
        nonlocal oped_confirm, oped_last_t, oped_prompted
        oped_confirm  = {"오프닝": 0,     "엔딩": 0}
        oped_last_t   = {"오프닝": 0.0,   "엔딩": 0.0}
        oped_prompted = {"오프닝": False,  "엔딩": False}
        pending_prompt[0] = None

    # ── 메인 루프 ─────────────────────────────────────────────────────────────
    time.sleep(BUF_SEC)

    while not stop_flag.value:
        t0 = time.perf_counter()

        # 커맨드 처리
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
                    reset_sync()
                    add_log("↺ 싱크 초기화")
                else:
                    add_log("⚠ 팟플레이어 미감지")
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
                stop_flag.value = True
                return

        drain_queues()

        hwnd   = find_potplayer_hwnd()
        pot_ok = bool(hwnd)
        lip_n  = len(lpb)
        aud_n  = len(aub)

        # 영상 변경 감지 → 전체 상태 초기화
        if hwnd:
            try:
                tbuf = ctypes.create_unicode_buffer(512)
                _user32.GetWindowTextW(hwnd, tbuf, 512)
                cur_title = tbuf.value.strip()
                if cur_title and cur_title != prev_title and prev_title:
                    post_key_to_potplayer(hwnd, VK_OEM_2, shift=True)
                    time.sleep(0.05)
                    post_key_to_potplayer(hwnd, 0x6F, shift=True)
                    lpb.clear()
                    aub.clear()
                    reset_sync()
                    reset_oped()
                    video_fps = get_video_fps(hwnd)
                    add_log(f"🔄 영상 변경 → 전체 초기화 (fps={video_fps})")
                prev_title = cur_title
            except Exception:
                pass

        # 오디오 미감지 경고 (1회)
        if aud_n == 0 and lip_n > 10 and not audio_warned:
            audio_warned = True
            add_log("⚠ 오디오 미감지 — 팟플레이어 재생 중인지 확인하세요")

        notify      = None
        oped_prompt = None

        # 오디오 최초 감지 알림 + fps 갱신
        if not audio_det and aud_n > 5:
            audio_det = True
            video_fps = get_video_fps(hwnd)
            notify = ("🎬 동영상 재생 감지", "팟플레이어에서 동영상 재생이 감지되었습니다.\n싱크 분석을 시작합니다.")
            add_log(f"🎬 동영상 재생 감지 (fps={video_fps})")

        # OP/ED 구간 감지 및 스킵
        pos = shared_pos.value if shared_pos else -1
        dur = shared_dur.value if shared_dur else -1

        if pos >= 0 and dur > 0:
            # 짧은 영상에서 오프닝/엔딩이 동시에 True가 되지 않도록
            # 영상 전반부는 오프닝, 후반부는 엔딩으로만 판정
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

        # 데이터 부족
        if aud_n < 10 or (lip_n < 10 and not has_prompt):
            push_state("데이터 수집 중", 0, total_correction_ms, log_lines, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        lip_sig, vad_sig = resample_aligned(lpb, aub, video_fps)

        if lip_sig is None or vad_sig is None:
            if has_prompt:
                push_state("데이터 수집 중", 0, total_correction_ms, log_lines, pot_ok,
                           lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        # 1차 신호 품질 체크 — flat 신호가 correlate에 들어가면 bogus lag 발생
        pre_lip_std = float(lip_sig.std())
        pre_vad_std = float(vad_sig.std())
        if pre_lip_std < 0.05 or pre_vad_std < 0.05:
            add_log(f"⚠ 신호 불충분 (lip_std={pre_lip_std:.3f} vad_std={pre_vad_std:.3f}) → 생략")
            push_state("신호 부족", 0, total_correction_ms, log_lines, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        raw_ms, lip_std, vad_std, lip_mean, vad_mean, confidence = compute_offset(lip_sig, vad_sig, video_fps)

        add_log(f"📊 raw={raw_ms:.0f}ms lip_std={lip_std:.3f} vad_std={vad_std:.3f} "
                f"lip_mean={lip_mean:.3f} vad_mean={vad_mean:.3f} conf={confidence:.3f} n={len(lip_sig)}")

        # 얼굴은 잡혔지만 입 움직임이 없는 구간 (대사 없음)
        if lip_mean < 0.4:
            push_state("미감지", 0, total_correction_ms, log_lines, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(1.0)
            continue

        # 2차 신호 품질 체크 (이진화 후)
        if lip_std < 0.05 or vad_std < 0.05:
            add_log(f"⚠ 신호 불충분 (lip_std={lip_std:.3f} vad_std={vad_std:.3f}) → 건너뜀")
            push_state("신호 부족", 0, total_correction_ms, log_lines, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        # 상관 신뢰도 체크 — peak가 낮으면 lip·vad 패턴이 맞지 않는 구간
        if confidence < CONFIDENCE_THRESH:
            add_log(f"⚠ 상관 신뢰도 낮음 (conf={confidence:.3f}) → 건너뜀")
            push_state("신뢰도 부족", 0, total_correction_ms, log_lines, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        # raw → 버퍼 평균 → EMA 순으로 노이즈 내성 확보
        offset_buf.append(raw_ms)
        if len(offset_buf) < OFFSET_BUF_SIZE:
            add_log(f"📦 버퍼 수집 중 ({len(offset_buf)}/{OFFSET_BUF_SIZE}) raw={raw_ms:.0f}ms")
            push_state("버퍼 수집 중", raw_ms, total_correction_ms, log_lines, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        buf_avg = float(np.mean(offset_buf))
        add_log(f"📦 버퍼 평균={buf_avg:.1f}ms (최근 {OFFSET_BUF_SIZE}회)")

        if not ema_initialized:
            smoothed_offset = buf_avg
            ema_initialized = True
        else:
            smoothed_offset = EMA_ALPHA * buf_avg + (1.0 - EMA_ALPHA) * smoothed_offset

        add_log(f"📈 smoothed={smoothed_offset:.1f}ms (EMA α={EMA_ALPHA})")

        # 보정 쿨다운 체크
        cooldown_remain = SYNC_COOLDOWN_SEC - (time.time() - last_correction_t)
        if cooldown_remain > 0:
            add_log(f"⏳ 보정 쿨다운 {cooldown_remain:.1f}초 남음 → 건너뜀")
            push_state("쿨다운 중", smoothed_offset, total_correction_ms, log_lines, pot_ok,
                       lip_n, aud_n, notify, oped_prompt)
            time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
            continue

        # 보정 실행
        if abs(smoothed_offset) >= THRESH and hwnd:
            steps = min(int(abs(smoothed_offset) / STEP), MAX_STEPS)
            sign  = 1 if smoothed_offset > 0 else -1

            # 누적 보정 상한 초과 방지
            if abs(total_correction_ms + steps * STEP * sign) > MTM:
                steps = max(0, int((MTM - abs(total_correction_ms)) / STEP))
            if steps == 0:
                add_log(f"⚠ 싱크 상한 도달 (±{MTM}ms)")
                push_state("상한 도달", smoothed_offset, total_correction_ms, log_lines, pot_ok,
                           lip_n, aud_n, notify, oped_prompt)
                time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
                continue

            vk        = VK_OEM_PERIOD if smoothed_offset > 0 else VK_OEM_COMMA
            direction = "빠르게(오디오 늦춤)" if smoothed_offset > 0 else "느리게(오디오 당김)"
            add_log(f"보정: {direction} ×{steps} ({steps*STEP}ms) [평균={smoothed_offset:.1f}ms]")
            for _ in range(steps):
                post_key_to_potplayer(hwnd, vk, shift=True)
                time.sleep(0.01)

            total_correction_ms += steps * STEP * sign
            last_correction_t    = time.time()
            offset_buf.clear()
            add_log(f"⏳ 보정 완료 → {SYNC_COOLDOWN_SEC:.0f}초 쿨다운 시작")
            status = "보정 완료"
        elif not hwnd:
            status = "팟플레이어 미감지"
        else:
            status = "정상"

        push_state(status, smoothed_offset, total_correction_ms, log_lines, pot_ok,
                   lip_n, aud_n, notify, oped_prompt)
        time.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))
