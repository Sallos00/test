"""
gui/run_refresh.py -- 100ms 주기 UI 갱신 Mixin
_refresh

[수정]
- 메인 state_queue 블록: _oped_monitor_running=True이고 _running=False일 때
  좀비 T3의 상태가 proc_dot/proc_lbl을 덮어쓰지 않도록 가드 추가.
  (기존: 좀비 T3 → state_queue → main 블록 → proc_dot=TEXT_DIM 덮어씀
         → oped 모니터가 ACCENT로 설정해도 즉시 원래대로 돌아감)

[버그 수정] 팟플레이어 상태 전환(Transition) 시 즉시 UI 갱신:
  · [연결됨 → 미감지]: _handle_pot_exit → _update_ui() 내에서
    pot_lbl="미감지"(ACCENT2), aud_lbl="대기 중"(TEXT_DIM),
    proc_lbl="대기 중"(TEXT_DIM) 을 즉시 반영.
    (기존: proc_lbl만 갱신 + 색상 오류(ACCENT→TEXT_DIM), pot/aud 미갱신)
  · 오디오 장치 표기를 aud_n 의존성에서 Windows 빌드 기반 즉시 표기로 변경.
    (기존: aud_n>0 일 때만 "캡처 중" → 재연결 직후 표기 지연 발생)
  · OP/ED 모니터 블록의 proc_lbl 표기에서 불필요한 aud_n>0 조건 제거.
    (요구사항: pot "연결됨" 이면 "OP/ED 감지 중", pot "미감지" 이면 "대기 중")
"""
import time
import threading
import collections
import platform as _platform

from win32_utils import find_potplayer_hwnd, get_playback_info

# Windows 빌드 확인 — ProcessLoopback은 빌드 19041(20H1) 이상에서만 지원
def _windows_build() -> int:
    try:
        return int(_platform.version().split(".")[-1])
    except Exception:
        return 0

