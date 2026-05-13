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
# 모듈 수준 Win32 / yt-dlp 보조 함수
# ─────────────────────────────────────────────────────────────────────────────

def _win32_set_clipboard(text: str) -> bool:
    """Win32 API로 클립보드에 유니코드 텍스트를 설정한다 (스레드 세이프).

    Tkinter의 root.clipboard_clear/append 는 메인 스레드에서만 안전하지만,
    이 함수는 Win32 직접 호출이므로 워커 스레드에서도 사용 가능하다.
    성공 시 True, 실패 시 False 반환.
    """
    import ctypes
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE  = 0x0002
    k32 = ctypes.windll.kernel32
    u32 = ctypes.windll.user32
    try:
        encoded = (text + '\0').encode('utf-16-le')
        h = k32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not h:
            return False
        ptr = k32.GlobalLock(h)
        if not ptr:
            k32.GlobalFree(h)
            return False
        ctypes.memmove(ptr, encoded, len(encoded))
        k32.GlobalUnlock(h)
        if not u32.OpenClipboard(0):
            k32.GlobalFree(h)
            return False
        u32.EmptyClipboard()
        if not u32.SetClipboardData(CF_UNICODETEXT, h):
            # 실패 시 메모리는 시스템이 소유하지 않으므로 직접 해제
            u32.CloseClipboard()
            k32.GlobalFree(h)
            return False
        # 성공 시 시스템이 h 소유 → GlobalFree 금지
        u32.CloseClipboard()
        return True
    except Exception:
        return False


_CDN_TITLE_RE = re.compile(
    r'^https?://'                   # 전체 URL이 제목으로 표시되는 경우
    r'|^video[-_]?\d{4,}'          # Facebook CDN 파일명 (video_12345, video12345678)
    r'|\.m3u8(?:[?#]|$)'           # HLS 매니페스트 URL
    r'|\.mp4(?:[?#]|$)'            # 직접 MP4 링크
    r'|[&?]Expires=\d',            # CDN 서명 쿼리스트링 잔재
    re.IGNORECASE,
)

def _is_cdn_url_title(title: str) -> bool:
    """제목 문자열이 CDN URL 또는 미디어 파일명 패턴이면 True 반환.

    PotPlayer가 직접 스트림 URL을 재생하면 창 제목이 CDN URL이나
    파일명(video_1234.mp4)으로 표시되는 경우가 있다.
    이런 '가짜 제목'으로 실제 영상 제목을 덮어쓰는 것을 방지하기 위해 사용.
    """
    if not title:
        return False
    if _CDN_TITLE_RE.search(title):
        return True
    # 공백이 없고 매우 긴 문자열 → URL 잔재 또는 해시 (실제 제목이 아님)
    if len(title) > 80 and ' ' not in title and '한' not in title:
        return True
    return False


def _pick_best_stream_url(info: dict) -> str:
    """yt-dlp JSON 메타데이터에서 PotPlayer 재생에 최적인 스트림 URL을 선택한다.

    선택 우선순위:
      1. info['url'] — 단일 포맷(영상+오디오 혼합)이 있으면 그대로 사용
      2. formats 중 vcodec + acodec 모두 포함(혼합) 포맷 → 높이 기준 최고화질
      3. formats 중 영상만 있는 포맷 → 높이 기준 최고화질
      4. formats 마지막 항목 (최후 보루)
    """
    direct = info.get("url", "")
    if direct and direct.startswith("http"):
        return direct
    formats = info.get("formats", [])
    if not formats:
        return ""
    merged = [
        f for f in formats
        if f.get("vcodec", "none") not in ("none", None)
        and f.get("acodec", "none") not in ("none", None)
    ]
    if merged:
        merged.sort(key=lambda f: f.get("height", 0) or 0, reverse=True)
        url = merged[0].get("url", "")
        if url:
            return url
    video_only = [
        f for f in formats
        if f.get("vcodec", "none") not in ("none", None)
    ]
    if video_only:
        video_only.sort(key=lambda f: f.get("height", 0) or 0, reverse=True)
        url = video_only[0].get("url", "")
        if url:
            return url
    return formats[-1].get("url", "")


# ─────────────────────────────────────────────────────────────────────────────
# 링크 재생 모드: 이 플래그가 True일 때 싱크 보정 · OP/ED 비활성화 처리는
# _toggle() 및 _start_oped_monitor() 진입부에서 체크한다.
# ─────────────────────────────────────────────────────────────────────────────
_LINK_HISTORY_FILENAME = "Livehistory.json"

# 플랫폼별 이어보기 버튼 색상
_PLATFORM_COLORS = {
    "youtube":  "#e63333",  # 빨간색
    "chzzk":    "#4caf50",  # 녹색
    "twitch":   "#9b6fe0",  # 보라색
    "facebook": "#4eb8f0",  # 하늘색
    "soop":     "#ff6b35",  # Soop 오렌지
    "default":  "#00c8e0",  # 기존 청록 (ACCENT)
}

# 공통 User-Agent — Chrome 최신 안정 버전으로 통일
# 너무 낮은 버전을 쓰면 일부 스트리밍 사이트에서 봇으로 탐지될 수 있다.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

# Soop(AfreecaTV) 도메인 정규식 — 모듈 전역 공유
_SOOP_RE = re.compile(
    r'(sooplive\.co\.kr|afreecatv\.com|soop\.com)',
    re.IGNORECASE,
)

