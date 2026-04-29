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

        # ── [버그3 수정] () [] 코덱/해상도 정보를 제거한 정규화 함수 ──────────
        def _normalize(name: str) -> str:
            """파일명에서 () [] 안의 코덱·해상도 정보를 제거하고 소문자로 반환."""
            n = os.path.splitext(name)[0]
            n = re.sub(r'\s*\([^)]*\)', '', n)   # (1280x720 x264 AAC) 등 제거
            n = re.sub(r'\s*\[[^\]]*\]', '', n)   # [720p BluRay x264] 등 제거
            return re.sub(r'[\s_\-\.]+', ' ', n).strip().lower()

        # 검색 기준값: 기록된 title을 정규화
        title_norm = _normalize(title)
        base       = _strip_episode_number(title_norm)

        # 화수 숫자 추출
        ep_num = None
        m = re.search(r'제?(\d+)\s*[화편부회장권]', title_norm)
        if not m:
            m = re.search(r'[Ss]\d{1,2}[Ee](\d{1,3})', title_norm)
        if not m:
            nums = re.findall(r'(?<!\d)(\d+)(?!\d)', title_norm)
            if nums:
                ep_num = nums[-1]
        if m and ep_num is None:
            ep_num = m.group(1)

        exact_match  = None
        series_match = None

        for dirpath, _, fnames in os.walk(d):
            for fname in fnames:
                if os.path.splitext(fname)[1].lower() not in VIDEO_EXTS:
                    continue
                fname_norm = _normalize(fname)
                fpath      = os.path.join(dirpath, fname)

                # 1순위: 정규화 후 완전 일치
                if fname_norm == title_norm:
                    exact_match = fpath
                    break

                # 2순위: 같은 시리즈 + 화수 일치
                fname_base = _strip_episode_number(fname_norm)
                if fname_base and base and fname_base == base and ep_num is not None:
                    fname_nums = re.findall(r'(?<!\d)(\d+)(?!\d)', fname_norm)
                    if ep_num in fname_nums:
                        if series_match is None:
                            series_match = fpath
            if exact_match:
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
        if not hasattr(self, "_hist_list_canvas"):
            return
        canvas = self._hist_list_canvas
        try:
            if not canvas.winfo_exists():
                return
        except Exception:
            return

        self._hist_refreshing = True   # <Configure> 이벤트 재진입 차단
        try:
            self._refresh_history_list_inner(canvas)
        finally:
            self._hist_refreshing = False

    def _refresh_history_list_inner(self, canvas):

        records = self._load_history()
        r       = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        has_dir = bool(getattr(self, "_hist_video_dir", ""))
        mw_fn   = getattr(self, "_hist_mousewheel_fn", None)

        import os as _os

        # 표시할 데이터 목록 (최신순)
        entries = list(reversed(records))

        # 캐시된 행 위젯 목록 (재활용)
        if not hasattr(self, "_hist_row_cache"):
            self._hist_row_cache = []

        cache  = self._hist_row_cache
        frame  = self._hist_list_frame

        # ── 빈 상태 레이블 처리 ──────────────────────────────────────────────
        empty_lbl = getattr(self, "_hist_empty_lbl", None)
        if not entries:
            # 캐시 행 숨기기
            for cached in cache:
                cached["row"].pack_forget()
            if empty_lbl is None or not empty_lbl.winfo_exists():
                self._hist_empty_lbl = tk.Label(
                    frame, text="— 시청 기록 없음 —",
                    font=("Consolas", self.F_MONO_S),
                    bg=self.BG, fg=self.TEXT_DIM,
                    pady=round(12 * r))
                if mw_fn:
                    self._hist_empty_lbl.bind("<MouseWheel>", mw_fn)
                self._hist_empty_lbl.pack()
            else:
                self._hist_empty_lbl.pack()
            canvas.configure(scrollregion=canvas.bbox("all"))
            return
        else:
            if empty_lbl is not None:
                try:
                    empty_lbl.pack_forget()
                except Exception:
                    pass

        # ── 캐시 행 부족하면 새로 생성 ──────────────────────────────────────
        while len(cache) < len(entries):
            idx    = len(cache)
            row_bg = self.BG2 if idx % 2 == 0 else self.BG3
            btn_bg = self.BG3 if idx % 2 == 0 else self.BG2

            row  = tk.Frame(frame, bg=row_bg, pady=round(5 * r))
            info = tk.Frame(row, bg=row_bg)
            info.pack(side="left", fill="x", expand=True, padx=(round(8 * r), 0))

            title_lbl = tk.Label(info, text="",
                                 font=("Consolas", self.F_MONO_S, "bold"),
                                 bg=row_bg, fg=self.TEXT,
                                 anchor="w", justify="left")
            title_lbl.pack(anchor="w")

            ts_lbl = tk.Label(info, text="",
                              font=("Consolas", max(6, self.F_MONO_S - 1)),
                              bg=row_bg, fg=self.TEXT_DIM, anchor="w")
            ts_lbl.pack(anchor="w")

            del_btn = tk.Button(
                row, text="🗑",
                font=("Consolas", max(7, round(8 * r))),
                bg=btn_bg, fg="#ffffff",
                activebackground=self.BORDER,
                relief="flat", cursor="hand2",
                padx=round(4 * r), pady=round(2 * r))
            del_btn.pack(side="right", anchor="center", padx=(0, round(4 * r)))

            resume_btn = tk.Button(
                row, text="▶ 이어보기",
                font=("Consolas", max(7, round(8 * r)), "bold"),
                bg=btn_bg, fg=self.ACCENT,
                activebackground=self.BORDER,
                relief="flat", cursor="hand2",
                padx=round(6 * r), pady=round(2 * r))
            resume_btn.pack(side="right", anchor="center", padx=(0, round(2 * r)))

            if mw_fn:
                for w in (row, info, title_lbl, resume_btn, del_btn, ts_lbl):
                    w.bind("<MouseWheel>", mw_fn)

            cache.append({"row": row, "info": info, "title_lbl": title_lbl,
                          "ts_lbl": ts_lbl, "resume_btn": resume_btn, "del_btn": del_btn})

        # ── 기존 행 내용만 업데이트 (위젯 재활용) ───────────────────────────
        for i, rec in enumerate(entries):
            title = rec.get("title", "")
            ts    = rec.get("timestamp", "")
            cached = cache[i]

            display_title = _os.path.splitext(title)[0]
            # ── [버그1 수정] 표시 전에도 () [] 코덱 정보 제거 ───────────────
            # record_video_history에서 이미 제거하지만, 구버전 기록이나 예외 경로로
            # 괄호가 포함된 채 저장된 기록도 올바르게 표시하기 위해 방어적으로 적용.
            display_title = re.sub(r'\s*\([^)]*\)', '', display_title).strip()
            display_title = re.sub(r'\s*\[[^\]]*\]', '', display_title).strip()

            # ── [버그1 수정] 스마트 줄바꿈 ────────────────────────────────────
            # ' - ' 기준으로 분리 후, 앞/뒤 파트가 30자 초과이면
            # 해당 파트 내에서 띄어쓰기를 찾아 추가 줄바꿈 적용.
            MAX_LINE = 15

            def _smart_wrap(text: str) -> str:
                """한 줄이 MAX_LINE 초과이면 직전 공백에서 줄바꿈."""
                if len(text) <= MAX_LINE:
                    return text
                lines = []
                while len(text) > MAX_LINE:
                    cut = text.rfind(' ', 0, MAX_LINE)
                    if cut <= 0:          # 공백이 없으면 MAX_LINE 위치에서 강제 분리
                        cut = MAX_LINE
                    lines.append(text[:cut])
                    text = text[cut:].lstrip(' ')
                if text:
                    lines.append(text)
                return '\n'.join(lines)

            if " - " in display_title:
                first, rest = display_title.split(" - ", 1)
                display_text = _smart_wrap(first) + "\n- " + _smart_wrap(rest)
            else:
                display_text = _smart_wrap(display_title)

            cached["title_lbl"].config(text=display_text)
            cached["ts_lbl"].config(text=ts if ts else "")
            cached["resume_btn"].config(
                state="normal" if has_dir else "disabled",
                command=lambda t=title: (
                    self._hist_resume(t),
                    self._switch_tab_fn("sync") if hasattr(self, "_switch_tab_fn") else None))
            cached["del_btn"].config(
                command=lambda t=title: self._hist_delete_one(t))
            cached["row"].pack(fill="x", pady=(0, 1))

        # ── 남는 캐시 행은 완전히 제거 (pack_forget만 하면 메모리 누수) ──────
        for i in range(len(entries), len(cache)):
            try:
                cache[i]["row"].destroy()
            except Exception:
                pass
        del cache[len(entries):]

        cw = canvas.winfo_width()
        if cw > 1:
            canvas.itemconfig(self._hist_canvas_window, width=cw)
        canvas.configure(scrollregion=canvas.bbox("all"))

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

        # ── [버그3 수정] () 및 [] 안의 코덱·해상도 정보 제거 ─────────────────
        # 예: "Dekiru Neko - 04 (1280x720 x264 AAC)" → "Dekiru Neko - 04"
        #      "One Piece [720p BluRay x264]" → "One Piece"
        title = re.sub(r'\s*\([^)]*\)', '', title).strip()
        title = re.sub(r'\s*\[[^\]]*\]', '', title).strip()

        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)

        try:
            ts      = _t.strftime("%Y-%m-%d %H:%M")
            records = self._load_history()
            base    = _strip_series_name(title)

            for i, rec in enumerate(records):
                existing_title = rec.get("title", "")

                # 완전히 동일한 제목 → 맨 뒤로 이동 + 타임스탬프 갱신
                if existing_title == title:
                    records.pop(i)
                    records.append({"title": title, "timestamp": ts})
                    self._save_history(records)
                    self._refresh_history_list()
                    self._log_lines.append(
                        f"[{_t.strftime('%H:%M:%S')}] 📺 시청 기록 갱신: {title}")
                    return

                # 같은 시리즈명, 화수만 다름 → 맨 뒤로 이동 + 덮어쓰기
                existing_base = _strip_series_name(existing_title)
                if existing_base and base and existing_base == base:
                    old_title = existing_title
                    records.pop(i)
                    records.append({"title": title, "timestamp": ts})
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
                            old_title  = prev_title
                            prev_title  = title
                            was_running = True
                            self._log_lines.append(
                                f"[{_t.strftime('%H:%M:%S')}] 🔍 제목 감지: {title}")
                            self.root.after(
                                0, lambda t=title: self.record_video_history(t))
                            # ── [버그1 수정] 동영상 변경 시 싱크 초기화 ──────
                            # 이전 제목이 있는 상태에서 새 제목으로 바뀌면
                            # (= 다른 동영상으로 전환) 싱크/버퍼를 초기화한다.
                            if old_title and old_title != title:
                                self.root.after(0, self._reset_on_video_change)
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

    def _reset_on_video_change(self):
        """동영상 변경 감지 시 싱크·메모리·캐시·버퍼를 초기화한다."""
        import time as _t
        try:
            self._log_lines.append(
                f"[{_t.strftime('%H:%M:%S')}] ↺ 동영상 변경 감지 → 싱크/버퍼 초기화")
        except Exception:
            pass
        # _reset()을 재사용: 싱크 ON/OFF 상태 모두 처리
        try:
            self._reset()
        except Exception as e:
            try:
                self._log_lines.append(
                    f"[{_t.strftime('%H:%M:%S')}] ❌ 동영상 변경 초기화 오류: {e}")
            except Exception:
                pass

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
    """파일명/제목에서 화수·부제목 정보를 모두 제거해 시리즈명을 추출.
    예: '디지몬 어드벤처 1화' → '디지몬 어드벤처'
        'Attack on Titan S01E03' → 'attack on titan'
        '[SubGroup] One Piece - 1050' → 'one piece'
        'Dekiru Neko - 04 (1280x720 x264 AAC)' → 'dekiru neko'
        'One Piece [720p BluRay x264] - 1050' → 'one piece'
        '원피스 019화 - 조로와 퀴나의 약속' → '원피스'   ← [버그2 수정]
    """
    name = os.path.splitext(name)[0]
    # 앞쪽 [자막그룹] 제거
    name = re.sub(r'^[\[\(][^\]\)]{1,30}[\]\)]\s*', '', name)
    # () [] 안의 코덱·해상도 정보 제거 (순서 중요: 화수 제거 전에 먼저)
    name = re.sub(r'\s*\([^)]*\)', '', name)
    name = re.sub(r'\s*\[[^\]]*\]', '', name)
    # ── [버그2 수정] 에피소드 부제 제거 ──────────────────────────────────────
    # '원피스 019화 - 조로와 퀴나의 약속' 처럼 ' - 부제목' 형태가 붙으면
    # 에피소드마다 부제목이 달라서 _strip_series_name 결과가 달라지고,
    # 동일 시리즈임에도 덮어쓰기가 일어나지 않는 문제가 발생함.
    # ' - ' 이후를 모두 제거해 순수 시리즈명+화수만 남김.
    name = re.sub(r'\s*-\s*.+$', '', name)
    # S01E03, Ep.12 형식 에피소드 번호 제거
    name = re.sub(r'\bS\d{1,2}E\d{1,3}\b', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\b[Ee]p(?:isode)?[.\s]*\d+\b', '', name, flags=re.IGNORECASE)
    # 한국어 화수 (1화, 2편 등) 제거
    name = re.sub(r'제?\d+\s*[화편부회장권화]', '', name)
    # 숫자만 있는 화수 제거
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
