"""
main.py -- AutoSinc 진입점
"""
import sys
import os

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
from gui.base        import LipSyncGUIBase
from gui.ui_layout   import LipSyncGUILayout
from gui.ui_logic    import LipSyncGUILogic
from gui.ui_logic2   import LipSyncGUILogic2
from gui.record_open import LipSyncGUIRecordOpen
from gui.run         import LipSyncGUIRun
from gui.auth        import LipSyncGUIAuth


class LipSyncGUI(LipSyncGUIBase, LipSyncGUILayout, LipSyncGUILogic, LipSyncGUILogic2, LipSyncGUIRecordOpen,
                 LipSyncGUIRun, LipSyncGUIAuth):
    pass


if __name__ == "__main__":
    # P1(lip_capture)은 여전히 별도 프로세스 → spawn 유지
    mp.freeze_support()
    mp.set_start_method("spawn", force=True)

    QSIZE       = CFG["QUEUE_MAXSIZE"]
    # P2·P3는 스레드 → queue.Queue (직렬화 오버헤드 없음)
    # lip_queue(P1↔T3)는 run.py에서 Pipe로 생성·관리 (32MB mp.Queue 파이프 버퍼 제거)
    audio_queue = queue.Queue(maxsize=QSIZE)     # P2(스레드)   → P3(스레드)
    state_queue = queue.Queue(maxsize=20)        # P3(스레드)   → GUI
    cmd_queue   = queue.Queue(maxsize=10)        # GUI          → P3(스레드)
    stop_flag   = threading.Event()             # P2·P3 스레드 종료 신호

    root = tk.Tk()
    app  = LipSyncGUI(root, state_queue, cmd_queue, stop_flag,
                      audio_queue=audio_queue)
    root.mainloop()
