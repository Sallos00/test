"""
gui_run.py -- 실행 제어, 프로세스 관리, 갱신, 인증 팝업 메서드
"""
import os
import time
import threading
import collections
import tkinter as tk
import winreg
from multiprocessing import Process

from win32_utils import (
    CFG, find_potplayer_hwnd, is_potplayer_playing,
    is_potplayer_running
)
from processes import proc_lip_capture, proc_audio_capture, proc_analyzer


class LipSyncGUIRun:

    # ── 시작 / 정지 ───────────────────────────────────────────────────────────
    def _toggle(self):
        if not self._running:
            hwnd = find_potplayer_hwnd()
            if not hwnd:
                # 팟플레이어 미감지 → 대기 모드로 전환
                self._start_btn.config(text="⏳ 대기 중...",
                                       bg=self.BG3, fg=self.TEXT_DIM,
                                       activebackground=self.BORDER,
                                       state="disabled")
                self._proc_lbl.config(text="팟플레이어 실행을 기다리는 중...",
                                      fg=self.ACCENT)
                # 백그라운드 스레드에서 감지될 때까지 대기
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
            # 정지 후 팟플레이어 재실행 감지 모니터 재시작
            threading.Thread(
                target=self._monitor_for_popup,
                kwargs={"wait_for_exit": True},
                daemon=True).start()

    # ── Windows 토스트 알림 ───────────────────────────────────────────────────
    @staticmethod
    def _register_app_id():
        """
        Windows 10 토스트 알림을 위한 앱 ID 레지스트리 등록.
        HKCU\\SOFTWARE\\Classes\\AppUserModelId\\LipSyncMonitor
        Windows 11은 없어도 동작하지만 Windows 10은 필수.
        """
        try:
            key_path = r"SOFTWARE\Classes\AppUserModelId\LipSyncMonitor"
            key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path,
                                     0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "DisplayName", 0,
                              winreg.REG_SZ, "Auto Sync")
            winreg.CloseKey(key)
        except Exception:
            pass

    @staticmethod
    def _toast(title: str, msg: str):
        """
        Windows 10/11 토스트 알림 표시.
        winotify 우선 → 없으면 win32api 풍선 도움말로 폴백.
        """
        try:
            from winotify import Notification, audio
            n = Notification(app_id="LipSyncMonitor",
                             title=title,
                             msg=msg,
                             duration="short")
            n.set_audio(audio.Default, loop=False)
            n.show()
            return
        except Exception:
            pass
        # 폴백: 트레이 버블 알림 (구형 방식 / winotify 없을 때)
        try:
            import win32gui, win32con
            hwnd = win32gui.FindWindow("Shell_TrayWnd", None)
            if hwnd:
                win32gui.Shell_NotifyIcon(win32con.NIM_MODIFY, (
                    hwnd, 0,
                    win32con.NIF_INFO,
                    win32con.WM_USER + 20,
                    None,
                    msg, title, 5,
                    win32con.NIIF_INFO
                ))
        except Exception:
            pass

    def _wait_for_potplayer(self):
        """팟플레이어가 감지될 때까지 0.5초마다 확인. 감지되면 알림 + 자동 시작."""
        while True:
            hwnd = find_potplayer_hwnd()
            if hwnd:
                # 팟플레이어 감지 알림
                self._toast("🎬 Auto Sync",
                            "팟플레이어가 감지되었습니다.\n싱크 보정을 시작합니다.")
                # UI 업데이트는 메인 스레드에서
                self.root.after(0, self._start_processes)
                return
            time.sleep(0.5)

    def _monitor_for_popup(self, wait_for_exit=False):
        """
        자동 시작 OFF 상태에서 팝업 모니터링.

        wait_for_exit=False (최초 실행):
          → 팟플레이어 켜짐 + 비디오 재생 감지 시 즉시 팝업
        wait_for_exit=True (무시 후):
          → 팟플레이어가 완전히 종료될 때까지 대기
          → 이후 팟플레이어 재실행 + 비디오 재생 감지 시 팝업
        """
        # 무시 후: 팟플레이어가 완전히 종료될 때까지 대기
        if wait_for_exit:
            while not self._closing and not self._running:
                if not is_potplayer_running():
                    break
                for _ in range(10):
                    if self._closing or self._running: return
                    time.sleep(0.1)

        # 팟플레이어 켜짐 + 비디오 재생 감지 대기
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
        """
        동영상 재생 감지 시 팝업.
        자동 시작 OFF일 때만 호출됨.
        """
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
                 bg=self.BG, fg=self.TEXT,
                 justify="center").pack()

        btn_f = tk.Frame(popup, bg=self.BG, pady=round(16*r))
        btn_f.pack()

        def on_yes():
            self._popup_open = False
            popup.destroy()
            self._toggle()

        def on_no():
            self._popup_open = False
            popup.destroy()
            # 무시 후: 팟플레이어 종료 대기 → 재실행 감지 후 팝업
            threading.Thread(
                target=self._monitor_for_popup,
                kwargs={"wait_for_exit": True},
                daemon=True).start()

        BTN = dict(font=("Consolas", max(8, round(8 * r)), "bold"), relief="flat",
                   cursor="hand2", padx=round(16*r), pady=round(6*r))
        tk.Button(btn_f, text="▶  시작",
                  bg=self.BG3, fg=self.ACCENT,
                  activebackground=self.BORDER,
                  command=on_yes, **BTN).pack(side="left", padx=round(6*r))
        tk.Button(btn_f, text="무시",
                  bg=self.BG3, fg=self.TEXT,
                  activebackground=self.BORDER,
                  command=on_no, **BTN).pack(side="left", padx=round(6*r))

    def _start_processes(self):
        """프로세스 시작 (팟플레이어 감지 확인 후 호출)."""
        self._running = True
        self.stop_flag.value = False
        for target, args in [
            (proc_lip_capture,   (self._lip_queue,   self.stop_flag, CFG)),
            (proc_audio_capture, (self._audio_queue, self.stop_flag, CFG)),
            (proc_analyzer,      (self._lip_queue, self._audio_queue,
                                  self.state_queue, self.cmd_queue,
                                  self.stop_flag, CFG)),
        ]:
            p = Process(target=target, args=args, daemon=True)
            p.start()
            self._processes.append(p)
        self._start_btn.config(text="⏹ 정지",
                               bg=self.BG3, fg=self.ACCENT2,
                               activebackground=self.BORDER,
                               state="normal")
        self._proc_lbl.config(
            text=f"P1·P2·P3 실행 중  (PID {', '.join(str(p.pid) for p in self._processes)})",
            fg=self.ACCENT3)
        self._toast("🎬 Auto Sync", "싱크 보정이 시작되었습니다.")

    def _stop_processes(self):
        self._running = False
        self.stop_flag.value = True
        for p in self._processes:
            p.join(timeout=2)
            if p.is_alive(): p.terminate()
        self._processes.clear()

    def _reset(self):
        try: self.cmd_queue.put_nowait("reset")
        except Exception: pass

    # ── 100ms 주기 UI 갱신 ────────────────────────────────────────────────────
    def _refresh(self):
        latest = None
        while True:
            try: latest = self.state_queue.get_nowait()
            except Exception: break

        if latest:
            pot_ok  = latest.get("potplayer_ok", False)
            aud_n   = latest.get("audio_samples", 0)
            lip_n   = latest.get("lip_samples", 0)
            offset  = latest.get("offset_ms", 0.0)
            status  = latest.get("status", "대기 중")
            corr    = latest.get("correction_ms", 0)
            logs    = latest.get("log_lines", [])
            notify  = latest.get("notify", None)

            # 알림 팝업 (P3에서 요청 시)
            if notify:
                threading.Thread(
                    target=self._toast,
                    args=(notify[0], notify[1]),
                    daemon=True).start()

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

            self._bar_ref.update_idletasks()
            bw    = self._bar_ref.winfo_width()
            ratio = min(abs(offset) / 500, 1.0)
            col   = self.ACCENT2 if abs(offset) >= 80 else self.ACCENT3
            self._bar.place(x=0, y=0, width=int(bw * ratio), height=4)
            self._bar.config(bg=col)

            badge_map = {
                "정상":              (self.ACCENT3,  self.BG3),
                "보정 완료":         (self.ACCENT,   self.BG3),
                "팟플레이어 미감지": (self.ACCENT2,  self.BG3),
                "데이터 수집 중":    (self.TEXT,     self.BG3),
                "대기 중":           (self.TEXT,     self.BG3),
            }
            fg, bg = badge_map.get(status, (self.TEXT, self.BG3))
            self._badge.config(text=f"  {status}  ", fg=fg, bg=bg)

            sign = "+" if corr >= 0 else ""
            self._corr_lbl.config(text=f"{sign}{corr} ms")
            self._lip_cnt.config(text=str(lip_n))
            self._aud_cnt.config(text=str(aud_n))
            # 프로세스 점 색상 업데이트
            pc = self.ACCENT3 if self._running else self.TEXT_DIM
            self._proc_dot.config(fg=pc)
            # 전체 로그를 _log_lines에 저장 (로그 팝업용, 최대 100줄 FIFO)
            self._log_lines = collections.deque(logs, maxlen=100)

        self.root.after(100, self._refresh)

    # ── 인증 ──────────────────────────────────────────────────────────────────
    def _check_auth_on_start(self):
        """시작 시 인증 상태 확인. 로컬 인증 있으면 서버 검증, 없으면 첫 실행 팝업."""
        import auth as _auth
        local = _auth.get_local_auth()

        if local:
            # 로컬 인증 있음 → 서버 빠르게 검증
            def _on_approved(token):
                self.root.after(0, self._after_auth_ok)
            def _on_revoked():
                self.root.after(0, self._show_auth_blocked_popup)
            def _on_pending():
                self.root.after(0, self._show_auth_pending_popup)
            def _on_error(msg):
                # 오프라인 등 오류 → 로컬 인증으로 그냥 통과
                self.root.after(0, self._after_auth_ok)
            _auth.verify(_on_approved, _on_revoked, _on_error, on_pending=_on_pending)
        else:
            # 로컬 인증 없음 → 첫 실행 팝업
            self.root.after(0, self._show_auth_request_popup)

    def _after_auth_ok(self):
        """인증 완료 후 정상 실행 흐름 시작."""
        if self._autostart_var.get():
            self.root.after(500, self._toggle)
        threading.Thread(
            target=self._monitor_for_popup,
            kwargs={"wait_for_exit": self._autostart_var.get()},
            daemon=True).start()

    def _show_auth_request_popup(self):
        """첫 실행 인증 요청 팝업."""
        import auth as _auth

        popup = tk.Toplevel(self.root)
        popup.title("Auto Sync — 인증")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        popup.protocol("WM_DELETE_WINDOW", self._on_close)  # X 버튼 → 종료
        popup.grab_set()

        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        self._place_popup(popup, round(320 * r), round(270 * r))

        F_TITLE = max(9, round(11 * r))
        F_BODY  = max(8, round(9  * r))
        F_BTN   = max(8, round(9  * r))
        PAD     = round(20 * r)

        tk.Label(popup, text="사용 허가 인증",
                 font=("Segoe UI", F_TITLE, "bold"),
                 bg=self.BG, fg=self.TEXT).pack(pady=(PAD, 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(10*r), 0))

        self._auth_msg = tk.Label(popup,
                 text="이 PC에서 처음 실행됩니다.\n사용 허가 인증을 받으시겠습니까?",
                 font=("Segoe UI", F_BODY),
                 bg=self.BG, fg=self.TEXT, justify="center")
        self._auth_msg.pack(pady=(round(14*r), 0))

        # 사용자명 입력 필드
        name_f = tk.Frame(popup, bg=self.BG)
        name_f.pack(fill="x", padx=round(24*r), pady=(round(12*r), 0))
        tk.Label(name_f, text="사용자명",
                 font=("Consolas", F_BODY),
                 bg=self.BG, fg=self.TEXT,
                 anchor="center").pack(fill="x")
        name_var = tk.StringVar()
        name_entry = tk.Entry(name_f,
                 textvariable=name_var,
                 font=("Consolas", F_BODY),
                 bg=self.BG2, fg=self.TEXT,
                 insertbackground=self.ACCENT,
                 justify="center",
                 relief="flat", bd=0,
                 highlightthickness=1,
                 highlightbackground=self.BORDER,
                 highlightcolor=self.ACCENT)
        name_entry.pack(fill="x", pady=(4, 0), ipady=round(5*r))
        tk.Label(name_f, text="사용자명을 입력해야 확인 버튼이 활성화됩니다.",
                 font=("Consolas", max(7, round(8*r))),
                 bg=self.BG, fg=self.TEXT_MID,
                 anchor="center").pack(fill="x", pady=(4, 0))

        # 로딩 도트 (대기 중일 때 표시)
        self._auth_dot = tk.Label(popup, text="",
                 font=("Consolas", F_BODY),
                 bg=self.BG, fg=self.ACCENT)
        self._auth_dot.pack(pady=(round(6*r), 0))

        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x",
                 padx=round(16*r), pady=(round(8*r), 0))

        btn_f = tk.Frame(popup, bg=self.BG)
        btn_f.pack(pady=round(12*r))

        BTN = dict(font=("Consolas", F_BTN, "bold"), relief="flat",
                   cursor="hand2", padx=round(14*r), pady=round(5*r))

        # 확인 버튼 — 초기에는 비활성화
        self._auth_confirm_btn = tk.Button(btn_f, text="확인",
                  bg=self.BG3, fg=self.TEXT_DIM,
                  activebackground=self.BORDER,
                  state="disabled", **BTN)
        self._auth_confirm_btn.pack(side="left", padx=round(6*r))

        self._auth_close_btn = tk.Button(btn_f, text="닫기",
                  bg=self.BG3, fg=self.TEXT,
                  activebackground=self.BORDER,
                  command=self._on_close, **BTN)
        self._auth_close_btn.pack(side="left", padx=round(6*r))

        # 사용자명 입력 여부에 따라 확인 버튼 활성/비활성
        def on_name_change(*args):
            if name_var.get().strip():
                self._auth_confirm_btn.config(state="normal", fg=self.ACCENT)
            else:
                self._auth_confirm_btn.config(state="disabled", fg=self.TEXT_DIM)
        name_var.trace_add("write", on_name_change)
        name_entry.focus_set()

        stop_event = threading.Event()
        self._auth_stop_event = stop_event
        self._auth_popup      = popup

        def on_confirm():
            """확인 버튼 — 인증 요청 전송 후 폴링 시작."""
            username = name_var.get().strip()
            self._auth_confirm_btn.config(state="disabled", fg=self.TEXT_DIM)
            self._auth_close_btn.config(state="disabled")
            name_entry.config(state="disabled")
            self._auth_msg.config(text="인증 요청을 전송하는 중...")

            pc_id = _auth.get_pc_id()

            def _do_request():
                resp = _auth.request_auth(pc_id, username)
                if resp.get("ok") and resp.get("status") == "approved":
                    token = resp.get("token", "")
                    _auth.save_local_auth(pc_id, token)
                    self.root.after(0, lambda: _on_approved(token))
                elif resp.get("ok"):
                    self.root.after(0, _start_polling)
                else:
                    msg = resp.get("msg", "서버 연결 실패")
                    self.root.after(0, lambda: _on_request_error(msg))

            threading.Thread(target=_do_request, daemon=True).start()

        def _start_polling():
            """요청 성공 → 대기 UI로 전환 후 폴링 시작."""
            self._auth_msg.config(
                text="인증 요청이 전송됐습니다.\n허가를 기다리는 중입니다...")
            self._auth_close_btn.config(state="normal")
            self._auth_dot_count = 0
            self._animate_auth_dot()
            pc_id = _auth.get_pc_id()
            _auth.poll_until_approved(pc_id, _on_approved, _on_revoked,
                                      _on_request_error, stop_event)

        def _animate_auth_dot():
            """로딩 도트 애니메이션."""
            if stop_event.is_set(): return
            try:
                if not popup.winfo_exists(): return
            except Exception: return
            dots = ["●○○", "○●○", "○○●"]
            self._auth_dot_count = getattr(self, "_auth_dot_count", 0)
            self._auth_dot.config(text=dots[self._auth_dot_count % 3])
            self._auth_dot_count += 1
            popup.after(500, _animate_auth_dot)

        self._animate_auth_dot = _animate_auth_dot

        def _on_approved(token):
            """허가 완료 → 팝업 닫고 프로그램 실행."""
            stop_event.set()
            try:
                if popup.winfo_exists():
                    popup.destroy()
            except Exception: pass
            self._after_auth_ok()

        def _on_revoked():
            stop_event.set()
            self.root.after(0, self._show_auth_revoked_popup)
            try:
                if popup.winfo_exists(): popup.destroy()
            except Exception: pass

        def _on_request_error(msg):
            self._auth_msg.config(text=f"오류: {msg}\n다시 시도해 주세요.")
            name_entry.config(state="normal")
            on_name_change()  # 버튼 상태 재평가
            self._auth_close_btn.config(state="normal")
            self._auth_dot.config(text="")

        self._auth_confirm_btn.config(command=on_confirm)

    def _show_auth_pending_popup(self):
        """재실행 시 시트 상태가 대기 중일 때 팝업."""
        import auth as _auth

        popup = tk.Toplevel(self.root)
        popup.title("Auto Sync — 허가 대기 중")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        popup.protocol("WM_DELETE_WINDOW", self._on_close)
        popup.grab_set()

        r       = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        self._place_popup(popup, round(300 * r), round(180 * r))

        PAD     = round(20 * r)
        F_TITLE = max(9, round(11 * r))
        F_BODY  = max(8, round(9  * r))
        F_BTN   = max(8, round(9  * r))

        tk.Label(popup, text="사용 허가 대기 중",
                 font=("Segoe UI", F_TITLE, "bold"),
                 bg=self.BG, fg=self.TEXT).pack(pady=(PAD, 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(10*r), 0))
        tk.Label(popup,
                 text="프로그램 사용 허가 대기 중입니다.\n허가 후 재실행해 주세요.",
                 font=("Segoe UI", F_BODY),
                 bg=self.BG, fg=self.TEXT, justify="center").pack(pady=(round(14*r), 0))

        # 로딩 도트 애니메이션
        dot_lbl = tk.Label(popup, text="●○○",
                 font=("Consolas", F_BODY),
                 bg=self.BG, fg=self.ACCENT)
        dot_lbl.pack(pady=(round(4*r), 0))

        stop_event = threading.Event()

        def _animate():
            dots = ["●○○", "○●○", "○○●"]
            count = [0]
            def _tick():
                if stop_event.is_set(): return
                try:
                    if not popup.winfo_exists(): return
                except Exception: return
                dot_lbl.config(text=dots[count[0] % 3])
                count[0] += 1
                popup.after(500, _tick)
            _tick()
        _animate()

        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x",
                 padx=round(16*r), pady=(round(10*r), 0))
        btn_f = tk.Frame(popup, bg=self.BG)
        btn_f.pack(pady=round(10*r))
        tk.Button(btn_f, text="닫기",
                  font=("Consolas", F_BTN, "bold"),
                  bg=self.BG3, fg=self.TEXT,
                  activebackground=self.BORDER,
                  relief="flat", cursor="hand2",
                  padx=round(14*r), pady=round(5*r),
                  command=lambda: [stop_event.set(), self._on_close()]).pack()

    def _show_auth_blocked_popup(self):
        """이미 인증된 사용자가 차단됐을 때 팝업 (로컬 인증 유지)."""
        popup = tk.Toplevel(self.root)
        popup.title("Auto Sync — 사용 차단")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        popup.protocol("WM_DELETE_WINDOW", self._on_close)
        popup.grab_set()

        r       = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        self._place_popup(popup, round(300 * r), round(170 * r))

        PAD     = round(20 * r)
        F_TITLE = max(9, round(11 * r))
        F_BODY  = max(8, round(9  * r))
        F_BTN   = max(8, round(9  * r))

        tk.Label(popup, text="사용 차단",
                 font=("Segoe UI", F_TITLE, "bold"),
                 bg=self.BG, fg=self.ACCENT2).pack(pady=(PAD, 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(10*r), 0))
        tk.Label(popup,
                 text="프로그램 사용이 차단 되었습니다.\n프로그램을 종료합니다.",
                 font=("Segoe UI", F_BODY),
                 bg=self.BG, fg=self.TEXT, justify="center").pack(pady=(round(14*r), 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x",
                 padx=round(16*r), pady=(round(12*r), 0))
        tk.Button(popup, text="확인",
                  font=("Consolas", F_BTN, "bold"),
                  bg=self.BG3, fg=self.ACCENT2,
                  activebackground=self.BORDER,
                  relief="flat", cursor="hand2",
                  padx=round(14*r), pady=round(5*r),
                  command=self._on_close).pack(pady=round(10*r))

    def _show_auth_revoked_popup(self):
        """인증 거부 안내 팝업."""
        import auth as _auth
        _auth.clear_local_auth()

        popup = tk.Toplevel(self.root)
        popup.title("Auto Sync — 사용 허가 요청 거부")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        popup.protocol("WM_DELETE_WINDOW", self._on_close)
        popup.grab_set()

        r = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        self._place_popup(popup, round(300 * r), round(170 * r))

        PAD     = round(20 * r)
        F_TITLE = max(9, round(11 * r))
        F_BODY  = max(8, round(9  * r))
        F_BTN   = max(8, round(9  * r))

        tk.Label(popup, text="사용 허가 요청 거부",
                 font=("Segoe UI", F_TITLE, "bold"),
                 bg=self.BG, fg=self.ACCENT2).pack(pady=(PAD, 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(10*r), 0))
        tk.Label(popup,
                 text="사용 허가 요청이 거부됐습니다.\n프로그램을 종료합니다.",
                 font=("Segoe UI", F_BODY),
                 bg=self.BG, fg=self.TEXT, justify="center").pack(pady=(round(14*r), 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x",
                 padx=round(16*r), pady=(round(12*r), 0))
        tk.Button(popup, text="확인",
                  font=("Consolas", F_BTN, "bold"),
                  bg=self.BG3, fg=self.ACCENT2,
                  activebackground=self.BORDER,
                  relief="flat", cursor="hand2",
                  padx=round(14*r), pady=round(5*r),
                  command=self._on_close).pack(pady=round(10*r))

    # ── 종료 ─────────────────────────────────────────────────────────────────
    def _on_close(self):
        self._closing = True
        self._popup_open = False
        # 예약된 팝업 콜백 취소
        if hasattr(self, "_popup_after_id"):
            try: self.root.after_cancel(self._popup_after_id)
            except Exception: pass
        self._save_pos()
        self._stop_processes()
        if self._tray:
            try: self._tray.stop()
            except Exception: pass
        try: self.root.destroy()
        except Exception: pass
