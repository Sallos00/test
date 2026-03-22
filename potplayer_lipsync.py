#!/usr/bin/env python3
"""
👄 팟플레이어 실시간 립싱크 자동 보정기
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
화면에서 입모양을 감지하고, 시스템 오디오를 캡처하여
실시간으로 싱크를 분석 → Win32 PostMessage로 팟플레이어에만 직접 명령 전송.
게임 등 다른 창에 키 입력이 절대 가지 않습니다.

■ 사전 설치:
  pip install opencv-python mediapipe numpy sounddevice scipy mss pywin32

■ 실행:
  python potplayer_lipsync.py

■ 스크립트 단축키 (터미널에서):
  Q  → 종료
  R  → 싱크 오프셋 초기화
  S  → 현재 상태 출력
"""

import sys, time, threading, collections
import numpy as np
import cv2
import ctypes
import ctypes.wintypes as wt
import sounddevice as sd
import mediapipe as mp
from scipy.signal import correlate
import mss

# ── Win32 API 상수 ────────────────────────────────────────────────────────────
user32 = ctypes.windll.user32

WM_KEYDOWN = 0x0100
WM_KEYUP   = 0x0101
WM_CHAR    = 0x0102

# 가상 키코드
VK_SHIFT  = 0x10
VK_OEM_PERIOD = 0xBE   # '.' → Shift 누르면 '>'
VK_OEM_COMMA  = 0xBC   # ',' → Shift 누르면 '<'
VK_OEM_2      = 0xBF   # '/' → Shift 누르면 '?'  (팟플레이어 싱크 초기화)

# ── 팟플레이어 창 핸들 관리 ───────────────────────────────────────────────────

def find_potplayer_hwnd():
    """
    실행 중인 팟플레이어 창 핸들(HWND)을 찾아 반환.
    팟플레이어 창 클래스명: 'PotPlayer'  (64bit: 'PotPlayer64')
    """
    for cls in ("PotPlayer64", "PotPlayer"):
        hwnd = user32.FindWindowW(cls, None)
        if hwnd:
            return hwnd
    return None

def post_key_to_potplayer(hwnd, vk_code, with_shift=False):
    """
    PostMessage로 팟플레이어 창에 직접 키 이벤트 전송.
    포커스 이동 없이 팟플레이어만 키를 받습니다.

    lparam 구성:
      비트 0-15  : 반복 횟수 (1)
      비트 16-23 : 스캔코드
      비트 24    : 확장 키 여부
      비트 31    : 0=KeyDown, 1=KeyUp
    """
    scan = user32.MapVirtualKeyW(vk_code, 0)

    def make_lparam(up=False):
        lp = 1 | (scan << 16)
        if up:
            lp |= (1 << 31) | (1 << 30)
        return lp

    if with_shift:
        # Shift KeyDown
        shift_scan = user32.MapVirtualKeyW(VK_SHIFT, 0)
        user32.PostMessageW(hwnd, WM_KEYDOWN, VK_SHIFT, 1 | (shift_scan << 16))

    user32.PostMessageW(hwnd, WM_KEYDOWN, vk_code, make_lparam(up=False))
    time.sleep(0.01)
    user32.PostMessageW(hwnd, WM_KEYUP,   vk_code, make_lparam(up=True))

    if with_shift:
        # Shift KeyUp
        shift_scan = user32.MapVirtualKeyW(VK_SHIFT, 0)
        user32.PostMessageW(hwnd, WM_KEYUP, VK_SHIFT,
                            1 | (shift_scan << 16) | (1 << 31) | (1 << 30))

def send_sync_forward(hwnd):
    """Shift+> : 오디오 +50ms (소리를 빠르게)"""
    post_key_to_potplayer(hwnd, VK_OEM_PERIOD, with_shift=True)

def send_sync_backward(hwnd):
    """Shift+< : 오디오 -50ms (소리를 느리게)"""
    post_key_to_potplayer(hwnd, VK_OEM_COMMA, with_shift=True)

def send_sync_reset(hwnd):
    """Shift+/ : 오디오 싱크 초기화"""
    post_key_to_potplayer(hwnd, VK_OEM_2, with_shift=True)

