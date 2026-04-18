"""mem_utils.py — 메모리·버퍼·큐 정리 공통 유틸리티

설계 원칙 (a + b + c = d):
  A. drain_queue(q)      : 큐/Pipe 한 개 비우기 + mp.Queue 파이프 버퍼 반환
  B. clear_buffers(*bufs): deque/list 버퍼 비우기
  C. run_gc()            : gc.collect() 실행
  D. release_mp_queue(q) : mp.Queue/Pipe close() + join_thread() → OS 파이프 버퍼 반환
  E. trim_working_set()  : SetProcessWorkingSetSize(-1,-1) → RAM 페이지 OS 반환

  조합 함수:
    flush_queues(*qs)              = A × N
    flush_buffers(*bufs)           = B × N
    full_cleanup(...)              = A × N  + B × N + C
    release_queues(*qs)            = D × N
    full_cleanup_and_release(...)  = A × N  + B × N + C + D × N
"""
import gc as _gc
import ctypes as _ctypes


# ── A. 큐/Pipe 드레인 ────────────────────────────────────────────────────────
def drain_queue(q) -> int:
    """큐 q를 비운다. mp.Queue / queue.Queue / multiprocessing.Connection 모두 지원.
    mp.Queue: cancel_join_thread() 먼저 호출해 피더 스레드 블로킹 방지.
    Connection(Pipe): poll()+recv() 반복으로 비운다.
    반환값: 꺼낸 아이템 수.
    """
    if q is None:
        return 0
    count = 0
    try:
        # Pipe Connection (poll+recv)
        if hasattr(q, 'poll') and hasattr(q, 'recv'):
            while q.poll():
                try:
                    q.recv()
                    count += 1
                except Exception:
                    break
            return count
        # mp.Queue / queue.Queue
        if hasattr(q, 'cancel_join_thread'):
            q.cancel_join_thread()
        while True:
            try:
                q.get_nowait()
                count += 1
            except Exception:
                break
    except Exception:
        pass
    return count


def flush_queues(*queues) -> int:
    """여러 큐를 순서대로 비운다."""
    total = 0
    for q in queues:
        if q is not None:
            total += drain_queue(q)
    return total


# ── B. 버퍼 비우기 ────────────────────────────────────────────────────────────
def clear_buffers(*bufs) -> None:
    """deque 또는 list 버퍼를 모두 비운다."""
    for b in bufs:
        if b is not None:
            try:
                b.clear()
            except Exception:
                pass


# ── C. GC 실행 ───────────────────────────────────────────────────────────────
def run_gc() -> None:
    """gc.collect()를 실행해 순환 참조를 즉시 수거한다."""
    _gc.collect()


# ── D. mp.Queue / Pipe Connection OS 반환 ────────────────────────────────────
def release_mp_queue(q) -> None:
    """mp.Queue / Pipe Connection의 파이프 버퍼를 OS에 반환한다.
    - mp.Queue:  드레인 → close() → join_thread()
    - Connection(Pipe): 드레인 → close()
    - queue.Queue: drain만 수행 (close() 없음)
    """
    if q is None:
        return
    drain_queue(q)
    try:
        q.close()
    except AttributeError:
        pass
    except Exception:
        pass
    try:
        q.join_thread()  # mp.Queue 전용 — Connection엔 없어서 AttributeError 무시됨
    except AttributeError:
        pass
    except Exception:
        pass


def release_queues(*queues) -> None:
    """여러 mp.Queue / Pipe Connection을 파이프 버퍼까지 반환한다."""
    for q in queues:
        release_mp_queue(q)


# ── 조합 함수 ────────────────────────────────────────────────────────────────
def full_cleanup(queues=(), bufs=()) -> None:
    """A + B + C: 큐 드레인 + 버퍼 클리어 + GC.
    싱크 보정 완료, 정상 판정, 대기 중 주기 정리 등 작업 단위 사이 정리에 사용.
    녹화 중에는 호출자가 직접 억제해야 한다.
    """
    flush_queues(*queues)
    clear_buffers(*bufs)
    run_gc()


def full_cleanup_and_release(queues=(), bufs=(), mp_queues=()) -> None:
    """A + B + C + D: 큐 드레인 + 버퍼 클리어 + GC + mp.Queue/Pipe 파이프 반환.
    프로세스/스레드 종료, 재시작 시 이전 큐를 완전히 버릴 때 사용.
    mp_queues: close()+join_thread()까지 호출할 큐 (소유자만 전달).
    queues:    드레인만 할 큐 (소유하지 않는 큐).
    """
    flush_queues(*queues)
    release_queues(*mp_queues)
    clear_buffers(*bufs)
    run_gc()


# ── E. Working Set 트림 ──────────────────────────────────────────────────────
def trim_working_set() -> None:
    """프로세스 Working Set을 OS에 반환 요청 (Windows 전용).

    SetProcessWorkingSetSize(handle, -1, -1) 을 호출해
    현재 접근되지 않는 RAM 페이지를 OS에 돌려준다.
    Commit(가상 주소 예약) 크기는 변하지 않으며,
    이후 해당 페이지에 접근하면 페이지 폴트로 자동 복구된다.

    주의: 복구 비용이 있으므로 반드시 '쉬는 구간'에만 호출한다.
      - _flush_and_gc() 직후 (GC 완료 + 10초 쿨다운 직전)
      - 싱크 OFF + 장시간 대기 중 주기 호출 (10분 간격)
    비-Windows 환경에서는 조용히 무시된다.
    """
    try:
        _kernel32 = _ctypes.windll.kernel32
        handle = _kernel32.GetCurrentProcess()
        # SIZE_T(-1) 캐스트 필수 — 그냥 -1 을 넘기면 64비트에서
        # INVALID_HANDLE_VALUE 로 해석되어 효과 없음
        _kernel32.SetProcessWorkingSetSize(
            handle,
            _ctypes.c_size_t(-1),
            _ctypes.c_size_t(-1),
        )
    except AttributeError:
        pass   # 비-Windows (windll 없음)
    except Exception:
        pass
