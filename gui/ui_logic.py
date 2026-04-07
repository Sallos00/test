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

    # ── 전체 시청 기록 삭제 ───────────────────────────────────────────────────
    def _hist_clear_all(self):
        import tkinter.messagebox as mb
        if not mb.askyesno("시청 기록 삭제", "시청 기록을 전부 삭제하시겠습니까?"):
            return
        self._save_history([])
        self._refresh_history_list()

    # ── 개별 시청 기록 삭제 ───────────────────────────────────────────────────
    def _hist_delete_one(self, title: str):
        records = self._load_history()
        records = [r for r in records if r.get("title", "") != title]
        self._save_history(records)
        self._refresh_history_list()

    # ── 이어보기 ──────────────────────────────────────────────────────────────
    def _hist_resume(self, title: str):
        d = getattr(self, "_hist_video_dir", "")
        if not d or not os.path.isdir(d):
            return
        VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv",
                      ".ts", ".m2ts", ".flv", ".webm", ".m4v"}
        base = _strip_episode_number(title)
        # 제목에서 화수 숫자 추출: "17화", "17편", "S01E17", 순수 숫자 등
        ep_num = None
        m = re.search(r'제?(\d+)\s*[화편부회장권]', title)
        if not m:
            m = re.search(r'[Ss]\d{1,2}[Ee](\d{1,3})', title)
        if not m:
            nums = re.findall(r'(?<!\d)(\d+)(?!\d)', title)
            if nums:
                m_val = nums[-1]
                ep_num = m_val
        if m and ep_num is None:
            ep_num = m.group(1)

        found = None
        exact_match = None
        series_match = None

        for dirpath, _, fnames in os.walk(d):
            for fname in fnames:
                if os.path.splitext(fname)[1].lower() not in VIDEO_EXTS:
                    continue
                fname_noext = os.path.splitext(fname)[0]
                # 1순위: 완전 일치
                if fname_noext == title or fname == title:
                    exact_match = os.path.join(dirpath, fname)
                    break
                # 2순위: 같은 시리즈 + 화수 일치
                if _strip_episode_number(fname) == base and ep_num is not None:
                    # fname 안에서 같은 위치의 숫자가 ep_num과 일치하는지 확인
                    fname_nums = re.findall(r'(?<!\d)(\d+)(?!\d)', fname_noext)
                    if ep_num in fname_nums:
                        series_match = os.path.join(dirpath, fname)
            if exact_match:
                break
            if found:
                break

        found = exact_match or series_match
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
        if not hasattr(self, "_hist_list_frame"):
            return
        try:
            if not self._hist_list_frame.winfo_exists():
                return
        except Exception:
            return
        frame = self._hist_list_frame
        canvas = self._hist_list_canvas

        # 깜빡임 방지: 자식 위젯만 파괴 (canvas는 숨기지 않음)
        for w in frame.winfo_children():
            w.destroy()

        cw = canvas.winfo_width()
        if cw > 1:
            canvas.itemconfig(self._hist_canvas_window, width=cw)

        records  = self._load_history()
        r        = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        has_dir  = bool(getattr(self, "_hist_video_dir", ""))
        mw_fn    = getattr(self, "_hist_mousewheel_fn", None)

        if not records:
            lbl = tk.Label(frame, text="— 시청 기록 없음 —",
                     font=("Consolas", self.F_MONO_S),
                     bg=self.BG, fg=self.TEXT_DIM,
                     pady=round(12*r))
            lbl.pack()
            if mw_fn:
                lbl.bind("<MouseWheel>", mw_fn)
            return

        for i, rec in enumerate(reversed(records)):
            title   = rec.get("title", "")
            ts      = rec.get("timestamp", "")
            row_bg  = self.BG2 if i % 2 == 0 else self.BG3

            row = tk.Frame(frame, bg=row_bg, pady=round(5*r))
            row.pack(fill="x", pady=(0, 1))

            info = tk.Frame(row, bg=row_bg)
            info.pack(side="left", fill="x", expand=True, padx=(round(8*r), 0))

            # 확장자 제거 후 ' - ' 기준으로 첫 번째 줄 / 나머지 줄 분리
            import os as _os
            display_title = _os.path.splitext(title)[0]
            if " - " in display_title:
                first, rest = display_title.split(" - ", 1)
                display_text = first + "\n- " + rest
            else:
                display_text = display_title

            title_lbl = tk.Label(info, text=display_text,
                     font=("Consolas", self.F_MONO_S, "bold"),
                     bg=row_bg, fg=self.TEXT,
                     anchor="w",
                     justify="left")
            title_lbl.pack(anchor="w")
            ts_lbl = None
            if ts:
                ts_lbl = tk.Label(info, text=ts,
                         font=("Consolas", max(6, self.F_MONO_S-1)),
                         bg=row_bg, fg=self.TEXT_DIM,
                         anchor="w")
                ts_lbl.pack(anchor="w")

            btn_bg = self.BG3 if i % 2 == 0 else self.BG2

            # 🗑 버튼: 이어보기 오른쪽, 휴지통 아이콘 흰색
            del_btn = tk.Button(
                row, text="🗑",
                font=("Consolas", max(7, round(8*r))),
                bg=btn_bg, fg="#ffffff",
                activebackground=self.BORDER,
                relief="flat", cursor="hand2",
                padx=round(4*r), pady=round(2*r),
                command=lambda t=title: self._hist_delete_one(t)
            )
            del_btn.pack(side="right", anchor="center", padx=(0, round(4*r)))

            resume_btn = tk.Button(
                row, text="▶ 이어보기",
                font=("Consolas", max(7, round(8*r)), "bold"),
                bg=btn_bg, fg=self.ACCENT,
                activebackground=self.BORDER,
                relief="flat", cursor="hand2",
                padx=round(6*r), pady=round(2*r),
                state="normal" if has_dir else "disabled",
                command=lambda t=title: (self._hist_resume(t), self._switch_tab_fn("sync") if hasattr(self, "_switch_tab_fn") else None)
            )
            resume_btn.pack(side="right", anchor="center", padx=(0, round(2*r)))

            # 마우스휠 이벤트를 캔버스로 전달
            if mw_fn:
                for w in (row, info, title_lbl, resume_btn, del_btn):
                    w.bind("<MouseWheel>", mw_fn)
                if ts_lbl:
                    ts_lbl.bind("<MouseWheel>", mw_fn)

    # ── history.json 로드/저장 ────────────────────────────────────────────────
    def _load_history(self):
        try:
            p = os.path.join(self.APP_DIR, "history.json")
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_history(self, records):
        import collections, time as _t
        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)
        try:
            os.makedirs(self.APP_DIR, exist_ok=True)
            p = os.path.join(self.APP_DIR, "history.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._log_lines.append(
                f"[{_t.strftime('%H:%M:%S')}] ❌ _save_history 오류: {e}")

    def record_video_history(self, title: str):
        """동영상 재생 감지 또는 제목 변경 시 호출.
        기록 기준:
          1. 완전히 동일한 제목 → 타임스탬프만 갱신 (중복 추가 방지)
          2. 같은 시리즈명인데 화수만 다름 → 기존 기록 덮어쓰기
          3. 새로운 작품 → 신규 기록 추가
        """
        import time as _t, collections
        if not title or not title.strip():
            return

        # 다섯 번째 개선: 괄호 안 내용 제거 (예: "(AniTv 1080p x264 AAC)" 삭제)
        title = re.sub(r'\s*\([^)]*\)', '', title).strip()

        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)

        try:
            ts      = _t.strftime("%Y-%m-%d %H:%M")
            records = self._load_history()
            base    = _strip_series_name(title)

            for rec in records:
                existing_title = rec.get("title", "")

                # 완전히 동일한 제목 → 타임스탬프만 갱신
                if existing_title == title:
                    rec["timestamp"] = ts
                    self._save_history(records)
                    self._refresh_history_list()
                    self._log_lines.append(
                        f"[{_t.strftime('%H:%M:%S')}] 📺 시청 기록 갱신: {title}")
                    return

                # 같은 시리즈명, 화수만 다름 → 덮어쓰기
                existing_base = _strip_series_name(existing_title)
                if existing_base and base and existing_base == base:
                    old_title        = existing_title
                    rec["title"]     = title
                    rec["timestamp"] = ts
                    self._save_history(records)
                    self._refresh_history_list()
                    self._log_lines.append(
                        f"[{_t.strftime('%H:%M:%S')}] 📺 시청 기록 덮어쓰기: {old_title} → {title}")
                    return

            # 신규 기록 추가
            records.append({"title": title, "timestamp": ts})
            self._save_history(records)
            self._refresh_history_list()
            self._log_lines.append(
                f"[{_t.strftime('%H:%M:%S')}] 📺 시청 기록 추가: {title}")

        except Exception as e:
            self._log_lines.append(
                f"[{_t.strftime('%H:%M:%S')}] ❌ record_video_history 오류: {e}")

    # ── PIP ───────────────────────────────────────────────────────────────────
    def _pip_toggle(self):
        hwnd = find_potplayer_hwnd()
        if not hwnd: return
        pip_send(hwnd)
        if self._pip_on:
            self._pip_on = False
            self._pip_btn.config(text="⧉ PIP OFF", fg=self.TEXT_MID,
                                 bg="#0e0e0e", relief="solid", bd=1)
        else:
            self._pip_on = True
            self._pip_btn.config(text="⧉ PIP ON", fg=self.ACCENT3,
                                 bg="#0e0e0e", relief="solid", bd=1)
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
        """PotPlayer 창 제목을 1초마다 감시해 변경 시 시청 기록 저장.

        기록 기준:
          - 기준1(재생 감지 팝업)과 기준2(제목 변경) 모두 이 watcher 하나로 처리.
          - 팝업 기록과의 중복은 record_video_history 내부에서
            '완전히 동일한 제목 → 타임스탬프만 갱신' 처리로 자연스럽게 방지됨.
          - 팟플레이어가 닫혔다가 다시 열리거나 다른 영상을 열면 항상 기록.
        """
        import threading, ctypes, time as _t, collections

        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)

        def _watch():
            prev_title  = ""
            was_running = False          # 이전 루프에서 PotPlayer가 있었는지
            user32      = ctypes.windll.user32
            buf         = ctypes.create_unicode_buffer(512)

            while not getattr(self, "_closing", False):
                try:
                    hwnd = find_potplayer_hwnd()
                    if hwnd:
                        user32.GetWindowTextW(hwnd, buf, 512)
                        title = _extract_potplayer_title(buf.value)

                        if title and title != prev_title:
                            prev_title  = title
                            was_running = True
                            self._log_lines.append(
                                f"[{_t.strftime('%H:%M:%S')}] 🔍 제목 감지: {title}")
                            self.root.after(
                                0, lambda t=title: self.record_video_history(t))
                        else:
                            was_running = True
                    else:
                        # PotPlayer가 닫히면 prev_title 초기화
                        # → 같은 영상을 다시 열면 다시 기록됨
                        if was_running:
                            prev_title  = ""
                            was_running = False
                except Exception as e:
                    try:
                        self._log_lines.append(
                            f"[{_t.strftime('%H:%M:%S')}] ⚠ 타이틀 감시 오류: {e}")
                    except Exception:
                        pass
                _t.sleep(1.0)

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
        # 깜빡임 방지: 먼저 화면 밖에 place 해두고 위젯 구성 후 정확한 위치로 이동
        frame = tk.Frame(self.root, bg=self.BORDER, bd=1, relief="solid")
        self._gear_menu_frame = frame
        frame.place(x=-9999, y=-9999)          # 화면 밖에 숨김
        ITEM = dict(font=("Consolas", max(8, round(9*r))), bg=self.BG2, fg=self.TEXT, relief="flat", cursor="hand2", activebackground=self.BG3, activeforeground=self.TEXT, anchor="w", padx=round(14*r), pady=round(7*r))
        def pick(fn):
            self._close_gear_menu(); fn()
        tk.Button(frame, text="⚙ 설정",        command=lambda: pick(self._open_settings),       **ITEM).pack(fill="x")
        tk.Frame(frame, bg=self.BORDER, height=1).pack(fill="x")
        tk.Button(frame, text="🎬 녹화 및 캡처", command=lambda: pick(self._open_record_capture), **ITEM).pack(fill="x")
        tk.Frame(frame, bg=self.BORDER, height=1).pack(fill="x")
        tk.Button(frame, text="📋 로그 보기",    command=lambda: pick(self._open_log_popup),      **ITEM).pack(fill="x")
        frame.update_idletasks()               # 크기 확정
        frame.place(x=bx + self._gear_btn.winfo_width() - mw, y=by)   # 정확한 위치로 이동
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



