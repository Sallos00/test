"""
video_hash.py  ─  OP/ED 구간 영상 Perceptual Hash 생성
======================================================

우선순위:
  1) video_path 가 실제 파일이면 → OpenCV VideoCapture로 프레임 추출 (정확)
  2) hwnd 가 있으면 → PotPlayer 창 캡처 (실시간 폴백)
  3) 둘 다 실패 → None 반환

알고리즘 (DCT-based pHash):
  1. 그레이스케일 + 32×32 리사이즈
  2. cv2.dct() 적용
  3. 상위 8×8 저주파 계수 추출
  4. DC 계수 제외한 평균 기준 이진화 → 64-bit 해시

성능 제약:
  * 전체 영상 분석 금지 → start_ms~end_ms 구간만 처리
  * 1초당 1프레임 샘플링 (최대 15프레임)
  * 창 캡처 폴백: 5프레임 × 0.25초 간격 (~1.25초)
"""
from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # 타입 힌트 전용

# pHash 파라미터
_DCT_SIZE   = 32   # DCT 입력 이미지 크기 (32×32)
_HASH_SIZE  = 8    # DCT 저주파 계수 크기 (8×8 = 64비트)

# 창 캡처 파라미터
_WIN_CAPTURE_COUNT    = 5     # 창 캡처 프레임 수
_WIN_CAPTURE_INTERVAL = 0.25  # 프레임 간 간격 (초)

# 파일 기반 파라미터
_FILE_MAX_FRAMES = 15   # 최대 추출 프레임 수


# ── 핵심 함수 ─────────────────────────────────────────────────────────────────

def generate_video_hash(video_path: str,
                        start_ms: int,
                        end_ms: int,
                        hwnd=None) -> list | None:
    """
    OP/ED 구간의 perceptual hash 생성.

    Args:
        video_path : 영상 파일 경로 또는 PotPlayer 창 제목
                     (실제 파일이 아닐 경우 창 캡처로 폴백)
        start_ms   : 구간 시작 시간 (밀리초)
        end_ms     : 구간 종료 시간 (밀리초)
        hwnd       : PotPlayer 창 핸들 (파일 기반 실패 시 폴백)

    Returns:
        64개의 0/1 정수 리스트 (pHash 벡터), 실패 시 None
    """
    frames = []

    # 1. 파일 기반 추출 시도 (가장 정확)
    if video_path and os.path.isfile(video_path):
        frames = _extract_frames_file(video_path, start_ms, end_ms)

    # 2. 창 캡처 폴백 (실시간)
    if not frames and hwnd:
        frames = _extract_frames_window(hwnd)

    if not frames:
        return None

    # 프레임별 pHash를 개별 보존 (집계하지 않음)
    # → 비교 시 sliding alignment로 구간 shift에 강인하게 대응
    hashes = []
    for frame in frames:
        try:
            h = _phash(frame)
            if h:
                hashes.append(h)
        except Exception:
            pass

    return hashes if hashes else None


# ── 프레임 추출 ───────────────────────────────────────────────────────────────

def _extract_frames_file(video_path: str,
                         start_ms: int,
                         end_ms: int) -> list:
    """
    OpenCV VideoCapture로 1초당 1프레임 추출.
    전체 영상이 아닌 start_ms~end_ms 구간만 처리한다.
    """
    try:
        import cv2
    except ImportError:
        return []

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []

    frames   = []
    dur_ms   = max(1, end_ms - start_ms)
    n_frames = min(_FILE_MAX_FRAMES, max(1, dur_ms // 1000))

    try:
        for i in range(n_frames):
            # 각 초의 중간 지점 (에지 아티팩트 회피)
            t_ms = start_ms + int(i * dur_ms / n_frames) + 500
            cap.set(cv2.CAP_PROP_POS_MSEC, float(t_ms))
            ret, frame = cap.read()
            if not ret or frame is None:
                continue
            try:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                frames.append(gray)
            except Exception:
                pass
    finally:
        cap.release()

    return frames


def _extract_frames_window(hwnd) -> list:
    """
    PotPlayer 창에서 현재 재생 중인 프레임을 캡처.
    _WIN_CAPTURE_COUNT 회, _WIN_CAPTURE_INTERVAL 초 간격.
    """
    try:
        import cv2
        from win32_utils import capture_window
    except ImportError:
        return []

    frames = []
    for i in range(_WIN_CAPTURE_COUNT):
        if i > 0:
            time.sleep(_WIN_CAPTURE_INTERVAL)
        raw = capture_window(hwnd)
        if raw is None:
            continue
        try:
            # UI 테두리 제거: 상하좌우 5% 마진 크롭
            h, w = raw.shape[:2]
            mh   = max(1, int(h * 0.05))
            mw   = max(1, int(w * 0.05))
            gray = cv2.cvtColor(raw[mh:h - mh, mw:w - mw],
                                cv2.COLOR_BGRA2GRAY)
            frames.append(gray)
        except Exception:
            pass
        finally:
            del raw

    return frames


# ── Perceptual Hash ───────────────────────────────────────────────────────────

def _phash(gray_frame) -> list:
    """
    단일 그레이스케일 프레임 → 64-bit DCT perceptual hash.

    Returns:
        64개의 0/1 정수 리스트, 실패 시 []
    """
    try:
        import cv2
        import numpy as np

        # 32×32 리사이즈 후 DCT
        resized = cv2.resize(gray_frame, (_DCT_SIZE, _DCT_SIZE),
                             interpolation=cv2.INTER_AREA)
        dct     = cv2.dct(resized.astype(np.float32))

        # 상위 8×8 저주파 계수
        dct_low  = dct[:_HASH_SIZE, :_HASH_SIZE]

        # DC 계수(0,0) 제외한 평균으로 이진화 → 조명 변화에 강인
        ac_vals  = dct_low.flatten()[1:]    # DC 제외
        mean_val = float(ac_vals.mean())

        bits = [1 if float(v) > mean_val else 0
                for v in dct_low.flatten()]
        return bits
    except Exception:
        return []


def _aggregate(hash_list: list) -> list:
    """
    여러 프레임 해시를 비트 단위 다수결로 집계.
    과반수가 1이면 1, 아니면 0.

    Args:
        hash_list : [[0,1,...], ...] 각 64비트 해시 목록
    Returns:
        집계된 64비트 해시 리스트
    """
    if not hash_list:
        return []
    if len(hash_list) == 1:
        return hash_list[0]

    n_bits    = len(hash_list[0])
    threshold = len(hash_list) / 2.0
    result    = []

    for i in range(n_bits):
        votes = sum(h[i] for h in hash_list if i < len(h))
        result.append(1 if votes > threshold else 0)

    return result
