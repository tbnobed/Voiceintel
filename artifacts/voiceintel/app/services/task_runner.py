"""
Bounded background task runner for the voicemail pipeline.

Why this exists:
The SendGrid Inbound Parse webhook, the manual /reprocess endpoint, and the
manual /poll endpoint each used to do `threading.Thread(target=..., daemon=True).start()`
with no upper bound. A burst of inbound voicemails (or a stuck SendGrid call,
or a hung Whisper transcription) would spawn one thread per request and each
thread would hold a Flask app context plus a DB connection. The default
SQLAlchemy pool is 5 + 10 overflow = 15 connections, so a burst of ~20
voicemails could exhaust the pool, blocking every subsequent web request.
faster-whisper / CTranslate2 inference is also not thread-safe, so unbounded
parallel transcription could deadlock the underlying model.

This module exposes a single module-level worker thread fed by a bounded
queue. Tasks are processed one at a time in FIFO order. Submitting to a
full queue returns False so the caller can return HTTP 503 (SendGrid will
retry the inbound webhook automatically). With one Whisper instance and one
Phi-3 instance on a single GPU/CPU, parallel processing buys nothing anyway;
serializing also makes backpressure behaviour predictable.

Public API:
    submit(fn, *args, **kwargs) -> bool   (False if queue full)
    pending_count() -> int                (queued + currently-running)
"""
import atexit
import logging
import queue
import threading

logger = logging.getLogger(__name__)

# Roughly two hours of backlog at ~40s per voicemail. Beyond this, queueing
# is meaningless and the operator should look at why processing is slow,
# not silently buffer thousands of jobs in memory.
_MAX_QUEUED = 200

_queue: "queue.Queue" = queue.Queue(maxsize=_MAX_QUEUED)
_running_lock = threading.Lock()
_running = 0
_shutdown = threading.Event()


def _worker() -> None:
    global _running
    while not _shutdown.is_set():
        try:
            item = _queue.get(timeout=1.0)
        except queue.Empty:
            continue
        if item is None:  # poison pill from atexit
            _queue.task_done()
            return
        fn, args, kwargs = item
        with _running_lock:
            _running += 1
        try:
            fn(*args, **kwargs)
        except Exception:
            logger.exception(
                "Background task %s raised — swallowed to keep worker alive",
                getattr(fn, "__name__", repr(fn)),
            )
        finally:
            with _running_lock:
                _running -= 1
            _queue.task_done()


_worker_thread = threading.Thread(
    target=_worker, name="vm-pipe", daemon=True
)
_worker_thread.start()


def submit(fn, *args, **kwargs) -> bool:
    """
    Queue `fn(*args, **kwargs)` for serialised background execution.

    Returns True if accepted, False if the queue is full. Callers facing a
    False return should respond 503 / queue depth message to the upstream
    sender (e.g. SendGrid will redeliver inbound webhooks on 5xx).
    """
    try:
        _queue.put_nowait((fn, args, kwargs))
        return True
    except queue.Full:
        logger.error(
            "Background queue full (%d items, max=%d) — rejecting %s",
            _queue.qsize(),
            _MAX_QUEUED,
            getattr(fn, "__name__", repr(fn)),
        )
        return False


def pending_count() -> int:
    """Queued + currently running. Cheap; safe to call from /health."""
    with _running_lock:
        return _queue.qsize() + _running


def queue_capacity() -> int:
    """Configured upper bound, for diagnostics."""
    return _MAX_QUEUED


def _shutdown_worker() -> None:
    """Best-effort clean shutdown on interpreter exit."""
    _shutdown.set()
    try:
        _queue.put_nowait(None)
    except queue.Full:
        pass


atexit.register(_shutdown_worker)
