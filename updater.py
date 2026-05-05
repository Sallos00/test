"""
updater.py -- AutoSinc 업데이트 다운로드 및 배치 런처

gui/auth.py의 _start_update_download 에서 분리된 루트 모듈.
업데이트 관련 I/O 로직(다운로드, 배치 파일 생성·실행)을 담당한다.
"""
import http.cookiejar
import logging
import os
import re
import subprocess
import sys
import threading
import urllib.request

log = logging.getLogger(__name__)

# ── 고정 파일명 ──────────────────────────────────────────────────────────────
_TMP_FILENAME = "Auto Sinc.exe.tmp"   # 다운로드 임시 저장 파일명
_EXE_FILENAME = "Auto Sinc.exe"       # 최종 실행 파일명
_BAT_FILENAME = "AutoSincUpDate.bat"  # 업데이트 배치 파일명


# ── 내부 헬퍼 ────────────────────────────────────────────────────────────────

def _get_save_dir() -> str:
    """현재 실행 파일(frozen) 또는 스크립트 기준 디렉터리를 반환한다.

    Nuitka onefile은 bootstrap이 temp에 압축 해제 후 child를 실행하므로
    child의 sys.executable은 temp 경로를 가리킨다.
    NUITKA_ONEFILE_PARENT(bootstrap PID)로 원본 exe 위치를 역추적한다.
    """
    if getattr(sys, "frozen", False):
        parent_pid = os.environ.get("NUITKA_ONEFILE_PARENT", "")
        if parent_pid:
            try:
                import ctypes
                import ctypes.wintypes
                _k32  = ctypes.windll.kernel32
                # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                h = _k32.OpenProcess(0x1000, False, int(parent_pid))
                if h:
                    buf  = ctypes.create_unicode_buffer(32767)
                    size = ctypes.wintypes.DWORD(32767)
                    ok   = _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
                    _k32.CloseHandle(h)
                    if ok:
                        log.debug("[updater] bootstrap exe 경로: %s", buf.value)
                        return os.path.dirname(buf.value)
            except Exception as _e:
                log.warning("[updater] bootstrap 경로 조회 실패, sys.executable 사용: %s", _e)
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _resolve_gdrive(url: str) -> str:
    """Google Drive 공유 URL을 직접 다운로드 URL로 변환한다.

    Google Drive 는 25 MB 초과 파일에 대해 바이러스 검사 확인 페이지를
    반환한다. drive.usercontent.google.com + confirm=t 조합으로 우회한다.
    Google Drive URL이 아니면 원본 URL 그대로 반환한다.
    """
    for pat in (
        r'drive\.google\.com/file/d/([^/?]+)',
        r'drive\.google\.com/open\?id=([^&]+)',
        r'drive\.google\.com/uc[?&].*?id=([^&]+)',
        r'drive\.usercontent\.google\.com/download.*?[?&]id=([^&]+)',
    ):
        m = re.search(pat, url)
        if m:
            fid = m.group(1)
            log.debug("[updater] Google Drive ID 감지: %s", fid)
            return (
                "https://drive.usercontent.google.com/download"
                f"?id={fid}&export=download&confirm=t"
            )
    return url


def _stream_response_to_file(resp, dest_path: str, on_progress) -> None:
    """HTTP 응답 스트림을 dest_path 에 청크 단위로 저장한다.

    on_progress(pct: int) 콜백은 백그라운드 스레드에서 직접 호출된다.
    UI 업데이트가 필요한 경우 호출자가 root.after() 로 래핑해야 한다.
    """
    total      = int(resp.headers.get("Content-Length", 0) or 0)
    downloaded = 0
    CHUNK      = 65536   # 64 KB — 메모리/속도 균형

    with open(dest_path, "wb") as f:
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if total > 0 and on_progress:
                pct = min(100, int(downloaded * 100 / total))
                on_progress(pct)


