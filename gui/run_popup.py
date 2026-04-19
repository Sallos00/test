"""
gui/run_popup.py -- 팟플레이어 감지·시작 팝업 Mixin
_toggle, _wait_for_potplayer, _monitor_for_popup, _show_start_popup
"""
import time
import threading
import tkinter as tk

from win32_utils import find_potplayer_hwnd, is_potplayer_playing, is_potplayer_running
from gui.ui_logic import _extract_potplayer_title


class PopupMixin:

    def _toggle(self):
        if not self._running:
            self._stop_oped_monitor()   # 싱크 시작 전 모니터 중지 (중복 방지)
            # ── [버그2 수정] on->off->on 시 잔류 커맨드/상태 제거 ──────────
            # stop_flag는 _start_processes에서 clear()하지만, cmd_queue에
            # 이전 "stop" 커맨드가 남아 있으면 T3가 즉시 종료돼 싱크가 시작 안됨.
            try:
                while True: self.cmd_queue.get_nowait()
            except Exception:
                pass
            try:
                while True: self.state_queue.get_nowait()
            except Exception:
                pass
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

        countdown      = [10]
        auto_close_id  = [None]

        def on_yes():
            if auto_close_id[0]:
                try: self.root.after_cancel(auto_close_id[0])
                except Exception: pass
            self._popup_open = False
            popup.destroy()
            self._toggle()

        def on_no():
            if auto_close_id[0]:
                try: self.root.after_cancel(auto_close_id[0])
                except Exception: pass
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
        ignore_btn = tk.Button(btn_f, text=f"무시 (10)",
                  bg=self.BG3, fg=self.TEXT, activebackground=self.BORDER,
                  command=on_no, **BTN)
        ignore_btn.pack(side="left", padx=round(6*r))

        def _tick():
            countdown[0] -= 1
            if countdown[0] <= 0:
                on_no()
                return
            try:
                ignore_btn.config(text=f"무시 ({countdown[0]})")
                auto_close_id[0] = self.root.after(1000, _tick)
            except Exception:
                pass

        auto_close_id[0] = self.root.after(1000, _tick)
        # 위젯 구성 완료 후 배치/표시
        self._place_popup(popup, pw, ph)
