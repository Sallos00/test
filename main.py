"""
main.py -- AutoSinc 진입점
"""
import sys
import os

# ── [메모리 수정] OpenBLAS / OMP 스레드 풀 크기 제한 ──────────────────────────
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

# ── oped_db.json 초기화 ──────────────────────────
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


def main():
    QSIZE = CFG["QUEUE_MAXSIZE"]

    _lip_r, _lip_w = mp.Pipe(duplex=False)
    lip_queue = _lip_r

    audio_queue = queue.Queue(maxsize=QSIZE)
    state_queue = queue.Queue(maxsize=20)
    cmd_queue   = queue.Queue(maxsize=10)
    stop_flag   = threading.Event()

    root = tk.Tk()
    app  = LipSyncGUI(root, state_queue, cmd_queue, stop_flag,
                      lip_queue=lip_queue, audio_queue=audio_queue)
    root.mainloop()


if __name__ == "__main__":
    try:
        mp.freeze_support()

        # ✅ 여기 핵심 수정 (크래시 원인 제거)
        try:
            mp.set_start_method("spawn")
        except RuntimeError:
            pass

        main()

    except Exception:
        import traceback
        print("=== 프로그램 실행 중 오류 발생 ===")
        traceback.print_exc()
        input("엔터를 누르면 종료됩니다...")
