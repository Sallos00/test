"""

gui_ui.py -- GUI 창/UI 구성, 팝업(설정/로그/메뉴) 메서드

"""

import tkinter as tk

from app_icon import apply_to_root_window
from win32_utils import find_potplayer_hwnd, get_playback_info, do_oped_skip

class LipSyncGUIUI:

    def _tray_show(self, icon=None, item=None):

        """트레이에서 창 다시 열기."""

        self.root.after(0, self.root.deiconify)

    def _tray_quit(self, icon=None, item=None):

        """트레이에서 완전 종료.

        pystray 메뉴 콜백은 알림 영역 메시지 루프 스레드에서 실행된다.
        트레이 메뉴가 닫힌 뒤 짧은 지연을 두고 _on_close를 호출한다.
        (_on_close 내부에서 트레이 stop은 백그라운드 스레드로 처리한다.)
        """

        # 트레이 팝업 메뉴가 완전히 닫힌 뒤 종료 루틴 실행 (교착·무응답 완화)
        try:

            self.root.after(120, self._on_close)

        except Exception:

            try:

                self._on_close()

            except Exception:

                pass

    # ── 창 설정 ───────────────────────────────────────────────────────────────

    def _build_window(self):

        r = self.root

        r.withdraw()

        r.title("Auto Sync")

        r.geometry(f"{self.W}x{self.H}")

        r.resizable(False, False)

        r.configure(bg=self.BG)

        try:

            apply_to_root_window(r)

        except Exception:

            pass

        r.update_idletasks()

        x, y = self._load_pos()

        r.geometry(f"{self.W}x{self.H}+{x}+{y}")

        r.deiconify()

    # ── UI 구성 ───────────────────────────────────────────────────────────────

    def _build_ui(self):

        MONO   = ("Consolas", self.F_MONO)

        MONO_S = ("Consolas", self.F_MONO_S)

        r = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]

        PAD  = max(10, round(18 * r))

        PAD2 = max(8,  round(14 * r))

        self._theme_widgets = []

        def reg(w, bg=None, fg=None, abg=None, afg=None, obg=None):

            self._theme_widgets.append((w, bg, fg, abg, afg, obg))

            return w

        # 헤더

        hdr = reg(tk.Frame(self.root, bg=self.BG, pady=0), bg="BG")

        hdr.pack(fill="x", padx=PAD, ipady=PAD2)

        ic_size = round(32 * r)

        self._icon_canvas = tk.Canvas(hdr, width=ic_size, height=ic_size,

                                      bg=self.BG, highlightthickness=0)

        self._icon_canvas.pack(side="left", anchor="center")

        self._icon_canvas.create_oval(1, 1, ic_size-1, ic_size-1,

                                      fill=self.BG3, outline=self.ACCENT, width=2)

        tx1 = round(12*r); ty1 = round(8*r)

        tx2 = round(12*r); ty2 = round(24*r)

        tx3 = round(26*r); ty3 = round(16*r)

        self._icon_canvas.create_polygon(tx1, ty1, tx2, ty2, tx3, ty3,

                                         fill=self.ACCENT, outline="")

        tf = reg(tk.Frame(hdr, bg=self.BG), bg="BG")

        tf.pack(side="left", padx=10, anchor="center")

        reg(tk.Label(tf, text="Auto Sync", font=("Segoe UI", self.F_TITLE, "bold"),

                     bg=self.BG, fg=self.TEXT), bg="BG", fg="TEXT").pack(anchor="w")

        reg(tk.Label(tf, text="PotPlayer 자동 싱크 보정 | 멀티코어",

                     font=("Segoe UI", max(7, self.F_TITLE - 5)), bg=self.BG, fg=self.TEXT_MID),

            bg="BG", fg="TEXT_MID").pack(anchor="w")

        right_f = reg(tk.Frame(hdr, bg=self.BG), bg="BG")

        right_f.pack(side="right", anchor="center")

        reg(tk.Label(right_f, text="v2.0", font=("Consolas", 7),

                     bg=self.ACCENT, fg="#0e0e0e", padx=5, pady=2),

            bg="ACCENT").pack(anchor="e")

        gear_fg = self.ACCENT if self._darkmode_var.get() else self.TEXT

        self._gear_btn = reg(tk.Button(right_f, text="⚙",

                                       font=("Segoe UI", self.F_GEAR),

                                       bg=self.BG, fg=gear_fg,

                                       activebackground=self.BG2,

                                       activeforeground=gear_fg,

                                       relief="flat", cursor="hand2",

                                       bd=0, padx=2, pady=2,

                                       command=self._toggle_gear_menu),

                             bg="BG", fg="GEAR_FG", abg="BG2", afg="GEAR_FG")

        self._gear_btn.pack(anchor="e", pady=(4, 0))

        reg(tk.Frame(self.root, bg=self.BORDER, height=1), bg="BORDER").pack(fill="x")

        # 상태 카드

        card = reg(tk.Frame(self.root, bg=self.BG2, pady=12, padx=16), bg="BG2")

        card.pack(fill="x", padx=PAD2, pady=(round(12*r), 0))

        def status_row(parent, label):

            row = reg(tk.Frame(parent, bg=self.BG2), bg="BG2")

            row.pack(fill="x", pady=2)

            reg(tk.Label(row, text=label, font=MONO, bg=self.BG2, fg=self.TEXT_MID,

                         width=11, anchor="w"), bg="BG2", fg="TEXT_MID").pack(side="left", anchor="center")

            dot = reg(tk.Label(row, text="●", font=("Consolas", 8),

                               bg=self.BG2, fg=self.TEXT_DIM), bg="BG2", fg="TEXT_DIM")

            dot.pack(side="left", anchor="center")

            lbl = reg(tk.Label(row, text="—", font=MONO,

                               bg=self.BG2, fg=self.TEXT_MID), bg="BG2", fg="TEXT_MID")

            lbl.pack(side="left", padx=4, anchor="center")

            return dot, lbl

        self._pot_dot, self._pot_lbl = status_row(card, "팟플레이어")

        self._aud_dot, self._aud_lbl = status_row(card, "오디오 장치")

        proc_row = reg(tk.Frame(card, bg=self.BG2), bg="BG2")

        proc_row.pack(fill="x", pady=(6, 0))

        reg(tk.Label(proc_row, text="프로세스", font=MONO,

                     bg=self.BG2, fg=self.TEXT_MID,

                     width=11, anchor="w"), bg="BG2", fg="TEXT_MID").pack(side="left", anchor="center")

        self._proc_dot = reg(tk.Label(proc_row, text="●", font=("Consolas", 8),

                                      bg=self.BG2, fg=self.TEXT_DIM), bg="BG2", fg="TEXT_DIM")

        self._proc_dot.pack(side="left", anchor="center")

        self._proc_lbl = reg(tk.Label(proc_row, text="대기 중", font=MONO,

                                      bg=self.BG2, fg=self.TEXT_MID), bg="BG2", fg="TEXT_MID")

        self._proc_lbl.pack(side="left", padx=4, anchor="center")

        # ── 재생 위치 / 전체 길이 행 ──────────────────────────────────────────

        dur_row = reg(tk.Frame(card, bg=self.BG2), bg="BG2")

        dur_row.pack(fill="x", pady=(4, 0))

        reg(tk.Label(dur_row, text="재생 위치", font=MONO,

                     bg=self.BG2, fg=self.TEXT_MID,

                     width=11, anchor="w"), bg="BG2", fg="TEXT_MID").pack(side="left", anchor="center")

        self._dur_lbl = reg(tk.Label(dur_row, text="— / —", font=MONO,

                                     bg=self.BG2, fg=self.TEXT_MID),

                            bg="BG2", fg="TEXT_MID")

        self._dur_lbl.pack(side="left", padx=4, anchor="center")

        # ── OP/ED 스킵 버튼 (오프셋 섹션 바로 위) ────────────────────────────
        # 자동 스킵 OFF → 버튼 활성 (수동 스킵 가능)
        # 자동 스킵 ON  → 버튼 비활성 + "자동 스킵 ON" 표시
        oped_row = reg(tk.Frame(self.root, bg=self.BG, padx=PAD), bg="BG")
        oped_row.pack(fill="x", pady=(round(12*r), 0))

        self._oped_btn = reg(tk.Button(oped_row, font=("Consolas", max(8, round(9*r)), "bold"),
                                       relief="flat", cursor="hand2",
                                       padx=round(8*r), pady=0,
                                       command=self._oped_skip),
                             bg="BG3", fg="ACCENT3", abg="BORDER")
        self._oped_btn.pack(fill="x")

        # 초기 상태 반영
        self._update_oped_btn()

        reg(tk.Frame(self.root, bg=self.BORDER, height=1),
            bg="BORDER").pack(fill="x", padx=PAD2, pady=(round(8*r), 0))

        # 오프셋 미터

        mf = reg(tk.Frame(self.root, bg=self.BG, pady=round(10*r), padx=PAD), bg="BG")

        mf.pack(fill="both", expand=True)

        top = reg(tk.Frame(mf, bg=self.BG), bg="BG")

        top.pack(fill="x")

        reg(tk.Label(top, text="OFFSET", font=("Consolas", 7, "bold"),

                     bg=self.BG, fg=self.TEXT_DIM), bg="BG", fg="TEXT_DIM").pack(side="left")

        self._badge = reg(tk.Label(top, text=" 대기 중 ",

                                   font=("Consolas", max(7, round(8*r)), "bold"),

                                   bg=self.BG3, fg=self.TEXT,

                                   padx=round(6*r), pady=2),

                          bg="BG3", fg="TEXT")

        self._badge.pack(side="right")

        self._offset_lbl = reg(tk.Label(mf, text="— ms",

                                        font=("Consolas", self.F_OFFSET, "bold"),

                                        bg=self.BG, fg=self.ACCENT), bg="BG", fg="ACCENT")

        self._offset_lbl.pack(anchor="w", pady=(2, 0))

        bar_bg = reg(tk.Frame(mf, bg=self.BG3, height=4), bg="BG3")

        bar_bg.pack(fill="x", pady=(4, 0))

        bar_bg.pack_propagate(False)

        self._bar = tk.Frame(bar_bg, bg=self.ACCENT, height=4)

        self._bar.place(x=0, y=0, width=0, height=4)

        self._bar_ref = bar_bg

        # 샘플 카운터 + 누적 보정

        row1 = reg(tk.Frame(mf, bg=self.BG), bg="BG")

        row1.pack(fill="x", pady=(6, 0))

        reg(tk.Label(row1, text="이미지 샘플", font=MONO_S,

                     bg=self.BG, fg=self.TEXT_MID), bg="BG", fg="TEXT_MID").pack(side="left")

        self._lip_cnt = reg(tk.Label(row1, text="0", font=MONO_S,

                                     bg=self.BG, fg=self.TEXT), bg="BG", fg="TEXT")

        self._lip_cnt.pack(side="left", padx=(4, 16))

        reg(tk.Label(row1, text="오디오 샘플", font=MONO_S,

                     bg=self.BG, fg=self.TEXT_MID), bg="BG", fg="TEXT_MID").pack(side="left")

        self._aud_cnt = reg(tk.Label(row1, text="0", font=MONO_S,

                                     bg=self.BG, fg=self.TEXT), bg="BG", fg="TEXT")

        self._aud_cnt.pack(side="left", padx=(4, 0))

        row2 = reg(tk.Frame(mf, bg=self.BG), bg="BG")

        row2.pack(fill="x", pady=(3, 0))

        reg(tk.Label(row2, text="누적 보정", font=MONO_S,

                     bg=self.BG, fg=self.TEXT_MID), bg="BG", fg="TEXT_MID").pack(side="left")

        self._corr_lbl = reg(tk.Label(row2, text="+0 ms", font=MONO_S,

                                      bg=self.BG, fg=self.TEXT), bg="BG", fg="TEXT")

        self._corr_lbl.pack(side="left", padx=(4, 0))

        reg(tk.Frame(self.root, bg=self.BORDER, height=1), bg="BORDER").pack(fill="x", padx=PAD2)

        # ── 버튼 행 1: 시작 / 초기화 / 종료 ─────────────────────────────────

        bf = reg(tk.Frame(self.root, bg=self.BG, padx=round(10*r), pady=round(6*r)), bg="BG")

        bf.pack(fill="x")

        bf.columnconfigure(2, weight=1)

        bf.rowconfigure(0, minsize=round(32*r))

        BTN = dict(font=("Consolas", max(8, round(9*r)), "bold"), relief="flat",

                   cursor="hand2", padx=round(8*r), pady=0, anchor="center")

        self._start_btn = reg(tk.Button(bf, text="▶ 시작",

                                        bg=self.BG3, fg=self.ACCENT,

                                        activebackground=self.BORDER,

                                        command=self._toggle, **BTN),

                              bg="BG3", fg="ACCENT", abg="BORDER")

        self._start_btn.grid(row=0, column=0, padx=(0, 2), sticky="nsew")

        self._reset_btn = reg(tk.Button(bf, text="↺ 초기화",

                                        bg=self.BG3, fg=self.TEXT_MID,

                                        activebackground=self.BORDER,

                                        command=self._reset, **BTN),

                              bg="BG3", fg="TEXT_MID", abg="BORDER")

        self._reset_btn.grid(row=0, column=1, padx=2, sticky="nsew")

        reg(tk.Frame(bf, bg=self.BG), bg="BG").grid(row=0, column=2, sticky="nsew")

        self._close_btn = reg(tk.Button(bf, text="✕ 종료",

                                        bg=self.BG3, fg=self.ACCENT2,

                                        activebackground=self.BORDER,

                                        command=self._on_close, **BTN),

                              bg="BG3", fg="ACCENT2", abg="BORDER")

        self._close_btn.grid(row=0, column=3, padx=(2, 0), sticky="nsew")

        # 재생 위치 폴링 시작 (1초 간격, 독립적으로 실행)

        self.root.after(1000, self._poll_playback_info)

    # ── OP/ED 스킵 버튼 상태 동기화 ──────────────────────────────────────────

    def _update_oped_btn(self):

        """_oped_auto_var / _oped_skip_sec_var 값에 따라 버튼 레이블·상태를 갱신."""

        if not hasattr(self, "_oped_btn"):

            return

        try:

            sec = int(self._oped_skip_sec_var.get())

        except (ValueError, AttributeError):

            sec = 90

        if self._oped_auto_var.get():

            self._oped_btn.config(

                text=f"⏭ 자동 스킵 ON  ({sec}초)",

                state="disabled",

                bg=self.BG3,

                fg=self.TEXT_DIM,

                activebackground=self.BORDER,

            )

        else:

            self._oped_btn.config(

                text=f"⏭ OP/ED 스킵  ({sec}초)",

                state="normal",

                bg=self.BG3,

                fg=self.ACCENT3,

                activebackground=self.BORDER,

            )

    # ── OP/ED 수동 스킵 실행 ─────────────────────────────────────────────────

    def _oped_skip(self):

        """메인창 OP/ED 스킵 버튼 핸들러 — 팟플레이어에 직접 SendMessage."""

        hwnd = find_potplayer_hwnd()

        if not hwnd:

            return

        pos_ms, dur_ms = get_playback_info(hwnd)

        if pos_ms is None:

            return

        try:

            skip_sec = max(10, min(600, int(self._oped_skip_sec_var.get())))

        except (ValueError, AttributeError):

            skip_sec = 90

        new_pos, ok = do_oped_skip(hwnd, pos_ms, dur_ms, skip_sec)

        if ok:

            def fmt(ms):

                s = ms // 1000

                return f"{s // 60}:{s % 60:02d}"

            if hasattr(self, "_log_lines"):

                import time as _t

                self._log_lines.append(

                    f"[{_t.strftime('%H:%M:%S')}]"

                    f" ⏭ 수동 스킵: {fmt(pos_ms)} → {fmt(new_pos)}"

                    f"  (전체 {fmt(dur_ms)})"

                )

    # ── 재생 위치 폴링 (1초 간격) ─────────────────────────────────────────────

    def _poll_playback_info(self):

        """팟플레이어에서 재생 위치/전체 길이를 읽어 상태 카드에 표시한다."""

        try:

            hwnd = find_potplayer_hwnd()

            if hwnd:

                pos_ms, dur_ms = get_playback_info(hwnd)

                if pos_ms is not None:

                    def fmt(ms):

                        s = ms // 1000

                        return f"{s // 60}:{s % 60:02d}"

                    if dur_ms is not None:
                        self._dur_lbl.config(text=f"{fmt(pos_ms)} / {fmt(dur_ms)}")
                    else:
                        self._dur_lbl.config(text=f"{fmt(pos_ms)} / —")

                else:

                    self._dur_lbl.config(text="— / —")

            else:

                self._dur_lbl.config(text="— / —")

        except Exception:

            pass

        # 창이 살아 있는 동안 계속 폴링

        if not self._closing:

            self.root.after(1000, self._poll_playback_info)

    # ── 톱니바퀴 드롭다운 메뉴 ───────────────────────────────────────────────

    def _toggle_gear_menu(self):

        if self._gear_menu_open:

            self._close_gear_menu()

        else:

            self._open_gear_menu()

    def _open_gear_menu(self):

        self._gear_menu_open = True

        self.root.update_idletasks()

        r = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]

        rx = self.root.winfo_rootx()

        ry = self.root.winfo_rooty()

        bx = self._gear_btn.winfo_rootx() - rx

        by = self._gear_btn.winfo_rooty() - ry + self._gear_btn.winfo_height() + 2

        mw = round(140 * r)

        frame = tk.Frame(self.root, bg=self.BORDER, bd=1, relief="solid")

        self._gear_menu_frame = frame

        ITEM = dict(font=("Consolas", max(8, round(9 * r))),

                    bg=self.BG2, fg=self.TEXT,

                    relief="flat", cursor="hand2",

                    activebackground=self.BG3, activeforeground=self.TEXT,

                    anchor="w", padx=round(14 * r), pady=round(7 * r))

        def pick(fn):

            self._close_gear_menu()

            fn()

        tk.Button(frame, text="⚙ 설정",

                  command=lambda: pick(self._open_settings), **ITEM).pack(fill="x")

        tk.Frame(frame, bg=self.BORDER, height=1).pack(fill="x")

        tk.Button(frame, text="📋 로그 보기",

                  command=lambda: pick(self._open_log_popup), **ITEM).pack(fill="x")

        frame.update_idletasks()

        frame.place(x=bx + self._gear_btn.winfo_width() - mw, y=by)

        frame.lift()

        def on_root_click(e):

            try:

                fx1 = frame.winfo_rootx()

                fy1 = frame.winfo_rooty()

                fx2 = fx1 + frame.winfo_width()

                fy2 = fy1 + frame.winfo_height()

                gx1 = self._gear_btn.winfo_rootx()

                gy1 = self._gear_btn.winfo_rooty()

                gx2 = gx1 + self._gear_btn.winfo_width()

                gy2 = gy1 + self._gear_btn.winfo_height()

                in_frame = fx1 <= e.x_root <= fx2 and fy1 <= e.y_root <= fy2

                in_gear  = gx1 <= e.x_root <= gx2 and gy1 <= e.y_root <= gy2

                if not in_frame and not in_gear:

                    self._close_gear_menu()

            except Exception:

                self._close_gear_menu()

        self.root.bind("<Button-1>", on_root_click)

    def _close_gear_menu(self):

        self._gear_menu_open = False

        if hasattr(self, "_gear_menu_frame") and self._gear_menu_frame:

            try: self._gear_menu_frame.destroy()

            except Exception: pass

        self._gear_menu_frame = None

        try: self.root.unbind("<Button-1>")

        except Exception: pass

    # ── 로그 팝업 ─────────────────────────────────────────────────────────────

    def _open_log_popup(self):

        popup = tk.Toplevel(self.root)

        popup.title("로그")

        popup.resizable(False, True)

        popup.configure(bg=self.BG)

        popup.grab_set()

        s = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])

        r = s["scale"]

        pw = round(320 * r)

        ph = round(280 * r)

        self._place_popup(popup, pw, ph)

        F_TITLE = max(9, round(11 * r))

        F_BTN   = max(8, round(9  * r))

        PAD     = round(10 * r)

        PAD2    = round(14 * r)

        tk.Label(popup, text="📋 로그",

                 font=("Segoe UI", F_TITLE, "bold"),

                 bg=self.BG, fg=self.TEXT).pack(pady=(PAD, 0))

        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(6*r), 0))

        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", padx=PAD2, pady=(round(8*r), 0), side="bottom")

        bf = tk.Frame(popup, bg=self.BG)

        bf.pack(side="bottom", pady=PAD)

        tk.Button(bf, text="🗑 로그 지우기",

                  font=("Consolas", F_BTN, "bold"),

                  bg=self.BG3, fg=self.TEXT,

                  activebackground=self.BORDER,

                  relief="flat", cursor="hand2",

                  padx=round(12*r), pady=round(5*r),

                  command=self._clear_log).pack(side="left", padx=round(6*r))

        tk.Button(bf, text="닫기",

                  font=("Consolas", F_BTN, "bold"),

                  bg=self.BG3, fg=self.TEXT,

                  activebackground=self.BORDER,

                  relief="flat", cursor="hand2",

                  padx=round(12*r), pady=round(5*r),

                  command=popup.destroy).pack(side="left", padx=round(6*r))

        frame = tk.Frame(popup, bg=self.BG2, padx=2, pady=2)

        frame.pack(fill="both", expand=True, padx=PAD2, pady=(round(6*r), 0))

        scrollbar = tk.Scrollbar(frame)

        scrollbar.pack(side="right", fill="y")

        txt = tk.Text(frame,

                      font=("Consolas", max(8, round(9*r))),

                      bg=self.BG2, fg=self.TEXT,

                      insertbackground=self.ACCENT,

                      selectbackground=self.BG3,

                      relief="flat", bd=0,

                      wrap="word", state="normal",

                      yscrollcommand=scrollbar.set)

        txt.pack(side="left", fill="both", expand=True)

        # 드래그 선택/복사는 허용하고, 직접 편집은 차단
        def _block_edit(event):
            return "break"

        txt.bind("<Key>", _block_edit)
        txt.bind("<<Paste>>", _block_edit)
        txt.bind("<<Cut>>", _block_edit)

        scrollbar.config(command=txt.yview)

        txt.tag_config("ok",   foreground=self.ACCENT3)

        txt.tag_config("info", foreground=self.ACCENT)

        txt.tag_config("warn", foreground="#e0a03c")

        txt.tag_config("err",  foreground=self.ACCENT2)

        txt.tag_config("skip", foreground="#b58cff")

        txt.tag_config("detect", foreground="#4ec9f0")

        txt.tag_config("sync", foreground="#ffd166")

        txt.tag_config("dim",  foreground=self.TEXT_DIM)

        self._log_popup_txt = txt

        self._update_log_popup()

        def auto_refresh():

            if popup.winfo_exists():

                self._update_log_popup()

                popup.after(1000, auto_refresh)

        popup.after(1000, auto_refresh)

    def _update_log_popup(self):

        try:

            txt = self._log_popup_txt

            # 사용자가 이미 아래쪽을 보고 있을 때만 자동 하단 이동
            y1, y2 = txt.yview()
            at_bottom = y2 >= 0.999

            txt.delete("1.0", "end")

            lines = list(self._log_lines) if hasattr(self, "_log_lines") else []

            if not lines:

                txt.insert("end", "— 로그 없음 —", "dim")

            else:

                for i, line in enumerate(lines):

                    if i > 0:

                        txt.insert("end", "\n")

                    if any(k in line for k in ("⏭", "오프닝", "엔딩", "스킵")):
                        tag = "skip"

                    elif any(k in line for k in ("🎬", "👁", "🔊", "감지", "미감지", "대기")):
                        tag = "detect"

                    elif any(k in line for k in ("보정", "OFFSET", "싱크", "상한")):
                        tag = "sync"

                    elif any(k in line for k in ("▶", "↺", "🔄", "정상", "OK")):

                        tag = "ok"

                    elif any(k in line for k in ("📊", "정보", "상태")):

                        tag = "info"

                    elif any(k in line for k in ("⚠", "주의", "경고")):

                        tag = "warn"

                    elif any(k in line for k in ("❌", "오류", "실패", "취소")):

                        tag = "err"

                    else:

                        tag = "dim"

                    txt.insert("end", line, tag)

            if at_bottom:
                txt.see("end")
            else:
                txt.yview_moveto(y1)

        except Exception:

            pass

    def _clear_log(self):

        if hasattr(self, "_log_lines"):

            self._log_lines.clear()

        self._update_log_popup()

    # ── 설정 팝업 ─────────────────────────────────────────────────────────────

    def _open_settings(self):

        popup = tk.Toplevel(self.root)

        popup.title("설정")

        popup.resizable(False, False)

        popup.configure(bg=self.BG)

        popup.grab_set()

        r = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]

        pw = round(300 * r)

        ph = round(350 * r)   # OP/ED 섹션 추가로 기존 300 → 380

        self._place_popup(popup, pw, ph)

        F_TITLE = max(9,  round(11 * r))

        F_MONO  = max(8,  round(9  * r))

        F_BTN   = max(8,  round(9  * r))

        PAD     = round(14 * r)

        PAD2    = round(18 * r)

        PAD_V   = round(10 * r)

        # 제목

        tk.Label(popup, text="⚙ 설정",

                 font=("Segoe UI", F_TITLE, "bold"),

                 bg=self.BG, fg=self.TEXT).pack(pady=(PAD, 0))

        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(12*r), 0))

        # ── 임시 변수 ──────────────────────────────────────────────────────────

        tmp_startup   = tk.BooleanVar(value=self._startup_var.get())

        tmp_autostart = tk.BooleanVar(value=self._autostart_var.get())

        tmp_darkmode  = tk.BooleanVar(value=self._darkmode_var.get())

        tmp_scale     = tk.StringVar( value=self._scale_var.get())

        tmp_oped_auto = tk.BooleanVar(value=self._oped_auto_var.get())

        tmp_oped_sec  = tk.StringVar( value=self._oped_skip_sec_var.get())

        CHK = dict(font=("Consolas", F_MONO),

                   bg=self.BG2, selectcolor=self.BG3,

                   activebackground=self.BG2,

                   activeforeground=self.TEXT,

                   relief="flat", cursor="hand2")

        # ── 기본 설정 카드 ─────────────────────────────────────────────────────

        card = tk.Frame(popup, bg=self.BG2, padx=PAD2, pady=PAD)

        card.pack(fill="x", padx=PAD, pady=(PAD_V, 0))

        tk.Checkbutton(card,

                       text="Windows 시작 시 자동 실행",

                       variable=tmp_startup,

                       fg=self.TEXT, **CHK).pack(anchor="w", pady=round(4*r))

        tk.Checkbutton(card,

                       text="프로그램 실행 시 자동 시작",

                       variable=tmp_autostart,

                       fg=self.TEXT, **CHK).pack(anchor="w", pady=round(4*r))

        tk.Checkbutton(card,

                       text="다크 모드",

                       variable=tmp_darkmode,

                       fg=self.TEXT, **CHK).pack(anchor="w", pady=round(4*r))

        tk.Checkbutton(card,

                       text="OP/ED 자동 스킵",

                       variable=tmp_oped_auto,

                       fg=self.TEXT, **CHK).pack(anchor="w", pady=round(4*r))

        # 스킵 초 입력 행

        sec_row = tk.Frame(card, bg=self.BG2)

        sec_row.pack(anchor="w", pady=(round(6*r), 0))

        tk.Label(sec_row, text="스킵 초",

                 font=("Consolas", F_MONO),

                 bg=self.BG2, fg=self.TEXT_MID).pack(side="left", padx=(0, round(8*r)))

        vcmd = (popup.register(lambda s: s.isdigit() or s == ""), "%P")

        sec_entry = tk.Spinbox(sec_row,

                               from_=10, to=600,

                               textvariable=tmp_oped_sec,

                               width=5,

                               font=("Consolas", F_MONO),

                               bg=self.BG3, fg=self.TEXT,

                               buttonbackground=self.BG3,

                               relief="flat",

                               validate="key", validatecommand=vcmd)

        sec_entry.pack(side="left")

        tk.Label(sec_row, text="초  (10~600)",

                 font=("Consolas", max(7, F_MONO - 1)),

                 bg=self.BG2, fg=self.TEXT_DIM).pack(side="left", padx=(round(6*r), 0))

        tk.Frame(card, bg=self.BORDER, height=1).pack(fill="x", pady=(round(8*r), round(4*r)))

        size_row = tk.Frame(card, bg=self.BG2)

        size_row.pack(anchor="w")

        tk.Label(size_row, text="UI 크기", font=("Consolas", F_MONO),

                 bg=self.BG2, fg=self.TEXT_MID).pack(side="left", padx=(0, round(10*r)))

        for size in ["소", "중", "대"]:

            tk.Radiobutton(size_row, text=size,

                           variable=tmp_scale, value=size,

                           font=("Consolas", F_MONO),

                           bg=self.BG2, fg=self.TEXT,

                           selectcolor=self.BG3,

                           activebackground=self.BG2,

                           activeforeground=self.TEXT,

                           relief="flat", cursor="hand2").pack(side="left", padx=round(4*r))

        # ── 저장 버튼 ──────────────────────────────────────────────────────────

        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", padx=PAD, pady=(round(12*r), 0))

        def on_save():

            self._startup_var.set(tmp_startup.get())

            self._autostart_var.set(tmp_autostart.get())

            darkmode_changed = tmp_darkmode.get() != self._darkmode_var.get()

            scale_changed    = tmp_scale.get()    != self._scale_var.get()

            self._darkmode_var.set(tmp_darkmode.get())

            self._scale_var.set(tmp_scale.get())

            # OP/ED 설정 반영

            self._oped_auto_var.set(tmp_oped_auto.get())

            try:

                sec = max(10, min(600, int(tmp_oped_sec.get())))

            except ValueError:

                sec = 90

            self._oped_skip_sec_var.set(str(sec))

            self._toggle_startup()

            self._save_settings()

            popup.destroy()

            # OP/ED 버튼 상태 갱신

            self._update_oped_btn()

            if darkmode_changed:

                self._toggle_darkmode()

            if scale_changed:

                self._toggle_scale(tmp_scale.get())

        bf = tk.Frame(popup, bg=self.BG)

        bf.pack(pady=PAD_V)

        tk.Button(bf, text="💾 저장",

                  font=("Consolas", F_BTN, "bold"),

                  bg=self.BG3, fg=self.ACCENT,

                  activebackground=self.BORDER,

                  relief="flat", cursor="hand2",

                  padx=round(16*r), pady=round(6*r),

                  command=on_save).pack(side="left", padx=(0, round(8*r)))

        tk.Button(bf, text="닫기",

                  font=("Consolas", F_BTN, "bold"),

                  bg=self.BG3, fg=self.TEXT,

                  activebackground=self.BORDER,

                  relief="flat", cursor="hand2",

                  padx=round(16*r), pady=round(6*r),

                  command=popup.destroy).pack(side="left")