_WIN_BUILD                = _windows_build()
_SUPPORT_PROCESS_LOOPBACK = (_WIN_BUILD >= 19041)
# 빌드 버전으로 결정한 오디오 캡처 모드 문자열 (UI 즉시 표기에 사용)
_AUDIO_CAPTURE_MODE       = "ProcessLoopback" if _SUPPORT_PROCESS_LOOPBACK else "GlobalLoopback"


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
                    # ── [재연결 수정] try/finally로 _pot_exit_handling 해제 보장 ──
                    # 기존: _stop_processes()에서 예외 발생 시 _update_ui가 스케줄되지
                    #   않아 _pot_exit_handling이 True로 영구 고착 →
                    #   _refresh()의 exit 감지 조건이 항상 False가 되어
                    #   팟플레이어 재시작 후에도 재연결 로직이 동작하지 않는 문제.
                    # 수정: _scheduled 플래그로 _update_ui 스케줄 성공 여부를 추적,
                    #   실패 시 finally 블록에서 즉시 플래그 해제 + 재연결 스레드 시작.
                    _scheduled = [False]
                    try:
                        self._stop_processes()
                        def _update_ui():
                            if self._closing:
                                self._pot_exit_handling = False
                                return
                            # ── [UI 탭 전환 버그 수정] 팟플레이어 종료 즉시 탭 전환 ──
                            # 기존: state_queue 드레인 타이밍에 의존 → _stop_processes가
                            #   state_queue를 먼저 비우면 pot_ok=False 상태가 소실되어
                            #   탭 전환이 영구적으로 발생하지 않는 경쟁조건 존재.
                            # 수정: _update_ui에서 _pot_was_ok를 직접 확인해 즉시 호출.
                            #   _refresh의 omed/main 블록과의 중복 전환은 이 시점에
                            #   _pot_was_ok=False로 설정함으로써 방지한다.
                            self._pot_was_ok = False   # 즉시 미감지 상태로 고정

                            # ── [연결됨 → 미감지] 전환: 상태 표기 즉시 갱신 ──────────
                            # 요구사항:
                            #   pot_lbl  → "미감지" (ACCENT2)
                            #   aud_lbl  → "대기 중" (TEXT_DIM)
                            #   proc_lbl → "대기 중" (TEXT_DIM)
                            self._pot_dot.config(fg=self.ACCENT2)
                            self._pot_lbl.config(text="미감지", fg=self.ACCENT2)
                            self._aud_dot.config(fg=self.TEXT_DIM)
                            self._aud_lbl.config(text="대기 중", fg=self.TEXT_DIM)
                            self._proc_dot.config(fg=self.TEXT_DIM)
                            self._proc_lbl.config(text="대기 중", fg=self.TEXT_DIM)
                            self._start_btn.config(
                                text="⏳ 대기 중...",
                                bg=self.BG3, fg=self.TEXT_DIM,
                                activebackground=self.BORDER,
                                state="disabled"
                            )
                            self._badge.config(text="  대기 중  ", fg=self.TEXT, bg=self.BG3)
                            # 싱크 일시 중단 상태: oped 모니터 유지 + 팟플 재연결 대기.
                            # 팟플레이어 재감지 시 _wait_for_potplayer → _start_processes 로
                            # 싱크가 자동으로 재개된다.
                            self._start_oped_monitor()
                            # ── [반복 실행 오류·재연결 수정] 중복 대기 스레드 방지 ──
                            # _waiting_for_pot 플래그가 세워져 있으면 이미 대기 중인
                            # 스레드가 존재하므로 새 스레드를 추가로 생성하지 않는다.
                            if not getattr(self, "_waiting_for_pot", False):
                                threading.Thread(
                                    target=self._wait_for_potplayer, daemon=True).start()
                            self._pot_exit_handling = False
                        self.root.after(0, _update_ui)
                        _scheduled[0] = True
                    except Exception:
                        pass
                    finally:
                        if not _scheduled[0]:
                            # _stop_processes() 예외 발생 → _update_ui가 스케줄되지
                            # 않은 비정상 경로. 플래그를 즉시 해제하고 재연결 대기
                            # 스레드를 보장해 싱크 재개 경로가 완전히 막히지 않도록 한다.
                            def _emergency_reset():
                                if self._closing:
                                    return
                                self._pot_exit_handling = False
                                if not getattr(self, "_waiting_for_pot", False):
                                    threading.Thread(
                                        target=self._wait_for_potplayer,
                                        daemon=True).start()
                            try:
                                self.root.after(0, _emergency_reset)
                            except Exception:
                                self._pot_exit_handling = False

        # Working Set 트림: 싱크 실행 중·미실행 모두 2분 주기로 수행.
        # [메모리 수정] 기존 10분(600s) → 2분(120s)으로 단축.
        # OpenBLAS 스레드 풀 스택 등 page-in된 페이지를 더 빠르게 page-out시킴.
        # GC는 full_cleanup/_flush_and_gc에서 별도로 처리하므로 여기서는 WS trim만.
        _WS_TRIM_INTERVAL = 120
        if _now - getattr(self, '_ws_trim_t', 0) >= _WS_TRIM_INTERVAL:
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
                    # _aud_capture_mode는 UI 표기에 더 이상 사용되지 않으나
                    # 디버그 로그 분류 목적으로 유지.
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
                self._pot_was_ok = pot_ok

                # ── 팟플레이어 상태 표기 ─────────────────────────────────────
                # pot_ok=True → "연결됨", pot_ok=False → "미감지"
                c = self.ACCENT3 if pot_ok else self.ACCENT2
                self._pot_dot.config(fg=c)
                self._pot_lbl.config(text="연결됨" if pot_ok else "미감지", fg=c)

                # ── 오디오 장치 상태 표기 ─────────────────────────────────────
                # 요구사항: pot "연결됨" → Windows 빌드 기반 캡처 모드 즉시 표기
                #            pot "미감지" → "대기 중"
                # (기존: aud_n>0 조건에 의존 → 연결 초기에 표기 지연 발생)
                if pot_ok:
                    self._aud_dot.config(fg=self.ACCENT3)
                    self._aud_lbl.config(
                        text=f"캡처 중 ({_AUDIO_CAPTURE_MODE})", fg=self.ACCENT3)
                else:
                    self._aud_dot.config(fg=self.TEXT_DIM)
                    self._aud_lbl.config(text="대기 중", fg=self.TEXT_DIM)

                # ── 프로세스 상태 표기 ────────────────────────────────────────
                # 요구사항: pot "연결됨" + 싱크 미실행 → "OP/ED 감지 중"
                #            pot "미감지"              → "대기 중"
                # (기존: aud_n>0 조건이 불필요하게 추가되어 있어 오도적 표기 발생)
                if pot_ok and not self._running:
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

        # ── [메모리 누수 수정] 토스트 전용 워커 스레드 ───────────────────────
        # 기존: 알림마다 threading.Thread 신규 생성 → 스레드 스택 누적 +
        #   winotify가 사용하는 WinRT DLL(CoreUIComponents, CoreMessaging 등)을
        #   스레드마다 반복 로드해 Commit 메모리 계단식 증가.
        # 수정: Queue 기반 단일 워커 스레드 재사용 → 스레드 생성 비용 0,
        #   WinRT DLL은 최초 1회만 로드.
        if main_toasts:
            import queue as _tq
            if not hasattr(self, '_toast_q'):
                self._toast_q = _tq.Queue()
                def _toast_worker():
                    while not getattr(self, '_closing', False):
                        try:
                            _title, _msg = self._toast_q.get(timeout=1.0)
                            self._toast(_title, _msg)
                        except Exception:
                            pass
                threading.Thread(target=_toast_worker,
                                 daemon=True, name='toast-worker').start()
            for title, msg in main_toasts:
                try:
                    self._toast_q.put_nowait((title, msg))
                except Exception:
                    pass

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

            self._pot_was_ok = pot_ok

            # ── 팟플레이어 상태 표기 ─────────────────────────────────────────
            c = self.ACCENT3 if pot_ok else self.ACCENT2
            t = "연결됨" if pot_ok else "미감지"
            self._pot_dot.config(fg=c); self._pot_lbl.config(text=t, fg=c)

            # ── 오디오 장치 상태 표기 ─────────────────────────────────────────
            # 요구사항: pot "연결됨" → Windows 빌드 기반 캡처 모드 즉시 표기
            #            pot "미감지" → "대기 중"
            # (기존: aud_n>0 의존으로 재연결 직후 표기 지연, 색상 불일치 발생)
            if pot_ok:
                self._aud_dot.config(fg=self.ACCENT3)
                self._aud_lbl.config(
                    text=f"캡처 중 ({_AUDIO_CAPTURE_MODE})", fg=self.ACCENT3)
            else:
                self._aud_dot.config(fg=self.TEXT_DIM)
                self._aud_lbl.config(text="대기 중", fg=self.TEXT_DIM)

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

            # ── 프로세스 상태 표기 ─────────────────────────────────────────────
            # 요구사항:
            #   pot "연결됨" + 싱크 실행 중 → "P1·T2·T3 실행 중"
            #   pot "미감지"               → "대기 중" (싱크 실행 여부 무관)
            #   pot "연결됨" + 싱크 미실행 → oped 모니터 블록이 처리, 덮어쓰지 않음
            #
            # [Bug Fix] 기존: self._running 여부만 보고 항상 proc_dot/lbl을 갱신.
            #   → oped 모니터가 ACCENT(초록)로 설정한 proc_dot을
            #     좀비 T3의 state가 TEXT_DIM(회색)으로 즉시 덮어씀.
            if pot_ok and self._running:
                self._proc_dot.config(fg=self.ACCENT3)
                self._proc_lbl.config(text="P1·T2·T3 실행 중", fg=self.ACCENT3)
            elif not pot_ok:
                # pot "미감지": 싱크 실행 여부 무관하게 "대기 중"
                self._proc_dot.config(fg=self.TEXT_DIM)
                self._proc_lbl.config(text="대기 중", fg=self.TEXT_DIM)
            elif not getattr(self, "_oped_monitor_running", False):
                # pot_ok=True, _running=False, oped 모니터도 없는 경우
                self._proc_dot.config(fg=self.TEXT_DIM)
                self._proc_lbl.config(text="대기 중", fg=self.TEXT_DIM)
            # pot_ok=True + _running=False + _oped_monitor_running=True:
            #   → oped 모니터 블록이 이미 proc_dot/proc_lbl을 올바르게 설정했으므로
            #     여기서 건드리지 않는다.

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
