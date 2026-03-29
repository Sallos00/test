"""gui_record.py -- 녹화 및 캡처 팝업"""
import os, time, threading
import tkinter as tk
from tkinter import filedialog
from gui_record_backend import (
    _CV2_OK, _SF_OK, _PIL_OK,
    _get_potplayer_rect, _show_overlay,
    _AudioRecorder, _ScreenRecorder, _save_mp4,
)

class RecordCapturePopup:
    """
    설정 팝업에서 '녹화 및 캡처' 버튼을 눌렀을 때 열리는 팝업.
    parent_gui : LipSyncGUI 인스턴스 (테마 상수·_place_popup 등 공유)
    """

    SETTING_KEY = "record_save_dir"

    def __init__(self, parent_gui):
        self.gui = parent_gui
        self._recording    = False
        self._screen_rec   = None
        self._audio_rec    = None
        self._rec_thread   = None
        self._popup        = None
        self._rec_btn      = None
        self._save_dir_var = None
        self._tab_frame    = None
        self._tabs         = {}

        # 저장된 경로 불러오기
        self._save_dir = self.gui._load_setting(self.SETTING_KEY, "")

    # ── 팝업 열기 ──────────────────────────────────────────────────────────
    def open(self):
        try:
            self._open_impl()
        except Exception as e:
            import traceback, collections
            msg = "❌ 녹화/캡처 오류: " + str(e) + "\n" + traceback.format_exc()
            print(msg)
            try:
                if not hasattr(self.gui, '_log_lines'):
                    self.gui._log_lines = collections.deque(maxlen=100)
                self.gui._log_lines.append(msg)
            except Exception:
                pass

    def _open_impl(self):
        if self._popup and self._popup.winfo_exists():
            self._popup.lift()
            return

        g   = self.gui
        r   = g.SCALES.get(g._scale_var.get(), g.SCALES["소"])["scale"]
        pw  = round(340 * r)
        ph  = round(400 * r)

        popup = tk.Toplevel(g.root)
        popup.title("녹화 및 캡처")
        popup.resizable(False, False)
        popup.configure(bg=g.BG)
        g._place_popup(popup, pw, ph)
        popup.grab_set()
        self._popup = popup

        F_TITLE = max(9,  round(11 * r))
        F_MONO  = max(8,  round(9  * r))
        F_BTN   = max(8,  round(9  * r))
        PAD     = round(14 * r)
        PAD2    = round(18 * r)
        PAD_V   = round(8  * r)

        # 제목
        tk.Label(popup, text="🎬 녹화 및 캡처",
                 font=("Segoe UI", F_TITLE, "bold"),
                 bg=g.BG, fg=g.TEXT).pack(pady=(PAD, 0))
        tk.Frame(popup, bg=g.BORDER, height=1).pack(fill="x", pady=(round(8*r), 0))

        # ── 저장 위치 ────────────────────────────────────────────────────
        dir_card = tk.Frame(popup, bg=g.BG2, padx=PAD2, pady=PAD_V)
        dir_card.pack(fill="x", padx=PAD, pady=(PAD_V, 0))

        tk.Label(dir_card, text="저장 위치",
                 font=("Consolas", F_MONO, "bold"),
                 bg=g.BG2, fg=g.TEXT_MID).pack(anchor="w")

        dir_row = tk.Frame(dir_card, bg=g.BG2)
        dir_row.pack(fill="x", pady=(round(4*r), 0))

        self._save_dir_var = tk.StringVar(value=self._save_dir)
        dir_entry = tk.Entry(dir_row,
                             textvariable=self._save_dir_var,
                             font=("Consolas", max(7, F_MONO - 1)),
                             bg=g.BG3, fg=g.TEXT,
                             insertbackground=g.ACCENT,
                             relief="flat", bd=4,
                             state="readonly")
        dir_entry.pack(side="left", fill="x", expand=True)

        btn_kw = dict(font=("Consolas", F_BTN, "bold"),
                      relief="flat", cursor="hand2",
                      padx=round(8*r), pady=round(3*r),
                      activebackground=g.BORDER)

        tk.Button(dir_row, text="📂",
                  bg=g.BG3, fg=g.TEXT,
                  command=self._pick_dir, **btn_kw).pack(side="left", padx=(4, 0))
        tk.Button(dir_row, text="🗂 열기",
                  bg=g.BG3, fg=g.TEXT_MID,
                  command=self._open_dir, **btn_kw).pack(side="left", padx=(4, 0))

        tk.Frame(popup, bg=g.BORDER, height=1).pack(fill="x", padx=PAD, pady=(PAD_V, 0))

        # ── 탭 버튼 ──────────────────────────────────────────────────────
        tab_btn_f = tk.Frame(popup, bg=g.BG)
        tab_btn_f.pack(fill="x", padx=PAD, pady=(PAD_V, 0))

        self._tab_btns  = {}
        self._tab_pages = {}
        self._cur_tab   = tk.StringVar(value="record")

        tab_content = tk.Frame(popup, bg=g.BG2)
        tab_content.pack(fill="both", expand=True, padx=PAD, pady=(0, PAD_V))

        def switch_tab(name):
            self._cur_tab.set(name)
            for n, page in self._tab_pages.items():
                page.pack_forget()
            self._tab_pages[name].pack(fill="both", expand=True)
            for n, btn in self._tab_btns.items():
                if n == name:
                    btn.config(bg=g.BG3, fg=g.ACCENT)
                else:
                    btn.config(bg=g.BG, fg=g.TEXT_MID)

        for label, key in [("🔴 녹화", "record"), ("📷 캡처", "capture")]:
            b = tk.Button(tab_btn_f, text=label,
                          font=("Consolas", F_BTN, "bold"),
                          relief="flat", cursor="hand2",
                          padx=round(12*r), pady=round(5*r),
                          command=lambda k=key: switch_tab(k))
            b.pack(side="left", padx=(0, 4))
            self._tab_btns[key] = b

        # 각 탭 페이지 생성
        record_page  = tk.Frame(tab_content, bg=g.BG2, padx=PAD2, pady=PAD_V)
        capture_page = tk.Frame(tab_content, bg=g.BG2, padx=PAD2, pady=PAD_V)
        self._tab_pages["record"]  = record_page
        self._tab_pages["capture"] = capture_page

        self._build_record_tab(record_page,  r, F_MONO, F_BTN, PAD_V, g)
        self._build_capture_tab(capture_page, r, F_MONO, F_BTN, PAD_V, g)

        switch_tab("record")

        # 닫기 버튼
        tk.Frame(popup, bg=g.BORDER, height=1).pack(fill="x", padx=PAD)
        tk.Button(popup, text="닫기",
                  font=("Consolas", F_BTN, "bold"),
                  bg=g.BG3, fg=g.TEXT,
                  activebackground=g.BORDER,
                  relief="flat", cursor="hand2",
                  padx=round(16*r), pady=round(5*r),
                  command=self._on_close).pack(pady=PAD_V)

        popup.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── 녹화 탭 ────────────────────────────────────────────────────────────
    def _build_record_tab(self, parent, r, F_MONO, F_BTN, PAD_V, g):
        self._range_var = tk.BooleanVar(value=False)

        # 구간녹화 체크박스
        chk = tk.Checkbutton(parent,
                              text="구간 녹화",
                              variable=self._range_var,
                              font=("Consolas", F_MONO),
                              bg=g.BG2, fg=g.TEXT,
                              selectcolor=g.BG3,
                              activebackground=g.BG2,
                              activeforeground=g.TEXT,
                              relief="flat", cursor="hand2",
                              command=self._on_range_toggle)
        chk.pack(anchor="w", pady=(0, round(4*r)))

        # 구간 입력 행
        range_row = tk.Frame(parent, bg=g.BG2)
        range_row.pack(anchor="w", pady=(0, round(8*r)))

        vcmd = (parent.winfo_toplevel().register(
            lambda s: all(c.isdigit() or c == ":" for c in s) or s == ""
        ), "%P")

        self._start_time_var = tk.StringVar(value="00:00")
        self._end_time_var   = tk.StringVar(value="00:00")

        tk.Label(range_row, text="시작",
                 font=("Consolas", max(7, F_MONO - 1)),
                 bg=g.BG2, fg=g.TEXT_MID).pack(side="left")
        self._start_entry = tk.Entry(range_row,
                                     textvariable=self._start_time_var,
                                     width=6,
                                     font=("Consolas", F_MONO),
                                     bg=g.BG3, fg=g.TEXT,
                                     insertbackground=g.ACCENT,
                                     relief="flat", bd=4,
                                     validate="key", validatecommand=vcmd,
                                     state="disabled")
        self._start_entry.pack(side="left", padx=(4, 0))

        tk.Label(range_row, text="~",
                 font=("Consolas", F_MONO),
                 bg=g.BG2, fg=g.TEXT_MID).pack(side="left", padx=4)

        tk.Label(range_row, text="종료",
                 font=("Consolas", max(7, F_MONO - 1)),
                 bg=g.BG2, fg=g.TEXT_MID).pack(side="left")
        self._end_entry = tk.Entry(range_row,
                                   textvariable=self._end_time_var,
                                   width=6,
                                   font=("Consolas", F_MONO),
                                   bg=g.BG3, fg=g.TEXT,
                                   insertbackground=g.ACCENT,
                                   relief="flat", bd=4,
                                   validate="key", validatecommand=vcmd,
                                   state="disabled")
        self._end_entry.pack(side="left", padx=(4, 0))

        tk.Label(range_row, text="(MM:SS)",
                 font=("Consolas", max(6, F_MONO - 2)),
                 bg=g.BG2, fg=g.TEXT_DIM).pack(side="left", padx=(6, 0))

        tk.Frame(parent, bg=g.BORDER, height=1).pack(fill="x", pady=(round(4*r), round(8*r)))

        # 녹화 버튼
        self._rec_btn = tk.Button(parent, text="⏺ 녹화 시작",
                                  font=("Consolas", F_BTN, "bold"),
                                  bg=g.BG3, fg=g.ACCENT2,
                                  activebackground=g.BORDER,
                                  relief="flat", cursor="hand2",
                                  padx=round(12*r), pady=round(6*r),
                                  command=self._toggle_record)
        self._rec_btn.pack(fill="x")
        self._update_rec_btn_state()

        # 상태 레이블
        self._rec_status = tk.Label(parent, text="",
                                    font=("Consolas", max(7, F_MONO - 1)),
                                    bg=g.BG2, fg=g.TEXT_DIM)
        self._rec_status.pack(anchor="w", pady=(round(4*r), 0))

    # ── 캡처 탭 ────────────────────────────────────────────────────────────
    def _build_capture_tab(self, parent, r, F_MONO, F_BTN, PAD_V, g):
        tk.Label(parent, text="팟플레이어 화면을 PNG로 캡처합니다.",
                 font=("Consolas", max(7, F_MONO - 1)),
                 bg=g.BG2, fg=g.TEXT_DIM).pack(anchor="w", pady=(0, round(8*r)))

        tk.Frame(parent, bg=g.BORDER, height=1).pack(fill="x", pady=(0, round(8*r)))

        self._cap_btn = tk.Button(parent, text="📷 화면 캡처",
                                  font=("Consolas", F_BTN, "bold"),
                                  bg=g.BG3, fg=g.ACCENT,
                                  activebackground=g.BORDER,
                                  relief="flat", cursor="hand2",
                                  padx=round(12*r), pady=round(6*r),
                                  command=self._do_capture)
        self._cap_btn.pack(fill="x")
        self._update_cap_btn_state()

        self._cap_status = tk.Label(parent, text="",
                                    font=("Consolas", max(7, F_MONO - 1)),
                                    bg=g.BG2, fg=g.TEXT_DIM)
        self._cap_status.pack(anchor="w", pady=(round(4*r), 0))

    # ── 저장 위치 ──────────────────────────────────────────────────────────
    def _pick_dir(self):
        path = filedialog.askdirectory(title="저장 위치 선택",
                                       initialdir=self._save_dir or os.path.expanduser("~"))
        if path:
            self._save_dir = path
            self._save_dir_var.set(path)
            self.gui._save_settings()
            # 버튼 활성 상태 갱신
            self._update_rec_btn_state()
            self._update_cap_btn_state()
            # 설정 키에 저장
            try:
                self.gui._settings[self.SETTING_KEY] = path
            except Exception:
                pass

    def _open_dir(self):
        d = self._save_dir
        if d and os.path.isdir(d):
            os.startfile(d)

    def _ensure_subdir(self, sub: str) -> str:
        """저장 위치 하위에 sub 폴더를 생성하고 경로를 반환."""
        path = os.path.join(self._save_dir, sub)
        os.makedirs(path, exist_ok=True)
        return path

    # ── 버튼 상태 ──────────────────────────────────────────────────────────
    def _update_rec_btn_state(self):
        if not hasattr(self, "_rec_btn") or self._rec_btn is None:
            return
        if self._save_dir and os.path.isdir(self._save_dir):
            self._rec_btn.config(state="normal")
        else:
            self._rec_btn.config(state="disabled")

    def _update_cap_btn_state(self):
        if not hasattr(self, "_cap_btn") or self._cap_btn is None:
            return
        if self._save_dir and os.path.isdir(self._save_dir):
            self._cap_btn.config(state="normal")
        else:
            self._cap_btn.config(state="disabled")

    # ── 구간 체크 토글 ─────────────────────────────────────────────────────
    def _on_range_toggle(self):
        on = self._range_var.get()
        state = "normal" if on else "disabled"
        self._start_entry.config(state=state)
        self._end_entry.config(state=state)

    # ── 시간 파싱 ──────────────────────────────────────────────────────────
    @staticmethod
    def _parse_time(s: str) -> int:
        """'MM:SS' → 초. 실패 시 0."""
        try:
            parts = s.strip().split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            return int(parts[0])
        except Exception:
            return 0

    # ── 녹화 토글 ──────────────────────────────────────────────────────────
    def _toggle_record(self):
        if self._recording:
            self._stop_record()
        else:
            self._start_record()

    def _start_record(self):
        if not _CV2_OK:
            self._rec_status.config(text="⚠ opencv-python 필요", fg="#e0a03c")
            return
        if not _PIL_OK:
            self._rec_status.config(text="⚠ Pillow 필요", fg="#e0a03c")
            return

        g = self.gui
        use_range = self._range_var.get()
        start_sec = self._parse_time(self._start_time_var.get()) if use_range else None
        end_sec   = self._parse_time(self._end_time_var.get())   if use_range else None

        def _run():
            # 구간 녹화: 시작 시각 대기
            if use_range and start_sec is not None:
                from win32_utils import find_potplayer_hwnd, get_playback_info
                self._rec_status.config(text=f"⏳ {start_sec//60:02d}:{start_sec%60:02d} 대기 중...")
                while True:
                    hwnd = find_potplayer_hwnd()
                    if hwnd:
                        pos_ms, _ = get_playback_info(hwnd)
                        if pos_ms is not None and pos_ms // 1000 >= start_sec:
                            break
                    time.sleep(0.2)

            # 팟플레이어 PID 찾기
            import psutil
            pid = None
            for p in psutil.process_iter(["pid", "name"]):
                if "potplayer" in p.info["name"].lower():
                    pid = p.info["pid"]
                    break

            # 오디오 + 화면 동시 시작
            self._audio_rec  = _AudioRecorder()
            self._screen_rec = _ScreenRecorder()
            try:
                self._screen_rec.start(fps=30)
            except Exception as e:
                self._rec_status.config(text=f"⚠ 화면 캡처 실패: {e}", fg="#e0a03c")
                return
            if pid:
                self._audio_rec.start(pid)

            self._recording = True
            self._rec_btn.config(text="⏹ 녹화 정지", fg=g.ACCENT3)
            self._rec_status.config(text="🔴 녹화 중...", fg=g.ACCENT2)
            _show_overlay(g.root, "🔴 녹화중", duration_ms=99999999)
            self._overlay_shown = True

            # 구간 녹화: 종료 시각 대기
            if use_range and end_sec is not None:
                from win32_utils import find_potplayer_hwnd, get_playback_info
                while self._recording:
                    hwnd = find_potplayer_hwnd()
                    if hwnd:
                        pos_ms, _ = get_playback_info(hwnd)
                        if pos_ms is not None and pos_ms // 1000 >= end_sec:
                            break
                    time.sleep(0.2)
                if self._recording:
                    g.root.after(0, self._stop_record)

        self._rec_thread = threading.Thread(target=_run, daemon=True)
        self._rec_thread.start()

    def _stop_record(self):
        if not self._recording:
            return
        g = self.gui
        self._recording = False

        self._rec_btn.config(text="⏺ 녹화 시작", fg=g.ACCENT2)
        self._rec_status.config(text="💾 저장 중...", fg=g.TEXT_MID)

        # 닫혀있지 않은 오버레이 닫기 (새 오버레이로 대체됨)
        _show_overlay(g.root, "✅ 녹화가 종료되었습니다.", duration_ms=3000)

        def _save():
            try:
                video_frames, fps, size = self._screen_rec.stop()
                audio_arr, audio_sr, audio_ch = self._audio_rec.stop()

                ts = time.strftime("%Y%m%d_%H%M%S")
                video_dir = self._ensure_subdir("Video")
                out_path  = os.path.join(video_dir, f"record_{ts}.mp4")

                _save_mp4(video_frames, fps, size,
                          audio_arr, audio_sr, audio_ch,
                          out_path)
                g.root.after(0, lambda: self._rec_status.config(
                    text=f"✅ 저장 완료: Video/{os.path.basename(out_path)}",
                    fg=g.ACCENT3))
            except Exception as e:
                g.root.after(0, lambda: self._rec_status.config(
                    text=f"⚠ 저장 실패: {e}", fg="#e0a03c"))

        threading.Thread(target=_save, daemon=True).start()

    # ── 화면 캡처 ──────────────────────────────────────────────────────────
    def _do_capture(self):
        if not _PIL_OK:
            self._cap_status.config(text="⚠ Pillow 필요", fg="#e0a03c")
            return
        g = self.gui
        rect = _get_potplayer_rect()
        if rect is None:
            self._cap_status.config(text="⚠ 팟플레이어 창 없음", fg="#e0a03c")
            return

        try:
            from PIL import ImageGrab
            px, py, pw, ph = rect
            img = ImageGrab.grab(bbox=(px, py, px + pw, py + ph))

            ts = time.strftime("%Y%m%d_%H%M%S")
            shot_dir = self._ensure_subdir("Screenshot")
            out_path = os.path.join(shot_dir, f"capture_{ts}.png")
            img.save(out_path, "PNG")

            self._cap_status.config(
                text=f"✅ 저장: Screenshot/{os.path.basename(out_path)}",
                fg=g.ACCENT3)
            _show_overlay(g.root, "📷 장면이 캡처되었습니다.", duration_ms=3000)
        except Exception as e:
            self._cap_status.config(text=f"⚠ 캡처 실패: {e}", fg="#e0a03c")

    # ── 닫기 ───────────────────────────────────────────────────────────────
    def _on_close(self):
        if self._recording:
            self._stop_record()
        try:
            self._popup.destroy()
        except Exception:
            pass
        self._popup = None
