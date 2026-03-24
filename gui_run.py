"""
gui_run.py -- 실행 제어, 프로세스 관리, 갱신, 인증 팝업 메서드
"""
import os
import time
import threading
import collections
import tkinter as tk
import winreg
from multiprocessing import Process, Queue, Value

import auth as _auth_module

from win32_utils import (
    CFG, find_potplayer_hwnd, is_potplayer_playing,
    is_potplayer_running, post_key_to_potplayer, VK_OEM_2
)
from processes import proc_lip_capture, proc_audio_capture, proc_analyzer


class LipSyncGUIRun:
    def _start_auto_skip_monitor(self):
        """싱크 미실행 상태에서 OP/ED 자동 스킵(P2+P3)만 백그라운드 실행."""
        if getattr(self, "_auto_skip_running", False):
            return
        try:
            runtime_cfg = self._build_cfg()
            qsize = runtime_cfg.get("QUEUE_MAXSIZE", 200)
            self._auto_lip_queue = Queue(maxsize=qsize)
            self._auto_audio_queue = Queue(maxsize=qsize)
            self._auto_state_queue = Queue(maxsize=20)
            self._auto_cmd_queue = Queue(maxsize=10)
            self._auto_stop_flag = Value("b", False)
            self._auto_processes = []
            for target, args in [
                (proc_audio_capture, (self._auto_audio_queue, self._auto_stop_flag, runtime_cfg)),
                (proc_analyzer, (self._auto_lip_queue, self._auto_audio_queue,
                                 self._auto_state_queue, self._auto_cmd_queue,
                                 self._auto_stop_flag, runtime_cfg)),
            ]:
                p = Process(target=target, args=args, daemon=True)
                p.start()
                self._auto_processes.append(p)
            self._auto_skip_running = True
        except Exception:
            self._auto_skip_running = False

    def _stop_auto_skip_monitor(self):
        """자동 스킵 전용 백그라운드(P2+P3) 중지."""
        if not getattr(self, "_auto_skip_running", False):
            return
        try:
            self._auto_stop_flag.value = True
            for p in self._auto_processes:
                p.join(timeout=2)
                if p.is_alive():
                    p.terminate()
        except Exception:
            pass
        self._auto_processes = []
        self._auto_skip_running = False


    # ── 시작 / 정지 ───────────────────────────────────────────────────────────
    def _toggle(self):
        if not self._running:
            self._stop_auto_skip_monitor()
            hwnd = find_potplayer_hwnd()
            if not hwnd:
                # 팟플레이어 미감지 → 대기 모드로 전환
                self._start_btn.config(text="⏳ 대기 중...",
                                       bg=self.BG3, fg=self.TEXT_DIM,
                                       activebackground=self.BORDER,
                                       state="disabled")
                self._proc_lbl.config(text="팟플레이어 실행을 기다리는 중...",
                                      fg=self.ACCENT)
                # 백그라운드 스레드에서 감지될 때까지 대기
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
            # 정지 후 팟플레이어 재실행 감지 모니터 재시작
            threading.Thread(
                target=self._monitor_for_popup,
                kwargs={"wait_for_exit": True},
                daemon=True).start()

    # ── Windows 토스트 알림 ───────────────────────────────────────────────────
    @staticmethod
    def _register_app_id():
        """
        Windows 10 토스트 알림을 위한 앱 ID 레지스트리 등록.
        HKCU\\SOFTWARE\\Classes\\AppUserModelId\\LipSyncMonitor
        Windows 11은 없어도 동작하지만 Windows 10은 필수.
        """
        try:
            key_path = r"SOFTWARE\Classes\AppUserModelId\LipSyncMonitor"
            key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path,
                                     0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "DisplayName", 0,
                              winreg.REG_SZ, "Auto Sync")
            winreg.CloseKey(key)
        except Exception:
            pass

    @staticmethod
    def _toast(title: str, msg: str):
        """
        Windows 10/11 토스트 알림 표시.
        winotify 우선 → 없으면 win32api 풍선 도움말로 폴백.
        """
        try:
            from winotify import Notification, audio
            n = Notification(app_id="LipSyncMonitor",
                             title=title,
                             msg=msg,
                             duration="short")
            n.set_audio(audio.Default, loop=False)
            n.show()
            return
        except Exception:
            pass
        # 폴백: 트레이 버블 알림 (구형 방식 / winotify 없을 때)
        try:
            import win32gui, win32con
            hwnd = win32gui.FindWindow("Shell_TrayWnd", None)
            if hwnd:
                win32gui.Shell_NotifyIcon(win32con.NIM_MODIFY, (
                    hwnd, 0,
                    win32con.NIF_INFO,
                    win32con.WM_USER + 20,
                    None,
                    msg, title, 5,
                    win32con.NIIF_INFO
                ))
        except Exception:
            pass

    def _wait_for_potplayer(self):
        """팟플레이어가 감지될 때까지 0.5초마다 확인. 감지되면 알림 + 자동 시작."""
        while True:
            hwnd = find_potplayer_hwnd()
            if hwnd:
                # 팟플레이어 감지 알림
                self._toast("🎬 Auto Sync",
                            "팟플레이어가 감지되었습니다.\n싱크 보정을 시작합니다.")
                # UI 업데이트는 메인 스레드에서
                self.root.after(0, self._start_processes)
                return
            time.sleep(0.5)

    def _monitor_for_popup(self, wait_for_exit=False):
        """
        자동 시작 OFF 상태에서 팝업 모니터링.
        """
        # 인증이 완료되지 않은 상태면 대기
        while not getattr(self, '_auth_ok', False):
            if self._closing: return
            time.sleep(0.1)
        # 무시 후: 팟플레이어가 완전히 종료될 때까지 대기
        if wait_for_exit:
            while not self._closing and not self._running:
                if not is_potplayer_running():
                    break
                for _ in range(10):
                    if self._closing or self._running: return
                    time.sleep(0.1)

        # 팟플레이어 켜짐 + 비디오 재생 감지 대기
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
        """
        동영상 재생 감지 시 팝업.
        자동 시작 OFF일 때만 호출됨.
        """
        try:
            if self._running or self._closing:
                self._popup_open = False
                return
            if not self.root.winfo_exists():
                return
        except Exception:
            return

        popup = tk.Toplevel(self.root)
        popup.title("Auto Sync")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        popup.grab_set()

        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        pw = round(300 * r)
        ph = round(160 * r)
        self._place_popup(popup, pw, ph)

        tk.Label(popup, text="🎬  동영상 재생 감지됨",
                 font=("Segoe UI", max(9, round(10 * r)), "bold"),
                 bg=self.BG, fg=self.TEXT).pack(pady=(round(18*r), round(6*r)))
        tk.Label(popup,
                 text="팟플레이어에서 동영상이 재생됩니다.\n싱크 보정을 시작할까요?",
                 font=("Segoe UI", max(8, round(9 * r))),
                 bg=self.BG, fg=self.TEXT,
                 justify="center").pack()

        btn_f = tk.Frame(popup, bg=self.BG, pady=round(16*r))
        btn_f.pack()

        def on_yes():
            self._popup_open = False
            popup.destroy()
            self._toggle()

        def on_no():
            self._popup_open = False
            popup.destroy()
            # 무시 후: 팟플레이어 종료 대기 → 재실행 감지 후 팝업
            threading.Thread(
                target=self._monitor_for_popup,
                kwargs={"wait_for_exit": True},
                daemon=True).start()

        BTN = dict(font=("Consolas", max(8, round(8 * r)), "bold"), relief="flat",
                   cursor="hand2", padx=round(16*r), pady=round(6*r))
        tk.Button(btn_f, text="▶  시작",
                  bg=self.BG3, fg=self.ACCENT,
                  activebackground=self.BORDER,
                  command=on_yes, **BTN).pack(side="left", padx=round(6*r))
        tk.Button(btn_f, text="무시",
                  bg=self.BG3, fg=self.TEXT,
                  activebackground=self.BORDER,
                  command=on_no, **BTN).pack(side="left", padx=round(6*r))

    def _start_processes(self):
        """프로세스 시작 (팟플레이어 감지 확인 후 호출)."""
        self._stop_auto_skip_monitor()
        self._running = True
        self.stop_flag.value = False
        runtime_cfg = self._build_cfg()
        for target, args in [
            (proc_lip_capture,   (self._lip_queue,   self.stop_flag, runtime_cfg)),
            (proc_audio_capture, (self._audio_queue, self.stop_flag, runtime_cfg)),
            (proc_analyzer,      (self._lip_queue, self._audio_queue,
                                  self.state_queue, self.cmd_queue,
                                  self.stop_flag, runtime_cfg)),
        ]:
            p = Process(target=target, args=args, daemon=True)
            p.start()
            self._processes.append(p)
        self._start_btn.config(text="⏹ 정지",
                               bg=self.BG3, fg=self.ACCENT2,
                               activebackground=self.BORDER,
                               state="normal")
        self._proc_lbl.config(
            text="P1·P2·P3 실행 중",
            fg=self.ACCENT3)
        self._toast("🎬 Auto Sync", "싱크 보정이 시작되었습니다.")

    def _stop_processes(self):
        self._running = False
        self.stop_flag.value = True
        for p in self._processes:
            p.join(timeout=2)
            if p.is_alive(): p.terminate()
        self._processes.clear()

    def _reset(self):
        if getattr(self, "_auto_skip_running", False):
            try:
                self._auto_cmd_queue.put_nowait("reset")
            except Exception:
                pass
            return

        if self._running:
            try:
                self.cmd_queue.put_nowait("reset")
            except Exception:
                pass
            return

        # 싱크 프로세스가 꺼져 있어도 팟플레이어 쪽 초기화는 즉시 적용
        hwnd = find_potplayer_hwnd()
        if not hwnd:
            self._proc_lbl.config(text="초기화 실패: 팟플레이어 미감지", fg=self.ACCENT2)
            return

        try:
            post_key_to_potplayer(hwnd, VK_OEM_2, shift=True)
            time.sleep(0.05)
            post_key_to_potplayer(hwnd, 0x6F, shift=True)
            self._proc_lbl.config(text="수동 초기화 완료", fg=self.ACCENT3)
        except Exception:
            self._proc_lbl.config(text="초기화 실패", fg=self.ACCENT2)

    # ── 100ms 주기 UI 갱신 ────────────────────────────────────────────────────
    def _refresh(self):
        want_auto_skip = (not self._running) and self._oped_auto_var.get()
        if want_auto_skip and not getattr(self, "_auto_skip_running", False):
            self._start_auto_skip_monitor()
        elif (not want_auto_skip) and getattr(self, "_auto_skip_running", False):
            self._stop_auto_skip_monitor()

        if getattr(self, "_auto_skip_running", False):
            auto_latest = None
            while True:
                try:
                    auto_latest = self._auto_state_queue.get_nowait()
                except Exception:
                    break
            if auto_latest:
                logs = auto_latest.get("log_lines", [])
                self._log_lines = collections.deque(logs, maxlen=100)

        latest = None
        while True:
            try: latest = self.state_queue.get_nowait()
            except Exception: break

        if latest:
            pot_ok  = latest.get("potplayer_ok", False)
            aud_n   = latest.get("audio_samples", 0)
            lip_n   = latest.get("lip_samples", 0)
            offset  = latest.get("offset_ms", 0.0)
            status  = latest.get("status", "대기 중")
            corr    = latest.get("correction_ms", 0)
            logs    = latest.get("log_lines", [])
            notify  = latest.get("notify", None)

            # 알림 팝업 (P3에서 요청 시)
            if notify:
                threading.Thread(
                    target=self._toast,
                    args=(notify[0], notify[1]),
                    daemon=True).start()

            c = self.ACCENT3 if pot_ok else self.ACCENT2
            t = "연결됨" if pot_ok else "미감지"
            self._pot_dot.config(fg=c); self._pot_lbl.config(text=t, fg=c)

            c = self.ACCENT3 if aud_n > 0 else self.TEXT_DIM
            t = "캡처 중" if aud_n > 0 else "대기 중"
            self._aud_dot.config(fg=c); self._aud_lbl.config(text=t, fg=c)

            if self._running and lip_n > 0:
                sign = "+" if offset > 0 else ""
                col  = (self.ACCENT2  if abs(offset) >= 80
                        else self.ACCENT3 if abs(offset) < 30
                        else self.ACCENT)
                self._offset_lbl.config(text=f"{sign}{offset:.0f} ms", fg=col)
            else:
                self._offset_lbl.config(text="— ms", fg=self.ACCENT)

            self._bar_ref.update_idletasks()
            bw    = self._bar_ref.winfo_width()
            ratio = min(abs(offset) / 500, 1.0)
            col   = self.ACCENT2 if abs(offset) >= 80 else self.ACCENT3
            self._bar.place(x=0, y=0, width=int(bw * ratio), height=4)
            self._bar.config(bg=col)

            badge_map = {
                "정상":              (self.ACCENT3,  self.BG3),
                "보정 완료":         (self.ACCENT,   self.BG3),
                "팟플레이어 미감지": (self.ACCENT2,  self.BG3),
                "데이터 수집 중":    (self.TEXT,     self.BG3),
                "대기 중":           (self.TEXT,     self.BG3),
            }
            fg, bg = badge_map.get(status, (self.TEXT, self.BG3))
            self._badge.config(text=f"  {status}  ", fg=fg, bg=bg)

            sign = "+" if corr >= 0 else ""
            self._corr_lbl.config(text=f"{sign}{corr} ms")
            self._lip_cnt.config(text=str(lip_n))
            self._aud_cnt.config(text=str(aud_n))
            # 프로세스 점 색상 업데이트
            pc = self.ACCENT3 if self._running else self.TEXT_DIM
            self._proc_dot.config(fg=pc)
            # 전체 로그를 _log_lines에 저장 (로그 팝업용, 최대 100줄 FIFO)
            self._log_lines = collections.deque(logs, maxlen=100)

        self.root.after(100, self._refresh)

    # ── 인증 ──────────────────────────────────────────────────────────────────
    def _on_close(self):
        self._closing = True
        self._popup_open = False
        # 예약된 팝업 콜백 취소
        if hasattr(self, "_popup_after_id"):
            try: self.root.after_cancel(self._popup_after_id)
            except Exception: pass
        self._save_pos()
        self._stop_processes()
        self._stop_auto_skip_monitor()
        if self._tray:
            try: self._tray.stop()
            except Exception: pass
        try: self.root.destroy()
        except Exception: pass
