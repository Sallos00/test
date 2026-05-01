"""
gui/record_open.py -- 녹화/캡처 탭 믹스인

[수정 내용]
  1. "자동 녹화" 체크박스 추가 (구간 녹화 체크박스 왼쪽에 배치)
  2. 자동 녹화 동작 로직 구현
       - 체크 ON + 녹화 시작 → 자동 녹화 모드 진입
       - PotPlayer 영상 재생 감지 → 영상 길이 읽기 → 즉시 녹화 시작
       - 종료 시간 도달 → 녹화 자동 종료 → 파일 저장
       - 저장 완료 후 영상 제목 변화 감지 → 변경 시 루프
  3. 자동 녹화 중 중복 실행 방지 (Lock 사용)
  4. 영상 길이 읽기 실패 시 예외 처리 및 재시도
  5. 모든 동작에 로그 출력 포함

기존 기능 완전 보존:
  - 구간 녹화 (start/end 시간 지정)
  - 녹화 중 UI 잠금 (기능1)
  - 오버레이 표시 (기능2)
  - OBS 방식 화면+오디오 동시 시작 (기능3)
  - 캡처 기능
  - 팝업으로 열기 / 메인창 탭 내장 모두 지원
"""
import os
import threading
import time as _time
import collections
import tkinter as tk
from mem_utils import run_gc