# ── 설정 ─────────────────────────────────────────────────────────────────────
CAPTURE_FPS        = 30          # 화면 캡처 FPS
AUDIO_SR           = 16000       # 오디오 샘플레이트
BUFFER_SEC         = 2.0         # 분석 버퍼 길이 (초)
ANALYSIS_INTERVAL  = 1.0         # 싱크 분석 주기 (초)
SYNC_THRESHOLD_MS  = 80          # 이 값(ms) 이상 어긋나면 보정
POTPLAYER_STEP_MS  = 50          # 팟플레이어 Shift+>,< 1회 = 50ms
MAX_CORRECT_STEP   = 20          # 1회 분석당 최대 보정 횟수
FACE_REGION_RATIO  = 0.6         # 화면 중앙 몇 % 를 얼굴 탐색 영역으로 쓸지
# 키 입력은 Win32 PostMessage로 팟플레이어 창에만 직접 전송 (pyautogui 미사용)

# ── 공유 버퍼 ─────────────────────────────────────────────────────────────────
class SharedBuffer:
    def __init__(self, fps, sr, sec):
        self.lock        = threading.Lock()
        self.lip_times   = collections.deque()  # (timestamp, lip_openness)
        self.audio_times = collections.deque()  # (timestamp, rms_energy)
        self.sec         = sec
        self.total_correction_ms = 0            # 누적 보정값

    def add_lip(self, t, val):
        with self.lock:
            self.lip_times.append((t, val))
            self._trim(self.lip_times)

    def add_audio(self, t, val):
        with self.lock:
            self.audio_times.append((t, val))
            self._trim(self.audio_times)

    def _trim(self, dq):
        now = time.time()
        while dq and now - dq[0][0] > self.sec:
            dq.popleft()

    def get_signals(self):
        with self.lock:
            lip   = list(self.lip_times)
            audio = list(self.audio_times)
        return lip, audio

buf = SharedBuffer(CAPTURE_FPS, AUDIO_SR, BUFFER_SEC)

# ── 1. 화면 캡처 + 입술 감지 스레드 ──────────────────────────────────────────
mp_face_mesh = mp.solutions.face_mesh

# MediaPipe 입술 랜드마크 인덱스 (위·아래 중앙)
LIP_TOP    = 13
LIP_BOTTOM = 14

def lip_capture_thread():
    face_mesh = mp_face_mesh.FaceMesh(
        static_image_mode=False,
        max_num_faces=1,
        refine_landmarks=True,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5)

    sct = mss.mss()
    monitor = sct.monitors[1]  # 주 모니터 전체

    print("📷 화면 캡처 시작...")
    interval = 1.0 / CAPTURE_FPS

    while not stop_event.is_set():
        t0 = time.time()

        # 화면 캡처
        img = np.array(sct.grab(monitor))
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

        # 중앙 영역만 얼굴 탐색 (속도 향상)
        h, w = img.shape[:2]
        margin_x = int(w * (1 - FACE_REGION_RATIO) / 2)
        margin_y = int(h * (1 - FACE_REGION_RATIO) / 2)
        roi = img[margin_y:h-margin_y, margin_x:w-margin_x]

        rgb = cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)
        results = face_mesh.process(rgb)

        openness = 0.0
        if results.multi_face_landmarks:
            lm = results.multi_face_landmarks[0].landmark
            top    = lm[LIP_TOP]
            bottom = lm[LIP_BOTTOM]
            # 입 열림 정도: y 좌표 차이 (정규화)
            openness = abs(bottom.y - top.y)

        buf.add_lip(time.time(), openness)

        elapsed = time.time() - t0
        sleep_t = interval - elapsed
        if sleep_t > 0:
            time.sleep(sleep_t)

    face_mesh.close()
    print("📷 화면 캡처 종료")

# ── 2. 시스템 오디오 캡처 스레드 ─────────────────────────────────────────────
#    Windows: WASAPI loopback (재생 중인 소리를 캡처)
#    macOS  : BlackHole 같은 가상 오디오 장치 필요
#    Linux  : PulseAudio monitor 장치 사용

def find_loopback_device():
    """WASAPI Loopback 장치 찾기 (Windows)"""
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        name = d['name'].lower()
        if ('loopback' in name or 'stereo mix' in name or
                'what u hear' in name or 'wasapi' in name):
            if d['max_input_channels'] > 0:
                return i
    return None

