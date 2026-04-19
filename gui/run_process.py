"""
gui/run_process.py -- 프로세스/스레드 시작·중지·초기화 Mixin
_start_processes, _stop_processes, _reset

[수정]
- _start_processes: stop_flag.clear() 대신 새 threading.Event() 생성 (좀비 스레드 부활 방지)
- _stop_processes:  스레드 join timeout 2s → 5s (T3 sleep INTERVAL=3s 보다 크게)
- T2/T3 스레드 목표에 예외 래퍼 추가 (무음 종료 방지)
"""
import time
import threading
import collections
import queue as _queue
import multiprocessing as _mp
from multiprocessing import Process, Array as _MpArray

from win32_utils import find_potplayer_hwnd, post_key_to_potplayer, VK_OEM_2
from mem_utils import full_cleanup_and_release, full_cleanup

# proc_analyzer가 각 루프 끝에서 sleep하는 최대 시간 (ANALYSIS_INTERVAL = 3.0s).
# 스레드 join timeout은 이 값보다 충분히 크게 설정해야 좀비 스레드를 막을 수 있다.
_THREAD_JOIN_TIMEOUT = 5  # INTERVAL(3) + 여유(2)


class ProcessMixin:

    def _start_processes(self):
        """P1(프로세스) + T2·T3(스레드) 시작."""
        self._stop_oped_monitor()   # 싱크 시작 시 별도 모니터 중지

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

    def _stop_processes(self):
        self._running = False
        self.stop_flag.set()
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
            self._proc_lbl.config(text="버퍼 초기화 완료 (팟플레이어 미감지)", fg=self.ACCENT3)
            return
        try:
            post_key_to_potplayer(hwnd, VK_OEM_2, shift=True)
            time.sleep(0.05)
            post_key_to_potplayer(hwnd, 0x6F, shift=True)
            self._proc_lbl.config(text="수동 초기화 완료", fg=self.ACCENT3)
        except Exception:
            self._proc_lbl.config(text="초기화 실패", fg=self.ACCENT2)
