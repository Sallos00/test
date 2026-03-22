#!/usr/bin/env python3
"""
👄 팟플레이어 실시간 립싱크 자동 보정기 (GUI 버전)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
화면에서 입모양을 감지하고, 시스템 오디오를 캡처하여
실시간으로 싱크를 분석 → Win32 PostMessage로 팟플레이어에만 직접 명령 전송.

■ 사전 설치:
  pip install opencv-python mediapipe numpy sounddevice scipy mss pywin32

■ 실행:
  python potplayer_lipsync_gui.py
"""

import sys, time, threading, collections
import numpy as np
import cv2
import ctypes
import sounddevice as sd
import mediapipe as mp
from scipy.signal import correlate
import mss
import tkinter as tk
from tkinter import font as tkfont

# ── Win32 API ─────────────────────────────────────────────────────────────────
user32 = ctypes.windll.user32
WM_KEYDOWN    = 0x0100
WM_KEYUP      = 0x0101
VK_SHIFT      = 0x10
VK_OEM_PERIOD = 0xBE
VK_OEM_COMMA  = 0xBC
VK_OEM_2      = 0xBF

def find_potplayer_hwnd():
    for cls in ("PotPlayer64", "PotPlayer"):
        hwnd = user32.FindWindowW(cls, None)
        if hwnd:
            return hwnd
    return None

def post_key_to_potplayer(hwnd, vk_code, with_shift=False):
    scan = user32.MapVirtualKeyW(vk_code, 0)
    def make_lparam(up=False):
        lp = 1 | (scan << 16)
        if up: lp |= (1 << 31) | (1 << 30)
        return lp
    if with_shift:
        ss = user32.MapVirtualKeyW(VK_SHIFT, 0)
        user32.PostMessageW(hwnd, WM_KEYDOWN, VK_SHIFT, 1 | (ss << 16))
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk_code, make_lparam(False))
    time.sleep(0.01)
    user32.PostMessageW(hwnd, WM_KEYUP,   vk_code, make_lparam(True))
    if with_shift:
        ss = user32.MapVirtualKeyW(VK_SHIFT, 0)
        user32.PostMessageW(hwnd, WM_KEYUP, VK_SHIFT,
                            1 | (ss << 16) | (1 << 31) | (1 << 30))

def send_sync_forward(hwnd):  post_key_to_potplayer(hwnd, VK_OEM_PERIOD, True)
def send_sync_backward(hwnd): post_key_to_potplayer(hwnd, VK_OEM_COMMA,  True)
def send_sync_reset(hwnd):    post_key_to_potplayer(hwnd, VK_OEM_2,      True)

# ── 설정 ─────────────────────────────────────────────────────────────────────
CAPTURE_FPS       = 30
AUDIO_SR          = 16000
BUFFER_SEC        = 2.0
ANALYSIS_INTERVAL = 1.0
SYNC_THRESHOLD_MS = 80
POTPLAYER_STEP_MS = 50
MAX_CORRECT_STEP  = 20
FACE_REGION_RATIO = 0.6

# ── 공유 버퍼 ─────────────────────────────────────────────────────────────────
class SharedBuffer:
    def __init__(self):
        self.lock        = threading.Lock()
        self.lip_times   = collections.deque()
        self.audio_times = collections.deque()
        self.total_correction_ms = 0

    def add_lip(self, t, v):
        with self.lock:
            self.lip_times.append((t, v))
            self._trim(self.lip_times)

    def add_audio(self, t, v):
        with self.lock:
            self.audio_times.append((t, v))
            self._trim(self.audio_times)

    def _trim(self, dq):
        now = time.time()
        while dq and now - dq[0][0] > BUFFER_SEC:
            dq.popleft()

    def get_signals(self):
        with self.lock:
            return list(self.lip_times), list(self.audio_times)

buf       = SharedBuffer()
stop_event = threading.Event()

