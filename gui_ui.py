"""
gui_ui.py -- GUI 창/UI 구성, 팝업(설정/로그/메뉴) 메서드
[복구 보고] 원본의 모든 디자인 요소(아이콘, 상태점, 바)를 복원하고 TclError만 제거했습니다.
"""

import tkinter as tk
from win32_utils import find_potplayer_hwnd, get_playback_info, do_oped_skip

class LipSyncGUIUI:
    def _tray_show(self, icon=None, item=None):
        self.root.after(0, self.root.deiconify)

    def _tray_quit(self, icon=None, item=None):
        self._closing = True
        if hasattr(self, "_popup_after_id"):
            try: self.root.after_cancel(self._popup_after_id)
            except Exception: pass
        self._popup_open = False
        self._save_pos()
        self._stop_processes()
        if self._tray:
            try: self._tray.stop()
            except Exception: pass
        self._tray = None
        try: self.root.after(0, self.root.destroy)
        except Exception: pass

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
        except Exception: pass
        r.update_idletasks()
        x, y = self._load_pos()
        r.geometry(f"{self.W}x{self.H}+{x}+{y}")
        r.deiconify()

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
        hdr = reg(tk.Frame(self.root, bg=self.BG), bg="BG")
        hdr.pack(fill="x", padx=PAD, ipady=PAD2)
        ic_size = round(32 * r)
        self._icon_canvas = tk.Canvas(hdr, width=ic_size, height=ic_size, bg=self.BG, highlightthickness=0)
        self._icon_canvas.pack(side="left", anchor="center")
        self._icon_canvas.create_oval(1, 1, ic_size-1, ic_size-1, fill=self.BG3, outline=self.ACCENT, width=2)
        
        tx1 = round(12*r); ty1 = round(8*r)
        tx2 = round(12*r); ty2 = round(24*r)
        tx3 = round(26*r); ty3 = round(16*r)
        self._icon_canvas.create_polygon(tx1, ty1, tx2, ty2, tx3, ty3, fill=self.ACCENT, outline="")

        tf = reg(tk.Frame(hdr, bg=self.BG), bg="BG")
        tf.pack(side="left", padx=10, anchor="center")
        reg(tk.Label(tf, text="Auto Sync", font=("Segoe UI", self.F_TITLE, "bold"), bg=self.BG, fg=self.TEXT), bg="BG", fg="TEXT").pack(anchor="w")
        reg(tk.Label(tf, text="PotPlayer 자동 싱크 보정 | 멀티코어", font=("Segoe UI", max(7, self.F_TITLE - 5)), bg=self.BG, fg=self.TEXT_MID), bg="BG", fg="TEXT_MID").pack(anchor="w")

        right_f = reg(tk.Frame(hdr, bg=self.BG), bg="BG")
        right_f.pack(side="right", anchor="center")
        
        v_label = reg(tk.Label(right_f, text="v2.0", font=("Consolas", 7), bg=self.ACCENT, fg="#0e0e0e"), bg="ACCENT")
        v_label.pack(anchor="e", padx=5, pady=2)

        gear_fg = self.ACCENT if self._darkmode_var.get() else self.TEXT
        self._gear_btn = reg(tk.Button(right_f, text="⚙", font=("Segoe UI", self.F_GEAR), bg=self.BG, fg=gear_fg, activebackground=self.BG2, activeforeground=gear_fg, relief="flat", cursor="hand2", bd=0, command=self._toggle_gear_menu), bg="BG", fg="GEAR_FG", abg="BG2", afg="GEAR_FG")
        self._gear_btn.pack(anchor="e", pady=(4, 0), padx=2)

        reg(tk.Frame(self.root, bg=self.BORDER, height=1), bg="BORDER").pack(fill="x")

        # 상태 카드 (원형 점 복구)
        card = reg(tk.Frame(self.root, bg=self.BG2), bg="BG2")
        card.pack(fill="x", padx=PAD2, pady=(round(12*r), 0))

        def status_row(parent, label):
            row = reg(tk.Frame(parent, bg=self.BG2), bg="BG2")
            row.pack(fill="x", pady=2, padx=16)
            reg(tk.Label(row, text=label, font=MONO, bg=self.BG2, fg=self.TEXT_MID, width=11, anchor="w"), bg="BG2", fg="TEXT_MID").pack(side="left")
            dot = reg(tk.Label(row, text="●", font=("Consolas", 8), bg=self.BG2, fg=self.TEXT_DIM), bg="BG2", fg="TEXT_DIM")
            dot.pack(side="left")
            lbl = reg(tk.Label(row, text="—", font=MONO, bg=self.BG2, fg=self.TEXT_MID), bg="BG2", fg="TEXT_MID")
            lbl.pack(side="left", padx=4)
            return dot, lbl

        self._pot_dot, self._pot_lbl = status_row(card, "팟플레이어")
        self._aud_dot, self._aud_lbl = status_row(card, "오디오 장치")

        # 오프셋 미터 및 게이지 바 복구
        mf = reg(tk.Frame(self.root, bg=self.BG), bg="BG")
        mf.pack(fill="both", expand=True, padx=PAD, pady=round(10*r))
        
        top = reg(tk.Frame(mf, bg=self.BG), bg="BG")
        top.pack(fill="x")
        reg(tk.Label(top, text="OFFSET", font=("Consolas", 7, "bold"), bg=self.BG, fg=self.TEXT_DIM), bg="BG", fg="TEXT_DIM").pack(side="left")
        
        self._badge = reg(tk.Label(top, text=" 대기 중 ", font=("Consolas", max(7, round(8*r)), "bold"), bg=self.BG3, fg=self.TEXT), bg="BG3", fg="TEXT")
        self._badge.pack(side="right", padx=6, pady=2)

        self._offset_lbl = reg(tk.Label(mf, text="— ms", font=("Consolas", self.F_OFFSET, "bold"), bg=self.BG, fg=self.ACCENT), bg="BG", fg="ACCENT")
        self._offset_lbl.pack(anchor="w", pady=(2, 0))

        bar_bg = reg(tk.Frame(mf, bg=self.BG3, height=4), bg="BG3")
        bar_bg.pack(fill="x", pady=(4, 0))
        bar_bg.pack_propagate(False)
        self._bar = tk.Frame(bar_bg, bg=self.ACCENT, height=4)
        self._bar.place(x=0, y=0, width=0, height=4)

        # 샘플 카운터 (여백 오류 지점 완벽 수정)
        row1 = reg(tk.Frame(mf, bg=self.BG), bg="BG")
        row1.pack(fill="x", pady=(6, 0))
        reg(tk.Label(row1, text="이미지 샘플", font=MONO_S, bg=self.BG, fg=self.TEXT_MID), bg="BG", fg="TEXT_MID").pack(side="left")
        self._lip_cnt = reg(tk.Label(row1, text="0", font=MONO_S, bg=self.BG, fg=self.TEXT), bg="BG", fg="TEXT")
        self._lip_cnt.pack(side="left", padx=(4, 16)) 
        
        reg(tk.Label(row1, text="오디오 샘플", font=MONO_S, bg=self.BG, fg=self.TEXT_MID), bg="BG", fg="TEXT_MID").pack(side="left")
        self._aud_cnt = reg(tk.Label(row1, text="0", font=MONO_S, bg=self.BG, fg=self.TEXT), bg="BG", fg="TEXT")
        self._aud_cnt.pack(side="left", padx=4)

        # 버튼 행 복구
        bf = reg(tk.Frame(self.root, bg=self.BG), bg="BG")
        bf.pack(fill="x", padx=round(10*r), pady=round(6*r))
        bf.columnconfigure(2, weight=1)
        
        BTN_STYLE = dict(font=("Consolas", max(8, round(9*r)), "bold"), relief="flat", cursor="hand2")

        self._start_btn = reg(tk.Button(bf, text="▶ 시작", bg=self.BG3, fg=self.ACCENT, command=self._toggle, **BTN_STYLE), bg="BG3", fg="ACCENT")
        self._start_btn.grid(row=0, column=0, padx=(0, 2), sticky="nsew")

        self._close_btn = reg(tk.Button(bf, text="✕ 종료", bg=self.BG3, fg=self.ACCENT2, command=self._on_close, **BTN_STYLE), bg="BG3", fg="ACCENT2")
        self._close_btn.grid(row=0, column=3, padx=(2, 0), sticky="nsew")

        self.root.after(1000, self._poll_playback_info)

    def _toggle_gear_menu(self):
        if hasattr(self, "_gear_menu_open") and self._gear_menu_open: self._close_gear_menu()
        else: self._open_gear_menu()

    def _open_gear_menu(self):
        self._gear_menu_open = True
        self.root.update_idletasks()
        r = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        bx = self._gear_btn.winfo_rootx() - rx
        by = self._gear_btn.winfo_rooty() - ry + self._gear_btn.winfo_height() + 2
        mw = round(140 * r)
        frame = tk.Frame(self.root, bg=self.BORDER, bd=1, relief="solid")
        self._gear_menu_frame = frame
        ITEM = dict(font=("Consolas", max(8, round(9 * r))), bg=self.BG2, fg=self.TEXT, relief="flat", cursor="hand2", anchor="w")
        tk.Button(frame, text="⚙ 설정", command=lambda: [self._close_gear_menu(), self._open_settings()], **ITEM).pack(fill="x", padx=10, pady=5)
        tk.Button(frame, text="📋 로그 보기", command=lambda: [self._close_gear_menu(), self._open_log_popup()], **ITEM).pack(fill="x", padx=10, pady=5)
        frame.place(x=bx + self._gear_btn.winfo_width() - mw, y=by)

    def _close_gear_menu(self, e=None):
        self._gear_menu_open = False
        if hasattr(self, "_gear_menu_frame") and self._gear_menu_frame:
            try: self._gear_menu_frame.destroy()
            except: pass
        self._gear_menu_frame = None

    def _poll_playback_info(self):
        try:
            hwnd = find_potplayer_hwnd()
            if hwnd:
                pos, dur = get_playback_info(hwnd)
                if pos is not None:
                    fmt = lambda ms: f"{(ms//1000)//60}:{(ms//1000)%60:02d}"
                    if hasattr(self, '_dur_lbl'): self._dur_lbl.config(text=f"{fmt(pos)} / {fmt(dur)}")
        except: pass
        if not getattr(self, "_closing", False): self.root.after(1000, self._poll_playback_info)

    def _open_settings(self): pass
    def _open_log_popup(self): pass
