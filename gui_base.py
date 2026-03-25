"""

gui_base.py -- LipSyncGUI 기본 클래스

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

    # 다크 테마

    DARK = dict(

        BG="#0e0e0e", BG2="#161616", BG3="#1e1e1e",

        BORDER="#2a2a2a", TEXT="#e8e8e8",

        TEXT_DIM="#555555", TEXT_MID="#888888",

    )

    # 라이트 테마

    LIGHT = dict(

        BG="#f5f5f5", BG2="#e8e8e8", BG3="#d8d8d8",

        BORDER="#bbbbbb", TEXT="#111111",

        TEXT_DIM="#666666", TEXT_MID="#333333",

    )

    ACCENT  = "#00c8e0"

    ACCENT2 = "#e03c3c"

    ACCENT3 = "#5ec44a"

    W, H = 340, 470

    SCALES = {

        "소": dict(w=340, h=470, scale=1.0),

        "중": dict(w=408, h=528, scale=1.2),

        "대": dict(w=476, h=616, scale=1.4),

    }

    APP_DIR   = os.path.join(os.environ.get("APPDATA", ""), "AutoSync")

    CFG_FILE  = os.path.join(APP_DIR, "settings.json")

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

        self._startup_var   = tk.BooleanVar(value=self._is_startup_registered())

        self._autostart_var = tk.BooleanVar(value=self._load_setting("autostart",  False))

        self._darkmode_var  = tk.BooleanVar(value=self._load_setting("darkmode",   True))

        self._scale_var     = tk.StringVar( value=self._load_setting("scale",      "중"))

        # ── OP/ED 자동 스킵 설정 ────────────────────────────────────────────
        self._oped_auto_var     = tk.BooleanVar(
            value=self._load_setting("oped_auto_skip", False))

        self._oped_skip_sec_var = tk.StringVar(
            value=str(self._load_setting("oped_skip_sec", 90)))

        self._apply_scale()

        self._apply_theme()

        self._register_app_id()

        self._build_window()

        self._build_ui()

        self._setup_tray()

        self._refresh()

        self.root.after(0, self._check_auth_on_start)

    # ── CFG 빌더: win32_utils.CFG + 런타임 GUI 설정값을 합쳐서 반환 ──────────
    def _build_cfg(self):

        """
        프로세스 시작 시 전달할 CFG dict를 반환한다.
        win32_utils.CFG 기본값에 현재 GUI 설정을 덮어씌운다.
        """

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

        self.W       = s["w"]

        self.H       = s["h"]

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

        def update_widget_colors(widget):

            try:

                wtype = widget.winfo_class()

                if wtype == "Toplevel":

                    widget.config(bg=self.BG)

                elif wtype == "Frame":

                    widget.config(bg=self.BG2)

                elif wtype == "Label":

                    widget.config(bg=self.BG, fg=self.TEXT)

                elif wtype == "Checkbutton":

                    widget.config(bg=self.BG2, fg=self.TEXT,

                                  selectcolor=self.BG3,

                                  activebackground=self.BG2,

                                  activeforeground=self.TEXT)

                elif wtype == "Button":

                    widget.config(bg=self.BG3, fg=self.TEXT,

                                  activebackground=self.BORDER)

            except Exception:

                pass

            for child in widget.winfo_children():

                update_widget_colors(child)

        for toplevel in self.root.winfo_children():

            if isinstance(toplevel, tk.Toplevel):

                try:

                    toplevel.configure(bg=self.BG)

                    update_widget_colors(toplevel)

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

    def _load_setting(self, key, default):

        try:

            with open(self.CFG_FILE, "r") as f:

                return json.load(f).get(key, default)

        except Exception:

            return default

    def _load_pos(self):

        sw = self.root.winfo_screenwidth()

        sh = self.root.winfo_screenheight()

        try:

            with open(self.CFG_FILE, "r") as f:

                data = json.load(f)

            x = max(0, min(int(data["x"]), sw - self.W))

            y = max(0, min(int(data["y"]), sh - self.H))

            return x, y

        except Exception:

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

                # OP/ED 자동 스킵 설정

                "oped_auto_skip": self._oped_auto_var.get(),

                "oped_skip_sec":  skip_sec,

            })

            with open(self.CFG_FILE, "w") as f:

                json.dump(existing, f)

        except Exception:

            pass

    def _save_pos(self): self._save_settings()

    def _place_popup(self, popup, pw, ph):

        popup.withdraw()

        self.root.update_idletasks()

        x = self.root.winfo_x() + (self.root.winfo_width()  - pw) // 2

        y = self.root.winfo_y() + (self.root.winfo_height() - ph) // 2

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

            import sys

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

        # X 버튼·최소화는 항상 트레이 숨김 경로로 연결 (pystray 없으면 창만 숨김, 종료 아님)
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self.root.bind("<Unmap>", self._on_root_unmap)

        try:

            import pystray

            self._tray_run_error = None

            if self._tray:

                try: self._tray.stop()

                except Exception: pass

            self._tray = None

            # Windows Shell 아이콘은 프로세스별 고유 ID가 안전 (이전 실행 캐시/충돌 완화)
            tray_uid = "AutoSync.%s" % (os.getpid(),)

            # pystray는 PIL Image를 ICO로 저장해 Shell에 넘김 — RGB 변환은 일부 Pillow/Win 조합에서 실패할 수 있음
            img = pil_image_for_tray(64)

            def tray_toggle_sync(icon, item):

                self.root.after(0, self._toggle)

            def tray_sync_label(item):

                return "⏹ 싱크 중지" if self._running else "▶ 싱크 시작"

            def tray_toggle_auto_skip(icon, item):
                enabled = bool(self._oped_auto_var.get())
                self._oped_auto_var.set(not enabled)
                self._save_settings()
                # gui_run mixin이 있으면 즉시 상태 반영
                if hasattr(self, "_start_auto_skip_monitor") and hasattr(self, "_stop_auto_skip_monitor"):
                    if self._oped_auto_var.get() and not self._running:
                        self.root.after(0, self._start_auto_skip_monitor)
                    else:
                        self.root.after(0, self._stop_auto_skip_monitor)
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

            self._tray = pystray.Icon(tray_uid, img, "Auto Sync", menu)
            self._tray_thread = None

            def _run_tray():
                try:
                    if sys.platform == "win32":
                        try:
                            import pythoncom
                            pythoncom.CoInitialize()
                        except Exception:
                            pass
                    self._tray.run()
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

            # Tk 메인 스레드와 병행: pystray FAQ 권장대로 아이콘 루프는 별도 스레드에서 run().
            # run_detached()는 환경에 따라 동기 예외/초기화 순서 문제가 있어 Windows에서도 동일 경로 사용.
            self._tray_thread = threading.Thread(
                target=_run_tray, daemon=False, name="pystray")
            self._tray_thread.start()

        except ImportError as e:
            self._tray = None
            msg = str(e)
            if "PIL" in msg or "Image" in msg:
                self._tray_run_error = (
                    "Pillow(PIL)가 깨졌거나 EXE에 포함되지 않았습니다: %s" % e)
            else:
                self._tray_run_error = (
                    "pystray 미설치 또는 백엔드 로드 실패: %s" % e)

        except Exception:
            self._tray = None
            self._tray_run_error = traceback.format_exc()[-900:]

    def _hide_to_tray(self):

        self._save_pos()

        if not self._tray:
            # 첫 숨김 시 한 번만 트레이 재시도 (초기화 레이스 대비)
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
                            "Python 실행이면: pip install pystray Pillow\n"
                            "EXE 실행이면: Pillow 번들 누락일 수 있으니 소스 기준으로 다시 빌드하세요.\n\n"
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
        """Unmap 직후 state가 아직 갱신 안 된 경우(Windows) 재확인 후 트레이로 숨김."""
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
        """작업표시줄 최소화(아이콘화) 시 작업표시줄 대신 트레이로 숨김."""
        try:
            if event.widget is not self.root:
                return
            if getattr(self, "_closing", False):
                return
            st = str(self.root.state())
            # 이미 withdraw 한 경우 재진입 방지
            if st == "withdrawn":
                return
            # Windows: 최소화 버튼 → 대개 "iconic"; 일부 환경은 "iconified"
            if st in ("iconic", "iconified"):
                self.root.after_idle(self._hide_to_tray)
                return
            # 최소화 직후 wm state가 잠깐 "normal"로 남는 경우
            self.root.after(40, self._unmap_maybe_minimize_to_tray)
        except Exception:
            pass
