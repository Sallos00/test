"""
gui/record_open.py -- 녹화/캡처 팝업 열기 믹스인

변경사항:
  [기능1] 녹화 중에는 구간 녹화 체크박스 및 시간 입력칸 비활성화
  [기능2] 오버레이는 팟플레이어 위에만 (record_backend._show_overlay 개선)
  [기능3] OBS 방식 — 화면+오디오 동시 시작 후 ffmpeg 합산
"""
import os, threading
import tkinter as tk


class LipSyncGUIRecordOpen:

    def _open_record_capture(self):
        save_dir = getattr(self, "_record_save_dir", None) or self._load_setting("record_save_dir", "")

        r   = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        pw  = round(340 * r)
        ph  = round(400 * r)

        popup = tk.Toplevel(self.root)
        popup.title("녹화 및 캡처")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        popup.grab_set()
        self._place_popup(popup, pw, ph)

        F_TITLE = max(9,  round(11 * r))
        F_MONO  = max(8,  round(9  * r))
        F_BTN   = max(8,  round(9  * r))
        PAD     = round(14 * r)
        PAD2    = round(18 * r)
        PAD_V   = round(8  * r)

        state = {
            "save_dir":   save_dir,
            "recording":  False,
            "screen_rec": None,
            "audio_rec":  None,
            "overlay":    None,
        }

        tk.Label(popup, text="🎬 녹화 및 캡처",
                 font=("Segoe UI", F_TITLE, "bold"),
                 bg=self.BG, fg=self.TEXT).pack(pady=(PAD, 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(8*r), 0))

        # ── 저장 위치 ──────────────────────────────────────────────────────────
        dir_card = tk.Frame(popup, bg=self.BG2, padx=PAD2, pady=PAD_V)
        dir_card.pack(fill="x", padx=PAD, pady=(PAD_V, 0))
        tk.Label(dir_card, text="저장 위치",
                 font=("Consolas", F_MONO, "bold"),
                 bg=self.BG2, fg=self.TEXT_MID).pack(anchor="w")
        dir_row = tk.Frame(dir_card, bg=self.BG2)
        dir_row.pack(fill="x", pady=(round(4*r), 0))
        save_dir_var = tk.StringVar(value=save_dir)

        tk.Entry(dir_row, textvariable=save_dir_var,
                 font=("Consolas", max(7, F_MONO - 1)),
                 bg="white", fg="#111111",
                 disabledforeground="#111111",
                 readonlybackground="white",
                 insertbackground=self.ACCENT,
                 relief="flat", bd=4, state="readonly").pack(side="left", fill="x", expand=True)

        btn_kw = dict(font=("Consolas", F_BTN, "bold"), relief="flat", cursor="hand2",
                      padx=round(8*r), pady=round(3*r), activebackground=self.BORDER)

        def pick_dir():
            from tkinter import filedialog
            path = filedialog.askdirectory(
                title="저장 위치 선택",
                initialdir=state["save_dir"] or os.path.expanduser("~"))
            if path:
                state["save_dir"] = path
                save_dir_var.set(path)
                try:
                    import json
                    existing = {}
                    try:
                        with open(self.CFG_FILE, "r") as f:
                            existing = json.load(f)
                    except Exception:
                        pass
                    existing["record_save_dir"] = path
                    os.makedirs(self.APP_DIR, exist_ok=True)
                    with open(self.CFG_FILE, "w") as f:
                        json.dump(existing, f)
                except Exception:
                    pass
                self._record_save_dir = path
                s = "normal" if os.path.isdir(path) else "disabled"
                rec_btn.config(state=s)
                cap_btn.config(state=s)

        def open_dir():
            d = state["save_dir"]
            if d and os.path.isdir(d):
                os.startfile(d)

        tk.Button(dir_row, text="📂", bg=self.BG3, fg=self.TEXT,
                  command=pick_dir, **btn_kw).pack(side="left", padx=(4, 0))
        tk.Button(dir_row, text="🗂 열기", bg=self.BG3, fg=self.TEXT_MID,
                  command=open_dir, **btn_kw).pack(side="left", padx=(4, 0))

        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", padx=PAD, pady=(PAD_V, 0))

        # ── 탭 ────────────────────────────────────────────────────────────────
        tab_btn_f   = tk.Frame(popup, bg=self.BG)
        tab_btn_f.pack(fill="x", padx=PAD, pady=(PAD_V, 0))
        tab_content = tk.Frame(popup, bg=self.BG2)
        tab_content.pack(fill="both", expand=True, padx=PAD, pady=(0, PAD_V))

        tab_btns  = {}
        tab_pages = {}

        def switch_tab(name):
            for p in tab_pages.values():
                p.pack_forget()
            tab_pages[name].pack(fill="both", expand=True)
            for n, b in tab_btns.items():
                b.config(bg=self.BG3 if n == name else self.BG,
                         fg=self.ACCENT if n == name else self.TEXT_MID)

        for label, key in [("🔴 녹화", "record"), ("📷 캡처", "capture")]:
            b = tk.Button(tab_btn_f, text=label,
                          font=("Consolas", F_BTN, "bold"),
                          relief="flat", cursor="hand2",
                          padx=round(12*r), pady=round(5*r),
                          command=lambda k=key: switch_tab(k))
            b.pack(side="left", padx=(0, 4))
            tab_btns[key] = b

        record_page  = tk.Frame(tab_content, bg=self.BG2, padx=PAD2, pady=PAD_V)
        capture_page = tk.Frame(tab_content, bg=self.BG2, padx=PAD2, pady=PAD_V)
        tab_pages["record"]  = record_page
        tab_pages["capture"] = capture_page

        # ── [기능1] 구간 녹화 위젯 (녹화 중 비활성화) ─────────────────────────
        range_var      = tk.BooleanVar(value=False)
        start_time_var = tk.StringVar(value="00:00")
        end_time_var   = tk.StringVar(value="00:00")

        range_chk = tk.Checkbutton(record_page, text="구간 녹화", variable=range_var,
                       font=("Consolas", F_MONO),
                       bg=self.BG2, fg=self.TEXT, selectcolor=self.BG3,
                       activebackground=self.BG2, activeforeground=self.TEXT,
                       relief="flat", cursor="hand2")
        range_chk.pack(anchor="w", pady=(0, round(4*r)))

        range_row = tk.Frame(record_page, bg=self.BG2)
        range_row.pack(anchor="w", pady=(0, round(8*r)))
        vcmd = (popup.register(lambda s: all(c.isdigit() or c == ":" for c in s) or s == ""), "%P")

        tk.Label(range_row, text="시작", font=("Consolas", max(7, F_MONO-1)),
                 bg=self.BG2, fg=self.TEXT_MID).pack(side="left")
        start_entry = tk.Entry(range_row, textvariable=start_time_var, width=6,
                               font=("Consolas", F_MONO), bg=self.BG3, fg=self.TEXT,
                               insertbackground=self.ACCENT, relief="flat", bd=4,
                               validate="key", validatecommand=vcmd, state="disabled")
        start_entry.pack(side="left", padx=(4, 0))
        tk.Label(range_row, text="~", font=("Consolas", F_MONO),
                 bg=self.BG2, fg=self.TEXT_MID).pack(side="left", padx=4)
        tk.Label(range_row, text="종료", font=("Consolas", max(7, F_MONO-1)),
                 bg=self.BG2, fg=self.TEXT_MID).pack(side="left")
        end_entry = tk.Entry(range_row, textvariable=end_time_var, width=6,
                             font=("Consolas", F_MONO), bg=self.BG3, fg=self.TEXT,
                             insertbackground=self.ACCENT, relief="flat", bd=4,
                             validate="key", validatecommand=vcmd, state="disabled")
        end_entry.pack(side="left", padx=(4, 0))
        tk.Label(range_row, text="(MM:SS)", font=("Consolas", max(6, F_MONO-2)),
                 bg=self.BG2, fg=self.TEXT_DIM).pack(side="left", padx=(6, 0))

        def on_range_toggle():
            """구간 녹화 체크박스 토글 — 녹화 중에는 체크박스 자체가 disabled라 호출 안 됨."""
            on = range_var.get()
            st = "normal" if on else "disabled"
            start_entry.config(state=st)
            end_entry.config(state=st)

        range_chk.config(command=on_range_toggle)

        # 녹화 중 구간 UI를 한꺼번에 잠그는 헬퍼
        def _lock_range_ui():
            range_chk.config(state="disabled")
            start_entry.config(state="disabled")
            end_entry.config(state="disabled")

        def _unlock_range_ui():
            range_chk.config(state="normal")
            if range_var.get():
                start_entry.config(state="normal")
                end_entry.config(state="normal")

        tk.Frame(record_page, bg=self.BORDER, height=1).pack(fill="x", pady=(round(4*r), round(8*r)))

        btn_state = "normal" if (save_dir and os.path.isdir(save_dir)) else "disabled"
        rec_btn = tk.Button(record_page, text="⏺ 녹화 시작",
                            font=("Consolas", F_BTN, "bold"),
                            bg=self.BG3, fg=self.ACCENT2,
                            activebackground=self.BORDER,
                            relief="flat", cursor="hand2",
                            padx=round(12*r), pady=round(6*r),
                            state=btn_state)
        rec_btn.pack(fill="x")
        rec_status = tk.Label(record_page, text="",
                              font=("Consolas", max(7, F_MONO-1)),
                              bg=self.BG2, fg=self.TEXT_DIM)
        rec_status.pack(anchor="w", pady=(round(4*r), 0))

        # ── 캡처 탭 ───────────────────────────────────────────────────────────
        tk.Label(capture_page, text="팟플레이어 화면을 PNG로 캡처합니다.",
                 font=("Consolas", max(7, F_MONO-1)),
                 bg=self.BG2, fg=self.TEXT_DIM).pack(anchor="w", pady=(0, round(8*r)))
        tk.Frame(capture_page, bg=self.BORDER, height=1).pack(fill="x", pady=(0, round(8*r)))
        cap_btn = tk.Button(capture_page, text="📷 화면 캡처",
                            font=("Consolas", F_BTN, "bold"),
                            bg=self.BG3, fg=self.ACCENT,
                            activebackground=self.BORDER,
                            relief="flat", cursor="hand2",
                            padx=round(12*r), pady=round(6*r),
                            state=btn_state)
        cap_btn.pack(fill="x")
        cap_status = tk.Label(capture_page, text="",
                              font=("Consolas", max(7, F_MONO-1)),
                              bg=self.BG2, fg=self.TEXT_DIM)
        cap_status.pack(anchor="w", pady=(round(4*r), 0))

        # ── 유틸 함수 ──────────────────────────────────────────────────────────
        def parse_time(s):
            try:
                parts = s.strip().split(":")
                return int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else int(parts[0])
            except Exception:
                return 0

        def ensure_subdir(sub):
            path = os.path.join(state["save_dir"], sub)
            os.makedirs(path, exist_ok=True)
            return path

        def show_recording_overlay():
            from gui.record_backend import _show_overlay
            _show_overlay(self.root, "🔴 녹화중", duration_ms=99999999)

        def close_recording_overlay():
            ov = state.get("overlay")
            if ov:
                try: ov.destroy()
                except Exception: pass
                state["overlay"] = None

        # ── 녹화 정지 ──────────────────────────────────────────────────────────
        def stop_record():
            if not state["recording"]:
                return
            state["recording"] = False
            rec_btn.config(text="⏺ 녹화 시작", fg=self.ACCENT2)
            # [기능1] 정지 시 구간 UI 복원
            self.root.after(0, _unlock_range_ui)
            close_recording_overlay()

            def _save():
                import time as _t, threading as _th
                self.root.after(0, lambda: rec_status.config(text="💾 저장 중...", fg=self.TEXT_MID))
                try:
                    video_result = [None]; video_exc = [None]
                    audio_result = [None, None, None]; audio_exc = [None]

                    def _sv():
                        try: video_result[0] = state["screen_rec"].stop()
                        except Exception as e: video_exc[0] = e

                    def _sa():
                        try:
                            arr, sr, ch = state["audio_rec"].stop()
                            audio_result[0], audio_result[1], audio_result[2] = arr, sr, ch
                        except Exception as e: audio_exc[0] = e

                    vt = _th.Thread(target=_sv, daemon=True)
                    at = _th.Thread(target=_sa, daemon=True)
                    vt.start(); at.start()
                    vt.join(timeout=20); at.join(timeout=10)

                    if video_exc[0]: raise video_exc[0]
                    if audio_exc[0]: raise audio_exc[0]

                    tmp_video = video_result[0]
                    audio_arr, audio_sr, audio_ch = audio_result

                    out = state.get("out_path") or os.path.join(
                        ensure_subdir("Video"), f"record_{_t.strftime('%Y%m%d_%H%M%S')}.mp4")
                    self.root.after(0, lambda: rec_status.config(text="⏳ 오디오 병합 중...", fg=self.TEXT_MID))
                    from gui.record_backend import _save_mp4
                    _save_mp4(tmp_video, audio_arr, audio_sr, audio_ch, out)
                    if os.path.isfile(out) and os.path.getsize(out) > 1024:
                        self.root.after(0, lambda: rec_status.config(
                            text="✅ 저장 완료: Video/" + os.path.basename(out),
                            fg=self.ACCENT3))
                    else:
                        self.root.after(0, lambda: rec_status.config(
                            text="⚠ 저장 실패: 파일이 비어있습니다.", fg="#e0a03c"))
                except Exception as e:
                    import traceback, tempfile
                    tb = traceback.format_exc()
                    try:
                        with open(os.path.join(tempfile.gettempdir(), "autosinc_record_error.txt"),
                                  "w", encoding="utf-8") as lf:
                            lf.write(tb)
                    except: pass
                    self.root.after(0, lambda: rec_status.config(
                        text="⚠ 저장 실패: " + str(e)[:80], fg="#e0a03c"))

            threading.Thread(target=_save, daemon=True).start()

        # ── 녹화 시작 ──────────────────────────────────────────────────────────
        def start_record():
            from gui.record_backend import _CV2_OK, _AudioRecorder, _ScreenRecorder
            if not _CV2_OK:
                rec_status.config(text="⚠ opencv-python 필요", fg="#e0a03c")
                return

            use_range = range_var.get()
            start_sec = parse_time(start_time_var.get()) if use_range else None
            end_sec   = parse_time(end_time_var.get())   if use_range else None

            def _run():
                import time as _t
                # 구간 녹화: 시작 시각 대기
                if use_range and start_sec is not None:
                    from win32_utils import find_potplayer_hwnd, get_playback_info
                    self.root.after(0, lambda: rec_status.config(
                        text=f"⏳ {start_sec//60:02d}:{start_sec%60:02d} 대기 중..."))
                    while True:
                        hwnd = find_potplayer_hwnd()
                        if hwnd:
                            pos_ms, _ = get_playback_info(hwnd)
                            if pos_ms is not None and pos_ms // 1000 >= start_sec:
                                break
                        _t.sleep(0.2)

                import psutil
                pid = None
                for p in psutil.process_iter(["pid", "name"]):
                    if "potplayer" in p.info["name"].lower():
                        pid = p.info["pid"]; break

                state["audio_rec"]  = _AudioRecorder()
                state["screen_rec"] = _ScreenRecorder()
                _ts       = _t.strftime("%Y%m%d_%H%M%S")
                _out_path = os.path.join(ensure_subdir("Video"), f"record_{_ts}.mp4")
                state["out_path"] = _out_path

                try:
                    # [기능3] OBS 방식: ffmpeg 즉시 기동 후 오디오도 동시 시작
                    state["screen_rec"].start(fps=30)
                except Exception as e:
                    self.root.after(0, lambda: rec_status.config(
                        text="⚠ 화면 캡처 실패: " + str(e), fg="#e0a03c"))
                    return

                if pid:
                    state["audio_rec"].start(pid)

                state["recording"] = True
                self.root.after(0, lambda: rec_btn.config(text="⏹ 녹화 정지", fg=self.ACCENT3))
                self.root.after(0, lambda: rec_status.config(text="🔴 녹화 중...", fg=self.ACCENT2))
                # [기능1] 녹화 시작 시 구간 UI 잠금
                self.root.after(0, _lock_range_ui)
                # [기능2] 오버레이 표시
                self.root.after(0, show_recording_overlay)

                # 구간 녹화: 종료 시각 대기
                if use_range and end_sec is not None:
                    from win32_utils import find_potplayer_hwnd, get_playback_info
                    while state["recording"]:
                        hwnd = find_potplayer_hwnd()
                        if hwnd:
                            pos_ms, _ = get_playback_info(hwnd)
                            if pos_ms is not None and pos_ms // 1000 >= end_sec:
                                break
                        _t.sleep(0.2)
                    if state["recording"]:
                        self.root.after(0, stop_record)

            threading.Thread(target=_run, daemon=True).start()

        def toggle_record():
            if state["recording"]:
                stop_record()
            else:
                start_record()

        rec_btn.config(command=toggle_record)

        # ── 캡처 ───────────────────────────────────────────────────────────────
        def do_capture():
            import time as _t
            from gui.record_backend import _get_potplayer_rect, _show_overlay
            rect = _get_potplayer_rect()
            if rect is None:
                cap_status.config(text="⚠ 팟플레이어 창 없음", fg="#e0a03c")
                return
            try:
                import mss, numpy as np, cv2
                from PIL import Image
                px, py, pw, ph = rect
                with mss.mss() as sct:
                    shot = sct.grab({"left": px, "top": py, "width": pw, "height": ph})
                    img  = np.array(shot)
                    img  = cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)
                ts  = _t.strftime("%Y%m%d_%H%M%S")
                out = os.path.join(ensure_subdir("Screenshot"), f"capture_{ts}.png")
                Image.fromarray(img).save(out, "PNG")
                cap_status.config(text="✅ 저장: Screenshot/" + os.path.basename(out),
                                  fg=self.ACCENT3)
                _show_overlay(self.root, "📷 장면이 캡처되었습니다.", duration_ms=3000)
            except Exception as e:
                cap_status.config(text="⚠ 캡처 실패: " + str(e), fg="#e0a03c")

        cap_btn.config(command=do_capture)
        switch_tab("record")

        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", padx=PAD)

        def on_close():
            if state["recording"]:
                stop_record()
            close_recording_overlay()
            try:
                popup.destroy()
            except Exception:
                pass

        tk.Button(popup, text="닫기",
                  font=("Consolas", F_BTN, "bold"),
                  bg=self.BG3, fg=self.TEXT,
                  activebackground=self.BORDER,
                  relief="flat", cursor="hand2",
                  padx=round(16*r), pady=round(5*r),
                  command=on_close).pack(pady=PAD_V)
        popup.protocol("WM_DELETE_WINDOW", on_close)
