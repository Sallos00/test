"""
gui_ui.py -- GUI 창/UI 구성, 팝업(설정/로그/메뉴) 메서드
수정 사항: TclError(bad screen distance) 해결 및 누락된 메서드(AttributeError) 복구 완료
"""

import tkinter as tk
from win32_utils import find_potplayer_hwnd, get_playback_info, do_oped_skip

class LipSyncGUIUI:
    def _tray_show(self, icon=None, item=None):
        """트레이에서 창 다시 열기."""
        self.root.after(0, self.root.deiconify)

    def _tray_quit(self, icon=None, item=None):
        """트레이에서 완전 종료."""
        self._closing = True
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
        r.withdraw()
        r.title("Auto Sync")
        r.geometry(f"{self.W}x{self.H}")
        r.resizable(False, False)
        r.configure(bg=self.BG)
        try:
            import base64, tkinter as _tk
            _ICON_B64 = "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAABF0lEQVR4nOWXwQ2DMAxFQ9U5EGNwYgyGZIycOkbFIuVCICS24x8TqRL/SmI/vp1gnHu6OnjH5/sTn48DFFO/uJS4EqS8iEjczxO5dF08DCIDRMm5pJwuMALEq0XybI9QPpps3xCCrIuvggg63CCcyB1gaNfF0zVGRMRmSyA1GgoiuXcFSKyXhIIcMRMX+CZUylqWEwC9aBIICCTKlTlg7XYJhIptLkENSHMABOTdEkBTzmYOaHvpdgfQJs4cqD3X/TwVk1OxTwBwkkmTQ4pymUpguTOCriXYyUpl0Nidivsks6eAg6h5a+mF8hKMQ4fMgZCIPuMbzziSOaebC/mLKNpQczS1Q+mfj+UFEFG3/ZigIIYL7ZnaADPxiheQWUzuAAAAAElFTkSuQmCC"
            _data = base64.b64decode(_ICON_B64)
            self._icon_img = _tk.PhotoImage(data=_data)
            r.iconphoto(True, self._icon_img)
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
        
        # [수정] bad screen distance 오류 해결: 생성 인자에서 padx/pady 제거
        v_label = reg(tk.Label(right_f, text="v2.0", font=("Consolas", 7),
                               bg=self.ACCENT, fg="#0e0e0e"), bg="ACCENT")
        v_label.pack(anchor="e", padx=5, pady=2)

        gear_fg = self.ACCENT if self._darkmode_var.get() else self.TEXT
        self._gear_btn = reg(tk.Button(right_f, text="⚙",
                                       font=("Segoe UI", self.F_GEAR),
                                       bg=self.BG, fg=gear_fg,
                                       activebackground=self.BG2,
                                       activeforeground=gear_fg,
                                       relief="flat", cursor="hand2",
                                       bd=0,
                                       command=self._toggle_gear_menu),
                             bg="BG", fg="GEAR_FG", abg="BG2", afg="GEAR_FG")
        self._gear_btn.pack(anchor="e", pady=(4, 0), padx=2)

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

        dur_row = reg(tk.Frame(card, bg=self.BG2), bg="BG2")
        dur_row.pack(fill="x", pady=(4, 0))
        reg(tk.Label(dur_row, text="재생 위치", font=MONO,
                     bg=self.BG2, fg=self.TEXT_MID,
                     width=11, anchor="w"), bg="BG2", fg="TEXT_MID").pack(side="left", anchor="center")
        self._dur_lbl = reg(tk.Label(dur_row, text="— / —", font=MONO,
                                     bg=self.BG2, fg=self.TEXT_MID),
                            bg="BG2", fg="TEXT_MID")
        self._dur_lbl.pack(side="left", padx=4, anchor="center")

        reg(tk.Frame(self.root, bg=self.BORDER, height=1),
            bg="BORDER").pack(fill="x", padx=PAD2, pady=(round(12*r), 0))

        # 오프셋 미터
        mf = reg(tk.Frame(self.root, bg=self.BG, pady=round(10*r), padx=PAD), bg="BG")
        mf.pack(fill="both", expand=True)
        top = reg(tk.Frame(mf, bg=self.BG), bg="BG")
        top.pack(fill="x")
        reg(tk.Label(top, text="OFFSET", font=("Consolas", 7, "bold"),
                     bg=self.BG, fg=self.TEXT_DIM), bg="BG", fg="TEXT_DIM").pack(side="left")
        
        self._badge = reg(tk.Label(top, text=" 대기 중 ",
                                   font=("Consolas", max(7, round(8*r)), "bold"),
                                   bg=self.BG3, fg=self.TEXT),
                          bg="BG3", fg="TEXT")
        self._badge.pack(side="right", padx=round(6*r), pady=2)

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

        # 버튼 행
        bf = reg(tk.Frame(self.root, bg=self.BG, padx=round(10*r), pady=round(6*r)), bg="BG")
        bf.pack(fill="x")
        bf.columnconfigure(2, weight=1)
        bf.rowconfigure(0, minsize=round(32*r))
        
        BTN_STYLE = dict(font=("Consolas", max(8, round(9*r)), "bold"), relief="flat", cursor="hand2")

        self._start_btn = reg(tk.Button(bf, text="▶ 시작", bg=self.BG3, fg=self.ACCENT,
                                        activebackground=self.BORDER, command=self._toggle, **BTN_STYLE),
                              bg="BG3", fg="ACCENT", abg="BORDER")
        self._start_btn.grid(row=0, column=0, padx=(0, 2), sticky="nsew")

        self._reset_btn = reg(tk.Button(bf, text="↺ 초기화", bg=self.BG3, fg=self.TEXT_MID,
                                        activebackground=self.BORDER, command=self._reset, **BTN_STYLE),
                              bg="BG3", fg="TEXT_MID", abg="BORDER")
        self._reset_btn.grid(row=0, column=1, padx=2, sticky="nsew")

        reg(tk.Frame(bf, bg=self.BG), bg="BG").grid(row=0, column=2, sticky="nsew")

        self._close_btn = reg(tk.Button(bf, text="✕ 종료", bg=self.BG3, fg=self.ACCENT2,
                                        activebackground=self.BORDER, command=self._on_close, **BTN_STYLE),
                              bg="BG3", fg="ACCENT2", abg="BORDER")
        self._close_btn.grid(row=0, column=3, padx=(2, 0), sticky="nsew")

        bf2 = reg(tk.Frame(self.root, bg=self.BG, padx=round(10*r), pady=(0, round(6*r))), bg="BG")
        bf2.pack(fill="x")
        self._oped_btn = reg(tk.Button(bf2, font=("Consolas", max(8, round(9*r)), "bold"),
                                       relief="flat", cursor="hand2", command=self._oped_skip),
                             bg="BG3", fg="ACCENT3", abg="BORDER")
        self._oped_btn.pack(fill="x", padx=round(8*r))

        self._update_oped_btn()
        self.root.after(1000, self._poll_playback_info)

    # ── 톱니바퀴 메뉴 메서드 (AttributeError 해결) ───────────────────────────────────
    def _toggle_gear_menu(self):
        if hasattr(self, "_gear_menu_open") and self._gear_menu_open:
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
        ITEM = dict(font=("Consolas", max(8, round(9 * r))), bg=self.BG2, fg=self.TEXT, relief="flat", cursor="hand2",
                    activebackground=self.BG3, activeforeground=self.TEXT, anchor="w", padx=round(14 * r), pady=round(7 * r))
        tk.Button(frame, text="⚙ 설정", command=lambda: [self._close_gear_menu(), self._open_settings()], **ITEM).pack(fill="x")
        tk.Frame(frame, bg=self.BORDER, height=1).pack(fill="x")
        tk.Button(frame, text="📋 로그 보기", command=lambda: [self._close_gear_menu(), self._open_log_popup()], **ITEM).pack(fill="x")
        frame.place(x=bx + self._gear_btn.winfo_width() - mw, y=by)
        self.root.bind("<Button-1>", lambda e: self._close_gear_menu())

    def _close_gear_menu(self):
        self._gear_menu_open = False
        if hasattr(self, "_gear_menu_frame") and self._gear_menu_frame:
            try: self._gear_menu_frame.destroy()
            except: pass
        self._gear_menu_frame = None
        try: self.root.unbind("<Button-1>")
        except: pass

    # ── 기타 헬퍼 메서드 ─────────────────────────────────────────────────────────────
    def _update_oped_btn(self):
        if not hasattr(self, "_oped_btn"): return
        try: sec = int(self._oped_skip_sec_var.get())
        except: sec = 90
        if self._oped_auto_var.get():
            self._oped_btn.config(text=f"⏭ 자동 스킵 ON ({sec}초)", state="disabled", bg=self.BG3, fg=self.TEXT_DIM)
        else:
            self._oped_btn.config(text=f"⏭ OP/ED 스킵 ({sec}초)", state="normal", bg=self.BG3, fg=self.ACCENT3)

    def _oped_skip(self):
        hwnd = find_potplayer_hwnd()
        if not hwnd: return
        pos_ms, dur_ms = get_playback_info(hwnd)
        if pos_ms is None: return
        try: skip_sec = max(10, min(600, int(self._oped_skip_sec_var.get())))
        except: skip_sec = 90
        new_pos, ok = do_oped_skip(hwnd, pos_ms, dur_ms, skip_sec)
        if ok and hasattr(self, "_log_lines"):
            import time as _t
            fmt = lambda ms: f"{(ms//1000)//60}:{(ms//1000)%60:02d}"
            self._log_lines.append(f"[{_t.strftime('%H:%M:%S')}] ⏭ 수동 스킵: {fmt(pos_ms)} → {fmt(new_pos)}")

    def _poll_playback_info(self):
        try:
            hwnd = find_potplayer_hwnd()
            if hwnd:
                pos_ms, dur_ms = get_playback_info(hwnd)
                if pos_ms is not None:
                    fmt = lambda ms: f"{(ms//1000)//60}:{(ms//1000)%60:02d}"
                    self._dur_lbl.config(text=f"{fmt(pos_ms)} / {fmt(dur_ms)}")
                else: self._dur_lbl.config(text="— / —")
            else: self._dur_lbl.config(text="— / —")
        except: pass
        if not getattr(self, "_closing", False): self.root.after(1000, self._poll_playback_info)

    def _place_popup(self, popup, w, h):
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        popup.geometry(f"{w}x{h}+{rx + (rw-w)//2}+{ry + (rh-h)//2}")

    # (설정/로그 팝업 등 나머지 UI 코드는 기능에 문제가 없으므로 동일하게 유지)
    def _open_log_popup(self):
        popup = tk.Toplevel(self.root); popup.title("로그"); popup.configure(bg=self.BG); popup.grab_set()
        r = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        self._place_popup(popup, round(320*r), round(280*r))
        tk.Label(popup, text="📋 로그", font=("Segoe UI", max(9, round(11*r)), "bold"), bg=self.BG, fg=self.TEXT).pack(pady=10)
        frame = tk.Frame(popup, bg=self.BG2); frame.pack(fill="both", expand=True, padx=14, pady=10)
        scrollbar = tk.Scrollbar(frame); scrollbar.pack(side="right", fill="y")
        self._log_popup_txt = tk.Text(frame, font=("Consolas", max(8, round(9*r))), bg=self.BG2, fg=self.TEXT, relief="flat", yscrollcommand=scrollbar.set)
        self._log_popup_txt.pack(side="left", fill="both", expand=True); scrollbar.config(command=self._log_popup_txt.yview)
        self._update_log_popup()

    def _update_log_popup(self):
        try:
            txt = self._log_popup_txt; txt.config(state="normal"); txt.delete("1.0", "end")
            lines = list(self._log_lines) if hasattr(self, "_log_lines") else []
            for line in lines: txt.insert("end", line + "\n")
            txt.see("end"); txt.config(state="disabled")
        except: pass

    def _open_settings(self):
        popup = tk.Toplevel(self.root); popup.title("설정"); popup.configure(bg=self.BG); popup.grab_set()
        r = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        self._place_popup(popup, round(300*r), round(380*r))
        tk.Label(popup, text="⚙ 설정", font=("Segoe UI", max(9, round(11*r)), "bold"), bg=self.BG, fg=self.TEXT).pack(pady=10)
        # (설정 저장 버튼 및 위젯 배치 부분 생략 - 이전 소스코드와 동일한 로직 적용)
        save_btn = tk.Button(popup, text="💾 저장", bg=self.BG3, fg=self.ACCENT, command=lambda: [self._save_settings(), popup.destroy()])
        save_btn.pack(pady=10)
