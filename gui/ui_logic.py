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
                    # Soop: yt-dlp로 스트림 URL 추출
                    self._link_status("⏳ Soop 영상 정보 추출 중…")
                    play_url, real_title = self._fetch_soop_play_info(url)
                    # _fetch_soop_play_info 실패 시 play_url == url(원본) → fallback 로그는 내부 기록
                else:
                    # 유튜브·치지직·페이스북·일반 URL 등: 원본 URL 그대로 PotPlayer에 전달
                    play_url   = url
                    real_title = ""
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

    def _bg_ensure_potplayer_ytdlp(self, pot_dir: str):
        """PotPlayer Module 폴더에 yt-dlp.exe 가 없으면 GitHub 최신 버전에서 다운로드.

        C:\\Program Files 는 일반 권한으로 쓸 수 없으므로:
          1) %TEMP% 에 먼저 다운로드
          2) 직접 복사 시도 → PermissionError 면 UAC(ShellExecuteW runas) 로 복사
        """
        import tempfile, urllib.request, shutil
        module_dir = os.path.join(pot_dir, "Module")
        dest       = os.path.join(module_dir, "yt-dlp.exe")
        tmp_path   = os.path.join(tempfile.gettempdir(), "yt-dlp_potplayer.exe")
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
                # ② 권한 없음 → UAC로 PowerShell 복사 (단발성 팝업)
                ps = f"Copy-Item -Path '{tmp_path}' -Destination '{dest}' -Force"
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
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    def _bg_ensure_potplayer_extension(self, pot_dir: str):
        """Extension\\Media\\UrlList 에 'MediaPlayParse - yt-dlp.as' 가 없으면
        서버 업데이트 시트 B3 URL 의 zip 을 내려받아 압축 해제 후 zip 삭제.

        C:\\Program Files 쓰기 권한 없을 경우:
          1) %TEMP% 에 다운로드 + 압축 해제
          2) 직접 복사 시도 → PermissionError 면 UAC(ShellExecuteW runas) 로 복사
        """
        import tempfile, urllib.request, zipfile, shutil
        ext_dir     = os.path.join(pot_dir, "Extension", "Media", "UrlList")
        target_file = os.path.join(ext_dir, "MediaPlayParse - yt-dlp.as")
        tmp_zip     = os.path.join(tempfile.gettempdir(), "_as_potplayer.zip")
        tmp_ext_dir = os.path.join(tempfile.gettempdir(), "_as_potplayer_ext")
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
            # %TEMP% 에 다운로드 + 압축 해제
            urllib.request.urlretrieve(ext_url, tmp_zip)
            if os.path.exists(tmp_ext_dir):
                shutil.rmtree(tmp_ext_dir, ignore_errors=True)
            os.makedirs(tmp_ext_dir, exist_ok=True)
            with zipfile.ZipFile(tmp_zip, "r") as zf:
                zf.extractall(tmp_ext_dir)
            try:
                # ① 직접 복사 (권한 있을 때)
                os.makedirs(ext_dir, exist_ok=True)
                for f in os.listdir(tmp_ext_dir):
                    shutil.copy2(os.path.join(tmp_ext_dir, f),
                                 os.path.join(ext_dir, f))
                self._log_lines.append(
                    f"[{_time.strftime('%H:%M:%S')}] ✅ PotPlayer Extension/MediaPlayParse - yt-dlp.as 설치 완료")
            except PermissionError:
                # ② 권한 없음 → UAC로 PowerShell 복사
                ps = (f"New-Item -ItemType Directory -Force -Path '{ext_dir}' | Out-Null; "
                      f"Copy-Item -Path '{tmp_ext_dir}\\*' -Destination '{ext_dir}' -Force")
                ok = self._runas_powershell(ps)
                _time.sleep(4)
                result = os.path.isfile(target_file)
                self._log_lines.append(
                    f"[{_time.strftime('%H:%M:%S')}] "
                    + ("✅ PotPlayer Extension/MediaPlayParse - yt-dlp.as 설치 완료 (관리자)" if result
                       else "⚠ PotPlayer Extension/yt-dlp.as 설치 실패 (UAC 거부)"))
        except Exception as e:
            self._log_lines.append(
                f"[{_time.strftime('%H:%M:%S')}] ⚠ PotPlayer Extension/yt-dlp.as 설치 실패: {e}")
        finally:
            try:
                if os.path.exists(tmp_zip):
                    os.remove(tmp_zip)
            except Exception:
                pass
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

    def _dl_stop_btn_show(self):
        """저장 중지 버튼 표시."""
        btn = getattr(self, "_link_stop_btn", None)
        if btn:
            try:
                r = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
                btn.pack(side="left", padx=(round(4*r), 0))
            except Exception:
                pass

    def _dl_stop_btn_hide(self):
        """저장 중지 버튼 숨기기."""
        btn = getattr(self, "_link_stop_btn", None)
        if btn:
            try:
                btn.pack_forget()
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

        def _run():
            ts = _time.strftime("%H:%M:%S")
            # 취소 플래그 초기화 + 파일 추적 셋 초기화
            self._link_save_cancelled     = False
            self._link_save_tracked_files = set()
            self._link_save_dl_dir        = dl_dir
            try:
                # ── 1) yt-dlp 확보 ────────────────────────────────────────────
                try:
                    ytdlp = self._ensure_ytdlp()
                except Exception as e:
                    _err = str(e)
                    self.root.after(0, lambda: self._link_status(
                        f"❌ yt-dlp 설치 실패: {_err}", warn=True))
                    self.root.after(0, self._dl_progress_hide)
                    self._log_lines.append(f"[{ts}] ❌ yt-dlp 설치 실패: {_err}")
                    return

                # ── 2) ffmpeg 확보 ────────────────────────────────────────────
                try:
                    ffmpeg = self._ensure_ffmpeg()
                except Exception as e:
                    _err = str(e)
                    self.root.after(0, lambda: self._link_status(
                        f"❌ ffmpeg 설치 실패: {_err}", warn=True))
                    self.root.after(0, self._dl_progress_hide)
                    self._log_lines.append(f"[{ts}] ❌ ffmpeg 설치 실패: {_err}")
                    return

                # ── 3) 영상 다운로드 (bestvideo+bestaudio → mp4 병합) ─────────
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
                    # AAC 재인코딩 유지
                    "--postprocessor-args", "ffmpeg:-c:a aac -b:a 192k",
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
                    _lines = []
                    _pl_cur   = 0   # 현재 다운로드 중인 재생목록 항목 번호
                    _pl_total = 0   # 재생목록 전체 항목 수
                    for _ln in _p.stdout:
                        # 취소 신호 감지 시 stdout 읽기 중단 (proc는 이미 kill됨)
                        if getattr(self, "_link_save_cancelled", False):
                            break
                        # ── 재생목록 카운터 파싱 ─────────────────────────────
                        # yt-dlp 출력 예: "[download] Downloading item 3 of 25"
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
                        # ── 파일 경로 추적 (모든 패턴 누적) ──────────────────
                        # yt-dlp가 생성하는 파일 종류:
                        #   [download] Destination: path/title.f137.mp4    (video fragment)
                        #   [download] Destination: path/title.f140.m4a    (audio fragment)
                        #   [Merger]   Merging formats into: "path/title.mp4"  (병합 결과)
                        #   [ffmpeg]   Merging formats into "path/title.mp4"   (동일 패턴)
                        # 단일 변수 덮어쓰기 → set에 누적 (fragment 누락 방지)
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

                # ── 재생목록 항목별 병렬 다운로드 + 백그라운드 병합 헬퍼 ─────────
                def _playlist_parallel_dl():
                    """재생목록을 항목별로 처리한다.
                    각 항목: 비디오·오디오 동시 다운로드 → 완료 즉시 백그라운드 ffmpeg 병합
                    → 병합 중에 다음 항목 다운로드 시작 (파이프라인).
                    """
                    import threading as _threading
                    from concurrent.futures import ThreadPoolExecutor as _TPE
                    nonlocal dest_file

                    entries = _meta.get("entries") or []
                    total   = len(entries)
                    if total == 0:
                        return 1, []
                    all_ok     = True
                    merge_pool = _TPE(max_workers=total)  # 병합은 모두 동시 진행
                    merge_futs = []

                    # cmd 에서 -f / -o / --merge-output-format / --postprocessor-args 제거
                    # → 스트림별로 개별 지정하기 위해
                    _skip_next = False
                    _pl_base   = []
                    _strip_keys = {"-f", "-o", "--merge-output-format",
                                   "--postprocessor-args"}
                    for _item in cmd[:-1]:   # 마지막 url 제외
                        if _skip_next:
                            _skip_next = False
                            continue
                        if _item in _strip_keys:
                            _skip_next = True
                            continue
                        _pl_base.append(_item)

                    for idx in range(1, total + 1):
                        if getattr(self, "_link_save_cancelled", False):
                            break

                        # 진행 카운터 초기화
                        self.root.after(0, lambda i=idx, t=total:
                            self._dl_progress_update(0.0, f"({i}/{t})"))

                        # 임시 출력 템플릿 (비디오/오디오 구분 접미사)
                        _tmpl_vid = os.path.join(
                            dl_dir, "%(title)s.%(id)s.__vid.%(ext)s")
                        _tmpl_aud = os.path.join(
                            dl_dir, "%(title)s.%(id)s.__aud.%(ext)s")

                        _vid_file = [None]
                        _aud_file = [None]
                        _vid_rc   = [1]
                        _aud_rc   = [1]

                        def _dl_stream(stream_cmd, file_ref, rc_ref,
                                       show_prog, _i=idx, _t=total):
                            _sp = subprocess.Popen(
                                stream_cmd,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8",
                                errors="replace",
                                creationflags=0x08000000)
                            self._link_save_proc = _sp
                            for _sln in _sp.stdout:
                                if getattr(self, "_link_save_cancelled", False):
                                    _sp.kill()
                                    break
                                if show_prog:
                                    _sm = re.search(
                                        r'\[download\]\s+([\d.]+)%', _sln)
                                    if _sm:
                                        _pct = float(_sm.group(1))
                                        self.root.after(
                                            0, lambda p=_pct, i=_i, t=_t:
                                            self._dl_progress_update(
                                                p, f"({i}/{t})"))
                                _df = re.search(
                                    r'\[download\]\s+Destination:\s+(.+)',
                                    _sln)
                                if _df:
                                    _fp = _df.group(1).strip()
                                    file_ref[0] = _fp
                                    self._link_save_tracked_files.add(_fp)
                            _sp.wait()
                            rc_ref[0] = _sp.returncode

                        _vcmd = [*_pl_base,
                                 "--playlist-items", str(idx),
                                 "-f", "bestvideo",
                                 "--no-post-overwrites",
                                 "-o", _tmpl_vid, url]
                        _acmd = [*_pl_base,
                                 "--playlist-items", str(idx),
                                 "-f", "bestaudio",
                                 "--no-post-overwrites",
                                 "-o", _tmpl_aud, url]

                        _vt = _threading.Thread(
                            target=_dl_stream,
                            args=(_vcmd, _vid_file, _vid_rc, True))
                        _at = _threading.Thread(
                            target=_dl_stream,
                            args=(_acmd, _aud_file, _aud_rc, False))
                        _vt.start(); _at.start()
                        _vt.join();  _at.join()

                        if getattr(self, "_link_save_cancelled", False):
                            break

                        if _vid_rc[0] != 0 or _aud_rc[0] != 0:
                            all_ok = False
                            continue

                        _vf = _vid_file[0]
                        _af = _aud_file[0]
                        if not _vf or not _af:
                            all_ok = False
                            continue

                        # 최종 출력명: 비디오 파일에서 __vid.ext 제거 후 .mp4
                        _out_mp4 = re.sub(r'\.__vid\.[^.]+$', '.mp4', _vf)
                        self._link_save_tracked_files.add(_out_mp4)
                        dest_file = _out_mp4

                        # 백그라운드 병합 제출 (즉시 다음 항목 다운로드로 진행)
                        def _merge(_v=_vf, _a=_af, _o=_out_mp4):
                            try:
                                subprocess.run(
                                    [ffmpeg, "-y",
                                     "-i", _v, "-i", _a,
                                     "-c:v", "copy",
                                     "-c:a", "aac", "-b:a", "192k",
                                     _o],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=0x08000000)
                            finally:
                                for _tf in (_v, _a):
                                    try:
                                        if os.path.isfile(_tf):
                                            os.remove(_tf)
                                    except Exception:
                                        pass
                        merge_futs.append(merge_pool.submit(_merge))

                    # 모든 백그라운드 병합 완료 대기
                    if merge_futs:
                        self.root.after(0, lambda: self._link_status(
                            "⏳ 백그라운드 병합 완료 대기 중…"))
                        for _fut in merge_futs:
                            try:
                                _fut.result()
                            except Exception:
                                all_ok = False

                    merge_pool.shutdown(wait=False)
                    return (0 if all_ok else 1), []

                # ── 1차 실행 ─────────────────────────────────────────────────
                if _is_playlist:
                    _rc, _captured_lines = _playlist_parallel_dl()
                else:
                    _rc, _captured_lines = _exec_ytdlp(cmd)

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

                if _rc == 0:
                    self._link_save_dest_glob = None
                    self.root.after(0, lambda: self._dl_progress_update(100.0))
                    self.root.after(0, lambda: self._link_status(
                        f"✅ 저장 완료 → {dl_dir}"))
                    self.root.after(2500, self._dl_progress_hide)
                    self._log_lines.append(
                        f"[{ts}] ✅ yt-dlp 저장 완료 | 원본 URL: {url} | m3u8 추출=False")
                else:
                    self._link_save_dest_glob = None
                    # 플랫폼별 실패 안내 메시지
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
                        f"[{ts}] ❌ yt-dlp 저장 실패 (code {_rc}) | 원본 URL: {url} | fallback=없음")

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
                    # Soop: yt-dlp로 스트림 URL 추출
                    self._link_status("⏳ Soop 영상 정보 추출 중…")
                    play_url, real_title = self._fetch_soop_play_info(url)
                    # 실패 시 play_url == url(원본) → fallback 로그는 내부 기록
                else:
                    # 유튜브·치지직·페이스북·일반 URL 등: 원본 URL 그대로 PotPlayer에 전달
                    play_url   = url
                    real_title = ""
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
