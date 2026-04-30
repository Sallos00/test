"""gui/ui_logic3.py -- PotPlayer 연동 · 기어 메뉴 로직 메서드 (ui_logic.py 에서 분리)

분리 내용:
  PotPlayer : _pip_toggle, _update_oped_btn, _oped_skip,
              _poll_playback_info, _start_title_watcher,
              _reset_on_video_change
  기어 메뉴 : _toggle_gear_menu, _open_gear_menu, _close_gear_menu

의존 관계:
  _start_title_watcher 내부에서 _extract_potplayer_title 을 사용하므로
  gui.ui_logic 에서 임포트한다. (record_open.py / run_popup.py 의 기존
  'from gui.ui_logic import _extract_potplayer_title' 임포트를 깨뜨리지 않음)
"""
import collections
import threading
import time as _time
import tkinter as tk
from win32_utils import find_potplayer_hwnd, get_playback_info, do_oped_skip, pip_send
from gui.ui_logic import _extract_potplayer_title


class LipSyncGUILogic3:

    # ══════════════════════════════════════════════════════════════════════════
    # ③ PotPlayer 연동 (기존 코드 완전 보존)
    # ══════════════════════════════════════════════════════════════════════════

    def _pip_toggle(self):
        hwnd = find_potplayer_hwnd()
        if not hwnd: return
        pip_send(hwnd)
        if self._pip_on:
            self._pip_on = False
            self._pip_btn.config(text="⧉ PIP OFF", fg=self.TEXT_MID,
                                 bg="#0e0e0e", relief="solid", bd=1)
        else:
            self._pip_on = True
            self._pip_btn.config(text="⧉ PIP ON", fg=self.ACCENT3,
                                 bg="#0e0e0e", relief="solid", bd=1)
        self._save_settings()

    def _update_oped_btn(self):
        if not hasattr(self, "_oped_btn"):
            return
        try:
            sec = int(self._oped_skip_sec_var.get())
        except (ValueError, AttributeError):
            sec = 90
        if self._oped_auto_var.get():
            self._oped_btn.config(
                text=f"⏭ 자동 스킵 ON  ({sec}초)",
                state="disabled", bg=self.BG3, fg=self.TEXT_DIM,
                activebackground=self.BORDER)
        else:
            self._oped_btn.config(
                text=f"⏭ OP/ED 스킵  ({sec}초)",
                state="normal", bg=self.BG3, fg=self.ACCENT3,
                activebackground=self.BORDER)

    def _oped_skip(self):
        """OP/ED 수동 스킵.
        링크 재생 모드 중에는 동작하지 않는다.
        """
        if getattr(self, "_link_play_mode", False):
            return
        hwnd = find_potplayer_hwnd()
        if not hwnd:
            return
        try:
            sec = int(self._oped_skip_sec_var.get())
        except (ValueError, AttributeError):
            sec = 90
        pos_ms, dur_ms = get_playback_info(hwnd)
        if pos_ms is None:
            return
        do_oped_skip(hwnd, pos_ms, dur_ms, skip_sec=sec)

    def _poll_playback_info(self):
        try:
            hwnd = find_potplayer_hwnd()
            if hwnd:
                pos_ms, dur_ms = get_playback_info(hwnd)
                if pos_ms is not None:
                    def fmt(ms):
                        s = ms // 1000
                        return f"{s//60}:{s%60:02d}"
                    txt = (f"{fmt(pos_ms)} / {fmt(dur_ms)}"
                           if dur_ms is not None else f"{fmt(pos_ms)} / —")
                    self._dur_lbl.config(text=txt, fg=self.ACCENT3)
                else:
                    self._dur_lbl.config(text="— / —", fg=self.TEXT_MID)
            else:
                self._dur_lbl.config(text="— / —", fg=self.TEXT_MID)
        except Exception:
            pass
        if not self._closing:
            self.root.after(1000, self._poll_playback_info)

    def _start_title_watcher(self):
        """PotPlayer 창 제목 1초 감시 → 변경 시 시청 기록 저장.
        링크 재생 모드 중에는 record_video_history 자체가 무시된다.
        """
        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)

        def _watch():
            import ctypes
            prev_title  = ""
            was_running = False
            user32      = ctypes.windll.user32
            buf         = ctypes.create_unicode_buffer(512)

            while not getattr(self, "_closing", False):
                try:
                    hwnd = find_potplayer_hwnd()
                    if hwnd:
                        user32.GetWindowTextW(hwnd, buf, 512)
                        title = _extract_potplayer_title(buf.value)
                        if title and title != prev_title:
                            old_title   = prev_title
                            prev_title  = title
                            was_running = True
                            self._log_lines.append(
                                f"[{_time.strftime('%H:%M:%S')}] 🔍 제목 감지: {title}")
                            self.root.after(
                                0, lambda t=title: self.record_video_history(t))
                            if old_title and old_title != title:
                                self.root.after(0, self._reset_on_video_change)
                        else:
                            was_running = True
                    else:
                        if was_running:
                            prev_title  = ""
                            was_running = False
                            # 링크 재생 모드 해제 (PotPlayer 종료 시)
                            self.root.after(0, lambda: self._set_link_play_mode(False))
                except Exception as e:
                    try:
                        self._log_lines.append(
                            f"[{_time.strftime('%H:%M:%S')}] ⚠ 타이틀 감시 오류: {e}")
                    except Exception:
                        pass
                _time.sleep(1.0)

        t = threading.Thread(target=_watch, daemon=True, name="title-watcher")
        t.start()

    def _reset_on_video_change(self):
        """동영상 변경 감지 시 싱크·메모리·캐시·버퍼를 초기화한다."""
        try:
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ↺ 동영상 변경 감지 → 싱크/버퍼 초기화")
        except Exception:
            pass
        try:
            self._reset()
        except Exception as e:
            try:
                self._log_lines.append(
                    f"[{_time.strftime('%H:%M:%S')}] ❌ 동영상 변경 초기화 오류: {e}")
            except Exception:
                pass

    # ══════════════════════════════════════════════════════════════════════════
    # ④ 기어 메뉴 (기존 코드 완전 보존)
    # ══════════════════════════════════════════════════════════════════════════

    def _toggle_gear_menu(self):
        if self._gear_menu_open:
            self._close_gear_menu()
        else:
            self._open_gear_menu()

    def _open_gear_menu(self):
        self._gear_menu_open = True
        self.root.update_idletasks()
        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        rx = self.root.winfo_rootx(); ry = self.root.winfo_rooty()
        bx = self._gear_btn.winfo_rootx() - rx
        by = self._gear_btn.winfo_rooty() - ry + self._gear_btn.winfo_height() + 2
        mw = round(140 * r)
        frame = tk.Frame(self.root, bg=self.BORDER, bd=1, relief="solid")
        self._gear_menu_frame = frame
        frame.place(x=-9999, y=-9999)
        ITEM = dict(font=("Consolas", max(8, round(9*r))), bg=self.BG2, fg=self.TEXT,
                    relief="flat", cursor="hand2",
                    activebackground=self.BG3, activeforeground=self.TEXT,
                    anchor="w", padx=round(14*r), pady=round(7*r))
        def pick(fn):
            self._close_gear_menu(); fn()
        tk.Button(frame, text="⚙ 설정",
                  command=lambda: pick(self._open_settings), **ITEM).pack(fill="x")
        tk.Frame(frame, bg=self.BORDER, height=1).pack(fill="x")
        tk.Button(frame, text="📋 로그 보기",
                  command=lambda: pick(self._open_log_popup), **ITEM).pack(fill="x")
        frame.update_idletasks()
        frame.place(x=bx + self._gear_btn.winfo_width() - mw, y=by)
        frame.lift()

        def on_root_click(e):
            try:
                fx1 = frame.winfo_rootx(); fy1 = frame.winfo_rooty()
                fx2 = fx1 + frame.winfo_width(); fy2 = fy1 + frame.winfo_height()
                gx1 = self._gear_btn.winfo_rootx(); gy1 = self._gear_btn.winfo_rooty()
                gx2 = gx1 + self._gear_btn.winfo_width(); gy2 = gy1 + self._gear_btn.winfo_height()
                if (not (fx1 <= e.x_root <= fx2 and fy1 <= e.y_root <= fy2) and
                        not (gx1 <= e.x_root <= gx2 and gy1 <= e.y_root <= gy2)):
                    self._close_gear_menu()
            except Exception:
                self._close_gear_menu()
        self.root.bind("<Button-1>", on_root_click)

    def _close_gear_menu(self):
        self._gear_menu_open = False
        if hasattr(self, "_gear_menu_frame") and self._gear_menu_frame:
            try: self._gear_menu_frame.destroy()
            except Exception: pass
        self._gear_menu_frame = None
        try: self.root.unbind("<Button-1>")
        except Exception: pass
