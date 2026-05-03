"""
gui/auth.py -- 인증 팝업 메서드
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
                elif s == _auth.AuthStatus.PENDING:
                    _auth.save_local_status(_auth.AuthStatus.PENDING)
                    self.root.after(0, self._show_auth_pending_popup)
            import threading as _t
            _t.Thread(target=_verify, daemon=True).start()
        elif status == _auth.AuthStatus.PENDING:
            self.root.after(0, self._show_auth_pending_popup)
        elif status == _auth.AuthStatus.REVOKED:
            self.root.after(0, self._show_auth_blocked_popup)
        else:
            self.root.after(0, self._show_auth_request_popup)

    def _after_auth_ok(self):
        """인증 완료 후 버전 체크 → 정상 실행 흐름 시작.

        [추가] 인증 OK 직후 백그라운드에서 서버 업데이트 시트 버전을 확인한다.
        - 버전 일치 또는 확인 실패 → 바로 _do_start_app() 호출
        - 버전 불일치               → 업데이트 안내 팝업 후 _do_start_app() 호출
        기존 실행 흐름은 모두 _do_start_app() 으로 유지된다.
        """
        self._auth_ok = True
        # [추가] 버전 체크를 백그라운드 스레드에서 수행 (UI 블로킹 방지)
        threading.Thread(
            target=self._check_version_and_start,
            daemon=True).start()

    def _do_start_app(self):
        """실제 앱 시작 로직 — 기존 _after_auth_ok 의 실행 흐름을 그대로 유지."""
        if self._autostart_var.get():
            self.root.after(500, self._toggle)
        else:
            # 자동 시작이 아닐 때 → oped 모니터 바로 시작
            self.root.after(200, self._start_oped_monitor)
        threading.Thread(
            target=self._monitor_for_popup,
            kwargs={"wait_for_exit": self._autostart_var.get()},
            daemon=True).start()
        # 5분마다 차단 여부 백그라운드 확인
        threading.Thread(target=self._monitor_auth, daemon=True).start()

    # ── [추가] 버전 체크 ────────────────────────────────────────────────────

    def _check_version_and_start(self):
        """백그라운드: 서버 업데이트 시트 버전 확인 → 결과에 따라 팝업 또는 바로 시작.

        - 서버 응답 실패 / 예외 발생 시 → 프로그램 종료 없이 바로 시작
        - 버전 일치   → 바로 시작
        - 버전 불일치 → G열 건너뛰기 여부 추가 확인 후 팝업 또는 바로 시작
        """
        import logging as _log
        try:
            resp    = _auth_module.check_version()
            latest  = resp.get("latest", "").strip()
            current = _auth_module.APP_VERSION
            if latest and latest != current:
                # G열 '차단' 여부 확인 — 차단이면 팝업 생략하고 바로 시작
                pc_id   = _auth_module.get_pc_id()
                skipped = _auth_module.check_update_skipped(pc_id)
                _log.debug(
                    "[update_skip] latest=%s current=%s skipped=%s",
                    latest, current, skipped)
                # skipped가 명시적으로 True일 때만 팝업 억제 (None/False/오류 → 팝업 표시)
                if skipped is True:
                    # G열 차단 확정 → 바로 시작
                    self.root.after(0, self._do_start_app)
                    return
                # 차단 아님 → 업데이트 팝업 표시
                self.root.after(
                    0, lambda: self._show_update_popup(current, latest))
                return
        except Exception:
            # 서버 응답 오류, 인터넷 미연결 등 → 무시하고 바로 시작
            pass
        # 버전 일치 / 체크 실패 → 바로 시작
        self.root.after(0, self._do_start_app)

    def _show_update_popup(self, current: str, latest: str):
        """버전 불일치 시 업데이트 안내 팝업.

        - "업데이트" / "나중에" 모두 팝업을 닫고 앱을 정상 시작한다.
        - 팝업 강제 종료(X 버튼) 시에도 앱은 정상 시작된다.
        - 예외 발생 시 팝업 없이 바로 시작한다.
        """
        # [수정] Bug 2: 팝업 진입 시점에 G열 '차단' 여부를 재확인한다.
        # _check_version_and_start()의 확인과 root.after() 스케줄 사이의
        # 타이밍 차이로 팝업이 노출될 수 있는 경로를 방어한다.
        try:
            if _auth_module.check_update_skipped(_auth_module.get_pc_id()):
                self._do_start_app()
                return
        except Exception:
            pass
        try:
            popup = tk.Toplevel(self.root)
            popup.title("Auto Sync — 업데이트")
            popup.resizable(False, False)
            popup.configure(bg=self.BG)
            popup.grab_set()

            r       = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
            # [수정] 높이를 200→250으로 확장하여 체크박스·버튼 잘림 방지
            self._place_popup(popup, round(300 * r), round(250 * r))

            PAD     = round(10 * r)   # [수정] 20→10: 구분선 위쪽(10*r)과 동일하게 맞춤
            F_TITLE = max(9,  round(11 * r))
            F_BODY  = max(8,  round(9  * r))
            F_SMALL = max(7,  round(8  * r))
            F_BTN   = max(8,  round(9  * r))

            # 팝업 닫기 + 앱 시작 (X 버튼 포함 모든 닫기 경로)
            def _close_and_start():
                try:
                    popup.destroy()
                except Exception:
                    pass
                self._do_start_app()

            popup.protocol("WM_DELETE_WINDOW", _close_and_start)

            # ── 제목 ──
            tk.Label(popup, text="업데이트 알림",
                     font=("Segoe UI", F_TITLE, "bold"),
                     bg=self.BG, fg=self.TEXT).pack(pady=(PAD, 0))
            tk.Frame(popup, bg=self.BORDER, height=1).pack(
                fill="x", pady=(round(10 * r), 0))

            # ── 버전 정보 ──
            info_f = tk.Frame(popup, bg=self.BG2,
                              padx=round(14 * r), pady=round(10 * r))
            info_f.pack(fill="x", padx=round(16 * r),
                        pady=(round(12 * r), 0))

            def _row(label, value, fg=None):
                row = tk.Frame(info_f, bg=self.BG2)
                row.pack(fill="x", pady=round(2 * r))
                tk.Label(row, text=label,
                         font=("Consolas", F_SMALL),
                         bg=self.BG2, fg=self.TEXT_MID,
                         width=8, anchor="e").pack(side="left")
                tk.Label(row, text=value,
                         font=("Consolas", F_SMALL, "bold"),
                         bg=self.BG2, fg=fg or self.TEXT).pack(
                    side="left", padx=(round(6 * r), 0))

            _row("현재 버전", current)
            _row("최신 버전", latest, fg=self.ACCENT3)

            # ── 안내 문구 ──
            tk.Label(popup,
                     text="새 버전이 있습니다.",
                     font=("Segoe UI", F_BODY),
                     bg=self.BG, fg=self.TEXT_MID).pack(
                pady=(round(8 * r), 0))

            tk.Frame(popup, bg=self.BORDER, height=1).pack(
                fill="x", padx=round(16 * r), pady=(round(12 * r), 0))

            # ── [추가] 버전 건너뛰기 체크박스 ──
            skip_var = tk.BooleanVar(value=False)
            tk.Checkbutton(popup, text="해당 버전 업데이트 건너뛰기",
                           variable=skip_var,
                           font=("Segoe UI", F_SMALL),
                           bg=self.BG, fg=self.TEXT_MID,
                           activebackground=self.BG,
                           selectcolor=self.BG2,
                           relief="flat", cursor="hand2").pack(
                pady=(round(4 * r), 0))

            # ── 버튼 ──
            btn_f = tk.Frame(popup, bg=self.BG)
            btn_f.pack(pady=round(8 * r))

            BTN = dict(font=("Consolas", F_BTN, "bold"),
                       relief="flat", cursor="hand2",
                       padx=round(14 * r), pady=round(5 * r))

            # [수정] Bug 1: "나중에" 핸들러.
            # skip 스레드를 _close_and_start() 이전에 기동한다.
            # 기존 순서(_close_and_start → 스레드 기동)에서는 _do_start_app() 내부
            # 예외가 전파될 경우 이후 블록이 실행되지 않아 G열이 갱신되지 않는다.
            # 스레드를 먼저 기동해 서버 요청을 독립적으로 보장한 뒤 팝업을 닫는다.
            # daemon=False: 앱 종료 시에도 HTTP 요청 완료 보장.
            def _on_later():
                should_skip = skip_var.get()   # popup 파괴 전에 값 확정 저장
                if should_skip:
                    pc_id = _auth_module.get_pc_id()
                    import logging as _log
                    _log.debug("[update_skip] skip_update_version 요청 시작: %s", pc_id)
                    threading.Thread(
                        target=_auth_module.skip_update_version,
                        args=(pc_id,),
                        daemon=False).start()
                _close_and_start()             # 팝업 닫기 + 앱 시작

            # [추가] 업데이트 버튼 참조 보관 → 체크박스 비활성 연동에 사용
            update_btn = tk.Button(btn_f, text="업데이트",
                                   bg=self.BG3, fg=self.ACCENT,
                                   activebackground=self.BORDER,
                                   command=_close_and_start, **BTN)
            update_btn.pack(side="left", padx=round(6 * r))

            tk.Button(btn_f, text="나중에",
                      bg=self.BG3, fg=self.TEXT,
                      activebackground=self.BORDER,
                      command=_on_later, **BTN).pack(
                side="left", padx=round(6 * r))

            # [추가] 체크박스 상태에 따라 업데이트 버튼 활성/비활성 전환
            def _on_skip_toggle(*_):
                try:
                    update_btn.configure(
                        state="disabled" if skip_var.get() else "normal")
                except Exception:
                    pass

            skip_var.trace_add("write", _on_skip_toggle)

        except Exception:
            # 팝업 생성 실패 시에도 앱은 정상 시작
            self._do_start_app()

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