# ── 상태 공유 (GUI ↔ 스레드) ──────────────────────────────────────────────────
class AppState:
    def __init__(self):
        self.lock           = threading.Lock()
        self.running        = False
        self.potplayer_ok   = False
        self.potplayer_hwnd = None
        self.lip_samples    = 0
        self.audio_samples  = 0
        self.last_offset_ms = 0.0
        self.last_status    = "대기 중"   # "정상" | "보정 중" | "오류"
        self.correction_ms  = 0
        self.log_lines      = collections.deque(maxlen=6)

    def log(self, msg):
        with self.lock:
            ts = time.strftime("%H:%M:%S")
            self.log_lines.append(f"[{ts}] {msg}")

state = AppState()

# ── 스레드 1: 화면 캡처 + 입술 감지 ──────────────────────────────────────────
mp_face_mesh = mp.solutions.face_mesh
LIP_TOP, LIP_BOTTOM = 13, 14

def lip_capture_thread():
    face_mesh = mp_face_mesh.FaceMesh(
        static_image_mode=False, max_num_faces=1,
        refine_landmarks=True,
        min_detection_confidence=0.5, min_tracking_confidence=0.5)
    sct     = mss.mss()
    monitor = sct.monitors[1]
    interval = 1.0 / CAPTURE_FPS

    while not stop_event.is_set():
        t0  = time.time()
        img = np.array(sct.grab(monitor))
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        h, w = img.shape[:2]
        mx = int(w * (1 - FACE_REGION_RATIO) / 2)
        my = int(h * (1 - FACE_REGION_RATIO) / 2)
        roi = img[my:h-my, mx:w-mx]
        rgb = cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)
        res = face_mesh.process(rgb)
        openness = 0.0
        if res.multi_face_landmarks:
            lm = res.multi_face_landmarks[0].landmark
            openness = abs(lm[LIP_BOTTOM].y - lm[LIP_TOP].y)
        buf.add_lip(time.time(), openness)
        with state.lock:
            state.lip_samples = len(buf.lip_times)
        elapsed = time.time() - t0
        st = interval - elapsed
        if st > 0: time.sleep(st)

    face_mesh.close()

# ── 스레드 2: 오디오 캡처 ─────────────────────────────────────────────────────
def find_loopback_device():
    for i, d in enumerate(sd.query_devices()):
        n = d['name'].lower()
        if any(k in n for k in ('loopback','stereo mix','what u hear','wasapi')):
            if d['max_input_channels'] > 0:
                return i
    return None

def audio_capture_thread():
    chunk  = int(AUDIO_SR * 0.05)
    dev    = find_loopback_device()
    if dev is None:
        state.log("⚠ 스테레오 믹스 미감지 — 기본 장치 사용")

    def cb(indata, frames, ti, status):
        rms = float(np.sqrt(np.mean(indata**2)))
        buf.add_audio(time.time(), rms)
        with state.lock:
            state.audio_samples = len(buf.audio_times)

    try:
        with sd.InputStream(samplerate=AUDIO_SR, channels=1,
                            dtype='float32', blocksize=chunk,
                            device=dev, callback=cb):
            while not stop_event.is_set():
                time.sleep(0.1)
    except Exception as e:
        state.log(f"❌ 오디오 오류: {e}")

# ── 스레드 3: 싱크 분석 + 보정 ───────────────────────────────────────────────
def resample(tvs, fps=30):
    if len(tvs) < 2: return None
    ts = np.array([x[0] for x in tvs])
    vs = np.array([x[1] for x in tvs])
    t_end   = ts[-1]
    t_start = t_end - BUFFER_SEC
    grid    = np.linspace(t_start, t_end, int(BUFFER_SEC * fps))
    return np.interp(grid, ts, vs)

def compute_offset_ms(lip, aud, fps=30):
    def norm(x):
        x = x - x.mean(); s = x.std()
        return x / s if s > 1e-9 else x
    corr = correlate(norm(lip), norm(aud), mode='full')
    lag  = np.argmax(corr) - (len(aud) - 1)
    return lag / fps * 1000

