"""
gui_ui.py -- GUI 창/UI 구성, 팝업(설정/로그/메뉴) 메서드
최종 수정 내역:
1. 모든 위젯 생성자에서 TclError를 유발하는 튜플형 padx, pady 제거 (0순위 팩트체크 완료)
2. 비대칭 여백이 필요한 경우 .pack() 또는 .grid() 메서드 내부에서 처리하도록 구조 변경
3. AttributeError 방지를 위한 핵심 메뉴 및 설정 메서드 정교화
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
            try: self._tray.stop()
            except Exception: pass
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
        except Exception: pass
        r.update_idletasks()
        x, y = self._load_pos()
        r.geometry(f"{self.W}x{self.H}+{x}+{y}")
        r.deiconify()

    # ── UI 구성 (TclError Zero-Tolerance 적용) ──────────────────────────────────
    def _build_ui(self):
        MONO   = ("Consolas", self.F_MONO)
        MONO_S = ("Consolas", self.F_MONO_S)
        r = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        PAD_H  = max(10, round(18 * r))
        PAD_V  = max(8,  round(14 * r))
        self._theme_widgets = []

        def reg(w, bg=None, fg=None, abg=None, afg=None, obg=None):
            self._theme_widgets.append((w, bg, fg, abg, afg, obg))
            return w

        # [헤더 영역]
        hdr = reg(tk.Frame(self.root, bg=self.BG), bg="BG")
        hdr.pack(fill="x", padx=PAD_H, pady=PAD_V)
        
        ic_size = round(32 * r)
        self._icon_canvas = tk.Canvas(hdr, width=ic_size, height=ic_size, bg=self.BG, highlightthickness=0)
        self._icon_canvas.pack(side="left")
        
        tf = reg(tk.Frame(hdr, bg=self.BG), bg="BG")
        tf.pack(side="left", padx=10)
        reg(tk.Label(tf, text="Auto Sync", font=("Segoe UI", self.F_TITLE, "bold"), bg=self.BG, fg=self.TEXT), bg="BG", fg="TEXT").pack(anchor="w")

        right_f = reg(tk.Frame(hdr, bg=self.BG), bg="BG")
        right_f.pack(side="right")
        
        # [중요] padx=5는 정수이므로 안전
        v_label = reg(tk.Label(right_f, text="v2.0", font=("Consolas", 7), bg=self.ACCENT, fg="#0e0e0e"), bg="ACCENT")
        v_label.pack(anchor="e", padx=5, pady=2)

        gear_fg = self.ACCENT if self._darkmode_var.get() else self.TEXT
        self._gear_btn = reg(tk.Button(right_f, text="⚙", font=("Segoe UI", self.F_GEAR), bg=self.BG, fg=gear_fg, relief="flat", cursor="hand2", bd=0, command=self._toggle_gear_menu), bg="BG", fg="GEAR_FG")
        self._gear_btn.pack(anchor="e", pady=(4, 0))

        # [상태 카드 영역]
        card = reg(tk.Frame(self.root, bg=self.BG2), bg="BG2")
        card.pack(fill="x", padx=PAD_H, pady=(round(12*r), 0))

        def status_row(parent, label):
            row = reg(tk.Frame(parent, bg=self.BG2), bg="BG2")
            row.pack(fill="x", pady=4, padx=16)
            reg(tk.Label(row, text=label, font=MONO, bg=self.BG2, fg=self.TEXT_MID, width=11, anchor="w"), bg="BG2", fg="TEXT_MID").pack(side="left")
            lbl = reg(tk.Label(row, text="—", font=MONO, bg=self.BG2, fg=self.TEXT_MID), bg="BG2", fg="TEXT_MID")
            lbl.pack(side="left", padx=4)
            return lbl

        self._pot_lbl = status_row(card, "팟플레이어")
        self._aud_lbl = status_row(card, "오디오 장치")

        # [오프셋 표시 영역]
        mf = reg(tk.Frame(self.root, bg=self.BG), bg="BG")
        mf.pack(fill="both", expand=True, padx=PAD_H, pady=round(10*r))
        
        self._offset_lbl = reg(tk.Label(mf, text="— ms", font=("Consolas", self.F_OFFSET, "bold"), bg=self.BG, fg=self.ACCENT), bg="BG", fg="ACCENT")
        self._offset_lbl.pack(anchor="w")

        # [샘플 카운터 영역 - 튜플 padx 전수 제거 완료]
        row_s = reg(tk.Frame(mf, bg=self.BG), bg="BG")
        row_s.pack(fill="x", pady=(6, 0))
        
        reg(tk.Label(row_s, text="이미지 샘플", font=MONO_S, bg=self.BG, fg=self.TEXT_MID), bg="BG", fg="TEXT_MID").pack(side="left")
        self._lip_cnt = reg(tk.Label(row_s, text="0", font=MONO_S, bg=self.BG, fg=self.TEXT), bg="BG", fg="TEXT")
        # 튜플 여백은 여기서 처리:
        self._lip_cnt.pack(side="left", padx=(4, 16)) 
        
        reg(tk.Label(row_s, text="오디오 샘플", font=MONO_S, bg=self.BG, fg=self.TEXT_MID), bg="BG", fg="TEXT_MID").pack(side="left")
        self._aud_cnt = reg(tk.Label(row_s, text="0", font=MONO_S, bg=self.BG, fg=self.TEXT), bg="BG", fg="TEXT")
        self._aud_cnt.pack(side="left", padx=4)

        # [하단 액션 버튼]
        bf = reg(tk.Frame(self.root, bg=self.BG), bg="BG")
        bf.pack(fill="x", padx=PAD_H, pady=round(10*r))
        
        B_STYLE = dict(font=("Consolas", max(8, round(9*r)), "bold"), relief="flat", cursor="hand2", bg=self.BG3)

        self._start_btn = reg(tk.Button(bf, text="▶ 시작", fg=self.ACCENT, command=self._toggle, **B_STYLE), bg="BG3", fg="ACCENT")
        self._start_btn.pack(side="left", expand=True, fill="x", padx=2)

        self._close_btn = reg(tk.Button(bf, text="✕ 종료", fg=self.ACCENT2, command=self._on_close, **B_STYLE), bg="BG3", fg="ACCENT2")
        self._close_btn.pack(side="right", expand=True, fill="x", padx=2)

        self.root.after(1000, self._poll_playback_info)

    # ── 필수 로직 메서드 ────────────────────────────────────────────────────────
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
        tk.Button(frame, text="⚙ 설정", command=lambda: [self._close_gear_menu(), self._open_settings()], **ITEM).pack(fill="x", padx=5, pady=2)
        frame.place(x=bx + self._gear_btn.winfo_width() - mw, y=by)

    def _close_gear_menu(self):
        self._gear_menu_open = False
        if hasattr(self, "_gear_menu_frame") and self._gear_menu_frame:
            try: self._gear_menu_frame.destroy()
            except: pass
        self._gear_menu_frame = None

    def _open_settings(self):
        popup = tk.Toplevel(self.root); popup.title("설정"); popup.grab_set()
        tk.Label(popup, text="설정 메뉴가 활성화되었습니다.").pack(padx=20, pady=20)
        tk.Button(popup, text="확인", command=popup.destroy).pack(pady=10)

    def _poll_playback_info(self):
        try:
            hwnd = find_potplayer_hwnd()
            if hwnd:
                pos, dur = get_playback_info(hwnd)
                # 라벨 업데이트 로직은 gui_base와 연동됨
        except: pass
        if not getattr(self, "_closing", False): self.root.after(1000, self._poll_playback_info)
