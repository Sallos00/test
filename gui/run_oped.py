"""
gui/run_oped.py -- OP/ED 백그라운드 모니터 Mixin
_start_oped_monitor, _stop_oped_monitor, _show_oped_skip_popup
"""
import time
import threading
import collections
import ctypes
import ctypes.wintypes
import queue as _queue
import tkinter as tk
from multiprocessing import Array as _MpArray

from win32_utils import find_potplayer_hwnd


class OpedMonitorMixin:

    # ── OP/ED 백그라운드 모니터 (싱크 OFF 상태에서도 동작) ───────────────────
    # 싱크가 꺼져 있어도 OP/ED 음악 감지 + 팝업/자동스킵은 항상 동작해야 한다.
    # P2(오디오캡처) + P3(싱크분석, lip 없이 오디오만) 를 별도로 구동한다.

    def _start_oped_monitor(self):
        """싱크 미실행 상태 전용 OP/ED 감지 스레드(T2+T3) 시작."""
        if getattr(self, "_oped_monitor_running", False):
            return
        try:
            runtime_cfg = self._build_cfg()

            # _om_lip_queue: oped 모니터에서는 실제로 데이터를 넣지 않는 더미 큐.
            # 기존 mp.Queue(maxsize=qsize)는 OS 파이프 버퍼 ~32MB를 사전 할당함.
            # queue.Queue로 교체 → 파이프 버퍼 낭비 없음, 직렬화 오버헤드 없음.
            # [수정] maxsize를 요구사항에 맞게 명시적으로 고정
            # lip_queue=20, audio_queue=30, state_queue=10 으로 제한하여 메모리 누적 방지
            self._om_lip_queue   = _queue.Queue(maxsize=20)   # 더미 — proc_analyzer 시그니처 호환
            self._om_audio_queue = _queue.Queue(maxsize=30)   # 오디오 샘플 누적 방지
            self._om_log_queue   = _queue.Queue(maxsize=200)
            self._om_state_queue = _queue.Queue(maxsize=10)   # 상태 메시지 누적 방지
            self._om_cmd_queue   = _queue.Queue(maxsize=10)
            self._om_stop_flag   = threading.Event()

            # shared_pos/dur: GUI 메인스레드가 갱신, T3가 읽음
            # 스레드 간 공유 → 일반 list + lock (Value 불필요)
            self._om_pos_lock    = threading.Lock()
            self._om_shared_pos  = [-1]
            self._om_shared_dur  = [-1]
            import ctypes as _ct
            self._om_stream_anchor = _MpArray(_ct.c_double, [0.0, 48000.0, 1.0])

            from processes import proc_audio_capture, proc_analyzer  # lazy import
            self._om_threads = []
            for target, args in [
                (proc_audio_capture, (
                    self._om_audio_queue,
                    self._om_stop_flag,
                    runtime_cfg,
                    self._om_log_queue,
                    self._om_stream_anchor,
                )),
                (proc_analyzer, (
                    self._om_lip_queue,
                    self._om_audio_queue,
                    self._om_state_queue,
                    self._om_cmd_queue,
                    self._om_stop_flag,
                    runtime_cfg,
                    self._om_shared_pos,
                    self._om_shared_dur,
                )),
            ]:
                t = threading.Thread(target=target, args=args, daemon=True)
                t.start()
                self._om_threads.append(t)

            self._oped_monitor_running = True
        except Exception as e:
            self._oped_monitor_running = False
            import time as _t
            if not hasattr(self, "_log_lines"):
                self._log_lines = collections.deque(maxlen=100)
            self._log_lines.append(f"[{_t.strftime('%H:%M:%S')}] ⚠ oped 모니터 시작 실패: {e}")

    def _stop_oped_monitor(self):
        """OP/ED 감지 전용 스레드 중지."""
        if not getattr(self, "_oped_monitor_running", False):
            return
        try:
            self._om_stop_flag.set()
            try:
                self._om_cmd_queue.put_nowait("stop")
            except Exception:
                pass
            for t in getattr(self, "_om_threads", []):
                t.join(timeout=2)
        except Exception:
            pass
        # ── [버그2 수정] 큐/flag를 명시적으로 None 처리해 재사용 방지 ─────────
        # _start_oped_monitor()에서 항상 새 인스턴스를 생성하므로 여기서 무효화
        self._om_stop_flag            = None
        self._om_cmd_queue            = None
        self._om_state_queue          = None
        self._om_log_queue            = None   # [Bug 1 수정] 로그 큐 참조도 무효화해 stale 데이터 수집 방지
        self._om_threads              = []
        self._oped_monitor_running    = False
        self._om_log_seen_count       = 0

    # ── OP/ED 스킵 팝업 ──────────────────────────────────────────────────────
    # P3가 oped_prompt를 state_queue에 실어 보내면 _refresh()가 호출
    # [스킵]              → "oped_skip"    → P3가 스킵 실행 + 쿨다운
    # [닫기] / 10초 경과  → "oped_no_skip" → P3가 쿨다운만 시작

    def _show_oped_skip_popup(self, prompt_info: dict, use_om_queue: bool = False):
        if getattr(self, "_oped_popup_open", False):
            return

        zone     = prompt_info.get("zone", "OP/ED")
        skip_sec = prompt_info.get("skip_sec", 90)

        hwnd = find_potplayer_hwnd()
        if not hwnd:
            return

        try:
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        except Exception:
            return

        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        pw = round(280 * r)
        ph = round(88  * r)
        # 멀티모니터: 가상 데스크탑 전체 범위로 클램프
        import ctypes as _ct
        vx = _ct.windll.user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        vy = _ct.windll.user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
        vw = _ct.windll.user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
        vh = _ct.windll.user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
        px = max(vx, min(rect.right  - pw - 12, vx + vw - pw))
        py = max(vy, min(rect.bottom - ph - 48, vy + vh - ph))

        self._oped_popup_open = True

        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.attributes("-topmost", False)   # owned window가 z-order 연동을 담당
        popup.configure(bg=self.BORDER)
        popup.geometry(f"{pw}x{ph}+{px}+{py}")
        popup.update_idletasks()

        # owner를 팟플레이어로 설정 → 팟플레이어가 뒤로 가면 팝업도 같이 뒤로 감
        _GWLP_HWNDPARENT = -8
        try:
            _ov_hwnd  = int(popup.wm_frame(), 16)
            _pot_hwnd = hwnd
            if _ov_hwnd and _pot_hwnd:
                try:
                    ctypes.windll.user32.SetWindowLongPtrW(_ov_hwnd, _GWLP_HWNDPARENT, _pot_hwnd)
                except AttributeError:
                    ctypes.windll.user32.SetWindowLongW(_ov_hwnd, _GWLP_HWNDPARENT, _pot_hwnd)
        except Exception:
            pass

        # 팟플레이어 이동 시 팝업 위치 동기화
        def _track_popup():
            try:
                if not popup.winfo_exists():
                    return
            except Exception:
                return
            _h = find_potplayer_hwnd()
            if _h:
                try:
                    _rc = ctypes.wintypes.RECT()
                    ctypes.windll.user32.GetWindowRect(_h, ctypes.byref(_rc))
                    _pw2 = popup.winfo_width() or pw
                    _ph2 = popup.winfo_height() or ph
                    _px2 = max(vx, min(_rc.right  - _pw2 - 12, vx + vw - _pw2))
                    _py2 = max(vy, min(_rc.bottom - _ph2 - 48, vy + vh - _ph2))
                    popup.geometry(f"+{_px2}+{_py2}")
                except Exception:
                    pass
            try:
                self.root.after(150, _track_popup)
            except Exception:
                pass

        self.root.after(150, _track_popup)

        def send_cmd(cmd: str):
            try:
                # oped 모니터 큐 또는 싱크 큐로 전송
                if use_om_queue and hasattr(self, "_om_cmd_queue"):
                    self._om_cmd_queue.put_nowait(cmd)
                else:
                    self.cmd_queue.put_nowait(cmd)
            except Exception:
                pass

        countdown = [10]
        after_id  = [None]

        def close_popup(skip: bool):
            self._oped_popup_open = False
            if after_id[0]:
                try: self.root.after_cancel(after_id[0])
                except Exception: pass
            send_cmd("oped_skip" if skip else "oped_no_skip")
            try: popup.destroy()
            except Exception: pass

        F_TITLE = max(8, round(9 * r))
        F_BTN   = max(7, round(8 * r))
        PAD     = round(10 * r)
        PAD_S   = round(6  * r)

        outer = tk.Frame(popup, bg=self.BORDER)
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        inner = tk.Frame(outer, bg=self.BG2, padx=PAD, pady=round(8 * r))
        inner.pack(fill="both", expand=True)

        lbl = tk.Label(inner,
                       text=f"🎵 {zone}을 스킵하시겠습니까? (10초)",
                       font=("Segoe UI", F_TITLE, "bold"),
                       bg=self.BG2, fg=self.TEXT, anchor="w")
        lbl.pack(fill="x")

        tk.Label(inner,
                 text=f"스킵 시 {skip_sec}초 앞으로 이동합니다.",
                 font=("Consolas", max(7, F_TITLE - 1)),
                 bg=self.BG2, fg=self.TEXT_MID, anchor="w").pack(fill="x", pady=(round(2*r), 0))

        btn_f = tk.Frame(inner, bg=self.BG2)
        btn_f.pack(anchor="e", pady=(PAD_S, 0))

        BTN = dict(font=("Consolas", F_BTN, "bold"), relief="flat", cursor="hand2",
                   padx=round(12*r), pady=round(3*r))

        tk.Button(btn_f, text="⏭ 스킵",
                  bg=self.BG3, fg=self.ACCENT, activebackground=self.BORDER,
                  command=lambda: close_popup(skip=True),
                  **BTN).pack(side="left", padx=(0, round(4*r)))
        tk.Button(btn_f, text="닫기",
                  bg=self.BG3, fg=self.TEXT_MID, activebackground=self.BORDER,
                  command=lambda: close_popup(skip=False),
                  **BTN).pack(side="left")

        def tick():
            countdown[0] -= 1
            if countdown[0] <= 0:
                close_popup(skip=False)
                return
            try:
                lbl.config(text=f"🎵 {zone}을 스킵하시겠습니까? ({countdown[0]}초)")
                after_id[0] = self.root.after(1000, tick)
            except Exception:
                close_popup(skip=False)

        after_id[0] = self.root.after(1000, tick)
