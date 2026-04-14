"""
gui/base.py -- LipSyncGUI 기본 클래스
테마, 설정 저장/불러오기, 스케일, 트레이 아이콘
"""
import os
import json
import sys
import threading
import traceback
import winreg
import tkinter as tk
import tkinter.messagebox as mb

from app_icon import apply_to_toplevel, pil_image_for_tray
from win32_utils import find_potplayer_hwnd, CFG


class LipSyncGUIBase:

    DARK = dict(
        BG="#0e0e0e", BG2="#161616", BG3="#1e1e1e",
        BORDER="#2a2a2a", TEXT="#e8e8e8",
        TEXT_DIM="#555555", TEXT_MID="#888888",
    )
    LIGHT = dict(
        BG="#f5f5f5", BG2="#e8e8e8", BG3="#d8d8d8",
        BORDER="#bbbbbb", TEXT="#111111",
        TEXT_DIM="#666666", TEXT_MID="#333333",
    )
    ACCENT  = "#00c8e0"
    ACCENT2 = "#e03c3c"
    ACCENT3 = "#5ec44a"
    W, H = 340, 480
    SCALES = {
        "소": dict(w=340, h=500, scale=1.0),
        "중": dict(w=408, h=570, scale=1.2),
        "대": dict(w=476, h=660, scale=1.4),
    }
    APP_DIR      = os.path.join(os.environ.get("APPDATA", ""), "AutoSync")
    CFG_FILE     = os.path.join(APP_DIR, "settings.json")
    STARTUP_REG  = r"Software\Microsoft\Windows\CurrentVersion\Run"
    STARTUP_NAME = "AutoSync"

    def __init__(self, root: tk.Tk, state_queue, cmd_queue, stop_flag,
                 lip_queue=None, audio_queue=None):
        self.root          = root
        self.state_queue   = state_queue
        self.cmd_queue     = cmd_queue
        self.stop_flag     = stop_flag
        self._lip_queue    = lip_queue
        self._audio_queue  = audio_queue
        self._running      = False
        self._closing      = False
        self._auth_ok      = False
        self._popup_open   = False
        self._gear_menu_open  = False
        self._gear_menu_frame = None
        self._popup_after_id  = None
        self._processes    = []
        self._tray         = None
        self._tray_thread  = None
        # ── [버그1 수정] _pot_was_ok 명시 초기화 ────────────────────────────
        # getattr(self, "_pot_was_ok", False) 에 의존하면 첫 _refresh에서 항상
        # False→False가 되어 팟플레이어 종료 감지(탭 전환)가 동작하지 않음.
        # None으로 초기화해 첫 poll 시에는 탭 전환을 건너뛰고 기준점만 설정.
        self._pot_was_ok   = None
        self._startup_var   = tk.BooleanVar(value=self._is_startup_registered())
        self._autostart_var = tk.BooleanVar(value=self._load_setting("autostart",  False))
        self._darkmode_var  = tk.BooleanVar(value=self._load_setting("darkmode",   True))
        self._scale_var     = tk.StringVar( value=self._load_setting("scale",      "중"))
        self._oped_auto_var     = tk.BooleanVar(
            value=self._load_setting("oped_auto_skip", False))
        self._oped_skip_sec_var = tk.StringVar(
            value=str(self._load_setting("oped_skip_sec", 90)))
        self._close_pot_var     = tk.BooleanVar(
            value=self._load_setting("close_potplayer_on_exit", False))
        self._apply_scale()
        self._apply_theme()
        self._build_window()
        self._build_ui()
        self._refresh()
        self.root.after(0, self._ensure_settings_file)
        self.root.after(0, self._check_auth_on_start)
        # 무거운 초기화는 창이 뜬 뒤 백그라운드/지연 처리
        self.root.after(0,  self._register_app_id)   # winreg 쓰기 → 첫 틱으로 지연
        self.root.after(50, self._setup_tray)         # PIL 이미지 빌드 포함

    def _build_cfg(self):
        try:
            skip_sec = max(10, min(600, int(self._oped_skip_sec_var.get())))
        except (ValueError, AttributeError):
            skip_sec = 90
        cfg = dict(CFG)
        cfg["OPED_AUTO_SKIP"] = self._oped_auto_var.get()
        cfg["OPED_SKIP_SEC"]  = skip_sec
        return cfg

    def _apply_scale(self):
        s = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])
        r = s["scale"]
        self.W        = s["w"]
        self.H        = s["h"]
        self.F_TITLE  = max(8,  round(13 * r))
        self.F_MONO   = max(7,  round(9  * r))
        self.F_MONO_S = max(7,  round(8  * r))
        self.F_OFFSET = max(16, round(32 * r))
        self.F_GEAR   = max(8,  round(10 * r))

    def _toggle_scale(self, size):
        self._scale_var.set(size)
        self._apply_scale()
        self._save_settings()
        if self._tray:
            try: self._tray.stop()
            except Exception: pass
        self._tray = None
        for w in self.root.winfo_children():
            w.destroy()
        self._theme_widgets = []
        self._hist_row_cache = []   # destroy된 위젯 참조 초기화
        self._poll_started   = False  # 폴링 루프 중복 방지 초기화
        self.root.geometry(f"{self.W}x{self.H}")
        self.root.configure(bg=self.BG)
        self._build_ui()
        self._setup_tray()

    def _apply_theme(self):
        t = self.DARK if self._darkmode_var.get() else self.LIGHT
        self.BG       = t["BG"]
        self.BG2      = t["BG2"]
        self.BG3      = t["BG3"]
        self.BORDER   = t["BORDER"]
        self.TEXT     = t["TEXT"]
        self.TEXT_DIM = t["TEXT_DIM"]
        self.TEXT_MID = t["TEXT_MID"]

    def _toggle_darkmode(self):
        self._apply_theme()
        self._save_settings()
        self.root.configure(bg=self.BG)
        COLOR = {
            "BG": self.BG, "BG2": self.BG2, "BG3": self.BG3,
            "BORDER": self.BORDER, "TEXT": self.TEXT,
            "TEXT_DIM": self.TEXT_DIM, "TEXT_MID": self.TEXT_MID,
            "ACCENT": self.ACCENT, "ACCENT2": self.ACCENT2,
            "GEAR_FG": self.ACCENT if self._darkmode_var.get() else self.TEXT,
        }
        try:
            gear_fg = self.ACCENT if self._darkmode_var.get() else self.TEXT
            self._gear_btn.config(fg=gear_fg, activeforeground=gear_fg)
        except Exception:
            pass
        for item in self._theme_widgets:
            w, bg, fg, abg, afg, obg = item
            try:
                kw = {}
                if bg:  kw["bg"]                = COLOR.get(bg,  bg)
                if fg:  kw["fg"]                = COLOR.get(fg,  fg)
                if abg: kw["activebackground"]  = COLOR.get(abg, abg)
                if afg: kw["activeforeground"]  = COLOR.get(afg, afg)
                if obg: kw["highlightbackground"] = COLOR.get(obg, obg)
                if kw: w.config(**kw)
            except Exception:
                pass
        try:
            ic_size = round(32 * self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"])
            self._icon_canvas.config(bg=self.BG)
            self._icon_canvas.delete("all")
            self._icon_canvas.create_oval(1, 1, ic_size-1, ic_size-1,
                                          fill=self.BG3, outline=self.ACCENT, width=2)
            r2 = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
            self._icon_canvas.create_polygon(
                round(12*r2), round(8*r2),
                round(12*r2), round(24*r2),
                round(26*r2), round(16*r2),
                fill=self.ACCENT, outline="")
        except Exception:
            pass
        try:
            self._update_oped_btn()
        except Exception:
            pass

    def _ensure_settings_file(self):
        try:
            if not os.path.exists(self.CFG_FILE):
                self._save_settings()
        except Exception:
            pass

    def _load_setting(self, key, default):
        try:
            with open(self.CFG_FILE, "r") as f:
                return json.load(f).get(key, default)
        except Exception:
            return default

    def _load_pos(self):
        import ctypes as _ct
        vx = _ct.windll.user32.GetSystemMetrics(76)
        vy = _ct.windll.user32.GetSystemMetrics(77)
        vw = _ct.windll.user32.GetSystemMetrics(78)
        vh = _ct.windll.user32.GetSystemMetrics(79)
        try:
            with open(self.CFG_FILE, "r") as f:
                data = json.load(f)
            x = max(vx, min(int(data["x"]), vx + vw - self.W))
            y = max(vy, min(int(data["y"]), vy + vh - self.H))
            return x, y
        except Exception:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            return (sw - self.W) // 2, (sh - self.H) // 2

    def _save_settings(self):
        try:
            os.makedirs(self.APP_DIR, exist_ok=True)
            existing = {}
            try:
                with open(self.CFG_FILE, "r") as f:
                    existing = json.load(f)
            except Exception:
                pass
            try:
                skip_sec = max(10, min(600, int(self._oped_skip_sec_var.get())))
            except (ValueError, AttributeError):
                skip_sec = 90
            existing.update({
                "x":              self.root.winfo_x(),
                "y":              self.root.winfo_y(),
                "autostart":      self._autostart_var.get(),
                "darkmode":       self._darkmode_var.get(),
                "scale":          self._scale_var.get(),
                "oped_auto_skip": self._oped_auto_var.get(),
                "oped_skip_sec":  skip_sec,
                "close_potplayer_on_exit": self._close_pot_var.get(),
                "pip_on":         getattr(self, "_pip_on", False),
                "record_save_dir": getattr(self, "_record_save_dir", None) or existing.get("record_save_dir", ""),
                "history_video_dir": getattr(self, "_hist_video_dir", None) or existing.get("history_video_dir", ""),
            })
            with open(self.CFG_FILE, "w") as f:
                json.dump(existing, f)
        except Exception:
            pass

    def _save_pos(self): self._save_settings()

    def _place_popup(self, popup, pw, ph):
        # transient 설정: 팝업 클릭 시 메인창이 함께 앞으로 올라옴
        popup.transient(self.root)
        # 화면 밖에서 렌더링 완료 후 이동 -> 깜빡임 방지
        popup.withdraw()
        popup.geometry(f"{pw}x{ph}+-9999+-9999")
        popup.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width()  - pw) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - ph) // 2
        import ctypes as _ct
        vx = _ct.windll.user32.GetSystemMetrics(76)
        vy = _ct.windll.user32.GetSystemMetrics(77)
        vw = _ct.windll.user32.GetSystemMetrics(78)
        vh = _ct.windll.user32.GetSystemMetrics(79)
        x = max(vx, min(x, vx + vw - pw))
        y = max(vy, min(y, vy + vh - ph))
        popup.geometry(f"{pw}x{ph}+{x}+{y}")
        try:
            apply_to_toplevel(popup, self.root)
        except Exception:
            pass
        popup.deiconify()

    def _is_startup_registered(self):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.STARTUP_REG)
            winreg.QueryValueEx(key, self.STARTUP_NAME)
            winreg.CloseKey(key)
            return True
        except Exception:
            return False

    def _toggle_startup(self):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 self.STARTUP_REG, 0, winreg.KEY_SET_VALUE)
            if self._startup_var.get():
                exe_path = (sys.executable
                            if getattr(sys, "frozen", False)
                            else os.path.abspath(__file__))
                winreg.SetValueEx(key, self.STARTUP_NAME, 0,
                                  winreg.REG_SZ, f'"{exe_path}"')
            else:
                try: winreg.DeleteValue(key, self.STARTUP_NAME)
                except Exception: pass
            winreg.CloseKey(key)
        except Exception as e:
            mb.showerror("오류", f"시작프로그램 설정 실패:\n{e}")
            self._startup_var.set(not self._startup_var.get())

    def _setup_tray(self):
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self.root.bind("<Unmap>", self._on_root_unmap)
        try:
            import pystray
            self._tray_run_error = None
            if self._tray:
                try: self._tray.stop()
                except Exception: pass
            self._tray = None
            tray_uid = "AutoSync.%s" % (os.getpid(),)

            def tray_toggle_sync(icon, item):
                self.root.after(0, self._toggle)

            def tray_sync_label(item):
                return "⏹ 싱크 중지" if self._running else "▶ 싱크 시작"

            def tray_toggle_auto_skip(icon, item):
                enabled = bool(self._oped_auto_var.get())
                self._oped_auto_var.set(not enabled)
                self._save_settings()
                if hasattr(self, "_start_auto_skip_monitor") and hasattr(self, "_stop_auto_skip_monitor"):
                    if not self._running:
                        self.root.after(0, self._stop_auto_skip_monitor)
                        self.root.after(80, self._start_auto_skip_monitor)
                if hasattr(self, "_update_oped_btn"):
                    self.root.after(0, self._update_oped_btn)

            def tray_auto_skip_label(item):
                return "⏹ 자동스킵 중지" if self._oped_auto_var.get() else "▶ 자동스킵 시작"

            menu = pystray.Menu(
                pystray.MenuItem("Auto Sync 열기", self._tray_show, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(tray_sync_label, tray_toggle_sync),
                pystray.MenuItem(tray_auto_skip_label, tray_toggle_auto_skip),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("종료", self._tray_quit),
            )

            # PIL 이미지 빌드 + tray.run()을 모두 백그라운드 스레드에서 실행
            # → 메인스레드(창 표시)를 블로킹하지 않음
            def _run_tray():
                try:
                    if sys.platform == "win32":
                        try:
                            import pythoncom
                            pythoncom.CoInitialize()
                        except Exception:
                            pass
                    img = pil_image_for_tray(64)   # ICO 빌드 비용을 스레드 안으로 이동
                    icon = pystray.Icon(tray_uid, img, "Auto Sync", menu)
                    self._tray = icon
                    icon.run()
                except Exception:
                    self._tray_run_error = traceback.format_exc()[-900:]
                    self._tray = None
                finally:
                    if sys.platform == "win32":
                        try:
                            import pythoncom
                            pythoncom.CoUninitialize()
                        except Exception:
                            pass

            self._tray_thread = threading.Thread(
                target=_run_tray, daemon=False, name="pystray")
            self._tray_thread.start()

        except ImportError as e:
            self._tray = None
            self._tray_run_error = (
                "pystray/Pillow 로드 실패: %s" % e)
        except Exception:
            self._tray = None
            self._tray_run_error = traceback.format_exc()[-900:]

    def _hide_to_tray(self):
        self._save_pos()
        if not self._tray:
            if not getattr(self, "_tray_retry_done", False):
                self._tray_retry_done = True
                self._setup_tray()
            if not self._tray:
                if not getattr(self, "_tray_warned", False):
                    self._tray_warned = True
                    err = getattr(self, "_tray_run_error", None) or "알 수 없음"
                    self.root.after(
                        0,
                        lambda: mb.showwarning(
                            "트레이",
                            "알림 영역(트레이) 아이콘을 만들 수 없습니다.\n"
                            "창은 작업 표시줄로 최소화됩니다.\n(%s)" % err,
                        ),
                    )
                try:
                    self.root.iconify()
                except Exception:
                    self.root.deiconify()
                return
        self.root.withdraw()

    def _unmap_maybe_minimize_to_tray(self):
        try:
            if getattr(self, "_closing", False):
                return
            if not self.root.winfo_exists():
                return
            st = str(self.root.state())
            if st == "withdrawn":
                return
            if st in ("iconic", "iconified"):
                self._hide_to_tray()
        except Exception:
            pass

    def _on_root_unmap(self, event):
        try:
            if event.widget is not self.root:
                return
            if getattr(self, "_closing", False):
                return
            st = str(self.root.state())
            if st == "withdrawn":
                return
            if st in ("iconic", "iconified"):
                self.root.after_idle(self._hide_to_tray)
                return
            self.root.after(40, self._unmap_maybe_minimize_to_tray)
        except Exception:
            pass
