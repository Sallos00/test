"""
gui/run.py -- 실행 제어, 프로세스 관리, 갱신, 인증 팝업 메서드

PyArmor 트라이얼 코드객체 제한(~30개) 우회를 위해 Mixin으로 분리.
외부에서 'from gui.run import LipSyncGUIRun' 은 기존과 동일하게 동작함.
  - run_oped.py    : OpedMonitorMixin  (_start_oped_monitor, _stop_oped_monitor, _show_oped_skip_popup)
  - run_process.py : ProcessMixin      (_start_processes, _stop_processes, _reset)
  - run_popup.py   : PopupMixin        (_toggle, _wait_for_potplayer, _monitor_for_popup, _show_start_popup)
  - run_notify.py  : NotifyMixin       (_register_app_id, _toast, _destroy_app_root, _on_close)
  - run_refresh.py : RefreshMixin      (_refresh)
"""
from gui.run_oped    import OpedMonitorMixin
from gui.run_process import ProcessMixin
from gui.run_popup   import PopupMixin
from gui.run_notify  import NotifyMixin
from gui.run_refresh import RefreshMixin


class LipSyncGUIRun(OpedMonitorMixin, ProcessMixin, PopupMixin, NotifyMixin, RefreshMixin):
    """
    실행 제어 클래스. 모든 메서드는 Mixin에서 상속.
    main.py 의 LipSyncGUI 다중상속 구조와 완전히 호환됨.
    """
