"""
gui/auth.py -- 인증 팝업 메서드
"""
import threading
import tkinter as tk

import auth as _auth_module
from win32_utils import CFG


class LipSyncGUIAuth:
    def _check_auth_on_start(self):
        """시작 시 인증 상태 확인. 로컬 인증 있으면 서버 검증, 없으면 첫 실행 팝업."""
        _auth = _auth_module
        local  = _auth.get_local_auth()
        status = _auth.get_local_status()

        if local and status == _auth.AuthStatus.APPROVED:
            # 허가 상태 → APPROVED 확인과 동시에 버전 체크를 병렬로 시작
            # (기존: _after_auth_ok 호출 후 UI 스레드에서 시작 → 변경: 즉시 시작)
            # 기능 게이트 초기화 — 모든 초기 작업 통과 후 _do_start_app()에서 True로 전환
            self._features_ready = False
            self._version_check_started_early = True
            threading.Thread(
                target=self._check_version_and_start,
                daemon=True).start()
            self.root.after(0, self._after_auth_ok)
            # 팟플레이어 감지 조기 시작 — 인증/버전/레지 확인과 병렬로 실행
            # 실제 기능(스킵 팝업 등)은 _features_ready=True 가 될 때까지 차단됨
            self.root.after(200, self._start_oped_monitor)
            def _verify():
                resp = _auth.check_auth(local["pc_id"], local["token"])
                s = resp.get("status", "")
                if s == _auth.AuthStatus.REVOKED:
                    _auth.save_local_status(_auth.AuthStatus.REVOKED)
                    self.root.after(0, self._show_auth_blocked_popup)
            import threading as _t
            _t.Thread(target=_verify, daemon=True).start()
        elif status == _auth.AuthStatus.PENDING:
            self.root.after(0, self._show_auth_pending_popup)
        elif status == _auth.AuthStatus.REVOKED:
            self.root.after(0, self._show_auth_blocked_popup)
        else:
            self.root.after(0, self._show_auth_request_popup)

    def _after_auth_ok(self):
        """인증 완료 후 버전 체크 → 정상 실행 흐름 시작.

        백그라운드에서 서버 업데이트 시트 버전을 확인한다.
        - 버전 일치 또는 확인 실패 → 바로 _do_start_app() 호출
        - 버전 불일치 + G열 비차단  → 업데이트 팝업 후 _do_start_app() 호출
        - 버전 불일치 + G열 차단    → 팝업 없이 바로 _do_start_app() 호출
        기존 실행 흐름은 모두 _do_start_app()으로 유지된다.
        """
        self._auth_ok = True
        # _check_auth_on_start(APPROVED 경로)에서 이미 시작한 경우 중복 시작 방지
        # PENDING→APPROVED, REVOKED→APPROVED 경로에서는 플래그 없으므로 정상 시작
        if not getattr(self, "_version_check_started_early", False):
            threading.Thread(
                target=self._check_version_and_start,
                daemon=True).start()

    def _do_start_app(self):
        """실제 앱 시작 로직 — 기존 _after_auth_ok의 실행 흐름을 그대로 유지."""
        # 모든 초기 작업(인증·버전·레지) 통과 → 기능 게이트 오픈
        self._features_ready = True
        if self._autostart_var.get():
            self.root.after(500, self._toggle)
        else:
            # 조기 시작되지 않은 경우에만 oped 모니터 시작
            if not getattr(self, "_oped_monitor_running", False):
                self.root.after(200, self._start_oped_monitor)
        threading.Thread(
            target=self._monitor_for_popup,
            kwargs={"wait_for_exit": self._autostart_var.get()},
            daemon=True).start()
        # 5분마다 차단 여부 백그라운드 확인
        threading.Thread(target=self._monitor_auth, daemon=True).start()

    def _maybe_show_pot_setting_popup(self):
        """PotPlayer 설정 팝업 — 최초 1회만 표시."""
        if _auth_module.get_pot_setting_shown():
            return
        self._show_pot_setting_popup()

    def _show_pot_setting_popup(self):
        """PotPlayer 레지스트리 변경 안내 팝업.
        처음부터 항목 목록을 표시하고 변경 버튼 클릭 시
        5가지 작업을 병렬로 실행한 뒤 완료되면 자동으로 닫는다.
        """
        import os, shutil as _shutil, subprocess as _sp
        try:
            popup = tk.Toplevel(self.root)
            popup.title("Auto Sync — PotPlayer 설정")
            popup.resizable(False, False)
            popup.configure(bg=self.BG)
            popup.grab_set()

            r       = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
            PAD     = round(10 * r)
            F_TITLE = max(9,  round(11 * r))
            F_BODY  = max(8,  round(9  * r))
            F_BTN   = max(8,  round(9  * r))
            F_LOG   = max(7,  round(8  * r))

            TASKS = [
                ("extension",  "Extension (.as)"),
                ("ytdlp_mod",  "PotPlayer yt-dlp.exe"),
                ("ffmpeg",     "ffmpeg.exe"),
                ("nm3u8",      "N_m3u8DL-RE.exe"),
                ("ytdlp",      "yt-dlp.exe"),
                ("registry",   "레지스트리 변경"),
            ]

            # 처음부터 전체 크기로 팝업 표시
            self._place_popup(popup,
                              round(400 * r),
                              round(160 * r + len(TASKS) * round(18 * r)))

            # [Bug 1] 닫기/X 클릭 시 save_pot_setting_shown() 호출 안 함
            # save는 변경 → 작업 완료 후에만 _worker 내에서 호출
            def _close():
                try:
                    popup.destroy()
                except Exception:
                    pass

            popup.protocol("WM_DELETE_WINDOW", _close)

            tk.Label(popup, text="PotPlayer 설정",
                     font=("Segoe UI", F_TITLE, "bold"),
                     bg=self.BG, fg=self.TEXT).pack(pady=(PAD, 0))
            tk.Frame(popup, bg=self.BORDER, height=1).pack(
                fill="x", pady=(round(8 * r), 0))
            tk.Label(popup,
                     text="프로그램의 원활한 기능 작동을 위해\n"
                          "추가 다운로드 및 PotPlayer의 레지스트리를 변경 시키겠습니까?",
                     font=("Segoe UI", F_BODY),
                     bg=self.BG, fg=self.TEXT_MID,
                     justify="center").pack(pady=round(10 * r))

            # ── 진행 상황 표시 프레임 (처음부터 표시) ──────────────────
            prog_frame = tk.Frame(popup, bg=self.BG)
            prog_labels: dict[str, tk.Label] = {}

            # [Bug 2] 팝업 열릴 때 파일 존재 여부를 미리 확인해 초기 상태 표시
            pot_dir_init = (self._get_potplayer_dir()
                            if hasattr(self, "_get_potplayer_dir") else "")
            _ext_filename = "MediaPlayParse - yt-dlp.as"

            def _initial_status(key: str):
                """True=설치, False=미설치, None=대기 중(판별 불가)"""
                try:
                    if key == "extension":
                        if not pot_dir_init:
                            return None
                        dirs = [
                            os.path.join(pot_dir_init, "Extension", "Media", "UrlList"),
                            os.path.join(pot_dir_init, "Extension", "Media", "PlayParse"),
                        ]
                        return all(os.path.isfile(os.path.join(d, _ext_filename))
                                   for d in dirs)
                    elif key == "ytdlp_mod":
                        if not pot_dir_init:
                            return None
                        return os.path.isfile(
                            os.path.join(pot_dir_init, "Module", "yt-dlp.exe"))
                    elif key == "ffmpeg":
                        local = (self._ffmpeg_path()
                                 if hasattr(self, "_ffmpeg_path") else "")
                        return bool(os.path.isfile(local) or
                                    _shutil.which("ffmpeg"))
                    elif key == "nm3u8":
                        local = (self._nm3u8dl_re_path()
                                 if hasattr(self, "_nm3u8dl_re_path") else "")
                        return bool(os.path.isfile(local) or
                                    _shutil.which("N_m3u8DL-RE"))
                    elif key == "ytdlp":
                        local = (self._ytdlp_path()
                                 if hasattr(self, "_ytdlp_path") else "")
                        return bool(os.path.isfile(local) or
                                    _shutil.which("yt-dlp"))
                except Exception:
                    pass
                return None  # registry 포함 판별 불가 → 대기 중

            for key, name in TASKS:
                st = _initial_status(key)
                if st is True:
                    init_text, init_color = "설치", self.ACCENT3
                elif st is False:
                    init_text, init_color = "미설치", self.ACCENT2
                else:
                    init_text, init_color = "대기 중", self.TEXT_DIM

                # 이름(col 0) · 상태(col 1) 그리드 분리 정렬
                row_idx = TASKS.index((key, name))
                tk.Label(prog_frame, text=name,
                         font=("Consolas", F_LOG),
                         bg=self.BG, fg=self.TEXT_DIM,
                         anchor="w").grid(row=row_idx, column=0,
                                          sticky="w",
                                          padx=(round(PAD * 2), round(4 * r)),
                                          pady=round(1 * r))
                lbl = tk.Label(prog_frame, text=init_text,
                               font=("Consolas", F_LOG),
                               bg=self.BG, fg=init_color,
                               anchor="w",
                               wraplength=round(90 * r))
                lbl.grid(row=row_idx, column=1,
                         sticky="w",
                         padx=(round(PAD * 2), 0),
                         pady=round(1 * r))
                prog_labels[key] = lbl

            # 이름 열 고정, 상태 열만 오른쪽으로 확장
            prog_frame.grid_columnconfigure(0, weight=0)
            prog_frame.grid_columnconfigure(1, weight=1)
            prog_frame.pack(fill="x", padx=round(PAD * 8), pady=(0, round(6 * r)))

            def _set_status(key: str, text: str, color: str | None = None):
                lbl = prog_labels.get(key)
                if lbl:
                    c = color or self.TEXT_DIM
                    popup.after(0, lambda l=lbl, t=text, cc=c:
                                (l.config(text=t, fg=cc)
                                 if l.winfo_exists() else None))

            btn_f = tk.Frame(popup, bg=self.BG)
            btn_f.pack(pady=(0, PAD))

            BTN = dict(font=("Consolas", F_BTN, "bold"),
                       relief="flat", cursor="hand2",
                       padx=round(14 * r), pady=round(5 * r))

            change_btn = tk.Button(btn_f, text="변경",
                                   bg=self.BG3, fg=self.ACCENT,
                                   activebackground=self.BORDER,
                                   **BTN)
            change_btn.pack(side="left", padx=round(6 * r))
            close_btn  = tk.Button(btn_f, text="닫기",
                                   bg=self.BG3, fg=self.TEXT,
                                   activebackground=self.BORDER,
                                   command=_close, **BTN)
            close_btn.pack(side="left", padx=round(6 * r))

            def _on_change():
                # 버튼 비활성화 + X 버튼도 비활성화
                change_btn.config(state="disabled")
                close_btn.config(state="disabled")
                popup.protocol("WM_DELETE_WINDOW", lambda: None)

                def _worker():
                    import concurrent.futures as _cf

                    # pot_dir 는 extension / ytdlp_mod 에 필요
                    pot_dir = self._get_potplayer_dir() if hasattr(
                        self, "_get_potplayer_dir") else ""

                    # ── 단일 UAC 처리용 큐 ─────────────────────────────────────
                    # extension / ytdlp_mod 에서 PermissionError 발생 시
                    # 각 함수가 직접 _runas_powershell 을 호출하는 대신
                    # 여기에 PS 명령을 쌓아두고, 풀 완료 후 한 번에 실행한다.
                    uac_queue: list = []

                    # 다운로드 실패 또는 건너뜀 발생 여부 추적
                    _had_failure = [False]

                    def _run_extension():
                        _set_status("extension", "설치 중…", self.ACCENT3)
                        try:
                            if pot_dir:
                                self._bg_ensure_potplayer_extension(pot_dir, uac_queue=uac_queue)
                            # UAC 큐에 들어간 경우 상태는 UAC 처리 후 갱신
                            if not any(e["key"] == "extension" for e in uac_queue):
                                _set_status("extension", "✅ 완료", self.ACCENT3)
                        except Exception as _e:
                            _had_failure[0] = True
                            _set_status("extension", f"❌ 실패: {_e}", self.ACCENT2)
                            try:
                                self._log_lines.append(f"[PotPlayerSetting] extension 오류: {_e}")
                            except Exception:
                                pass

                    def _run_ytdlp_mod():
                        _set_status("ytdlp_mod", "설치 중…", self.ACCENT3)
                        try:
                            if pot_dir:
                                self._bg_ensure_potplayer_ytdlp(pot_dir, uac_queue=uac_queue)
                            if not any(e["key"] == "ytdlp_mod" for e in uac_queue):
                                _set_status("ytdlp_mod", "✅ 완료", self.ACCENT3)
                        except Exception as _e:
                            _had_failure[0] = True
                            _set_status("ytdlp_mod", f"❌ 실패: {_e}", self.ACCENT2)
                            try:
                                self._log_lines.append(f"[PotPlayerSetting] ytdlp_mod 오류: {_e}")
                            except Exception:
                                pass

                    def _run_ffmpeg():
                        _set_status("ffmpeg", "다운로드 중…", self.ACCENT3)
                        try:
                            self._ensure_ffmpeg()
                            _set_status("ffmpeg", "✅ 완료", self.ACCENT3)
                        except Exception as _e:
                            _had_failure[0] = True
                            _set_status("ffmpeg", f"❌ 실패: {_e}", self.ACCENT2)
                            try:
                                self._log_lines.append(f"[PotPlayerSetting] ffmpeg 오류: {_e}")
                            except Exception:
                                pass

                    def _run_nm3u8():
                        _set_status("nm3u8", "다운로드 중…", self.ACCENT3)
                        try:
                            self._ensure_nm3u8dl_re()
                            _set_status("nm3u8", "✅ 완료", self.ACCENT3)
                        except Exception as _e:
                            _had_failure[0] = True
                            _set_status("nm3u8", f"❌ 실패: {_e}", self.ACCENT2)
                            try:
                                self._log_lines.append(f"[PotPlayerSetting] nm3u8 오류: {_e}")
                            except Exception:
                                pass

                    def _run_ytdlp():
                        _set_status("ytdlp", "다운로드 중…", self.ACCENT3)
                        try:
                            self._ensure_ytdlp()
                            _set_status("ytdlp", "✅ 완료", self.ACCENT3)
                        except Exception as _e:
                            _had_failure[0] = True
                            _set_status("ytdlp", f"❌ 실패: {_e}", self.ACCENT2)
                            try:
                                self._log_lines.append(f"[PotPlayerSetting] ytdlp 오류: {_e}")
                            except Exception:
                                pass

                    # B4 레지스트리 파일 다운로드 + 자동 적용 (팝업 없음)
                    def _run_b4():
                        _set_status("registry", "다운로드 중…", self.ACCENT3)
                        try:
                            exec_url = _auth_module.get_server_exec_url()
                            if not exec_url:
                                _had_failure[0] = True
                                _set_status("registry", "건너뜀", self.TEXT_DIM)
                                try:
                                    self._log_lines.append(
                                        "[PotPlayerSetting] registry 건너뜀: "
                                        "서버 exec_url 미설정 (B4 셀 비어있음)")
                                except Exception:
                                    pass
                                return
                            import updater as _updater
                            fname = (exec_url.split("?")[0].rstrip("/")
                                     .split("/")[-1] or "pot_setting.reg")
                            dest = os.path.join(self.APP_DIR, fname)
                            os.makedirs(self.APP_DIR, exist_ok=True)
                            _updater._download(exec_url, dest, None)
                            # regedit /s → 확인 팝업 없이 자동 적용
                            # HKEY_CURRENT_USER 이므로 관리자 권한 불필요
                            _set_status("registry", "적용 중…", self.ACCENT3)
                            proc = _sp.Popen(
                                ["regedit", "/s", dest],
                                creationflags=0x08000000 if os.name == "nt" else 0)
                            proc.wait()
                            _set_status("registry", "✅ 완료", self.ACCENT3)
                        except Exception as _e:
                            _had_failure[0] = True
                            _set_status("registry", f"❌ 실패: {_e}", self.ACCENT2)
                            try:
                                self._log_lines.append(
                                    f"[PotPlayerSetting] registry 오류: {_e}")
                            except Exception:
                                pass

                    with _cf.ThreadPoolExecutor(max_workers=5) as _pool:
                        _pool.submit(_run_extension)
                        _pool.submit(_run_ytdlp_mod)
                        _pool.submit(_run_ffmpeg)
                        _pool.submit(_run_nm3u8)
                        _pool.submit(_run_ytdlp)

                    # B4 다운로드 (병렬 풀 완료 후 순차 실행)
                    _run_b4()

                    # ── 단일 UAC 처리 ──────────────────────────────────────────
                    # extension / ytdlp_mod / registry 를 한 번에 합쳐서
                    # _runas_powershell 을 1회만 호출한다.
                    if uac_queue:
                        import time as _t, shutil as _sh
                        for _entry in uac_queue:
                            _set_status(_entry["key"], "관리자 권한 요청 중…", self.ACCENT3)
                        combined_ps = "; ".join(_e["ps"] for _e in uac_queue)
                        self._runas_powershell(combined_ps)
                        _t.sleep(4)   # UAC 승인 + 실행 완료 대기
                        for _entry in uac_queue:
                            _chk = _entry.get("check")
                            # check=None 이면 실행 여부 확인 불가 → 성공으로 간주
                            # check=경로  이면 파일 존재 여부로 성공 판단
                            _ok = True if _chk is None else os.path.isfile(_chk)
                            _set_status(_entry["key"],
                                        "✅ 완료 (관리자)" if _ok else "⚠ 실패 (UAC 거부)",
                                        self.ACCENT3 if _ok else self.ACCENT2)
                            if not _ok:
                                _had_failure[0] = True
                            # 임시 파일/디렉터리 정리
                            _tmp = _entry.get("tmp")
                            if _tmp:
                                try:
                                    if os.path.isdir(_tmp):
                                        _sh.rmtree(_tmp, ignore_errors=True)
                                    elif os.path.isfile(_tmp):
                                        os.remove(_tmp)
                                except Exception:
                                    pass
                    # 실패·건너뜀이 하나라도 있으면 False, 전부 성공이면 True 기록
                    if _had_failure[0]:
                        _auth_module._save_settings({"pot_setting_shown": False})
                    else:
                        _auth_module.save_pot_setting_shown()
                    popup.after(800, _close)

                threading.Thread(target=_worker, daemon=True).start()

            change_btn.config(command=_on_change)

        except Exception:
            pass

    def _check_version_and_start(self):
        """백그라운드: 서버 업데이트 시트 버전 확인 → 결과에 따라 팝업 또는 바로 시작.

        - 서버 응답 실패 / 예외 발생 시 → 프로그램 종료 없이 바로 시작
        - 버전 일치                      → 바로 시작
        - 버전 불일치 + G열 '차단'       → 팝업 생략, 바로 시작
        - 버전 불일치 + G열 비차단       → 업데이트 팝업 표시
        """
        # H열 다운로드 권한 확인 — 버전 체크와 병렬 백그라운드 실행
        threading.Thread(
            target=self._check_download_permission_bg,
            daemon=True,
            name="download-perm-check",
        ).start()

        import logging as _log
        try:
            resp    = _auth_module.check_version()
            latest  = resp.get("latest", "").strip()
            current = _auth_module.APP_VERSION.strip()
            if latest and latest != current:
                # G열 '차단' 여부 확인 — 문자열 공백/케이스 방어
                pc_id   = _auth_module.get_pc_id()
                skipped = _auth_module.check_update_skipped(pc_id)
                _log.debug(
                    "[version_check] latest=%s current=%s skipped=%s",
                    latest, current, skipped)
                if skipped is True:
                    # G열 차단 확정 → 팝업 없이 바로 시작
                    self.root.after(0, self._do_start_app)
                    self.root.after(0, self._maybe_show_pot_setting_popup)
                    return
                # 차단 아님 → 업데이트 팝업 표시 (서버 B2 다운로드 URL도 전달)
                download_url = resp.get("url", "").strip()
                self.root.after(
                    0, lambda: self._show_update_popup(current, latest,
                                                       download_url=download_url))
                return
        except Exception:
            # 서버 응답 오류, 인터넷 미연결 등 → 무시하고 바로 시작
            pass
        # 버전 일치 / 체크 실패 → 바로 시작
        self.root.after(0, self._do_start_app)
        self.root.after(0, self._maybe_show_pot_setting_popup)

    def _check_download_permission_bg(self):
        """백그라운드: 인증목록 H열 다운로드 권한 확인 → 다운 버튼 표시/숨김.

        "차단"일 경우 저장 버튼 숨김, "허가"(또는 그 외)일 경우 표시.
        서버 오류 시 버튼 상태를 변경하지 않는다.
        """
        try:
            pc_id = _auth_module.get_pc_id()
            perm  = _auth_module.check_download_permission(pc_id)
            if perm:  # 빈 문자열(오류)이면 상태 변경 없음
                self.root.after(0, lambda: self._apply_download_permission(perm))
        except Exception:
            pass  # 오류 시 버튼 상태 그대로 유지

    def _apply_download_permission(self, perm: str):
        """다운로드 권한에 따라 저장 버튼 + 자막 토글 버튼 표시/숨김 (UI 스레드에서 호출).

        perm == "차단" : pack_forget() 으로 버튼 숨김
        perm == "허가" : 원래 pack 옵션으로 버튼 복원
        """
        btn = getattr(self, "_link_save_btn", None)
        if btn is None:
            return
        try:
            if perm == "차단":
                btn.pack_forget()
                for _attr in ("_link_sub_video_btn", "_link_sub_both_btn"):
                    _b = getattr(self, _attr, None)
                    if _b:
                        _b.pack_forget()
            else:
                kw = getattr(self, "_link_save_btn_pack_kw", dict(side="left"))
                btn.pack(**kw)
                for _attr, _kw_attr in (
                    ("_link_sub_video_btn", "_link_sub_video_btn_pack_kw"),
                    ("_link_sub_both_btn",  "_link_sub_both_btn_pack_kw"),
                ):
                    _b  = getattr(self, _attr, None)
                    _kw = getattr(self, _kw_attr, dict(side="left"))
                    if _b:
                        _b.pack(**_kw)
        except Exception:
            pass

    def _show_update_popup(self, current: str, latest: str, on_close=None, download_url: str = ""):
        """버전 불일치 시 업데이트 안내 팝업.

        - "업데이트" / "나중에" 모두 팝업을 닫고 앱을 정상 시작한다.
        - 팝업 강제 종료(X 버튼) 시에도 앱은 정상 시작된다.
        - 예외 발생 시 팝업 없이 바로 시작한다.
        """
        # 팝업 진입 시점에 G열 '차단' 여부를 재확인한다.
        # _check_version_and_start()의 확인과 root.after() 스케줄 사이의
        # 타이밍 간격에서 팝업이 노출될 수 있는 경로를 방어한다.
        # 확인 실패(네트워크 오류 등) 시에는 기존대로 팝업을 표시한다.
        try:
            if _auth_module.check_update_skipped(_auth_module.get_pc_id()):
                self._do_start_app()
                self._maybe_show_pot_setting_popup()
                return
        except Exception:
            pass
        try:
            popup = tk.Toplevel(self.root)
            popup.title("Auto Sync — 업데이트")
            popup.resizable(False, False)
            popup.configure(bg=self.BG)
            popup.grab_set()

            r       = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
            self._place_popup(popup, round(300 * r), round(250 * r))

            PAD     = round(10 * r)
            F_TITLE = max(9,  round(11 * r))
            F_BODY  = max(8,  round(9  * r))
            F_SMALL = max(7,  round(8  * r))
            F_BTN   = max(8,  round(9  * r))

            # 팝업 닫기 + 앱 시작 (X 버튼 포함 모든 닫기 경로)
            # on_close 가 지정된 경우(설정 팝업에서 호출) _do_start_app() 대신 콜백 실행
            def _close_and_start():
                try:
                    popup.destroy()
                except Exception:
                    pass
                if on_close is not None:
                    try:
                        on_close()
                    except Exception:
                        pass
                else:
                    self._do_start_app()
                    self._maybe_show_pot_setting_popup()

            popup.protocol("WM_DELETE_WINDOW", _close_and_start)

            # ── 제목 ──
            tk.Label(popup, text="업데이트 알림",
                     font=("Segoe UI", F_TITLE, "bold"),
                     bg=self.BG, fg=self.TEXT).pack(pady=(PAD, 0))
            tk.Frame(popup, bg=self.BORDER, height=1).pack(
                fill="x", pady=(round(10 * r), 0))

            # ── 버전 정보 ──
            info_f = tk.Frame(popup, bg=self.BG2,
                              padx=round(14 * r), pady=round(10 * r))
            info_f.pack(fill="x", padx=round(16 * r),
                        pady=(round(12 * r), 0))

            def _row(label, value, fg=None):
                row = tk.Frame(info_f, bg=self.BG2)
                row.pack(fill="x", pady=round(2 * r))
                tk.Label(row, text=label,
                         font=("Consolas", F_SMALL),
                         bg=self.BG2, fg=self.TEXT_MID,
                         width=8, anchor="e").pack(side="left")
                tk.Label(row, text=value,
                         font=("Consolas", F_SMALL, "bold"),
                         bg=self.BG2, fg=fg or self.TEXT).pack(
                    side="left", padx=(round(6 * r), 0))

            _row("현재 버전", current)
            _row("최신 버전", latest, fg=self.ACCENT3)

            # ── 안내 문구 ──
            tk.Label(popup,
                     text="새 버전이 있습니다.",
                     font=("Segoe UI", F_BODY),
                     bg=self.BG, fg=self.TEXT_MID).pack(
                pady=(round(8 * r), 0))

            tk.Frame(popup, bg=self.BORDER, height=1).pack(
                fill="x", padx=round(16 * r), pady=(round(12 * r), 0))

            # ── 버전 건너뛰기 체크박스 ──
            skip_var = tk.BooleanVar(value=False)
            tk.Checkbutton(popup, text="해당 버전 업데이트 건너뛰기",
                           variable=skip_var,
                           font=("Segoe UI", F_SMALL),
                           bg=self.BG, fg=self.TEXT_MID,
                           activebackground=self.BG,
                           selectcolor=self.BG2,
                           relief="flat", cursor="hand2").pack(
                pady=(round(4 * r), 0))

            # ── 버튼 ──
            btn_f = tk.Frame(popup, bg=self.BG)
            btn_f.pack(pady=round(8 * r))

            BTN = dict(font=("Consolas", F_BTN, "bold"),
                       relief="flat", cursor="hand2",
                       padx=round(14 * r), pady=round(5 * r))

            # "나중에" 핸들러:
            # skip 스레드를 _close_and_start() 이전에 기동한다.
            # _do_start_app() 내부 예외 전파와 무관하게 G열 갱신을 보장하기 위해
            # 서버 요청 스레드를 먼저 띄운 뒤 팝업을 닫는다.
            # daemon=False: 앱 종료 시에도 HTTP 요청 완료를 보장한다.
            def _on_later():
                should_skip = skip_var.get()   # popup 파괴 전에 값 확정
                if should_skip:
                    import logging as _log
                    pc_id = _auth_module.get_pc_id()
                    _log.debug("[update_skip] skip_update_version 요청 시작: %s", pc_id)
                    threading.Thread(
                        target=_auth_module.skip_update_version,
                        args=(pc_id,),
                        daemon=False).start()
                _close_and_start()             # 팝업 닫기 + 앱 시작

            # ── 업데이트 버튼 핸들러 ──────────────────────────────────────────
            # _start_update_download() 공용 메서드에 위임한다.
            # progress 콜백 → 버튼 텍스트 갱신, error 콜백 → 버튼 복원 + 팝업 오류 표시
            def _on_update():
                if not download_url:
                    import tkinter.messagebox as _mb
                    _mb.showerror(
                        "업데이트 오류",
                        "다운로드 URL을 가져올 수 없습니다.\n나중에 다시 시도해 주세요.",
                        parent=popup,
                    )
                    return

                # ── 다운로드 중: 닫기(X) 및 나중에 버튼 비활성화 ──
                popup.protocol("WM_DELETE_WINDOW", lambda: None)
                try:
                    later_btn.configure(state="disabled")
                except Exception:
                    pass
                update_btn.configure(state="disabled", text="다운로드 중…")

                def _on_progress(pct: int):
                    # 백그라운드 스레드 → root.after() 로 UI 스레드 전달
                    def _ui():
                        try:
                            update_btn.configure(text=f"다운로드 중… {pct}%")
                        except Exception:
                            pass
                    try:
                        self.root.after(0, _ui)
                    except Exception:
                        pass

                def _on_error(msg: str):
                    # 백그라운드 스레드 → root.after() 로 UI 스레드 전달
                    def _ui():
                        # 오류 시 버튼/닫기 복원
                        try:
                            popup.protocol("WM_DELETE_WINDOW", _close_and_start)
                        except Exception:
                            pass
                        try:
                            later_btn.configure(state="normal")
                        except Exception:
                            pass
                        try:
                            update_btn.configure(state="normal", text="업데이트")
                        except Exception:
                            pass
                        try:
                            import tkinter.messagebox as _mb2
                            _mb2.showerror(
                                "다운로드 실패",
                                f"업데이트 파일을 다운로드할 수 없습니다.\n{msg}",
                                parent=popup,
                            )
                        except Exception:
                            pass
                    try:
                        self.root.after(0, _ui)
                    except Exception:
                        pass

                self._start_update_download(
                    download_url,
                    on_progress=_on_progress,
                    on_error=_on_error,
                )

            # 업데이트 버튼 참조 보관 → 체크박스 비활성 연동
            update_btn = tk.Button(btn_f, text="업데이트",
                                   bg=self.BG3, fg=self.ACCENT,
                                   activebackground=self.BORDER,
                                   command=_on_update, **BTN)
            update_btn.pack(side="left", padx=round(6 * r))

            # "나중에" 버튼 참조 보관 → 다운로드 중 비활성화에 사용
            later_btn = tk.Button(btn_f, text="나중에",
                                  bg=self.BG3, fg=self.TEXT,
                                  activebackground=self.BORDER,
                                  command=_on_later, **BTN)
            later_btn.pack(side="left", padx=round(6 * r))

            # 체크박스 상태에 따라 업데이트 버튼 활성/비활성 전환
            def _on_skip_toggle(*_):
                try:
                    update_btn.configure(
                        state="disabled" if skip_var.get() else "normal")
                except Exception:
                    pass

            skip_var.trace_add("write", _on_skip_toggle)

            # ── 자동 업데이트: 체크 시 팝업 표시 직후 다운로드 자동 시작 ──
            if getattr(self, "_auto_update_var", None) and self._auto_update_var.get():
                popup.after(0, _on_update)

        except Exception:
            # 팝업 생성 실패 시에도 앱은 정상 시작
            self._do_start_app()
            self._maybe_show_pot_setting_popup()

    def _start_update_download(self, download_url: str, on_progress=None, on_error=None):
        """업데이트 파일 다운로드 → AutoSincUpDate.bat 생성·실행 → 앱 종료.

        업데이트 팝업과 설정 팝업 업데이트 버튼이 공용으로 사용한다.
        실제 다운로드·배치 생성 로직은 루트의 updater.py 에 위임한다.

        on_progress(pct: int) : 진행률(0-100) 콜백, 선택
        on_error(msg: str)    : 오류 콜백, 선택. 미전달 시 messagebox 표시
        """
        import updater as _updater

        # on_progress / on_error 는 백그라운드 스레드에서 직접 호출되므로
        # tkinter 위젯 조작이 필요하면 호출자(ui_logic2.py 등)가 root.after() 로
        # 이미 감싸서 전달한다. on_close 는 root.after() 로 감싸 전달한다.
        _updater.start_update_download(
            download_url=download_url,
            on_progress=on_progress,
            on_error=on_error,
            on_close=lambda: self.root.after(0, self._on_close),
        )

    def _monitor_auth(self):
        """5분마다 서버에서 차단 여부 확인. 차단 시 싱크 중지 + 차단 팝업."""
        import time as _time
        while not self._closing:
            _time.sleep(300)  # 5분
            if self._closing: return
            try:
                local = _auth_module.get_local_auth()
                if not local: return
                resp = _auth_module.check_auth(local["pc_id"], local["token"])
                s = resp.get("status", "")
                if s == _auth_module.AuthStatus.REVOKED:
                    _auth_module.save_local_status(_auth_module.AuthStatus.REVOKED)
                    def _on_revoked():
                        # 싱크 중지
                        if self._running:
                            self._toggle()
                        # 차단 팝업
                        self._show_auth_blocked_popup()
                    self.root.after(0, _on_revoked)
                    return
            except Exception:
                pass

    def _show_auth_request_popup(self):
        """첫 실행 인증 요청 팝업."""
        _auth = _auth_module

        popup = tk.Toplevel(self.root)
        popup.title("Auto Sync — 인증")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        popup.protocol("WM_DELETE_WINDOW", self._on_close)  # X 버튼 → 종료
        popup.grab_set()

        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        self._place_popup(popup, round(320 * r), round(270 * r))

        F_TITLE = max(9, round(11 * r))
        F_BODY  = max(8, round(9  * r))
        F_BTN   = max(8, round(9  * r))
        PAD     = round(20 * r)

        tk.Label(popup, text="사용 허가 인증",
                 font=("Segoe UI", F_TITLE, "bold"),
                 bg=self.BG, fg=self.TEXT).pack(pady=(PAD, 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(10*r), 0))

        self._auth_msg = tk.Label(popup,
                 text="이 PC에서 처음 실행됩니다.\n사용 허가 인증을 받으시겠습니까?",
                 font=("Segoe UI", F_BODY),
                 bg=self.BG, fg=self.TEXT, justify="center")
        self._auth_msg.pack(pady=(round(14*r), 0))

        # 사용자명 입력 필드
        name_f = tk.Frame(popup, bg=self.BG)
        name_f.pack(fill="x", padx=round(24*r), pady=(round(12*r), 0))
        tk.Label(name_f, text="사용자명",
                 font=("Consolas", F_BODY),
                 bg=self.BG, fg=self.TEXT,
                 anchor="center").pack(fill="x")
        name_var = tk.StringVar()
        name_entry = tk.Entry(name_f,
                 textvariable=name_var,
                 font=("Consolas", F_BODY),
                 bg=self.BG2, fg=self.TEXT,
                 insertbackground=self.ACCENT,
                 justify="center",
                 relief="flat", bd=0,
                 highlightthickness=1,
                 highlightbackground=self.BORDER,
                 highlightcolor=self.ACCENT)
        name_entry.pack(fill="x", pady=(4, 0), ipady=round(5*r))
        tk.Label(name_f, text="사용자명을 입력해야 확인 버튼이 활성화됩니다.",
                 font=("Consolas", max(7, round(8*r))),
                 bg=self.BG, fg=self.TEXT_MID,
                 anchor="center").pack(fill="x", pady=(4, 0))

        # 로딩 도트 (대기 중일 때 표시)
        self._auth_dot = tk.Label(popup, text="",
                 font=("Consolas", F_BODY),
                 bg=self.BG, fg=self.ACCENT)
        self._auth_dot.pack(pady=(round(6*r), 0))

        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x",
                 padx=round(16*r), pady=(round(8*r), 0))

        btn_f = tk.Frame(popup, bg=self.BG)
        btn_f.pack(pady=round(12*r))

        BTN = dict(font=("Consolas", F_BTN, "bold"), relief="flat",
                   cursor="hand2", padx=round(14*r), pady=round(5*r))

        # 확인 버튼 — 초기에는 비활성화
        self._auth_confirm_btn = tk.Button(btn_f, text="확인",
                  bg=self.BG3, fg=self.TEXT_DIM,
                  activebackground=self.BORDER,
                  state="disabled", **BTN)
        self._auth_confirm_btn.pack(side="left", padx=round(6*r))

        self._auth_close_btn = tk.Button(btn_f, text="닫기",
                  bg=self.BG3, fg=self.TEXT,
                  activebackground=self.BORDER,
                  command=self._on_close, **BTN)
        self._auth_close_btn.pack(side="left", padx=round(6*r))

        # 사용자명 입력 여부에 따라 확인 버튼 활성/비활성
        def on_name_change(*args):
            if name_var.get().strip():
                self._auth_confirm_btn.config(state="normal", fg=self.ACCENT)
            else:
                self._auth_confirm_btn.config(state="disabled", fg=self.TEXT_DIM)
        name_var.trace_add("write", on_name_change)
        name_entry.focus_set()

        stop_event = threading.Event()
        self._auth_stop_event = stop_event
        self._auth_popup      = popup

        def on_confirm():
            """확인 버튼 — 인증 요청 전송 후 폴링 시작."""
            username = name_var.get().strip()
            self._auth_confirm_btn.config(state="disabled", fg=self.TEXT_DIM)
            self._auth_close_btn.config(state="disabled")
            name_entry.config(state="disabled")
            self._auth_msg.config(text="인증 요청을 전송하는 중...")

            pc_id = _auth.get_pc_id()

            def _do_request():
                resp = _auth.request_auth(pc_id, username)
                if resp.get("ok") and resp.get("status") == "approved":
                    token = resp.get("token", "")
                    _auth.save_local_auth(pc_id, token)
                    self.root.after(0, lambda: _on_approved(token))
                elif resp.get("ok"):
                    # 대기 상태 로컬 저장 → 재실행 시 ④번 팝업 표시
                    _auth._save_settings({
                        "auth_id":     pc_id,
                        "auth_status": _auth.AuthStatus.PENDING,
                    })
                    self.root.after(0, _start_polling)
                else:
                    msg = resp.get("msg", "서버 연결 실패")
                    self.root.after(0, lambda: _on_request_error(msg))

            threading.Thread(target=_do_request, daemon=True).start()

        def _start_polling():
            """요청 성공 → 대기 UI로 전환 후 폴링 시작."""
            self._auth_msg.config(
                text="인증 요청이 전송됐습니다.\n허가를 기다리는 중입니다...")
            self._auth_close_btn.config(state="normal")
            self._auth_dot_count = 0
            self._animate_auth_dot()
            pc_id = _auth.get_pc_id()
            _auth.poll_until_approved(pc_id, _on_approved, _on_revoked,
                                      _on_request_error, stop_event)

        def _animate_auth_dot():
            """로딩 도트 애니메이션."""
            if stop_event.is_set(): return
            try:
                if not popup.winfo_exists(): return
            except Exception: return
            dots = ["●○○", "○●○", "○○●"]
            self._auth_dot_count = getattr(self, "_auth_dot_count", 0)
            self._auth_dot.config(text=dots[self._auth_dot_count % 3])
            self._auth_dot_count += 1
            popup.after(500, _animate_auth_dot)

        self._animate_auth_dot = _animate_auth_dot

        def _on_approved(token):
            """허가 완료 → 메시지 변경 후 자동 닫힘."""
            stop_event.set()
            try:
                if not popup.winfo_exists(): return
            except Exception: return
            for w in popup.winfo_children():
                try: w.destroy()
                except Exception: pass
            r2 = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
            PAD2 = round(24 * r2)
            new_h = round(140 * r2)
            new_w = round(280 * r2)
            x = self.root.winfo_x() + (self.root.winfo_width()  - new_w) // 2
            y = self.root.winfo_y() + (self.root.winfo_height() - new_h) // 2
            popup.geometry(f"{new_w}x{new_h}+{x}+{y}")
            tk.Label(popup, text="허가 완료",
                     font=("Segoe UI", max(9, round(11*r2)), "bold"),
                     bg=self.BG, fg=self.ACCENT3).pack(pady=(PAD2, 0))
            tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(10*r2), 0))
            tk.Label(popup,
                     text="허가가 완료되었습니다!\n잠시 후 자동으로 실행됩니다.",
                     font=("Segoe UI", max(8, round(9*r2))),
                     bg=self.BG, fg=self.TEXT, justify="center").pack(pady=(round(16*r2), 0))
            popup.after(2000, lambda: [popup.destroy(), self._after_auth_ok()])

        def _on_revoked():
            stop_event.set()
            self.root.after(0, self._show_auth_revoked_popup)
            try:
                if popup.winfo_exists(): popup.destroy()
            except Exception: pass

        def _on_request_error(msg):
            self._auth_msg.config(text=f"오류: {msg}\n다시 시도해 주세요.")
            name_entry.config(state="normal")
            on_name_change()  # 버튼 상태 재평가
            self._auth_close_btn.config(state="normal")
            self._auth_dot.config(text="")

        self._auth_confirm_btn.config(command=on_confirm)

    def _show_auth_pending_popup(self):
        """재실행 시 시트 상태가 대기 중일 때 팝업."""
        _auth = _auth_module

        # 이미 열려있으면 중복 방지
        if hasattr(self, '_auth_pending_popup') and self._auth_pending_popup:
            try:
                if self._auth_pending_popup.winfo_exists():
                    return
            except Exception:
                pass

        popup = tk.Toplevel(self.root)
        popup.title("Auto Sync — 허가 대기 중")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        popup.protocol("WM_DELETE_WINDOW", lambda: [_auth.save_local_status(_auth.AuthStatus.PENDING), self._on_close()])
        popup.grab_set()
        self._auth_pending_popup = popup

        r       = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        self._place_popup(popup, round(300 * r), round(180 * r))

        PAD     = round(20 * r)
        F_TITLE = max(9, round(11 * r))
        F_BODY  = max(8, round(9  * r))
        F_BTN   = max(8, round(9  * r))

        tk.Label(popup, text="사용 허가 대기 중",
                 font=("Segoe UI", F_TITLE, "bold"),
                 bg=self.BG, fg=self.TEXT).pack(pady=(PAD, 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(10*r), 0))
        tk.Label(popup,
                 text="프로그램 사용 허가 대기 중입니다.\n허가 후 자동으로 실행됩니다.",
                 font=("Segoe UI", F_BODY),
                 bg=self.BG, fg=self.TEXT, justify="center").pack(pady=(round(14*r), 0))

        dot_lbl = tk.Label(popup, text="●○○",
                 font=("Consolas", F_BODY),
                 bg=self.BG, fg=self.ACCENT)
        dot_lbl.pack(pady=(round(4*r), 0))

        stop_event = threading.Event()

        def _animate():
            dots = ["●○○", "○●○", "○○●"]
            count = [0]
            def _tick():
                if stop_event.is_set(): return
                try:
                    if not popup.winfo_exists(): return
                except Exception: return
                dot_lbl.config(text=dots[count[0] % 3])
                count[0] += 1
                popup.after(500, _tick)
            _tick()
        _animate()

        # 백그라운드에서 서버 확인 (1초 후 첫 확인, 이후 10초마다)
        pc_id = _auth.get_pc_id()
        def _poll():
            first = True
            while not stop_event.is_set():
                if first:
                    stop_event.wait(1)   # 팝업 표시 후 1초 대기
                    first = False
                else:
                    stop_event.wait(10)
                if stop_event.is_set(): return
                resp = _auth.check_auth(pc_id)
                s = resp.get("status", "")
                if resp.get("ok") and s == _auth.AuthStatus.APPROVED:
                    token = resp.get("token", "")
                    _auth.save_local_auth(pc_id, token)
                    stop_event.set()
                    def _on_approved_ui():
                        try:
                            if not popup.winfo_exists(): return
                        except Exception: return
                        for w in popup.winfo_children():
                            try: w.destroy()
                            except Exception: pass
                        r2 = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
                        PAD2 = round(24 * r2)
                        new_h = round(140 * r2)
                        new_w = round(280 * r2)
                        x = self.root.winfo_x() + (self.root.winfo_width()  - new_w) // 2
                        y = self.root.winfo_y() + (self.root.winfo_height() - new_h) // 2
                        popup.geometry(f"{new_w}x{new_h}+{x}+{y}")
                        tk.Label(popup, text="허가 완료",
                                 font=("Segoe UI", max(9, round(11*r2)), "bold"),
                                 bg=self.BG, fg=self.ACCENT3).pack(pady=(PAD2, 0))
                        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(10*r2), 0))
                        tk.Label(popup,
                                 text="허가가 완료되었습니다!\n잠시 후 자동으로 실행됩니다.",
                                 font=("Segoe UI", max(8, round(9*r2))),
                                 bg=self.BG, fg=self.TEXT, justify="center").pack(pady=(round(16*r2), 0))
                        popup.after(2000, lambda: [popup.destroy(), self._after_auth_ok()])
                    self.root.after(0, _on_approved_ui)
                    return
                elif s == _auth.AuthStatus.REVOKED:
                    _auth.save_local_status(_auth.AuthStatus.REVOKED)
                    stop_event.set()
                    try:
                        if popup.winfo_exists(): popup.destroy()
                    except Exception: pass
                    self.root.after(0, self._show_auth_blocked_popup)
                    return
        threading.Thread(target=_poll, daemon=True).start()

        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x",
                 padx=round(16*r), pady=(round(10*r), 0))
        btn_f = tk.Frame(popup, bg=self.BG)
        btn_f.pack(pady=round(10*r))
        tk.Button(btn_f, text="닫기",
                  font=("Consolas", F_BTN, "bold"),
                  bg=self.BG3, fg=self.TEXT,
                  activebackground=self.BORDER,
                  relief="flat", cursor="hand2",
                  padx=round(14*r), pady=round(5*r),
                  command=lambda: [
                      stop_event.set(),
                      _auth.save_local_status(_auth.AuthStatus.PENDING),
                      self._on_close()
                  ]).pack()

    def _show_auth_blocked_popup(self):
        """이미 인증된 사용자가 차단됐을 때 팝업 — 서버 확인 후 해제 시 자동 실행."""
        _auth = _auth_module

        popup = tk.Toplevel(self.root)
        popup.title("Auto Sync — 사용 차단")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        popup.protocol("WM_DELETE_WINDOW", lambda: [_auth.save_local_status(_auth.AuthStatus.REVOKED), self._on_close()])
        popup.grab_set()

        r       = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        self._place_popup(popup, round(300 * r), round(170 * r))

        PAD     = round(20 * r)
        F_TITLE = max(9, round(11 * r))
        F_BODY  = max(8, round(9  * r))
        F_BTN   = max(8, round(9  * r))

        tk.Label(popup, text="사용 차단",
                 font=("Segoe UI", F_TITLE, "bold"),
                 bg=self.BG, fg=self.ACCENT2).pack(pady=(PAD, 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(10*r), 0))
        tk.Label(popup,
                 text="프로그램 사용이 차단 되었습니다.\n프로그램을 종료합니다.",
                 font=("Segoe UI", F_BODY),
                 bg=self.BG, fg=self.TEXT, justify="center").pack(pady=(round(14*r), 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x",
                 padx=round(16*r), pady=(round(12*r), 0))
        tk.Button(popup, text="확인",
                  font=("Consolas", F_BTN, "bold"),
                  bg=self.BG3, fg=self.ACCENT2,
                  activebackground=self.BORDER,
                  relief="flat", cursor="hand2",
                  padx=round(14*r), pady=round(5*r),
                  command=lambda: [
                      _auth.save_local_status(_auth.AuthStatus.REVOKED),
                      self._on_close()
                  ]).pack(pady=round(10*r))

        # 백그라운드에서 서버 확인 — 차단 해제(허가)되면 UI 전환
        stop_event = threading.Event()
        pc_id = _auth.get_pc_id()

        def _poll():
            first = True
            while not stop_event.is_set():
                if first:
                    stop_event.wait(1)
                    first = False
                else:
                    stop_event.wait(10)
                if stop_event.is_set(): return
                resp = _auth.check_auth(pc_id)
                s = resp.get("status", "")
                if resp.get("ok") and s == _auth.AuthStatus.APPROVED:
                    token = resp.get("token", "")
                    _auth.save_local_auth(pc_id, token)
                    stop_event.set()
                    def _on_unblocked():
                        try:
                            if not popup.winfo_exists(): return
                        except Exception: return
                        for w in popup.winfo_children():
                            try: w.destroy()
                            except Exception: pass
                        r2 = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
                        new_h = round(140 * r2)
                        new_w = round(280 * r2)
                        x = self.root.winfo_x() + (self.root.winfo_width()  - new_w) // 2
                        y = self.root.winfo_y() + (self.root.winfo_height() - new_h) // 2
                        popup.geometry(f"{new_w}x{new_h}+{x}+{y}")
                        tk.Label(popup, text="차단 해제",
                                 font=("Segoe UI", max(9, round(11*r2)), "bold"),
                                 bg=self.BG, fg=self.ACCENT3).pack(pady=(round(24*r2), 0))
                        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(10*r2), 0))
                        tk.Label(popup,
                                 text="차단이 해제되어 정상 이용 가능합니다!\n잠시 후 자동으로 실행됩니다.",
                                 font=("Segoe UI", max(8, round(9*r2))),
                                 bg=self.BG, fg=self.TEXT, justify="center").pack(pady=(round(16*r2), 0))
                        popup.after(2000, lambda: [popup.destroy(), self._after_auth_ok()])
                    self.root.after(0, _on_unblocked)
                    return

        threading.Thread(target=_poll, daemon=True).start()

    def _show_auth_revoked_popup(self):
        """인증 거부 안내 팝업."""
        _auth = _auth_module
        _auth.clear_local_auth()

        popup = tk.Toplevel(self.root)
        popup.title("Auto Sync — 사용 허가 요청 거부")
        popup.resizable(False, False)
        popup.configure(bg=self.BG)
        popup.protocol("WM_DELETE_WINDOW", self._on_close)
        popup.grab_set()

        r = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        self._place_popup(popup, round(300 * r), round(170 * r))

        PAD     = round(20 * r)
        F_TITLE = max(9, round(11 * r))
        F_BODY  = max(8, round(9  * r))
        F_BTN   = max(8, round(9  * r))

        tk.Label(popup, text="사용 허가 요청 거부",
                 font=("Segoe UI", F_TITLE, "bold"),
                 bg=self.BG, fg=self.ACCENT2).pack(pady=(PAD, 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x", pady=(round(10*r), 0))
        tk.Label(popup,
                 text="사용 허가 요청이 거부됐습니다.\n프로그램을 종료합니다.",
                 font=("Segoe UI", F_BODY),
                 bg=self.BG, fg=self.TEXT, justify="center").pack(pady=(round(14*r), 0))
        tk.Frame(popup, bg=self.BORDER, height=1).pack(fill="x",
                 padx=round(16*r), pady=(round(12*r), 0))
        tk.Button(popup, text="확인",
                  font=("Consolas", F_BTN, "bold"),
                  bg=self.BG3, fg=self.ACCENT2,
                  activebackground=self.BORDER,
                  relief="flat", cursor="hand2",
                  padx=round(14*r), pady=round(5*r),
                  command=self._on_close).pack(pady=round(10*r))

    # ── 종료 ─────────────────────────────────────────────────────────────────
