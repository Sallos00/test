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
from multiprocessing import Queue, Value
import tkinter as tk

from win32_utils import CFG
from gui.base        import LipSyncGUIBase
from gui.ui          import LipSyncGUIUI
from gui.record_open import LipSyncGUIRecordOpen
from gui.run         import LipSyncGUIRun
from gui.auth        import LipSyncGUIAuth


class LipSyncGUI(LipSyncGUIBase, LipSyncGUIUI, LipSyncGUIRecordOpen,
                 LipSyncGUIRun, LipSyncGUIAuth):
    pass


if __name__ == "__main__":
    mp.freeze_support()
    mp.set_start_method("spawn", force=True)

    QSIZE       = CFG["QUEUE_MAXSIZE"]
    lip_queue   = Queue(maxsize=QSIZE)
    audio_queue = Queue(maxsize=QSIZE)
    state_queue = Queue(maxsize=20)
    cmd_queue   = Queue(maxsize=10)
    stop_flag   = Value("b", False)

    root = tk.Tk()
    app  = LipSyncGUI(root, state_queue, cmd_queue, stop_flag,
                      lip_queue=lip_queue, audio_queue=audio_queue)
    root.mainloop()
