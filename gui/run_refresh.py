"""
gui/run_refresh.py -- 100ms 주기 UI 갱신 Mixin
_refresh

[수정]
- 메인 state_queue 블록: _oped_monitor_running=True이고 _running=False일 때
  좀비 T3의 상태가 proc_dot/proc_lbl을 덮어쓰지 않도록 가드 추가.
  (기존: 좀비 T3 → state_queue → main 블록 → proc_dot=TEXT_DIM 덮어씀
         → oped 모니터가 ACCENT로 설정해도 즉시 원래대로 돌아감)
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

        _WS_TRIM_INTERVAL = 600
        if (not self._running
                and _now - getattr(self, '_ws_trim_t', 0) >= _WS_TRIM_INTERVAL):
            self._ws_trim_t = _now
            from mem_utils import trim_working_set
            trim_working_set()

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
                    if "[ProcessLoopback]" in msg:
                        self._aud_capture_mode = "ProcessLoopback"
                    elif "[GlobalLoopback]" in msg:
                        self._aud_capture_mode = "GlobalLoopback"
                except Exception:
                    break

        # ── oped 모니터 state_queue 처리 ─────────────────────────────────────
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
                    wrap = (seen >= len(om_logs) and last_log is not None
                            and om_logs[-1] != last_log)
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
                pot_ok = om_latest.get("potplayer_ok", False)
                aud_n  = om_latest.get("audio_samples", 0) if pot_ok else 0
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
                if pot_ok and aud_n > 0 and not self._running:
                    self._proc_dot.config(fg=self.ACCENT)
                    self._proc_lbl.config(text="OP/ED 감지 중", fg=self.ACCENT)
                else:
                    self._proc_dot.config(fg=self.TEXT_DIM)
                    self._proc_lbl.config(text="대기 중", fg=self.TEXT_DIM)

        # ── 메인 state_queue 처리 ─────────────────────────────────────────────
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

            # ── [Bug Fix] proc_dot 덮어쓰기 방지 ────────────────────────────
            # 기존: self._running 여부만 보고 항상 proc_dot을 갱신.
            #       → oped 모니터가 ACCENT(초록)으로 설정한 proc_dot을
            #         좀비 T3의 state가 TEXT_DIM(회색)으로 즉시 덮어씀.
            # 수정: 싱크 실행 중일 때만 갱신. oped 모니터 모드에서는
            #       위의 oped 블록이 이미 proc_dot을 올바르게 설정했으므로
            #       여기서 건드리지 않는다.
            if self._running:
                self._proc_dot.config(fg=self.ACCENT3)
            elif not getattr(self, "_oped_monitor_running", False):
                self._proc_dot.config(fg=self.TEXT_DIM)
            # _oped_monitor_running=True인 경우: oped 블록이 이미 처리함

            if not hasattr(self, "_log_lines"):
                self._log_lines = collections.deque(maxlen=100)
            if logs:
                seen     = getattr(self, "_log_seen_count", 0)
                last_log = getattr(self, "_log_seen_last", None)
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
