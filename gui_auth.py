"""
gui_auth.py -- 인증 팝업 메서드
"""
import threading
import tkinter as tk

import auth as _auth_module
from win32_utils import CFG


class LipSyncGUIAuth:
    def _check_auth_on_start(self):
        """시작 시 인증 상태 확인. 로컬 인증 있으면 서버 검증, 없으면 첫 실행 팝업."""
        _auth = _auth_module
        local  = _auth.get_local_auth()
        status = _auth.get_local_status()

        if local and status == _auth.AuthStatus.APPROVED:
            # 허가 상태 → 즉시 실행, 백그라운드에서 차단 여부만 확인
            self.root.after(0, self._after_auth_ok)
            def _verify():
                resp = _auth.check_auth(local["pc_id"], local["token"])
                s = resp.get("status", "")
                if s == _auth.AuthStatus.REVOKED:
                    _auth.save_local_status(_auth.AuthStatus.REVOKED)
                    self.root.after(0, self._show_auth_blocked_popup)
            import threading as _t
            _t.Thread(target=_verify, daemon=True).start()
        elif status == _auth.AuthStatus.PENDING:
            self.root.after(0, self._show_auth_pending_popup)
        elif status == _auth.AuthStatus.REVOKED:
            self.root.after(0, self._show_auth_blocked_popup)
        else:
            self.root.after(0, self._show_auth_request_popup)

    def _after_auth_ok(self):
        """인증 완료 후 정상 실행 흐름 시작."""
        self._auth_ok = True
        if self._autostart_var.get():
            self.root.after(500, self._toggle)
        threading.Thread(
            target=self._monitor_for_popup,
            kwargs={"wait_for_exit": self._autostart_var.get()},
            daemon=True).start()
        # 5분마다 차단 여부 백그라운드 확인
        threading.Thread(target=self._monitor_auth, daemon=True).start()

    def _monitor_auth(self):
        """5분마다 서버에서 차단 여부 확인. 차단 시 싱크 중지 + 차단 팝업."""
        import time as _time
        while not self._closing:
            _time.sleep(300)  # 5분
            if self._closing: return
            try:
                local = _auth_module.get_local_auth()
                if not local: return
                resp = _auth_module.check_auth(local["pc_id"], local["token"])
                s = resp.get("status", "")
                if s == _auth_module.AuthStatus.REVOKED:
                    _auth_module.save_local_status(_auth_module.AuthStatus.REVOKED)
                    def _on_revoked():
                        # 싱크 중지
                        if self._running:
                            self._toggle()
                        # 차단 팝업
                        self._show_auth_blocked_popup()
                    self.root.after(0, _on_revoked)
                    return
            except Exception:
                pass

    def _show_auth_request_popup(self):
        """첫 실행 인증 요청 팝업."""
        _auth = _auth_module

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
                    # 대기 상태 로컬 저장 → 재실행 시 ④번 팝업 표시
                    _auth._save_settings({
                        "auth_id":     pc_id,
                        "auth_status": _auth.AuthStatus.PENDING,
                    })
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
            """허가 완료 → 메시지 변경 후 자동 닫힘."""
            stop_event.set()
            try:
                if not popup.winfo_exists(): return
            except Exception: return
            for w in popup.winfo_children():
                try: w.destroy()
                except Exception: pass
            r2 = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
            PAD2 = round(24 * r2)
            new_h = round(140 * r2)
            new_w = round(280 * r2)
            x = self.root.winfo_x() + (self.root.winfo_width()  - new_w) // 2
            y = self.root.winfo_y() + (self.root.winfo_height() - new_h) // 2
            popup.geometry(f"{new_w}x{new_h}+{x}+{y}")
            tk.Label(popup, text="허가 완료",
                     font=("Segoe UI", max(9, round(11*r2)), "bold"),
                     bg=self.BG, fg=self.ACCENT3).pack(pady=(PAD2, 0))
            tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(10*r2), 0))
            tk.Label(popup,
                     text="허가가 완료되었습니다!\n잠시 후 자동으로 실행됩니다.",
                     font=("Segoe UI", max(8, round(9*r2))),
                     bg=self.BG, fg=self.TEXT, justify="center").pack(pady=(round(16*r2), 0))
            popup.after(2000, lambda: [popup.destroy(), self._after_auth_ok()])

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
        _auth = _auth_module

        # 이미 열려있으면 중복 방지
        if hasattr(self, '_auth_pending_popup') and self._auth_pending_popup:
            try:
                if self._auth_pending_popup.winfo_exists():
                    return
            except Exception:
                pass

        popup = tk.Toplevel(self.root)
        popup.title("Auto Sync — 허가 대기 중")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        popup.protocol("WM_DELETE_WINDOW", lambda: [_auth.save_local_status(_auth.AuthStatus.PENDING), self._on_close()])
        popup.grab_set()
        self._auth_pending_popup = popup

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
                 text="프로그램 사용 허가 대기 중입니다.\n허가 후 자동으로 실행됩니다.",
                 font=("Segoe UI", F_BODY),
                 bg=self.BG, fg=self.TEXT, justify="center").pack(pady=(round(14*r), 0))

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

        # 백그라운드에서 서버 확인 (1초 후 첫 확인, 이후 10초마다)
        pc_id = _auth.get_pc_id()
        def _poll():
            first = True
            while not stop_event.is_set():
                if first:
                    stop_event.wait(1)   # 팝업 표시 후 1초 대기
                    first = False
                else:
                    stop_event.wait(10)
                if stop_event.is_set(): return
                resp = _auth.check_auth(pc_id)
                s = resp.get("status", "")
                if resp.get("ok") and s == _auth.AuthStatus.APPROVED:
                    token = resp.get("token", "")
                    _auth.save_local_auth(pc_id, token)
                    stop_event.set()
                    def _on_approved_ui():
                        try:
                            if not popup.winfo_exists(): return
                        except Exception: return
                        for w in popup.winfo_children():
                            try: w.destroy()
                            except Exception: pass
                        r2 = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
                        PAD2 = round(24 * r2)
                        new_h = round(140 * r2)
                        new_w = round(280 * r2)
                        x = self.root.winfo_x() + (self.root.winfo_width()  - new_w) // 2
                        y = self.root.winfo_y() + (self.root.winfo_height() - new_h) // 2
                        popup.geometry(f"{new_w}x{new_h}+{x}+{y}")
                        tk.Label(popup, text="허가 완료",
                                 font=("Segoe UI", max(9, round(11*r2)), "bold"),
                                 bg=self.BG, fg=self.ACCENT3).pack(pady=(PAD2, 0))
                        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(10*r2), 0))
                        tk.Label(popup,
                                 text="허가가 완료되었습니다!\n잠시 후 자동으로 실행됩니다.",
                                 font=("Segoe UI", max(8, round(9*r2))),
                                 bg=self.BG, fg=self.TEXT, justify="center").pack(pady=(round(16*r2), 0))
                        popup.after(2000, lambda: [popup.destroy(), self._after_auth_ok()])
                    self.root.after(0, _on_approved_ui)
                    return
                elif s == _auth.AuthStatus.REVOKED:
                    _auth.save_local_status(_auth.AuthStatus.REVOKED)
                    stop_event.set()
                    try:
                        if popup.winfo_exists(): popup.destroy()
                    except Exception: pass
                    self.root.after(0, self._show_auth_blocked_popup)
                    return
        threading.Thread(target=_poll, daemon=True).start()

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
                  command=lambda: [
                      stop_event.set(),
                      _auth.save_local_status(_auth.AuthStatus.PENDING),
                      self._on_close()
                  ]).pack()

    def _show_auth_blocked_popup(self):
        """이미 인증된 사용자가 차단됐을 때 팝업 — 서버 확인 후 해제 시 자동 실행."""
        _auth = _auth_module

        popup = tk.Toplevel(self.root)
        popup.title("Auto Sync — 사용 차단")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        popup.protocol("WM_DELETE_WINDOW", lambda: [_auth.save_local_status(_auth.AuthStatus.REVOKED), self._on_close()])
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
                  command=lambda: [
                      _auth.save_local_status(_auth.AuthStatus.REVOKED),
                      self._on_close()
                  ]).pack(pady=round(10*r))

        # 백그라운드에서 서버 확인 — 차단 해제(허가)되면 UI 전환
        stop_event = threading.Event()
        pc_id = _auth.get_pc_id()

        def _poll():
            first = True
            while not stop_event.is_set():
                if first:
                    stop_event.wait(1)
                    first = False
                else:
                    stop_event.wait(10)
                if stop_event.is_set(): return
                resp = _auth.check_auth(pc_id)
                s = resp.get("status", "")
                if resp.get("ok") and s == _auth.AuthStatus.APPROVED:
                    token = resp.get("token", "")
                    _auth.save_local_auth(pc_id, token)
                    stop_event.set()
                    def _on_unblocked():
                        try:
                            if not popup.winfo_exists(): return
                        except Exception: return
                        for w in popup.winfo_children():
                            try: w.destroy()
                            except Exception: pass
                        r2 = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
                        new_h = round(140 * r2)
                        new_w = round(280 * r2)
                        x = self.root.winfo_x() + (self.root.winfo_width()  - new_w) // 2
                        y = self.root.winfo_y() + (self.root.winfo_height() - new_h) // 2
                        popup.geometry(f"{new_w}x{new_h}+{x}+{y}")
                        tk.Label(popup, text="차단 해제",
                                 font=("Segoe UI", max(9, round(11*r2)), "bold"),
                                 bg=self.BG, fg=self.ACCENT3).pack(pady=(round(24*r2), 0))
                        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(10*r2), 0))
                        tk.Label(popup,
                                 text="차단이 해제되어 정상 이용 가능합니다!\n잠시 후 자동으로 실행됩니다.",
                                 font=("Segoe UI", max(8, round(9*r2))),
                                 bg=self.BG, fg=self.TEXT, justify="center").pack(pady=(round(16*r2), 0))
                        popup.after(2000, lambda: [popup.destroy(), self._after_auth_ok()])
                    self.root.after(0, _on_unblocked)
                    return

        threading.Thread(target=_poll, daemon=True).start()

    def _show_auth_revoked_popup(self):
        """인증 거부 안내 팝업."""
        _auth = _auth_module
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