class LipSyncGUIRecordOpen:

    # ══════════════════════════════════════════════════════════════════════════
    # 공통 위젯 빌더
    # ══════════════════════════════════════════════════════════════════════════

    def _build_record_widgets(self, container, r, vcmd_widget=None):
        """저장위치·녹화·캡처 위젯을 container에 구성하고 (state, stop_record) 반환."""

        save_dir = (getattr(self, "_record_save_dir", None)
                    or self._load_setting("record_save_dir", ""))

        F_MONO = max(8,  round(9  * r))
        F_BTN  = max(8,  round(9  * r))
        PAD    = round(14 * r)
        PAD2   = round(18 * r)
        PAD_V  = round(8  * r)

        # ── 상태 딕셔너리 ─────────────────────────────────────────────────────
        state = {
            "save_dir":   save_dir,
            "recording":  False,
            "screen_rec": None,
            "audio_rec":  None,
            "overlay":    None,
            "out_path":   None,
            # [NEW] 자동 녹화 상태
            "auto_rec_active": False,
            "auto_rec_lock":   threading.Lock(),
        }

        # ── 로그 헬퍼 ─────────────────────────────────────────────────────────
        def _log(msg: str):
            if not hasattr(self, "_log_lines"):
                self._log_lines = collections.deque(maxlen=100)
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] {msg}")

        # ── 저장 위치 ──────────────────────────────────────────────────────────
        dir_card = tk.Frame(container, bg=self.BG2, padx=PAD2, pady=PAD_V)
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
                 relief="flat", bd=4, state="readonly").pack(
            side="left", fill="x", expand=True)

        btn_kw = dict(font=("Consolas", F_BTN, "bold"), relief="flat",
                      cursor="hand2", padx=round(8*r), pady=round(3*r),
                      activebackground=self.BORDER)

        # rec_btn / cap_btn 을 pick_dir 안에서 참조하므로 전방 선언
        _rec_btn_ref = [None]
        _cap_btn_ref = [None]

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
                s = "normal" if os.path.isdir(path) else "disabled"
                self._record_save_dir = path
                if _rec_btn_ref[0]:
                    _rec_btn_ref[0].config(state=s)
                if _cap_btn_ref[0]:
                    _cap_btn_ref[0].config(state=s)

        def open_dir():
            d = state["save_dir"]
            if d and os.path.isdir(d):
                os.startfile(d)

        tk.Button(dir_row, text="📂", bg=self.BG3, fg=self.TEXT,
                  command=pick_dir, **btn_kw).pack(side="left", padx=(4, 0))
        tk.Button(dir_row, text="🗂 열기", bg=self.BG3, fg=self.TEXT_MID,
                  command=open_dir, **btn_kw).pack(side="left", padx=(4, 0))

        tk.Frame(container, bg=self.BORDER, height=1).pack(
            fill="x", padx=PAD, pady=(PAD_V, 0))

        # ── 내부 탭 (녹화 / 캡처) ──────────────────────────────────────────────
        tab_btn_f   = tk.Frame(container, bg=self.BG)
        tab_btn_f.pack(fill="x", padx=PAD, pady=(PAD_V, 0))
        tab_content = tk.Frame(container, bg=self.BG2)
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

        # ── 구간 녹화 + [NEW] 자동 녹화 체크박스 행 ───────────────────────────
        reg_widget = vcmd_widget or container
        vcmd = (reg_widget.register(
            lambda s: all(c.isdigit() or c == ":" for c in s) or s == ""),
                "%P")

        chk_row = tk.Frame(record_page, bg=self.BG2)
        chk_row.pack(anchor="w", pady=(0, round(4*r)))

        # [NEW] 자동 녹화 체크박스 (구간 녹화 왼쪽)
        auto_rec_var = tk.BooleanVar(value=False)
        auto_rec_chk = tk.Checkbutton(
            chk_row, text="자동 녹화", variable=auto_rec_var,
            font=("Consolas", F_MONO),
            bg=self.BG2, fg=self.ACCENT3,
            selectcolor=self.BG3,
            activebackground=self.BG2, activeforeground=self.ACCENT3,
            relief="flat", cursor="hand2")
        auto_rec_chk.pack(side="left", padx=(0, round(12*r)))

        # 구간 녹화 체크박스
        range_var      = tk.BooleanVar(value=False)
        start_time_var = tk.StringVar(value="00:00")
        end_time_var   = tk.StringVar(value="00:00")

        range_chk = tk.Checkbutton(
            chk_row, text="구간 녹화", variable=range_var,
            font=("Consolas", F_MONO),
            bg=self.BG2, fg=self.TEXT,
            selectcolor=self.BG3,
            activebackground=self.BG2, activeforeground=self.TEXT,
            relief="flat", cursor="hand2")
        range_chk.pack(side="left")

        range_row = tk.Frame(record_page, bg=self.BG2)
        range_row.pack(anchor="w", pady=(0, round(8*r)))

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
            on = range_var.get()
            st = "normal" if on else "disabled"
            start_entry.config(state=st)
            end_entry.config(state=st)

        range_chk.config(command=on_range_toggle)

        def _lock_range_ui():
            range_chk.config(state="disabled")
            auto_rec_chk.config(state="disabled")
            start_entry.config(state="disabled")
            end_entry.config(state="disabled")

        def _unlock_range_ui():
            range_chk.config(state="normal")
            auto_rec_chk.config(state="normal")
            if range_var.get():
                start_entry.config(state="normal")
                end_entry.config(state="normal")

        tk.Frame(record_page, bg=self.BORDER, height=1).pack(
            fill="x", pady=(round(4*r), round(8*r)))

        btn_state = "normal" if (save_dir and os.path.isdir(save_dir)) else "disabled"
        rec_btn = tk.Button(record_page, text="⏺ 녹화 시작",
                            font=("Consolas", F_BTN, "bold"),
                            bg=self.BG3, fg=self.ACCENT2,
                            activebackground=self.BORDER,
                            relief="flat", cursor="hand2",
                            padx=round(12*r), pady=round(6*r),
                            state=btn_state)
        rec_btn.pack(fill="x")
        _rec_btn_ref[0] = rec_btn

        rec_status = tk.Label(record_page, text="",
                              font=("Consolas", max(7, F_MONO-1)),
                              bg=self.BG2, fg=self.TEXT_DIM)
        rec_status.pack(anchor="w", pady=(round(4*r), 0))

        # ── 캡처 탭 ────────────────────────────────────────────────────────────
        tk.Label(capture_page, text="팟플레이어 화면을 PNG로 캡처합니다.",
                 font=("Consolas", max(7, F_MONO-1)),
                 bg=self.BG2, fg=self.TEXT_DIM).pack(anchor="w",
                                                      pady=(0, round(8*r)))
        tk.Frame(capture_page, bg=self.BORDER, height=1).pack(
            fill="x", pady=(0, round(8*r)))
        cap_btn = tk.Button(capture_page, text="📷 화면 캡처",
                            font=("Consolas", F_BTN, "bold"),
                            bg=self.BG3, fg=self.ACCENT,
                            activebackground=self.BORDER,
                            relief="flat", cursor="hand2",
                            padx=round(12*r), pady=round(6*r),
                            state=btn_state)
        cap_btn.pack(fill="x")
        _cap_btn_ref[0] = cap_btn

        cap_status = tk.Label(capture_page, text="",
                              font=("Consolas", max(7, F_MONO-1)),
                              bg=self.BG2, fg=self.TEXT_DIM)
        cap_status.pack(anchor="w", pady=(round(4*r), 0))

        # ── 유틸 함수 ──────────────────────────────────────────────────────────
        def parse_time(s):
            try:
                parts = s.strip().split(":")
                if len(parts) == 2:
                    return int(parts[0]) * 60 + int(parts[1])
                return int(parts[0])
            except Exception:
                return 0

        def ensure_subdir(sub):
            d = os.path.join(state["save_dir"], sub)
            os.makedirs(d, exist_ok=True)
            return d

        def show_recording_overlay():
            from gui.record_backend import _show_overlay
            close_recording_overlay()
            state["overlay"] = _show_overlay(self.root, "🔴 녹화 중", duration_ms=0)

        def close_recording_overlay():
            ov = state.get("overlay")
            if ov:
                try: ov.destroy()
                except Exception: pass
                state["overlay"] = None

        # ══════════════════════════════════════════════════════════════════════
        # 녹화 정지 (기존 코드 보존)
        # ══════════════════════════════════════════════════════════════════════
        def stop_record():
            if not state["recording"]:
                return
            state["recording"] = False
            self._recording = False
            for _q in (getattr(self, "cmd_queue", None),
                       getattr(self, "_om_cmd_queue", None)):
                if _q is not None:
                    try: _q.put_nowait("recording_stop")
                    except Exception: pass
            rec_btn.config(text="⏺ 녹화 시작", fg=self.ACCENT2)
            self.root.after(0, _unlock_range_ui)
            close_recording_overlay()
            _log("⏹ 녹화 정지")

            def _save():
                import threading as _th
                self.root.after(0, lambda: rec_status.config(
                    text="💾 저장 중...", fg=self.TEXT_MID))
                out_path_saved = [None]
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

                    _vid_t = getattr(state["screen_rec"], "_first_frame_qpc_sec", 0.0)
                    _aud_t = getattr(state["audio_rec"],  "_first_audio_qpc_sec", 0.0)
                    audio_offset_sec = (_aud_t - _vid_t) if (_vid_t and _aud_t) else 0.0

                    import time as _t2
                    out = state.get("out_path") or os.path.join(
                        ensure_subdir("Video"),
                        f"record_{_t2.strftime('%Y%m%d_%H%M%S')}.mp4")
                    out_path_saved[0] = out
                    self.root.after(0, lambda: rec_status.config(
                        text="⏳ 오디오 병합 중...", fg=self.TEXT_MID))
                    from gui.record_backend import _save_mp4
                    _save_mp4(tmp_video, audio_arr, audio_sr, audio_ch,
                              out, audio_offset_sec)
                    if os.path.isfile(out) and os.path.getsize(out) > 1024:
                        _log(f"✅ 저장 완료: {os.path.basename(out)}")
                        self.root.after(0, lambda: rec_status.config(
                            text="✅ 저장 완료: Video/" + os.path.basename(out),
                            fg=self.ACCENT3))
                    else:
                        _log("⚠ 저장 실패: 파일이 비어있습니다.")
                        self.root.after(0, lambda: rec_status.config(
                            text="⚠ 저장 실패: 파일이 비어있습니다.",
                            fg="#e0a03c"))
                        out_path_saved[0] = None

                except Exception as e:
                    import traceback, tempfile
                    tb = traceback.format_exc()
                    try:
                        with open(os.path.join(tempfile.gettempdir(),
                                               "autosinc_record_error.txt"),
                                  "w", encoding="utf-8") as lf:
                            lf.write(tb)
                    except Exception:
                        pass
                    _log(f"❌ 저장 실패: {e}")
                    self.root.after(0, lambda: rec_status.config(
                        text="⚠ 저장 실패: " + str(e)[:80], fg="#e0a03c"))
                    out_path_saved[0] = None

                finally:
                    try: del audio_arr
                    except Exception: pass
                    try: del audio_result
                    except Exception: pass
                    try: del video_result
                    except Exception: pass
                    try:
                        state["screen_rec"] = None
                        state["audio_rec"]  = None
                    except Exception:
                        pass
                    run_gc()

                # [NEW] 자동 녹화 루프: 저장 완료 후 제목 변화 감지 → 재시작
                if state.get("auto_rec_active") and out_path_saved[0]:
                    _auto_rec_wait_and_loop()

            threading.Thread(target=_save, daemon=True).start()

        # ══════════════════════════════════════════════════════════════════════
        # [NEW] 자동 녹화 로직
        # ══════════════════════════════════════════════════════════════════════

        def _get_video_duration_with_retry(max_wait_sec=30) -> int:
            """영상 길이(초)를 가져온다. 실패 시 최대 max_wait_sec 동안 재시도."""
            from win32_utils import find_potplayer_hwnd, get_playback_info
            deadline = _time.time() + max_wait_sec
            while _time.time() < deadline:
                hwnd = find_potplayer_hwnd()
                if hwnd:
                    pos_ms, dur_ms = get_playback_info(hwnd)
                    if dur_ms and dur_ms > 0:
                        _log(f"⏱ 영상 길이: {dur_ms // 1000}초")
                        return dur_ms // 1000
                _time.sleep(0.5)
            _log("⚠ 영상 길이 가져오기 실패 (타임아웃)")
            return 0

        def _wait_for_video_play(max_wait_sec=60) -> bool:
            """PotPlayer 에서 영상이 재생될 때까지 대기. 성공 시 True."""
            from win32_utils import find_potplayer_hwnd, is_potplayer_playing
            _log("⏳ 영상 재생 감지 대기 중...")
            deadline = _time.time() + max_wait_sec
            while _time.time() < deadline:
                if not state.get("auto_rec_active"):
                    return False
                hwnd = find_potplayer_hwnd()
                if hwnd and is_potplayer_playing(hwnd):
                    _log("🎬 영상 재생 감지됨")
                    return True
                _time.sleep(0.5)
            _log("⚠ 영상 재생 감지 타임아웃")
            return False

        def _get_current_potplayer_title() -> str:
            """현재 PotPlayer 창 제목에서 파일명을 추출한다."""
            import ctypes
            from win32_utils import find_potplayer_hwnd
            from gui.ui_logic import _extract_potplayer_title
            hwnd = find_potplayer_hwnd()
            if not hwnd:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(512)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
                return _extract_potplayer_title(buf.value)
            except Exception:
                return ""

        def _auto_rec_wait_and_loop():
            """저장 완료 후 영상 제목 변화를 감지해 자동 녹화를 재시작한다.
            별도 데몬 스레드에서 실행된다.
            """
            if not state.get("auto_rec_active"):
                return

            _log("🔄 다음 영상 대기 중 (제목 변화 감지)...")
            prev_title = _get_current_potplayer_title()

            deadline = _time.time() + 300   # 최대 5분 대기
            while _time.time() < deadline:
                if not state.get("auto_rec_active"):
                    _log("🛑 자동 녹화 루프 종료 (사용자 중단)")
                    return
                cur_title = _get_current_potplayer_title()
                if cur_title and cur_title != prev_title:
                    _log(f"🔍 제목 변경 감지: {prev_title!r} → {cur_title!r}")
                    _time.sleep(1.0)   # 영상 안정화 대기
                    # 메인 스레드에서 자동 녹화 재시작
                    self.root.after(0, _auto_rec_start_one)
                    return
                _time.sleep(1.0)

            _log("⚠ 제목 변화 없음 (5분 초과) → 자동 녹화 대기 종료")
            state["auto_rec_active"] = False
            self.root.after(0, lambda: rec_status.config(
                text="⏸ 자동 녹화 대기 종료 (제목 변화 없음)", fg="#e0a03c"))

        def _auto_rec_start_one():
            """자동 녹화 1회 실행 (스레드 진입점)."""
            if not state.get("auto_rec_active"):
                return
            if state["recording"]:
                _log("⚠ 이미 녹화 중 — 자동 녹화 루프 건너뜀")
                return

            def _run_auto():
                from win32_utils import find_potplayer_hwnd, get_playback_info
                from gui.record_backend import _CV2_OK, _AudioRecorder, _ScreenRecorder
                from gui._record_impl import check_ffmpeg, download_ffmpeg
                import psutil

                if not _CV2_OK:
                    self.root.after(0, lambda: rec_status.config(
                        text="⚠ opencv-python 필요", fg="#e0a03c"))
                    state["auto_rec_active"] = False
                    return

                # ── [NEW] ffmpeg 확인 / 자동 다운로드 ───────────────────────────
                try:
                    _ff = check_ffmpeg()
                    if not _ff:
                        self.root.after(0, lambda: rec_status.config(
                            text="⬇ ffmpeg 다운로드 중...", fg=self.TEXT_MID))
                        _log("⬇ ffmpeg 없음 → 자동 다운로드 시작 (자동 녹화)")
                        _ff = download_ffmpeg()
                        _log(f"✅ ffmpeg 다운로드 완료: {_ff}")
                    else:
                        _log(f"✅ ffmpeg 확인: {_ff}")
                except Exception as _fe:
                    self.root.after(0, lambda e=_fe: rec_status.config(
                        text=f"⚠ ffmpeg 오류: {e}", fg="#e0a03c"))
                    _log(f"❌ ffmpeg 준비 실패: {_fe}")
                    state["auto_rec_active"] = False
                    return
                # ────────────────────────────────────────────────────────────────

                # 1) 영상 재생 감지
                self.root.after(0, lambda: rec_status.config(
                    text="⏳ 영상 재생 감지 중...", fg=self.TEXT_MID))
                if not _wait_for_video_play(max_wait_sec=60):
                    state["auto_rec_active"] = False
                    self.root.after(0, lambda: rec_status.config(
                        text="⚠ 영상 재생 감지 실패", fg="#e0a03c"))
                    return

                # 2) 영상 길이 가져오기
                self.root.after(0, lambda: rec_status.config(
                    text="⏳ 영상 길이 확인 중...", fg=self.TEXT_MID))
                dur_sec = _get_video_duration_with_retry(max_wait_sec=30)
                if dur_sec <= 0:
                    state["auto_rec_active"] = False
                    self.root.after(0, lambda: rec_status.config(
                        text="⚠ 영상 길이 가져오기 실패 → 자동 녹화 취소",
                        fg="#e0a03c"))
                    _log("❌ 자동 녹화 취소: 영상 길이 없음")
                    return

                end_sec_auto = dur_sec - 1   # 끝 1초 여유

                # 3) PotPlayer PID 탐색
                pid = None
                try:
                    for p in psutil.process_iter(["pid", "name"]):
                        if "potplayer" in p.info["name"].lower():
                            pid = p.info["pid"]
                            break
                except Exception:
                    pass

                # 4) 녹화 시작 (중복 실행 방지)
                with state["auto_rec_lock"]:
                    if state["recording"]:
                        _log("⚠ 이미 녹화 중 → 자동 녹화 스킵")
                        return

                    state["audio_rec"]  = _AudioRecorder()
                    state["screen_rec"] = _ScreenRecorder()
                    _ts       = _time.strftime("%Y%m%d_%H%M%S")
                    _out_path = os.path.join(ensure_subdir("Video"),
                                             f"auto_record_{_ts}.mp4")
                    state["out_path"] = _out_path

                    try:
                        state["screen_rec"].start(fps=30, out_path=_out_path)
                    except Exception as e:
                        self.root.after(0, lambda: rec_status.config(
                            text="⚠ 화면 캡처 실패: " + str(e), fg="#e0a03c"))
                        _log(f"❌ 화면 캡처 실패: {e}")
                        state["auto_rec_active"] = False
                        return

                    if pid:
                        state["audio_rec"].start(pid)

                    state["recording"] = True
                    self._recording = True

                for _q in (getattr(self, "cmd_queue", None),
                           getattr(self, "_om_cmd_queue", None)):
                    if _q is not None:
                        try: _q.put_nowait("recording_start")
                        except Exception: pass

                self.root.after(0, lambda: rec_btn.config(
                    text="⏹ 녹화 정지", fg=self.ACCENT3))
                self.root.after(0, lambda: rec_status.config(
                    text=f"🔴 자동 녹화 중... (총 {dur_sec}초)",
                    fg=self.ACCENT2))
                self.root.after(0, _lock_range_ui)
                self.root.after(0, show_recording_overlay)
                _log(f"🔴 자동 녹화 시작 (예상 종료: {end_sec_auto}초)")

                # 5) 종료 시간 대기
                while state["recording"]:
                    hwnd = find_potplayer_hwnd()
                    if hwnd:
                        pos_ms, _ = get_playback_info(hwnd)
                        if pos_ms is not None and pos_ms // 1000 >= end_sec_auto:
                            break
                    _time.sleep(0.3)

                # 6) 녹화 자동 종료 (stop_record → _save → _auto_rec_wait_and_loop)
                if state["recording"]:
                    self.root.after(0, stop_record)

            threading.Thread(target=_run_auto, daemon=True,
                             name="auto-rec-worker").start()

        # ══════════════════════════════════════════════════════════════════════
        # 녹화 시작 (기존 코드 보존 + 자동 녹화 모드 분기)
        # ══════════════════════════════════════════════════════════════════════
        def start_record():
            from gui.record_backend import _CV2_OK, _AudioRecorder, _ScreenRecorder
            if not _CV2_OK:
                rec_status.config(text="⚠ opencv-python 필요", fg="#e0a03c")
                return

            # [NEW] 자동 녹화 모드 분기
            if auto_rec_var.get():
                if state["recording"]:
                    _log("⚠ 자동 녹화 시작 불가: 이미 녹화 중")
                    return
                _log("🤖 자동 녹화 모드 진입")
                state["auto_rec_active"] = True
                rec_status.config(text="🤖 자동 녹화 모드 시작...", fg=self.ACCENT3)
                _auto_rec_start_one()
                return

            # 기존 수동 녹화 로직
            state["auto_rec_active"] = False
            use_range = range_var.get()
            start_sec = parse_time(start_time_var.get()) if use_range else None
            end_sec   = parse_time(end_time_var.get())   if use_range else None

            def _run():
                # ── [NEW] ffmpeg 확인 / 자동 다운로드 ───────────────────────────
                from gui._record_impl import check_ffmpeg, download_ffmpeg
                try:
                    _ff = check_ffmpeg()
                    if not _ff:
                        self.root.after(0, lambda: rec_status.config(
                            text="⬇ ffmpeg 다운로드 중...", fg=self.TEXT_MID))
                        _log("⬇ ffmpeg 없음 → 자동 다운로드 시작")
                        _ff = download_ffmpeg()
                        _log(f"✅ ffmpeg 다운로드 완료: {_ff}")
                    else:
                        _log(f"✅ ffmpeg 확인: {_ff}")
                except Exception as _fe:
                    self.root.after(0, lambda e=_fe: rec_status.config(
                        text=f"⚠ ffmpeg 오류: {e}", fg="#e0a03c"))
                    _log(f"❌ ffmpeg 준비 실패: {_fe}")
                    return
                # ────────────────────────────────────────────────────────────────

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
                        _time.sleep(0.2)

                import psutil
                pid = None
                for p in psutil.process_iter(["pid", "name"]):
                    if "potplayer" in p.info["name"].lower():
                        pid = p.info["pid"]
                        break

                state["audio_rec"]  = _AudioRecorder()
                state["screen_rec"] = _ScreenRecorder()
                _ts       = _time.strftime("%Y%m%d_%H%M%S")
                _out_path = os.path.join(ensure_subdir("Video"),
                                         f"record_{_ts}.mp4")
                state["out_path"] = _out_path

                try:
                    state["screen_rec"].start(fps=30, out_path=_out_path)
                except Exception as e:
                    self.root.after(0, lambda: rec_status.config(
                        text="⚠ 화면 캡처 실패: " + str(e), fg="#e0a03c"))
                    _log(f"❌ 화면 캡처 실패: {e}")
                    return

                if pid:
                    state["audio_rec"].start(pid)

                state["recording"] = True
                self._recording = True
                for _q in (getattr(self, "cmd_queue", None),
                           getattr(self, "_om_cmd_queue", None)):
                    if _q is not None:
                        try: _q.put_nowait("recording_start")
                        except Exception: pass

                self.root.after(0, lambda: rec_btn.config(
                    text="⏹ 녹화 정지", fg=self.ACCENT3))
                self.root.after(0, lambda: rec_status.config(
                    text="🔴 녹화 중...", fg=self.ACCENT2))
                self.root.after(0, _lock_range_ui)
                self.root.after(0, show_recording_overlay)
                _log("🔴 녹화 시작")

                if use_range and end_sec is not None:
                    from win32_utils import find_potplayer_hwnd, get_playback_info
                    while state["recording"]:
                        hwnd = find_potplayer_hwnd()
                        if hwnd:
                            pos_ms, _ = get_playback_info(hwnd)
                            if pos_ms is not None and pos_ms // 1000 >= end_sec:
                                break
                        _time.sleep(0.2)
                    if state["recording"]:
                        self.root.after(0, stop_record)

            threading.Thread(target=_run, daemon=True).start()

        def toggle_record():
            if state["recording"]:
                # 자동 녹화 중단 플래그
                state["auto_rec_active"] = False
                stop_record()
            else:
                start_record()

        rec_btn.config(command=toggle_record)

        # ── 캡처 (기존 코드 완전 보존) ─────────────────────────────────────────
        def do_capture():
            import ctypes
            import ctypes.wintypes as wt
            from gui.record_backend import _get_potplayer_video_hwnd, _show_overlay
            from win32_utils import find_potplayer_hwnd
            hwnd = find_potplayer_hwnd()
            if not hwnd:
                cap_status.config(text="⚠ 팟플레이어 창 없음", fg="#e0a03c")
                return
            try:
                import numpy as np
                import cv2
                from PIL import Image

                video_hwnd = _get_potplayer_video_hwnd(hwnd)
                target = video_hwnd if video_hwnd else hwnd

                gdi32  = ctypes.windll.gdi32
                user32 = ctypes.windll.user32

                rc = wt.RECT()
                user32.GetClientRect(target, ctypes.byref(rc))
                cw = rc.right - rc.left
                ch = rc.bottom - rc.top
                if cw <= 0 or ch <= 0:
                    cap_status.config(text="⚠ 창 크기 오류", fg="#e0a03c")
                    return

                class BITMAPINFOHEADER(ctypes.Structure):
                    _fields_ = [
                        ("biSize",          ctypes.c_uint32),
                        ("biWidth",         ctypes.c_int32),
                        ("biHeight",        ctypes.c_int32),
                        ("biPlanes",        ctypes.c_uint16),
                        ("biBitCount",      ctypes.c_uint16),
                        ("biCompression",   ctypes.c_uint32),
                        ("biSizeImage",     ctypes.c_uint32),
                        ("biXPelsPerMeter", ctypes.c_int32),
                        ("biYPelsPerMeter", ctypes.c_int32),
                        ("biClrUsed",       ctypes.c_uint32),
                        ("biClrImportant",  ctypes.c_uint32),
                    ]

                class BITMAPINFO(ctypes.Structure):
                    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                                ("bmiColors", ctypes.c_uint32 * 3)]

                def _make_dib_section(w, h):
                    bmi_ = BITMAPINFO()
                    bmi_.bmiHeader.biSize        = ctypes.sizeof(BITMAPINFOHEADER)
                    bmi_.bmiHeader.biWidth       = w
                    bmi_.bmiHeader.biHeight      = -h
                    bmi_.bmiHeader.biPlanes      = 1
                    bmi_.bmiHeader.biBitCount    = 32
                    bmi_.bmiHeader.biCompression = 0
                    hdc_s = user32.GetDC(None)
                    hdc_m = gdi32.CreateCompatibleDC(hdc_s)
                    pb    = ctypes.c_void_p()
                    hb    = gdi32.CreateDIBSection(hdc_m, ctypes.byref(bmi_),
                                                   0, ctypes.byref(pb), None, 0)
                    if not hb or not pb.value:
                        gdi32.DeleteDC(hdc_m)
                        user32.ReleaseDC(None, hdc_s)
                        return None
                    ob = gdi32.SelectObject(hdc_m, hb)
                    return hdc_s, hdc_m, hb, pb, ob

                def _free_dib(hdc_s, hdc_m, hb, ob):
                    gdi32.SelectObject(hdc_m, ob)
                    gdi32.DeleteObject(hb)
                    gdi32.DeleteDC(hdc_m)
                    user32.ReleaseDC(None, hdc_s)

                def _read_pixels(pb, w, h):
                    raw = (ctypes.c_uint8 * (w * h * 4)).from_address(pb.value)
                    return np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4).copy()

                def _is_black(a):
                    return bool(a[..., :3].mean() < 1.0)

                PW_RENDERFULLCONTENT = 0x00000002
                arr = None

                for pw_flag in (PW_RENDERFULLCONTENT, 0):
                    dib = _make_dib_section(cw, ch)
                    if dib is None:
                        continue
                    hdc_s, hdc_m, hb, pb, ob = dib
                    try:
                        user32.PrintWindow(target, hdc_m, pw_flag)
                        candidate = _read_pixels(pb, cw, ch)
                        if not _is_black(candidate):
                            arr = candidate
                    finally:
                        _free_dib(hdc_s, hdc_m, hb, ob)
                    if arr is not None:
                        break

                if arr is None:
                    dib = _make_dib_section(cw, ch)
                    if dib is not None:
                        hdc_s, hdc_m, hb, pb, ob = dib
                        try:
                            hdc_win = user32.GetWindowDC(target)
                            if hdc_win:
                                gdi32.BitBlt(hdc_m, 0, 0, cw, ch,
                                             hdc_win, 0, 0, 0x00CC0020)
                                user32.ReleaseDC(target, hdc_win)
                            candidate = _read_pixels(pb, cw, ch)
                            if not _is_black(candidate):
                                arr = candidate
                        finally:
                            _free_dib(hdc_s, hdc_m, hb, ob)

                if arr is None:
                    cap_status.config(
                        text="⚠ 캡처 실패: 검은 화면 (팟플레이어가 가려져 있거나 GPU 렌더러 문제)",
                        fg="#e0a03c")
                    return

                img = cv2.cvtColor(arr, cv2.COLOR_BGRA2RGB)
                del arr; arr = None
                ts  = _time.strftime("%Y%m%d_%H%M%S")
                out = os.path.join(ensure_subdir("Screenshot"),
                                   f"capture_{ts}.png")
                Image.fromarray(img).save(out, "PNG")
                del img; img = None
                run_gc()
                cap_status.config(
                    text="✅ 저장: Screenshot/" + os.path.basename(out),
                    fg=self.ACCENT3)
                _show_overlay(self.root, "📷 장면이 캡처되었습니다.", duration_ms=3000)
                _log(f"📷 캡처 저장: {os.path.basename(out)}")

            except Exception as e:
                cap_status.config(text="⚠ 캡처 실패: " + str(e), fg="#e0a03c")
                _log(f"❌ 캡처 실패: {e}")

        cap_btn.config(command=do_capture)
        switch_tab("record")
        return state, stop_record

    # ══════════════════════════════════════════════════════════════════════════
    # 팝업으로 열기 (기어 메뉴 등 외부 호출용) — 기존 코드 보존
    # ══════════════════════════════════════════════════════════════════════════
    def _open_record_capture(self):
        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        pw = round(340 * r)
        ph = round(400 * r)

        popup = tk.Toplevel(self.root)
        popup.title("녹화 및 캡처")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        popup.withdraw()

        F_TITLE = max(9, round(11 * r))
        F_BTN   = max(8, round(9  * r))
        PAD     = round(14 * r)
        PAD_V   = round(8  * r)

        tk.Label(popup, text="🎬 녹화 및 캡처",
                 font=("Segoe UI", F_TITLE, "bold"),
                 bg=self.BG, fg=self.TEXT).pack(pady=(PAD, 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(
            fill="x", pady=(round(8*r), 0))

        state, stop_record = self._build_record_widgets(
            popup, r, vcmd_widget=popup)

        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", padx=PAD)

        def on_close():
            if state["recording"]:
                state["auto_rec_active"] = False
                stop_record()
            ov = state.get("overlay")
            if ov:
                try: ov.destroy()
                except Exception: pass
            try: popup.destroy()
            except Exception: pass

        tk.Button(popup, text="닫기",
                  font=("Consolas", F_BTN, "bold"),
                  bg=self.BG3, fg=self.TEXT,
                  activebackground=self.BORDER,
                  relief="flat", cursor="hand2",
                  padx=round(16*r), pady=round(5*r),
                  command=on_close).pack(pady=PAD_V)
        popup.protocol("WM_DELETE_WINDOW", on_close)
        popup.grab_set()
        self._place_popup(popup, pw, ph)

    # ── 메인창 탭에 직접 내장 ─────────────────────────────────────────────────
    def _build_record_tab(self, parent, r, P, P2):
        """메인창 '녹화/캡처' 탭 프레임에 위젯 구성."""
        self._build_record_widgets(parent, r, vcmd_widget=None)