def audio_capture_thread():
    chunk_size = int(AUDIO_SR * 0.05)  # 50ms 청크

    device_idx = find_loopback_device()
    if device_idx is None:
        print("⚠️  WASAPI Loopback 장치를 찾지 못했습니다.")
        print("   Windows: '스테레오 믹스' 또는 'Loopback' 장치를 활성화해 주세요.")
        print("   → 제어판 > 소리 > 녹음 탭에서 '스테레오 믹스' 활성화")
        print("   기본 입력 장치(마이크)로 대체합니다 (정확도 낮음)\n")
        device_idx = None  # 기본 장치 사용

    print(f"🎵 오디오 캡처 시작 (장치: {sd.query_devices(device_idx)['name'] if device_idx is not None else '기본 장치'})...")

    def callback(indata, frames, time_info, status):
        rms = float(np.sqrt(np.mean(indata**2)))
        buf.add_audio(time.time(), rms)

    try:
        with sd.InputStream(
            samplerate=AUDIO_SR,
            channels=1,
            dtype='float32',
            blocksize=chunk_size,
            device=device_idx,
            callback=callback):
            while not stop_event.is_set():
                time.sleep(0.1)
    except Exception as e:
        print(f"❌ 오디오 캡처 오류: {e}")

    print("🎵 오디오 캡처 종료")

# ── 3. 싱크 분석 + 팟플레이어 보정 스레드 ────────────────────────────────────

def resample_signal(times_vals, target_fps=30, duration=BUFFER_SEC):
    """불규칙 타임스탬프 신호를 균등 간격으로 리샘플"""
    if len(times_vals) < 2:
        return None
    times = np.array([x[0] for x in times_vals])
    vals  = np.array([x[1] for x in times_vals])
    t_end   = times[-1]
    t_start = t_end - duration
    n       = int(duration * target_fps)
    t_grid  = np.linspace(t_start, t_end, n)
    resampled = np.interp(t_grid, times, vals)
    return resampled

def compute_offset_ms(lip_sig, audio_sig, fps=30):
    """교차상관으로 오프셋(ms) 계산. 양수 = 오디오가 늦음"""
    # 정규화
    def norm(x):
        x = x - x.mean()
        s = x.std()
        return x / s if s > 1e-9 else x

    l = norm(lip_sig)
    a = norm(audio_sig)

    corr = correlate(l, a, mode='full')
    lag_frames = np.argmax(corr) - (len(a) - 1)
    return lag_frames / fps * 1000  # ms

def press_sync_key(offset_ms):
    """
    Win32 PostMessage로 팟플레이어 창에만 직접 키 전송.
    offset_ms > 0 : 오디오가 늦음 → Shift+> (빠르게)
    offset_ms < 0 : 오디오가 빠름 → Shift+< (느리게)
    게임 등 다른 창에는 절대 입력되지 않습니다.
    """
    hwnd = find_potplayer_hwnd()
    if not hwnd:
        print("  ⚠️  팟플레이어 창을 찾을 수 없습니다. 실행 중인지 확인해 주세요.")
        return 0

    steps = min(int(abs(offset_ms) / POTPLAYER_STEP_MS), MAX_CORRECT_STEP)
    if steps == 0:
        return 0

    label = "빠르게(Shift+>)" if offset_ms > 0 else "느리게(Shift+<)"
    print(f"  🎹 PostMessage → 팟플레이어(HWND:{hwnd:#010x})  {label} × {steps}회  ({steps * POTPLAYER_STEP_MS}ms 보정)")

    for _ in range(steps):
        if offset_ms > 0:
            send_sync_forward(hwnd)
        else:
            send_sync_backward(hwnd)
        time.sleep(0.05)

    correction = steps * POTPLAYER_STEP_MS * (1 if offset_ms > 0 else -1)
    buf.total_correction_ms += correction
    return correction

