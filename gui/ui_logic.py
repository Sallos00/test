"""gui/ui_logic.py -- 링크 재생·시청기록 로직 메서드

[수정 내용]
  1. 링크 재생 관련 상태 변수 추가
       _link_play_mode (bool) : True 시 싱크 보정 · OP/ED 감지 비활성화
  2. 링크 재생 메서드 추가
       _link_play            — URL 입력 → PotPlayer Ctrl+V 재생 + Livehistory.json 기록
       _link_resume          — 마지막 URL 이어보기
       _update_link_resume_btn — 이어보기 버튼 활성화/비활성화 갱신
  3. 링크 시청 기록(Livehistory.json) 관리 메서드 추가
       _load_live_history / _save_live_history
       _refresh_live_history_list / _refresh_live_history_list_inner
       _live_hist_clear_all / _live_hist_delete_one
  4. _toggle, _start_oped_monitor 진입부에 _link_play_mode 가드 적용
     (기존 코드 최소 수정, 플래그 방식으로 결합도 최소화)

기존 메서드 (완전 보존):
  시청 기록 : _hist_browse_dir, _hist_open_dir, _hist_resume,
              _refresh_history_list, _load_history, _save_history,
              record_video_history

모듈 함수  : _strip_episode_number, _extract_potplayer_title

PotPlayer 연동 · 기어 메뉴 메서드는 gui/ui_logic3.py (LipSyncGUILogic3) 로 분리됨.
"""
import os
import re
import json
import time as _time
import collections
import threading
import subprocess
import tkinter as tk
import tkinter.filedialog as fd
from win32_utils import find_potplayer_hwnd, get_playback_info, do_oped_skip, pip_send


# ─────────────────────────────────────────────────────────────────────────────
# 링크 재생 모드: 이 플래그가 True일 때 싱크 보정 · OP/ED 비활성화 처리는
# _toggle() 및 _start_oped_monitor() 진입부에서 체크한다.
# ─────────────────────────────────────────────────────────────────────────────
_LINK_HISTORY_FILENAME = "Livehistory.json"