def _download(url: str, dest_path: str, on_progress) -> None:
    """URL 을 dest_path 에 다운로드한다. 100 MB 초과 파일도 처리한다.

    처리 흐름:
      1. Google Drive URL → drive.usercontent URL 변환 (confirm=t)
      2. CookieJar + 리다이렉트 처리 opener 사용
      3. 응답 Content-Type 이 text/html 이면 confirm 토큰 재추출 후 재시도
         (구글 바이러스 검사 경고 페이지 우회)
      4. 청크 스트리밍으로 파일 저장
    """
    resolved = _resolve_gdrive(url)
    log.debug("[updater] 다운로드 시작: %s", resolved)

    jar    = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPRedirectHandler(),
    )

    req = urllib.request.Request(resolved, headers={"User-Agent": "Mozilla/5.0"})

    with opener.open(req, timeout=120) as resp:
        ct = resp.headers.get("Content-Type", "")

        # Google Drive 바이러스 검사 확인 페이지 감지
        if "text/html" in ct:
            html_head = resp.read(8192).decode("utf-8", errors="replace")
            # 페이지 내 confirm 토큰 추출 재시도
            m_tok = re.search(r'confirm=([0-9A-Za-z_\-]+)', html_head)
            if m_tok:
                token       = m_tok.group(1)
                confirm_url = resolved + "&confirm=" + token
                log.debug("[updater] confirm 토큰 재시도: %s", token)
                req2 = urllib.request.Request(
                    confirm_url, headers={"User-Agent": "Mozilla/5.0"})
                with opener.open(req2, timeout=120) as resp2:
                    _stream_response_to_file(resp2, dest_path, on_progress)
            else:
                raise RuntimeError(
                    "Google Drive 확인 페이지 처리 실패 — confirm 토큰 없음\n"
                    "브라우저에서 직접 다운로드 후 수동 업데이트해 주세요."
                )
        else:
            _stream_response_to_file(resp, dest_path, on_progress)

    log.debug("[updater] 다운로드 완료: %s", dest_path)


def _write_bat(bat_path: str, exe_path: str, tmp_path: str, pid: int) -> None:
    """AutoSincUpDate.bat 파일을 작성한다.

    배치 처리 순서:
      1. PID 기반 대기  — 현재 AutoSinc 프로세스 종료 확인
      2. 이름 기반 대기 — 동일 이름 프로세스 완전 종료 확인 (재시작 방어)
      3. 기존 exe 삭제  — 잠금 해제될 때까지 1초 간격 재시도
      4. tmp → exe 이름 변경
      5. Nuitka onefile 환경변수 제거
      6. 새 exe 실행
      7. 배치 자기 삭제
    """
    exe_name = os.path.basename(exe_path)   # "Auto Sinc.exe"
    workdir  = os.path.dirname(exe_path) or "C:\\"

    bat_lines = [
        "@echo off",
        # 1단계: PID 기반 대기
        ":wait_pid",
        f'tasklist /FI "PID eq {pid}" 2>nul | find /I "{pid}" > nul',
        "if errorlevel 1 goto :wait_name",
        "timeout /t 1 /nobreak > nul",
        "goto :wait_pid",
        # 2단계: 이름 기반 재확인 (완전 종료 보장)
        ":wait_name",
        f'tasklist /FI "IMAGENAME eq {exe_name}" 2>nul | find /I "{exe_name}" > nul',
        "if errorlevel 1 goto :replace",
        "timeout /t 1 /nobreak > nul",
        "goto :wait_name",
        # 3단계: 기존 exe 삭제 (파일 잠금 해제 대기 루프)
        ":replace",
        ":delloop",
        f'del /F /Q "{exe_path}" 2>nul',
        f'if exist "{exe_path}" (',
        "  timeout /t 1 /nobreak > nul",
        "  goto :delloop",
        ")",
        # 4단계: Auto Sinc.exe.tmp → Auto Sinc.exe 이름 변경
        f'move /Y "{tmp_path}" "{exe_path}"',
        "timeout /t 2 /nobreak > nul",
        # 5단계: Nuitka onefile 잔류 환경변수 제거
        # bootstrap이 child 실행 전 설정한 NUITKA_ONEFILE_PARENT가 남아 있으면
        # 새 exe가 자신을 child로 착각해 압축 해제 없이 실행을 시도한다.
        "set NUITKA_ONEFILE_PARENT=",
        # 6단계: 새 EXE 실행
        f'if exist "{exe_path}" (',
        f'  start "" /D "{workdir}" "{exe_path}"',
        ")",
        # 7단계: 배치 자기 삭제
        'del "%~f0"',
    ]

    with open(bat_path, "w", encoding="mbcs") as f:
        f.write("\r\n".join(bat_lines))

    log.debug("[updater] 배치 파일 작성: %s", bat_path)


