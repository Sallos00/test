"""gui/ui.py -- GUI 창/UI 구성, 팝업(설정/로그/메뉴) 메서드"""
import tkinter as tk
from app_icon import apply_to_root_window
from win32_utils import find_potplayer_hwnd, get_playback_info, do_oped_skip, pip_send

class LipSyncGUIUI:

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
        card = reg(tk.Frame(self.root, bg=self.BG2, pady=12, padx=16), bg="BG2")
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
        or_ = reg(tk.Frame(self.root, bg=self.BG, padx=P), bg="BG")
        or_.pack(fill="x", pady=(round(12*r), 0))
        self._oped_btn = reg(tk.Button(or_, font=("Consolas", max(8, round(9*r)), "bold"), relief="flat", cursor="hand2", padx=round(8*r), pady=0, command=self._oped_skip), bg="BG3", fg="ACCENT3", abg="BORDER")
        self._oped_btn.pack(fill="x")
        self._update_oped_btn()
        reg(tk.Frame(self.root, bg=self.BORDER, height=1), bg="BORDER").pack(fill="x", padx=P2, pady=(round(8*r), 0))
        mf = reg(tk.Frame(self.root, bg=self.BG, pady=round(10*r), padx=P), bg="BG")
        mf.pack(fill="both", expand=True)
        tp = reg(tk.Frame(mf, bg=self.BG), bg="BG")
        tp.pack(fill="x")
        reg(tk.Label(tp, text="OFFSET", font=("Consolas", 7, "bold"), bg=self.BG, fg=self.TEXT_DIM), bg="BG", fg="TEXT_DIM").pack(side="left")
        self._badge = reg(tk.Label(tp, text=" 대기 중 ", font=("Consolas", max(7, round(8*r)), "bold"), bg=self.BG3, fg=self.TEXT, padx=round(6*r), pady=2), bg="BG3", fg="TEXT")
        self._badge.pack(side="right")
        self._pip_on = bool(self._load_setting("pip_on", False))
        offr = reg(tk.Frame(mf, bg=self.BG), bg="BG")
        offr.pack(fill="x", pady=(2, 0))
        self._offset_lbl = reg(tk.Label(offr, text="— ms", font=("Consolas", self.F_OFFSET, "bold"), bg=self.BG, fg=self.ACCENT), bg="BG", fg="ACCENT")
        self._offset_lbl.pack(side="left", anchor="w")
        self._pip_btn = reg(tk.Button(offr, text="⧉ PIP OFF", font=("Consolas", max(7, round(8*r)), "bold"), bg=self.BG3, fg=self.TEXT_MID, activebackground=self.BORDER, relief="flat", cursor="hand2", padx=round(6*r), pady=2, command=self._pip_toggle), bg="BG3", fg="TEXT_MID", abg="BORDER")
        self._pip_btn.pack(side="right", anchor="s", padx=(4, 0))
        if self._pip_on:
            self._pip_btn.config(text="⧉ PIP ON", fg=self.ACCENT3)
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
        reg(tk.Frame(self.root, bg=self.BORDER, height=1), bg="BORDER").pack(fill="x", padx=P2)
        bf = reg(tk.Frame(self.root, bg=self.BG, padx=round(10*r), pady=round(6*r)), bg="BG")
        bf.pack(fill="x")
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
        self.root.after(1000, self._poll_playback_info)

    def _pip_toggle(self):
        hwnd = find_potplayer_hwnd()
        if not hwnd: return
        pip_send(hwnd)
        if self._pip_on:
            self._pip_on = False
            self._pip_btn.config(text="⧉ PIP OFF", fg=self.TEXT_MID)
        else:
            self._pip_on = True
            self._pip_btn.config(text="⧉ PIP ON", fg=self.ACCENT3)
        self._save_settings()

    def _update_oped_btn(self):
        if not hasattr(self, "_oped_btn"): return
        try: sec = int(self._oped_skip_sec_var.get())
        except (ValueError, AttributeError): sec = 90
        if self._oped_auto_var.get():
            self._oped_btn.config(text=f"⏭ 자동 스킵 ON  ({sec}초)", state="disabled", bg=self.BG3, fg=self.TEXT_DIM, activebackground=self.BORDER)
        else:
            self._oped_btn.config(text=f"⏭ OP/ED 스킵  ({sec}초)", state="normal", bg=self.BG3, fg=self.ACCENT3, activebackground=self.BORDER)

    def _oped_skip(self):
        hwnd = find_potplayer_hwnd()
        if not hwnd: return
        pos_ms, dur_ms = get_playback_info(hwnd)
        if pos_ms is None: return
        try: skip_sec = max(10, min(600, int(self._oped_skip_sec_var.get())))
        except (ValueError, AttributeError): skip_sec = 90
        new_pos, ok = do_oped_skip(hwnd, pos_ms, dur_ms, skip_sec)
        if ok:
            def fmt(ms):
                s = ms // 1000
                return f"{s//60}:{s%60:02d}"
            if hasattr(self, "_log_lines"):
                import time as _t
                self._log_lines.append(f"[{_t.strftime('%H:%M:%S')}] ⏭ 수동 스킵: {fmt(pos_ms)} → {fmt(new_pos)}  (전체 {fmt(dur_ms)})")

    def _poll_playback_info(self):
        try:
            hwnd = find_potplayer_hwnd()
            if hwnd:
                pos_ms, dur_ms = get_playback_info(hwnd)
                if pos_ms is not None:
                    def fmt(ms):
                        s = ms // 1000
                        return f"{s//60}:{s%60:02d}"
                    txt = f"{fmt(pos_ms)} / {fmt(dur_ms)}" if dur_ms is not None else f"{fmt(pos_ms)} / —"
                    self._dur_lbl.config(text=txt, fg=self.ACCENT3)
                else:
                    self._dur_lbl.config(text="— / —", fg=self.TEXT_MID)
            else:
                self._dur_lbl.config(text="— / —", fg=self.TEXT_MID)
        except Exception: pass
        if not self._closing:
            self.root.after(1000, self._poll_playback_info)

    def _toggle_gear_menu(self):
        if self._gear_menu_open: self._close_gear_menu()
        else: self._open_gear_menu()

    def _open_gear_menu(self):
        self._gear_menu_open = True
        self.root.update_idletasks()
        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        rx = self.root.winfo_rootx(); ry = self.root.winfo_rooty()
        bx = self._gear_btn.winfo_rootx() - rx
        by = self._gear_btn.winfo_rooty() - ry + self._gear_btn.winfo_height() + 2
        mw = round(140 * r)
        frame = tk.Frame(self.root, bg=self.BORDER, bd=1, relief="solid")
        self._gear_menu_frame = frame
        ITEM = dict(font=("Consolas", max(8, round(9*r))), bg=self.BG2, fg=self.TEXT, relief="flat", cursor="hand2", activebackground=self.BG3, activeforeground=self.TEXT, anchor="w", padx=round(14*r), pady=round(7*r))
        def pick(fn):
            self._close_gear_menu(); fn()
        tk.Button(frame, text="⚙ 설정",        command=lambda: pick(self._open_settings),       **ITEM).pack(fill="x")
        tk.Frame(frame, bg=self.BORDER, height=1).pack(fill="x")
        tk.Button(frame, text="🎬 녹화 및 캡처", command=lambda: pick(self._open_record_capture), **ITEM).pack(fill="x")
        tk.Frame(frame, bg=self.BORDER, height=1).pack(fill="x")
        tk.Button(frame, text="📋 로그 보기",    command=lambda: pick(self._open_log_popup),      **ITEM).pack(fill="x")
        frame.update_idletasks()
        frame.place(x=bx + self._gear_btn.winfo_width() - mw, y=by)
        frame.lift()
        def on_root_click(e):
            try:
                fx1=frame.winfo_rootx(); fy1=frame.winfo_rooty()
                fx2=fx1+frame.winfo_width(); fy2=fy1+frame.winfo_height()
                gx1=self._gear_btn.winfo_rootx(); gy1=self._gear_btn.winfo_rooty()
                gx2=gx1+self._gear_btn.winfo_width(); gy2=gy1+self._gear_btn.winfo_height()
                if not (fx1<=e.x_root<=fx2 and fy1<=e.y_root<=fy2) and \
                   not (gx1<=e.x_root<=gx2 and gy1<=e.y_root<=gy2):
                    self._close_gear_menu()
            except Exception: self._close_gear_menu()
        self.root.bind("<Button-1>", on_root_click)

    def _close_gear_menu(self):
        self._gear_menu_open = False
        if hasattr(self, "_gear_menu_frame") and self._gear_menu_frame:
            try: self._gear_menu_frame.destroy()
            except Exception: pass
        self._gear_menu_frame = None
        try: self.root.unbind("<Button-1>")
        except Exception: pass
    def _open_log_popup(self):
        popup = tk.Toplevel(self.root)
        popup.title("로그")
        popup.resizable(False, True)
        popup.configure(bg=self.BG)
        popup.grab_set()
        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        pw = round(320*r); ph = round(280*r)
        self._place_popup(popup, pw, ph)
        FT = max(9, round(11*r)); FB = max(8, round(9*r)); P = round(10*r); P2 = round(14*r)
        tk.Label(popup, text="📋 로그", font=("Segoe UI", FT, "bold"), bg=self.BG, fg=self.TEXT).pack(pady=(P, 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(6*r), 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", padx=P2, pady=(round(8*r), 0), side="bottom")
        bf = tk.Frame(popup, bg=self.BG)
        bf.pack(side="bottom", pady=P)
        tk.Button(bf, text="🗑 로그 지우기", font=("Consolas", FB, "bold"), bg=self.BG3, fg=self.TEXT, activebackground=self.BORDER, relief="flat", cursor="hand2", padx=round(12*r), pady=round(5*r), command=self._clear_log).pack(side="left", padx=round(6*r))
        _cf = [None]
        tk.Button(bf, text="닫기", font=("Consolas", FB, "bold"), bg=self.BG3, fg=self.TEXT, activebackground=self.BORDER, relief="flat", cursor="hand2", padx=round(12*r), pady=round(5*r), command=lambda: _cf[0] and _cf[0]()).pack(side="left", padx=round(6*r))
        frame = tk.Frame(popup, bg=self.BG2, padx=2, pady=2)
        frame.pack(fill="both", expand=True, padx=P2, pady=(round(6*r), 0))
        sb = tk.Scrollbar(frame)
        sb.pack(side="right", fill="y")
        txt = tk.Text(frame, font=("Consolas", max(8, round(9*r))), bg=self.BG2, fg=self.TEXT, insertbackground=self.ACCENT, selectbackground=self.BG3, relief="flat", bd=0, wrap="word", state="normal", yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        for ev, fn in [("<Key>", lambda e: None if (e.keysym=="c" and e.state&0x4) else "break"), ("<<Paste>>", lambda e: "break"), ("<<Cut>>", lambda e: "break"), ("<Control-c>", lambda e: None), ("<Control-C>", lambda e: None)]:
            txt.bind(ev, fn)
        self._log_user_scrolled = False
        def _chk(): self._log_user_scrolled = txt.yview()[1] < 0.999
        def _on_scroll(*a): txt.yview(*a); _chk()
        sb.config(command=_on_scroll)
        txt.bind("<MouseWheel>", lambda e: (txt.yview_scroll(int(-1*(e.delta/120)), "units"), _chk()))
        txt.bind("<Button-4>",   lambda e: (txt.yview_scroll(-1, "units"), _chk()))
        txt.bind("<Button-5>",   lambda e: (txt.yview_scroll( 1, "units"), _chk()))
        for tag, fg in [("ok", self.ACCENT3), ("info", self.ACCENT), ("warn", "#e0a03c"), ("err", self.ACCENT2), ("skip", "#b58cff"), ("detect", "#4ec9f0"), ("sync", "#ffd166"), ("dim", self.TEXT_DIM)]:
            txt.tag_config(tag, foreground=fg)
        self._log_popup_txt = txt
        def _close():
            self._log_user_scrolled = False; self._log_popup_txt = None; _cf[0] = None
            try: popup.destroy()
            except Exception: pass
        _cf[0] = _close
        popup.protocol("WM_DELETE_WINDOW", _close)
        self._update_log_popup()
        def _refresh():
            if popup.winfo_exists(): self._update_log_popup(); popup.after(1000, _refresh)
        popup.after(1000, _refresh)

    def _update_log_popup(self):
        try:
            txt = self._log_popup_txt
            if txt is None: return
            at_bottom = not getattr(self, "_log_user_scrolled", False)
            anchor_line = total_before = 0
            if not at_bottom:
                try:
                    total_before = int(txt.index("end-1c").split(".")[0])
                    anchor_line  = int(txt.index("@0,0").split(".")[0])
                except Exception: pass
            txt.delete("1.0", "end")
            lines = list(self._log_lines) if hasattr(self, "_log_lines") else []
            if not lines:
                txt.insert("end", "— 로그 없음 —", "dim")
            else:
                for i, line in enumerate(lines):
                    if i > 0: txt.insert("end", "\n")
                    if   any(k in line for k in ("⏭","오프닝","엔딩","스킵")):         tag="skip"
                    elif any(k in line for k in ("🎬","👁","🔊","감지","미감지","대기")): tag="detect"
                    elif any(k in line for k in ("보정","OFFSET","싱크","상한")):        tag="sync"
                    elif any(k in line for k in ("▶","↺","🔄","정상","OK")):            tag="ok"
                    elif any(k in line for k in ("📊","정보","상태")):                  tag="info"
                    elif any(k in line for k in ("⚠","주의","경고")):                  tag="warn"
                    elif any(k in line for k in ("❌","오류","실패","취소")):           tag="err"
                    else:                                                                tag="dim"
                    txt.insert("end", line, tag)
            if at_bottom: txt.see("end")
            elif total_before > 0 and anchor_line > 0:
                total_after = int(txt.index("end-1c").split(".")[0])
                if total_after > 0: txt.yview_moveto((anchor_line-1) / total_after)
        except Exception: pass

    def _clear_log(self):
        if hasattr(self, "_log_lines"): self._log_lines.clear()
        self._update_log_popup()

    def _open_settings(self):
        popup = tk.Toplevel(self.root)
        popup.title("설정")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        popup.grab_set()
        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        pw = round(300*r); ph = round(350*r)
        self._place_popup(popup, pw, ph)
        FT = max(9, round(11*r)); FM = max(8, round(9*r)); FB = FM
        P  = round(14*r); P2 = round(18*r); PV = round(10*r)
        tk.Label(popup, text="⚙ 설정", font=("Segoe UI", FT, "bold"), bg=self.BG, fg=self.TEXT).pack(pady=(P, 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(12*r), 0))
        ts = tk.BooleanVar(value=self._startup_var.get())
        ta = tk.BooleanVar(value=self._autostart_var.get())
        td = tk.BooleanVar(value=self._darkmode_var.get())
        tsc= tk.StringVar( value=self._scale_var.get())
        to = tk.BooleanVar(value=self._oped_auto_var.get())
        te = tk.StringVar( value=self._oped_skip_sec_var.get())
        CHK = dict(font=("Consolas", FM), bg=self.BG2, selectcolor=self.BG3, activebackground=self.BG2, activeforeground=self.TEXT, relief="flat", cursor="hand2")
        card = tk.Frame(popup, bg=self.BG2, padx=P2, pady=P)
        card.pack(fill="x", padx=P, pady=(PV, 0))
        for text, var in [("Windows 시작 시 자동 실행", ts), ("프로그램 실행 시 자동 시작", ta), ("다크 모드", td), ("OP/ED 자동 스킵", to)]:
            tk.Checkbutton(card, text=text, variable=var, fg=self.TEXT, **CHK).pack(anchor="w", pady=round(4*r))
        sr = tk.Frame(card, bg=self.BG2)
        sr.pack(anchor="w", pady=(round(6*r), 0))
        tk.Label(sr, text="스킵 초", font=("Consolas", FM), bg=self.BG2, fg=self.TEXT_MID).pack(side="left", padx=(0, round(8*r)))
        vcmd = (popup.register(lambda s: s.isdigit() or s == ""), "%P")
        tk.Spinbox(sr, from_=10, to=600, textvariable=te, width=5, font=("Consolas", FM), bg=self.BG3, fg=self.TEXT, buttonbackground=self.BG3, relief="flat", validate="key", validatecommand=vcmd).pack(side="left")
        tk.Label(sr, text="초  (10~600)", font=("Consolas", max(7, FM-1)), bg=self.BG2, fg=self.TEXT_DIM).pack(side="left", padx=(round(6*r), 0))
        tk.Frame(card, bg=self.BORDER, height=1).pack(fill="x", pady=(round(8*r), round(4*r)))
        szr = tk.Frame(card, bg=self.BG2)
        szr.pack(anchor="w")
        tk.Label(szr, text="UI 크기", font=("Consolas", FM), bg=self.BG2, fg=self.TEXT_MID).pack(side="left", padx=(0, round(10*r)))
        for sz in ["소", "중", "대"]:
            tk.Radiobutton(szr, text=sz, variable=tsc, value=sz, font=("Consolas", FM), bg=self.BG2, fg=self.TEXT, selectcolor=self.BG3, activebackground=self.BG2, activeforeground=self.TEXT, relief="flat", cursor="hand2").pack(side="left", padx=round(4*r))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", padx=P, pady=(round(12*r), 0))
        def on_save():
            self._startup_var.set(ts.get()); self._autostart_var.set(ta.get())
            dc = td.get() != self._darkmode_var.get()
            sc = tsc.get() != self._scale_var.get()
            self._darkmode_var.set(td.get()); self._scale_var.set(tsc.get())
            self._oped_auto_var.set(to.get())
            try: sec = max(10, min(600, int(te.get())))
            except ValueError: sec = 90
            self._oped_skip_sec_var.set(str(sec))
            self._toggle_startup(); self._save_settings(); popup.destroy()
            if not self._running:
                try: self._stop_oped_monitor(); self._start_oped_monitor()
                except Exception: pass
            self._update_oped_btn()
            if dc: self._toggle_darkmode()
            if sc: self._toggle_scale(tsc.get())
        bf = tk.Frame(popup, bg=self.BG)
        bf.pack(pady=PV)
        tk.Button(bf, text="💾 저장", font=("Consolas", FB, "bold"), bg=self.BG3, fg=self.ACCENT, activebackground=self.BORDER, relief="flat", cursor="hand2", padx=round(16*r), pady=round(6*r), command=on_save).pack(side="left", padx=(0, round(8*r)))
        tk.Button(bf, text="닫기",   font=("Consolas", FB, "bold"), bg=self.BG3, fg=self.TEXT,   activebackground=self.BORDER, relief="flat", cursor="hand2", padx=round(16*r), pady=round(6*r), command=popup.destroy).pack(side="left")