class LipSyncGUILogic:

    # ══════════════════════════════════════════════════════════════════════════
    # ① 링크 재생 기능 (NEW)
    # ══════════════════════════════════════════════════════════════════════════

    def _ensure_link_play_mode_state(self):
        """링크 재생 모드 상태 변수가 없으면 초기화."""
        if not hasattr(self, "_link_play_mode"):
            self._link_play_mode = False

    def _set_link_play_mode(self, active: bool):
        """링크 재생 모드 플래그를 설정하고 로그를 남긴다.

        active=True  → 싱크 보정 · OP/ED 감지 비활성화
        active=False → 원래 상태로 복귀 (단, 이미 실행 중이던 보정은 유지)
        """
        self._link_play_mode = active
        ts = _time.strftime("%H:%M:%S")
        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)
        if active:
            self._log_lines.append(
                f"[{ts}] 🔗 링크 재생 모드 ON → 싱크 보정·OP/ED 비활성화")
        else:
            self._log_lines.append(
                f"[{ts}] 🔗 링크 재생 모드 OFF → 싱크 보정·OP/ED 복귀")

    def _launch_potplayer_if_needed(self) -> bool:
        """PotPlayer가 실행 중이 아니면 실행한다. 성공 시 True 반환."""
        hwnd = find_potplayer_hwnd()
        if hwnd:
            return True
        # 레지스트리에서 PotPlayer 설치 경로 탐색
        paths = [
            r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe",
            r"C:\Program Files (x86)\DAUM\PotPlayer\PotPlayerMini.exe",
            r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini.exe",
        ]
        try:
            import winreg
            for base in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for key_path in (
                    r"SOFTWARE\DAUM\PotPlayer64",
                    r"SOFTWARE\DAUM\PotPlayer",
                    r"SOFTWARE\WOW6432Node\DAUM\PotPlayer64",
                ):
                    try:
                        with winreg.OpenKey(base, key_path) as k:
                            exe, _ = winreg.QueryValueEx(k, "ProgramPath")
                            if exe and os.path.isfile(exe):
                                paths.insert(0, exe)
                    except Exception:
                        pass
        except Exception:
            pass

        for path in paths:
            if os.path.isfile(path):
                try:
                    subprocess.Popen([path], shell=False)
                    _time.sleep(2.0)   # 창 생성 대기
                    if not hasattr(self, "_log_lines"):
                        self._log_lines = collections.deque(maxlen=100)
                    self._log_lines.append(
                        f"[{_time.strftime('%H:%M:%S')}] ▶ PotPlayer 실행: {path}")
                    return bool(find_potplayer_hwnd())
                except Exception as e:
                    if not hasattr(self, "_log_lines"):
                        self._log_lines = collections.deque(maxlen=100)
                    self._log_lines.append(
                        f"[{_time.strftime('%H:%M:%S')}] ❌ PotPlayer 실행 실패: {e}")
        return False

    def _link_play(self):
        """재생 버튼 콜백.
        1) URL 확인 → 2) Livehistory.json 생성 → 3) PotPlayer 확인/실행
        4) 클립보드 복사 → 5) Ctrl+V 전달 → 6) 기록 저장
        """
        self._ensure_link_play_mode_state()
        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)

        url = getattr(self, "_link_url_var", tk.StringVar()).get().strip()
        if not url:
            self._link_status("⚠ URL을 입력하세요.", warn=True)
            return

        def _run():
            ts = _time.strftime("%H:%M:%S")
            try:
                # Livehistory.json 존재 확인 / 생성
                self._ensure_live_history_file()

                # PotPlayer 실행 확인
                self._link_status("⏳ PotPlayer 확인 중...")
                ok = self._launch_potplayer_if_needed()
                if not ok:
                    self._link_status("❌ PotPlayer를 실행할 수 없습니다.", warn=True)
                    self._log_lines.append(f"[{ts}] ❌ PotPlayer 실행 실패")
                    return

                hwnd = find_potplayer_hwnd()
                if not hwnd:
                    self._link_status("❌ PotPlayer 핸들을 찾을 수 없습니다.", warn=True)
                    return

                # 클립보드에 URL 복사 (tkinter 메인 스레드에서 실행)
                self.root.after(0, lambda: self._clipboard_set(url))
                _time.sleep(0.3)   # 클립보드 반영 대기

                # PotPlayer에 Ctrl+V 전달
                import ctypes
                VK_CONTROL = 0x11
                VK_V       = 0x56
                user32 = ctypes.windll.user32
                user32.SetForegroundWindow(hwnd)
                _time.sleep(0.1)
                user32.keybd_event(VK_CONTROL, 0, 0, 0)
                user32.keybd_event(VK_V,       0, 0, 0)
                user32.keybd_event(VK_V,       0, 2, 0)   # KEYUP
                user32.keybd_event(VK_CONTROL, 0, 2, 0)   # KEYUP
                _time.sleep(0.1)

                # 링크 재생 모드 ON → 싱크/OP/ED 비활성화
                self.root.after(0, lambda: self._set_link_play_mode(True))

                # 시청 기록 저장
                self.root.after(0, lambda: self._save_live_history_entry(url))
                self.root.after(0, lambda: self._refresh_live_history_list())
                self.root.after(0, lambda: self._update_link_resume_btn())
                self.root.after(0, lambda: self._link_status(f"✅ 재생 중: {url[:50]}"))
                self._log_lines.append(f"[{ts}] 🔗 링크 재생: {url}")

            except Exception as e:
                self._link_status(f"❌ 오류: {e}", warn=True)
                self._log_lines.append(f"[{_time.strftime('%H:%M:%S')}] ❌ 링크 재생 오류: {e}")

        threading.Thread(target=_run, daemon=True, name="link-play").start()

    def _link_resume(self):
        """이어보기 버튼 콜백 — 마지막 기록된 URL로 재생 (URL은 입력창에 노출하지 않음)."""
        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)

        records = self._load_live_history()
        if not records:
            self._link_status("⚠ 이어보기 기록이 없습니다.", warn=True)
            return

        last_url = records[-1].get("url", "")
        if not last_url:
            self._link_status("⚠ 저장된 URL이 없습니다.", warn=True)
            return

        # URL을 입력창에 노출하지 않고 내부적으로 재생
        self._link_play_url(last_url)

    def _clipboard_set(self, text: str):
        """메인 스레드에서 클립보드에 텍스트를 복사한다."""
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()
        except Exception as e:
            if not hasattr(self, "_log_lines"):
                self._log_lines = collections.deque(maxlen=100)
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ❌ 클립보드 복사 오류: {e}")

    def _link_status(self, msg: str, warn: bool = False):
        """링크 재생 탭 상태 레이블을 갱신한다 (스레드 세이프)."""
        def _do():
            lbl = getattr(self, "_link_status_lbl", None)
            if lbl and lbl.winfo_exists():
                fg = "#e0a03c" if warn else self.TEXT_DIM
                lbl.config(text=msg, fg=fg)
        self.root.after(0, _do)

    def _update_link_resume_btn(self):
        """이어보기 버튼 상태를 기록 유무에 따라 갱신한다."""
        btn = getattr(self, "_link_resume_btn", None)
        if not btn:
            return
        try:
            if not btn.winfo_exists():
                return
        except Exception:
            return
        records = self._load_live_history()
        if records:
            btn.config(state="normal", fg=self.ACCENT3)   # [수정] 녹색
        else:
            btn.config(state="disabled", fg=self.TEXT_MID)

    # ── Livehistory.json 파일 관리 ────────────────────────────────────────────

    def _live_history_path(self) -> str:
        return os.path.join(self.APP_DIR, _LINK_HISTORY_FILENAME)

    def _ensure_live_history_file(self):
        """Livehistory.json 파일이 없으면 생성한다."""
        try:
            os.makedirs(self.APP_DIR, exist_ok=True)
            p = self._live_history_path()
            if not os.path.exists(p):
                with open(p, "w", encoding="utf-8") as f:
                    json.dump([], f, ensure_ascii=False)
        except Exception as e:
            if not hasattr(self, "_log_lines"):
                self._log_lines = collections.deque(maxlen=100)
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ❌ Livehistory 생성 오류: {e}")

    def _load_live_history(self) -> list:
        """Livehistory.json 로드. 실패 시 빈 리스트 반환."""
        try:
            p = self._live_history_path()
            if not os.path.exists(p):
                return []
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_live_history(self, records: list):
        """Livehistory.json 저장."""
        try:
            os.makedirs(self.APP_DIR, exist_ok=True)
            p = self._live_history_path()
            with open(p, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            if not hasattr(self, "_log_lines"):
                self._log_lines = collections.deque(maxlen=100)
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ❌ Livehistory 저장 오류: {e}")

    def _save_live_history_entry(self, url: str, title: str = ""):
        """URL(및 제목)을 Livehistory.json에 기록한다.
        URL은 내부 저장 전용 — 목록 표시에는 title을 사용한다.
        동일한 URL이 이미 있으면 타임스탬프와 제목을 갱신 (중복 방지).
        """
        if not url:
            return
        ts = _time.strftime("%Y-%m-%d %H:%M")
        records = self._load_live_history()

        for i, rec in enumerate(records):
            if rec.get("url", "") == url:
                records.pop(i)
                records.append({"url": url, "title": title or rec.get("title", ""), "timestamp": ts})
                self._save_live_history(records)
                if not hasattr(self, "_log_lines"):
                    self._log_lines = collections.deque(maxlen=100)
                self._log_lines.append(
                    f"[{_time.strftime('%H:%M:%S')}] 🔗 링크 기록 갱신: {url}")
                return

        records.append({"url": url, "title": title, "timestamp": ts})
        self._save_live_history(records)
        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)
        self._log_lines.append(
            f"[{_time.strftime('%H:%M:%S')}] 🔗 링크 기록 추가: {url}")

    def _live_hist_update_title(self, title: str):
        """마지막으로 기록된 항목의 제목을 PotPlayer 창 제목으로 갱신한다."""
        if not title:
            return
        records = self._load_live_history()
        if not records:
            return
        records[-1]["title"] = title
        self._save_live_history(records)
        self._refresh_live_history_list()
        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)
        self._log_lines.append(
            f"[{_time.strftime('%H:%M:%S')}] 🔗 링크 기록 제목 갱신: {title}")

    def _live_hist_clear_all(self):
        """링크 시청 기록 전체 삭제."""
        import tkinter.messagebox as mb
        if not mb.askyesno("링크 기록 삭제", "링크 시청 기록을 전부 삭제하시겠습니까?"):
            return
        self._save_live_history([])
        self._update_link_resume_btn()
        self._refresh_live_history_list()

    def _live_hist_delete_one(self, url: str):
        """링크 시청 기록 개별 삭제."""
        records = self._load_live_history()
        records = [r for r in records if r.get("url", "") != url]
        self._save_live_history(records)
        self._update_link_resume_btn()
        self._refresh_live_history_list()

    # ── yt-dlp 영상 저장 기능 ─────────────────────────────────────────────────

    # yt-dlp 최신 릴리즈 단일 실행파일 (Windows x64)
    _YTDLP_URL = (
        "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
    )
    # ffmpeg Builds (yt-dlp 공식 권장 빌드, Windows x64 GPL)
    _FFMPEG_ZIP_URL = (
        "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/"
        "ffmpeg-master-latest-win64-gpl.zip"
    )

    # ── 공통 다운로드 헬퍼 ───────────────────────────────────────────────────

    def _download_file(self, url: str, dest: str, label: str):
        """url → dest 로 파일을 내려받는다. 진행률을 상태 레이블에 표시한다.
        실패 시 임시 파일을 삭제하고 예외를 발생시킨다.
        """
        import urllib.request
        tmp = dest + ".tmp"

        def _hook(block_num, block_size, total_size):
            if total_size > 0:
                pct = min(100.0, block_num * block_size * 100.0 / total_size)
                self.root.after(0, lambda p=pct: self._dl_progress_update(p))
                self.root.after(0, lambda p=pct: self._link_status(
                    f"⬇ {label} 설치 중… {p:.0f}%"))

        try:
            urllib.request.urlretrieve(url, tmp, reporthook=_hook)
            os.replace(tmp, dest)   # 원자적 이동
        except Exception as e:
            if os.path.exists(tmp):
                try: os.remove(tmp)
                except Exception: pass
            raise RuntimeError(f"{label} 다운로드 실패: {e}") from e

    # ── yt-dlp ───────────────────────────────────────────────────────────────

    def _ytdlp_path(self) -> str:
        return os.path.join(self.APP_DIR, "yt-dlp.exe")

    def _ensure_ytdlp(self) -> str:
        """yt-dlp 실행파일을 확보해 경로를 반환한다.
        없으면 GitHub Releases에서 자동 다운로드한다.
        """
        import shutil

        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)

        found = shutil.which("yt-dlp")
        if found:
            self._log_lines.append(f"[{_time.strftime('%H:%M:%S')}] ✅ yt-dlp (PATH): {found}")
            return found

        local = self._ytdlp_path()
        if os.path.isfile(local):
            self._log_lines.append(f"[{_time.strftime('%H:%M:%S')}] ✅ yt-dlp (로컬): {local}")
            return local

        self._log_lines.append(f"[{_time.strftime('%H:%M:%S')}] ⬇ yt-dlp 다운로드 시작")
        self.root.after(0, self._dl_progress_show)
        os.makedirs(self.APP_DIR, exist_ok=True)
        self._download_file(self._YTDLP_URL, local, "yt-dlp")
        self._log_lines.append(f"[{_time.strftime('%H:%M:%S')}] ✅ yt-dlp 설치 완료: {local}")
        return local

    # ── ffmpeg ────────────────────────────────────────────────────────────────

    def _ffmpeg_path(self) -> str:
        return os.path.join(self.APP_DIR, "ffmpeg.exe")

    def _ensure_ffmpeg(self) -> str:
        """ffmpeg 실행파일을 확보해 경로를 반환한다.
        없으면 yt-dlp/FFmpeg-Builds zip을 내려받아 ffmpeg.exe만 추출한다.
        """
        import shutil, zipfile

        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)

        found = shutil.which("ffmpeg")
        if found:
            self._log_lines.append(f"[{_time.strftime('%H:%M:%S')}] ✅ ffmpeg (PATH): {found}")
            return found

        local = self._ffmpeg_path()
        if os.path.isfile(local):
            self._log_lines.append(f"[{_time.strftime('%H:%M:%S')}] ✅ ffmpeg (로컬): {local}")
            return local

        # zip 다운로드 후 ffmpeg.exe 만 추출
        self._log_lines.append(f"[{_time.strftime('%H:%M:%S')}] ⬇ ffmpeg 다운로드 시작")
        os.makedirs(self.APP_DIR, exist_ok=True)
        zip_path = local + ".zip"
        self._download_file(self._FFMPEG_ZIP_URL, zip_path, "ffmpeg")

        self.root.after(0, lambda: self._link_status("⬇ ffmpeg 압축 해제 중…"))
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                # zip 내부 경로: ffmpeg-master-.../bin/ffmpeg.exe
                target = next(
                    (n for n in zf.namelist() if n.endswith("/ffmpeg.exe")),
                    None
                )
                if target is None:
                    raise RuntimeError("zip 안에서 ffmpeg.exe를 찾을 수 없습니다.")
                with zf.open(target) as src, open(local, "wb") as dst:
                    dst.write(src.read())
        finally:
            try: os.remove(zip_path)
            except Exception: pass

        self._log_lines.append(f"[{_time.strftime('%H:%M:%S')}] ✅ ffmpeg 설치 완료: {local}")
        return local

    def _link_save(self):
        """저장 버튼 콜백 — yt-dlp + ffmpeg를 사용해 최고 화질로 저장한다.
        두 실행파일이 없으면 최초 1회 자동 다운로드 후 APP_DIR에 캐시한다.
        """
        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)

        url = getattr(self, "_link_url_var", tk.StringVar()).get().strip()
        if not url:
            self._link_status("⚠ URL을 입력하세요.", warn=True)
            return

        import pathlib
        dl_dir = str(pathlib.Path.home() / "Downloads")
        try:
            os.makedirs(dl_dir, exist_ok=True)
        except Exception:
            dl_dir = self.APP_DIR

        def _run():
            ts = _time.strftime("%H:%M:%S")
            try:
                # ── 1) yt-dlp 확보 ────────────────────────────────────────────
                try:
                    ytdlp = self._ensure_ytdlp()
                except Exception as e:
                    self.root.after(0, lambda: self._link_status(
                        f"❌ yt-dlp 설치 실패: {e}", warn=True))
                    self.root.after(0, self._dl_progress_hide)
                    self._log_lines.append(f"[{ts}] ❌ yt-dlp 설치 실패: {e}")
                    return

                # ── 2) ffmpeg 확보 ────────────────────────────────────────────
                try:
                    ffmpeg = self._ensure_ffmpeg()
                except Exception as e:
                    self.root.after(0, lambda: self._link_status(
                        f"❌ ffmpeg 설치 실패: {e}", warn=True))
                    self.root.after(0, self._dl_progress_hide)
                    self._log_lines.append(f"[{ts}] ❌ ffmpeg 설치 실패: {e}")
                    return

                # ── 3) 영상 다운로드 (bestvideo+bestaudio → mp4 병합) ─────────
                self.root.after(0, lambda: self._dl_progress_update(0.0))
                self.root.after(0, lambda: self._link_status("동영상을 저장하고 있습니다."))
                self.root.after(0, self._dl_progress_show)

                cmd = [
                    ytdlp,
                    "-f", "bestvideo+bestaudio/best",
                    "--merge-output-format", "mp4",
                    "--ffmpeg-location", os.path.dirname(ffmpeg),
                    "--newline",
                    "-o", os.path.join(dl_dir, "%(title)s.%(ext)s"),
                    url,
                ]

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )

                for line in proc.stdout:
                    m = re.search(r'\[download\]\s+([\d.]+)%', line)
                    if m:
                        pct = float(m.group(1))
                        self.root.after(0, lambda p=pct: self._dl_progress_update(p))

                proc.wait()

                if proc.returncode == 0:
                    self.root.after(0, lambda: self._dl_progress_update(100.0))
                    self.root.after(0, lambda: self._link_status(
                        f"✅ 저장 완료 → {dl_dir}"))
                    self.root.after(2500, self._dl_progress_hide)
                    self._log_lines.append(f"[{ts}] ✅ yt-dlp 저장 완료: {url}")
                else:
                    self.root.after(0, lambda: self._link_status(
                        "❌ 저장 실패. URL이 올바른지 확인하세요.", warn=True))
                    self.root.after(0, self._dl_progress_hide)
                    self._log_lines.append(
                        f"[{ts}] ❌ yt-dlp 저장 실패 (code {proc.returncode}): {url}")

            except Exception as e:
                self.root.after(0, lambda: self._link_status(f"❌ 오류: {e}", warn=True))
                self.root.after(0, self._dl_progress_hide)
                self._log_lines.append(f"[{ts}] ❌ yt-dlp 예외: {e}")

        threading.Thread(target=_run, daemon=True, name="yt-dlp-save").start()

    def _dl_progress_show(self):
        """다운로드 진행 UI 표시."""
        dl_row = getattr(self, "_dl_row", None)
        if dl_row:
            try:
                dl_row.pack(fill="x", pady=(round(2), 0))
            except Exception:
                pass
        lbl = getattr(self, "_dl_pct_lbl", None)
        if lbl:
            try:
                lbl.config(text="0%")
            except Exception:
                pass
        bar = getattr(self, "_dl_bar", None)
        if bar:
            try:
                bar.place(x=0, y=0, width=0, height=8)
            except Exception:
                pass

    def _dl_progress_hide(self):
        """다운로드 진행 UI 숨기기."""
        dl_row = getattr(self, "_dl_row", None)
        if dl_row:
            try:
                dl_row.pack_forget()
            except Exception:
                pass
        bar = getattr(self, "_dl_bar", None)
        if bar:
            try:
                bar.place(x=0, y=0, width=0, height=8)
            except Exception:
                pass

    def _dl_progress_update(self, pct: float):
        """다운로드 진행률(0–100) 업데이트."""
        bar_bg = getattr(self, "_dl_bar_bg", None)
        bar    = getattr(self, "_dl_bar",    None)
        lbl    = getattr(self, "_dl_pct_lbl", None)
        if bar_bg and bar:
            try:
                bar_bg.update_idletasks()
                w       = bar_bg.winfo_width()
                fill_w  = max(0, int(w * pct / 100))
                bar.place(x=0, y=0, width=fill_w, height=8)
            except Exception:
                pass
        if lbl:
            try:
                lbl.config(text=f"{pct:.1f}%")
            except Exception:
                pass

    # ── 링크 기록 목록 갱신 ───────────────────────────────────────────────────

    def _refresh_live_history_list(self):
        """링크 재생 탭의 기록 목록을 갱신한다."""
        if not hasattr(self, "_live_hist_canvas"):
            return
        canvas = self._live_hist_canvas
        try:
            if not canvas.winfo_exists():
                return
        except Exception:
            return

        self._live_hist_refreshing = True
        try:
            self._refresh_live_history_list_inner(canvas)
        finally:
            self._live_hist_refreshing = False

    def _refresh_live_history_list_inner(self, canvas):
        """실제 링크 기록 목록 위젯을 업데이트한다 (행 캐시 재활용)."""
        records  = self._load_live_history()
        r        = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        mw_fn    = getattr(self, "_live_hist_mousewheel_fn", None)
        entries  = list(reversed(records))   # 최신순

        if not hasattr(self, "_live_hist_row_cache"):
            self._live_hist_row_cache = []

        cache = self._live_hist_row_cache
        frame = self._live_hist_frame

        # 빈 상태
        empty_lbl = getattr(self, "_live_hist_empty_lbl", None)
        if not entries:
            for cached in cache:
                cached["row"].pack_forget()
            if empty_lbl is None or not empty_lbl.winfo_exists():
                self._live_hist_empty_lbl = tk.Label(
                    frame, text="— 링크 재생 기록 없음 —",
                    font=("Consolas", self.F_MONO_S),
                    bg=self.BG, fg=self.TEXT_DIM,
                    pady=round(12 * r))
                if mw_fn:
                    self._live_hist_empty_lbl.bind("<MouseWheel>", mw_fn)
                self._live_hist_empty_lbl.pack()
            else:
                self._live_hist_empty_lbl.pack()
            canvas.configure(scrollregion=canvas.bbox("all"))
            return
        else:
            if empty_lbl is not None:
                try: empty_lbl.pack_forget()
                except Exception: pass

        # 캐시 부족 시 새 행 생성
        while len(cache) < len(entries):
            idx    = len(cache)
            row_bg = self.BG2 if idx % 2 == 0 else self.BG3
            btn_bg = self.BG3 if idx % 2 == 0 else self.BG2

            row  = tk.Frame(frame, bg=row_bg, pady=round(5 * r))
            info = tk.Frame(row, bg=row_bg)
            info.pack(side="left", fill="x", expand=True, padx=(round(8 * r), 0))

            url_lbl = tk.Label(info, text="",
                               font=("Consolas", self.F_MONO_S, "bold"),
                               bg=row_bg, fg=self.TEXT,
                               anchor="w", justify="left")
            url_lbl.pack(anchor="w")

            ts_lbl = tk.Label(info, text="",
                              font=("Consolas", max(6, self.F_MONO_S - 1)),
                              bg=row_bg, fg=self.TEXT_DIM, anchor="w")
            ts_lbl.pack(anchor="w")

            del_btn = tk.Button(
                row, text="🗑",
                font=("Consolas", max(7, round(8 * r))),
                bg=btn_bg, fg="#ffffff",
                activebackground=self.BORDER,
                relief="flat", cursor="hand2",
                padx=round(4 * r), pady=round(2 * r))
            del_btn.pack(side="right", anchor="center", padx=(0, round(4 * r)))

            # 이어보기 버튼 — 텍스트 색상 녹색 (요구사항)
            resume_btn = tk.Button(
                row, text="▶ 이어보기",
                font=("Consolas", max(7, round(8 * r)), "bold"),
                bg=btn_bg, fg=self.ACCENT3,   # [수정] 녹색
                activebackground=self.BORDER,
                relief="flat", cursor="hand2",
                padx=round(6 * r), pady=round(2 * r))
            resume_btn.pack(side="right", anchor="center", padx=(0, round(2 * r)))

            if mw_fn:
                for w in (row, info, url_lbl, resume_btn, del_btn, ts_lbl):
                    w.bind("<MouseWheel>", mw_fn)

            cache.append({"row": row, "info": info, "url_lbl": url_lbl,
                          "ts_lbl": ts_lbl, "resume_btn": resume_btn,
                          "del_btn": del_btn})

        # 행 내용 업데이트
        for i, rec in enumerate(entries):
            url    = rec.get("url", "")
            title  = rec.get("title", "")
            ts     = rec.get("timestamp", "")
            cached = cache[i]

            # 표시: 제목 우선, 없으면 URL 앞부분으로 대체 (URL 자체는 숨김)
            if title:
                display_text = title[:60] + "…" if len(title) > 60 else title
            else:
                display_text = "(제목 불러오는 중…)"

            cached["url_lbl"].config(text=display_text)
            cached["ts_lbl"].config(text=ts if ts else "")
            cached["resume_btn"].config(
                command=lambda u=url: self._link_resume_from_record(u))
            cached["del_btn"].config(
                command=lambda u=url: self._live_hist_delete_one(u))
            cached["row"].pack(fill="x", pady=(0, 1))

        # 남는 캐시 행 제거
        for i in range(len(entries), len(cache)):
            try: cache[i]["row"].destroy()
            except Exception: pass
        del cache[len(entries):]

        cw = canvas.winfo_width()
        if cw > 1:
            canvas.itemconfig(self._live_hist_canvas_window, width=cw)
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _link_resume_from_record(self, url: str):
        """기록 목록의 이어보기 버튼 클릭 — 특정 URL로 재생 (URL 입력창 미표시)."""
        self._link_play_url(url)

    def _link_play_url(self, url: str):
        """지정된 URL을 PotPlayer에서 재생한다. 입력창을 수정하지 않는다."""
        self._ensure_link_play_mode_state()
        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)

        if not url:
            self._link_status("⚠ URL이 비어 있습니다.", warn=True)
            return

        def _run():
            ts = _time.strftime("%H:%M:%S")
            try:
                self._ensure_live_history_file()
                self._link_status("⏳ PotPlayer 확인 중...")
                ok = self._launch_potplayer_if_needed()
                if not ok:
                    self._link_status("❌ PotPlayer를 실행할 수 없습니다.", warn=True)
                    return

                hwnd = find_potplayer_hwnd()
                if not hwnd:
                    self._link_status("❌ PotPlayer 핸들을 찾을 수 없습니다.", warn=True)
                    return

                self.root.after(0, lambda: self._clipboard_set(url))
                _time.sleep(0.3)

                import ctypes
                VK_CONTROL = 0x11
                VK_V       = 0x56
                user32 = ctypes.windll.user32
                user32.SetForegroundWindow(hwnd)
                _time.sleep(0.1)
                user32.keybd_event(VK_CONTROL, 0, 0, 0)
                user32.keybd_event(VK_V,       0, 0, 0)
                user32.keybd_event(VK_V,       0, 2, 0)
                user32.keybd_event(VK_CONTROL, 0, 2, 0)
                _time.sleep(0.1)

                self.root.after(0, lambda: self._set_link_play_mode(True))
                self.root.after(0, lambda: self._save_live_history_entry(url))
                self.root.after(0, lambda: self._refresh_live_history_list())
                self.root.after(0, lambda: self._update_link_resume_btn())
                self.root.after(0, lambda: self._link_status("✅ 재생 중 (이어보기)…"))
                self._log_lines.append(f"[{ts}] 🔗 이어보기 재생: {url}")

            except Exception as e:
                self._link_status(f"❌ 오류: {e}", warn=True)
                self._log_lines.append(f"[{_time.strftime('%H:%M:%S')}] ❌ 이어보기 오류: {e}")

        threading.Thread(target=_run, daemon=True, name="link-resume").start()

    # ══════════════════════════════════════════════════════════════════════════
    # ② 시청 기록 탭 (기존 코드 완전 보존)
    # ══════════════════════════════════════════════════════════════════════════

    def _hist_browse_dir(self):
        init = self._hist_video_dir if getattr(self, "_hist_video_dir", "") else "/"
        d = fd.askdirectory(title="동영상 폴더 지정", initialdir=init)
        if not d:
            return
        self._hist_video_dir = d
        self._hist_dir_lbl.config(text=d, fg=self.TEXT)
        self._hist_open_btn.config(state="normal")
        self._save_settings()
        self._refresh_history_list()

    def _hist_open_dir(self):
        d = getattr(self, "_hist_video_dir", "")
        if d and os.path.isdir(d):
            try: os.startfile(d)
            except Exception: pass

    def _hist_clear_all(self):
        import tkinter.messagebox as mb
        if not mb.askyesno("시청 기록 삭제", "시청 기록을 전부 삭제하시겠습니까?"):
            return
        self._save_history([])
        self._refresh_history_list()

    def _hist_delete_one(self, title: str):
        records = self._load_history()
        records = [r for r in records if r.get("title", "") != title]
        self._save_history(records)
        self._refresh_history_list()

    def _hist_resume(self, title: str):
        d = getattr(self, "_hist_video_dir", "")
        if not d or not os.path.isdir(d):
            return
        VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv",
                      ".ts", ".m2ts", ".flv", ".webm", ".m4v"}

        def _normalize(name: str) -> str:
            n = os.path.splitext(name)[0]
            n = re.sub(r'\s*\([^)]*\)', '', n)
            n = re.sub(r'\s*\[[^\]]*\]', '', n)
            return re.sub(r'[\s_\-\.]+', ' ', n).strip().lower()

        title_norm = _normalize(title)
        base       = _strip_episode_number(title_norm)
        ep_num     = None
        m = re.search(r'제?(\d+)\s*[화편부회장권]', title_norm)
        if not m:
            m = re.search(r'[Ss]\d{1,2}[Ee](\d{1,3})', title_norm)
        if not m:
            nums = re.findall(r'(?<!\d)(\d+)(?!\d)', title_norm)
            if nums:
                ep_num = nums[-1]
        if m and ep_num is None:
            ep_num = m.group(1)

        exact_match = series_match = None
        for dirpath, _, fnames in os.walk(d):
            for fname in fnames:
                if os.path.splitext(fname)[1].lower() not in VIDEO_EXTS:
                    continue
                fname_norm = _normalize(fname)
                fpath      = os.path.join(dirpath, fname)
                if fname_norm == title_norm:
                    exact_match = fpath
                    break
                fname_base = _strip_episode_number(fname_norm)
                if fname_base and base and fname_base == base and ep_num is not None:
                    fname_nums = re.findall(r'(?<!\d)(\d+)(?!\d)', fname_norm)
                    if ep_num in fname_nums and series_match is None:
                        series_match = fpath
            if exact_match:
                break

        found = exact_match or series_match
        if found:
            try: os.startfile(found)
            except Exception: pass
        else:
            import tkinter.messagebox as mb
            mb.showwarning("이어보기",
                           f"폴더에서 해당 동영상을 찾을 수 없습니다.\n\n"
                           f"제목: {title}\n폴더: {d}")

    def _refresh_history_list(self):
        if not hasattr(self, "_hist_list_canvas"):
            return
        canvas = self._hist_list_canvas
        try:
            if not canvas.winfo_exists():
                return
        except Exception:
            return
        self._hist_refreshing = True
        try:
            self._refresh_history_list_inner(canvas)
        finally:
            self._hist_refreshing = False

    def _refresh_history_list_inner(self, canvas):
        records = self._load_history()
        r       = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        has_dir = bool(getattr(self, "_hist_video_dir", ""))
        mw_fn   = getattr(self, "_hist_mousewheel_fn", None)
        entries = list(reversed(records))

        if not hasattr(self, "_hist_row_cache"):
            self._hist_row_cache = []

        cache = self._hist_row_cache
        frame = self._hist_list_frame

        empty_lbl = getattr(self, "_hist_empty_lbl", None)
        if not entries:
            for cached in cache:
                cached["row"].pack_forget()
            if empty_lbl is None or not empty_lbl.winfo_exists():
                self._hist_empty_lbl = tk.Label(
                    frame, text="— 시청 기록 없음 —",
                    font=("Consolas", self.F_MONO_S),
                    bg=self.BG, fg=self.TEXT_DIM,
                    pady=round(12 * r))
                if mw_fn:
                    self._hist_empty_lbl.bind("<MouseWheel>", mw_fn)
                self._hist_empty_lbl.pack()
            else:
                self._hist_empty_lbl.pack()
            canvas.configure(scrollregion=canvas.bbox("all"))
            return
        else:
            if empty_lbl is not None:
                try: empty_lbl.pack_forget()
                except Exception: pass

        while len(cache) < len(entries):
            idx    = len(cache)
            row_bg = self.BG2 if idx % 2 == 0 else self.BG3
            btn_bg = self.BG3 if idx % 2 == 0 else self.BG2

            row  = tk.Frame(frame, bg=row_bg, pady=round(5 * r))
            info = tk.Frame(row, bg=row_bg)
            info.pack(side="left", fill="x", expand=True, padx=(round(8 * r), 0))

            title_lbl = tk.Label(info, text="",
                                 font=("Consolas", self.F_MONO_S, "bold"),
                                 bg=row_bg, fg=self.TEXT,
                                 anchor="w", justify="left")
            title_lbl.pack(anchor="w")

            ts_lbl = tk.Label(info, text="",
                              font=("Consolas", max(6, self.F_MONO_S - 1)),
                              bg=row_bg, fg=self.TEXT_DIM, anchor="w")
            ts_lbl.pack(anchor="w")

            del_btn = tk.Button(
                row, text="🗑",
                font=("Consolas", max(7, round(8 * r))),
                bg=btn_bg, fg="#ffffff",
                activebackground=self.BORDER,
                relief="flat", cursor="hand2",
                padx=round(4 * r), pady=round(2 * r))
            del_btn.pack(side="right", anchor="center", padx=(0, round(4 * r)))

            resume_btn = tk.Button(
                row, text="▶ 이어보기",
                font=("Consolas", max(7, round(8 * r)), "bold"),
                bg=btn_bg, fg=self.ACCENT3,   # [수정] 녹색
                activebackground=self.BORDER,
                relief="flat", cursor="hand2",
                padx=round(6 * r), pady=round(2 * r))
            resume_btn.pack(side="right", anchor="center", padx=(0, round(2 * r)))

            if mw_fn:
                for w in (row, info, title_lbl, resume_btn, del_btn, ts_lbl):
                    w.bind("<MouseWheel>", mw_fn)

            cache.append({"row": row, "info": info, "title_lbl": title_lbl,
                          "ts_lbl": ts_lbl, "resume_btn": resume_btn,
                          "del_btn": del_btn})

        for i, rec in enumerate(entries):
            title  = rec.get("title", "")
            ts     = rec.get("timestamp", "")
            cached = cache[i]

            display_title = os.path.splitext(title)[0]
            display_title = re.sub(r'\s*\([^)]*\)', '', display_title).strip()
            display_title = re.sub(r'\s*\[[^\]]*\]', '', display_title).strip()

            MAX_LINE = 15

            def _smart_wrap(text: str) -> str:
                if len(text) <= MAX_LINE:
                    return text
                lines = []
                while len(text) > MAX_LINE:
                    cut = text.rfind(' ', 0, MAX_LINE)
                    if cut <= 0:
                        cut = MAX_LINE
                    lines.append(text[:cut])
                    text = text[cut:].lstrip(' ')
                if text:
                    lines.append(text)
                return '\n'.join(lines)

            if " - " in display_title:
                first, rest  = display_title.split(" - ", 1)
                display_text = _smart_wrap(first) + "\n- " + _smart_wrap(rest)
            else:
                display_text = _smart_wrap(display_title)

            cached["title_lbl"].config(text=display_text)
            cached["ts_lbl"].config(text=ts if ts else "")
            cached["resume_btn"].config(
                state="normal" if has_dir else "disabled",
                command=lambda t=title: (
                    self._hist_resume(t),
                    self._switch_tab_fn("sync") if hasattr(self, "_switch_tab_fn") else None))
            cached["del_btn"].config(
                command=lambda t=title: self._hist_delete_one(t))
            cached["row"].pack(fill="x", pady=(0, 1))

        for i in range(len(entries), len(cache)):
            try: cache[i]["row"].destroy()
            except Exception: pass
        del cache[len(entries):]

        cw = canvas.winfo_width()
        if cw > 1:
            canvas.itemconfig(self._hist_canvas_window, width=cw)
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _load_history(self):
        try:
            p = os.path.join(self.APP_DIR, "history.json")
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_history(self, records):
        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)
        try:
            os.makedirs(self.APP_DIR, exist_ok=True)
            p = os.path.join(self.APP_DIR, "history.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ❌ _save_history 오류: {e}")

    def record_video_history(self, title: str):
        """동영상 재생 감지 또는 제목 변경 시 호출.
        링크 재생 모드(_link_play_mode=True)일 때는 기록하지 않는다.
        """
        # [수정] 링크 재생 모드 중에는 시청 기록 탭에 기록하지 않음
        if getattr(self, "_link_play_mode", False):
            return

        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)
        if not title or not title.strip():
            return

        title = re.sub(r'\s*\([^)]*\)', '', title).strip()
        title = re.sub(r'\s*\[[^\]]*\]', '', title).strip()

        try:
            ts      = _time.strftime("%Y-%m-%d %H:%M")
            records = self._load_history()
            base    = _strip_series_name(title)

            for i, rec in enumerate(records):
                existing_title = rec.get("title", "")
                if existing_title == title:
                    records.pop(i)
                    records.append({"title": title, "timestamp": ts})
                    self._save_history(records)
                    self._refresh_history_list()
                    self._log_lines.append(
                        f"[{_time.strftime('%H:%M:%S')}] 📺 시청 기록 갱신: {title}")
                    return
                existing_base = _strip_series_name(existing_title)
                if existing_base and base and existing_base == base:
                    old_title = existing_title
                    records.pop(i)
                    records.append({"title": title, "timestamp": ts})
                    self._save_history(records)
                    self._refresh_history_list()
                    self._log_lines.append(
                        f"[{_time.strftime('%H:%M:%S')}] 📺 시청 기록 덮어쓰기: {old_title} → {title}")
                    return

            records.append({"title": title, "timestamp": ts})
            self._save_history(records)
            self._refresh_history_list()
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] 📺 시청 기록 추가: {title}")

        except Exception as e:
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ❌ record_video_history 오류: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 모듈 수준 유틸 함수 (기존 코드 완전 보존)
# ─────────────────────────────────────────────────────────────────────────────

