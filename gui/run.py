"""
gui/run.py -- 실행 제어, 프로세스 관리, 갱신, 인증 팝업 메서드
"""
import os
import time
import threading
import collections
import ctypes
import ctypes.wintypes
import tkinter as tk
import winreg
from multiprocessing import Process, Queue, Value, Array, Array

import auth as _auth_module

from win32_utils import (
    CFG, find_potplayer_hwnd, is_potplayer_playing,
    is_potplayer_running, post_key_to_potplayer, VK_OEM_2,
    get_playback_info,
)
from gui.ui_logic import _extract_potplayer_title
# processes 모듈은 numpy/psutil 등 무거운 패키지를 포함하므로 실제 사용 시점에 import
# (모듈 레벨 import 시 메인스레드를 블로킹해 시작 속도 저하 유발)

class LipSyncGUIRun:

    # ── OP/ED 백그라운드 모니터 (싱크 OFF 상태에서도 동작) ───────────────────
    # 싱크가 꺼져 있어도 OP/ED 음악 감지 + 팝업/자동스킵은 항상 동작해야 한다.
    # P2(오디오캡처) + P3(싱크분석, lip 없이 오디오만) 를 별도로 구동한다.

    def _start_oped_monitor(self):
        """싱크 미실행 상태 전용 OP/ED 감지 프로세스(P2+P3) 시작."""
        if getattr(self, "_oped_monitor_running", False):
            return
        try:
            runtime_cfg = self._build_cfg()
            qsize = runtime_cfg.get("QUEUE_MAXSIZE", 200)

            self._om_lip_queue   = Queue(maxsize=qsize)
            self._om_audio_queue = Queue(maxsize=qsize)
            self._om_log_queue   = Queue(maxsize=200)
            self._om_state_queue = Queue(maxsize=20)
            self._om_cmd_queue   = Queue(maxsize=10)
            self._om_stop_flag   = Value("b", False)

            # shared_pos/dur: GUI 메인스레드가 갱신, P3가 읽음
            self._om_shared_pos  = Value(ctypes.c_longlong, -1)
            self._om_shared_dur  = Value(ctypes.c_longlong, -1)

            self._om_stream_anchor = Array(ctypes.c_double, [0, 48000, 1])

            from processes import proc_audio_capture, proc_analyzer  # lazy import
            self._om_processes = []
            for target, args in [
                (proc_audio_capture, (
                    self._om_audio_queue,
                    self._om_stop_flag,
                    runtime_cfg,
                    self._om_log_queue,
                    self._om_stream_anchor,
                )),
                (proc_analyzer, (
                    self._om_lip_queue,
                    self._om_audio_queue,
                    self._om_state_queue,
                    self._om_cmd_queue,
                    self._om_stop_flag,
                    runtime_cfg,
                    self._om_shared_pos,
                    self._om_shared_dur,
                )),
            ]:
                p = Process(target=target, args=args, daemon=True)
                p.start()
                self._om_processes.append(p)

            self._oped_monitor_running = True
        except Exception as e:
            self._oped_monitor_running = False
            import time as _t
            if not hasattr(self, "_log_lines"):
                self._log_lines = collections.deque(maxlen=100)
            self._log_lines.append(f"[{_t.strftime('%H:%M:%S')}] ⚠ oped 모니터 시작 실패: {e}")

    def _stop_oped_monitor(self):
        """OP/ED 감지 전용 프로세스 중지."""
        if not getattr(self, "_oped_monitor_running", False):
            return
        try:
            self._om_stop_flag.value = True
            # analyzer에 stop 커맨드 전송으로 즉시 탈출 유도
            try:
                self._om_cmd_queue.put_nowait("stop")
            except Exception:
                pass
            import threading as _th
            def _join(p):
                p.join(timeout=1)
                if p.is_alive(): p.terminate()
            ts = [_th.Thread(target=_join, args=(p,), daemon=True) for p in self._om_processes]
            for t in ts: t.start()
            for t in ts: t.join()
        except Exception:
            pass
        self._om_processes            = []
        self._oped_monitor_running    = False
        self._om_log_seen_count       = 0

    def _toggle(self):
        if not self._running:
            self._stop_oped_monitor()   # 싱크 시작 전 모니터 중지 (중복 방지)
            hwnd = find_potplayer_hwnd()
            if not hwnd:
                self._start_btn.config(text="⏳ 대기 중...",
                                       bg=self.BG3, fg=self.TEXT_DIM,
                                       activebackground=self.BORDER,
                                       state="disabled")
                self._proc_lbl.config(text="팟플레이어 실행을 기다리는 중...",
                                      fg=self.ACCENT)
                threading.Thread(target=self._wait_for_potplayer,
                                 daemon=True).start()
            else:
                self._start_processes()
        else:
            self._stop_processes()
            self._start_btn.config(text="▶ 시작",
                                   bg=self.BG3, fg=self.ACCENT,
                                   activebackground=self.BORDER,
                                   state="normal")
            self._proc_lbl.config(text="중지됨", fg=self.TEXT_DIM)
            self._start_oped_monitor()   # 싱크 정지 후 모니터 재시작
            threading.Thread(
                target=self._monitor_for_popup,
                kwargs={"wait_for_exit": True},
                daemon=True).start()

    # ── Windows 토스트 알림 ───────────────────────────────────────────────────
    @staticmethod
    def _register_app_id():
        try:
            key_path = r"SOFTWARE\Classes\AppUserModelId\LipSyncMonitor"
            key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path,
                                     0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "Auto Sinc")
            winreg.CloseKey(key)
        except Exception:
            pass

    @staticmethod
    def _toast(title: str, msg: str):
        try:
            from winotify import Notification, audio
            n = Notification(app_id="LipSyncMonitor",
                             title=title, msg=msg, duration="short")
            n.set_audio(audio.Default, loop=False)
            n.show()
            return
        except Exception:
            pass
        try:
            import win32gui, win32con
            hwnd = win32gui.FindWindow("Shell_TrayWnd", None)
            if hwnd:
                win32gui.Shell_NotifyIcon(win32con.NIM_MODIFY, (
                    hwnd, 0, win32con.NIF_INFO, win32con.WM_USER + 20,
                    None, msg, title, 5, win32con.NIIF_INFO
                ))
        except Exception:
            pass

    def _wait_for_potplayer(self):
        while True:
            hwnd = find_potplayer_hwnd()
            if hwnd:
                self._toast("🎬 Auto Sync",
                            "팟플레이어가 감지되었습니다.\n싱크 보정을 시작합니다.")
                self.root.after(0, self._start_processes)
                return
            time.sleep(0.5)

    def _monitor_for_popup(self, wait_for_exit=False):
        """싱크 OFF 상태에서 팟플레이어 재생 감지 시 시작 팝업 표시."""
        while not getattr(self, '_auth_ok', False):
            if self._closing: return
            time.sleep(0.1)

        if wait_for_exit:
            while not self._closing and not self._running:
                if not is_potplayer_running():
                    break
                for _ in range(10):
                    if self._closing or self._running: return
                    time.sleep(0.1)

        while not self._closing and not self._running:
            hwnd = find_potplayer_hwnd()
            if hwnd and is_potplayer_playing(hwnd) and is_potplayer_running():
                if self._closing or self._running:
                    return
                self._popup_open = True
                def _safe_show():
                    if not self._closing and not self._running:
                        self._show_start_popup()
                    else:
                        self._popup_open = False
                self._popup_after_id = self.root.after_idle(_safe_show)
                return
            for _ in range(10):
                if self._closing or self._running: return
                time.sleep(0.1)

    def _show_start_popup(self):
        """동영상 재생 감지 시 싱크 시작 여부 팝업."""
        try:
            if self._running or self._closing:
                self._popup_open = False
                return
            if not self.root.winfo_exists():
                return
        except Exception:
            return
        # 재생 감지 시 싱크 보정 탭으로 자동 전환
        if hasattr(self, "_switch_tab_fn"):
            self._switch_tab_fn("sync")

        popup = tk.Toplevel(self.root)
        popup.title("Auto Sync")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        popup.grab_set()
        # 위젯을 먼저 구성하고 마지막에 표시 → 깜빡임 방지
        popup.withdraw()

        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        pw = round(300 * r)
        ph = round(160 * r)

        tk.Label(popup, text="🎬  동영상 재생 감지됨",
                 font=("Segoe UI", max(9, round(10 * r)), "bold"),
                 bg=self.BG, fg=self.TEXT).pack(pady=(round(18*r), round(6*r)))
        tk.Label(popup,
                 text="팟플레이어에서 동영상이 재생됩니다.\n싱크 보정을 시작할까요?",
                 font=("Segoe UI", max(8, round(9 * r))),
                 bg=self.BG, fg=self.TEXT, justify="center").pack()

        # 팝업이 뜨는 시점에 창 제목에서 동영상 제목을 추출해 시청기록 저장
        # 기록 기준 1: 재생 감지 팝업 발생 시점에 시청 기록 저장
        # (title_watcher와 중복돼도 record_video_history 내부에서 타임스탬프 갱신만 함)
        try:
            import ctypes as _ct
            _u32 = _ct.windll.user32
            _buf = _ct.create_unicode_buffer(512)
            _hwnd = find_potplayer_hwnd()
            if _hwnd:
                _u32.GetWindowTextW(_hwnd, _buf, 512)
                _title = _extract_potplayer_title(_buf.value)
                if _title:
                    self.root.after(0, lambda t=_title: self.record_video_history(t))
        except Exception:
            pass

        btn_f = tk.Frame(popup, bg=self.BG, pady=round(16*r))
        btn_f.pack()

        def on_yes():
            self._popup_open = False
            popup.destroy()
            self._toggle()

        def on_no():
            self._popup_open = False
            popup.destroy()
            threading.Thread(
                target=self._monitor_for_popup,
                kwargs={"wait_for_exit": True},
                daemon=True).start()

        BTN = dict(font=("Consolas", max(8, round(8 * r)), "bold"), relief="flat",
                   cursor="hand2", padx=round(16*r), pady=round(6*r))
        tk.Button(btn_f, text="▶  시작",
                  bg=self.BG3, fg=self.ACCENT, activebackground=self.BORDER,
                  command=on_yes, **BTN).pack(side="left", padx=round(6*r))
        tk.Button(btn_f, text="무시",
                  bg=self.BG3, fg=self.TEXT, activebackground=self.BORDER,
                  command=on_no, **BTN).pack(side="left", padx=round(6*r))
        # 위젯 구성 완료 후 배치/표시
        self._place_popup(popup, pw, ph)

    def _start_processes(self):
        """P1·P2·P3 프로세스 시작."""
        self._stop_oped_monitor()   # 싱크 시작 시 별도 모니터 중지
        self._running = True
        self.stop_flag.value = False
        runtime_cfg = self._build_cfg()

        # GUI 메인스레드 → P3 공유 재생 위치/길이 (ms), -1 = 미확인
        self._shared_pos = Value(ctypes.c_longlong, -1)
        self._shared_dur = Value(ctypes.c_longlong, -1)

        # 오디오 스트림 기준점 — P2가 첫 패킷에서 기록, P1이 읽어서 타임스탬프 통일
        # [qp_origin, sr, freq] : 0이면 미확립
        self._stream_anchor = Array(ctypes.c_double, [0, 48000, 1])

        # 싱크 ON 상태 전용 로그 큐 (P2 → GUI 직접 전달)
        self._main_log_queue = Queue(maxsize=200)

        from processes import proc_lip_capture, proc_audio_capture, proc_analyzer  # lazy import
        for target, args in [
            (proc_lip_capture,   (self._lip_queue,   self.stop_flag, runtime_cfg,
                                  self._stream_anchor)),
            (proc_audio_capture, (self._audio_queue, self.stop_flag, runtime_cfg,
                                  self._main_log_queue, self._stream_anchor)),
            (proc_analyzer,      (self._lip_queue, self._audio_queue,
                                  self.state_queue, self.cmd_queue,
                                  self.stop_flag, runtime_cfg,
                                  self._shared_pos, self._shared_dur)),
        ]:
            p = Process(target=target, args=args, daemon=True)
            p.start()
            self._processes.append(p)

        self._start_btn.config(text="⏹ 정지",
                               bg=self.BG3, fg=self.ACCENT2,
                               activebackground=self.BORDER,
                               state="normal")
        self._proc_lbl.config(text="P1·P2·P3 실행 중", fg=self.ACCENT3)
        self._toast("🎬 Auto Sync", "싱크 보정이 시작되었습니다.")

    def _stop_processes(self):
        self._running = False
        self.stop_flag.value = True
        # P3(analyzer)에 stop 커맨드를 직접 전송해 ANALYSIS_INTERVAL 대기 없이 즉시 종료
        try:
            self.cmd_queue.put_nowait("stop")
        except Exception:
            pass
        # 병렬 join으로 대기 시간 단축 (timeout 1초로 단축)
        procs = list(self._processes)
        ts = [threading.Thread(target=lambda p=p: (p.join(timeout=1), p.terminate() if p.is_alive() else None), daemon=True)
              for p in procs]
        for t in ts: t.start()
        for t in ts: t.join()
        self._processes.clear()
        if hasattr(self, "_shared_pos"): self._shared_pos.value = -1
        if hasattr(self, "_shared_dur"): self._shared_dur.value = -1

    def _reset(self):
        if self._running:
            # 싱크 실행 중: P3에 reset + oped_reset 커맨드 전송
            try:
                self.cmd_queue.put_nowait("reset")
                self.cmd_queue.put_nowait("oped_reset")
            except Exception:
                pass
            # 즉시 드레인. oped_reset이 P3에서 처리되기 전에
            # _refresh()가 구버전 oped_prompt를 꺼내 팝업을 재호출하는
            # 타이밍 버그를 방지한다.
            try:
                while True:
                    self.state_queue.get_nowait()
            except Exception:
                pass
            return
        # 싱크 OFF: oped 모니터에 oped_reset 전송 + 팟플레이어 직접 초기화
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
        hwnd = find_potplayer_hwnd()
        if not hwnd:
            self._proc_lbl.config(text="초기화 실패: 팟플레이어 미감지", fg=self.ACCENT2)
            return
        try:
            post_key_to_potplayer(hwnd, VK_OEM_2, shift=True)
            time.sleep(0.05)
            post_key_to_potplayer(hwnd, 0x6F, shift=True)
            self._corr_lbl.config(text="+0 ms")
            self._proc_lbl.config(text="수동 초기화 완료", fg=self.ACCENT3)
        except Exception:
            self._proc_lbl.config(text="초기화 실패", fg=self.ACCENT2)

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
                    self._shared_pos.value = pv
                    self._shared_dur.value = dv
                if getattr(self, "_oped_monitor_running", False) and hasattr(self, "_om_shared_pos"):
                    self._om_shared_pos.value = pv
                    self._om_shared_dur.value = dv

        # oped 모니터 상태 진단 (매 30초마다)
        if time.time() - getattr(self, "_diag_t", 0) > 30:
            self._diag_t = time.time()
            running = getattr(self, "_oped_monitor_running", False)
            procs   = getattr(self, "_om_processes", [])
            alive   = [p.is_alive() for p in procs]
            import datetime as _dt
            msg = (f"[{_dt.datetime.now().strftime('%H:%M:%S')}] 🔧 oped_monitor={running} "
                   f"procs={len(procs)} alive={alive}")
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
                    seen = getattr(self, "_om_log_seen_count", 0)
                    for line in om_logs[seen:]:
                        self._log_lines.append(line)
                    self._om_log_seen_count = len(om_logs)
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
                self._proc_dot.config(fg=self.ACCENT)
                self._proc_lbl.config(text="OP/ED 감지 중", fg=self.ACCENT)

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
            # 마지막으로 본 줄 이후 새 항목만 추가 (set 비교 제거)
            if not hasattr(self, "_log_lines"):
                self._log_lines = collections.deque(maxlen=100)
            if logs:
                seen = getattr(self, "_log_seen_count", 0)
                for line in logs[seen:]:
                    self._log_lines.append(line)
                self._log_seen_count = len(logs)

        self.root.after(100, self._refresh)

    # ── 인증 ──────────────────────────────────────────────────────────────────
    def _destroy_app_root(self):
        try:
            if self.root.winfo_exists():
                self.root.destroy()
        except Exception:
            pass

    # ── OP/ED 스킵 팝업 ──────────────────────────────────────────────────────
    # P3가 oped_prompt를 state_queue에 실어 보내면 _refresh()가 호출
    # [스킵]              → "oped_skip"    → P3가 스킵 실행 + 쿨다운
    # [닫기] / 10초 경과  → "oped_no_skip" → P3가 쿨다운만 시작

    def _show_oped_skip_popup(self, prompt_info: dict, use_om_queue: bool = False):
        if getattr(self, "_oped_popup_open", False):
            return

        zone     = prompt_info.get("zone", "OP/ED")
        skip_sec = prompt_info.get("skip_sec", 90)

        hwnd = find_potplayer_hwnd()
        if not hwnd:
            return

        try:
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        except Exception:
            return

        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        pw = round(280 * r)
        ph = round(88  * r)
        # 멀티모니터: 가상 데스크탑 전체 범위로 클램프
        import ctypes as _ct
        vx = _ct.windll.user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        vy = _ct.windll.user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
        vw = _ct.windll.user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
        vh = _ct.windll.user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
        px = max(vx, min(rect.right  - pw - 12, vx + vw - pw))
        py = max(vy, min(rect.bottom - ph - 48, vy + vh - ph))

        self._oped_popup_open = True

        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.attributes("-topmost", False)   # owned window가 z-order 연동을 담당
        popup.configure(bg=self.BORDER)
        popup.geometry(f"{pw}x{ph}+{px}+{py}")
        popup.update_idletasks()

        # owner를 팟플레이어로 설정 → 팟플레이어가 뒤로 가면 팝업도 같이 뒤로 감
        _GWLP_HWNDPARENT = -8
        try:
            _ov_hwnd  = int(popup.wm_frame(), 16)
            _pot_hwnd = hwnd
            if _ov_hwnd and _pot_hwnd:
                try:
                    ctypes.windll.user32.SetWindowLongPtrW(_ov_hwnd, _GWLP_HWNDPARENT, _pot_hwnd)
                except AttributeError:
                    ctypes.windll.user32.SetWindowLongW(_ov_hwnd, _GWLP_HWNDPARENT, _pot_hwnd)
        except Exception:
            pass

        # 팟플레이어 이동 시 팝업 위치 동기화
        def _track_popup():
            try:
                if not popup.winfo_exists():
                    return
            except Exception:
                return
            _h = find_potplayer_hwnd()
            if _h:
                try:
                    _rc = ctypes.wintypes.RECT()
                    ctypes.windll.user32.GetWindowRect(_h, ctypes.byref(_rc))
                    _pw2 = popup.winfo_width() or pw
                    _ph2 = popup.winfo_height() or ph
                    _px2 = max(vx, min(_rc.right  - _pw2 - 12, vx + vw - _pw2))
                    _py2 = max(vy, min(_rc.bottom - _ph2 - 48, vy + vh - _ph2))
                    popup.geometry(f"+{_px2}+{_py2}")
                except Exception:
                    pass
            try:
                self.root.after(150, _track_popup)
            except Exception:
                pass

        self.root.after(150, _track_popup)

        def send_cmd(cmd: str):
            try:
                # oped 모니터 큐 또는 싱크 큐로 전송
                if use_om_queue and hasattr(self, "_om_cmd_queue"):
                    self._om_cmd_queue.put_nowait(cmd)
                else:
                    self.cmd_queue.put_nowait(cmd)
            except Exception:
                pass

        countdown = [10]
        after_id  = [None]

        def close_popup(skip: bool):
            self._oped_popup_open = False
            if after_id[0]:
                try: self.root.after_cancel(after_id[0])
                except Exception: pass
            send_cmd("oped_skip" if skip else "oped_no_skip")
            try: popup.destroy()
            except Exception: pass

        F_TITLE = max(8, round(9 * r))
        F_BTN   = max(7, round(8 * r))
        PAD     = round(10 * r)
        PAD_S   = round(6  * r)

        outer = tk.Frame(popup, bg=self.BORDER)
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        inner = tk.Frame(outer, bg=self.BG2, padx=PAD, pady=round(8 * r))
        inner.pack(fill="both", expand=True)

        lbl = tk.Label(inner,
                       text=f"🎵 {zone}을 스킵하시겠습니까? (10초)",
                       font=("Segoe UI", F_TITLE, "bold"),
                       bg=self.BG2, fg=self.TEXT, anchor="w")
        lbl.pack(fill="x")

        tk.Label(inner,
                 text=f"스킵 시 {skip_sec}초 앞으로 이동합니다.",
                 font=("Consolas", max(7, F_TITLE - 1)),
                 bg=self.BG2, fg=self.TEXT_MID, anchor="w").pack(fill="x", pady=(round(2*r), 0))

        btn_f = tk.Frame(inner, bg=self.BG2)
        btn_f.pack(anchor="e", pady=(PAD_S, 0))

        BTN = dict(font=("Consolas", F_BTN, "bold"), relief="flat", cursor="hand2",
                   padx=round(12*r), pady=round(3*r))

        tk.Button(btn_f, text="⏭ 스킵",
                  bg=self.BG3, fg=self.ACCENT, activebackground=self.BORDER,
                  command=lambda: close_popup(skip=True),
                  **BTN).pack(side="left", padx=(0, round(4*r)))
        tk.Button(btn_f, text="닫기",
                  bg=self.BG3, fg=self.TEXT_MID, activebackground=self.BORDER,
                  command=lambda: close_popup(skip=False),
                  **BTN).pack(side="left")

        def tick():
            countdown[0] -= 1
            if countdown[0] <= 0:
                close_popup(skip=False)
                return
            try:
                lbl.config(text=f"🎵 {zone}을 스킵하시겠습니까? ({countdown[0]}초)")
                after_id[0] = self.root.after(1000, tick)
            except Exception:
                close_popup(skip=False)

        after_id[0] = self.root.after(1000, tick)

    def _on_close(self):
        if getattr(self, "_app_shutdown_started", False):
            return
        self._app_shutdown_started = True
        self._closing = True
        self._popup_open = False
        if hasattr(self, "_popup_after_id"):
            try: self.root.after_cancel(self._popup_after_id)
            except Exception: pass
        # 열려 있는 모든 Toplevel 팝업 닫기
        try:
            for w in self.root.winfo_children():
                if isinstance(w, tk.Toplevel):
                    try: w.destroy()
                    except Exception: pass
        except Exception: pass
        # 오버레이 정리
        try:
            from gui.record_backend import _active_overlays
            for ov in list(_active_overlays):
                try: ov.destroy()
                except Exception: pass
            _active_overlays.clear()
        except Exception: pass
        self._save_pos()
        tray_ref = self._tray
        self._tray = None

        # ── 팟플레이어 종료: 백그라운드 스레드 시작 전, 메인스레드에서 즉시 전송 ──
        # daemon=True 스레드는 메인 프로세스 종료 시 강제 kill 되므로
        # _stop_processes() 완료를 기다리는 동안 앱이 먼저 닫혀 명령이 누락될 수 있음.
        # WM_CLOSE는 단순 Win32 호출이라 메인스레드에서 안전하게 선행 실행 가능.
        if getattr(self, "_close_pot_var", None) and self._close_pot_var.get():
            try:
                import ctypes as _ct
                hwnd = find_potplayer_hwnd()
                if hwnd:
                    WM_CLOSE = 0x0010
                    _ct.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass

        # 창은 즉시 파괴 → 사용자에게 즉각 반응
        # 프로세스 정리·tray 정지는 백그라운드에서 병렬 처리
        self._destroy_app_root()

        def _shutdown_bg():
            # _stop_processes와 _stop_oped_monitor를 병렬 실행해 대기 시간 절반으로 단축
            import threading as _th
            t1 = _th.Thread(target=self._stop_processes,   daemon=True)
            t2 = _th.Thread(target=self._stop_oped_monitor, daemon=True)
            t1.start(); t2.start()
            t1.join();  t2.join()
            if tray_ref:
                try: tray_ref.stop()
                except Exception: pass

        threading.Thread(target=_shutdown_bg, daemon=False, name="shutdown").start()
