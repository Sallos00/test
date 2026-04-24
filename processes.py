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

# ── [영상 해시 학습] 신규 모듈 — 모듈 로드 실패가 T3를 죽이지 않도록 안전하게 import ──
# top-level import 대신 함수 내부 lazy import를 사용한다.
# 이유: processes.py는 run_process.py에서 lazy import(`from processes import ...`)되는데,
#   이 시점에 상단 import가 실행되므로 신규 모듈 중 하나라도 실패하면
#   proc_analyzer 함수를 가져오지 못해 T3 스레드가 시작조차 못하고 종료된다.
# 해결: 실제로 사용하는 proc_analyzer 함수 내부에서 lazy import → import 실패 시
#   해당 호출만 건너뛰고 스레드는 계속 실행된다.
_HASH_SIM_THRESHOLD = 0.85   # 영상 해시 유사도 임계값 (이 이상이면 동일 OP/ED 판정)


def _import_hash_modules():
    """
    영상 해시 관련 모듈을 안전하게 lazy import.

    Returns:
        (generate_video_hash, compare_video_hash,
         load_db, save_db, get_series, prune_series,
         make_path_key)
        import 실패 시 None 튜플 반환.
    """
    try:
        from video_hash import generate_video_hash
        from similarity import compare_video_hash
        from db_manager import load_db, save_db, get_series, prune_series
        from series_key import make_path_key
        return (generate_video_hash, compare_video_hash,
                load_db, save_db, get_series, prune_series,
                make_path_key)
    except Exception as _ie:
        return (None,) * 7

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

    def _estimate_lip_y_ratio(face_gray):
        """얼굴 하위 55~82% 구간에서 가장 어두운 행 → 입술 위치 추정.

        기존 50~92% 범위는 아래 문제를 유발:
        - 50~60%: 눈/쌍꺼풀 그림자까지 포함 → 잘못된 위치 반환 가능
        - 82~92%: 턱 라인/목 그림자 → 실제 입술보다 낮은 위치 반환

        lbpcascade_animeface 바운딩박스 기준 입술은 대략 65~80% 위치에 분포.
        55~82% 범위로 좁혀 눈·턱 그림자 영향을 최소화한다.
        """
        h            = face_gray.shape[0]
        search_start = int(h * 0.55)
        search_end   = int(h * 0.82)
        if search_end <= search_start:
            return 0.70
        row_means = face_gray[search_start:search_end].mean(axis=1)
        darkest   = int(np.argmin(row_means))
        return float(np.clip((search_start + darkest) / h - 0.02, 0.55, 0.80))

    while not stop_flag.is_set():
        t0   = time.perf_counter()

        # hwnd는 1초마다만 재조회 (find_potplayer_hwnd 비용 절감)
        _t_now = time.perf_counter()
        if _t_now - _hwnd_cache_t >= 1.0:
            _cached_hwnd = find_potplayer_hwnd()
            _hwnd_cache_t = _t_now
        hwnd = _cached_hwnd

        # [Fix] capture_window 실행 후 QPC를 측정해야 립 타임스탬프가 정확.
        # capture_window(PrintWindow)는 10~50ms 소요되므로, 캡처 전에 측정하면
        # 립 타임스탬프가 조기에 찍혀 "립이 오디오보다 앞서 발생"으로 오인됨.
        # → 교차상관에서 lag>0(오디오가 앞섬) 판정이 지속되어 오디오 딜레이가
        #   보정할수록 계속 증가하는 피드백 루프 발생. 캡처 완료 후 측정으로 수정.
        raw  = capture_window(hwnd) if hwnd else None
        qp_now_val = qpc_now()

        # [Bug B 수정] 앵커 미확립 시 절대 QPC(~3600s)를 t_hw로 사용 금지.
        # 이전: 앵커 없으면 t_hw = qpc_now/_freq (시스템 부팅 후 경과초, 수천 초)
        #   → 앵커 확립 후 t_hw~0s 값과 Pipe에서 공존.
        #   trim(latest-oldest>window)은 역전(음수)이라 절대 미작동
        #   → drain=10개/3s 기준 최대 94초 오염 지속, 교차상관 피크 랜덤화.
        # 수정: 앵커 없으면 t_hw=None → lip_queue에 적재 안 함.
        #   P1은 캡처·얼굴탐지 워밍업 계속하되 타임스탬프 오염 없음.
        if stream_anchor is not None and stream_anchor[0] > 0:
            qp_origin = stream_anchor[0]
            sr_anc    = stream_anchor[1]
            freq_anc  = stream_anchor[2]
            t_hw = (qp_now_val - qp_origin) / freq_anc
        else:
            t_hw = None  # 앵커 미확립 → 이 프레임은 큐에 넣지 않음

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
            gray_eq    = cv2.equalizeHist(gray_small)
            faces = cascade.detectMultiScale(
                gray_eq,
                scaleFactor=1.1, minNeighbors=10, minSize=(30, 30),
            )
            del gray_small, gray_eq  # 탐지 직후 즉시 해제 (캡처 해상도 50% 배열 누적 방지)
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
            # [Bug B 수정] 앵커 확립 후(t_hw is not None)에만 Pipe에 전송
            if t_hw is not None:
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
    MUSIC_MAX_CV      = 0.65     # 변동계수 상한 (0.8→0.65 강화: 나레이션+BGM 오탐 방지)
    MUSIC_MIN_FILL    = 0.75     # RMS > 평균×0.5 비율 하한 (0.70→0.75 강화)
    MUSIC_CONFIRM     = 5        # 연속 감지 횟수 기준 (2→5: 약 15초 필요)
    MUSIC_MIN_CONT_SEC = 15.0    # OP/ED 확정을 위한 최소 연속 음악 감지 시간(초)
    # 줄거리 요약·예고편은 BGM+나레이션 조합이지만 지속 시간이 짧거나 RMS 변동이 크다.
    # CONFIRM 횟수(×INTERVAL)와 최소 지속 시간을 함께 검사해 오탐률을 낮춘다.

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
    oped_music_start_t = {"오프닝": 0.0, "엔딩": 0.0}  # 연속 음악 감지 시작 시각
    # ── [영상 해시 학습] 해시 상태 추적 ──────────────────────────────────────
    # oped_hash_done : 현재 감지 이벤트에서 해시 생성·DB 갱신 완료 여부
    # oped_hash_mc   : DB 조회 결과 match_count (0=미조회, 1=1화, 2+=확정)
    oped_hash_done = {"오프닝": False, "엔딩": False}
    oped_hash_mc   = {"오프닝": 0,     "엔딩": 0}
    last_cd_log   = 0.0   # 쿨다운 로그 스로틀
    last_rms_log  = 0.0   # RMS 진단 로그 스로틀
    pending_prompt = [None]
    # [Bug 2 수정] oped_skip/oped_no_skip 수신 시 pending_prompt[0]은 이미 push_state에서
    # 소진된 상태이므로 zone 정보를 별도로 보존해야 함.
    _last_oped_zone = [None]  # 마지막으로 팝업을 전송한 OP/ED zone

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
        del vals  # 리스트 참조 해제
        mean_rms = float(arr.mean())
        if mean_rms < MUSIC_MIN_RMS:
            del arr
            return False
        cv   = float(arr.std() / mean_rms) if mean_rms > 1e-9 else 999.0
        fill = float((arr > mean_rms * 0.5).sum()) / len(arr)
        del arr  # numpy 배열 즉시 해제
        return cv < MUSIC_MAX_CV and fill > MUSIC_MIN_FILL

    def drain_queues():
        """lip_queue(Pipe)와 audio_queue(threading.Queue)를 드레인해 lpb/aub에 적재.

        [drain 상한 설정]
        이전 수정(while True 무제한)은 threading.Queue 특성상 직렬화 비용 없이
        순식간에 대량 처리가 가능해 비정상 상황(zombie T2, queue 폭발)에서
        Python heap에 수천 개 객체가 한번에 올라와 GC 압박 → 메모리 스파이크.

        mp.Queue(백업)와 달리 threading.Queue는 inter-process 직렬화 비용이 없어
        자연적인 속도 제한이 없으므로 명시적 상한 필요.

        상한 계산:
          - 오디오: 94pkt/s × 3s(INTERVAL) × 2(여유) = 564
          - 립:     15fps  × 3s(INTERVAL) × 3(여유) = 135
          → 정상 사이클에서는 항상 빈 큐가 될 때 break로 먼저 종료됨.
            상한은 비정상 상황(queue 폭발)에 대한 메모리 안전장치.
        """
        # ── 드레인 상한 ───────────────────────────────────────────────────────
        # 정상: 3초 사이클당 립=45개, 오디오=282개 → 상한 전에 큐가 비워짐
        # 비정상(폭발 상황): 상한에서 멈춰 GC 압박/메모리 스파이크 방지
        _LIP_DRAIN_LIMIT   = 135   # 15fps × 3s × 3배 여유
        _AUDIO_DRAIN_LIMIT = 564   # 94pkt/s × 3s × 2배 여유

        # ── lip_queue(Pipe) 드레인 ───────────────────────────────────────────
        _lip_is_pipe = hasattr(lip_queue, 'poll') and not hasattr(lip_queue, 'get_nowait')
        for _ in range(_LIP_DRAIN_LIMIT):
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
            except (EOFError, OSError):
                break
            except Exception:
                break

        # ── audio_queue(threading.Queue) 드레인 ─────────────────────────────
        for _ in range(_AUDIO_DRAIN_LIMIT):
            try:
                item = audio_queue.get_nowait()
                if isinstance(item, tuple) and len(item) == 2 and item[0] == "LOG":
                    log_lines.append(f"🔊 {item[1]}")
                else:
                    aub.append(item)
            except Exception:
                break

        # ── 타임스탬프 윈도우 트리밍 ─────────────────────────────────────────
        # [추가] 역전 감지: Bug B 잔재로 lpb[-1] < lpb[0]인 경우(앵커 전 절대
        # QPC 항목이 남은 경우) trim 조건이 음수가 돼 절대 미작동.
        # 역전이 감지되면 lpb 전체 초기화.
        _trim_win = max(BUF_SEC, MUSIC_WINDOW_SEC)
        if lpb:
            latest_lip = lpb[-1][0]
            oldest_lip = lpb[0][0]
            if latest_lip < oldest_lip:
                add_log(
                    f"⚠ [버퍼] lpb 타임스탬프 역전 "
                    f"(oldest={oldest_lip:.1f}s > latest={latest_lip:.1f}s) → 초기화"
                )
                lpb.clear()
            else:
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
            del lip_ts, aud_ts, lip_vs, rms_vs, vad_vs, rms_diff, aud_vs
            return None, None

        n_samples = int((t_end - t_start) * fps)
        if n_samples < fps:
            del lip_ts, aud_ts, lip_vs, rms_vs, vad_vs, rms_diff, aud_vs
            return None, None

        t_grid  = np.linspace(t_start, t_end, n_samples)
        lip_sig = np.interp(t_grid, lip_ts, lip_vs)
        aud_sig = np.interp(t_grid, aud_ts, aud_vs)
        # 중간 배열 즉시 해제 (수 MB 규모 누적 방지)
        del lip_ts, aud_ts, lip_vs, rms_vs, vad_vs, rms_diff, aud_vs, t_grid
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

        result = (lag / fps * 1000, lip_bin.std(), aud.std(), lip.mean(), aud.mean(), confidence)
        # 교차상관 배열은 신호 길이의 2배 크기 → 즉시 해제
        del corr, sub_corr, lip_bin, lip_sig, aud_sig
        return result

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
        oped_music_start_t["오프닝"] = 0.0
        oped_music_start_t["엔딩"]   = 0.0
        pending_prompt[0] = None
        _last_oped_zone[0] = None  # [Bug 2 수정] 초기화 시 zone 정보도 클리어
        # ── [영상 해시 학습] 해시 상태도 초기화 ──────────────────────────────
        oped_hash_done["오프닝"] = False
        oped_hash_done["엔딩"]   = False
        oped_hash_mc["오프닝"]   = 0
        oped_hash_mc["엔딩"]     = 0

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

    # 메모리 누수 방지: 주기적 GC — 보정/정상 판정 시 _flush_and_gc가 호출되지만
    # 장시간 수집 중 상태(STATUS_COLLECTING, STATUS_LOW_CONF 등)에서는
    # 순환 참조 객체가 누적될 수 있음. 120사이클(~6분)마다 한 번 gc.collect() 수행.
    _gc_cycle_count = 0
    _GC_PERIOD = 120

    while not stop_flag.is_set():
        t0 = time.perf_counter()

        # 주기적 GC: 장시간 대기/수집 구간에서 순환 참조 객체 누적 방지
        _gc_cycle_count += 1
        if _gc_cycle_count >= _GC_PERIOD:
            _gc_cycle_count = 0
            import gc as _gc
            _gc.collect()

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
                # [Bug 2 수정] pending_prompt[0]은 push_state()에서 이미 소진된 상태.
                # _last_oped_zone으로 zone을 추적해 올바른 쿨다운 키를 사용한다.
                zone = _last_oped_zone[0] or "오프닝"
                _last_oped_zone[0] = None
                oped_confirm[zone]  = 0
                oped_music_start_t[zone] = 0.0
                oped_last_t[zone]   = time.time()
                oped_prompted[zone] = False
                pending_prompt[0]   = None
                # ── [영상 해시 학습] 스킵 완료 시 해시 상태 초기화
                oped_hash_done[zone] = False
                oped_hash_mc[zone]   = 0
                add_log(f"⏭ {zone} 스킵 완료 → 쿨다운 {OPED_COOLDOWN_SEC}초")
            elif cmd == "oped_no_skip":
                # [Bug 2 수정] oped_skip과 동일하게 _last_oped_zone으로 zone 추적
                zone = _last_oped_zone[0] or "오프닝"
                _last_oped_zone[0] = None
                oped_confirm[zone]  = 0
                oped_music_start_t[zone] = 0.0
                oped_last_t[zone]   = time.time()
                oped_prompted[zone] = False
                pending_prompt[0]   = None
                # ── [영상 해시 학습] 스킵 건너뜀 시 해시 상태 초기화
                oped_hash_done[zone] = False
                oped_hash_mc[zone]   = 0
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
            cooled  = (time.time() - oped_last_t[zone]) > OPED_COOLDOWN_SEC if in_zone else False

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
                            del vals  # 리스트 참조 해제
                            mean = float(arr.mean())
                            cv   = float(arr.std() / mean) if mean > 1e-9 else 999.0
                            fill = float((arr > mean * 0.5).sum()) / len(arr)
                            del arr  # numpy 배열 즉시 해제
                            add_log(f"🔍 {zone} rms={mean:.4f} cv={cv:.2f} fill={fill:.2f} "
                                    f"music={music} confirm={oped_confirm[zone]}/{MUSIC_CONFIRM}")

                if music:
                    now_music = time.time()
                    # 연속 감지 시작 시각 기록 (처음 감지되는 시점)
                    if oped_confirm[zone] == 0:
                        oped_music_start_t[zone] = now_music
                    oped_confirm[zone] += 1
                    cont_sec = now_music - oped_music_start_t[zone]
                    add_log(f"🎵 {zone} 음악 감지 ({oped_confirm[zone]}/{MUSIC_CONFIRM}회, "
                            f"지속={cont_sec:.0f}s/{MUSIC_MIN_CONT_SEC:.0f}s)")
                    # CONFIRM 횟수 + 최소 연속 지속 시간 모두 충족해야 OP/ED 후보 확정.
                    # 줄거리 요약·예고편은 통상 15초 미만이므로 오탐 방지.
                    if (oped_confirm[zone] >= MUSIC_CONFIRM
                            and cont_sec >= MUSIC_MIN_CONT_SEC):
                        # ── [영상 해시 학습] 1단계: 오디오 후보 탐지 완료 ──────────────
                        # 2단계(시간 기반 안정성)는 MUSIC_CONFIRM×INTERVAL(≥15초)로 이미 통과.
                        # 3단계: 영상 해시 생성 → DB 매칭 → match_count 기반 확정.
                        if not oped_hash_done[zone]:
                            oped_hash_done[zone] = True
                            try:
                                # ── lazy import: 실패해도 T3 스레드는 계속 실행 ──
                                (_gen_hash, _cmp_hash,
                                 _load_db, _save_db, _get_series, _prune,
                                 _mk_key) = _import_hash_modules()

                                if _gen_hash is None:
                                    # 모듈 import 실패 → 1화 취급, 팝업만 표시
                                    oped_hash_mc[zone] = 1
                                    add_log(f"⚠ [{zone}] 해시 모듈 import 실패 → 1화 취급")
                                else:
                                    # 현재 재생 구간 정보
                                    _seg_end_ms   = pos
                                    _seg_start_ms = max(0, int(pos - cont_sec * 1000))
                                    _path_key     = _mk_key(prev_title)

                                    add_log(f"🧩 [{zone}] 영상 해시 생성 중 "
                                            f"({_seg_start_ms//1000}s~{_seg_end_ms//1000}s, "
                                            f"key='{_path_key}')")

                                    _vhash = _gen_hash(
                                        prev_title,   # 창 제목 → 실제 파일 경로 탐색 후 폴백: 창 캡처
                                        _seg_start_ms,
                                        _seg_end_ms,
                                        hwnd,
                                    )

                                    if _vhash:
                                        _db     = _load_db()
                                        _series = _get_series(_db, _path_key)
                                        _matched = False

                                        for _item in _series[zone]:
                                            _sim = _cmp_hash(
                                                _vhash,
                                                _item.get("video_hash", [])
                                            )
                                            if _sim >= _HASH_SIM_THRESHOLD:
                                                _item["match_count"] = \
                                                    _item.get("match_count", 1) + 1
                                                oped_hash_mc[zone] = _item["match_count"]
                                                _matched = True
                                                add_log(
                                                    f"✅ [{zone}] 해시 매칭 "
                                                    f"sim={_sim:.3f} → "
                                                    f"match_count={oped_hash_mc[zone]}"
                                                )
                                                break

                                        if not _matched:
                                            _series[zone].append({
                                                "video_hash":  _vhash,
                                                "match_count": 1,
                                            })
                                            oped_hash_mc[zone] = 1
                                            _prune(_series, zone)
                                            add_log(f"🆕 [{zone}] 해시 신규 등록 "
                                                    f"(match_count=1)")

                                        _save_db(_db)
                                        del _db, _series, _vhash
                                    else:
                                        oped_hash_mc[zone] = 1
                                        add_log(f"⚠ [{zone}] 해시 생성 실패 → 1화 취급")

                            except Exception as _he:
                                oped_hash_mc[zone] = 1
                                add_log(f"⚠ [{zone}] 해시 처리 오류: {_he}")

                        # ── 스킵 실행 조건 결정 ─────────────────────────────────────
                        _mc = oped_hash_mc[zone]

                        if OAS:
                            # ━━ 자동스킵 모드: _mc 관계없이 항상 스킵 시도 ━━
                            # _mc는 학습 신뢰도를 나타내지만, OAS=True면 오디오
                            # 확정(MUSIC_CONFIRM + 연속 시간)만으로 충분히 신뢰.
                            # _mc=1(1화)이어도 사용자가 자동스킵을 선택했으므로 스킵.
                            if execute_skip():
                                oped_confirm[zone]       = 0
                                oped_music_start_t[zone] = 0.0
                                oped_hash_done[zone]     = False
                                oped_hash_mc[zone]       = 0
                                oped_last_t[zone]        = time.time()
                                add_log(f"⏭ {zone} 자동스킵 완료 "
                                        f"(match={_mc}) → 쿨다운 {OPED_COOLDOWN_SEC}초")
                            elif not oped_prompted[zone]:
                                # execute_skip() 실패(hwnd 없음 등) → 팝업 폴백
                                oped_prompt         = {"zone": zone, "skip_sec": OSS}
                                oped_prompted[zone] = True
                                _last_oped_zone[0]  = zone
                                add_log(f"⚠ {zone} 자동스킵 실패 → 팝업 폴백 (match={_mc})")
                        else:
                            # ━━ 수동 모드: _mc 기반으로 팝업 표시 ━━
                            if not oped_prompted[zone]:
                                oped_prompt         = {"zone": zone, "skip_sec": OSS}
                                oped_prompted[zone] = True
                                _last_oped_zone[0]  = zone
                                if _mc >= 2:
                                    add_log(f"🎵 {zone} 팝업 전송 (확정, match={_mc})")
                                else:
                                    add_log(f"🎵 {zone} 팝업 전송 (1화 확률 기반, match={_mc})")
                elif oped_confirm[zone] > 0:
                    oped_confirm[zone] -= 1
                    # 음악 감지가 끊기면 연속 시작 시각도 초기화
                    if oped_confirm[zone] == 0:
                        oped_music_start_t[zone] = 0.0
                        # ── [영상 해시 학습] 음악 감지 초기화 시 해시 상태도 리셋
                        # (다음 감지 이벤트에서 해시를 새로 생성하기 위함)
                        oped_hash_done[zone] = False
                        oped_hash_mc[zone]   = 0

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
            # [Bug 2 수정] oped prompt 유무와 무관하게 항상 상태를 전송.
            # 이전에는 has_prompt가 False이면 state_queue에 아무것도 보내지 않아
            # GUI가 stale 상태(이전 badge/status)를 계속 표시하는 문제가 있었음.
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
        # del 전에 len 값 보존
        _n_sig = len(lip_sig)

        add_log(f"📊 raw={raw_ms:.0f}ms lip_std={lip_std:.3f} aud_std={aud_std:.4f} "
                f"lip_mean={lip_mean:.4f} conf={confidence:.3f} n={_n_sig}")
        del lip_sig, aud_sig  # 분석 완료 → 대형 신호 배열 즉시 해제

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

            # [버그 수정] VK 키 방향 교정
            # Shift+, (VK_OEM_COMMA)  = 오디오 뒤로 미룸(늦춤)
            # Shift+. (VK_OEM_PERIOD) = 오디오 앞으로 당김
            # smoothed_offset > 0 : 오디오가 립보다 앞서 있음 → 뒤로 밀어야 함 → VK_OEM_COMMA
            # smoothed_offset < 0 : 오디오가 립보다 늦어 있음 → 앞으로 당겨야 함 → VK_OEM_PERIOD
            vk        = VK_OEM_COMMA  if smoothed_offset > 0 else VK_OEM_PERIOD
            direction = "오디오 늦춤(Shift+,)" if smoothed_offset > 0 else "오디오 당김(Shift+.)"
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
