"""
gui_run.py -- 실행 제어, 프로세스 관리, 갱신, 인증 팝업 메서드
"""
import os
import time
import threading
import collections
import ctypes
import ctypes.wintypes
import tkinter as tk
import winreg
from multiprocessing import Process, Queue, Value

import auth as _auth_module

from win32_utils import (
    CFG, find_potplayer_hwnd, is_potplayer_playing,
    is_potplayer_running, post_key_to_potplayer, VK_OEM_2,
    get_playback_info,
)
from processes import proc_lip_capture, proc_audio_capture, proc_analyzer


class LipSyncGUIRun:

    # ── OP/ED 백그라운드 모니터 (싱크 OFF 상태에서도 동작) ───────────────────
    # 싱크가 꺼져 있어도 OP/ED 음악 감지 + 팝업/자동스킵은 항상 동작해야 한다.
    # P2(오디오캡처) + P3(싱크분석, lip 없이 오디오만) 를 별도로 구동한다.

    def _start_oped_monitor(self):
        """싱크 미실행 상태 전용 OP/ED 감지 프로세스(P2+P3) 시작."""
        if getattr(self, "_oped_monitor_running", False):
            return
        try:
            runtime_cfg = self._build_cfg()
            qsize = runtime_cfg.get("QUEUE_MAXSIZE", 200)

            self._om_lip_queue   = Queue(maxsize=qsize)
            self._om_audio_queue = Queue(maxsize=qsize)
            self._om_log_queue   = Queue(maxsize=200)   # P2 전용 로그 큐
            self._om_state_queue = Queue(maxsize=20)
            self._om_cmd_queue   = Queue(maxsize=10)
            self._om_stop_flag   = Value("b", False)

            # shared_pos/dur: GUI 메인스레드가 갱신, P3가 읽음
            self._om_shared_pos  = Value(ctypes.c_longlong, -1)
            self._om_shared_dur  = Value(ctypes.c_longlong, -1)

            self._om_processes = []
            for target, args in [
                (proc_audio_capture, (
                    self._om_audio_queue,
                    self._om_stop_flag,
                    runtime_cfg,
                    self._om_log_queue,
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
                p = Process(target=target, args=args, daemon=True)
                p.start()
                self._om_processes.append(p)

            self._oped_monitor_running = True
        except Exception as e:
            self._oped_monitor_running = False
            import time as _t
            if not hasattr(self, "_log_lines"):
                self._log_lines = collections.deque(maxlen=100)
            self._log_lines.append(f"[{_t.strftime('%H:%M:%S')}] ⚠ oped 모니터 시작 실패: {e}")

    def _stop_oped_monitor(self):
        """OP/ED 감지 전용 프로세스 중지."""
        if not getattr(self, "_oped_monitor_running", False):
            return
        try:
            self._om_stop_flag.value = True
            import threading as _th
            def _join(p):
                p.join(timeout=2)
                if p.is_alive(): p.terminate()
            ts = [_th.Thread(target=_join, args=(p,), daemon=True) for p in self._om_processes]
            for t in ts: t.start()
            for t in ts: t.join()
        except Exception:
            pass
        self._om_processes            = []
        self._oped_monitor_running    = False

    def _toggle(self):
        if not self._running:
            self._stop_oped_monitor()   # 싱크 시작 전 모니터 중지 (중복 방지)
            hwnd = find_potplayer_hwnd()
            if not hwnd:
                self._start_btn.config(text="⏳ 대기 중...",
                                       bg=self.BG3, fg=self.TEXT_DIM,
                                       activebackground=self.BORDER,
                                       state="disabled")
                self._proc_lbl.config(text="팟플레이어 실행을 기다리는 중...",
                                      fg=self.ACCENT)
                threading.Thread(target=self._wait_for_potplayer,
                                 daemon=True).start()
            else:
                self._start_processes()
        else:
            self._stop_processes()
            self._start_btn.config(text="▶ 시작",
                                   bg=self.BG3, fg=self.ACCENT,
                                   activebackground=self.BORDER,
                                   state="normal")
            self._proc_lbl.config(text="중지됨", fg=self.TEXT_DIM)
            self._start_oped_monitor()   # 싱크 정지 후 모니터 재시작
            threading.Thread(
                target=self._monitor_for_popup,
                kwargs={"wait_for_exit": True},
                daemon=True).start()


    # ── Windows 토스트 알림 ───────────────────────────────────────────────────
    @staticmethod
    def _register_app_id():
        try:
            key_path = r"SOFTWARE\Classes\AppUserModelId\LipSyncMonitor"
            key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path,
                                     0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "Auto Sinc")
            winreg.CloseKey(key)
        except Exception:
            pass

    @staticmethod
    def _toast(title: str, msg: str):
        try:
            from winotify import Notification, audio
            n = Notification(app_id="LipSyncMonitor",
                             title=title, msg=msg, duration="short")
            n.set_audio(audio.Default, loop=False)
            n.show()
            return
        except Exception:
            pass
        try:
            import win32gui, win32con
            hwnd = win32gui.FindWindow("Shell_TrayWnd", None)
            if hwnd:
                win32gui.Shell_NotifyIcon(win32con.NIM_MODIFY, (
                    hwnd, 0, win32con.NIF_INFO, win32con.WM_USER + 20,
                    None, msg, title, 5, win32con.NIIF_INFO
                ))
        except Exception:
            pass

    def _wait_for_potplayer(self):
        while True:
            hwnd = find_potplayer_hwnd()
            if hwnd:
                self._toast("🎬 Auto Sync",
                            "팟플레이어가 감지되었습니다.\n싱크 보정을 시작합니다.")
                self.root.after(0, self._start_processes)
                return
            time.sleep(0.5)

    def _monitor_for_popup(self, wait_for_exit=False):
        """싱크 OFF 상태에서 팟플레이어 재생 감지 시 시작 팝업 표시."""
        while not getattr(self, '_auth_ok', False):
            if self._closing: return
            time.sleep(0.1)

        if wait_for_exit:
            while not self._closing and not self._running:
                if not is_potplayer_running():
                    break
                for _ in range(10):
                    if self._closing or self._running: return
                    time.sleep(0.1)

        while not self._closing and not self._running:
            hwnd = find_potplayer_hwnd()
            if hwnd and is_potplayer_playing(hwnd) and is_potplayer_running():
                if self._closing or self._running:
                    return
                self._popup_open = True
                def _safe_show():
                    if not self._closing and not self._running:
                        self._show_start_popup()
                    else:
                        self._popup_open = False
                self._popup_after_id = self.root.after_idle(_safe_show)
                return
            for _ in range(10):
                if self._closing or self._running: return
                time.sleep(0.1)

    def _show_start_popup(self):
        """동영상 재생 감지 시 싱크 시작 여부 팝업."""
        try:
            if self._running or self._closing:
                self._popup_open = False
                return
            if not self.root.winfo_exists():
                return
        except Exception:
            return

        popup = tk.Toplevel(self.root)
        popup.title("Auto Sync")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        popup.grab_set()

        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        pw = round(300 * r)
        ph = round(160 * r)
        self._place_popup(popup, pw, ph)

        tk.Label(popup, text="🎬  동영상 재생 감지됨",
                 font=("Segoe UI", max(9, round(10 * r)), "bold"),
                 bg=self.BG, fg=self.TEXT).pack(pady=(round(18*r), round(6*r)))
        tk.Label(popup,
                 text="팟플레이어에서 동영상이 재생됩니다.\n싱크 보정을 시작할까요?",
                 font=("Segoe UI", max(8, round(9 * r))),
                 bg=self.BG, fg=self.TEXT, justify="center").pack()

        btn_f = tk.Frame(popup, bg=self.BG, pady=round(16*r))
        btn_f.pack()

        def on_yes():
            self._popup_open = False
            popup.destroy()
            self._toggle()

        def on_no():
            self._popup_open = False
            popup.destroy()
            threading.Thread(
                target=self._monitor_for_popup,
                kwargs={"wait_for_exit": True},
                daemon=True).start()

        BTN = dict(font=("Consolas", max(8, round(8 * r)), "bold"), relief="flat",
                   cursor="hand2", padx=round(16*r), pady=round(6*r))
        tk.Button(btn_f, text="▶  시작",
                  bg=self.BG3, fg=self.ACCENT, activebackground=self.BORDER,
                  command=on_yes, **BTN).pack(side="left", padx=round(6*r))
        tk.Button(btn_f, text="무시",
                  bg=self.BG3, fg=self.TEXT, activebackground=self.BORDER,
                  command=on_no, **BTN).pack(side="left", padx=round(6*r))

    def _start_processes(self):
        """P1·P2·P3 프로세스 시작."""
        self._stop_oped_monitor()   # 싱크 시작 시 별도 모니터 중지
        self._running = True
        self.stop_flag.value = False
        runtime_cfg = self._build_cfg()

        # GUI 메인스레드 → P3 공유 재생 위치/길이 (ms), -1 = 미확인
        self._shared_pos = Value(ctypes.c_longlong, -1)
        self._shared_dur = Value(ctypes.c_longlong, -1)

        # 싱크 ON 상태 전용 로그 큐 (P2 → GUI 직접 전달)
        self._main_log_queue = Queue(maxsize=200)

        for target, args in [
            (proc_lip_capture,   (self._lip_queue,   self.stop_flag, runtime_cfg)),
            (proc_audio_capture, (self._audio_queue, self.stop_flag, runtime_cfg,
                                  self._main_log_queue)),   # log_queue 전달
            (proc_analyzer,      (self._lip_queue, self._audio_queue,
                                  self.state_queue, self.cmd_queue,
                                  self.stop_flag, runtime_cfg,
                                  self._shared_pos, self._shared_dur)),
        ]:
            p = Process(target=target, args=args, daemon=True)
            p.start()
            self._processes.append(p)

        self._start_btn.config(text="⏹ 정지",
                               bg=self.BG3, fg=self.ACCENT2,
                               activebackground=self.BORDER,
                               state="normal")
        self._proc_lbl.config(text="P1·P2·P3 실행 중", fg=self.ACCENT3)
        self._toast("🎬 Auto Sync", "싱크 보정이 시작되었습니다.")

    def _stop_processes(self):
        self._running = False
        self.stop_flag.value = True
        for p in self._processes:
            p.join(timeout=2)
            if p.is_alive(): p.terminate()
        self._processes.clear()
        if hasattr(self, "_shared_pos"): self._shared_pos.value = -1
        if hasattr(self, "_shared_dur"): self._shared_dur.value = -1

    def _reset(self):
        if self._running:
            # 싱크 실행 중: P3에 reset + oped_reset 커맨드 전송
            try:
                self.cmd_queue.put_nowait("reset")
                self.cmd_queue.put_nowait("oped_reset")
            except Exception:
                pass
            return
        # 싱크 OFF: oped 모니터에 oped_reset 전송 + 팟플레이어 직접 초기화
        if getattr(self, "_oped_monitor_running", False):
            try:
                self._om_cmd_queue.put_nowait("oped_reset")
            except Exception:
                pass
        hwnd = find_potplayer_hwnd()
        if not hwnd:
            self._proc_lbl.config(text="초기화 실패: 팟플레이어 미감지", fg=self.ACCENT2)
            return
        try:
            post_key_to_potplayer(hwnd, VK_OEM_2, shift=True)
            time.sleep(0.05)
            post_key_to_potplayer(hwnd, 0x6F, shift=True)
            self._proc_lbl.config(text="수동 초기화 완료", fg=self.ACCENT3)
        except Exception:
            self._proc_lbl.config(text="초기화 실패", fg=self.ACCENT2)

    # ── 100ms 주기 UI 갱신 ────────────────────────────────────────────────────
    def _refresh(self):
        if self._closing:
            return

        # 재생 위치/길이 갱신 — 500ms 간격으로 throttle (FindWindowW 비용 절감)
        _now = time.time()
        if _now - getattr(self, '_hwnd_refresh_t', 0) >= 0.5:
            self._hwnd_refresh_t = _now
            hwnd = find_potplayer_hwnd()
            self._cached_hwnd = hwnd
            if hwnd:
                pos, dur = get_playback_info(hwnd)
                pv = pos if pos is not None else -1
                dv = dur if dur is not None else -1
                if self._running and hasattr(self, "_shared_pos"):
                    self._shared_pos.value = pv
                    self._shared_dur.value = dv
                if getattr(self, "_oped_monitor_running", False) and hasattr(self, "_om_shared_pos"):
                    self._om_shared_pos.value = pv
                    self._om_shared_dur.value = dv

        # oped 모니터 상태 진단 (매 5초마다)
        import time as _diag_t
        if _diag_t.time() - getattr(self, "_diag_t", 0) > 5:
            self._diag_t = _diag_t.time()
            running = getattr(self, "_oped_monitor_running", False)
            procs   = getattr(self, "_om_processes", [])
            alive   = [p.is_alive() for p in procs]
            msg = (f"[{_diag_t.strftime('%H:%M:%S')}] 🔧 oped_monitor={running} "
                   f"procs={len(procs)} alive={alive}")
            if not hasattr(self, "_log_lines"):
                self._log_lines = collections.deque(maxlen=100)
            self._log_lines.append(msg)

        # ── P2 로그 큐 수집 (싱크 ON/OFF 무관하게 항상 처리) ────────────────
        # audio_capture.py 의 send_log() 가 이미 타임스탬프를 붙여서 보냄
        for _lq_attr in ("_main_log_queue", "_om_log_queue"):
            _lq = getattr(self, _lq_attr, None)
            if _lq is None:
                continue
            while True:
                try:
                    msg = _lq.get_nowait()
                    if not hasattr(self, "_log_lines"):
                        self._log_lines = collections.deque(maxlen=100)
                    self._log_lines.append(f"🔊 {msg}")
                except Exception:
                    break

        # ── oped 모니터(싱크 OFF) state_queue 처리 ───────────────────────
        if getattr(self, "_oped_monitor_running", False):
            om_latest  = None
            om_prompts = []
            while True:
                try:
                    item = self._om_state_queue.get_nowait()
                    om_latest = item
                    p = item.get("oped_prompt") if isinstance(item, dict) else None
                    if p:
                        om_prompts.append(p)
                except Exception:
                    break
            for p in om_prompts:
                self._show_oped_skip_popup(p, use_om_queue=True)
            if om_latest:
                om_logs = om_latest.get("log_lines")
                if om_logs is not None:
                    # P3 log_lines 로 덮어쓰지 않고 새 항목만 추가
                    # (P2 오디오 로그가 사라지는 것을 방지)
                    existing = set(self._log_lines) if hasattr(self, "_log_lines") else set()
                    if not hasattr(self, "_log_lines"):
                        self._log_lines = collections.deque(maxlen=100)
                    for line in om_logs:
                        if line not in existing:
                            self._log_lines.append(line)
                            existing.add(line)
                # 싱크 OFF 상태에서 팟플레이어·오디오·프로세스 상태 표시 갱신
                pot_ok = om_latest.get("potplayer_ok", False)
                aud_n  = om_latest.get("audio_samples", 0)
                c = self.ACCENT3 if pot_ok else self.ACCENT2
                self._pot_dot.config(fg=c)
                self._pot_lbl.config(text="연결됨" if pot_ok else "미감지", fg=c)
                c = self.ACCENT3 if aud_n > 0 else self.TEXT_DIM
                self._aud_dot.config(fg=c)
                self._aud_lbl.config(text="캡처 중" if aud_n > 0 else "대기 중", fg=c)
                self._proc_dot.config(fg=self.ACCENT)
                self._proc_lbl.config(text="OP/ED 감지 중", fg=self.ACCENT)

        latest       = None
        main_toasts  = []
        main_prompts = []
        while True:
            try:
                item = self.state_queue.get_nowait()
                latest = item
                n = item.get("notify")
                if n:
                    main_toasts.append(n)
                p = item.get("oped_prompt") if isinstance(item, dict) else None
                if p:
                    main_prompts.append(p)
            except Exception:
                break

        for title, msg in main_toasts:
            threading.Thread(target=self._toast, args=(title, msg),
                             daemon=True).start()

        for p in main_prompts:
            self._show_oped_skip_popup(p)

        if latest:
            pot_ok = latest.get("potplayer_ok", False)
            aud_n  = latest.get("audio_samples", 0)
            lip_n  = latest.get("lip_samples", 0)
            offset = latest.get("offset_ms", 0.0)
            status = latest.get("status", "대기 중")
            corr   = latest.get("correction_ms", 0)
            logs   = latest.get("log_lines", [])

            c = self.ACCENT3 if pot_ok else self.ACCENT2
            t = "연결됨" if pot_ok else "미감지"
            self._pot_dot.config(fg=c); self._pot_lbl.config(text=t, fg=c)

            c = self.ACCENT3 if aud_n > 0 else self.TEXT_DIM
            t = "캡처 중" if aud_n > 0 else "대기 중"
            self._aud_dot.config(fg=c); self._aud_lbl.config(text=t, fg=c)

            if self._running and lip_n > 0:
                sign = "+" if offset > 0 else ""
                col  = (self.ACCENT2  if abs(offset) >= 80
                        else self.ACCENT3 if abs(offset) < 30
                        else self.ACCENT)
                self._offset_lbl.config(text=f"{sign}{offset:.0f} ms", fg=col)
            else:
                self._offset_lbl.config(text="— ms", fg=self.ACCENT)

            bw    = self._bar_ref.winfo_width()
            ratio = min(abs(offset) / 500, 1.0)
            col   = self.ACCENT2 if abs(offset) >= 80 else self.ACCENT3
            self._bar.place(x=0, y=0, width=int(bw * ratio), height=4)
            self._bar.config(bg=col)

            badge_map = {
                "정상":              (self.ACCENT3, self.BG3),
                "보정 완료":         (self.ACCENT,  self.BG3),
                "팟플레이어 미감지": (self.ACCENT2, self.BG3),
                "데이터 수집 중":    (self.TEXT,    self.BG3),
                "대기 중":           (self.TEXT,    self.BG3),
            }
            fg, bg = badge_map.get(status, (self.TEXT, self.BG3))
            self._badge.config(text=f"  {status}  ", fg=fg, bg=bg)

            sign = "+" if corr >= 0 else ""
            self._corr_lbl.config(text=f"{sign}{corr} ms")
            self._lip_cnt.config(text=str(lip_n))
            self._aud_cnt.config(text=str(aud_n))
            pc = self.ACCENT3 if self._running else self.TEXT_DIM
            self._proc_dot.config(fg=pc)
            # P3 log_lines 로 덮어쓰지 않고 새 항목만 추가
            existing = set(self._log_lines) if hasattr(self, "_log_lines") else set()
            if not hasattr(self, "_log_lines"):
                self._log_lines = collections.deque(maxlen=100)
            for line in logs:
                if line not in existing:
                    self._log_lines.append(line)
                    existing.add(line)

        self.root.after(100, self._refresh)

    # ── 인증 ──────────────────────────────────────────────────────────────────
    def _destroy_app_root(self):
        try:
            if self.root.winfo_exists():
                self.root.destroy()
        except Exception:
            pass

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
        popup.attributes("-topmost", True)
        popup.configure(bg=self.BORDER)
        popup.geometry(f"{pw}x{ph}+{px}+{py}")

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

    def _on_close(self):
        if getattr(self, "_app_shutdown_started", False):
            return
        self._app_shutdown_started = True
        self._closing = True
        self._popup_open = False
        if hasattr(self, "_popup_after_id"):
            try: self.root.after_cancel(self._popup_after_id)
            except Exception: pass
        self._save_pos()
        self._stop_processes()
        self._stop_oped_monitor()   # oped 모니터도 종료
        tray_ref = self._tray
        self._tray = None

        def _stop_tray_bg():
            if not tray_ref: return
            try: tray_ref.stop()
            except Exception: pass

        threading.Thread(target=_stop_tray_bg, daemon=True).start()
        try:
            self.root.after(320, self._destroy_app_root)
        except Exception:
            self._destroy_app_root()
