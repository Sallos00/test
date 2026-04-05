"""gui/ui_logic.py -- 팝업·시청기록·PotPlayer 연동 로직 메서드

포함 메서드:
  시청 기록 : _hist_browse_dir, _hist_open_dir, _hist_resume,
              _refresh_history_list, _load_history, _save_history,
              record_video_history
  PotPlayer : _pip_toggle, _update_oped_btn, _oped_skip,
              _poll_playback_info, _start_title_watcher
  팝업      : _toggle_gear_menu, _open_gear_menu, _close_gear_menu,
              _open_log_popup, _update_log_popup, _clear_log, _open_settings

모듈 함수  : _strip_episode_number, _extract_potplayer_title
"""
import os
import re
import json
import tkinter as tk
import tkinter.filedialog as fd
from win32_utils import find_potplayer_hwnd, get_playback_info, do_oped_skip, pip_send


class LipSyncGUILogic:

    # ── 폴더 지정 / 열기 ──────────────────────────────────────────────────────
    def _hist_browse_dir(self):
        init = self._hist_video_dir if getattr(self, "_hist_video_dir", "") else "/"
        d = fd.askdirectory(title="동영상 폴더 지정", initialdir=init)
        if not d:
            return
        self._hist_video_dir = d
        self._hist_dir_lbl.config(text=d, fg=self.TEXT)
        self._hist_open_btn.config(state="normal")
        self._save_settings()
        self._refresh_history_list()

    def _hist_open_dir(self):
        d = getattr(self, "_hist_video_dir", "")
        if d and os.path.isdir(d):
            try: os.startfile(d)
            except Exception: pass

    # ── 이어보기 ──────────────────────────────────────────────────────────────
    def _hist_resume(self, title: str):
        d = getattr(self, "_hist_video_dir", "")
        if not d or not os.path.isdir(d):
            return
        VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv",
                      ".ts", ".m2ts", ".flv", ".webm", ".m4v"}
        base = _strip_episode_number(title)
        found = None
        for fname in os.listdir(d):
            fpath = os.path.join(d, fname)
            if not os.path.isfile(fpath):
                continue
            if os.path.splitext(fname)[1].lower() not in VIDEO_EXTS:
                continue
            # 정확한 파일명 일치
            if os.path.splitext(fname)[0] == title or fname == title:
                found = fpath
                break
            # 숫자만 다른 경우
            if _strip_episode_number(fname) == base:
                found = fpath
                break
        if found:
            try: os.startfile(found)
            except Exception: pass
        else:
            import tkinter.messagebox as mb
            mb.showwarning("이어보기",
                           f"폴더에서 해당 동영상을 찾을 수 없습니다.\n\n"
                           f"제목: {title}\n폴더: {d}")

    # ── 시청 기록 목록 갱신 ───────────────────────────────────────────────────
    def _refresh_history_list(self):
        frame = self._hist_list_frame
        for w in frame.winfo_children():
            w.destroy()

        records  = self._load_history()
        r        = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        has_dir  = bool(getattr(self, "_hist_video_dir", ""))

        if not records:
            tk.Label(frame, text="— 시청 기록 없음 —",
                     font=("Consolas", self.F_MONO_S),
                     bg=self.BG, fg=self.TEXT_DIM,
                     pady=round(12*r)).pack()
            return

        for i, rec in enumerate(reversed(records)):
            title   = rec.get("title", "")
            ts      = rec.get("timestamp", "")
            row_bg  = self.BG2 if i % 2 == 0 else self.BG3

            row = tk.Frame(frame, bg=row_bg, padx=round(8*r), pady=round(5*r))
            row.pack(fill="x", pady=(0, 1))

            info = tk.Frame(row, bg=row_bg)
            info.pack(side="left", fill="x", expand=True)

            tk.Label(info, text=title,
                     font=("Consolas", self.F_MONO_S, "bold"),
                     bg=row_bg, fg=self.TEXT,
                     anchor="w",
                     wraplength=round(190*r),
                     justify="left").pack(anchor="w")
            if ts:
                tk.Label(info, text=ts,
                         font=("Consolas", max(6, self.F_MONO_S-1)),
                         bg=row_bg, fg=self.TEXT_DIM,
                         anchor="w").pack(anchor="w")

            btn_bg = self.BG3 if i % 2 == 0 else self.BG2
            tk.Button(
                row, text="▶ 이어보기",
                font=("Consolas", max(7, round(8*r)), "bold"),
                bg=btn_bg, fg=self.ACCENT,
                activebackground=self.BORDER,
                relief="flat", cursor="hand2",
                padx=round(6*r), pady=round(2*r),
                state="normal" if has_dir else "disabled",
                command=lambda t=title: self._hist_resume(t)
            ).pack(side="right", anchor="center")

    # ── history.json 로드/저장 ────────────────────────────────────────────────
    def _load_history(self):
        try:
            p = os.path.join(self.APP_DIR, "history.json")
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_history(self, records):
        try:
            os.makedirs(self.APP_DIR, exist_ok=True)
            p = os.path.join(self.APP_DIR, "history.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def record_video_history(self, title: str):
        """동영상 감지 또는 이름 변경 시 호출. 숫자만 다른 기록은 덮어씀."""
        import time as _t
        ts      = _t.strftime("%Y-%m-%d %H:%M")
        records = self._load_history()
        base    = _strip_episode_number(title)

        # 완전히 동일한 제목이 이미 마지막 기록이면 무시 (재시작 후 중복 방지)
        if records and records[-1].get("title", "") == title:
            return

        for rec in records:
            if _strip_episode_number(rec.get("title", "")) == base:
                rec["title"]     = title
                rec["timestamp"] = ts
                self._save_history(records)
                if hasattr(self, "_hist_list_frame"):
                    self._refresh_history_list()
                return
        records.append({"title": title, "timestamp": ts})
        self._save_history(records)
        if hasattr(self, "_hist_list_frame"):
            self._refresh_history_list()

    # ── PIP ───────────────────────────────────────────────────────────────────
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

    def _start_title_watcher(self):
        """별도 스레드에서 PotPlayer 창 제목 변경을 감지해 시청 기록 기록."""
        import threading, ctypes, time as _t

        # 시작 시 history.json 마지막 기록을 초기 비교값으로 사용
        try:
            records = self._load_history()
            self._last_detected_title = records[-1].get("title", "") if records else ""
        except Exception:
            self._last_detected_title = ""

        def _watch():
            prev_hwnd  = None
            prev_title = self._last_detected_title
            user32     = ctypes.windll.user32
            buf        = ctypes.create_unicode_buffer(512)

            while not self._closing:
                try:
                    hwnd = find_potplayer_hwnd()
                    if hwnd:
                        user32.GetWindowTextW(hwnd, buf, 512)
                        raw   = buf.value
                        title = _extract_potplayer_title(raw)

                        if title and title != prev_title:
                            prev_title = title
                            self._last_detected_title = title
                            self.root.after(0, lambda t=title: self.record_video_history(t))
                    else:
                        prev_hwnd = None
                except Exception:
                    pass
                _t.sleep(0.5)

        t = threading.Thread(target=_watch, daemon=True, name="title-watcher")
        t.start()

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


# ── 모듈 수준 유틸 ────────────────────────────────────────────────────────────

def _strip_episode_number(name: str) -> str:
    """파일명/제목에서 에피소드 숫자를 제거해 기본 제목만 반환 (소문자)."""
    name = os.path.splitext(name)[0]
    name = re.sub(r'[\[\(]\d+[\]\)]', '', name)
    name = re.sub(r'[Ee](?:pisode)?\s*\d+', '', name, flags=re.IGNORECASE)
    name = re.sub(r'#\d+', '', name)
    name = re.sub(r'(?<!\w)\d+(?!\w)', '', name)
    name = re.sub(r'[\s_\-\.]+', ' ', name).strip()
    return name.lower()


def _extract_potplayer_title(window_title: str) -> str:
    """PotPlayer 창 제목에서 동영상 파일명을 추출."""
    if not window_title:
        return ""
    m = re.match(r'^(.+?)\s*-\s*PotPlayer', window_title, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        if title and title not in ("", "-"):
            return title
    return ""
