"""mem_utils.py — 메모리·버퍼·큐 정리 공통 유틸리티

설계 원칙 (a + b + c = d):
  A. drain_queue(q)      : 큐/Pipe 한 개 비우기 + mp.Queue 파이프 버퍼 반환
  B. clear_buffers(*bufs): deque/list 버퍼 비우기
  C. run_gc()            : gc.collect() 실행
  D. release_mp_queue(q) : mp.Queue/Pipe close() + join_thread() → OS 파이프 버퍼 반환

  조합 함수:
    flush_queues(*qs)              = A × N
    flush_buffers(*bufs)           = B × N
    full_cleanup(...)              = A × N  + B × N + C
    release_queues(*qs)            = D × N
    full_cleanup_and_release(...)  = A × N  + B × N + C + D × N
"""
import gc as _gc


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
