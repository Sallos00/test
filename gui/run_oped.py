"""
gui/run_oped.py -- OP/ED 백그라운드 모니터 Mixin
_start_oped_monitor, _stop_oped_monitor, _show_oped_skip_popup

[수정]
- _start_oped_monitor: 스레드 목표에 예외 래퍼 추가 (무음 종료 방지)
- _stop_oped_monitor:  join timeout 2s → 8s
  (proc_analyzer의 BUF_SEC(3s) 초기 대기 + INTERVAL(3s) sleep을 충분히 커버)
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

# oped monitor 스레드 join timeout:
# proc_analyzer 초기 대기(BUF_SEC=3s) + 루프 sleep(INTERVAL=3s) + 여유(2s)
_OM_JOIN_TIMEOUT = 8


class OpedMonitorMixin:

    # ── OP/ED 백그라운드 모니터 (싱크 OFF 상태에서도 동작) ───────────────────
    def _start_oped_monitor(self):
        """싱크 미실행 상태 전용 OP/ED 감지 스레드(T2+T3) 시작."""
        # [수정] 링크 재생 모드 중에는 OP/ED 감지 비활성화
        if getattr(self, "_link_play_mode", False):
            import collections, time as _t
            if not hasattr(self, "_log_lines"):
                self._log_lines = collections.deque(maxlen=100)
            self._log_lines.append(
                f"[{_t.strftime('%H:%M:%S')}] ⚠ 링크 재생 모드 중 — OP/ED 감지 비활성화")
            return
        if getattr(self, "_oped_monitor_running", False):
            return
        try:
            runtime_cfg = self._build_cfg()

            self._om_lip_queue   = _queue.Queue(maxsize=20)
            self._om_audio_queue = _queue.Queue(maxsize=30)
            self._om_log_queue   = _queue.Queue(maxsize=200)
            self._om_state_queue = _queue.Queue(maxsize=10)
            self._om_cmd_queue   = _queue.Queue(maxsize=10)
            self._om_stop_flag   = threading.Event()

            self._om_pos_lock    = threading.Lock()
            self._om_shared_pos  = [-1]
            self._om_shared_dur  = [-1]
            import ctypes as _ct
            self._om_stream_anchor = _MpArray(_ct.c_double, [0.0, 48000.0, 1.0])

            from processes import proc_audio_capture, proc_analyzer  # lazy import

            # ── [Bug Fix] 스레드 예외 래퍼 ──────────────────────────────────
            # 스레드 내부 예외(ImportError 등)는 무음 종료로 이어진다.
            # 래퍼로 감싸 _log_lines에 기록해 문제를 가시화한다.
            if not hasattr(self, "_log_lines"):
                self._log_lines = collections.deque(maxlen=100)
            _log_ref = self._log_lines

            def _wrap_om(fn, label):
                def _safe(*args):
                    try:
                        fn(*args)
                    except Exception as _e:
                        import traceback as _tb, time as _t
                        _log_ref.append(
                            f"[{_t.strftime('%H:%M:%S')}] "
                            f"❌ oped 스레드[{label}] 비정상 종료: {_e}"
                        )
                        _log_ref.append(_tb.format_exc()[-300:])
                return _safe

            # 현재 세션 flag/queue를 클로저에 캡처
            _cur_stop   = self._om_stop_flag
            _cur_audio  = self._om_audio_queue
            _cur_state  = self._om_state_queue
            _cur_cmd    = self._om_cmd_queue
            _cur_anchor = self._om_stream_anchor
            _cur_pos    = self._om_shared_pos
            _cur_dur    = self._om_shared_dur

            self._om_threads = []
            for fn, args, label in [
                (proc_audio_capture, (
                    _cur_audio,
                    _cur_stop,
                    runtime_cfg,
                    self._om_log_queue,
                    _cur_anchor,
                ), "T2_om_audio"),
                (proc_analyzer, (
                    self._om_lip_queue,
                    _cur_audio,
                    _cur_state,
                    _cur_cmd,
                    _cur_stop,
                    runtime_cfg,
                    _cur_pos,
                    _cur_dur,
                ), "T3_om_analyzer"),
            ]:
                t = threading.Thread(
                    target=_wrap_om(fn, label), args=args, daemon=True)
                t.start()
                self._om_threads.append(t)

            self._oped_monitor_running = True
        except Exception as e:
            self._oped_monitor_running = False
            import time as _t
            if not hasattr(self, "_log_lines"):
                self._log_lines = collections.deque(maxlen=100)
            self._log_lines.append(
                f"[{_t.strftime('%H:%M:%S')}] ⚠ oped 모니터 시작 실패: {e}")

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
            # ── [Bug Fix] join timeout을 _OM_JOIN_TIMEOUT(8s)으로 증가 ──────
            # 기존 2s는 proc_analyzer의 BUF_SEC(3s)+INTERVAL(3s)보다 짧아
            # join이 먼저 타임아웃되어 좀비 스레드가 발생하였음.
            for t in getattr(self, "_om_threads", []):
                t.join(timeout=_OM_JOIN_TIMEOUT)
        except Exception:
            pass
        self._om_stop_flag            = None
        self._om_cmd_queue            = None
        self._om_state_queue          = None
        self._om_log_queue            = None
        self._om_threads              = []
        self._oped_monitor_running    = False
        self._om_log_seen_count       = 0
        # 스레드 종료 후 잔여 큐 드레인 + 참조 해제
        # (기존에 None 처리가 누락되어 Queue 내 오디오 데이터가 GC 전까지 잔류)
        from mem_utils import full_cleanup
        _lq = getattr(self, '_om_lip_queue',   None)
        _aq = getattr(self, '_om_audio_queue',  None)
        if _lq is not None or _aq is not None:
            full_cleanup(queues=[q for q in (_lq, _aq) if q is not None])
        self._om_lip_queue            = None
        self._om_audio_queue          = None
        # 공유 메모리(_MpArray) 및 공유 위치 리스트 참조 해제
        self._om_stream_anchor        = None
        self._om_shared_pos           = None
        self._om_shared_dur           = None

    # ── OP/ED 스킵 팝업 ──────────────────────────────────────────────────────
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
        import ctypes as _ct
        vx = _ct.windll.user32.GetSystemMetrics(76)
        vy = _ct.windll.user32.GetSystemMetrics(77)
        vw = _ct.windll.user32.GetSystemMetrics(78)
        vh = _ct.windll.user32.GetSystemMetrics(79)
        px = max(vx, min(rect.right  - pw - 12, vx + vw - pw))
        py = max(vy, min(rect.bottom - ph - 48, vy + vh - ph))

        self._oped_popup_open = True

        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=self.BORDER)
        popup.geometry(f"{pw}x{ph}+{px}+{py}")
        popup.update_idletasks()

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
