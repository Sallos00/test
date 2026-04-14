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