def analysis_thread():
    time.sleep(BUFFER_SEC)
    while not stop_event.is_set():
        t0 = time.time()

        # 팟플레이어 감지
        hwnd = find_potplayer_hwnd()
        with state.lock:
            state.potplayer_ok   = bool(hwnd)
            state.potplayer_hwnd = hwnd

        lip_data, aud_data = buf.get_signals()
        if len(lip_data) < 10 or len(aud_data) < 10:
            time.sleep(ANALYSIS_INTERVAL); continue

        lip_sig = resample(lip_data)
        aud_sig = resample(aud_data)
        if lip_sig is None or aud_sig is None:
            time.sleep(ANALYSIS_INTERVAL); continue

        n = min(len(lip_sig), len(aud_sig))
        offset_ms = compute_offset_ms(lip_sig[-n:], aud_sig[-n:])

        with state.lock:
            state.last_offset_ms = offset_ms

        if abs(offset_ms) >= SYNC_THRESHOLD_MS and hwnd:
            steps = min(int(abs(offset_ms) / POTPLAYER_STEP_MS), MAX_CORRECT_STEP)
            direction = "빠르게" if offset_ms > 0 else "느리게"
            state.log(f"보정: {direction} ×{steps} ({steps*POTPLAYER_STEP_MS}ms)")
            with state.lock: state.last_status = "보정 중"
            for _ in range(steps):
                if offset_ms > 0: send_sync_forward(hwnd)
                else:             send_sync_backward(hwnd)
                time.sleep(0.05)
            corr = steps * POTPLAYER_STEP_MS * (1 if offset_ms > 0 else -1)
            with state.lock:
                buf.total_correction_ms += corr
                state.correction_ms      = buf.total_correction_ms
                state.last_status        = "정상"
        else:
            with state.lock:
                state.last_status    = "정상" if hwnd else "팟플레이어 미감지"
                state.correction_ms  = buf.total_correction_ms

        elapsed = time.time() - t0
        st = ANALYSIS_INTERVAL - elapsed
        if st > 0: time.sleep(st)