def _launch_bat(bat_path: str) -> None:
    """배치 파일을 완전히 독립된 새 프로세스(cmd.exe)로 실행한다.

    CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW 플래그로 현재 프로세스
    종료 후에도 배치가 계속 동작하도록 보장한다.

    Nuitka onefile은 bootstrap이 child 실행 전 NUITKA_ONEFILE_PARENT 환경변수를
    설정한다. cmd.exe가 이를 상속하면 배치가 실행하는 새 exe도 이를 물려받아
    자신을 child로 착각한다. Win32 API로 직접 삭제해 이를 방지한다.
    """
    # Win32 레이어에서 Nuitka onefile 환경변수 제거
    # (배치 파일 내 set 명령으로도 제거하지만 cmd.exe 실행 시점에 이미
    #  상속되므로 Python 단에서도 미리 제거한다.)
    _NUITKA_KEYS = ("NUITKA_ONEFILE_PARENT",)
    try:
        import ctypes
        _k32 = ctypes.windll.kernel32
        for _key in _NUITKA_KEYS:
            _k32.SetEnvironmentVariableW(_key, None)
            log.debug("[updater] Win32 env 삭제: %s", _key)
    except Exception as _e:
        log.warning("[updater] Win32 env 처리 실패(계속 진행): %s", _e)

    # CRT 레이어(os.environ)에서도 제거
    clean_env = {k: v for k, v in os.environ.items() if k not in _NUITKA_KEYS}

    si = subprocess.STARTUPINFO()
    si.dwFlags    |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0   # SW_HIDE

    subprocess.Popen(
        ["cmd.exe", "/c", bat_path],
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                       | subprocess.CREATE_NO_WINDOW),
        startupinfo=si,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=clean_env,
        close_fds=True,
    )
    log.debug("[updater] 배치 런처 실행: %s", bat_path)


# ── 공개 API ─────────────────────────────────────────────────────────────────

def start_update_download(
    download_url: str,
    on_progress=None,
    on_error=None,
    on_close=None,
) -> None:
    """업데이트 파일 다운로드 → AutoSincUpDate.bat 생성·실행 → 앱 종료.

    백그라운드 스레드에서 실행되며, UI 콜백은 호출자(gui/auth.py)가
    root.after() 로 감싸 전달해야 한다.

    Parameters
    ----------
    download_url : str
        업데이트 시트 B2 셀의 다운로드 URL.
        Google Drive URL 은 자동으로 직접 다운로드 URL 로 변환된다.
    on_progress : callable(pct: int) | None
        진행률(0-100) 콜백. 백그라운드 스레드에서 직접 호출된다.
    on_error : callable(msg: str) | None
        오류 발생 시 콜백. 미전달 시 tkinter.messagebox 로 표시한다.
    on_close : callable() | None
        배치 실행 성공 후 앱 종료 콜백. 보통 self._on_close 를 전달한다.
        백그라운드 스레드에서 호출되므로 호출자가 root.after() 로 감싸야 한다.
    """

    def _worker():
        save_dir = _get_save_dir()
        exe_path = os.path.join(save_dir, _EXE_FILENAME)
        tmp_path = os.path.join(save_dir, _TMP_FILENAME)
        bat_path = os.path.join(save_dir, _BAT_FILENAME)

        # 이전 임시 파일 잔재 정리
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

        try:
            _download(download_url, tmp_path, on_progress)

            pid = os.getpid()
            _write_bat(bat_path, exe_path, tmp_path, pid)
            _launch_bat(bat_path)

            if on_close:
                on_close()

        except Exception as exc:
            log.warning("[updater] 업데이트 실패: %s", exc)
            # 실패한 임시 파일 정리
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            _notify_error(str(exc), on_error)

    threading.Thread(target=_worker, daemon=True).start()


def _notify_error(msg: str, on_error) -> None:
    """오류를 on_error 콜백 또는 기본 messagebox 로 표시한다."""
    if on_error:
        on_error(msg)
    else:
        try:
            import tkinter.messagebox as _mb
            _mb.showerror(
                "다운로드 실패",
                f"업데이트 파일을 다운로드할 수 없습니다.\n{msg}",
            )
        except Exception:
            pass
