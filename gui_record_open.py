"""gui_record_open.py -- 녹화/캡처 팝업 열기 믹스인"""
import os, threading
import tkinter as tk

class LipSyncGUIRecordOpen:

    def _open_record_capture(self):
        from gui_record import RecordCapturePopup
        inst = getattr(self, "_record_popup_inst", None)
        if inst is not None and inst._popup is not None:
            try:
                if inst._popup.winfo_exists():
                    inst._popup.lift()
                    return
            except Exception:
                pass
        self._record_popup_inst = RecordCapturePopup(self)
        self._record_popup_inst.open()

    def _open_record_capture(self):
        save_dir = self._load_setting("record_save_dir", "")

        r     = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        pw    = round(340 * r)
        ph    = round(400 * r)

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
        }

        tk.Label(popup, text="🎬 녹화 및 캡처",
                 font=("Segoe UI", F_TITLE, "bold"),
                 bg=self.BG, fg=self.TEXT).pack(pady=(PAD, 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(8*r), 0))

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
                 bg=self.BG3, fg=self.TEXT,
                 insertbackground=self.ACCENT,
                 relief="flat", bd=4, state="readonly").pack(side="left", fill="x", expand=True)
        btn_kw = dict(font=("Consolas", F_BTN, "bold"), relief="flat", cursor="hand2",
                      padx=round(8*r), pady=round(3*r), activebackground=self.BORDER)

        def pick_dir():
            import os
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
                    import os as _os
                    _os.makedirs(self.APP_DIR, exist_ok=True)
                    with open(self.CFG_FILE, "w") as f:
                        json.dump(existing, f)
                except Exception:
                    pass
                rec_btn.config(state="normal" if path else "disabled")
                cap_btn.config(state="normal" if path else "disabled")

        def open_dir():
            import os
            d = state["save_dir"]
            if d and os.path.isdir(d):
                os.startfile(d)

        tk.Button(dir_row, text="📂", bg=self.BG3, fg=self.TEXT,
                  command=pick_dir, **btn_kw).pack(side="left", padx=(4, 0))
        tk.Button(dir_row, text="🗂 열기", bg=self.BG3, fg=self.TEXT_MID,
                  command=open_dir, **btn_kw).pack(side="left", padx=(4, 0))

        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", padx=PAD, pady=(PAD_V, 0))

        tab_btn_f = tk.Frame(popup, bg=self.BG)
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

        range_var       = tk.BooleanVar(value=False)
        start_time_var  = tk.StringVar(value="00:00")
        end_time_var    = tk.StringVar(value="00:00")

        tk.Checkbutton(record_page, text="구간 녹화", variable=range_var,
                       font=("Consolas", F_MONO),
                       bg=self.BG2, fg=self.TEXT, selectcolor=self.BG3,
                       activebackground=self.BG2, activeforeground=self.TEXT,
                       relief="flat", cursor="hand2",
                       command=lambda: (
                           start_entry.config(state="normal" if range_var.get() else "disabled"),
                           end_entry.config(state="normal" if range_var.get() else "disabled"),
                       )).pack(anchor="w", pady=(0, round(4*r)))

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

        tk.Frame(record_page, bg=self.BORDER, height=1).pack(fill="x", pady=(round(4*r), round(8*r)))

        btn_state = "normal" if (save_dir and __import__("os").path.isdir(save_dir)) else "disabled"
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

        def parse_time(s):
            try:
                parts = s.strip().split(":")
                return int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else int(parts[0])
            except Exception:
                return 0

        def ensure_subdir(sub):
            import os
            path = os.path.join(state["save_dir"], sub)
            os.makedirs(path, exist_ok=True)
            return path

        def stop_record():
            if not state["recording"]:
                return
            state["recording"] = False
            rec_btn.config(text="⏺ 녹화 시작", fg=self.ACCENT2)
            rec_status.config(text="💾 저장 중...", fg=self.TEXT_MID)
            from gui_record_backend import _show_overlay, _save_mp4
            _show_overlay(self.root, "✅ 녹화가 종료되었습니다.", duration_ms=3000)
            def _save():
                import os, time as _t
                try:
                    video_frames, fps, size = state["screen_rec"].stop()
                    audio_arr, audio_sr, audio_ch = state["audio_rec"].stop()
                    ts = _t.strftime("%Y%m%d_%H%M%S")
                    out = os.path.join(ensure_subdir("Video"), f"record_{ts}.mp4")
                    _save_mp4(video_frames, fps, size, audio_arr, audio_sr, audio_ch, out)
                    self.root.after(0, lambda: rec_status.config(
                        text="✅ 저장 완료: Video/" + os.path.basename(out), fg=self.ACCENT3))
                except Exception as e:
                    self.root.after(0, lambda: rec_status.config(
                        text="⚠ 저장 실패: " + str(e), fg="#e0a03c"))
            import threading as _th
            _th.Thread(target=_save, daemon=True).start()

        def start_record():
            from gui_record_backend import _CV2_OK, _PIL_OK, _AudioRecorder, _ScreenRecorder, _show_overlay
            if not _CV2_OK:
                rec_status.config(text="⚠ opencv-python 필요", fg="#e0a03c"); return
            if not _PIL_OK:
                rec_status.config(text="⚠ Pillow 필요", fg="#e0a03c"); return
            use_range = range_var.get()
            start_sec = parse_time(start_time_var.get()) if use_range else None
            end_sec   = parse_time(end_time_var.get())   if use_range else None
            def _run():
                import time as _t, psutil
                if use_range and start_sec is not None:
                    from win32_utils import find_potplayer_hwnd, get_playback_info
                    rec_status.config(text=f"⏳ {start_sec//60:02d}:{start_sec%60:02d} 대기 중...")
                    while True:
                        hwnd = find_potplayer_hwnd()
                        if hwnd:
                            pos_ms, _ = get_playback_info(hwnd)
                            if pos_ms is not None and pos_ms // 1000 >= start_sec:
                                break
                        _t.sleep(0.2)
                pid = None
                for p in psutil.process_iter(["pid", "name"]):
                    if "potplayer" in p.info["name"].lower():
                        pid = p.info["pid"]; break
                state["audio_rec"]  = _AudioRecorder()
                state["screen_rec"] = _ScreenRecorder()
                try:
                    state["screen_rec"].start(fps=30)
                except Exception as e:
                    rec_status.config(text="⚠ 화면 캡처 실패: " + str(e), fg="#e0a03c"); return
                if pid:
                    state["audio_rec"].start(pid)
                state["recording"] = True
                rec_btn.config(text="⏹ 녹화 정지", fg=self.ACCENT3)
                rec_status.config(text="🔴 녹화 중...", fg=self.ACCENT2)
                _show_overlay(self.root, "🔴 녹화중", duration_ms=99999999)
                if use_range and end_sec is not None:
                    import time as _t
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
            import threading as _th
            _th.Thread(target=_run, daemon=True).start()

        def toggle_record():
            if state["recording"]:
                stop_record()
            else:
                start_record()

        rec_btn.config(command=toggle_record)

        def do_capture():
            from gui_record_backend import _PIL_OK, _get_potplayer_rect, _show_overlay
            import os, time as _t
            if not _PIL_OK:
                cap_status.config(text="⚠ Pillow 필요", fg="#e0a03c"); return
            rect = _get_potplayer_rect()
            if rect is None:
                cap_status.config(text="⚠ 팟플레이어 창 없음", fg="#e0a03c"); return
            try:
                from PIL import ImageGrab
                px, py, pw, ph = rect
                img = ImageGrab.grab(bbox=(px, py, px+pw, py+ph))
                ts  = _t.strftime("%Y%m%d_%H%M%S")
                out = os.path.join(ensure_subdir("Screenshot"), f"capture_{ts}.png")
                img.save(out, "PNG")
                cap_status.config(text="✅ 저장: Screenshot/" + os.path.basename(out), fg=self.ACCENT3)
                _show_overlay(self.root, "📷 장면이 캡처되었습니다.", duration_ms=3000)
            except Exception as e:
                cap_status.config(text="⚠ 캡처 실패: " + str(e), fg="#e0a03c")

        cap_btn.config(command=do_capture)

        switch_tab("record")

        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", padx=PAD)

        def on_close():
            if state["recording"]:
                stop_record()
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
