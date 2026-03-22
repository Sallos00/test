"""
gui_ui.py -- GUI 창/UI 구성, 팝업(설정/로그/메뉴) 메서드
"""
import tkinter as tk

from win32_utils import find_potplayer_hwnd


class LipSyncGUIUI:
    def _tray_show(self, icon=None, item=None):
        """트레이에서 창 다시 열기."""
        self.root.after(0, self.root.deiconify)

    def _tray_quit(self, icon=None, item=None):
        """트레이에서 완전 종료."""
        self._closing = True
        # 예약된 팝업 콜백 취소
        if hasattr(self, "_popup_after_id"):
            try: self.root.after_cancel(self._popup_after_id)
            except Exception: pass
        self._popup_open = False
        self._save_pos()
        self._stop_processes()
        if self._tray:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None
        try: self.root.after(0, self.root.destroy)
        except Exception: pass

    # ── 창 설정 ───────────────────────────────────────────────────────────────
    def _build_window(self):
        r = self.root
        r.withdraw()           # 위치 확정 전까지 숨김
        r.title("Auto Sync")
        r.geometry(f"{self.W}x{self.H}")
        r.resizable(False, False)
        r.configure(bg=self.BG)
        # 상단바 아이콘 설정
        try:
            from PIL import Image, ImageDraw, ImageTk
            img  = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse([1, 1, 30, 30], fill="#1e1e1e",
                         outline="#00c8e0", width=2)
            draw.polygon([(11, 7), (11, 25), (25, 16)], fill="#00c8e0")
            self._icon_img = ImageTk.PhotoImage(img)
            r.iconphoto(True, self._icon_img)
        except Exception:
            pass
        r.update_idletasks()
        x, y = self._load_pos()
        r.geometry(f"{self.W}x{self.H}+{x}+{y}")
        r.deiconify()          # 위치 확정 후 표시
        # protocol은 _setup_tray에서 설정

    # ── UI 구성 ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        MONO   = ("Consolas", self.F_MONO)
        MONO_S = ("Consolas", self.F_MONO_S)
        r = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        PAD  = max(10, round(18 * r))   # 기본 여백
        PAD2 = max(8,  round(14 * r))   # 카드 여백

        # 색상 업데이트 대상 위젯 목록
        self._theme_widgets = []  # (widget, attr) 튜플 리스트

        def reg(w, bg=None, fg=None, abg=None, afg=None, obg=None):
            """위젯을 테마 업데이트 목록에 등록."""
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
        # 삼각형 좌표 비율 스케일
        tx1 = round(12 * r); ty1 = round(8  * r)
        tx2 = round(12 * r); ty2 = round(24 * r)
        tx3 = round(26 * r); ty3 = round(16 * r)
        self._icon_canvas.create_polygon(tx1, ty1, tx2, ty2, tx3, ty3,
            fill=self.ACCENT, outline="")

        tf = reg(tk.Frame(hdr, bg=self.BG), bg="BG")
        tf.pack(side="left", padx=10, anchor="center")
        reg(tk.Label(tf, text="Auto Sync", font=("Segoe UI", self.F_TITLE, "bold"),
                 bg=self.BG, fg=self.TEXT), bg="BG", fg="TEXT").pack(anchor="w")
        reg(tk.Label(tf, text="PotPlayer 자동 싱크 보정  |  멀티코어",
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

        reg(tk.Frame(self.root, bg=self.BORDER, height=1),
            bg="BORDER").pack(fill="x", padx=PAD2, pady=(round(12*r),0))

        # 오프셋 미터 (expand=True로 창 크기에 따라 늘어남)
        mf = reg(tk.Frame(self.root, bg=self.BG, pady=round(10*r), padx=PAD), bg="BG")
        mf.pack(fill="both", expand=True)
        top = reg(tk.Frame(mf, bg=self.BG), bg="BG")
        top.pack(fill="x")
        reg(tk.Label(top, text="OFFSET", font=("Consolas", 7, "bold"),
                 bg=self.BG, fg=self.TEXT_DIM), bg="BG", fg="TEXT_DIM").pack(side="left")
        self._badge = reg(tk.Label(top, text="  대기 중  ",
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

        # 버튼
        bf = reg(tk.Frame(self.root, bg=self.BG, padx=round(10*r), pady=round(8*r)), bg="BG")
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

    # ── 톱니바퀴 드롭다운 메뉴 ───────────────────────────────────────────────
    def _toggle_gear_menu(self):
        if self._gear_menu_open:
            self._close_gear_menu()
        else:
            self._open_gear_menu()

    def _open_gear_menu(self):
        self._gear_menu_open = True
        self.root.update_idletasks()

        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
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

        tk.Button(frame, text="⚙  설정",
                  command=lambda: pick(self._open_settings), **ITEM).pack(fill="x")
        tk.Frame(frame, bg=self.BORDER, height=1).pack(fill="x")
        tk.Button(frame, text="📋  로그 보기",
                  command=lambda: pick(self._open_log_popup), **ITEM).pack(fill="x")

        frame.update_idletasks()
        frame.place(x=bx + self._gear_btn.winfo_width() - mw, y=by)
        frame.lift()

        # 메인창 다른 곳 클릭 시 메뉴 닫기
        def on_root_click(e):
            try:
                fx1 = frame.winfo_rootx()
                fy1 = frame.winfo_rooty()
                fx2 = fx1 + frame.winfo_width()
                fy2 = fy1 + frame.winfo_height()
                # 톱니바퀴 버튼 영역
                gx1 = self._gear_btn.winfo_rootx()
                gy1 = self._gear_btn.winfo_rooty()
                gx2 = gx1 + self._gear_btn.winfo_width()
                gy2 = gy1 + self._gear_btn.winfo_height()
                # 메뉴 프레임 또는 톱니바퀴 버튼 안 클릭이면 무시
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
        """전체 로그를 스크롤 가능한 팝업으로 표시."""
        popup = tk.Toplevel(self.root)
        popup.title("로그")
        popup.resizable(False, True)
        popup.configure(bg=self.BG)
        popup.grab_set()

        s   = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])
        r   = s["scale"]
        pw  = round(320 * r)
        ph  = round(280 * r)
        self._place_popup(popup, pw, ph)

        F_TITLE = max(9,  round(11 * r))
        F_BTN   = max(8,  round(9  * r))
        PAD     = round(10 * r)
        PAD2    = round(14 * r)

        # 제목 (상단 고정)
        tk.Label(popup, text="📋  로그",
                 font=("Segoe UI", F_TITLE, "bold"),
                 bg=self.BG, fg=self.TEXT).pack(pady=(PAD, 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(6*r), 0))

        # ★ 버튼 영역을 텍스트보다 먼저 pack → 스크롤이 길어져도 버튼이 항상 보임
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", padx=PAD2, pady=(round(8*r), 0), side="bottom")
        bf = tk.Frame(popup, bg=self.BG)
        bf.pack(side="bottom", pady=PAD)
        tk.Button(bf, text="🗑  로그 지우기",
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

        # 스크롤 텍스트 영역 (버튼 위, 나머지 공간 채움)
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
                      wrap="word", state="disabled",
                      yscrollcommand=scrollbar.set)
        txt.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=txt.yview)

        # 색상 태그 정의
        txt.tag_config("ok",   foreground=self.ACCENT3)   # 초록  — 정상/시작/초기화
        txt.tag_config("info", foreground=self.ACCENT)    # 청록  — 감지/정보
        txt.tag_config("warn", foreground="#e0a03c")      # 노랑  — 보정/경고
        txt.tag_config("err",  foreground=self.ACCENT2)   # 빨강  — 오류/취소
        txt.tag_config("dim",  foreground=self.TEXT_DIM)  # 회색  — 진단/기타

        # 현재 로그 내용 채우기
        self._log_popup_txt = txt
        self._update_log_popup()

        # 1초마다 자동 갱신
        def auto_refresh():
            if popup.winfo_exists():
                self._update_log_popup()
                popup.after(1000, auto_refresh)
        popup.after(1000, auto_refresh)

    def _update_log_popup(self):
        """로그 팝업 텍스트 갱신 (줄별 색상 적용)."""
        try:
            txt = self._log_popup_txt
            txt.config(state="normal")
            txt.delete("1.0", "end")
            lines = list(self._log_lines) if hasattr(self, "_log_lines") else []
            if not lines:
                txt.insert("end", "— 로그 없음 —", "dim")
            else:
                for i, line in enumerate(lines):
                    if i > 0:
                        txt.insert("end", "\n")
                    # 이모지/키워드 기준으로 태그 결정
                    if any(k in line for k in ("▶", "↺", "🔄", "정상", "OK")):
                        tag = "ok"
                    elif any(k in line for k in ("🎬", "👁", "🔊", "📊", "감지")):
                        tag = "info"
                    elif any(k in line for k in ("보정", "⚠", "상한")):
                        tag = "warn"
                    elif any(k in line for k in ("❌", "오류", "실패", "취소")):
                        tag = "err"
                    else:
                        tag = "dim"
                    txt.insert("end", line, tag)
            txt.see("end")
            txt.config(state="disabled")
        except Exception:
            pass

    def _clear_log(self):
        """로그 초기화."""
        if hasattr(self, "_log_lines"):
            self._log_lines.clear()
        self._update_log_popup()
    def _open_settings(self):
        """설정 팝업창 — 메인창 정중앙에 표시."""
        popup = tk.Toplevel(self.root)
        popup.title("설정")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        popup.grab_set()

        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        pw = round(300 * r)
        ph = round(300 * r)
        self._place_popup(popup, pw, ph)

        F_TITLE = max(9,  round(11 * r))
        F_MONO  = max(8,  round(9  * r))
        F_BTN   = max(8,  round(9  * r))
        PAD     = round(14 * r)
        PAD2    = round(18 * r)
        PAD_V   = round(10 * r)

        # 제목
        tk.Label(popup, text="⚙  설정",
                 font=("Segoe UI", F_TITLE, "bold"),
                 bg=self.BG, fg=self.TEXT).pack(pady=(PAD, 0))

        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(12*r), 0))

        # 임시 변수 (저장 버튼 누를 때만 실제 적용)
        tmp_startup   = tk.BooleanVar(value=self._startup_var.get())
        tmp_autostart = tk.BooleanVar(value=self._autostart_var.get())
        tmp_darkmode  = tk.BooleanVar(value=self._darkmode_var.get())
        tmp_scale     = tk.StringVar(value=self._scale_var.get())

        # 설정 항목 카드
        card = tk.Frame(popup, bg=self.BG2, padx=PAD2, pady=PAD)
        card.pack(fill="x", padx=PAD, pady=(PAD_V, 0))

        CHK = dict(font=("Consolas", F_MONO),
                   bg=self.BG2, selectcolor=self.BG3,
                   activebackground=self.BG2,
                   activeforeground=self.TEXT,
                   relief="flat", cursor="hand2")

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

        # 크기 선택
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

        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", padx=PAD, pady=(round(12*r), 0))

        def on_save():
            # 실제 변수에 반영
            self._startup_var.set(tmp_startup.get())
            self._autostart_var.set(tmp_autostart.get())
            darkmode_changed = tmp_darkmode.get() != self._darkmode_var.get()
            scale_changed    = tmp_scale.get()    != self._scale_var.get()
            self._darkmode_var.set(tmp_darkmode.get())
            self._scale_var.set(tmp_scale.get())
            # 시작프로그램 등록/해제
            self._toggle_startup()
            self._save_settings()
            popup.destroy()
            # 다크모드 변경 적용
            if darkmode_changed:
                self._toggle_darkmode()
            # 크기 변경 적용
            if scale_changed:
                self._toggle_scale(tmp_scale.get())

        # 버튼
        bf = tk.Frame(popup, bg=self.BG)
        bf.pack(pady=PAD_V)
        tk.Button(bf, text="💾  저장",
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
