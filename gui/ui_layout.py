"""gui/ui_layout.py -- GUI 창/윈도우·탭·위젯 레이아웃 구성 메서드

포함 메서드:
  _tray_show, _tray_quit     — 트레이 아이콘 콜백
  _build_window              — 루트 윈도우 초기화
  _build_ui                  — 메인 UI 레이아웃 전체 구성 (헤더·탭바·카드·버튼)
  _build_history_tab         — 시청 기록 탭 위젯 구성
"""
import os
import tkinter as tk
import tkinter.filedialog as fd
from app_icon import apply_to_root_window
from win32_utils import find_potplayer_hwnd, get_playback_info, do_oped_skip, pip_send


class LipSyncGUILayout:

    def _tray_show(self, icon=None, item=None):
        self.root.after(0, self.root.deiconify)

    def _tray_quit(self, icon=None, item=None):
        try:
            self.root.after(120, self._on_close)
        except Exception:
            try: self._on_close()
            except Exception: pass

    def _build_window(self):
        r = self.root
        r.withdraw()
        r.title("Auto Sync")
        r.geometry(f"{self.W}x{self.H}")
        r.resizable(False, False)
        r.configure(bg=self.BG)
        try: apply_to_root_window(r)
        except Exception: pass
        r.update_idletasks()
        x, y = self._load_pos()
        r.geometry(f"{self.W}x{self.H}+{x}+{y}")
        r.deiconify()

    def _build_ui(self):
        MONO   = ("Consolas", self.F_MONO)
        MONO_S = ("Consolas", self.F_MONO_S)
        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        P  = max(10, round(18 * r))
        P2 = max(8,  round(14 * r))
        self._theme_widgets = []
        def reg(w, bg=None, fg=None, abg=None, afg=None, obg=None):
            self._theme_widgets.append((w, bg, fg, abg, afg, obg))
            return w

        # ── 헤더 ──────────────────────────────────────────────────────────────
        hdr = reg(tk.Frame(self.root, bg=self.BG, pady=0), bg="BG")
        hdr.pack(fill="x", padx=P, ipady=P2)
        ic = round(32 * r)
        self._icon_canvas = tk.Canvas(hdr, width=ic, height=ic, bg=self.BG, highlightthickness=0)
        self._icon_canvas.pack(side="left", anchor="center")
        self._icon_canvas.create_oval(1, 1, ic-1, ic-1, fill=self.BG3, outline=self.ACCENT, width=2)
        self._icon_canvas.create_polygon(round(12*r), round(8*r), round(12*r), round(24*r), round(26*r), round(16*r), fill=self.ACCENT, outline="")
        tf = reg(tk.Frame(hdr, bg=self.BG), bg="BG")
        tf.pack(side="left", padx=10, anchor="center")
        reg(tk.Label(tf, text="Auto Sync", font=("Segoe UI", self.F_TITLE, "bold"), bg=self.BG, fg=self.TEXT), bg="BG", fg="TEXT").pack(anchor="w")
        reg(tk.Label(tf, text="PotPlayer 자동 싱크 보정 | 멀티코어", font=("Segoe UI", max(7, self.F_TITLE-5)), bg=self.BG, fg=self.TEXT_MID), bg="BG", fg="TEXT_MID").pack(anchor="w")
        rf = reg(tk.Frame(hdr, bg=self.BG), bg="BG")
        rf.pack(side="right", anchor="center")
        reg(tk.Label(rf, text="v2.0", font=("Consolas", 7), bg=self.ACCENT, fg="#0e0e0e", padx=5, pady=2), bg="ACCENT").pack(anchor="e")
        gfg = self.ACCENT if self._darkmode_var.get() else self.TEXT
        self._gear_btn = reg(tk.Button(rf, text="⚙", font=("Segoe UI", self.F_GEAR), bg=self.BG, fg=gfg, activebackground=self.BG2, activeforeground=gfg, relief="flat", cursor="hand2", bd=0, padx=2, pady=2, command=self._toggle_gear_menu), bg="BG", fg="GEAR_FG", abg="BG2", afg="GEAR_FG")
        self._gear_btn.pack(anchor="e", pady=(4, 0))
        reg(tk.Frame(self.root, bg=self.BORDER, height=1), bg="BORDER").pack(fill="x")

        # ── 탭 바 ──────────────────────────────────────────────────────────────
        TAB_F = max(8, round(9 * r))
        tab_bar = reg(tk.Frame(self.root, bg=self.BG2), bg="BG2")
        tab_bar.pack(fill="x")
        self._tab_var = getattr(self, "_tab_var", tk.StringVar(value="sync"))
        self._tab_frames = {}
        self._tab_btn_sync    = None
        self._tab_btn_history = None

        tab_inner = tk.Frame(tab_bar, bg=self.BG2)
        tab_inner.pack(side="left", padx=P2, pady=(round(6*r), 0))

        # PIP 버튼 — 탭바 오른쪽에 배치
        self._pip_on = bool(self._load_setting("pip_on", False))
        pip_text   = "⧉ PIP ON" if self._pip_on else "⧉ PIP OFF"
        pip_fg     = self.ACCENT3 if self._pip_on else self.TEXT_MID
        pip_bg     = "#0e0e0e"
        self._pip_btn = reg(
            tk.Button(tab_bar, text=pip_text,
                      font=("Consolas", max(7, round(8*r)), "bold"),
                      bg=pip_bg, fg=pip_fg,
                      activebackground=self.BG3, activeforeground=self.TEXT,
                      relief="solid", cursor="hand2",
                      padx=round(7*r), pady=round(3*r),
                      bd=1, highlightthickness=0,
                      command=self._pip_toggle),
            bg="BG2", fg="TEXT_MID", abg="BG3")

        def _switch_tab(name):
            self._tab_var.set(name)
            for n, frame in self._tab_frames.items():
                if n == name:
                    frame.pack(fill="both", expand=True)
                else:
                    frame.pack_forget()
            _update_tab_styles()

        def _update_tab_styles():
            cur = self._tab_var.get()
            for name, btn in [("sync", self._tab_btn_sync), ("history", self._tab_btn_history)]:
                if btn is None: continue
                if name == cur:
                    btn.config(bg=self.BG, fg=self.ACCENT, font=("Consolas", TAB_F, "bold"))
                else:
                    btn.config(bg=self.BG2, fg=self.TEXT_MID, font=("Consolas", TAB_F))

        self._pip_btn.pack(side="right", padx=(0, P2), pady=(round(4*r), 0))

        self._tab_btn_sync = tk.Button(
            tab_inner, text="싱크 보정",
            font=("Consolas", TAB_F, "bold"),
            bg=self.BG, fg=self.ACCENT,
            activebackground=self.BG3, activeforeground=self.ACCENT,
            relief="flat", cursor="hand2", padx=round(10*r), pady=round(4*r),
            bd=0, command=lambda: _switch_tab("sync"))
        self._tab_btn_sync.pack(side="left")

        self._tab_btn_history = tk.Button(
            tab_inner, text="시청 기록",
            font=("Consolas", TAB_F),
            bg=self.BG2, fg=self.TEXT_MID,
            activebackground=self.BG3, activeforeground=self.TEXT,
            relief="flat", cursor="hand2", padx=round(10*r), pady=round(4*r),
            bd=0, command=lambda: _switch_tab("history"))
        self._tab_btn_history.pack(side="left")

        self._switch_tab_fn        = _switch_tab
        self._update_tab_styles_fn = _update_tab_styles

        reg(tk.Frame(self.root, bg=self.BORDER, height=1), bg="BORDER").pack(fill="x")

        # ── 탭 컨테이너 ────────────────────────────────────────────────────────
        container = reg(tk.Frame(self.root, bg=self.BG), bg="BG")
        container.pack(fill="both", expand=True)

        # ════════════════════════════════════════════════════════
        # 탭1: 싱크 보정
        # ════════════════════════════════════════════════════════
        sync_frame = reg(tk.Frame(container, bg=self.BG), bg="BG")
        self._tab_frames["sync"] = sync_frame

        card = reg(tk.Frame(sync_frame, bg=self.BG2, pady=12, padx=16), bg="BG2")
        card.pack(fill="x", padx=P2, pady=(round(12*r), 0))
        def status_row(parent, label):
            row = reg(tk.Frame(parent, bg=self.BG2), bg="BG2")
            row.pack(fill="x", pady=2)
            reg(tk.Label(row, text=label, font=MONO, bg=self.BG2, fg=self.TEXT_MID, width=11, anchor="w"), bg="BG2", fg="TEXT_MID").pack(side="left", anchor="center")
            dot = reg(tk.Label(row, text="●", font=("Consolas", 8), bg=self.BG2, fg=self.TEXT_DIM), bg="BG2", fg="TEXT_DIM")
            dot.pack(side="left", anchor="center")
            lbl = reg(tk.Label(row, text="—", font=MONO, bg=self.BG2, fg=self.TEXT_MID), bg="BG2", fg="TEXT_MID")
            lbl.pack(side="left", padx=4, anchor="center")
            return dot, lbl
        self._pot_dot, self._pot_lbl = status_row(card, "팟플레이어")
        self._aud_dot, self._aud_lbl = status_row(card, "오디오 장치")
        pr = reg(tk.Frame(card, bg=self.BG2), bg="BG2")
        pr.pack(fill="x", pady=(6, 0))
        reg(tk.Label(pr, text="프로세스", font=MONO, bg=self.BG2, fg=self.TEXT_MID, width=11, anchor="w"), bg="BG2", fg="TEXT_MID").pack(side="left", anchor="center")
        self._proc_dot = reg(tk.Label(pr, text="●", font=("Consolas", 8), bg=self.BG2, fg=self.TEXT_DIM), bg="BG2", fg="TEXT_DIM")
        self._proc_dot.pack(side="left", anchor="center")
        self._proc_lbl = reg(tk.Label(pr, text="대기 중", font=MONO, bg=self.BG2, fg=self.TEXT_MID), bg="BG2", fg="TEXT_MID")
        self._proc_lbl.pack(side="left", padx=4, anchor="center")
        dr = reg(tk.Frame(card, bg=self.BG2), bg="BG2")
        dr.pack(fill="x", pady=(4, 0))
        reg(tk.Label(dr, text="재생 위치", font=MONO, bg=self.BG2, fg=self.TEXT_MID, width=11, anchor="w"), bg="BG2", fg="TEXT_MID").pack(side="left", anchor="center")
        self._dur_lbl = reg(tk.Label(dr, text="— / —", font=MONO, bg=self.BG2, fg=self.TEXT_MID), bg="BG2", fg="TEXT_MID")
        self._dur_lbl.pack(side="left", padx=4, anchor="center")
        or_ = reg(tk.Frame(sync_frame, bg=self.BG, padx=P), bg="BG")
        or_.pack(fill="x", pady=(round(12*r), 0))
        self._oped_btn = reg(tk.Button(or_, font=("Consolas", max(8, round(9*r)), "bold"), relief="flat", cursor="hand2", padx=round(8*r), pady=0, command=self._oped_skip), bg="BG3", fg="ACCENT3", abg="BORDER")
        self._oped_btn.pack(fill="x")
        self._update_oped_btn()
        reg(tk.Frame(sync_frame, bg=self.BORDER, height=1), bg="BORDER").pack(fill="x", padx=P2, pady=(round(8*r), 0))
        mf = reg(tk.Frame(sync_frame, bg=self.BG, pady=round(6*r), padx=P), bg="BG")
        mf.pack(fill="x")
        tp = reg(tk.Frame(mf, bg=self.BG), bg="BG")
        tp.pack(fill="x")
        reg(tk.Label(tp, text="OFFSET", font=("Consolas", 7, "bold"), bg=self.BG, fg=self.TEXT_DIM), bg="BG", fg="TEXT_DIM").pack(side="left")
        self._badge = reg(tk.Label(tp, text=" 대기 중 ", font=("Consolas", max(7, round(8*r)), "bold"), bg=self.BG3, fg=self.TEXT, padx=round(6*r), pady=2), bg="BG3", fg="TEXT")
        self._badge.pack(side="right")
        offr = reg(tk.Frame(mf, bg=self.BG), bg="BG")
        offr.pack(fill="x", pady=(2, 0))
        self._offset_lbl = reg(tk.Label(offr, text="— ms", font=("Consolas", self.F_OFFSET, "bold"), bg=self.BG, fg=self.ACCENT), bg="BG", fg="ACCENT")
        self._offset_lbl.pack(side="left", anchor="w")
        bar_bg = reg(tk.Frame(mf, bg=self.BG3, height=4), bg="BG3")
        bar_bg.pack(fill="x", pady=(4, 0))
        bar_bg.pack_propagate(False)
        self._bar = tk.Frame(bar_bg, bg=self.ACCENT, height=4)
        self._bar.place(x=0, y=0, width=0, height=4)
        self._bar_ref = bar_bg
        r1 = reg(tk.Frame(mf, bg=self.BG), bg="BG")
        r1.pack(fill="x", pady=(6, 0))
        reg(tk.Label(r1, text="이미지 샘플", font=MONO_S, bg=self.BG, fg=self.TEXT_MID), bg="BG", fg="TEXT_MID").pack(side="left")
        self._lip_cnt = reg(tk.Label(r1, text="0", font=MONO_S, bg=self.BG, fg=self.TEXT), bg="BG", fg="TEXT")
        self._lip_cnt.pack(side="left", padx=(4, 16))
        reg(tk.Label(r1, text="오디오 샘플", font=MONO_S, bg=self.BG, fg=self.TEXT_MID), bg="BG", fg="TEXT_MID").pack(side="left")
        self._aud_cnt = reg(tk.Label(r1, text="0", font=MONO_S, bg=self.BG, fg=self.TEXT), bg="BG", fg="TEXT")
        self._aud_cnt.pack(side="left", padx=(4, 0))
        r2 = reg(tk.Frame(mf, bg=self.BG), bg="BG")
        r2.pack(fill="x", pady=(3, 0))
        reg(tk.Label(r2, text="누적 보정", font=MONO_S, bg=self.BG, fg=self.TEXT_MID), bg="BG", fg="TEXT_MID").pack(side="left")
        self._corr_lbl = reg(tk.Label(r2, text="+0 ms", font=MONO_S, bg=self.BG, fg=self.TEXT), bg="BG", fg="TEXT")
        self._corr_lbl.pack(side="left", padx=(4, 0))

        # ── 하단 버튼 (container보다 먼저 pack해야 expand=True가 남은 공간만 차지함) ──
        reg(tk.Frame(self.root, bg=self.BORDER, height=1), bg="BORDER").pack(fill="x", padx=P2, side="bottom")
        bf = reg(tk.Frame(self.root, bg=self.BG, padx=round(10*r), pady=round(6*r)), bg="BG")
        bf.pack(fill="x", side="bottom")
        bf.columnconfigure(2, weight=1)
        bf.rowconfigure(0, minsize=round(32*r))
        BTN = dict(font=("Consolas", max(8, round(9*r)), "bold"), relief="flat", cursor="hand2", padx=round(8*r), pady=0, anchor="center")
        self._start_btn = reg(tk.Button(bf, text="▶ 시작", bg=self.BG3, fg=self.ACCENT, activebackground=self.BORDER, command=self._toggle, **BTN), bg="BG3", fg="ACCENT", abg="BORDER")
        self._start_btn.grid(row=0, column=0, padx=(0, 2), sticky="nsew")
        self._reset_btn = reg(tk.Button(bf, text="↺ 초기화", bg=self.BG3, fg=self.TEXT_MID, activebackground=self.BORDER, command=self._reset, **BTN), bg="BG3", fg="TEXT_MID", abg="BORDER")
        self._reset_btn.grid(row=0, column=1, padx=2, sticky="nsew")
        reg(tk.Frame(bf, bg=self.BG), bg="BG").grid(row=0, column=2, sticky="nsew")
        self._close_btn = reg(tk.Button(bf, text="✕ 종료", bg=self.BG3, fg=self.ACCENT2, activebackground=self.BORDER, command=self._on_close, **BTN), bg="BG3", fg="ACCENT2", abg="BORDER")
        self._close_btn.grid(row=0, column=3, padx=(2, 0), sticky="nsew")

        # ════════════════════════════════════════════════════════
        # 탭2: 시청 기록
        # ════════════════════════════════════════════════════════
        hist_frame = reg(tk.Frame(container, bg=self.BG), bg="BG")
        self._tab_frames["history"] = hist_frame
        self._build_history_tab(hist_frame, r, P, P2, MONO, MONO_S)

        # 싱크 탭 먼저 표시
        _switch_tab("sync")
        self.root.after(1000, self._poll_playback_info)
        self.root.after(500,  self._start_title_watcher)

    # ── 시청 기록 탭 구성 ─────────────────────────────────────────────────────
    def _build_history_tab(self, parent, r, P, P2, MONO, MONO_S):
        reg = lambda w, bg=None, fg=None, abg=None, afg=None, obg=None: (
            self._theme_widgets.append((w, bg, fg, abg, afg, obg)), w)[1]

        BTN_S = dict(font=("Consolas", max(7, round(8*r)), "bold"),
                     relief="flat", cursor="hand2",
                     padx=round(6*r), pady=round(3*r))

        # 상단 폴더 지정 카드
        top = tk.Frame(parent, bg=self.BG2, padx=round(10*r), pady=round(8*r))
        top.pack(fill="x", padx=P2, pady=(round(10*r), 0))
        reg(top, bg="BG2")

        reg(tk.Label(top, text="동영상 폴더",
                     font=("Consolas", self.F_MONO_S, "bold"),
                     bg=self.BG2, fg=self.TEXT_MID),
            bg="BG2", fg="TEXT_MID").pack(anchor="w")

        dir_row = tk.Frame(top, bg=self.BG2)
        dir_row.pack(fill="x", pady=(round(4*r), 0))
        reg(dir_row, bg="BG2")

        self._hist_dir_lbl = reg(
            tk.Label(dir_row, text="(지정 안 됨)",
                     font=("Consolas", self.F_MONO_S),
                     bg=self.BG3, fg=self.TEXT_DIM,
                     anchor="w", padx=6, width=1),
            bg="BG3", fg="TEXT_DIM")
        self._hist_dir_lbl.pack(side="left", fill="x", expand=True)

        self._hist_browse_btn = reg(
            tk.Button(dir_row, text="📂",
                      bg=self.BG3, fg=self.TEXT,
                      activebackground=self.BORDER,
                      command=self._hist_browse_dir, **BTN_S),
            bg="BG3", fg="TEXT", abg="BORDER")
        self._hist_browse_btn.pack(side="left", padx=(round(4*r), 0))

        self._hist_open_btn = reg(
            tk.Button(dir_row, text="🗁 열기",
                      bg=self.BG3, fg=self.TEXT_MID,
                      activebackground=self.BORDER,
                      state="disabled",
                      command=self._hist_open_dir, **BTN_S),
            bg="BG3", fg="TEXT_MID", abg="BORDER")
        self._hist_open_btn.pack(side="left", padx=(round(4*r), 0))

        # 구분선
        reg(tk.Frame(parent, bg=self.BORDER, height=1),
            bg="BORDER").pack(fill="x", padx=P2, pady=(round(8*r), 0))

        # 기록 목록 헤더
        hdr_row = tk.Frame(parent, bg=self.BG, padx=P2)
        hdr_row.pack(fill="x", pady=(round(6*r), 0))
        reg(hdr_row, bg="BG")
        reg(tk.Label(hdr_row, text="시청 기록",
                     font=("Consolas", self.F_MONO_S, "bold"),
                     bg=self.BG, fg=self.TEXT_DIM),
            bg="BG", fg="TEXT_DIM").pack(side="left")

        # 스크롤 영역
        list_outer = tk.Frame(parent, bg=self.BG)
        list_outer.pack(fill="both", expand=True, padx=P2, pady=(round(4*r), 0))
        reg(list_outer, bg="BG")

        sb = tk.Scrollbar(list_outer, bg=self.BG3, troughcolor=self.BG2,
                          relief="flat", width=8)
        sb.pack(side="right", fill="y")

        canvas = tk.Canvas(list_outer, bg=self.BG, highlightthickness=0,
                           yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.config(command=canvas.yview)

        self._hist_list_canvas = canvas
        self._hist_list_frame  = tk.Frame(canvas, bg=self.BG)
        self._hist_canvas_window = canvas.create_window(
            (0, 0), window=self._hist_list_frame, anchor="nw")

        def _on_frame_cfg(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
        def _on_canvas_cfg(e):
            canvas.itemconfig(self._hist_canvas_window, width=e.width)

        self._hist_list_frame.bind("<Configure>", _on_frame_cfg)
        canvas.bind("<Configure>", _on_canvas_cfg)
        canvas.bind("<MouseWheel>",
                    lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        # 저장된 폴더 복원
        saved_dir = self._load_setting("history_video_dir", "")
        if saved_dir and os.path.isdir(saved_dir):
            self._hist_video_dir = saved_dir
            self._hist_dir_lbl.config(text=saved_dir, fg=self.TEXT)
            self._hist_open_btn.config(state="normal")
        else:
            self._hist_video_dir = ""

        self._refresh_history_list()
