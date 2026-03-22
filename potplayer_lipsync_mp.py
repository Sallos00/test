"""
potplayer_lipsync_mp.py -- 진입점
"""
import multiprocessing as mp
from multiprocessing import Queue, Value

from win32_utils import CFG
from gui_base import LipSyncGUIBase
from gui_ui   import LipSyncGUIUI
from gui_run  import LipSyncGUIRun
import tkinter as tk


class LipSyncGUI(LipSyncGUIBase, LipSyncGUIUI, LipSyncGUIRun):
    """세 Mixin을 합친 최종 GUI 클래스."""
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