def _strip_series_name(name: str) -> str:
    """파일명/제목에서 화수·부제목 정보를 모두 제거해 시리즈명을 추출."""
    name = os.path.splitext(name)[0]
    name = re.sub(r'^[\[\(][^\]\)]{1,30}[\]\)]\s*', '', name)
    name = re.sub(r'\s*\([^)]*\)', '', name)
    name = re.sub(r'\s*\[[^\]]*\]', '', name)
    name = re.sub(r'\s*-\s*.+$', '', name)
    name = re.sub(r'\bS\d{1,2}E\d{1,3}\b', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\b[Ee]p(?:isode)?[.\s]*\d+\b', '', name, flags=re.IGNORECASE)
    name = re.sub(r'제?\d+\s*[화편부회장권화]', '', name)
    name = re.sub(r'(?<![\w가-힣])[-_\s]*\d{1,4}(?![\w가-힣])', '', name)
    name = re.sub(r'[\s_\-\.]+', ' ', name).strip()
    return name.lower()


def _strip_episode_number(name: str) -> str:
    """하위호환용 alias."""
    return _strip_series_name(name)


def _extract_potplayer_title(window_title: str) -> str:
    """PotPlayer 창 제목에서 동영상 파일명을 추출."""
    if not window_title:
        return ""
    m = re.match(
        r'^(.+?)\s*-\s*(?:PotPlayer(?:64)?|팟플레이어(?:64)?)(?:\s.*)?$',
        window_title, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        if title and title not in ("", "-"):
            return title
    m = re.match(
        r'^(?:PotPlayer(?:64)?|팟플레이어(?:64)?)\s*-\s*(.+)$',
        window_title, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        if title and title not in ("", "-"):
            return title
    return ""
