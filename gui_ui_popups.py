"""gui_ui_popups.py -- 팝업 메서드 (로그/설정/녹화)
"""
import tkinter as tk

class LipSyncGUIPopups:

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

    def _open_record_capture(self):
        from gui_record import RecordCapturePopup
        inst = getattr(self, "_record_popup_inst", None)
        if inst is not None and inst._popup is not None:
            try:
                if inst._popup.winfo_exists():
                    inst._popup.lift()
                    return
            except Exception:
                pass
        self._record_popup_inst = RecordCapturePopup(self)
        self._record_popup_inst.open()
