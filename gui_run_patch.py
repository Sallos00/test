"""
gui_run_patch.py
────────────────
gui_run.py 에 _show_oped_skip_popup 메서드를 추가하는 패치 스크립트.

사용법:
    python gui_run_patch.py          # gui_run.py 와 같은 폴더에서 실행
"""

import re, shutil, os

TARGET = "gui_run.py"

if not os.path.exists(TARGET):
    print(f"❌ {TARGET} 파일을 찾을 수 없습니다.")
    raise SystemExit(1)

shutil.copy2(TARGET, TARGET + ".bak")
print(f"📦 원본 백업: {TARGET}.bak")

with open(TARGET, "r", encoding="utf-8") as f:
    src = f.read()

# ── 삽입할 코드 ──────────────────────────────────────────────────────────────
# _on_close 메서드 바로 앞에 삽입 (파일 끝 부분)

NEW_METHOD = '''
    # ── OP/ED 스킵 팝업 ──────────────────────────────────────────────────────
    # processes.py proc_analyzer 가 oped_prompt 를 state_queue 에 실어 보내면
    # _refresh() 가 이 메서드를 호출한다.
    #
    # 팝업 위치: 팟플레이어 동영상 우측 하단
    # 버튼     : [스킵] → oped_skip 커맨드, [닫기] → oped_no_skip 커맨드
    # 자동 닫힘: 10초 카운트다운 후 oped_no_skip 전송 (요구사항 3)
    # 쿨다운   : 스킵/닫기 모두 3분 (요구사항 4) → proc_analyzer 가 관리

    def _show_oped_skip_popup(self, prompt_info: dict, auto_mode: bool = False):
        """
        OP/ED 스킵 여부를 묻는 팝업을 팟플레이어 우측 하단에 표시한다.

        prompt_info = {"zone": "오프닝" or "엔딩", "skip_sec": 90}
        auto_mode   = True 이면 _auto_cmd_queue 사용, False 이면 cmd_queue 사용.
        """
        # 중복 팝업 방지
        if getattr(self, "_oped_popup_open", False):
            return

        import ctypes
        import ctypes.wintypes

        zone     = prompt_info.get("zone", "OP/ED")
        skip_sec = prompt_info.get("skip_sec", 90)

        # 팟플레이어 창 위치 조회
        from win32_utils import find_potplayer_hwnd
        hwnd = find_potplayer_hwnd()
        if not hwnd:
            return

        try:
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        except Exception:
            return

        # 팝업 크기
        r  = self.SCALES.get(self._scale_var.get(), self.SCALES["소"])["scale"]
        pw = round(280 * r)
        ph = round(88  * r)

        # 팟플레이어 우측 하단 기준 위치 (화면 밖으로 나가지 않게 클램프)
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        px = max(0, min(rect.right  - pw - 12, sw - pw))
        py = max(0, min(rect.bottom - ph - 48, sh - ph))

        self._oped_popup_open = True

        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)          # 타이틀바 없음
        popup.attributes("-topmost", True)    # 항상 위
        popup.configure(bg=self.BORDER)
        popup.geometry(f"{pw}x{ph}+{px}+{py}")

        # ── 커맨드 전송 헬퍼 ──────────────────────────────────────────────────
        def send_cmd(cmd: str):
            try:
                if auto_mode:
                    self._auto_cmd_queue.put_nowait(cmd)
                else:
                    self.cmd_queue.put_nowait(cmd)
            except Exception:
                pass

        countdown  = [10]
        _after_id  = [None]

        def close_popup(skip: bool):
            """팝업 닫기. skip=True이면 스킵, False이면 그냥 닫기."""
            self._oped_popup_open = False
            if _after_id[0]:
                try:
                    self.root.after_cancel(_after_id[0])
                except Exception:
                    pass
            send_cmd("oped_skip" if skip else "oped_no_skip")
            try:
                popup.destroy()
            except Exception:
                pass

        # ── UI ────────────────────────────────────────────────────────────────
        F_TITLE = max(8, round(9  * r))
        F_BTN   = max(7, round(8  * r))
        PAD     = round(10 * r)
        PAD_S   = round(6  * r)

        # 외곽 프레임 (테두리 역할)
        outer = tk.Frame(popup, bg=self.BORDER)
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        inner = tk.Frame(outer, bg=self.BG2, padx=PAD, pady=round(8 * r))
        inner.pack(fill="both", expand=True)

        # 제목 라벨 (카운트다운 갱신)
        lbl_text = f"🎵 {zone}을 스킵하시겠습니까? (10초)"
        lbl = tk.Label(inner,
                       text=lbl_text,
                       font=("Segoe UI", F_TITLE, "bold"),
                       bg=self.BG2, fg=self.TEXT,
                       anchor="w")
        lbl.pack(fill="x")

        tk.Label(inner,
                 text=f"스킵 시 {skip_sec}초 앞으로 이동합니다.",
                 font=("Consolas", max(7, F_TITLE - 1)),
                 bg=self.BG2, fg=self.TEXT_MID,
                 anchor="w").pack(fill="x", pady=(round(2 * r), 0))

        # 버튼 행
        btn_f = tk.Frame(inner, bg=self.BG2)
        btn_f.pack(anchor="e", pady=(PAD_S, 0))

        BTN = dict(font=("Consolas", F_BTN, "bold"),
                   relief="flat", cursor="hand2",
                   padx=round(12 * r), pady=round(3 * r))

        tk.Button(btn_f, text="⏭ 스킵",
                  bg=self.BG3, fg=self.ACCENT,
                  activebackground=self.BORDER,
                  command=lambda: close_popup(skip=True),
                  **BTN).pack(side="left", padx=(0, round(4 * r)))

        tk.Button(btn_f, text="닫기",
                  bg=self.BG3, fg=self.TEXT_MID,
                  activebackground=self.BORDER,
                  command=lambda: close_popup(skip=False),
                  **BTN).pack(side="left")

        # ── 10초 카운트다운 (요구사항 3) ──────────────────────────────────────
        def tick():
            countdown[0] -= 1
            if countdown[0] <= 0:
                close_popup(skip=False)
                return
            try:
                lbl.config(text=f"🎵 {zone}을 스킵하시겠습니까? ({countdown[0]}초)")
                _after_id[0] = self.root.after(1000, tick)
            except Exception:
                close_popup(skip=False)

        _after_id[0] = self.root.after(1000, tick)

'''

# ── 삽입 위치: _on_close 정의 바로 앞 ─────────────────────────────────────────
ANCHOR = "    def _on_close(self):"

if ANCHOR not in src:
    print("❌ 삽입 위치(_on_close)를 찾을 수 없습니다.")
    raise SystemExit(1)

new_src = src.replace(ANCHOR, NEW_METHOD + ANCHOR, 1)

# ── 이미 적용됐는지 확인 ──────────────────────────────────────────────────────
if "_show_oped_skip_popup" in src:
    print("ℹ️  _show_oped_skip_popup 이미 존재합니다. 패치를 건너뜁니다.")
    raise SystemExit(0)

with open(TARGET, "w", encoding="utf-8") as f:
    f.write(new_src)

print("✅ 패치 완료: _show_oped_skip_popup 메서드 추가됨")
print(f"   삽입 위치: _on_close 바로 앞")
