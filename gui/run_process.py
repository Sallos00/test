"""
gui/run_process.py -- 프로세스/스레드 시작·중지·초기화 Mixin
_start_processes, _stop_processes, _reset

[수정]
- _start_processes: stop_flag.clear() 대신 새 threading.Event() 생성 (좀비 스레드 부활 방지)
- _stop_processes:  스레드 join timeout 2s → 5s (T3 sleep INTERVAL=3s 보다 크게)
- T2/T3 스레드 목표에 예외 래퍼 추가 (무음 종료 방지)
- [이슈1] _start_processes 완료 시점에 hwnd 직접 조회 → 팟플 재시작 직후
  '미감지' 고정 현상 방지 (oped 모니터 종료 후 UI에 잔류하는 pot_ok=False 즉시 교정)
- [이슈2] _start_processes 진입 시 대기 중인 팝업 after_id 취소 +
  이미 열린 팝업 위젯(_start_popup_widget) 강제 소멸 → 싱크 중 팝업 노출 차단
- [버그 수정] _start_processes 완료 시점에 aud_lbl도 즉시 갱신 (재연결 직후 표기 지연 방지)
  Windows 빌드 버전 기반으로 캡처 모드를 결정해 pot_lbl과 동시에 aud_lbl 업데이트.
"""
import time
import threading
import collections
import queue as _queue
import multiprocessing as _mp
import platform as _platform
from multiprocessing import Process, Array as _MpArray

from win32_utils import find_potplayer_hwnd, post_key_to_potplayer, VK_OEM_2
from mem_utils import full_cleanup_and_release, full_cleanup

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

# proc_analyzer가 각 루프 끝에서 sleep하는 최대 시간 (ANALYSIS_INTERVAL = 3.0s).
# 스레드 join timeout은 이 값보다 충분히 크게 설정해야 좀비 스레드를 막을 수 있다.
_THREAD_JOIN_TIMEOUT = 5  # INTERVAL(3) + 여유(2)