def _strip_series_name(name: str) -> str:
    """파일명/제목에서 화수 정보만 제거해 시리즈명을 추출.
    예: '디지몬 어드벤처 1화' → '디지몬 어드벤처'
        'Attack on Titan S01E03' → 'attack on titan'
        '[SubGroup] One Piece - 1050' → 'one piece'
    """
    name = os.path.splitext(name)[0]
    name = re.sub(r'^[\[\(][^\]\)]{1,30}[\]\)]\s*', '', name)
    name = re.sub(r'[\[\(](?:1080|720|480|2160|4K|BluRay|WEB|HDTV|HEVC|x264|x265|AAC|AC3)[^\]\)]*[\]\)]', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\bS\d{1,2}E\d{1,3}\b', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\b[Ee]p(?:isode)?[.\s]*\d+\b', '', name, flags=re.IGNORECASE)
    name = re.sub(r'제?\d+\s*[화편부회장권화]', '', name)
    name = re.sub(r'(?<![\w가-힣])[-_\s]*\d{1,4}(?![\w가-힣])', '', name)
    name = re.sub(r'[\s_\-\.]+', ' ', name).strip()
    return name.lower()

def _strip_episode_number(name: str) -> str:
    """하위호환용 alias."""
    return _strip_series_name(name)

def _extract_potplayer_title(window_title: str) -> str:
    """PotPlayer 창 제목에서 동영상 파일명을 추출.
    지원 형식:
      파일명 - PotPlayer64
      파일명 - PotPlayer
      PotPlayer64 - 파일명  (일부 버전)
    """
    if not window_title:
        return ""
    m = re.match(r'^(.+?)\s*-\s*(?:PotPlayer(?:64)?|팟플레이어(?:64)?)(?:\s.*)?$', window_title, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        if title and title not in ("", "-"):
            return title
    m = re.match(r'^(?:PotPlayer(?:64)?|팟플레이어(?:64)?)\s*-\s*(.+)$', window_title, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        if title and title not in ("", "-"):
            return title
    return ""