def analysis_thread():
    print(f"🔍 싱크 분석 시작 (주기: {ANALYSIS_INTERVAL}초, 임계값: ±{SYNC_THRESHOLD_MS}ms)\n")
    time.sleep(BUFFER_SEC)  # 버퍼 채울 때까지 대기

    while not stop_event.is_set():
        t_start = time.time()

        lip_data, audio_data = buf.get_signals()

        if len(lip_data) < 10 or len(audio_data) < 10:
            time.sleep(ANALYSIS_INTERVAL)
            continue

        lip_sig   = resample_signal(lip_data)
        audio_sig = resample_signal(audio_data)

        if lip_sig is None or audio_sig is None:
            time.sleep(ANALYSIS_INTERVAL)
            continue

        # 길이 맞추기
        min_len   = min(len(lip_sig), len(audio_sig))
        lip_sig   = lip_sig[-min_len:]
        audio_sig = audio_sig[-min_len:]

        offset_ms = compute_offset_ms(lip_sig, audio_sig)

        status = "✅ 싱크 정상" if abs(offset_ms) < SYNC_THRESHOLD_MS else f"⚠️  싱크 오차: {offset_ms:+.1f}ms"
        print(f"[{time.strftime('%H:%M:%S')}] {status}  |  누적 보정: {buf.total_correction_ms:+d}ms")

        if abs(offset_ms) >= SYNC_THRESHOLD_MS:
            press_sync_key(offset_ms)

        elapsed = time.time() - t_start
        sleep_t = ANALYSIS_INTERVAL - elapsed
        if sleep_t > 0:
            time.sleep(sleep_t)

    print("🔍 분석 종료")

# ── 4. 키보드 입력 처리 ───────────────────────────────────────────────────────

def keyboard_thread():
    import msvcrt  # Windows 전용
    print("⌨️  Q=종료  R=싱크초기화  S=상태출력\n")
    while not stop_event.is_set():
        if msvcrt.kbhit():
            key = msvcrt.getch().decode('utf-8', errors='ignore').lower()
            if key == 'q':
                print("\n👋 종료합니다...")
                stop_event.set()
            elif key == 'r':
                hwnd = find_potplayer_hwnd()
                if hwnd:
                    send_sync_reset(hwnd)   # Shift+/ → 팟플레이어 싱크 초기화
                    buf.total_correction_ms = 0
                    print("🔄 싱크 초기화 완료 (PostMessage → 팟플레이어)")
                else:
                    print("⚠️  팟플레이어 창을 찾을 수 없습니다.")
            elif key == 's':
                lip_data, audio_data = buf.get_signals()
                hwnd = find_potplayer_hwnd()
                print(f"  팟플레이어 HWND : {hwnd:#010x}" if hwnd else "  팟플레이어 : 미감지")
                print(f"  입술 샘플 : {len(lip_data)}  오디오 샘플 : {len(audio_data)}  누적 보정 : {buf.total_correction_ms:+d}ms")
        time.sleep(0.05)

# ── 메인 ─────────────────────────────────────────────────────────────────────

stop_event = threading.Event()

def main():
    print("=" * 55)
    print(" 👄 팟플레이어 실시간 립싱크 자동 보정기")
    print(" 🎮 Win32 PostMessage 방식 — 게임 입력 간섭 없음")
    print("=" * 55)
    print()

    # 시작 시 팟플레이어 감지 확인
    hwnd = find_potplayer_hwnd()
    if hwnd:
        print(f"✅ 팟플레이어 감지됨  (HWND: {hwnd:#010x})")
    else:
        print("⚠️  팟플레이어를 찾지 못했습니다. 먼저 실행 후 영상을 재생해 주세요.")
    print()
    print("주의사항:")
    print("  1. 얼굴이 화면에 크고 정면으로 보여야 정확합니다.")
    print("  2. Windows '스테레오 믹스'가 활성화되어 있어야")
    print("     시스템 오디오를 정확히 캡처할 수 있습니다.")
    print()

    threads = [
        threading.Thread(target=lip_capture_thread,   daemon=True, name="LipCapture"),
        threading.Thread(target=audio_capture_thread, daemon=True, name="AudioCapture"),
        threading.Thread(target=analysis_thread,      daemon=True, name="Analysis"),
    ]

    # 키보드 스레드는 Windows 전용
    try:
        import msvcrt
        threads.append(threading.Thread(target=keyboard_thread, daemon=True, name="Keyboard"))
    except ImportError:
        print("ℹ️  키보드 제어는 Windows 전용입니다. Ctrl+C 로 종료하세요.\n")

    for t in threads:
        t.start()

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n👋 종료합니다...")
        stop_event.set()

    for t in threads:
        t.join(timeout=2.0)

    print(f"\n📊 최종 누적 보정값: {buf.total_correction_ms:+d}ms")
    print("✅ 종료 완료")

if __name__ == "__main__":
    main()
