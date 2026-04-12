"""gui/ui_logic2.py -- 팝업 UI 로직 (ui_logic.py 에서 분리)"""
import os
import tkinter as tk
from win32_utils import find_potplayer_hwnd, get_playback_info, do_oped_skip, pip_send


class LipSyncGUILogic2:

    def _open_log_popup(self):
        popup = tk.Toplevel(self.root)
        popup.title("로그")
        popup.resizable(False, True)
        popup.configure(bg=self.BG)
        # 위젯을 먼저 구성하고 마지막에 표시 → 깜빡임 방지
        popup.withdraw()
        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        pw = round(320*r); ph = round(280*r)
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
        # ── [버그3 수정] 팝업 열 때 렌더 카운터·last_line 초기화 ──────────────
        # 이전 팝업이 닫힌 후 카운터가 남아있으면 재오픈 시 신규 줄을 표시 안 함
        self._log_popup_rendered  = 0
        self._log_popup_last_line = None
        def _close():
            self._log_user_scrolled = False; self._log_popup_txt = None; _cf[0] = None
            try: popup.destroy()
            except Exception: pass
        _cf[0] = _close
        popup.protocol("WM_DELETE_WINDOW", _close)
        self._update_log_popup()
        # 위젯 구성 완료 후 배치/표시
        self._place_popup(popup, pw, ph)
        def _refresh():
            if popup.winfo_exists(): self._update_log_popup(); popup.after(1000, _refresh)
        popup.after(1000, _refresh)

    def _update_log_popup(self):
        try:
            txt = self._log_popup_txt
            if txt is None: return
            lines = list(self._log_lines) if hasattr(self, "_log_lines") else []
            at_bottom = not getattr(self, "_log_user_scrolled", False)

            # 마지막으로 렌더링한 줄을 기억해 동기화 여부 판단.
            # _log_lines는 maxlen=100 deque라 101번째 줄이 오면 앞이 밀려남.
            # 줄 수(prev_count)만 비교하면 wrap 후 len이 동일해 새 줄을 놓침.
            # 마지막 줄 내용까지 함께 비교해 wrap 여부를 확실히 감지한다.
            prev_last = getattr(self, "_log_popup_last_line", None)
            prev_count = getattr(self, "_log_popup_rendered", 0)

            cur_last = lines[-1] if lines else None
            wrap_occurred = (prev_count >= len(lines) and cur_last != prev_last)

            if not lines:
                # 로그 없음 → 전체 초기화
                txt.config(state="normal")
                txt.delete("1.0", "end")
                txt.insert("end", "— 로그 없음 —", "dim")
                self._log_popup_rendered = 0
                self._log_popup_last_line = None
                return

            if wrap_occurred or prev_count == 0:
                # deque가 wrap됐거나 첫 렌더링 → Text 위젯 전체 재동기화.
                # Text 위젯 줄 수를 _log_lines(최대 100줄)와 동일하게 유지해
                # 무제한 누적으로 인한 메모리 증가를 방지한다.
                txt.config(state="normal")
                txt.delete("1.0", "end")
                for i, line in enumerate(lines):
                    if i > 0: txt.insert("end", "\n")
                    txt.insert("end", line, self._log_tag(line))
                self._log_popup_rendered = len(lines)
                self._log_popup_last_line = cur_last
                if at_bottom: txt.see("end")
                return

            # 새로 추가된 줄만 이어 붙이기 (증분 append)
            new_lines = lines[prev_count:]
            if not new_lines:
                return
            txt.config(state="normal")
            for line in new_lines:
                txt.insert("end", "\n" + line, self._log_tag(line))
            self._log_popup_rendered = len(lines)
            self._log_popup_last_line = cur_last
            if at_bottom:
                txt.see("end")
        except Exception:
            pass

    @staticmethod
    def _log_tag(line: str) -> str:
        if   any(k in line for k in ("⏭","오프닝","엔딩","스킵")):         return "skip"
        elif any(k in line for k in ("🎬","👁","🔊","감지","미감지","대기")): return "detect"
        elif any(k in line for k in ("보정","OFFSET","싱크","상한")):        return "sync"
        elif any(k in line for k in ("▶","↺","🔄","정상","OK")):            return "ok"
        elif any(k in line for k in ("📊","정보","상태")):                  return "info"
        elif any(k in line for k in ("⚠","주의","경고")):                  return "warn"
        elif any(k in line for k in ("❌","오류","실패","취소")):           return "err"
        return "dim"

    def _clear_log(self):
        if hasattr(self, "_log_lines"): self._log_lines.clear()
        self._log_popup_rendered = 0
        self._log_popup_last_line = None
        self._update_log_popup()

    def _open_settings(self):
        popup = tk.Toplevel(self.root)
        popup.title("설정")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        # 위젯을 먼저 구성하고 마지막에 표시 → 깜빡임 방지
        # grab_set은 withdraw 이후에 호출해야 순간 노출이 없음
        popup.withdraw()
        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        pw = round(300*r); ph = round(370*r)
        FT = max(9, round(11*r)); FM = max(8, round(9*r)); FB = FM
        P  = round(14*r); P2 = round(18*r); PV = round(10*r)
        tk.Label(popup, text="⚙ 설정", font=("Segoe UI", FT, "bold"), bg=self.BG, fg=self.TEXT).pack(pady=(P, 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(12*r), 0))
        ts  = tk.BooleanVar(value=self._startup_var.get())
        ta  = tk.BooleanVar(value=self._autostart_var.get())
        td  = tk.BooleanVar(value=self._darkmode_var.get())
        tsc = tk.StringVar( value=self._scale_var.get())
        to  = tk.BooleanVar(value=self._oped_auto_var.get())
        te  = tk.StringVar( value=self._oped_skip_sec_var.get())
        tcp = tk.BooleanVar(value=getattr(self, "_close_pot_var", tk.BooleanVar(value=False)).get())
        CHK = dict(font=("Consolas", FM), bg=self.BG2, selectcolor=self.BG3, activebackground=self.BG2, activeforeground=self.TEXT, relief="flat", cursor="hand2")
        card = tk.Frame(popup, bg=self.BG2, padx=P2, pady=P)
        card.pack(fill="x", padx=P, pady=(PV, 0))
        for text, var in [("Windows 시작 시 자동 실행", ts), ("프로그램 실행 시 자동 시작", ta), ("종료 시 팟플레이어 종료", tcp), ("다크 모드", td), ("OP/ED 자동 스킵", to)]:
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
            if hasattr(self, "_close_pot_var"):
                self._close_pot_var.set(tcp.get())
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
        # 위젯 구성 완료 후 배치/표시 (grab_set은 deiconify 직전에 호출해야 깜빡임 없음)
        popup.grab_set()
        self._place_popup(popup, pw, ph)


