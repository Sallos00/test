"""
gui/run_process.py -- 프로세스/스레드 시작·중지·초기화 Mixin
_start_processes, _stop_processes, _reset
"""
import time
import threading
import queue as _queue
import multiprocessing as _mp
from multiprocessing import Process, Array as _MpArray

from win32_utils import find_potplayer_hwnd, post_key_to_potplayer, VK_OEM_2
from mem_utils import full_cleanup_and_release, full_cleanup


class ProcessMixin:

    def _start_processes(self):
        """P1(프로세스) + T2·T3(스레드) 시작."""
        self._stop_oped_monitor()   # 싱크 시작 시 별도 모니터 중지
        self._running = True
        self._pot_exit_handling = False  # [Bug 3 수정] 팟플레이어 종료 자동처리 플래그 초기화
        self.stop_flag.clear()
        runtime_cfg = self._build_cfg()

        # 재시작마다 로그 seen 카운터 리셋
        # (T3는 새 log_lines deque를 인덱스 0부터 시작하므로 seen도 0으로 맞춤)
        self._log_seen_count = 0

        # GUI 메인스레드 → T3 공유 재생 위치/길이 (ms), -1 = 미확인
        # 스레드 간 공유 → 일반 list + lock
        self._pos_lock   = threading.Lock()
        self._shared_pos = [-1]
        self._shared_dur = [-1]

        # 오디오 스트림 기준점 — T2가 첫 패킷에서 기록, P1이 읽어서 타임스탬프 통일
        # 프로세스(P1)↔스레드(T2) 간 공유이므로 multiprocessing.Array 사용
        import ctypes as _ct
        self._stream_anchor = _MpArray(_ct.c_double, [0.0, 48000.0, 1.0])

        # 싱크 ON 상태 전용 로그 큐 (T2 → GUI 직접 전달)
        self._main_log_queue = _queue.Queue(maxsize=200)

        # ── lip_queue: Pipe 로 교체 (메모리 절감) ────────────────────────────
        # mp.Queue는 내부적으로 OS 파이프+피더 스레드를 사용하고
        # 생성 시 ~32MB 파이프 버퍼를 사전 할당한다.
        # multiprocessing.Pipe(duplex=False) 는 단방향 Pipe로
        # 실제 전송된 데이터만큼만 버퍼를 씀 → 메모리 증가 억제.
        # P1(프로세스)이 writer, T3(스레드)가 reader.
        # drain_queue / release_mp_queue 모두 Connection.poll()+recv() 지원.
        #
        # 이전 큐/Pipe 잔류 데이터 먼저 해제
        _old_lip = getattr(self, '_lip_queue_writer', None)
        _old_lip_r = getattr(self, '_lip_queue', None)
        if _old_lip is not None or _old_lip_r is not None:
            full_cleanup_and_release(
                mp_queues=[q for q in (_old_lip_r, _old_lip) if q is not None],
            )
        _old_aud = getattr(self, '_audio_queue', None)
        if _old_aud is not None:
            full_cleanup(queues=[_old_aud])

        # Pipe 생성: reader → T3, writer → P1
        _lip_r, _lip_w = _mp.Pipe(duplex=False)
        self._lip_queue        = _lip_r   # T3(proc_analyzer)가 읽는 쪽
        self._lip_queue_writer = _lip_w   # P1(proc_lip_capture)이 쓰는 쪽
        # [수정] audio_queue maxsize를 30으로 고정
        # 기존 runtime_cfg["QUEUE_MAXSIZE"](기본 200)는 과도하게 크므로
        # 메모리 누수를 유발한다. 30으로 제한하여 누적 방지.
        self._audio_queue = _queue.Queue(maxsize=30)

        from processes import proc_lip_capture, proc_audio_capture, proc_analyzer  # lazy import

        # P1: 별도 프로세스 → multiprocessing.Event 필요 (threading.Event는 pickle 불가)
        # writer 쪽(_lip_queue_writer)을 P1에 전달
        self._p1_stop_flag = _mp.Event()
        p1 = Process(target=proc_lip_capture,
                     args=(self._lip_queue_writer, self._p1_stop_flag, runtime_cfg, self._stream_anchor),
                     daemon=True)
        p1.start()
        # P1 fork 후 부모 쪽 writer는 닫아야 reader의 EOFError가 정상 동작함
        # 단, 부모에서 close하면 GC 전까지 참조가 남으므로 명시적으로 닫는다
        try:
            self._lip_queue_writer.close()
        except Exception:
            pass
        self._processes.append(p1)

        # T2·T3: 스레드 → threading.Event (stop_flag) 그대로 사용
        # T3에는 reader(_lip_queue) 전달
        for target, args in [
            (proc_audio_capture, (self._audio_queue, self.stop_flag, runtime_cfg,
                                  self._main_log_queue, self._stream_anchor)),
            (proc_analyzer,      (self._lip_queue, self._audio_queue,
                                  self.state_queue, self.cmd_queue,
                                  self.stop_flag, runtime_cfg,
                                  self._shared_pos, self._shared_dur)),
        ]:
            t = threading.Thread(target=target, args=args, daemon=True)
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
        # P1 전용 stop flag도 함께 세움
        if hasattr(self, "_p1_stop_flag"):
            self._p1_stop_flag.set()
        try:
            self.cmd_queue.put_nowait("stop")
        except Exception:
            pass
        # P1(프로세스)는 terminate, T2·T3(스레드)는 join
        procs = list(self._processes)
        def _stop(w):
            if isinstance(w, Process):
                w.join(timeout=2)
                if w.is_alive():
                    w.terminate()
            else:
                w.join(timeout=2)
        ts = [threading.Thread(target=_stop, args=(w,), daemon=True) for w in procs]
        for t in ts: t.start()
        for t in ts: t.join()
        self._processes.clear()
        if hasattr(self, "_shared_pos"): self._shared_pos[0] = -1
        if hasattr(self, "_shared_dur"): self._shared_dur[0] = -1
        # state_queue 잔류 데이터 드레인 — 재시작 후 _refresh()가 구버전 로그를
        # 읽어 _log_seen_count를 오염시키는 것을 방지한다.
        try:
            while True: self.state_queue.get_nowait()
        except Exception:
            pass
        # 중지 즉시 큐 파이프 버퍼 완전 해제.
        # _lip_queue: Pipe reader(Connection) → drain(poll+recv) + close() 로 OS 파이프 반환.
        # _audio_queue, _main_log_queue: queue.Queue → 드레인만 수행.
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
            # 싱크 실행 중: P3에 reset + oped_reset 커맨드 전송
            try:
                self.cmd_queue.put_nowait("reset")
                self.cmd_queue.put_nowait("oped_reset")
            except Exception:
                pass
            # 즉시 드레인 + 로그 카운터 리셋
            # → reset 후 P3가 새 log_lines를 보내도 seen 값이 남아
            #   "↺ 싱크 초기화" 로그가 표시 안 되는 버그 수정
            try:
                while True:
                    self.state_queue.get_nowait()
            except Exception:
                pass
            self._log_seen_count = 0
            # ── [버그4 수정] 초기화 시 메모리·캐시·버퍼·파이프 버퍼 완전 비우기 ─
            _qs = [q for q in (
                getattr(self, '_lip_queue', None),
                getattr(self, '_audio_queue', None),
                getattr(self, '_main_log_queue', None),
            ) if q is not None]
            full_cleanup(queues=_qs)
            # [버그2 수정] log_lines는 지우지 않음 - 로그 기록은 유지
            self._log_popup_rendered  = 0
            self._log_popup_last_line = None
            # UI 초기화
            self._offset_lbl.config(text="— ms", fg=self.ACCENT)
            self._corr_lbl.config(text="+0 ms")
            self._lip_cnt.config(text="0")
            self._aud_cnt.config(text="0")
            self._bar.place(x=0, y=0, width=0, height=4)
            self._badge.config(text="  대기 중  ", fg=self.TEXT, bg=self.BG3)
            return
        # ── [버그4 수정] 싱크 OFF에서도 버퍼 완전 초기화 ────────────────────
        # oped 모니터에 oped_reset 전송 + 모든 큐/버퍼 비우기
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

        # [버그2 수정] log_lines는 지우지 않음 - 로그 기록은 유지
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
            # [버그4 수정] 팟플레이어 없어도 버퍼 클리어는 이미 완료됨
            self._proc_lbl.config(text="버퍼 초기화 완료 (팟플레이어 미감지)", fg=self.ACCENT3)
            return
        try:
            post_key_to_potplayer(hwnd, VK_OEM_2, shift=True)
            time.sleep(0.05)
            post_key_to_potplayer(hwnd, 0x6F, shift=True)
            self._proc_lbl.config(text="수동 초기화 완료", fg=self.ACCENT3)
        except Exception:
            self._proc_lbl.config(text="초기화 실패", fg=self.ACCENT2)
