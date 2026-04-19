"""
gui/run_refresh.py -- 100ms 주기 UI 갱신 Mixin
_refresh
"""
import time
import threading
import collections

from win32_utils import find_potplayer_hwnd, get_playback_info


class RefreshMixin:

    # ── 100ms 주기 UI 갱신 ────────────────────────────────────────────────────
    def _refresh(self):
        if self._closing:
            return

        # 재생 위치/길이 갱신 — 500ms 간격으로 throttle (FindWindowW 비용 절감)
        _now = time.time()
        if _now - getattr(self, '_hwnd_refresh_t', 0) >= 0.5:
            self._hwnd_refresh_t = _now
            hwnd = find_potplayer_hwnd()
            self._cached_hwnd = hwnd
            if hwnd:
                pos, dur = get_playback_info(hwnd)
                pv = pos if pos is not None else -1
                dv = dur if dur is not None else -1
                if self._running and hasattr(self, "_shared_pos"):
                    self._shared_pos[0] = pv
                    self._shared_dur[0] = dv
                if getattr(self, "_oped_monitor_running", False) and hasattr(self, "_om_shared_pos"):
                    self._om_shared_pos[0] = pv
                    self._om_shared_dur[0] = dv

            # ── [Bug 3 수정] 싱크 작동 중 팟플레이어 종료 감지 → 자동 대기 상태 전환 ──
            # 팟플레이어가 사라졌을 때 proc_analyzer가 STATUS_NO_POT을 보내더라도
            # GUI는 _running=True 상태를 유지해 "싱크 작동 중"으로 보임.
            # hwnd 캐시를 직접 확인해 즉시 감지하고 백그라운드 스레드에서
            # _stop_processes() 후 _wait_for_potplayer()로 전환한다.
            if (self._running and not hwnd
                    and not getattr(self, "_pot_exit_handling", False)):
                self._pot_exit_handling = True
                def _handle_pot_exit():
                    self._stop_processes()
                    def _update_ui():
                        if self._closing:
                            self._pot_exit_handling = False
                            return
                        self._start_btn.config(
                            text="⏳ 대기 중...",
                            bg=self.BG3, fg=self.TEXT_DIM,
                            activebackground=self.BORDER,
                            state="disabled"
                        )
                        self._proc_lbl.config(
                            text="팟플레이어 실행을 기다리는 중...", fg=self.ACCENT
                        )
                        self._badge.config(text="  대기 중  ", fg=self.TEXT, bg=self.BG3)
                        self._start_oped_monitor()
                        threading.Thread(
                            target=self._wait_for_potplayer, daemon=True).start()
                        self._pot_exit_handling = False
                    self.root.after(0, _update_ui)
                threading.Thread(
                    target=_handle_pot_exit, daemon=True,
                    name="pot-exit-handler").start()

        # ── Working Set 주기적 트림 ──────────────────────────────────────────
        # 싱크 ON 중에는 proc_analyzer의 _flush_and_gc()가 보정/정상 판정마다 처리.
        # 싱크 OFF + oped 모니터 대기 중(장시간 구동)에는 여기서 10분마다 한 번 트림.
        # 10분보다 짧게 설정하면 방금 복구한 페이지를 바로 내려 페이지 폴트 낭비.
        _WS_TRIM_INTERVAL = 600  # 10분(초)
        if (not self._running
                and _now - getattr(self, '_ws_trim_t', 0) >= _WS_TRIM_INTERVAL):
            self._ws_trim_t = _now
            from mem_utils import trim_working_set
            trim_working_set()
        # ─────────────────────────────────────────────────────────────────────

        # oped 모니터 상태 진단 (매 30초마다)
        if time.time() - getattr(self, "_diag_t", 0) > 30:
            self._diag_t = time.time()
            running = getattr(self, "_oped_monitor_running", False)
            threads = getattr(self, "_om_threads", [])
            alive   = [t.is_alive() for t in threads]
            import datetime as _dt
            msg = (f"[{_dt.datetime.now().strftime('%H:%M:%S')}] 🔧 oped_monitor={running} "
                   f"threads={len(threads)} alive={alive}")
            if not hasattr(self, "_log_lines"):
                self._log_lines = collections.deque(maxlen=100)
            self._log_lines.append(msg)

        # ── P2 로그 큐 수집 (싱크 ON/OFF 무관하게 항상 처리) ────────────────
        # audio_capture.py 의 send_log() 가 이미 타임스탬프를 붙여서 보냄
        for _lq_attr in ("_main_log_queue", "_om_log_queue"):
            _lq = getattr(self, _lq_attr, None)
            if _lq is None:
                continue
            while True:
                try:
                    msg = _lq.get_nowait()
                    if not hasattr(self, "_log_lines"):
                        self._log_lines = collections.deque(maxlen=100)
                    self._log_lines.append(f"🔊 {msg}")
                    # 캡처 방식 감지 — send_log 메시지에서 추출
                    if "[ProcessLoopback]" in msg:
                        self._aud_capture_mode = "ProcessLoopback"
                    elif "[GlobalLoopback]" in msg:
                        self._aud_capture_mode = "GlobalLoopback"
                except Exception:
                    break

        # ── oped 모니터(싱크 OFF) state_queue 처리 ───────────────────────
        if getattr(self, "_oped_monitor_running", False):
            om_latest  = None
            om_prompts = []
            while True:
                try:
                    item = self._om_state_queue.get_nowait()
                    om_latest = item
                    p = item.get("oped_prompt") if isinstance(item, dict) else None
                    if p:
                        om_prompts.append(p)
                except Exception:
                    break
            for p in om_prompts:
                self._show_oped_skip_popup(p, use_om_queue=True)
            if om_latest:
                om_logs = om_latest.get("log_lines")
                if om_logs is not None:
                    if not hasattr(self, "_log_lines"):
                        self._log_lines = collections.deque(maxlen=100)
                    seen     = getattr(self, "_om_log_seen_count", 0)
                    last_log = getattr(self, "_om_log_seen_last", None)
                    # oped monitor도 동일한 wrap 감지 적용
                    wrap = (seen >= len(om_logs) and last_log is not None
                            and om_logs[-1] != last_log)
                    # [Bug 1 수정] wrap 발생 시 전체를 재추가하지 않고 마지막 1개만 추가.
                    # 기존 코드(seen=0 후 전체 재추가)는 _log_lines에 T2·T3 이외 소스의
                    # 로그(audio capture 직접 로그 등)를 모두 덮어씌우는 문제가 있었음.
                    if wrap:
                        self._log_lines.append(om_logs[-1])
                    elif seen > len(om_logs):
                        seen = 0
                        for line in om_logs[seen:]:
                            self._log_lines.append(line)
                    else:
                        for line in om_logs[seen:]:
                            self._log_lines.append(line)
                    self._om_log_seen_count = len(om_logs)
                    self._om_log_seen_last  = om_logs[-1] if om_logs else None
                # 싱크 OFF 상태에서 팟플레이어·오디오·프로세스 상태 표시 갱신
                pot_ok = om_latest.get("potplayer_ok", False)
                aud_n  = om_latest.get("audio_samples", 0) if pot_ok else 0
                # 팟플레이어 종료 감지 → 시청 기록 탭으로 전환
                if not pot_ok and getattr(self, "_pot_was_ok", False):
                    if hasattr(self, "_switch_tab_fn"):
                        self._switch_tab_fn("history")
                self._pot_was_ok = pot_ok
                c = self.ACCENT3 if pot_ok else self.ACCENT2
                self._pot_dot.config(fg=c)
                self._pot_lbl.config(text="연결됨" if pot_ok else "미감지", fg=c)
                c = self.ACCENT3 if aud_n > 0 else self.TEXT_DIM
                self._aud_dot.config(fg=c)
                _aud_mode = getattr(self, "_aud_capture_mode", "")
                _aud_suffix = f" ({_aud_mode})" if _aud_mode and aud_n > 0 else ""
                self._aud_lbl.config(text=("캡처 중" if aud_n > 0 else "대기 중") + _aud_suffix, fg=c)
                # ── [버그1 수정] 세 가지 조건이 모두 충족될 때만 "OP/ED 감지 중" 표시 ──
                # 조건1) 팟플레이어 감지됨, 조건2) 오디오 캡처 확인됨, 조건3) 메인 싱크 미실행
                if pot_ok and aud_n > 0 and not self._running:
                    self._proc_dot.config(fg=self.ACCENT)
                    self._proc_lbl.config(text="OP/ED 감지 중", fg=self.ACCENT)
                else:
                    self._proc_dot.config(fg=self.TEXT_DIM)
                    self._proc_lbl.config(text="대기 중", fg=self.TEXT_DIM)

        latest       = None
        main_toasts  = []
        main_prompts = []
        while True:
            try:
                item = self.state_queue.get_nowait()
                latest = item
                n = item.get("notify")
                if n:
                    main_toasts.append(n)
                p = item.get("oped_prompt") if isinstance(item, dict) else None
                if p:
                    main_prompts.append(p)
            except Exception:
                break

        for title, msg in main_toasts:
            threading.Thread(target=self._toast, args=(title, msg),
                             daemon=True).start()

        for p in main_prompts:
            self._show_oped_skip_popup(p)

        if latest:
            pot_ok = latest.get("potplayer_ok", False)
            aud_n  = latest.get("audio_samples", 0)
            lip_n  = latest.get("lip_samples", 0)
            offset = latest.get("offset_ms", 0.0)
            status = latest.get("status", "대기 중")
            corr   = latest.get("correction_ms", 0)
            logs   = latest.get("log_lines", [])

            # 팟플레이어 종료 감지 → 시청 기록 탭으로 전환
            if not pot_ok and getattr(self, "_pot_was_ok", False):
                if hasattr(self, "_switch_tab_fn"):
                    self._switch_tab_fn("history")
            self._pot_was_ok = pot_ok
            c = self.ACCENT3 if pot_ok else self.ACCENT2
            t = "연결됨" if pot_ok else "미감지"
            self._pot_dot.config(fg=c); self._pot_lbl.config(text=t, fg=c)

            _aud_n_disp = aud_n if pot_ok else 0
            c = self.ACCENT3 if _aud_n_disp > 0 else self.TEXT_DIM
            _aud_mode = getattr(self, "_aud_capture_mode", "")
            _aud_suffix = f" ({_aud_mode})" if _aud_mode and _aud_n_disp > 0 else ""
            t = ("캡처 중" if _aud_n_disp > 0 else "대기 중") + _aud_suffix
            self._aud_dot.config(fg=c); self._aud_lbl.config(text=t, fg=c)

            if self._running and lip_n > 0:
                sign = "+" if offset > 0 else ""
                col  = (self.ACCENT2  if abs(offset) >= 80
                        else self.ACCENT3 if abs(offset) < 30
                        else self.ACCENT)
                self._offset_lbl.config(text=f"{sign}{offset:.0f} ms", fg=col)
            else:
                self._offset_lbl.config(text="— ms", fg=self.ACCENT)

            bw    = self._bar_ref.winfo_width()
            ratio = min(abs(offset) / 500, 1.0)
            col   = self.ACCENT2 if abs(offset) >= 80 else self.ACCENT3
            self._bar.place(x=0, y=0, width=int(bw * ratio), height=4)
            self._bar.config(bg=col)

            badge_map = {
                "정상":              (self.ACCENT3, self.BG3),
                "보정 완료":         (self.ACCENT,  self.BG3),
                "팟플레이어 미감지": (self.ACCENT2, self.BG3),
                "데이터 수집 중":    (self.TEXT,    self.BG3),
                "대기 중":           (self.TEXT,    self.BG3),
            }
            fg, bg = badge_map.get(status, (self.TEXT, self.BG3))
            self._badge.config(text=f"  {status}  ", fg=fg, bg=bg)

            sign = "+" if corr >= 0 else ""
            self._corr_lbl.config(text=f"{sign}{corr} ms")
            self._lip_cnt.config(text=str(lip_n))
            self._aud_cnt.config(text=str(aud_n))
            pc = self.ACCENT3 if self._running else self.TEXT_DIM
            self._proc_dot.config(fg=pc)
            # 마지막으로 본 줄 이후 새 항목만 추가
            if not hasattr(self, "_log_lines"):
                self._log_lines = collections.deque(maxlen=100)
            if logs:
                seen     = getattr(self, "_log_seen_count", 0)
                last_log = getattr(self, "_log_seen_last", None)

                # ── [Bug 1 수정] deque wrap 감지 및 단일 항목 추가 ────────────
                # log_lines는 maxlen=100 deque. 100개가 꽉 찬 뒤 새 항목이 들어오면
                # len(logs)는 항상 100으로 고정되어 seen == len(logs)가 유지됨.
                # 이 상태에서 logs[-1]이 이전과 달라지면 wrap이 발생한 것.
                # 기존: seen=0 후 전체 100개 재추가 → 오디오 캡처 로그 등
                # 다른 소스의 로그가 모두 덮어씌워지는 문제 발생.
                # 수정: wrap 시 마지막 1개(신규 항목)만 추가.
                wrap = (seen >= len(logs) and last_log is not None
                        and logs[-1] != last_log)
                if wrap:
                    self._log_lines.append(logs[-1])
                elif seen > len(logs):
                    seen = 0
                    for line in logs[seen:]:
                        self._log_lines.append(line)
                else:
                    for line in logs[seen:]:
                        self._log_lines.append(line)
                self._log_seen_count = len(logs)
                self._log_seen_last  = logs[-1] if logs else None

        self.root.after(100, self._refresh)