class ProcessMixin:

    def _start_processes(self):
        """P1(프로세스) + T2·T3(스레드) 시작."""

        # ── [반복 실행 오류 수정] 이중 시작 방지 ────────────────────────────
        # 복수의 _wait_for_potplayer 스레드가 동시에 pot 감지 후 after(0)을 통해
        # _start_processes를 연속 호출하면, 두 번째 호출이 첫 번째가 생성한
        # 파이프·큐를 강제 해제해 T2/T3가 즉시 죽는 문제가 발생한다.
        # self._running이 True이면 이미 세션이 활성화된 상태이므로 즉시 반환한다.
        if self._running:
            return

        self._stop_oped_monitor()   # 싱크 시작 시 별도 모니터 중지

        # ── [Bug Fix 이슈2] 싱크 시작 시 대기 중인 '동영상 감지' 팝업 즉시 취소 ──
        # _monitor_for_popup이 after_idle로 예약한 _safe_show 콜백을 취소하고,
        # 이미 열려 있는 팝업 창(countdown 진행 중)도 강제로 닫는다.
        # 경쟁 조건: _running=False인 순간 팝업이 트리거되었으나
        #   그 직후 _start_processes가 호출되면 팝업이 싱크 중에도 노출됨.
        _pending = getattr(self, '_popup_after_id', None)
        if _pending is not None:
            try:
                self.root.after_cancel(_pending)
            except Exception:
                pass
            self._popup_after_id = None
        self._popup_open = False
        _open_popup = getattr(self, '_start_popup_widget', None)
        if _open_popup is not None:
            try:
                _open_popup.destroy()
            except Exception:
                pass
            self._start_popup_widget = None

        # ── [Bug Fix 1] 세션마다 새 stop_flag 생성 ──────────────────────────
        # 기존: self.stop_flag.clear() → 직전 세션에서 죽지 못한 좀비 T3가
        #   clear() 직후 루프를 재개해 새 T3와 state_queue를 공유하는 경쟁 발생.
        # 수정: 새 Event()를 생성 → 좀비 T3는 여전히 old_event(set 상태)를 참조하므로
        #   자연스럽게 종료되고, 새 T2/T3는 새 Event를 사용해 간섭 없이 동작한다.
        self.stop_flag = threading.Event()

        self._running = True
        self._pot_exit_handling = False
        runtime_cfg = self._build_cfg()

        self._log_seen_count = 0

        self._pos_lock   = threading.Lock()
        self._shared_pos = [-1]
        self._shared_dur = [-1]

        import ctypes as _ct
        self._stream_anchor = _MpArray(_ct.c_double, [0.0, 48000.0, 1.0])

        self._main_log_queue = _queue.Queue(maxsize=200)

        _old_lip   = getattr(self, '_lip_queue_writer', None)
        _old_lip_r = getattr(self, '_lip_queue', None)
        if _old_lip is not None or _old_lip_r is not None:
            full_cleanup_and_release(
                mp_queues=[q for q in (_old_lip_r, _old_lip) if q is not None],
            )
        _old_aud = getattr(self, '_audio_queue', None)
        if _old_aud is not None:
            full_cleanup(queues=[_old_aud])

        _lip_r, _lip_w = _mp.Pipe(duplex=False)
        self._lip_queue        = _lip_r
        self._lip_queue_writer = _lip_w
        self._audio_queue = _queue.Queue(maxsize=30)

        from processes import proc_lip_capture, proc_audio_capture, proc_analyzer  # lazy import

        self._p1_stop_flag = _mp.Event()
        p1 = Process(target=proc_lip_capture,
                     args=(self._lip_queue_writer, self._p1_stop_flag, runtime_cfg, self._stream_anchor),
                     daemon=True)
        p1.start()
        try:
            self._lip_queue_writer.close()
        except Exception:
            pass
        self._processes.append(p1)

        # ── [Bug Fix 2] T2·T3 예외 래퍼 ────────────────────────────────────
        # 스레드 내부 예외는 메인스레드로 전파되지 않아 무음 종료된다.
        # 래퍼로 감싸 _log_lines에 기록, 문제 진단을 가능하게 한다.
        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)
        _log_ref = self._log_lines

        def _wrap(fn, label):
            def _safe(*args):
                try:
                    fn(*args)
                except Exception as _e:
                    import traceback as _tb, time as _t
                    _log_ref.append(
                        f"[{_t.strftime('%H:%M:%S')}] "
                        f"❌ 스레드[{label}] 비정상 종료: {_e}"
                    )
                    _log_ref.append(_tb.format_exc()[-300:])
            return _safe

        # 현재 세션 flag를 명시적으로 캡처 (클로저 안전)
        _cur_stop = self.stop_flag

        for fn, args, label in [
            (proc_audio_capture,
             (self._audio_queue, _cur_stop, runtime_cfg,
              self._main_log_queue, self._stream_anchor),
             "T2_audio"),
            (proc_analyzer,
             (self._lip_queue, self._audio_queue,
              self.state_queue, self.cmd_queue,
              _cur_stop, runtime_cfg,
              self._shared_pos, self._shared_dur),
             "T3_analyzer"),
        ]:
            t = threading.Thread(target=_wrap(fn, label), args=args, daemon=True)
            t.start()
            self._processes.append(t)

        self._start_btn.config(text="⏹ 정지",
                               bg=self.BG3, fg=self.ACCENT2,
                               activebackground=self.BORDER,
                               state="normal")
        self._proc_lbl.config(text="P1·T2·T3 실행 중", fg=self.ACCENT3)
        self._toast("🎬 Auto Sync", "싱크 보정이 시작되었습니다.")

        # ── [Bug Fix 이슈1] 재시작 직후 '미감지' 상태 고정 방지 ─────────────
        # _stop_oped_monitor() 종료 후 oped 모니터의 마지막 potplayer_ok=False 값이
        # UI에 잔류한 채 T3의 첫 상태 보고가 도달하기 전까지 '미감지'로 고정됨.
        # 해결: _start_processes 완료 시점에 hwnd를 직접 조회하여 즉시 '연결됨'으로 갱신.
        #
        # [버그 수정] 재연결 시 aud_lbl도 즉시 갱신:
        # 기존에는 pot_lbl만 즉시 갱신하고 aud_lbl은 T3 상태가 도달할 때까지 대기
        # → 재연결 직후 aud_lbl이 "대기 중"으로 잔류하는 표기 지연 발생.
        # 해결: hwnd 확인 후 Windows 빌드 기반 캡처 모드를 pot_lbl과 함께 즉시 표기.
        _hwnd_now = find_potplayer_hwnd()
        if _hwnd_now:
            self._pot_dot.config(fg=self.ACCENT3)
            self._pot_lbl.config(text="연결됨", fg=self.ACCENT3)
            # ── [미감지 → 연결됨] 전환: 오디오 장치 상태 즉시 갱신 ──────────
            # 요구사항: pot "연결됨" 시 Windows 빌드 버전 체크 후 캡처 모드 즉시 표기
            self._aud_dot.config(fg=self.ACCENT3)
            self._aud_lbl.config(
                text=f"캡처 중 ({_AUDIO_CAPTURE_MODE})", fg=self.ACCENT3)

    def _stop_processes(self):
        self._running = False
        self.stop_flag.set()
        # ── [재연결 수정] PID 캐시 즉시 무효화 ───────────────────────────────
        # 팟플레이어 종료 즉시 _pid_cache를 초기화해 새 T2가 죽은 PID를
        # 사용하지 않도록 한다. (_stop_processes → _start_processes 순으로 호출되므로
        # 여기서 한 번만 무효화해도 새 캡처 세션이 올바른 PID를 탐색한다.)
        try:
            from audio_capture import invalidate_pid_cache
            invalidate_pid_cache()
        except Exception:
            pass
        if hasattr(self, "_p1_stop_flag"):
            self._p1_stop_flag.set()
        try:
            self.cmd_queue.put_nowait("stop")
        except Exception:
            pass
        procs = list(self._processes)

        # ── [Bug Fix 3] 스레드 join timeout을 5s로 증가 ─────────────────────
        # proc_analyzer(T3)는 루프 끝에서 최대 INTERVAL(3.0s) sleep한다.
        # 기존 2s timeout은 T3가 sleep 중일 때 join이 먼저 타임아웃 → 좀비 스레드 발생.
        # 5s로 늘려 T3가 sleep에서 깨어나 stop_flag를 확인하고 정상 종료하도록 한다.
        def _stop(w):
            if isinstance(w, Process):
                w.join(timeout=2)
                if w.is_alive():
                    w.terminate()
            else:
                w.join(timeout=_THREAD_JOIN_TIMEOUT)

        ts = [threading.Thread(target=_stop, args=(w,), daemon=True) for w in procs]
        for t in ts: t.start()
        for t in ts: t.join()
        self._processes.clear()
        if hasattr(self, "_shared_pos"): self._shared_pos[0] = -1
        if hasattr(self, "_shared_dur"): self._shared_dur[0] = -1
        try:
            while True: self.state_queue.get_nowait()
        except Exception:
            pass
        _lip_q = getattr(self, '_lip_queue', None)
        _other_qs = [q for q in (
            getattr(self, '_audio_queue', None),
            getattr(self, '_main_log_queue', None),
        ) if q is not None]
        full_cleanup_and_release(
            queues=_other_qs,
            mp_queues=([_lip_q] if _lip_q is not None else []),
        )

        # ── [반복 실행 오류 수정] 큐·파이프 참조 초기화 ─────────────────────
        # _stop_processes 후 참조가 남아있으면, 재시작 시 _start_processes가
        # 이미 해제된 자원을 다시 full_cleanup_and_release하려다 OSError/데이터
        # 손상이 발생한다. None으로 초기화해 이중 해제를 완전히 방지한다.
        self._lip_queue        = None
        self._lip_queue_writer = None
        self._audio_queue      = None
        self._main_log_queue   = None

    def _reset(self):
        if self._running:
            try:
                self.cmd_queue.put_nowait("reset")
                self.cmd_queue.put_nowait("oped_reset")
            except Exception:
                pass
            try:
                while True:
                    self.state_queue.get_nowait()
            except Exception:
                pass
            self._log_seen_count = 0
            _qs = [q for q in (
                getattr(self, '_lip_queue', None),
                getattr(self, '_audio_queue', None),
                getattr(self, '_main_log_queue', None),
            ) if q is not None]
            full_cleanup(queues=_qs)
            self._log_popup_rendered  = 0
            self._log_popup_last_line = None
            self._offset_lbl.config(text="— ms", fg=self.ACCENT)
            self._corr_lbl.config(text="+0 ms")
            self._lip_cnt.config(text="0")
            self._aud_cnt.config(text="0")
            self._bar.place(x=0, y=0, width=0, height=4)
            self._badge.config(text="  대기 중  ", fg=self.TEXT, bg=self.BG3)
            return
        if getattr(self, "_oped_monitor_running", False):
            try:
                self._om_cmd_queue.put_nowait("oped_reset")
            except Exception:
                pass
            try:
                while True:
                    self._om_state_queue.get_nowait()
            except Exception:
                pass
            _om_qs = [q for q in (
                getattr(self, '_om_lip_queue', None),
                getattr(self, '_om_audio_queue', None),
                getattr(self, '_om_log_queue', None),
            ) if q is not None]
            full_cleanup(queues=_om_qs)

        self._log_popup_rendered  = 0
        self._log_popup_last_line = None
        self._om_log_seen_count   = 0
        self._offset_lbl.config(text="— ms", fg=self.ACCENT)
        self._corr_lbl.config(text="+0 ms")
        self._lip_cnt.config(text="0")
        self._aud_cnt.config(text="0")
        self._bar.place(x=0, y=0, width=0, height=4)
        self._badge.config(text="  대기 중  ", fg=self.TEXT, bg=self.BG3)

        hwnd = find_potplayer_hwnd()
        if not hwnd:
            self._proc_lbl.config(text="초기화 완료", fg=self.ACCENT3)
            return
        try:
            post_key_to_potplayer(hwnd, VK_OEM_2, shift=True)
            time.sleep(0.05)
            post_key_to_potplayer(hwnd, 0x6F, shift=True)
            self._proc_lbl.config(text="수동 초기화 완료", fg=self.ACCENT3)
        except Exception:
            self._proc_lbl.config(text="초기화 실패", fg=self.ACCENT2)