# ── GUI ───────────────────────────────────────────────────────────────────────
class LipSyncGUI:
    # 색상 팔레트 (다크 모노크롬)
    BG        = "#0e0e0e"
    BG2       = "#161616"
    BG3       = "#1e1e1e"
    BORDER    = "#2a2a2a"
    ACCENT    = "#00e5ff"      # 청록 포인트
    ACCENT2   = "#ff4f4f"      # 경고 빨강
    ACCENT3   = "#b0ff6f"      # 정상 그린
    TEXT      = "#e8e8e8"
    TEXT_DIM  = "#555555"
    TEXT_MID  = "#888888"

    W, H = 340, 420

    def __init__(self, root: tk.Tk):
        self.root = root
        self._build_window()
        self._build_ui()
        self._refresh()

    # ── 창 설정 ───────────────────────────────────────────────────────────────
    def _build_window(self):
        r = self.root
        r.title("LipSync")
        r.geometry(f"{self.W}x{self.H}")
        r.resizable(False, False)
        r.configure(bg=self.BG)
        # 창 가운데 배치
        r.update_idletasks()
        sw = r.winfo_screenwidth()
        sh = r.winfo_screenheight()
        x  = (sw - self.W) // 2
        y  = (sh - self.H) // 2
        r.geometry(f"{self.W}x{self.H}+{x}+{y}")
        r.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI 구성 ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        MONO = ("Consolas", 9)
        MONO_SM = ("Consolas", 8)

        # ── 헤더 ──────────────────────────────────────────────────────────────
        header = tk.Frame(self.root, bg=self.BG, pady=14)
        header.pack(fill="x", padx=18)

        tk.Label(header, text="👄", font=("Segoe UI Emoji", 18),
                 bg=self.BG, fg=self.ACCENT).pack(side="left")

        title_f = tk.Frame(header, bg=self.BG)
        title_f.pack(side="left", padx=10)
        tk.Label(title_f, text="LipSync Monitor",
                 font=("Segoe UI", 13, "bold"),
                 bg=self.BG, fg=self.TEXT).pack(anchor="w")
        tk.Label(title_f, text="PotPlayer 자동 싱크 보정",
                 font=("Segoe UI", 8),
                 bg=self.BG, fg=self.TEXT_MID).pack(anchor="w")

        # 버전 뱃지
        tk.Label(header, text="v1.0",
                 font=("Consolas", 7), bg=self.BG3,
                 fg=self.TEXT_DIM, padx=5, pady=2,
                 relief="flat").pack(side="right", anchor="n", pady=6)

        # ── 구분선 ────────────────────────────────────────────────────────────
        tk.Frame(self.root, bg=self.BORDER, height=1).pack(fill="x", padx=0)

        # ── 상태 카드 ─────────────────────────────────────────────────────────
        card = tk.Frame(self.root, bg=self.BG2, pady=12, padx=16)
        card.pack(fill="x", padx=14, pady=(12, 0))

        # 팟플레이어 상태
        row1 = tk.Frame(card, bg=self.BG2)
        row1.pack(fill="x", pady=2)
        tk.Label(row1, text="팟플레이어", font=MONO,
                 bg=self.BG2, fg=self.TEXT_MID, width=11, anchor="w").pack(side="left")
        self._pot_dot = tk.Label(row1, text="●", font=("Consolas", 10),
                                  bg=self.BG2, fg=self.ACCENT2)
        self._pot_dot.pack(side="left")
        self._pot_lbl = tk.Label(row1, text="미감지", font=MONO,
                                  bg=self.BG2, fg=self.ACCENT2)
        self._pot_lbl.pack(side="left", padx=4)

        # 오디오 장치 상태
        row2 = tk.Frame(card, bg=self.BG2)
        row2.pack(fill="x", pady=2)
        tk.Label(row2, text="오디오 장치", font=MONO,
                 bg=self.BG2, fg=self.TEXT_MID, width=11, anchor="w").pack(side="left")
        self._aud_dot = tk.Label(row2, text="●", font=("Consolas", 10),
                                  bg=self.BG2, fg=self.TEXT_DIM)
        self._aud_dot.pack(side="left")
        self._aud_lbl = tk.Label(row2, text="초기화 중…", font=MONO,
                                  bg=self.BG2, fg=self.TEXT_DIM)
        self._aud_lbl.pack(side="left", padx=4)

        # ── 구분선 ────────────────────────────────────────────────────────────
        tk.Frame(self.root, bg=self.BORDER, height=1).pack(fill="x", padx=14, pady=(12,0))

        # ── 오프셋 미터 ───────────────────────────────────────────────────────
        meter_f = tk.Frame(self.root, bg=self.BG, pady=10, padx=18)
        meter_f.pack(fill="x")

        top_row = tk.Frame(meter_f, bg=self.BG)
        top_row.pack(fill="x")
        tk.Label(top_row, text="OFFSET", font=("Consolas", 7, "bold"),
                 bg=self.BG, fg=self.TEXT_DIM).pack(side="left")
        self._status_badge = tk.Label(top_row, text="  대기 중  ",
                                       font=("Consolas", 7),
                                       bg=self.BG3, fg=self.TEXT_MID,
                                       padx=6, pady=2)
        self._status_badge.pack(side="right")

        # 큰 오프셋 숫자
        self._offset_lbl = tk.Label(meter_f, text="— ms",
                                     font=("Consolas", 32, "bold"),
                                     bg=self.BG, fg=self.ACCENT)
        self._offset_lbl.pack(anchor="w", pady=(2, 0))

        # 프로그레스 바 (오프셋 시각화)
        bar_bg = tk.Frame(meter_f, bg=self.BG3, height=4)
        bar_bg.pack(fill="x", pady=(4, 0))
        bar_bg.pack_propagate(False)
        self._bar_inner = tk.Frame(bar_bg, bg=self.ACCENT, height=4)
        self._bar_inner.place(x=0, y=0, width=0, height=4)
        self._bar_bg_ref = bar_bg

        # 누적 보정
        corr_row = tk.Frame(meter_f, bg=self.BG)
        corr_row.pack(fill="x", pady=(6, 0))
        tk.Label(corr_row, text="누적 보정", font=MONO_SM,
                 bg=self.BG, fg=self.TEXT_DIM).pack(side="left")
        self._corr_lbl = tk.Label(corr_row, text="+0 ms", font=MONO_SM,
                                   bg=self.BG, fg=self.TEXT_MID)
        self._corr_lbl.pack(side="left", padx=6)

        # ── 샘플 카운터 ───────────────────────────────────────────────────────
        tk.Frame(self.root, bg=self.BORDER, height=1).pack(fill="x", padx=14, pady=(8,0))

        sample_f = tk.Frame(self.root, bg=self.BG, padx=18, pady=8)
        sample_f.pack(fill="x")
        tk.Label(sample_f, text="입술 샘플", font=MONO_SM,
                 bg=self.BG, fg=self.TEXT_DIM).grid(row=0, column=0, sticky="w")
        self._lip_cnt = tk.Label(sample_f, text="0", font=MONO_SM,
                                  bg=self.BG, fg=self.TEXT_MID)
        self._lip_cnt.grid(row=0, column=1, sticky="w", padx=8)

        tk.Label(sample_f, text="오디오 샘플", font=MONO_SM,
                 bg=self.BG, fg=self.TEXT_DIM).grid(row=0, column=2, sticky="w", padx=(20,0))
        self._aud_cnt = tk.Label(sample_f, text="0", font=MONO_SM,
                                  bg=self.BG, fg=self.TEXT_MID)
        self._aud_cnt.grid(row=0, column=3, sticky="w", padx=8)

        # ── 로그 영역 ─────────────────────────────────────────────────────────
        tk.Frame(self.root, bg=self.BORDER, height=1).pack(fill="x", padx=14)

        log_f = tk.Frame(self.root, bg=self.BG2, padx=12, pady=8)
        log_f.pack(fill="both", expand=True, padx=14, pady=(0, 0))

        tk.Label(log_f, text="LOG", font=("Consolas", 7, "bold"),
                 bg=self.BG2, fg=self.TEXT_DIM).pack(anchor="w")

        self._log_lbl = tk.Label(log_f, text="",
                                  font=("Consolas", 8),
                                  bg=self.BG2, fg=self.TEXT_MID,
                                  justify="left", anchor="w",
                                  wraplength=self.W - 54)
        self._log_lbl.pack(anchor="w", pady=(2, 0))

        # ── 버튼 영역 ─────────────────────────────────────────────────────────
        tk.Frame(self.root, bg=self.BORDER, height=1).pack(fill="x", padx=0)

        btn_f = tk.Frame(self.root, bg=self.BG, pady=10, padx=14)
        btn_f.pack(fill="x")

        BTN = dict(font=("Consolas", 8, "bold"), relief="flat",
                   cursor="hand2", padx=12, pady=6)

        self._start_btn = tk.Button(btn_f, text="▶  시작",
                                     bg=self.ACCENT, fg=self.BG,
                                     activebackground="#00b8cc",
                                     command=self._toggle_start, **BTN)
        self._start_btn.pack(side="left")

        tk.Button(btn_f, text="↺  초기화",
                  bg=self.BG3, fg=self.TEXT_MID,
                  activebackground=self.BORDER,
                  command=self._reset_sync, **BTN).pack(side="left", padx=6)

        tk.Button(btn_f, text="✕  종료",
                  bg=self.BG3, fg=self.ACCENT2,
                  activebackground=self.BORDER,
                  command=self._on_close, **BTN).pack(side="right")

    # ── 시작 / 정지 ───────────────────────────────────────────────────────────
    def _toggle_start(self):
        if not state.running:
            state.running = True
            stop_event.clear()
            for target in (lip_capture_thread, audio_capture_thread, analysis_thread):
                threading.Thread(target=target, daemon=True).start()
            self._start_btn.config(text="⏹  정지",
                                   bg=self.BG3, fg=self.ACCENT2,
                                   activebackground=self.BORDER)
            state.log("▶ 분석 시작")
        else:
            state.running = False
            stop_event.set()
            self._start_btn.config(text="▶  시작",
                                   bg=self.ACCENT, fg=self.BG,
                                   activebackground="#00b8cc")
            state.log("⏹ 분석 중지")

    # ── 초기화 ────────────────────────────────────────────────────────────────
    def _reset_sync(self):
        hwnd = find_potplayer_hwnd()
        if hwnd:
            send_sync_reset(hwnd)
            buf.total_correction_ms = 0
            with state.lock: state.correction_ms = 0
            state.log("↺ 싱크 초기화 (Shift+/)")
        else:
            state.log("⚠ 팟플레이어 미감지")

    # ── 주기적 UI 갱신 (100ms) ────────────────────────────────────────────────
    def _refresh(self):
        with state.lock:
            pot_ok     = state.potplayer_ok
            lip_n      = state.lip_samples
            aud_n      = state.audio_samples
            offset     = state.last_offset_ms
            status     = state.last_status
            corr       = state.correction_ms
            logs       = list(state.log_lines)
            running    = state.running

        # 팟플레이어 상태
        if pot_ok:
            self._pot_dot.config(fg=self.ACCENT3)
            self._pot_lbl.config(text="연결됨", fg=self.ACCENT3)
        else:
            self._pot_dot.config(fg=self.ACCENT2)
            self._pot_lbl.config(text="미감지", fg=self.ACCENT2)

        # 오디오 상태
        if aud_n > 0:
            self._aud_dot.config(fg=self.ACCENT3)
            self._aud_lbl.config(text="캡처 중", fg=self.ACCENT3)
        else:
            self._aud_dot.config(fg=self.TEXT_DIM)
            self._aud_lbl.config(text="대기 중", fg=self.TEXT_DIM)

        # 오프셋 숫자
        if running and lip_n > 0:
            sign = "+" if offset > 0 else ""
            self._offset_lbl.config(
                text=f"{sign}{offset:.0f} ms",
                fg=(self.ACCENT2 if abs(offset) >= SYNC_THRESHOLD_MS
                    else self.ACCENT3 if abs(offset) < 30
                    else self.ACCENT))
        else:
            self._offset_lbl.config(text="— ms", fg=self.ACCENT)

        # 프로그레스 바
        self._bar_bg_ref.update_idletasks()
        bar_w = self._bar_bg_ref.winfo_width()
        ratio = min(abs(offset) / 500, 1.0)
        fill  = int(bar_w * ratio)
        col   = (self.ACCENT2 if abs(offset) >= SYNC_THRESHOLD_MS else self.ACCENT3)
        self._bar_inner.place(x=0, y=0, width=fill, height=4)
        self._bar_inner.config(bg=col)

        # 상태 뱃지
        badge_map = {
            "정상":         (self.ACCENT3,  self.BG3),
            "보정 중":      (self.ACCENT,   self.BG3),
            "팟플레이어 미감지": (self.ACCENT2, self.BG3),
            "대기 중":      (self.TEXT_DIM, self.BG3),
        }
        fg, bg = badge_map.get(status, (self.TEXT_DIM, self.BG3))
        self._status_badge.config(text=f"  {status}  ", fg=fg, bg=bg)

        # 누적 보정
        sign = "+" if corr >= 0 else ""
        self._corr_lbl.config(text=f"{sign}{corr} ms")

        # 샘플 카운터
        self._lip_cnt.config(text=str(lip_n))
        self._aud_cnt.config(text=str(aud_n))

        # 로그
        self._log_lbl.config(text="\n".join(logs[-4:]) if logs else "")

        self.root.after(100, self._refresh)

    def _on_close(self):
        stop_event.set()
        self.root.destroy()

# ── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    root = tk.Tk()
    app  = LipSyncGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
