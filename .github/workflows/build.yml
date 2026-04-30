"""
main.py -- AutoSinc 진입점
"""
import sys
import os

# ── [메모리 수정] OpenBLAS / OMP 스레드 풀 크기 제한 ──────────────────────────
# scipy.signal.correlate 최초 호출 시 OpenBLAS가 스레드 풀을 초기화한다.
# 기본값(논리 코어 수)이면 코어당 32MB 스택을 VirtualAlloc으로 커밋 →
#   11코어 환경 기준 numpy용·scipy용 각각 약 352MB, 합계 704MB가 Working Set에 올라옴.
# numpy와 scipy는 별도 OpenBLAS 인스턴스를 사용하므로 두 배로 발생한다.
# 이 앱의 correlate 입력은 수십~수백 샘플이어서 스레드 병렬화 이득이 없으므로
# 스레드 수를 1로 제한해 스레드 풀 자체를 만들지 않도록 한다.
# ※ numpy/scipy import 이전에 반드시 설정해야 한다.
for _env_key in ("OPENBLAS_NUM_THREADS", "OPENBLAS64_NUM_THREADS",
                 "OMP_NUM_THREADS", "MKL_NUM_THREADS",
                 "BLIS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_env_key, "1")

if getattr(sys, "frozen", False):
    _base = sys._MEIPASS
else:
    _base = os.path.dirname(os.path.abspath(__file__))

if _base not in sys.path:
    sys.path.insert(0, _base)

import multiprocessing as mp
import queue
import threading
import tkinter as tk

from win32_utils import CFG

# ── oped_db.json 초기화: 앱 시작 시 파일이 없으면 빈 JSON으로 생성 ──────────
try:
    _appdata = os.environ.get("APPDATA", "")
    if _appdata:
        _db_dir  = os.path.join(_appdata, "AutoSync")
        _db_path = os.path.join(_db_dir, "oped_db.json")
        os.makedirs(_db_dir, exist_ok=True)
        if not os.path.exists(_db_path):
            with open(_db_path, "w", encoding="utf-8") as _f:
                _f.write("{}")
except Exception:
    pass

from gui.base        import LipSyncGUIBase
from gui.ui_layout   import LipSyncGUILayout
from gui.ui_logic    import LipSyncGUILogic
from gui.ui_logic2   import LipSyncGUILogic2
from gui.ui_logic3   import LipSyncGUILogic3
from gui.record_open import LipSyncGUIRecordOpen
from gui.run         import LipSyncGUIRun
from gui.auth        import LipSyncGUIAuth


class LipSyncGUI(LipSyncGUIBase, LipSyncGUILayout, LipSyncGUILogic, LipSyncGUILogic2, LipSyncGUILogic3, LipSyncGUIRecordOpen,
                 LipSyncGUIRun, LipSyncGUIAuth):
    pass


if __name__ == "__main__":
    # P1(lip_capture)은 여전히 별도 프로세스 → spawn 유지
    mp.freeze_support()
    mp.set_start_method("spawn", force=True)

    try:
        QSIZE       = CFG["QUEUE_MAXSIZE"]
        # P1(프로세스)↔T3(스레드) 간 lip_queue: Pipe(단방향)로 교체
        #   - mp.Queue는 생성 시 OS 파이프 버퍼 ~32MB를 사전 할당.
        #   - Pipe는 실제 전송 데이터만큼만 버퍼를 사용 → 메모리 절감.
        #   - 초기값은 더미(재시작 시 _start_processes에서 항상 새로 생성).
        _lip_r, _lip_w = mp.Pipe(duplex=False)
        lip_queue   = _lip_r          # reader: T3(proc_analyzer)
        # _lip_w(writer)는 _start_processes에서 P1에 전달 후 부모에서 close됨.
        # 여기서는 참조만 유지해 GC 방지; 실제 close는 _start_processes 담당.
        audio_queue = queue.Queue(maxsize=QSIZE)     # P2(스레드)   → P3(스레드)
        state_queue = queue.Queue(maxsize=20)        # P3(스레드)   → GUI
        cmd_queue   = queue.Queue(maxsize=10)        # GUI          → P3(스레드)
        stop_flag   = threading.Event()             # P2·P3 스레드 종료 신호

        root = tk.Tk()
        app  = LipSyncGUI(root, state_queue, cmd_queue, stop_flag,
                          lip_queue=lip_queue, audio_queue=audio_queue)
        root.mainloop()
    except Exception:
        # 기록 및 사용자 알림: APPDATA\AutoSync\startup_error.log
        import traceback
        import datetime
        try:
            _appdata = os.environ.get("APPDATA", "")
            _err_dir = os.path.join(_appdata, "AutoSync") if _appdata else os.path.dirname(__file__)
            os.makedirs(_err_dir, exist_ok=True)
            _err_path = os.path.join(_err_dir, "startup_error.log")
            with open(_err_path, "a", encoding="utf-8") as _f:
                _f.write(f"\n--- {datetime.datetime.now().isoformat()} ---\n")
                traceback.print_exc(file=_f)
        except Exception:
            _err_path = "(로그 생성 실패)"
        try:
            # 윈도우 메시지박스로 오류 표시
            import tkinter.messagebox as _mb
            try:
                _tmp_root = tk.Tk()
                _tmp_root.withdraw()
                _mb.showerror("AutoSync 시작 오류", f"프로그램 시작 중 오류가 발생했습니다. 로그: {_err_path}")
                _tmp_root.destroy()
            except Exception:
                pass
        except Exception:
            pass
        # 예외를 다시 발생시켜 호출자(콘솔/OS)에 알려줌
        raise