def _get_platform_color(url: str) -> str:
    """URL로 플랫폼을 판별해 이어보기 버튼 색상을 반환한다."""
    if not url:
        return _PLATFORM_COLORS["default"]
    u = url.lower()
    if re.search(r'(youtube\.com|youtu\.be)', u):
        return _PLATFORM_COLORS["youtube"]
    if re.search(r'(chzzk\.naver\.com|chzzk\.com)', u):
        return _PLATFORM_COLORS["chzzk"]
    if re.search(r'(twitch\.tv)', u):
        return _PLATFORM_COLORS["twitch"]
    if re.search(r'(facebook\.com|fb\.watch|fb\.com)', u):
        return _PLATFORM_COLORS["facebook"]
    if _SOOP_RE.search(u):
        return _PLATFORM_COLORS["soop"]
    return _PLATFORM_COLORS["default"]


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

        active=True  → 싱크 보정 · OP/ED 감지 비활성화, oped 모니터 중지
        active=False → 원래 상태로 복귀 (단, 이미 실행 중이던 보정은 유지), oped 모니터 재시작
        """
        self._link_play_mode = active
        ts = _time.strftime("%H:%M:%S")
        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)
        if active:
            self._log_lines.append(
                f"[{ts}] 🔗 링크 재생 모드 ON → 싱크 보정·OP/ED 비활성화")
            # oped 모니터가 실행 중이면 즉시 중지 (팝업 가드 타이밍 이슈 완전 제거)
            if getattr(self, "_oped_monitor_running", False):
                threading.Thread(
                    target=self._stop_oped_monitor,
                    daemon=True, name="stop-oped-link").start()
        else:
            self._log_lines.append(
                f"[{ts}] 🔗 링크 재생 모드 OFF → 싱크 보정·OP/ED 복귀")
            # 싱크가 실행 중이 아닐 때만 oped 모니터 재시작
            if not getattr(self, "_running", False):
                self.root.after(200, self._start_oped_monitor)

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
        4) 클립보드 복사+Ctrl+V 전달 → 5) 기록 저장
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

                # ── PotPlayer 부가 파일 병렬 확인 (완료 후 PotPlayer 실행) ──────
                pot_dir = self._get_potplayer_dir()
                if pot_dir:
                    self._link_status("⏳ PotPlayer 파일 확인 중...")
                    t1 = threading.Thread(
                        target=self._bg_ensure_potplayer_ytdlp,
                        args=(pot_dir,), daemon=True,
                        name="pot-ytdlp-check")
                    t2 = threading.Thread(
                        target=self._bg_ensure_potplayer_extension,
                        args=(pot_dir,), daemon=True,
                        name="pot-ext-check")
                    t1.start()
                    t2.start()
                    t1.join()   # 두 작업이 모두 끝날 때까지 대기
                    t2.join()

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

                # ── Soop : yt-dlp -j 추출 ─────────────────────────────────
                # ── 그 외: 원본 URL 그대로 PotPlayer에 전달 ────────────────
                is_soop = bool(_SOOP_RE.search(url))

                self._log_lines.append(
                    f"[{ts}] 🔗 링크 재생 시작 | 원본 URL: {url} | "
                    f"platform={'soop' if is_soop else 'general'}")

                if is_soop:
                    # Soop: CDP(실제 Chrome)로 m3u8 URL + 제목 추출
                    self._link_status("⏳ Soop m3u8 감지 중 (CDP)…")
                    try:
                        self._ensure_playwright()
                        _cdp_result = self._extract_m3u8_playwright(url)
                    except Exception as _cdp_e:
                        _cdp_result = None
                        self._log_lines.append(
                            f"[{ts}] ⚠ Soop CDP 준비 실패: {_cdp_e}")
                    if _cdp_result and _cdp_result.get("url"):
                        play_url   = _cdp_result["url"]
                        real_title = _cdp_result.get("title", "")
                        self._log_lines.append(
                            f"[{ts}] ✅ Soop CDP m3u8 감지: {play_url[:80]}")
                    else:
                        play_url   = url
                        real_title = self._fetch_page_title(url)
                        self._log_lines.append(
                            f"[{ts}] ⚠ Soop CDP 실패 → 원본 URL fallback")
                else:
                    # 유튜브·치지직·페이스북·일반 URL 등: 원본 URL 그대로 PotPlayer에 전달
                    play_url   = url
                    real_title = self._fetch_page_title(url)
                    self._log_lines.append(
                        f"[{ts}] 🔗 일반 URL 직접 전달 (m3u8 추출 없음, fallback 없음)")

                # PotPlayer에 URL 전달 (클립보드 Ctrl+V)
                self._send_url_to_potplayer(hwnd, play_url)

                # 링크 재생 모드 ON → 싱크/OP/ED 비활성화
                self.root.after(0, lambda: self._set_link_play_mode(True))

                # 시청 기록 저장 (원본 URL + 추출된 실제 제목)
                self.root.after(0, lambda: self._save_live_history_entry(url, title=real_title))
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
            last_url = records[-1].get("url", "")
            color = _get_platform_color(last_url)
            btn.config(state="normal", fg=color)
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
        """마지막으로 기록된 항목의 제목을 PotPlayer 창 제목으로 갱신한다.

        PotPlayer 창 제목이 CDN URL 이나 미디어 파일명(video_12345.mp4)으로
        표시되는 경우 실제 영상 제목을 CDN URL 패턴의 '가짜 제목'으로
        덮어쓰지 않도록 차단한다.

        [수정] 동일 계열(시리즈) 이전 기록이 있으면 덮어쓰기(중복 제거).
        """
        if not title:
            return
        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)
        records = self._load_live_history()
        if not records:
            return

        # ── [버그1 수정] CDN URL 패턴 제목 차단 ────────────────────────────────
        # PotPlayer 가 스트림 URL 을 직접 재생하면 창 제목이 CDN URL 또는
        # 미디어 파일명으로 표시된다. 이미 실제 제목이 저장돼 있으면 유지.
        existing_title = records[-1].get("title", "")
        if existing_title and _is_cdn_url_title(title):
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] 🔗 CDN URL 제목 차단 — "
                f"기존 제목 유지: {existing_title!r} (무시된 제목: {title[:60]!r})")
            return

        # ── [Facebook 제목 정리] 조회수·반응수 등 불필요한 접두어 제거 ──────────
        # PotPlayer 가 Facebook 영상을 재생하면 창 제목에
        # "1.9K views · 18 reactions | 실제 제목" 형태가 포함될 수 있다.
        # "|" 구분자 기준 마지막 부분만 실제 제목으로 사용한다.
        last_url = records[-1].get("url", "")
        if re.search(r'(facebook\.com|fb\.watch|fb\.com)', last_url.lower()):
            if "|" in title:
                # parts[0] = 조회수·반응수 등 불필요한 접두어
                # parts[1] = 실제 제목  (두 번째 | 이후 이름 등은 버림)
                cleaned = title.split("|")[1].strip()
                if cleaned:  # 빈 문자열 방지
                    self._log_lines.append(
                        f"[{_time.strftime('%H:%M:%S')}] 🔗 Facebook 제목 정리: "
                        f"{title!r} → {cleaned!r}")
                    title = cleaned

        # 마지막 항목 제목 갱신
        records[-1]["title"] = title

        # ── 계열(시리즈) 기반 중복 제거 ─────────────────────────────────────
        # _strip_series_name 으로 계열명 추출 후 동일 계열의 이전 기록 삭제
        new_series = _strip_series_name(title)
        if new_series:
            current_url = records[-1].get("url", "")
            kept = []
            for rec in records[:-1]:   # 마지막(방금 갱신된 것) 제외한 이전 기록들
                existing_title = rec.get("title", "")
                if existing_title:
                    existing_series = _strip_series_name(existing_title)
                    # 계열명이 같고 URL이 다르면 이전 화수 기록으로 간주 → 제거
                    if (existing_series
                            and existing_series == new_series
                            and rec.get("url", "") != current_url):
                        self._log_lines.append(
                            f"[{_time.strftime('%H:%M:%S')}] 🔗 링크 계열 덮어쓰기: "
                            f"{existing_title} → {title}")
                        continue   # 이전 기록 드롭
                kept.append(rec)
            kept.append(records[-1])   # 갱신된 최신 기록 추가
            records = kept

        self._save_live_history(records)
        self._refresh_live_history_list()
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

    # ── PotPlayer 부가 파일 자동 설치 ────────────────────────────────────────

    def _get_potplayer_dir(self) -> str:
        """PotPlayer 설치 디렉터리를 반환한다. 찾지 못하면 빈 문자열 반환."""
        candidates = [
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
                                candidates.insert(0, exe)
                    except Exception:
                        pass
        except Exception:
            pass
        for path in candidates:
            if os.path.isfile(path):
                return os.path.dirname(path)
        return ""

    @staticmethod
    def _runas_powershell(ps_cmd: str) -> bool:
        """PowerShell 명령을 관리자 권한(UAC)으로 실행한다. 성공 시 True 반환."""
        import ctypes
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "powershell.exe",
            f"-NonInteractive -WindowStyle Hidden -Command \"{ps_cmd}\"",
            None, 0,
        )
        return ret > 32

    def _bg_ensure_potplayer_ytdlp(self, pot_dir: str, uac_queue: list | None = None):
        """PotPlayer Module 폴더에 yt-dlp.exe 가 없으면 GitHub 최신 버전에서 다운로드.

        C:\\Program Files 는 일반 권한으로 쓸 수 없으므로:
          1) %TEMP% 에 먼저 다운로드
          2) 직접 복사 시도 → PermissionError 면 UAC(ShellExecuteW runas) 로 복사
        uac_queue 가 전달되면 UAC 호출 대신 명령을 큐에 추가하고 반환
        (caller 가 한 번에 일괄 처리하므로 관리자 권한 팝업이 1회만 표시됨)
        """
        import tempfile, urllib.request, shutil
        module_dir = os.path.join(pot_dir, "Module")
        dest       = os.path.join(module_dir, "yt-dlp.exe")
        tmp_path   = os.path.join(tempfile.gettempdir(), "yt-dlp_potplayer.exe")
        _skip_tmp_cleanup = False
        try:
            if os.path.isfile(dest):
                return
            ts = _time.strftime("%H:%M:%S")
            self._log_lines.append(f"[{ts}] ⬇ PotPlayer Module/yt-dlp.exe 다운로드 시작")
            urllib.request.urlretrieve(
                "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
                tmp_path)
            try:
                # ① 직접 복사 (관리자로 실행 중이거나 권한 있을 때)
                os.makedirs(module_dir, exist_ok=True)
                shutil.copy2(tmp_path, dest)
                self._log_lines.append(
                    f"[{_time.strftime('%H:%M:%S')}] ✅ PotPlayer Module/yt-dlp.exe 설치 완료")
            except PermissionError:
                ps = f"Copy-Item -Path '{tmp_path}' -Destination '{dest}' -Force"
                if uac_queue is not None:
                    # ② UAC를 caller에서 일괄 처리 — tmp_path는 caller가 정리
                    uac_queue.append({"key": "ytdlp_mod", "check": dest,
                                      "ps": ps, "tmp": tmp_path})
                    _skip_tmp_cleanup = True
                    return
                # ② 권한 없음 → UAC로 PowerShell 복사 (단발성 팝업)
                ok = self._runas_powershell(ps)
                _time.sleep(4)   # UAC 승인 + 복사 완료 대기
                result = os.path.isfile(dest)
                self._log_lines.append(
                    f"[{_time.strftime('%H:%M:%S')}] "
                    + ("✅ PotPlayer Module/yt-dlp.exe 설치 완료 (관리자)" if result
                       else "⚠ PotPlayer Module/yt-dlp.exe 설치 실패 (UAC 거부)"))
        except Exception as e:
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ⚠ PotPlayer Module/yt-dlp.exe 설치 실패: {e}")
        finally:
            if not _skip_tmp_cleanup:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass

    def _bg_ensure_potplayer_extension(self, pot_dir: str, uac_queue: list | None = None):
        """Extension\\Media\\UrlList 및 Extension\\Media\\PlayParse 에
        'MediaPlayParse - yt-dlp.as' 가 없으면
        서버 업데이트 시트 B3 URL 의 zip 을 내려받아 압축 해제 후 zip 삭제.

        C:\\Program Files 쓰기 권한 없을 경우:
          1) %TEMP% 에 다운로드 + 압축 해제
          2) 직접 복사 시도 → PermissionError 면 UAC(ShellExecuteW runas) 로 복사
        uac_queue 가 전달되면 UAC 호출 대신 명령을 큐에 추가하고 반환
        (caller 가 한 번에 일괄 처리하므로 관리자 권한 팝업이 1회만 표시됨)
        """
        import tempfile, urllib.request, zipfile, shutil

        _install_dirs = [
            os.path.join(pot_dir, "Extension", "Media", "UrlList"),
            os.path.join(pot_dir, "Extension", "Media", "PlayParse"),
        ]
        _filename = "MediaPlayParse - yt-dlp.as"
        if all(os.path.isfile(os.path.join(d, _filename)) for d in _install_dirs):
            return

        ext_dir     = _install_dirs[0]
        target_file = os.path.join(ext_dir, _filename)
        tmp_zip     = os.path.join(tempfile.gettempdir(), "_as_potplayer.zip")
        tmp_ext_dir = os.path.join(tempfile.gettempdir(), "_as_potplayer_ext")
        _skip_ext_cleanup = False
        try:
            if os.path.isfile(target_file):
                return
            # B3 URL 취득
            ext_url = ""
            try:
                import auth as _auth_mod
                resp = _auth_mod.check_version()
                ext_url = resp.get("ext_url", "").strip()
            except Exception:
                pass
            if not ext_url:
                self._log_lines.append(
                    f"[{_time.strftime('%H:%M:%S')}] ⚠ Extension 설치: 서버 B3 URL 없음")
                return
            ts = _time.strftime("%H:%M:%S")
            self._log_lines.append(
                f"[{ts}] ⬇ PotPlayer Extension/MediaPlayParse - yt-dlp.as 다운로드 시작")
            # Google Drive 공유 URL → 직접 다운로드 URL 변환 (confirm=t 포함)
            import re as _re
            _dl_url = ext_url
            for _pat in (
                r'drive\.google\.com/file/d/([^/?]+)',
                r'drive\.google\.com/open\?id=([^&]+)',
                r'drive\.google\.com/uc[?&].*?id=([^&]+)',
                r'drive\.usercontent\.google\.com/download.*?[?&]id=([^&]+)',
            ):
                _m = _re.search(_pat, _dl_url)
                if _m:
                    _dl_url = (
                        "https://drive.usercontent.google.com/download"
                        f"?id={_m.group(1)}&export=download&confirm=t"
                    )
                    break
            urllib.request.urlretrieve(_dl_url, tmp_zip)
            if os.path.exists(tmp_ext_dir):
                shutil.rmtree(tmp_ext_dir, ignore_errors=True)
            os.makedirs(tmp_ext_dir, exist_ok=True)
            with zipfile.ZipFile(tmp_zip, "r") as zf:
                zf.extractall(tmp_ext_dir)

            _uac_dirs = []
            for _tgt_dir in _install_dirs:
                if os.path.isfile(os.path.join(_tgt_dir, _filename)):
                    continue
                try:
                    os.makedirs(_tgt_dir, exist_ok=True)
                    for f in os.listdir(tmp_ext_dir):
                        shutil.copy2(os.path.join(tmp_ext_dir, f),
                                     os.path.join(_tgt_dir, f))
                    self._log_lines.append(
                        f"[{_time.strftime('%H:%M:%S')}] ✅ "
                        f"{os.path.relpath(_tgt_dir, pot_dir)} 설치 완료")
                except PermissionError:
                    _uac_dirs.append(_tgt_dir)

            if _uac_dirs:
                ps_parts = []
                for _d in _uac_dirs:
                    ps_parts.append(
                        f"New-Item -ItemType Directory -Force -Path '{_d}' | Out-Null; "
                        f"Copy-Item -Path '{tmp_ext_dir}\\*' -Destination '{_d}' -Force")
                if uac_queue is not None:
                    # UAC를 caller에서 일괄 처리 — tmp_ext_dir는 caller가 정리
                    uac_queue.append({"key": "extension",
                                      "check": os.path.join(_install_dirs[0], _filename),
                                      "ps": "; ".join(ps_parts),
                                      "tmp": tmp_ext_dir})
                    _skip_ext_cleanup = True
                    return
                self._runas_powershell("; ".join(ps_parts))
                _time.sleep(4)
                for _d in _uac_dirs:
                    result = os.path.isfile(os.path.join(_d, _filename))
                    self._log_lines.append(
                        f"[{_time.strftime('%H:%M:%S')}] "
                        + (f"✅ {os.path.relpath(_d, pot_dir)} 설치 완료 (관리자)"
                           if result
                           else f"⚠ {os.path.relpath(_d, pot_dir)} 설치 실패 (UAC 거부)"))
        except Exception as e:
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ⚠ PotPlayer Extension/yt-dlp.as 설치 실패: {e}")
        finally:
            try:
                if os.path.exists(tmp_zip):
                    os.remove(tmp_zip)
            except Exception:
                pass
            if not _skip_ext_cleanup:
                try:
                    shutil.rmtree(tmp_ext_dir, ignore_errors=True)
                except Exception:
                    pass

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
    # N_m3u8DL-RE GitHub API (최신 릴리즈 자동 조회, Windows x64)
    _NM3U8_RE_API_URL = (
        "https://api.github.com/repos/nilaoda/N_m3u8DL-RE/releases/latest"
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
            return self._verify_ffmpeg(found)

        local = self._ffmpeg_path()
        if os.path.isfile(local):
            self._log_lines.append(f"[{_time.strftime('%H:%M:%S')}] ✅ ffmpeg (로컬): {local}")
            return self._verify_ffmpeg(local)

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
        return self._verify_ffmpeg(local)

    def _verify_ffmpeg(self, ffmpeg_path: str) -> str:
        """ffmpeg 가 실제로 실행 가능한지 확인한다.

        ffmpeg -version 을 실행해 버전 문자열이 반환되면 정상.
        실패 시 RuntimeError 를 발생시켜 다운로드 체인을 즉시 중단한다.
        (경로는 있는데 실행 안 되는 경우 — 손상된 바이너리, 권한 오류 등)
        """
        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)
        try:
            _r = subprocess.run(
                [ffmpeg_path, "-version"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",  # cp949 오류 방지
                timeout=10,
                creationflags=0x08000000 if os.name == "nt" else 0)
            if _r.returncode == 0:
                _ver = _r.stdout.splitlines()[0] if _r.stdout else "?"
                self._log_lines.append(
                    f"[{_time.strftime('%H:%M:%S')}] ✅ ffmpeg 검증 OK: {_ver}")
                return ffmpeg_path
            else:
                raise RuntimeError(
                    f"ffmpeg 실행 실패 (returncode={_r.returncode}): "
                    f"{_r.stderr.strip()[:200]}")
        except FileNotFoundError:
            raise RuntimeError(f"ffmpeg 실행파일을 찾을 수 없습니다: {ffmpeg_path}")
        except subprocess.TimeoutExpired:
            raise RuntimeError("ffmpeg -version 타임아웃 (10초 초과)")
        except RuntimeError:
            raise
        except Exception as _e:
            raise RuntimeError(f"ffmpeg 검증 중 오류: {_e}") from _e

    # ── N_m3u8DL-RE ──────────────────────────────────────────────────────────

    def _nm3u8dl_re_path(self) -> str:
        return os.path.join(self.APP_DIR, "N_m3u8DL-RE.exe")

    def _ensure_nm3u8dl_re(self) -> str:
        """N_m3u8DL-RE 실행파일을 확보해 경로를 반환한다.
        없으면 GitHub Releases API에서 최신 Windows x64 버전을 자동 다운로드한다.
        """
        import shutil, zipfile
        import json as _json
        import urllib.request

        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)

        found = shutil.which("N_m3u8DL-RE")
        if found:
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ✅ N_m3u8DL-RE (PATH): {found}")
            return found

        local = self._nm3u8dl_re_path()
        if os.path.isfile(local):
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ✅ N_m3u8DL-RE (로컬): {local}")
            return local

        # GitHub API로 최신 릴리즈 zip URL 조회
        self._log_lines.append(
            f"[{_time.strftime('%H:%M:%S')}] ⬇ N_m3u8DL-RE 최신 버전 조회 중…")
        self.root.after(0, lambda: self._link_status("⬇ N_m3u8DL-RE 버전 조회 중…"))
        req = urllib.request.Request(
            self._NM3U8_RE_API_URL,
            headers={"User-Agent": "AutoSinc"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            release_info = _json.loads(resp.read().decode())

        # win-x64 zip 에셋 선택
        asset_url = None
        for asset in release_info.get("assets", []):
            name = asset.get("name", "")
            if "win" in name.lower() and "x64" in name.lower() and name.endswith(".zip"):
                asset_url = asset["browser_download_url"]
                break
        if asset_url is None:
            raise RuntimeError("N_m3u8DL-RE Windows x64 zip 에셋을 찾을 수 없습니다.")

        os.makedirs(self.APP_DIR, exist_ok=True)
        zip_path = local + ".zip"
        self._log_lines.append(
            f"[{_time.strftime('%H:%M:%S')}] ⬇ N_m3u8DL-RE 다운로드 시작: {asset_url}")
        self._download_file(asset_url, zip_path, "N_m3u8DL-RE")

        self.root.after(0, lambda: self._link_status("⬇ N_m3u8DL-RE 압축 해제 중…"))
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                target = next(
                    (n for n in zf.namelist()
                     if n.lower().endswith("n_m3u8dl-re.exe")),
                    None)
                if target is None:
                    raise RuntimeError("zip 안에서 N_m3u8DL-RE.exe를 찾을 수 없습니다.")
                with zf.open(target) as src, open(local, "wb") as dst:
                    dst.write(src.read())
        finally:
            try:
                os.remove(zip_path)
            except Exception:
                pass

        self._log_lines.append(
            f"[{_time.strftime('%H:%M:%S')}] ✅ N_m3u8DL-RE 설치 완료: {local}")
        return local

    # ── Playwright (헤드리스 브라우저 m3u8 추출) ──────────────────────────────

    # ── Python 인터프리터 경로 확보 헬퍼 ─────────────────────────────────────
    @staticmethod
    def _resolve_python_exe() -> str:
        """실행 가능한 Python 인터프리터 경로를 반환한다.

        [WinError 193] 방어 로직:
          1. PyInstaller 패키징 환경(sys.frozen=True)에서는 sys.executable 이
             앱 자신의 .exe 이므로 Python 인터프리터로 사용할 수 없다.
             → PATH 에서 python / python3 을 직접 탐색한다.
          2. pythonw.exe 로 실행된 경우 python.exe 로 교체를 시도한다.
             (pythonw.exe 는 서브프로세스 stdout/stderr 캡처에 문제가 있음)
          3. 위 모든 방법이 실패하면 RuntimeError 를 발생시켜
             호출부에서 Playwright 준비 실패로 처리하도록 한다.
        """
        import sys, shutil

        # ── 1단계: PyInstaller 패키징 여부 확인 ──────────────────────────
        if not getattr(sys, "frozen", False):
            _exe = sys.executable or ""

            # ── 2단계: pythonw.exe → python.exe 교체 시도 ────────────────
            if os.name == "nt" and _exe.lower().endswith("pythonw.exe"):
                _candidate = _exe[:-len("pythonw.exe")] + "python.exe"
                if os.path.isfile(_candidate):
                    return _candidate

            # ── 3단계: sys.executable 이 직접 실행 가능한지 확인 ──────────
            if _exe and os.path.isfile(_exe):
                return _exe

        # ── 4단계: PATH 에서 인터프리터 탐색 ─────────────────────────────
        # Python 버전 구분 없이 찾을 수 있는 이름을 우선 순서로 시도
        for _name in ("python3", "python", "python3.exe", "python.exe"):
            _found = shutil.which(_name)
            if _found:
                return _found

        raise RuntimeError(
            "Python 인터프리터를 찾을 수 없습니다 — "
            "PATH 에 python 또는 python3 가 없습니다.")

    def _ensure_playwright(self):
        """playwright Python 패키지와 Chromium 브라우저를 확보한다.
        없으면 pip install 후 playwright install chromium 을 실행한다.
        """
        import importlib

        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)

        # [WinError 193 수정] sys.executable 을 직접 쓰지 않고
        # 실제로 실행 가능한 Python 인터프리터를 안전하게 확보한다.
        _py_exe = self._resolve_python_exe()
        self._log_lines.append(
            f"[{_time.strftime('%H:%M:%S')}] 🐍 Python 인터프리터: {_py_exe}")

        # 패키지 설치 여부 확인
        if importlib.util.find_spec("playwright") is None:
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ⬇ playwright 패키지 설치 중…")
            self.root.after(0, lambda: self._link_status(
                "⬇ playwright 설치 중… (최초 1회)"))
            _pip = subprocess.run(
                [_py_exe, "-m", "pip", "install", "playwright",
                 "--quiet", "--disable-pip-version-check"],
                capture_output=True, text=True,
                creationflags=0x08000000 if os.name == "nt" else 0)
            if _pip.returncode != 0:
                raise RuntimeError(
                    f"playwright pip 설치 실패: {_pip.stderr.strip()[:200]}")
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ✅ playwright 패키지 설치 완료")
            # 설치 후 모듈 캐시 갱신 (같은 프로세스에서 바로 import 가능하도록)
            importlib.invalidate_caches()

        # Chromium 브라우저 설치 여부 확인
        _chromium_ok = False
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as _pw:
                _chromium_ok = os.path.isfile(
                    _pw.chromium.executable_path)
        except Exception:
            pass

        if not _chromium_ok:
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ⬇ Playwright Chromium 설치 중…")
            self.root.after(0, lambda: self._link_status(
                "⬇ Playwright Chromium 설치 중… (최초 1회, 수분 소요)"))
            _inst = subprocess.run(
                [_py_exe, "-m", "playwright", "install", "chromium"],
                capture_output=True, text=True,
                creationflags=0x08000000 if os.name == "nt" else 0)
            if _inst.returncode != 0:
                raise RuntimeError(
                    f"Chromium 설치 실패: {_inst.stderr.strip()[:200]}")
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ✅ Playwright Chromium 설치 완료")

    def _find_real_chrome(self) -> str | None:
        """사용자 PC에 설치된 실제 Chrome/Edge 실행파일 경로를 반환한다."""
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(
                r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        for p in candidates:
            if os.path.isfile(p):
                return p
        return None

    def _extract_m3u8_playwright(self, page_url: str,
                                  timeout_ms: int = 30000) -> dict | None:
        """실제 Chrome을 CDP로 실행해 page_url 의 m3u8 URL과 헤더/쿠키를 반환한다.

        [개선사항]
          1. request + response 양쪽에서 m3u8 감지
             - request  이벤트: URL만 보고 빠르게 캡처
             - response 이벤트: Content-Type 헤더가 m3u8인 응답도 캡처
               (URL에 .m3u8이 없어도 application/vnd.apple.mpegurl 등으로 판별)
          2. m3u8 요청의 실제 헤더(Referer, Authorization, Cookie 등) 캡처
             - 페이지 쿠키가 아닌 실제 요청 헤더를 그대로 다운로더에 전달
          3. 재생 트리거 강화
             - 클릭 → JS click() dispatch → 스크롤 → video.play() 순으로 시도
          4. 자동화 탐지 우회 강화
             - navigator.webdriver 패치 (내장 Chromium 사용 시)
             - Chrome 미설치 시에도 실제 User-Agent 주입

        반환: {"url": str, "referer": str, "cookies": str, "req_headers": dict} 또는 None
        """
        import subprocess as _sub, socket, time as _tm, json as _json
        import urllib.request as _ureq

        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)

        try:
            from playwright.sync_api import sync_playwright, TimeoutError as _PWTimeout
        except ImportError:
            return None

        # ── 감지 결과 저장소 ──────────────────────────────────────────────
        # _found_info: {"url": str, "req_headers": dict}
        _found_info: dict | None = None
        _found_subs: list        = []   # [{url, req_headers}, ...]
        _cookies_list: list      = []
        _chrome_proc             = None

        # m3u8 판별 정규식 (URL 기반)
        _M3U8_URL_RE = re.compile(r'\.m3u8(?:[?#]|$)', re.IGNORECASE)
        # m3u8 판별 Content-Type 키워드 (응답 헤더 기반)
        _M3U8_CT_KW  = ("mpegurl", "m3u8", "vnd.apple")
        # 자막 URL 판별
        _SUB_URL_RE  = re.compile(
            r'\.(?:vtt|srt|ttml|dfxp)(?:[?#]|$)', re.IGNORECASE)

        # ── 빈 포트 탐색 ──────────────────────────────────────────────────
        def _free_port() -> int:
            with socket.socket() as _s:
                _s.bind(("127.0.0.1", 0))
                return _s.getsockname()[1]

        _dbg_port = _free_port()

        try:
            with sync_playwright() as _pw:
                _chrome_exe = self._find_real_chrome()

                if _chrome_exe:
                    # ── CDP: 실제 Chrome 을 headless 로 직접 실행 ──────────
                    self._log_lines.append(
                        f"[{_time.strftime('%H:%M:%S')}] 🌐 CDP 실제 Chrome: {_chrome_exe}")
                    _chrome_proc = _sub.Popen(
                        [
                            _chrome_exe,
                            f"--remote-debugging-port={_dbg_port}",
                            "--headless=new",
                            "--no-first-run",
                            "--no-default-browser-check",
                            "--disable-extensions",
                            "--disable-blink-features=AutomationControlled",
                            "--disable-gpu",
                            "--autoplay-policy=no-user-gesture-required",
                            "about:blank",
                        ],
                        stdout=_sub.DEVNULL,
                        stderr=_sub.DEVNULL,
                        creationflags=0x08000000 if os.name == "nt" else 0,
                    )
                    # CDP 포트가 열릴 때까지 최대 5초 대기
                    _cdp_url = f"http://127.0.0.1:{_dbg_port}"
                    for _ in range(20):
                        _tm.sleep(0.25)
                        try:
                            with _ureq.urlopen(
                                    f"{_cdp_url}/json/version", timeout=1):
                                break
                        except Exception:
                            pass

                    _browser = _pw.chromium.connect_over_cdp(_cdp_url)
                    _ctx     = _browser.contexts[0] if _browser.contexts \
                               else _browser.new_context()
                else:
                    # ── 폴백: 내장 Chromium launch ────────────────────────
                    self._log_lines.append(
                        f"[{_time.strftime('%H:%M:%S')}] 🌐 내장 Chromium 사용 (Chrome 미감지)")
                    _browser = _pw.chromium.launch(
                        headless=True,
                        args=[
                            "--disable-blink-features=AutomationControlled",
                            "--autoplay-policy=no-user-gesture-required",
                        ])
                    _ctx = _browser.new_context(
                        user_agent=_UA,
                        ignore_https_errors=True)

                # ── [개선1] request + response 양쪽에서 m3u8 감지 ──────────
                #
                # ▸ request 이벤트:
                #   브라우저가 요청을 보내기 직전에 발생.
                #   URL에 .m3u8 이 있으면 즉시 캡처.
                #   shouldInterceptRequest 와 동일한 타이밍.
                #
                # ▸ response 이벤트:
                #   서버 응답이 도착했을 때 발생.
                #   URL에 .m3u8 이 없어도 Content-Type 으로 HLS 판별 가능.
                #   (일부 사이트는 /stream?id=xxx 같은 URL로 m3u8을 내려줌)

                def _capture(url: str, req_headers: dict):
                    """m3u8 URL을 처음 발견했을 때만 저장한다."""
                    nonlocal _found_info
                    if _found_info is None:
                        _found_info = {"url": url, "req_headers": req_headers}

                def _on_request(req):
                    """[개선2] URL 기반 감지 + 요청 헤더 캡처 (m3u8 + 자막)."""
                    _u = req.url
                    try:
                        _hdrs = dict(req.headers)
                    except Exception:
                        _hdrs = {}
                    if not _found_info and _M3U8_URL_RE.search(_u):
                        _capture(_u, _hdrs)
                        self._log_lines.append(
                            f"[{_time.strftime('%H:%M:%S')}] "
                            f"📡 [request] m3u8 감지: {_u[:80]}")
                    if _SUB_URL_RE.search(_u):
                        if not any(s["url"] == _u for s in _found_subs):
                            _found_subs.append({"url": _u, "req_headers": _hdrs})
                            self._log_lines.append(
                                f"[{_time.strftime('%H:%M:%S')}] "
                                f"📡 [request] 자막 감지: {_u[:80]}")

                def _on_response(resp):
                    """[개선1] Content-Type 기반 감지 (URL에 .m3u8 없는 경우)."""
                    if _found_info:
                        return
                    try:
                        _ct = resp.headers.get("content-type", "")
                    except Exception:
                        return
                    if any(kw in _ct.lower() for kw in _M3U8_CT_KW):
                        _u = resp.url
                        try:
                            _hdrs = dict(resp.request.headers)
                        except Exception:
                            _hdrs = {}
                        _capture(_u, _hdrs)
                        self._log_lines.append(
                            f"[{_time.strftime('%H:%M:%S')}] "
                            f"📡 [response] m3u8 감지 (Content-Type={_ct[:40]}): "
                            f"{_u[:80]}")

                _ctx.on("request",  _on_request)
                _ctx.on("response", _on_response)

                # ── [개선4] navigator.webdriver 패치 (내장 Chromium 전용) ──
                # 실제 Chrome(CDP 연결)은 이미 패치 불필요
                if not _chrome_exe:
                    _ctx.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined
                        });
                        window.chrome = { runtime: {} };
                    """)

                _page = _ctx.new_page()

                # ── 페이지 로드 ───────────────────────────────────────────
                try:
                    _page.goto(page_url,
                               wait_until="domcontentloaded",
                               timeout=timeout_ms)
                except _PWTimeout:
                    pass

                # ── [개선3] 재생 트리거 강화 ─────────────────────────────
                #
                # 단계별 시도:
                #   1) CSS 셀렉터로 재생 버튼 클릭 (기존 방식)
                #   2) JS dispatchEvent('click') — click() 을 막는 사이트 우회
                #   3) 페이지 스크롤 — lazy-load 동영상 활성화
                #   4) video.play() 직접 호출 — autoplay 차단 우회
                #   5) 스크롤 후 video.play() 재시도

                _PLAY_SELECTORS = [
                    "button.play",
                    "button[aria-label*='play' i]",
                    "button[title*='play' i]",
                    ".play-button",
                    ".vjs-big-play-button",
                    ".plyr__control--overlaid",
                    "[class*='play'][class*='btn']",
                    "[class*='btn'][class*='play']",
                    "[data-testid*='play']",
                    "video",
                ]

                def _try_click_selectors():
                    """1단계: CSS 셀렉터 클릭."""
                    for _sel in _PLAY_SELECTORS:
                        if _found_info:
                            return
                        try:
                            _page.locator(_sel).first.click(timeout=1500)
                        except Exception:
                            pass

                def _try_js_dispatch():
                    """2단계: JS dispatchEvent 로 클릭 강제."""
                    if _found_info:
                        return
                    try:
                        _page.evaluate("""() => {
                            const sels = [
                                'button.play', '.vjs-big-play-button',
                                '.plyr__control--overlaid', 'video',
                                '[class*="play"]'
                            ];
                            for (const s of sels) {
                                const el = document.querySelector(s);
                                if (el) {
                                    el.dispatchEvent(
                                        new MouseEvent('click',
                                            {bubbles: true, cancelable: true}));
                                    break;
                                }
                            }
                        }""")
                    except Exception:
                        pass

                def _try_scroll():
                    """3단계: 스크롤로 lazy-load 동영상 활성화."""
                    if _found_info:
                        return
                    try:
                        _page.evaluate(
                            "window.scrollTo(0, document.body.scrollHeight * 0.3)")
                        _tm.sleep(0.5)
                        _page.evaluate("window.scrollTo(0, 0)")
                    except Exception:
                        pass

                def _try_video_play():
                    """4단계: video.play() 직접 호출."""
                    if _found_info:
                        return
                    try:
                        _page.evaluate("""() => {
                            const videos = document.querySelectorAll('video');
                            videos.forEach(v => {
                                v.muted = true;
                                v.play().catch(() => {});
                            });
                        }""")
                    except Exception:
                        pass

                # 재생 트리거 순차 실행
                _try_click_selectors()
                if not _found_info:
                    _try_js_dispatch()
                if not _found_info:
                    _try_scroll()
                if not _found_info:
                    _try_video_play()

                # ── m3u8 감지될 때까지 최대 20초 폴링 ────────────────────
                _deadline      = _tm.time() + 20
                _last_retry_at = 0   # video.play() 재시도 마지막 구간 기록
                while not _found_info and _tm.time() < _deadline:
                    _tm.sleep(0.5)
                    # 5초·10초 구간에 video.play() 재시도
                    # 부동소수점 오차로 정확히 5.0/10.0이 되지 않으므로
                    # ±0.4초 허용 범위(0.5초 sleep 주기의 절반 미만)로 비교한다.
                    _elapsed = 20 - (_deadline - _tm.time())
                    for _target in (5, 10):
                        if (abs(_elapsed - _target) < 0.4
                                and _last_retry_at != _target):
                            _last_retry_at = _target
                            _try_video_play()
                            break

                # ── 쿠키 수집 ─────────────────────────────────────────────
                try:
                    _cookies_list = _ctx.cookies()
                except Exception:
                    _cookies_list = []

                # ── 페이지 제목 추출 ──────────────────────────────────────
                # 앱의 onReceivedTitle 방식과 동일:
                #   1순위: og:title 메타태그 (대부분의 동영상 사이트)
                #   2순위: twitter:title 메타태그
                #   3순위: document.title (브라우저 탭 제목)
                _page_title = ""
                try:
                    _page_title = _page.evaluate("""() => {
                        const og = document.querySelector(
                            'meta[property="og:title"]');
                        if (og && og.content) return og.content.trim();
                        const tw = document.querySelector(
                            'meta[name="twitter:title"]');
                        if (tw && tw.content) return tw.content.trim();
                        return document.title.trim();
                    }""") or ""
                except Exception:
                    pass

                try:
                    _browser.close()
                except Exception:
                    pass

        except Exception as _e:
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ⚠ CDP/Playwright 오류: {_e}")
            return None
        finally:
            # Chrome 프로세스 정리
            if _chrome_proc is not None:
                try:
                    _chrome_proc.terminate()
                except Exception:
                    pass

        if _found_info:
            _req_hdrs    = _found_info.get("req_headers", {})
            _req_cookie  = _req_hdrs.get("cookie", "")
            _page_cookie = "; ".join(
                f"{c['name']}={c['value']}" for c in _cookies_list)
            _final_cookie = _req_cookie if _req_cookie else _page_cookie
            _req_referer  = _req_hdrs.get("referer", "") or page_url

            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] 🔗 m3u8 감지 완료: "
                f"{_found_info['url'][:80]} | "
                f"제목={_page_title[:30]!r} | "
                f"쿠키={len(_cookies_list)}개")
            return {
                "url":         _found_info["url"],
                "referer":     _req_referer,
                "cookies":     _final_cookie,
                "req_headers": _req_hdrs,
                "title":       _page_title,   # 페이지에서 추출한 동영상 제목
                "subtitles":   _found_subs,   # [{url, req_headers}, ...] 자막 URL 목록
            }
        return None

    def _fetch_page_title(self, url: str) -> str:
        """URL 페이지에서 og:title → twitter:title → <title> 순으로 제목을 추출한다."""
        import urllib.request
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": _UA,
                    "Accept-Language": "ko-KR,ko;q=0.9",
                })
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read(65536).decode("utf-8", errors="replace")
            for pat in [
                r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
                r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:title["\']',
                r'<title[^>]*>([^<]+)</title>',
            ]:
                m = re.search(pat, raw, re.IGNORECASE)
                if m:
                    return m.group(1).strip()
        except Exception:
            pass
        return ""

    def _dl_stop_btn_show(self):
        """저장 중지 버튼 표시 + 자막 토글 버튼을 중지 버튼 오른쪽으로 이동."""
        btn = getattr(self, "_link_stop_btn", None)
        if btn:
            try:
                r = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
                btn.pack(side="left", padx=(round(4*r), 0))
            except Exception:
                pass
        for _attr, _kw_attr in (
            ("_link_sub_video_btn", "_link_sub_video_btn_pack_kw"),
            ("_link_sub_both_btn",  "_link_sub_both_btn_pack_kw"),
        ):
            _b  = getattr(self, _attr, None)
            _kw = getattr(self, _kw_attr, dict(side="left"))
            if _b:
                try:
                    _b.pack_forget()
                    _b.pack(**_kw)
                except Exception:
                    pass

    def _dl_stop_btn_hide(self):
        """저장 중지 버튼 숨기기 + 자막 토글 버튼 원상복구."""
        btn = getattr(self, "_link_stop_btn", None)
        if btn:
            try:
                btn.pack_forget()
            except Exception:
                pass
        for _attr, _kw_attr in (
            ("_link_sub_video_btn", "_link_sub_video_btn_pack_kw"),
            ("_link_sub_both_btn",  "_link_sub_both_btn_pack_kw"),
        ):
            _b  = getattr(self, _attr, None)
            _kw = getattr(self, _kw_attr, dict(side="left"))
            if _b:
                try:
                    _b.pack_forget()
                    _b.pack(**_kw)
                except Exception:
                    pass

    def _link_save_cancel(self):
        """중지 버튼 콜백 — 진행 중인 저장을 즉시 중단하고 잔여 파일을 정리한다.

        [버그3 수정] yt-dlp 프로세스 트리(ffmpeg 포함) 전체 종료:
          - subprocess.run(taskkill) 에 creationflags=CREATE_NO_WINDOW 추가
            → CMD 창이 순간적으로 나타나지 않음
          - Win32 TerminateProcess API 를 직접 사용하는 폴백 추가
            → taskkill 바이너리 없이도 프로세스 트리 강제 종료 가능
          - _link_save_tracked_files 로 다운로드 중 생성된 모든 파일 추적 후 삭제

        [버그4 수정] 콘솔창 깜빡임 방지:
          - creationflags=0x08000000 (CREATE_NO_WINDOW) 를 taskkill 호출에도 적용
          - shell=False 유지 (shell=True 는 cmd.exe 를 거치므로 반드시 False)
        """
        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)

        # ① 취소 플래그 설정 (worker thread 의 post-wait 처리에서 감지)
        self._link_save_cancelled = True

        # ② yt-dlp + 자식 프로세스(ffmpeg) 전체 강제 종료
        proc = getattr(self, "_link_save_proc", None)
        pid  = proc.pid if (proc and proc.poll() is None) else None

        if pid:
            _killed = False

            # 방법1: taskkill /F /T — 콘솔 창 없이 실행 (CREATE_NO_WINDOW)
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    creationflags=0x08000000,   # CREATE_NO_WINDOW — CMD 깜빡임 방지
                    shell=False,                 # shell=True 이면 cmd.exe 경유 → 창 발생
                )
                _killed = True
            except FileNotFoundError:
                pass    # taskkill.exe 없는 환경 → 방법2로 폴백
            except Exception:
                pass

            if not _killed:
                # 방법2: Win32 TerminateProcess + 자식 프로세스 직접 탐색 후 종료
                try:
                    import ctypes
                    import ctypes.wintypes

                    TH32CS_SNAPPROCESS  = 0x00000002
                    PROCESS_TERMINATE   = 0x0001
                    PROCESS_QUERY_INFO  = 0x1000

                    class _PROCESSENTRY32(ctypes.Structure):
                        _fields_ = [
                            ("dwSize",              ctypes.wintypes.DWORD),
                            ("cntUsage",            ctypes.wintypes.DWORD),
                            ("th32ProcessID",       ctypes.wintypes.DWORD),
                            ("th32DefaultHeapID",   ctypes.POINTER(ctypes.c_ulong)),
                            ("th32ModuleID",        ctypes.wintypes.DWORD),
                            ("cntThreads",          ctypes.wintypes.DWORD),
                            ("th32ParentProcessID", ctypes.wintypes.DWORD),
                            ("pcPriClassBase",      ctypes.c_long),
                            ("dwFlags",             ctypes.wintypes.DWORD),
                            ("szExeFile",           ctypes.c_char * 260),
                        ]

                    k32 = ctypes.windll.kernel32
                    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
                    if snap and snap != ctypes.wintypes.HANDLE(-1).value:
                        entry = _PROCESSENTRY32()
                        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
                        children = []
                        if k32.Process32First(snap, ctypes.byref(entry)):
                            while True:
                                if entry.th32ParentProcessID == pid:
                                    children.append(entry.th32ProcessID)
                                if not k32.Process32Next(snap, ctypes.byref(entry)):
                                    break
                        k32.CloseHandle(snap)
                        for cpid in children:
                            h = k32.OpenProcess(PROCESS_TERMINATE, False, cpid)
                            if h:
                                k32.TerminateProcess(h, 1)
                                k32.CloseHandle(h)
                    # 부모(yt-dlp) 도 종료
                    try:
                        proc.kill()
                    except Exception:
                        pass
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        self._link_save_proc = None

        # 취소 즉시 UI 업데이트 (파일 삭제는 별도 스레드에서)
        self.root.after(0, self._dl_stop_btn_hide)
        self.root.after(0, lambda: self._link_save_btn.config(state="normal"))
        self.root.after(0, lambda: self._link_status("⏹ 중단 중… 잔여 파일 정리 중."))
        self._log_lines.append(
            f"[{_time.strftime('%H:%M:%S')}] ⏹ yt-dlp 중단 요청 (PID={pid})")

        # ③ 잔여 파일 삭제 — 별도 스레드에서 0.8초 대기 후 실행
        #    (프로세스 완전 종료 + OS 파일 핸들 해제 대기, 이전 0.6s → 0.8s)
        tracked_snapshot = set(getattr(self, "_link_save_tracked_files", set()))

        def _cleanup():
            _time.sleep(0.8)

            deleted = []

            # 취소 시점 스냅샷 + 취소 이후 worker thread 가 추가한 경로를 병합
            # (경쟁 조건: 스냅샷 캡처 직후에 Destination 라인이 출력된 경우 누락 방지)
            live_paths = getattr(self, "_link_save_tracked_files", set())
            all_paths  = tracked_snapshot | live_paths

            # yt-dlp stdout 에서 추적한 파일 + .part/.ytdl 임시 파일 모두 삭제
            for path in all_paths:
                for candidate in (path, path + ".part", path + ".ytdl"):
                    if os.path.isfile(candidate):
                        try:
                            os.remove(candidate)
                            deleted.append(os.path.basename(candidate))
                        except Exception:
                            pass

            # [버그2 수정] glob 으로 다운로드 디렉터리 내 잔여 임시 파일 보완 삭제
            #   취소가 "[download] Destination:" 출력 이전에 발생해
            #   tracked_files 가 비어 있을 경우에도 .part / .ytdl 파일을
            #   다운로드 디렉터리 전체에서 찾아 삭제한다.
            import glob as _glob
            _dl_dir = getattr(self, "_link_save_dl_dir", None)
            if _dl_dir and os.path.isdir(_dl_dir):
                for _pat in ("*.part", "*.ytdl"):
                    for _f in _glob.glob(
                            os.path.join(_dl_dir, "**", _pat), recursive=True):
                        if os.path.isfile(_f):
                            _bn = os.path.basename(_f)
                            if _bn not in deleted:
                                try:
                                    os.remove(_f)
                                    deleted.append(_bn)
                                except Exception:
                                    pass

            # 상태 초기화
            self._link_save_tracked_files = set()
            self._link_save_dest_glob     = None

            n  = len(deleted)
            ts = _time.strftime("%H:%M:%S")
            self._log_lines.append(
                f"[{ts}] ⏹ 잔여 파일 {n}개 삭제: {deleted}")

            self.root.after(0, self._dl_progress_hide)
            self.root.after(0, lambda: self._link_status(
                f"⏹ 저장 중단 — 잔여 파일 {n}개 삭제됨."))

        threading.Thread(
            target=_cleanup, daemon=True, name="dl-cancel-cleanup").start()

    def _link_save(self):
        """저장 버튼 콜백 — yt-dlp + ffmpeg를 사용해 최고 화질로 저장한다.
        두 실행파일이 없으면 최초 1회 자동 다운로드 후 APP_DIR에 캐시한다.
        """
        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)

        # 자막 포함 여부
        _save_with_sub = (
            getattr(self, "_link_subtitle_var", None) is not None
            and self._link_subtitle_var.get() == "both"
        )

        url = getattr(self, "_link_url_var", tk.StringVar()).get().strip()
        if not url:
            self._link_status("⚠ URL을 입력하세요.", warn=True)
            return

        import pathlib
        # 저장 위치: 링크 탭 저장 경로 (녹화/캡처 탭과 연동) → 설정폴더/Video/Download
        _rec_dir = getattr(self, "_record_save_dir", None)
        if not _rec_dir:
            _rec_dir = self._load_setting("record_save_dir", "")
        if _rec_dir and os.path.isdir(_rec_dir):
            dl_dir = os.path.join(_rec_dir, "Video", "Download")
        else:
            dl_dir = str(pathlib.Path.home() / "Downloads")
        try:
            os.makedirs(dl_dir, exist_ok=True)
        except Exception:
            dl_dir = self.APP_DIR

        # 스레드 시작 전 즉시 버튼 비활성화 — after(0) 비동기 처리 시
        # 더블클릭으로 중복 실행되는 경쟁 조건을 방지한다.
        try:
            self._link_save_btn.config(state="disabled")
        except Exception:
            pass

        def _run():
            ts = _time.strftime("%H:%M:%S")
            # 취소 플래그 초기화 + 파일 추적 셋 초기화
            self._link_save_cancelled     = False
            self._link_save_tracked_files = set()
            self._link_save_dl_dir        = dl_dir
            try:
                # ── 1) yt-dlp / ffmpeg / N_m3u8DL-RE 병렬 확보 ───────────────
                import concurrent.futures as _cf
                _results   = {}
                _exc       = {}

                def _fetch(key, fn):
                    try:
                        _results[key] = fn()
                    except Exception as _e:
                        _exc[key] = _e

                with _cf.ThreadPoolExecutor(max_workers=3) as _pool:
                    _pool.submit(_fetch, "ytdlp",   self._ensure_ytdlp)
                    _pool.submit(_fetch, "ffmpeg",  self._ensure_ffmpeg)
                    _pool.submit(_fetch, "nm3u8",   self._ensure_nm3u8dl_re)

                # yt-dlp / ffmpeg 실패 시 즉시 중단
                if "ytdlp" in _exc:
                    _err = str(_exc["ytdlp"])
                    self.root.after(0, lambda: self._link_status(
                        f"❌ yt-dlp 설치 실패: {_err}", warn=True))
                    self.root.after(0, self._dl_progress_hide)
                    self._log_lines.append(f"[{ts}] ❌ yt-dlp 설치 실패: {_err}")
                    return
                if "ffmpeg" in _exc:
                    _err = str(_exc["ffmpeg"])
                    self.root.after(0, lambda: self._link_status(
                        f"❌ ffmpeg 설치 실패: {_err}", warn=True))
                    self.root.after(0, self._dl_progress_hide)
                    self._log_lines.append(f"[{ts}] ❌ ffmpeg 설치 실패: {_err}")
                    return
                # N_m3u8DL-RE 설치 실패는 폴백 불가 경고만 기록 (중단하지 않음)
                if "nm3u8" in _exc:
                    self._log_lines.append(
                        f"[{ts}] ⚠ N_m3u8DL-RE 설치 실패 (폴백 불가): {_exc['nm3u8']}")

                ytdlp  = _results["ytdlp"]
                ffmpeg = _results["ffmpeg"]
                _nm3u8dl_re_exe = _results.get("nm3u8")  # None 이면 폴백 불가

                # ── 2) 영상 다운로드 (bestvideo+bestaudio → mp4 병합) ─────────
                self.root.after(0, lambda: self._dl_progress_update(0.0))
                self.root.after(0, lambda: self._link_status("동영상을 저장하고 있습니다."))
                self.root.after(0, self._dl_progress_show)
                self.root.after(0, self._dl_stop_btn_show)
                self.root.after(0, lambda: self._link_save_btn.config(state="disabled"))

                # 저장 파일 경로 패턴 추적 (중단 시 삭제용)
                self._link_save_dest_glob = os.path.join(dl_dir, "")

                # ── Facebook / Soop / 치지직 여부 판별 ──────────────────────
                _is_facebook = bool(re.search(
                    r'(facebook\.com|fb\.watch|fb\.com)', url.lower()))
                _is_chzzk = bool(re.search(
                    r'chzzk\.naver\.com', url.lower()))
                _is_soop = bool(_SOOP_RE.search(url))

                self._log_lines.append(
                    f"[{ts}] 💾 저장 시작 | 원본 URL: {url} | "
                    f"platform={'facebook' if _is_facebook else 'soop' if _is_soop else 'chzzk' if _is_chzzk else 'general'}")

                # ── yt-dlp 명령 구성 ───────────────────────────────────────────
                # [수정1] --postprocessor-args 뒤 쉼표 누락 버그 수정
                # [수정2] 병렬 다운로드 옵션 추가 (-N 8 / --concurrent-fragments 8)
                # [수정3] Facebook 쿠키·헤더 처리 추가
                # [수정4] 치지직 HLS 전용 옵션 분기 추가
                #
                # 치지직·Soop은 HLS(m3u8) 세그먼트 방식이므로 버퍼 1M 적용
                _buf = "1M" if (_is_chzzk or _is_soop) else "16K"

                # ── 재생목록 여부 사전 확인 → 출력 경로 결정 ──────────────────
                # 단일 영상이면 폴더 생성 없이 dl_dir 바로 저장,
                # 재생목록이면 기존대로 playlist_title 하위 폴더에 저장.
                _is_playlist = False
                try:
                    import json as _json
                    _meta_proc = __import__("subprocess").run(
                        [ytdlp, "--flat-playlist", "-J",
                         "--no-warnings", "--quiet", url],
                        capture_output=True, text=True, timeout=30,
                        creationflags=0x08000000 if os.name == "nt" else 0)
                    _meta = _json.loads(_meta_proc.stdout)
                    _is_playlist = (
                        _meta.get("_type") == "playlist"
                        and int(_meta.get("playlist_count") or 0) > 1
                    )
                except Exception:
                    pass  # 확인 실패 시 단일 영상으로 간주

                if _is_playlist:
                    _out_tmpl = os.path.join(
                        dl_dir, "%(playlist_title)s", "%(title)s.%(ext)s")
                else:
                    _out_tmpl = os.path.join(dl_dir, "%(title)s.%(ext)s")

                cmd = [
                    ytdlp,
                    "-f", "bestvideo+bestaudio/best",
                    "--merge-output-format", "mp4",
                    # 병렬 다운로드
                    "-N", "8",
                    "--concurrent-fragments", "8",
                    "--buffer-size", _buf,
                    # AAC 재인코딩 유지 + ffmpeg 진행률 출력 억제
                    # (-loglevel error: \r 출력 차단 → readline() 데드락 방지)
                    "--postprocessor-args", "ffmpeg:-loglevel error -c:a aac -b:a 192k",
                    "--ffmpeg-location", os.path.dirname(ffmpeg),
                    "--newline",
                    "-o", _out_tmpl,
                ]

                # ── 치지직 전용 HLS 속도 개선 옵션 ──────────────────────────
                # [버그1 수정] --hls-use-mpegts 제거
                #   --hls-use-mpegts 는 HLS 세그먼트를 단일 순차(sequential)
                #   MPEG-TS 스트림으로 처리하도록 yt-dlp 에 지시한다.
                #   이 모드에서는 전역 옵션으로 설정된 --concurrent-fragments 8 이
                #   내부적으로 무효화되어 병렬 다운로드가 동작하지 않는다.
                #   두 옵션은 상호 배타적이므로 --hls-use-mpegts 를 제거한다.
                # 1) --retries / --fragment-retries : Naver CDN 토큰 만료·
                #    속도 제한으로 인한 세그먼트 오류 자동 재시도
                # 2) --sleep-interval 0 : 세그먼트 요청 사이 불필요한 대기 제거
                if _is_chzzk:
                    cmd.extend([
                        "--retries", "10",
                        "--fragment-retries", "10",
                        "--sleep-interval", "0",
                        "--max-sleep-interval", "0",
                    ])

                # ── Soop 전용 저장 옵션 (Facebook 방식 준용) ─────────────────
                # Referer 헤더 추가: Soop CDN 인증 우회
                # HLS 세그먼트 재시도 옵션 적용 (치지직과 동일 이유)
                if _is_soop:
                    cmd.extend([
                        "--add-header", "Referer:https://www.sooplive.co.kr/",
                        "--retries", "10",
                        "--fragment-retries", "10",
                        "--sleep-interval", "0",
                        "--max-sleep-interval", "0",
                    ])
                    self.root.after(0, lambda: self._link_status("⏳ Soop 저장 중…"))
                    self._log_lines.append(
                        f"[{ts}] 💾 Soop 저장 옵션 적용 | 원본 URL: {url}")

                # [버그3 수정] Facebook: 비로그인(공개 콘텐츠) 우선 시도
                #   --cookies-from-browser 를 초기 커맨드에서 제거하고,
                #   인증 오류가 확인된 경우에만 2차 시도에서 쿠키를 사용한다.
                if _is_facebook:
                    cmd.extend(["--add-header",
                                "Referer:https://www.facebook.com/"])
                    self.root.after(0, lambda: self._link_status(
                        "⏳ Facebook 저장 중…"))

                cmd.append(url)

                # ── yt-dlp 실행 헬퍼 ─────────────────────────────────────────
                # stdout 스트리밍·진행률 업데이트·파일 경로 추적을 캡슐화.
                # Facebook 2차 재시도에서 코드 중복 없이 재사용한다.
                dest_file = None

                def _exec_ytdlp(ytdlp_cmd):
                    """yt-dlp 를 실행하고 진행 상황을 업데이트한다.
                    반환: (returncode: int, captured_lines: list[str])
                    """
                    nonlocal dest_file
                    _p = subprocess.Popen(
                        ytdlp_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        creationflags=0x08000000,  # CREATE_NO_WINDOW
                    )
                    self._link_save_proc = _p
                    _lines  = []
                    _pl_cur   = 0
                    _pl_total = 0
                    # ── \r / \n 모두 줄 구분자로 처리 ─────────────────────
                    # ffmpeg 는 진행률을 \r 로 출력하므로 readline() 이 블로킹되고
                    # 파이프 버퍼가 가득 차 데드락이 발생한다.
                    # 청크 단위로 읽어 \r·\n 모두 분리하는 방식으로 해결한다.
                    _rbuf = ""
                    while True:
                        _chunk = _p.stdout.read(512)
                        if not _chunk:
                            break
                        _rbuf += _chunk
                        while True:
                            _ni = _rbuf.find('\n')
                            _ri = _rbuf.find('\r')
                            if _ni == -1 and _ri == -1:
                                break
                            if _ni == -1:
                                _idx, _skip = _ri, 1
                            elif _ri == -1:
                                _idx, _skip = _ni, 1
                            else:
                                _idx  = min(_ni, _ri)
                                # \r\n 을 한 줄로 처리
                                _skip = 2 if (_rbuf[_idx] == '\r'
                                              and _idx + 1 < len(_rbuf)
                                              and _rbuf[_idx + 1] == '\n') else 1
                            _ln   = _rbuf[:_idx]
                            _rbuf = _rbuf[_idx + _skip:]

                            if not _ln.strip():
                                continue

                            if getattr(self, "_link_save_cancelled", False):
                                break

                            # ── 재생목록 카운터 파싱 ─────────────────────────────
                            _pm = re.search(
                                r'\[download\]\s+Downloading item\s+(\d+)\s+of\s+(\d+)',
                                _ln)
                            if _pm:
                                _pl_cur   = int(_pm.group(1))
                                _pl_total = int(_pm.group(2))
                            # ── 진행률 파싱 + 레이블 업데이트 ──────────────────
                            _m = re.search(r'\[download\]\s+([\d.]+)%', _ln)
                            if _m:
                                _pct  = float(_m.group(1))
                                _info = (f"({_pl_cur}/{_pl_total})"
                                         if _pl_total > 1 else "")
                                self.root.after(
                                    0, lambda p=_pct, i=_info:
                                        self._dl_progress_update(p, i))
                            # ── ffmpeg 병합 단계 감지 → 상태 메시지 표시 ────────
                            elif re.search(r'\[(?:Merger|ffmpeg)\]', _ln):
                                self.root.after(0, lambda: self._link_status(
                                    "⏳ ffmpeg 병합 중… (잠시 기다려 주세요)"))
                                self.root.after(
                                    0, lambda: self._dl_progress_update(99.0))
                            # ── 파일 경로 추적 ───────────────────────────────────
                            _mf = re.search(
                                r'\[(?:download|Merger|ffmpeg)\]\s+'
                                r'(?:Destination|Merging formats into):?\s+"?([^"\n]+\.\w+)"?',
                                _ln)
                            if _mf:
                                _tp = _mf.group(1).strip().strip('"')
                                self._link_save_tracked_files.add(_tp)
                                dest_file = _tp
                            _lines.append(_ln)
                    _p.wait()
                    self._link_save_proc = None
                    return _p.returncode, _lines

                # ── 1차 실행 ─────────────────────────────────────────────────
                # 재생목록·단일 영상 모두 _exec_ytdlp 로 처리한다.
                # yt-dlp 가 자체적으로 bestvideo+bestaudio 병합을 수행하므로
                # 별도 ffmpeg 호출 없이 안정적으로 동작한다.
                self._log_lines.append(
                    f"[{ts}] ▶ [1단계] yt-dlp 직접 다운로드 시도 | URL: {url}")
                _rc, _captured_lines = _exec_ytdlp(cmd)

                self._log_lines.append(
                    f"[{ts}] {'✅ [1단계] yt-dlp 성공' if _rc == 0 else f'⚠ [1단계] yt-dlp 실패 (rc={_rc}) → 2단계로'}")

                # ── 취소된 경우 → _link_save_cancel 이 파일 정리를 담당하므로 여기서 종료
                if getattr(self, "_link_save_cancelled", False):
                    return

                # [버그3 수정] Facebook 인증 오류 감지 → 쿠키로 2차 재시도
                #   1차 시도 실패 후 출력에 인증 관련 키워드가 있으면
                #   --cookies-from-browser 를 추가해 한 번 더 실행한다.
                if _is_facebook and _rc != 0:
                    _fb_auth_kws = (
                        "login", "cookie", "private", "sign in",
                        "not available", "requires authentication",
                        "could not extract", "unable to extract",
                        "need to log", "authentication", "restricted",
                        "unavailable",
                    )
                    _needs_retry = any(
                        kw in _ln.lower()
                        for _ln in _captured_lines
                        for kw in _fb_auth_kws
                    )
                    if _needs_retry:
                        for _fb_browser in ("chrome", "firefox", "edge", "chromium"):
                            _browser_hint = _fb_browser
                            break
                        # cmd 마지막 원소는 url 이므로 제거 후 쿠키 옵션 삽입
                        _cmd_retry = cmd[:-1] + [
                            "--cookies-from-browser", _browser_hint,
                            "--extractor-retries", "3",
                            url,
                        ]
                        self.root.after(0, lambda bh=_browser_hint: self._link_status(
                            f"⏳ Facebook 재시도 중 ({bh} 쿠키 사용)…"))
                        self._log_lines.append(
                            f"[{ts}] ⚠ Facebook 비로그인 실패 "
                            f"→ {_browser_hint} 쿠키로 재시도")
                        _rc, _ = _exec_ytdlp(_cmd_retry)

                        if getattr(self, "_link_save_cancelled", False):
                            return

                # ── Soop 인증 오류 감지 → 쿠키로 2차 재시도 (Facebook 방식 준용) ──
                elif _is_soop and _rc != 0:
                    _soop_auth_kws = (
                        "login", "cookie", "private", "sign in",
                        "not available", "requires authentication",
                        "could not extract", "unable to extract",
                        "need to log", "authentication", "restricted",
                        "unavailable",
                    )
                    _needs_retry = any(
                        kw in _ln.lower()
                        for _ln in _captured_lines
                        for kw in _soop_auth_kws
                    )
                    if _needs_retry:
                        for _soop_browser in ("chrome", "firefox", "edge", "chromium"):
                            _browser_hint = _soop_browser
                            break
                        _cmd_retry = cmd[:-1] + [
                            "--cookies-from-browser", _browser_hint,
                            "--extractor-retries", "3",
                            url,
                        ]
                        self.root.after(0, lambda bh=_browser_hint: self._link_status(
                            f"⏳ Soop 재시도 중 ({bh} 쿠키 사용)…"))
                        self._log_lines.append(
                            f"[{ts}] ⚠ Soop 비로그인 실패 "
                            f"→ {_browser_hint} 쿠키로 재시도 | 원본 URL: {url}")
                        _rc, _ = _exec_ytdlp(_cmd_retry)

                        if getattr(self, "_link_save_cancelled", False):
                            return

                # ── 치지직 인증 오류 감지 → 쿠키로 2차 재시도 (Facebook 방식 준용) ─
                elif _is_chzzk and _rc != 0:
                    _chzzk_auth_kws = (
                        "login", "cookie", "private", "sign in",
                        "not available", "requires authentication",
                        "could not extract", "unable to extract",
                        "need to log", "authentication", "restricted",
                        "unavailable",
                    )
                    _needs_retry = any(
                        kw in _ln.lower()
                        for _ln in _captured_lines
                        for kw in _chzzk_auth_kws
                    )
                    if _needs_retry:
                        for _chzzk_browser in ("chrome", "firefox", "edge", "chromium"):
                            _browser_hint = _chzzk_browser
                            break
                        _cmd_retry = cmd[:-1] + [
                            "--cookies-from-browser", _browser_hint,
                            "--extractor-retries", "3",
                            url,
                        ]
                        self.root.after(0, lambda bh=_browser_hint: self._link_status(
                            f"⏳ 치지직 재시도 중 ({bh} 쿠키 사용)…"))
                        self._log_lines.append(
                            f"[{ts}] ⚠ 치지직 비로그인 실패 "
                            f"→ {_browser_hint} 쿠키로 재시도 | 원본 URL: {url}")
                        _rc, _ = _exec_ytdlp(_cmd_retry)

                        if getattr(self, "_link_save_cancelled", False):
                            return

                # ── N_m3u8DL-RE 실행 헬퍼 ───────────────────────────────────
                def _exec_nm3u8dl_re(target_url: str,
                                    extra_headers: dict | None = None,
                                    save_name: str | None = None) -> int:
                    """N_m3u8DL-RE 로 target_url 을 다운로드한다.
                    extra_headers : {"referer": str, "cookies": str}
                    save_name     : 저장 파일명 (확장자 제외). None 이면 자동 결정.
                    """
                    if not _nm3u8dl_re_exe:
                        self._log_lines.append(
                            f"[{_time.strftime('%H:%M:%S')}] ⚠ N_m3u8DL-RE 없음 — 폴백 불가")
                        return 1

                    # 다운로드 전 dl_dir 의 .ts 파일 목록을 스냅샷
                    import glob as _glob
                    _ts_before = set(
                        _glob.glob(os.path.join(dl_dir, "**", "*.ts"), recursive=True))

                    _nc = [
                        _nm3u8dl_re_exe, target_url,
                        "--save-dir", dl_dir,
                        "--auto-select",
                        "--no-log",
                    ]
                    if save_name:
                        _nc += ["--save-name", save_name]
                    if extra_headers:
                        if extra_headers.get("referer"):
                            _nc += ["--header", f"Referer:{extra_headers['referer']}"]
                        if extra_headers.get("cookies"):
                            _nc += ["--header", f"Cookie:{extra_headers['cookies']}"]
                    _np = subprocess.Popen(
                        _nc,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        creationflags=0x08000000 if os.name == "nt" else 0)
                    self._link_save_proc = _np
                    for _nln in _np.stdout:
                        if getattr(self, "_link_save_cancelled", False):
                            _np.kill()
                            break
                        _nm = re.search(r'(\d+(?:\.\d+)?)%', _nln)
                        if _nm:
                            _npct = float(_nm.group(1))
                            self.root.after(
                                0, lambda p=_npct: self._dl_progress_update(p))
                    _np.wait()
                    self._link_save_proc = None
                    _rc_nm = _np.returncode

                    if _rc_nm != 0 or getattr(self, "_link_save_cancelled", False):
                        return _rc_nm

                    # ── 다운로드 후 새로 생긴 .ts 파일 → .mp4 변환 ──────────
                    # N_m3u8DL-RE 가 .ts 로 저장했을 경우 ffmpeg 로 컨테이너 변환
                    _ts_after = set(
                        _glob.glob(os.path.join(dl_dir, "**", "*.ts"), recursive=True))
                    _new_ts = _ts_after - _ts_before

                    if not _new_ts:
                        # .ts 파일이 새로 생기지 않았으면 이미 mp4 등으로 저장된 것
                        return _rc_nm

                    self.root.after(0, lambda: self._link_status(
                        "⏳ N_m3u8DL-RE 완료 → .ts → .mp4 변환 중…"))
                    _all_converted = True
                    for _ts_path in sorted(_new_ts):
                        if getattr(self, "_link_save_cancelled", False):
                            break
                        # save_name 이 있으면 그 이름으로, 없으면 원본 .ts 이름 유지
                        if save_name:
                            _mp4_path = os.path.join(dl_dir, f"{save_name}.mp4")
                            # 동명 파일이 이미 있으면 타임스탬프 추가
                            if os.path.exists(_mp4_path):
                                _sfx = _time.strftime("%Y%m%d_%H%M%S")
                                _mp4_path = os.path.join(
                                    dl_dir, f"{save_name}_{_sfx}.mp4")
                        else:
                            _mp4_path = os.path.splitext(_ts_path)[0] + ".mp4"
                        self._log_lines.append(
                            f"[{_time.strftime('%H:%M:%S')}] 🔀 .ts → .mp4: "
                            f"{os.path.basename(_ts_path)}")
                        try:
                            _conv = subprocess.run(
                                [
                                    ffmpeg,
                                    "-y",                    # 덮어쓰기 허용
                                    "-i", _ts_path,
                                    "-c", "copy",            # 재인코딩 없이 컨테이너만 변환
                                    "-movflags", "+faststart",  # 스트리밍 최적화
                                    _mp4_path,
                                ],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace",  # cp949 오류 방지
                                timeout=600,
                                creationflags=0x08000000 if os.name == "nt" else 0)
                            if _conv.returncode == 0:
                                try:
                                    os.remove(_ts_path)  # 원본 .ts 삭제
                                except Exception:
                                    pass
                                self._log_lines.append(
                                    f"[{_time.strftime('%H:%M:%S')}] ✅ 변환 완료: "
                                    f"{os.path.basename(_mp4_path)}")
                            else:
                                _all_converted = False
                                self._log_lines.append(
                                    f"[{_time.strftime('%H:%M:%S')}] ❌ 변환 실패: "
                                    f"{os.path.basename(_ts_path)} — "
                                    f"{_conv.stderr.strip()[:100]}")
                        except subprocess.TimeoutExpired:
                            _all_converted = False
                            self._log_lines.append(
                                f"[{_time.strftime('%H:%M:%S')}] ❌ 변환 타임아웃: "
                                f"{os.path.basename(_ts_path)}")
                        except Exception as _ce:
                            _all_converted = False
                            self._log_lines.append(
                                f"[{_time.strftime('%H:%M:%S')}] ❌ 변환 오류: {_ce}")

                    return 0 if _all_converted else 1

                # ══ 폴백 체인 ════════════════════════════════════════════════
                # 단계 1(yt-dlp 직접) 실패 시 단계 2(CDP)로 바로 진행한다.

                _final_rc   = _rc        # 최종 결과 추적
                _final_step = "yt-dlp"   # 마지막 시도 단계 이름

                # ── 단계 2: Playwright CDP → m3u8 감지 → yt-dlp → N_m3u8DL-RE ─
                if _final_rc != 0 and not getattr(self, "_link_save_cancelled", False):
                    self.root.after(0, lambda: self._link_status(
                        "⏳ 헤드리스 브라우저로 m3u8 감지 중… (최초 실행 시 설치 포함)"))
                    self._log_lines.append(
                        f"[{ts}] ⚠ [2단계] Playwright CDP 시도 | URL: {url}")
                    try:
                        self._ensure_playwright()
                        _pw_result = self._extract_m3u8_playwright(url)
                    except Exception as _pwe:
                        _pw_result = None
                        self._log_lines.append(
                            f"[{ts}] ❌ [2단계] Playwright 준비 실패: {_pwe}")

                    if _pw_result:
                        _pw_m3u8  = _pw_result["url"]
                        _pw_hdrs  = {
                            "referer": _pw_result["referer"],
                            "cookies": _pw_result["cookies"],
                        }
                        _rh = _pw_result.get("req_headers", {})
                        if _rh:
                            _rh_summary = ", ".join(
                                f"{k}={v[:20]}" for k, v in _rh.items()
                                if k.lower() in ("referer", "authorization",
                                                 "cookie", "origin"))
                            if _rh_summary:
                                self._log_lines.append(
                                    f"[{ts}] 📋 [2단계] 캡처된 요청 헤더: {_rh_summary}")

                        # ── 페이지 제목 → 안전한 파일명으로 변환 ──────────
                        # 앱의 onReceivedTitle 과 동일한 방식으로 추출한 제목을
                        # Windows/Linux 파일명으로 쓸 수 있게 정제한다.
                        _raw_title = _pw_result.get("title", "").strip()

                        def _safe_filename(t: str, max_len: int = 120) -> str:
                            """제목 문자열을 파일명으로 쓸 수 있게 정제한다.
                            - Windows 금지 문자 제거
                            - 앞뒤 공백·점 제거
                            - 최대 길이 제한
                            - 비어있으면 타임스탬프 반환
                            """
                            if not t:
                                return f"master_{_time.strftime('%Y%m%d_%H%M%S')}"
                            # Windows 파일명 금지 문자 제거
                            t = re.sub(r'[\\/:*?"<>|]', '', t)
                            # 연속 공백 → 단일 공백
                            t = re.sub(r'\s+', ' ', t).strip(' .')
                            return t[:max_len] if t else \
                                f"master_{_time.strftime('%Y%m%d_%H%M%S')}"

                        _safe_title = _safe_filename(_raw_title)
                        self._log_lines.append(
                            f"[{ts}] 🔗 [2단계] m3u8 감지: {_pw_m3u8[:80]} "
                            f"| 제목: {_safe_title[:40]!r}")

                        # ── 2-a: N_m3u8DL-RE 다운로드 ────────────────────
                        self.root.after(0, lambda: self._link_status(
                            "⏳ m3u8 감지 완료 → N_m3u8DL-RE로 다운로드 중…"))
                        self._log_lines.append(
                            f"[{ts}] ▶ [2-a] N_m3u8DL-RE 시도")
                        _final_rc   = _exec_nm3u8dl_re(
                            _pw_m3u8, _pw_hdrs, save_name=_safe_title)
                        _final_step = "CDP+N_m3u8DL-RE"

                        # ── 2-b: N_m3u8DL-RE 실패 시 yt-dlp 폴백 ─────────
                        if (_final_rc != 0
                                and not getattr(self, "_link_save_cancelled", False)):
                            self.root.after(0, lambda: self._link_status(
                                "⏳ N_m3u8DL-RE 실패 → yt-dlp로 재시도 중…"))
                            self._log_lines.append(
                                f"[{ts}] ⚠ [2-b] N_m3u8DL-RE 실패 → yt-dlp 폴백")
                            # 제목을 알고 있으면 고정 파일명 사용,
                            # 없으면 yt-dlp 가 스스로 제목을 추출하도록 %(title)s 템플릿
                            _yt_out = os.path.join(
                                dl_dir,
                                f"{_safe_title}.%(ext)s" if _raw_title
                                else "%(title)s.%(ext)s")
                            _pw_cmd = [
                                ytdlp,
                                "--no-warnings",
                                "-f", "bestvideo+bestaudio/best",
                                "--merge-output-format", "mp4",
                                "--ffmpeg-location", os.path.dirname(ffmpeg),
                                "--postprocessor-args",
                                "ffmpeg:-loglevel error -c:a aac -b:a 192k",
                                "-N", "4",
                                "--concurrent-fragments", "4",
                                "--retries", "10",
                                "--fragment-retries", "10",
                                "--sleep-interval", "0",
                                "--max-sleep-interval", "0",
                                "--newline",
                                "-o", _yt_out,
                            ]
                            if _pw_hdrs.get("referer"):
                                _pw_cmd += ["--add-header",
                                            f"Referer:{_pw_hdrs['referer']}"]
                            if _pw_hdrs.get("cookies"):
                                _pw_cmd += ["--add-header",
                                            f"Cookie:{_pw_hdrs['cookies']}"]
                            _pw_cmd.append(_pw_m3u8)
                            _final_rc, _ = _exec_ytdlp(_pw_cmd)
                            _final_step  = "CDP+yt-dlp"

                        # ── 2-c: yt-dlp 실패 시 ffmpeg 폴백 ──────────────
                        if (_final_rc != 0
                                and not getattr(self, "_link_save_cancelled", False)):
                            self.root.after(0, lambda: self._link_status(
                                "⏳ yt-dlp 실패 → ffmpeg로 재시도 중…"))
                            self._log_lines.append(
                                f"[{ts}] ⚠ [2-c] yt-dlp 실패 → ffmpeg 폴백")

                            # 제목을 파일명으로 사용, 이미 존재하면 타임스탬프 추가
                            _out_mp4 = os.path.join(dl_dir, f"{_safe_title}.mp4")
                            if os.path.exists(_out_mp4):
                                _suffix  = _time.strftime("%Y%m%d_%H%M%S")
                                _out_mp4 = os.path.join(
                                    dl_dir,
                                    f"{_safe_title}_{_suffix}.mp4")

                            _hdr_str = ""
                            if _pw_hdrs.get("referer"):
                                _hdr_str += f"Referer: {_pw_hdrs['referer']}\r\n"
                            if _pw_hdrs.get("cookies"):
                                _hdr_str += f"Cookie: {_pw_hdrs['cookies']}\r\n"

                            _ff_cmd = [ffmpeg, "-y"]
                            if _hdr_str:
                                _ff_cmd += ["-headers", _hdr_str]
                            _ff_cmd += [
                                "-reconnect", "1",
                                "-reconnect_streamed", "1",
                                "-reconnect_delay_max", "5",
                                "-i", _pw_m3u8,
                                "-c:v", "copy",
                                "-c:a", "aac",
                                "-b:a", "192k",
                                "-movflags", "+faststart",
                                "-progress", "pipe:1",
                                "-nostats",
                                _out_mp4,
                            ]

                            try:
                                _ff_proc = subprocess.Popen(
                                    _ff_cmd,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    text=True,
                                    encoding="utf-8", errors="replace",
                                    creationflags=0x08000000 if os.name == "nt" else 0)
                                self._link_save_proc = _ff_proc

                                _ff_err_lines = []
                                def _drain_stderr():
                                    for _el in _ff_proc.stderr:
                                        _ff_err_lines.append(_el.rstrip())
                                _drain_t = threading.Thread(
                                    target=_drain_stderr, daemon=True,
                                    name="ffmpeg-stderr-drain")
                                _drain_t.start()

                                for _fln in _ff_proc.stdout:
                                    if getattr(self, "_link_save_cancelled", False):
                                        _ff_proc.kill()
                                        break
                                    _fln = _fln.strip()
                                    if _fln.startswith("total_size="):
                                        try:
                                            _mb = int(_fln.split("=", 1)[1]) / (1024*1024)
                                            self.root.after(0, lambda m=_mb:
                                                self._link_status(
                                                    f"⏳ ffmpeg 다운로드 중… {m:.1f} MB"))
                                            self.root.after(0, lambda:
                                                self._dl_progress_update(50.0))
                                        except ValueError:
                                            pass
                                    elif _fln == "progress=end":
                                        self.root.after(0, lambda:
                                            self._dl_progress_update(100.0))

                                _ff_proc.wait()
                                _drain_t.join(timeout=3)
                                self._link_save_proc = None
                                _final_rc   = _ff_proc.returncode
                                _final_step = "CDP+ffmpeg"

                                if _final_rc == 0:
                                    self.root.after(0, lambda:
                                        self._dl_progress_update(100.0))
                                    self._log_lines.append(
                                        f"[{ts}] ✅ [2-c] ffmpeg 완료: "
                                        f"{os.path.basename(_out_mp4)}")
                                else:
                                    for _f in (_out_mp4, _out_mp4 + ".part"):
                                        try:
                                            if os.path.isfile(_f):
                                                os.remove(_f)
                                        except Exception:
                                            pass
                                    _err_tail = " | ".join(
                                        _ff_err_lines[-3:]) if _ff_err_lines else ""
                                    self._log_lines.append(
                                        f"[{ts}] ❌ [2-c] ffmpeg 실패 "
                                        f"(rc={_final_rc}): {_err_tail[:150]}")
                            except Exception as _ffe:
                                self._link_save_proc = None
                                _final_rc   = 1
                                _final_step = "CDP+ffmpeg"
                                self._log_lines.append(
                                    f"[{ts}] ❌ [2-c] ffmpeg 예외: {_ffe}")
                    else:
                        self._log_lines.append(
                            f"[{ts}] ⚠ [2단계] m3u8 감지 실패 — 모든 방법 소진")

                # ── 최종 실패 시 임시 파일 정리 ──────────────────────────────
                if (_final_rc != 0
                        and not getattr(self, "_link_save_cancelled", False)):
                    self._log_lines.append(
                        f"[{ts}] 🧹 임시 파일 정리 시작…")
                    import glob as _glob_cleanup
                    _tmp_patterns = [
                        "*.part", "*.part-Frag*", "*.ytdl",
                        "*.mp4.part", "*.ts",
                    ]
                    _deleted_tmp = []
                    for _pat in _tmp_patterns:
                        for _f in _glob_cleanup.glob(
                                os.path.join(dl_dir, "**", _pat), recursive=True):
                            try:
                                if os.path.isfile(_f):
                                    os.remove(_f)
                                    _deleted_tmp.append(os.path.basename(_f))
                            except Exception:
                                pass
                    if _deleted_tmp:
                        self._log_lines.append(
                            f"[{ts}] 🧹 임시 파일 {len(_deleted_tmp)}개 삭제: "
                            f"{_deleted_tmp[:5]}"
                            + (" 외…" if len(_deleted_tmp) > 5 else ""))
                # ── 최종 결과 처리 ───────────────────────────────────────────
                if getattr(self, "_link_save_cancelled", False):
                    return

                self._link_save_dest_glob = None
                if _final_rc == 0:
                    self.root.after(0, lambda: self._dl_progress_update(100.0))
                    self.root.after(0, lambda: self._link_status(
                        f"✅ 저장 완료 → {dl_dir}"))
                    self.root.after(2500, self._dl_progress_hide)
                    self._log_lines.append(
                        f"[{ts}] ✅ 저장 완료 ({_final_step}) | 원본 URL: {url}")

                    # ── 자막 다운로드 (영상+자막 선택 시) ───────────────────
                    if _save_with_sub:
                        _cdp_subs = (
                            _pw_result.get("subtitles", [])
                            if isinstance(locals().get("_pw_result"), dict)
                            else []
                        )
                        # 실제 저장된 영상 파일명 (확장자 제외) 을 자막 파일명으로 사용
                        _video_stem = (
                            os.path.splitext(os.path.basename(dest_file))[0]
                            if dest_file else None
                        )
                        def _dl_subtitle(cdp_subs=_cdp_subs,
                                         video_stem=_video_stem):
                            import urllib.request as _ureq
                            try:
                                self.root.after(0, lambda: self._link_status(
                                    "⏳ 자막 다운로드 중…"))
                                _sub_ok = False
                                for _si, _s in enumerate(cdp_subs):
                                    _su    = _s.get("url", "")
                                    _shdrs = _s.get("req_headers", {})
                                    if not _su:
                                        continue
                                    _ext = re.search(
                                        r'\.(vtt|srt|ttml|dfxp)', _su, re.IGNORECASE)
                                    _ext  = _ext.group(1).lower() if _ext else "vtt"
                                    # 영상 파일명 기준, 없으면 번호로 대체
                                    _sname = video_stem if video_stem \
                                             else f"subtitle_{_si+1}"
                                    _sdest = os.path.join(dl_dir, f"{_sname}.{_ext}")
                                    try:
                                        _req = _ureq.Request(_su, headers={
                                            "User-Agent": _UA,
                                            **{k: v for k, v in _shdrs.items()
                                               if k.lower() in (
                                                   "referer", "origin",
                                                   "cookie", "authorization")},
                                        })
                                        with _ureq.urlopen(_req, timeout=30) as _sr:
                                            _sdata = _sr.read()
                                        with open(_sdest, "wb") as _sf:
                                            _sf.write(_sdata)
                                        self._log_lines.append(
                                            f"[{ts}] ✅ 자막 저장: "
                                            f"{os.path.basename(_sdest)}")
                                        _sub_ok = True
                                    except Exception as _sde:
                                        self._log_lines.append(
                                            f"[{ts}] ⚠ 자막 직접 다운로드 실패: {_sde}")
                                if not _sub_ok:
                                    # yt-dlp fallback — -o 에 영상 파일명 stem 사용
                                    _sub_tmpl = (
                                        os.path.join(dl_dir, f"{video_stem}.%(ext)s")
                                        if video_stem
                                        else os.path.join(dl_dir, "%(title)s.%(ext)s")
                                    )
                                    _sub_cmd = [
                                        ytdlp, "--no-warnings",
                                        "--write-sub", "--write-auto-sub",
                                        "--sub-langs", "ko,ko-KR,en,en-US",
                                        "--sub-format", "vtt/srt/best",
                                        "--skip-download",
                                        "-o", _sub_tmpl,
                                        url,
                                    ]
                                    _sp = subprocess.run(
                                        _sub_cmd,
                                        capture_output=True, text=True,
                                        encoding="utf-8", errors="replace",
                                        timeout=60,
                                        creationflags=0x08000000 if os.name == "nt" else 0)
                                    _sub_ok = _sp.returncode == 0
                                if _sub_ok:
                                    self.root.after(0, lambda: self._link_status(
                                        f"✅ 저장+자막 완료 → {dl_dir}"))
                                else:
                                    self.root.after(0, lambda: self._link_status(
                                        f"✅ 저장 완료 (자막 없음) → {dl_dir}"))
                            except Exception as _se:
                                self._log_lines.append(
                                    f"[{ts}] ⚠ 자막 다운로드 예외: {_se}")
                        threading.Thread(target=_dl_subtitle, daemon=True).start()
                else:
                    if _is_facebook:
                        self.root.after(0, lambda: self._link_status(
                            "❌ Facebook 저장 실패. "
                            "공개 콘텐츠인지 확인하거나, "
                            "브라우저에서 Facebook에 로그인 후 다시 시도하세요.",
                            warn=True))
                    elif _is_soop:
                        self.root.after(0, lambda: self._link_status(
                            "❌ Soop 저장 실패. "
                            "공개 영상인지 확인하거나, "
                            "브라우저에서 Soop에 로그인 후 다시 시도하세요.",
                            warn=True))
                    elif _is_chzzk:
                        self.root.after(0, lambda: self._link_status(
                            "❌ 치지직 저장 실패. "
                            "공개 영상인지 확인하거나, "
                            "브라우저에서 치지직에 로그인 후 다시 시도하세요.",
                            warn=True))
                    else:
                        self.root.after(0, lambda: self._link_status(
                            "❌ 저장 실패. URL이 올바른지 확인하세요.", warn=True))
                    self.root.after(0, self._dl_progress_hide)
                    self._log_lines.append(
                        f"[{ts}] ❌ 저장 실패 — 모든 방법 소진 "
                        f"(마지막 시도: {_final_step}) | 원본 URL: {url}")

            except Exception as e:
                _err = str(e)
                self._link_save_proc = None
                self._link_save_dest_glob = None
                self.root.after(0, lambda: self._link_status(f"❌ 오류: {_err}", warn=True))
                self.root.after(0, self._dl_progress_hide)
                self._log_lines.append(f"[{ts}] ❌ yt-dlp 예외: {_err}")
            finally:
                self._link_save_proc = None
                # 취소된 경우 UI 복구는 _link_save_cancel의 _cleanup 스레드가 담당
                # (여기서 중복 처리하면 "⏹ 저장 중단" 메시지가 덮어쓰여짐)
                if not getattr(self, "_link_save_cancelled", False):
                    self.root.after(0, self._dl_stop_btn_hide)
                    self.root.after(0, lambda: self._link_save_btn.config(state="normal"))

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

    def _dl_progress_update(self, pct: float, playlist_info: str = ""):
        """다운로드 진행률(0–100) 업데이트.
        playlist_info: 재생목록일 때 '(현재/전체)' 문자열, 단일 영상이면 빈 문자열.
        """
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
                txt = f"{pct:.1f}%"
                if playlist_info:
                    txt += f"  {playlist_info}"
                lbl.config(text=txt)
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

            # 이어보기 버튼 — 색상은 URL 확정 후 업데이트 단계에서 지정
            resume_btn = tk.Button(
                row, text="▶ 이어보기",
                font=("Consolas", max(7, round(8 * r)), "bold"),
                bg=btn_bg, fg=_PLATFORM_COLORS["default"],
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

            if title:
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
                    first, rest = display_title.split(" - ", 1)
                    display_text = _smart_wrap(first) + "\n- " + _smart_wrap(rest)
                else:
                    display_text = _smart_wrap(display_title)
            else:
                display_text = "(제목 불러오는 중…)"

            cached["url_lbl"].config(text=display_text)
            cached["ts_lbl"].config(text=ts if ts else "")
            cached["resume_btn"].config(
                fg=_get_platform_color(url),
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

    def _fetch_soop_play_info(self, url: str):
        """Soop(AfreecaTV) URL에서 실제 영상 제목과 재생용 스트림 URL을 추출한다.

        yt-dlp -j (dump-json)으로 메타데이터를 읽고
        스트림 URL(m3u8 포함)과 실제 제목을 반환한다.

        로그에 기록하는 항목:
          - 원본 URL
          - 추출된 m3u8 URL 사용 여부
          - fallback 여부

        Returns:
            (stream_url, real_title)
            추출 실패 시 (original_url, "") 반환 (fallback)
        """
        if not hasattr(self, "_log_lines"):
            self._log_lines = collections.deque(maxlen=100)

        ts = _time.strftime("%H:%M:%S")
        self._log_lines.append(
            f"[{ts}] 🔍 Soop 영상 정보 추출 시도 | 원본 URL: {url}")

        try:
            ytdlp = self._ensure_ytdlp()
        except Exception as e:
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ⚠ yt-dlp 없음 → Soop fallback: {e}")
            return url, ""

        def _parse_info(raw_json: str):
            """JSON 파싱 후 (stream_url, title) 반환."""
            info = json.loads(raw_json.strip().splitlines()[0])
            title = (
                info.get("fulltitle") or
                info.get("title") or
                info.get("webpage_url_basename") or ""
            )
            stream_url = _pick_best_stream_url(info) or url
            return stream_url, title

        base_cmd = [
            ytdlp,
            "-j",                          # --dump-json: 단일 JSON 출력
            "--no-playlist",
            "-f", "bestvideo+bestaudio/best",
            "--extractor-retries", "2",
            "--socket-timeout", "15",
        ]

        # 브라우저 쿠키 순서대로 시도 (로그인 콘텐츠 대응)
        for browser in ("chrome", "firefox", "edge", "chromium"):
            try:
                cmd = base_cmd + ["--cookies-from-browser", browser, url]
                proc = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=35,
                    creationflags=0x08000000,   # CREATE_NO_WINDOW
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    stream_url, title = _parse_info(proc.stdout)
                    _is_m3u8 = ".m3u8" in stream_url
                    self._log_lines.append(
                        f"[{_time.strftime('%H:%M:%S')}] ✅ Soop 정보 추출 완료 "
                        f"({browser}) | 제목={title!r} | "
                        f"m3u8={'사용' if _is_m3u8 else '미사용'} | fallback=False")
                    return stream_url, title
                self._log_lines.append(
                    f"[{_time.strftime('%H:%M:%S')}] ⚠ Soop 정보 추출 실패 "
                    f"({browser}), 다음 브라우저 시도…")
            except subprocess.TimeoutExpired:
                self._log_lines.append(
                    f"[{_time.strftime('%H:%M:%S')}] ⚠ Soop 추출 타임아웃 ({browser})")
            except (json.JSONDecodeError, Exception) as e:
                self._log_lines.append(
                    f"[{_time.strftime('%H:%M:%S')}] ⚠ Soop 추출 예외 ({browser}): {e}")

        # 쿠키 없이 재시도 (공개 영상)
        try:
            proc2 = subprocess.run(
                base_cmd + [url],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=25,
                creationflags=0x08000000,
            )
            if proc2.returncode == 0 and proc2.stdout.strip():
                stream_url, title = _parse_info(proc2.stdout)
                _is_m3u8 = ".m3u8" in stream_url
                self._log_lines.append(
                    f"[{_time.strftime('%H:%M:%S')}] ✅ Soop 정보 추출 완료 "
                    f"(쿠키 없음) | 제목={title!r} | "
                    f"m3u8={'사용' if _is_m3u8 else '미사용'} | fallback=False")
                return stream_url, title
        except Exception as e:
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ⚠ Soop 쿠키 없음 추출 실패: {e}")

        # 모두 실패 → fallback: 원본 URL 반환 (PotPlayer 직접 처리 시도)
        self._log_lines.append(
            f"[{_time.strftime('%H:%M:%S')}] ⚠ Soop 정보 추출 전체 실패 "
            f"→ 원본 URL fallback 사용 | 원본 URL: {url}")
        return url, ""

    def _send_url_to_potplayer(self, hwnd: int, url: str):
        """PotPlayer 창에 URL을 클립보드 Ctrl+V로 전달한다.

        PotPlayer는 클립보드에 복사된 URL을 Ctrl+V로 받으면 해당 링크의
        영상을 직접 재생한다 (모든 URL 공통 적용).
        SetForegroundWindow + keybd_event 방식으로 실제 키 입력을 전달한다.
        """
        import ctypes

        # ① Win32 API로 클립보드 설정 (워커 스레드 세이프)
        ok = _win32_set_clipboard(url)
        if not ok:
            self.root.after(0, lambda: self._clipboard_set(url))

        # ② PotPlayer 포커스 후 Ctrl+V 전달
        _time.sleep(0.3)
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

                # ── Soop : yt-dlp -j 추출 ─────────────────────────────────
                # ── 그 외: 원본 URL 그대로 PotPlayer에 전달 ────────────────
                is_soop = bool(_SOOP_RE.search(url))

                self._log_lines.append(
                    f"[{ts}] 🔗 이어보기 재생 시작 | 원본 URL: {url} | "
                    f"platform={'soop' if is_soop else 'general'}")

                if is_soop:
                    # Soop: CDP(실제 Chrome)로 m3u8 URL + 제목 추출
                    self._link_status("⏳ Soop m3u8 감지 중 (CDP)…")
                    try:
                        self._ensure_playwright()
                        _cdp_result = self._extract_m3u8_playwright(url)
                    except Exception as _cdp_e:
                        _cdp_result = None
                        self._log_lines.append(
                            f"[{ts}] ⚠ Soop CDP 준비 실패: {_cdp_e}")
                    if _cdp_result and _cdp_result.get("url"):
                        play_url   = _cdp_result["url"]
                        real_title = _cdp_result.get("title", "")
                        self._log_lines.append(
                            f"[{ts}] ✅ Soop CDP m3u8 감지: {play_url[:80]}")
                    else:
                        play_url   = url
                        real_title = self._fetch_page_title(url)
                        self._log_lines.append(
                            f"[{ts}] ⚠ Soop CDP 실패 → 원본 URL fallback")
                else:
                    # 유튜브·치지직·페이스북·일반 URL 등: 원본 URL 그대로 PotPlayer에 전달
                    play_url   = url
                    real_title = self._fetch_page_title(url)
                    self._log_lines.append(
                        f"[{ts}] 🔗 일반 URL 직접 전달 (m3u8 추출 없음, fallback 없음)")

                # PotPlayer에 URL 전달
                self._send_url_to_potplayer(hwnd, play_url)

                self.root.after(0, lambda: self._set_link_play_mode(True))
                # 이어보기 기록은 원본 URL + 실제 제목으로 저장
                self.root.after(0, lambda: self._save_live_history_entry(url, title=real_title))
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
                bg=btn_bg, fg=self.ACCENT,
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
