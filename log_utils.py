"""
log_utils.py -- 로그 관련 유틸리티

send_log  : 오디오 캡처 프로세스(P2)용 — log_queue 또는 audio_queue로 전송
make_logger: 분석 프로세스(P3)용 — deque에 타임스탬프 포함 문자열 추가
"""
import time
from collections import deque


def make_send_log(audio_queue, log_queue, queue_put_fn):
    """P2용 send_log 함수를 반환한다."""
    def send_log(msg: str):
        full = f"[{time.strftime('%H:%M:%S')}] {msg}"
        if log_queue is not None:
            try:
                log_queue.put_nowait(full)
                return
            except Exception:
                pass
        queue_put_fn(audio_queue, ("LOG", full))
    return send_log


def make_add_log(log_lines: deque):
    """P3용 add_log 함수를 반환한다."""
    def add_log(msg: str):
        log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
    return add_log


# 상태별 로그 메시지 상수
STATUS_OK          = "정상"
STATUS_CORRECTED   = "보정 완료"
STATUS_COLLECTING  = "데이터 수집 중"
STATUS_NO_SIGNAL   = "신호 부족"
STATUS_LOW_CONF    = "신뢰도 부족"
STATUS_COOLDOWN    = "쿨다운 중"
STATUS_UNDETECTED  = "미감지"
STATUS_CEILING     = "상한 도달"
STATUS_NO_POT      = "팟플레이어 미감지"
STATUS_BUFFERING   = "버퍼 수집 중"
