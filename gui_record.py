"""gui_record.py -- 녹화 및 캡처 팝업"""
import os, time, threading
import tkinter as tk
from tkinter import filedialog
from gui_record_backend import (
    _CV2_OK, _SF_OK, _PIL_OK,
    _get_potplayer_rect, _get_potplayer_video_hwnd, _show_overlay,
    _hide_all_overlays, _show_all_overlays,
    _AudioRecorder, _ScreenRecorder, _merge_audio, _find_ffmpeg,
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

        # 저장된 경로 불러오기 (settings.json 직접 읽기)
        self._save_dir = self._load_save_dir()

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
    # ── 저장 경로 직접 읽기/쓰기 (gui_base._save_settings에 의존하지 않음) ──
    def _load_save_dir(self) -> str:
        """settings.json에서 record_save_dir만 읽어 반환. 없으면 ""."""
        try:
            import json
            with open(self.gui.CFG_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("record_save_dir", "")
        except Exception:
            return ""

    def _persist_save_dir(self, path: str):
        """record_save_dir을 settings.json에 즉시 저장. 다른 키는 건드리지 않음."""
        import json
        cfg = self.gui.CFG_FILE
        try:
            os.makedirs(os.path.dirname(cfg), exist_ok=True)
            existing = {}
            try:
                with open(cfg, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                pass
            existing["record_save_dir"] = path
            # 원자적 쓰기: 임시 파일에 먼저 쓴 뒤 rename
            tmp = cfg + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            os.replace(tmp, cfg)
            # gui_base가 나중에 _save_settings()를 호출할 때 덮어쓰지 않도록 속성도 세팅
            self.gui._record_save_dir = path
        except Exception:
            pass

    def _pick_dir(self):
        path = filedialog.askdirectory(title="저장 위치 선택",
                                       initialdir=self._save_dir or os.path.expanduser("~"))
        if path:
            self._save_dir = path
            self._save_dir_var.set(path)
            self._persist_save_dir(path)   # 즉시 독립 저장
            self._update_rec_btn_state()
            self._update_cap_btn_state()

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
        if not _find_ffmpeg():
            self._rec_status.config(
                text="⚠ ffmpeg를 찾을 수 없습니다. PATH에 추가하거나 프로그램 폴더에 ffmpeg.exe를 넣어주세요.",
                fg="#e0a03c")
            return

        g = self.gui
        use_range = self._range_var.get()
        start_sec = self._parse_time(self._start_time_var.get()) if use_range else None
        end_sec   = self._parse_time(self._end_time_var.get())   if use_range else None

        def _run():
            # 구간 녹화: 시작 시각 대기
            if use_range and start_sec is not None:
                from win32_utils import find_potplayer_hwnd, get_playback_info
                g.root.after(0, lambda: self._rec_status.config(
                    text=f"⏳ {start_sec//60:02d}:{start_sec%60:02d} 대기 중...", fg=g.TEXT_MID))
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

            if pid is None:
                g.root.after(0, lambda: self._rec_status.config(
                    text="⚠ 팟플레이어를 찾을 수 없습니다.", fg="#e0a03c"))
                return

            # 저장 경로 미리 결정
            ts        = time.strftime("%Y%m%d_%H%M%S")
            video_dir = self._ensure_subdir("Video")
            out_path  = os.path.join(video_dir, f"record_{ts}.mp4")

            # 화면 녹화 시작 (ffmpeg를 즉시 기동해 실시간 인코딩)
            self._screen_rec = _ScreenRecorder()
            try:
                self._screen_rec.start(fps=30, root=g.root, out_path=out_path)
            except Exception as e:
                g.root.after(0, lambda: self._rec_status.config(
                    text=f"⚠ 화면 캡처 실패: {e}", fg="#e0a03c"))
                return

            # 오디오 캡처 시작
            self._audio_rec = _AudioRecorder()
            self._audio_rec.start(pid)

            self._recording  = True
            self._out_path   = out_path
            g.root.after(0, lambda: self._rec_btn.config(text="⏹ 녹화 정지", fg=g.ACCENT3))
            g.root.after(0, lambda: self._rec_status.config(text="🔴 녹화 중...", fg=g.ACCENT2))
            _show_overlay(g.root, "🔴 녹화중", duration_ms=99999999)

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

        g.root.after(0, lambda: self._rec_btn.config(text="⏺ 녹화 시작", fg=g.ACCENT2))
        g.root.after(0, lambda: self._rec_status.config(text="⏳ 인코딩 완료 대기 중...", fg=g.TEXT_MID))
        _show_overlay(g.root, "✅ 녹화가 종료되었습니다.", duration_ms=3000)

        def _finish():
            import traceback, tempfile
            try:
                # 1. 화면 캡처 중단 → ffmpeg가 파이프를 닫고 인코딩 완료할 때까지 대기
                g.root.after(0, lambda: self._rec_status.config(
                    text="⏳ 영상 인코딩 완료 대기 중...", fg=g.TEXT_MID))
                tmp_video = self._screen_rec.stop()   # ffmpeg communicate() 완료까지 블로킹

                # 2. 오디오 수집 중단
                g.root.after(0, lambda: self._rec_status.config(
                    text="⏳ 오디오 병합 중...", fg=g.TEXT_MID))
                audio_arr, audio_sr, audio_ch = self._audio_rec.stop()

                # 3. 오디오를 완성된 MP4에 병합
                out_path = self._out_path
                _merge_audio(tmp_video, audio_arr, audio_sr, audio_ch, out_path)

                if os.path.isfile(out_path) and os.path.getsize(out_path) > 1024:
                    msg = f"✅ 저장 완료: Video/{os.path.basename(out_path)}"
                    g.root.after(0, lambda m=msg: self._rec_status.config(text=m, fg=g.ACCENT3))
                else:
                    g.root.after(0, lambda: self._rec_status.config(
                        text="⚠ 저장 실패: 파일이 비어있습니다.", fg="#e0a03c"))

            except Exception as e:
                tb = traceback.format_exc()
                try:
                    log_path = os.path.join(tempfile.gettempdir(), "autosinc_record_error.txt")
                    with open(log_path, "w", encoding="utf-8") as lf:
                        lf.write(tb)
                except Exception:
                    pass
                short = str(e)[:80]
                msg = f"⚠ 저장 실패: {short}"
                g.root.after(0, lambda m=msg: self._rec_status.config(text=m, fg="#e0a03c"))

        threading.Thread(target=_finish, daemon=True).start()

    # ── 화면 캡처 ──────────────────────────────────────────────────────────
    def _do_capture(self):
        if not _PIL_OK:
            self._cap_status.config(text="⚠ Pillow 필요", fg="#e0a03c")
            return
        g = self.gui

        ts       = time.strftime("%Y%m%d_%H%M%S")
        shot_dir = self._ensure_subdir("Screenshot")
        out_path = os.path.join(shot_dir, f"capture_{ts}.png")

        # ── 1순위: WGC HWND 캡처 (오버레이 자동 제외) ────────────────────
        if self._try_wgc_capture(out_path):
            self._cap_status.config(
                text=f"✅ 저장: Screenshot/{os.path.basename(out_path)}",
                fg=g.ACCENT3)
            _show_overlay(g.root, "📷 장면이 캡처되었습니다.", duration_ms=3000)
            return

        # ── 2순위: mss/ImageGrab fallback (오버레이 hide/show) ───────────
        rect = _get_potplayer_rect()
        if rect is None:
            self._cap_status.config(text="⚠ 팟플레이어 창 없음", fg="#e0a03c")
            return
        try:
            from PIL import ImageGrab
            px, py, pw, ph = rect
            _hide_all_overlays()
            # withdraw가 실제 화면에 반영될 때까지 대기
            g.root.update_idletasks()
            import time as _time
            _time.sleep(0.02)   # OS 렌더링 반영 여유
            img = ImageGrab.grab(bbox=(px, py, px + pw, py + ph))
            _show_all_overlays()
            img.save(out_path, "PNG")
            self._cap_status.config(
                text=f"✅ 저장: Screenshot/{os.path.basename(out_path)}",
                fg=g.ACCENT3)
            _show_overlay(g.root, "📷 장면이 캡처되었습니다.", duration_ms=3000)
        except Exception as e:
            _show_all_overlays()
            self._cap_status.config(text=f"⚠ 캡처 실패: {e}", fg="#e0a03c")

    def _try_wgc_capture(self, out_path: str) -> bool:
        """
        WGC로 팟플레이어 HWND를 한 프레임 캡처해 PNG로 저장.
        성공하면 True, pywinrt 없거나 실패하면 False.
        """
        try:
            from win32_utils import find_potplayer_hwnd
            import numpy as np
            import cv2 as _cv2

            hwnd = find_potplayer_hwnd()
            if hwnd is None:
                return False
            video_hwnd = _get_potplayer_video_hwnd(hwnd)
            target = video_hwnd if video_hwnd else hwnd

            # pywinrt 두 네임스페이스 모두 시도
            GraphicsCaptureItem = Direct3D11CaptureFramePool = None
            DirectXPixelFormat  = create_direct3d_device = None
            BitmapBufferAccessMode = SoftwareBitmap = None
            try:
                from winsdk.windows.graphics.capture import (
                    GraphicsCaptureItem, Direct3D11CaptureFramePool)
                from winsdk.windows.graphics.directx import DirectXPixelFormat
                from winsdk.windows.graphics.directx.direct3d11 import create_direct3d_device
                from winsdk.windows.graphics.imaging import (
                    BitmapBufferAccessMode, SoftwareBitmap)
            except ImportError:
                try:
                    from winrt.windows.graphics.capture import (
                        GraphicsCaptureItem, Direct3D11CaptureFramePool)
                    from winrt.windows.graphics.directx import DirectXPixelFormat
                    from winrt.windows.graphics.directx.direct3d11 import create_direct3d_device
                    from winrt.windows.graphics.imaging import (
                        BitmapBufferAccessMode, SoftwareBitmap)
                except ImportError:
                    return False

            item = GraphicsCaptureItem.create_for_window(target)
            if item is None:
                return False

            d3d      = create_direct3d_device()
            BGRA8    = DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED
            item_size = item.size
            pool     = Direct3D11CaptureFramePool.create(d3d, BGRA8, 1, item_size)
            session  = pool.create_capture_session(item)
            try:
                session.is_cursor_capture_enabled = False
            except Exception:
                pass
            session.start_capture()

            import threading as _th
            got = _th.Event()
            frame_box = [None]

            def _on_frame(sender, _):
                f = sender.try_get_next_frame()
                if f is not None:
                    frame_box[0] = f
                    got.set()

            pool.frame_arrived += _on_frame
            got.wait(timeout=2.0)
            session.close()
            pool.close()

            f = frame_box[0]
            if f is None:
                return False

            surface = f.surface
            sb = SoftwareBitmap.create_copy_from_surface_async(surface).get()
            buf   = sb.lock_buffer(BitmapBufferAccessMode.READ)
            plane = buf.get_plane_description(0)
            ref   = buf.create_reference()
            raw   = bytes(ref)
            h, w  = plane.height, plane.width
            arr   = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
            bgr   = _cv2.cvtColor(arr, _cv2.COLOR_BGRA2BGR)
            rgb   = _cv2.cvtColor(bgr, _cv2.COLOR_BGR2RGB)
            from PIL import Image
            Image.fromarray(rgb).save(out_path, "PNG")
            return True

        except Exception:
            return False

    # ── 닫기 ───────────────────────────────────────────────────────────────
    def _on_close(self):
        if self._recording:
            self._stop_record()
        try:
            self._popup.destroy()
        except Exception:
            pass
        self._popup = None
