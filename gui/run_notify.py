"""
gui/run_notify.py -- 알림·종료 Mixin
_register_app_id, _toast, _destroy_app_root, _on_close
"""
import threading
import winreg

from win32_utils import find_potplayer_hwnd


class NotifyMixin:

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

    def _destroy_app_root(self):
        try:
            if self.root.winfo_exists():
                self.root.destroy()
        except Exception:
            pass

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
            import tkinter as tk
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
            t1 = threading.Thread(target=self._stop_processes,    daemon=True)
            t2 = threading.Thread(target=self._stop_oped_monitor, daemon=True)
            t1.start(); t2.start()
            t1.join();  t2.join()
            if tray_ref:
                try: tray_ref.stop()
                except Exception: pass

        threading.Thread(target=_shutdown_bg, daemon=False, name="shutdown").start()
