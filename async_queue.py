# -*- coding: utf-8 -*-
"""
async_queue.py -- PDD EZ Async Task Queue Module (t4)

Purpose: Replace all background paths in gui.py (batch recognition, realtime
screenshot, file OCR, table import) with a unified async task queue.

Design goals:
- Thread pool with configurable max_workers (default 1 to prevent API rate limits)
- Task state machine: pending / running / done / cancelled / error
- Progress reporting: tasks call progress(percent, stage) 0-100
- Cooperative cancellation: tasks check cancel_event.is_set()
- Error isolation: all exceptions caught, on_error callback invoked
- Thread-safe: lock-protected queue and state table
- Daemon workers: program exit never hangs

Thread safety contract:
- All callbacks (on_progress, on_done, on_error) fire in worker thread
- Caller is responsible for win.after() wrapping if needed
- This module knows nothing about Tk

API Signature Table:
--------------------------------------------------------------------------------
class TaskQueue:
    def __init__(self, max_workers: int = 1) -> None:
        # Thread pool; max_workers=1 to prevent API concurrency quota exhaustion

    def submit(
        self,
        name: str,
        fn: Callable[[ProgressCallback], Any],
        *,
        on_done: Callable[[Any], None] | None = None,
        on_progress: Callable[[int, str], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        # Returns unique task_id (uuid4). Same id cannot be reused.

    def task_status(self, task_id: str) -> Literal["pending","running","done","cancelled","error"]:
        # Query task state. Unknown id returns 'pending'.

    def wait(self, task_id: str, timeout: float | None = None) -> bool:
        # Block until task reaches terminal state. Returns True if done within timeout.

    def cancel(self, task_id: str) -> bool:
        # Request cancellation. Returns True if task became CANCELLED.
        # For pending tasks: state changes to CANCELLED, task never runs.
        # For running tasks: sets cancel_event (cooperative); returns False.

    def cancel_all(self) -> int:
        # Cancel all pending tasks. Returns count of cancelled tasks.

    def shutdown(self, wait: bool = True) -> None:
        # Shutdown thread pool. wait=True waits for running tasks.

ProgressCallback = Callable[[int, str], None]
    # Task function signature: fn(progress: ProgressCallback) -> Any
    # Task must check cancel_event.is_set() for cooperative cancellation.

--------------------------------------------------------------------------------
T5 gui.py Integration Guide (4 Paths)
--------------------------------------------------------------------------------

1. Batch Recognition (_run_batch_sequence)
   Old: threading.Thread(target=_batch_thread_wrapper, daemon=True).start()
        queue.Queue() polling _poll_batch_queue
   New: q = TaskQueue()
        task_id = q.submit(
            name="batch_recognition",
            fn=lambda prog: _run_batch_logic(prog),
            on_done=lambda _: _finish_batch(...),
            on_progress=lambda pct, stage: win.after(0, lambda: _update_status(pct, stage)),
            on_error=lambda e: win.after(0, lambda: _show_error(e)),
            cancel_event=self._batch_stop,
        )

2. Realtime Screenshot (_on_realtime_screenshot)
   Old: threading.Thread(target=task, daemon=True).start()
        win.after(0, lambda i=items: _fill_from_ocr(i))
   New: task_id = q.submit(
            name="realtime_ocr",
            fn=_realtime_task,
            on_done=lambda items: win.after(0, lambda i=items: _fill_from_ocr(i)),
            on_error=lambda e: win.after(0, lambda: status_text.set(f'OCR failed: {e}')),
        )

3. File OCR (_ocr_fill)
   Old: threading.Thread(target=task, daemon=True).start()
        win.after(0, lambda i=items: _fill_from_ocr(i, source='file'))
   New: task_id = q.submit(
            name="file_ocr",
            fn=lambda _: _ocr_generic_to_items(path, ...),
            on_done=lambda items: win.after(0, lambda i=items: _fill_from_ocr(i, source='file')),
            on_error=lambda e: win.after(0, lambda: _show_error(str(e))),
        )

4. Table Import (_do_import)
   Old: threading.Thread(target=task, daemon=True).start()
        win.after(0, lambda i, s: _import_done(i, s, has_region))
   New: task_id = q.submit(
            name="table_import",
            fn=lambda _: import_items(path, mapping=mapping),
            on_done=lambda result: win.after(0, lambda: _import_done(*result)),
            on_error=lambda e: win.after(0, lambda: messagebox.showerror("Import failed", str(e))),
        )
--------------------------------------------------------------------------------

Iron rules:
1. Pure stdlib (threading/queue/uuid), zero third-party dependencies
2. This module knows nothing about Tk; all callbacks fire in worker thread
3. Task functions are cooperatively cancelled -- must check cancel_event
4. Tasks are one-time: same task_id cannot be resubmitted
5. Worker threads are daemon -- program exit never hangs
"""

from __future__ import annotations

import queue
import threading
import uuid
from enum import Enum
from typing import Any, Callable

__all__ = [
    "TaskQueue",
    "ProgressCallback",
    "TaskState",
]


class TaskState(str, Enum):
    """Task lifecycle states."""
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"
    ERROR = "error"


# Type alias
ProgressCallback = Callable[[int, str], None]


class _TaskRecord:
    """Internal record for a single task."""
    __slots__ = (
        "name", "state", "result", "exc", "cancel_event",
        "on_done", "on_progress", "on_error",
        "fn", "done_event",
    )

    def __init__(
        self,
        name: str,
        fn: Callable[[ProgressCallback], Any],
        cancel_event: threading.Event | None,
        on_done: Callable[[Any], None] | None,
        on_progress: Callable[[int, str], None] | None,
        on_error: Callable[[BaseException], None] | None,
    ):
        self.name = name
        self.state: TaskState = TaskState.PENDING
        self.result: Any = None
        self.exc: BaseException | None = None
        self.cancel_event = cancel_event
        self.on_done = on_done
        self.on_progress = on_progress
        self.on_error = on_error
        self.fn = fn
        self.done_event = threading.Event()


class TaskQueue:
    """
    Async task queue with thread pool.

    Args:
        max_workers: Number of parallel worker threads. Default 1 (prevents
            API concurrency quota exhaustion). OCR tasks typically use 1.
    """

    def __init__(self, max_workers: int = 1) -> None:
        if max_workers < 0:
            raise ValueError("max_workers must be >= 0")
        self._max_workers = max_workers
        self._tasks: dict[str, _TaskRecord] = {}
        self._work_queue: queue.Queue[tuple[str, _TaskRecord] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._workers: list[threading.Thread] = []
        self._started = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(
        self,
        name: str,
        fn: Callable[[ProgressCallback], Any],
        *,
        on_done: Callable[[Any], None] | None = None,
        on_progress: Callable[[int, str], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """
        Submit a task and return a unique task_id.

        Args:
            name: Task name for debugging/logging.
            fn: Task function, signature: fn(progress: Callable[[int,str], None]) -> Any
                Call progress(percent, stage) to report 0-100 progress.
                Task MUST check cancel_event.is_set() for cooperative cancellation.
            on_done: Called when task completes normally. Callback fires in worker thread.
                     Caller must wrap with win.after() if using Tk.
            on_progress: Called to report progress. Callback fires in worker thread.
                         Caller must wrap with win.after() if using Tk.
            on_error: Called when task raises an exception. Callback fires in worker thread.
                      Caller must wrap with win.after() if using Tk.
            cancel_event: Optional cancellation event. fn should check is_set().
                         If not provided, an internal event is created.

        Returns:
            task_id: uuid4 string. Same id cannot be reused.
        """
        task_id = str(uuid.uuid4())

        with self._lock:
            if self._shutdown_event.is_set():
                raise RuntimeError("cannot submit to a shut down queue")

            record = _TaskRecord(
                name=name,
                fn=fn,
                cancel_event=cancel_event,
                on_done=on_done,
                on_progress=on_progress,
                on_error=on_error,
            )
            self._tasks[task_id] = record
            self._work_queue.put((task_id, record))
            self._ensure_started()

        return task_id

    def task_status(self, task_id: str) -> TaskState:
        """Query task state. Unknown id returns PENDING (treated as not started/cancelled)."""
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return TaskState.PENDING
            return record.state

    def wait(self, task_id: str, timeout: float | None = None) -> bool:
        """
        Block until task reaches terminal state.

        Returns:
            True  -- task reached terminal state (done/cancelled/error) within timeout.
            False -- timeout expired, task still pending/running.
        """
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return True  # Unknown task treated as finished
            done_event = record.done_event

        return done_event.wait(timeout=timeout)

    def cancel(self, task_id: str) -> bool:
        """
        Request cancellation of a task.

        Returns:
            True  -- task state changed to CANCELLED (pending task that never ran).
            False -- task already in running/done/error state, cannot cancel.
        """
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return False

            if record.state == TaskState.PENDING:
                record.state = TaskState.CANCELLED
                record.done_event.set()
                return True
            elif record.state == TaskState.RUNNING:
                # Cooperative cancellation: signal cancel_event
                if record.cancel_event is not None:
                    record.cancel_event.set()
                return False
            else:
                return False

    def cancel_all(self) -> int:
        """
        Cancel all pending tasks.

        Returns:
            Number of tasks that were cancelled.
        """
        cancelled = 0
        with self._lock:
            for task_id, record in self._tasks.items():
                if record.state == TaskState.PENDING:
                    record.state = TaskState.CANCELLED
                    record.done_event.set()
                    cancelled += 1
        return cancelled

    def shutdown(self, wait: bool = True) -> None:
        """
        Shutdown the thread pool.

        Args:
            wait: True (default) waits for running tasks; False returns immediately.

        修复：shutdown 时把 _tasks 里所有
        PENDING 任务标 CANCELLED + set done_event，让 task_status() 语义一致；
        仅 drain _work_queue 不会改 self._tasks，外部对 PENDING task_id 仍
        收到 PENDING（与 cancel() 行为不一致）。先标 PENDING→CANCELLED 再
        drain queue，最后 join workers（wait=True）。
        """
        self._shutdown_event.set()
        # R2: 先把所有 PENDING 记录标 CANCELLED + set done_event，保证
        # task_status() / wait() 行为与 cancel() 一致（先前仅 drain queue，
        # self._tasks 里 PENDING 状态泄漏，对外接口语义不一致）。
        with self._lock:
            for _tid, rec in list(self._tasks.items()):
                if rec.state == TaskState.PENDING:
                    rec.state = TaskState.CANCELLED
                    rec.done_event.set()
        # Drain queue of unprocessed items
        while True:
            try:
                self._work_queue.get_nowait()
            except queue.Empty:
                break

        if wait:
            for t in self._workers:
                t.join(timeout=30.0)

    def __enter__(self) -> "TaskQueue":
        return self

    def __exit__(self, *_) -> None:
        self.shutdown(wait=True)

    # ------------------------------------------------------------------
    # Internal implementation
    # ------------------------------------------------------------------

    def _ensure_started(self) -> None:
        """Ensure worker threads are started (lazy start)."""
        if self._started:
            return
        self._started = True
        for _ in range(self._max_workers):
            t = threading.Thread(target=self._worker_loop, daemon=True, name="AsyncTaskQueue-Worker")
            t.start()
            self._workers.append(t)

    def _worker_loop(self) -> None:
        """
        Worker main loop:
        - Dequeue task
        - Check cancellation state
        - Execute task (catch all exceptions)
        - Fire callbacks
        """
        while True:
            try:
                item = self._work_queue.get(timeout=0.1)
                if item is None:
                    continue
                task_id, record = item

                # Check shutdown after dequeue
                if self._shutdown_event.is_set():
                    break

                # Re-check state from shared dict (not from local variable)
                # This handles race where cancel()/cancel_all() was called
                # between submit and here. State was already set to CANCELLED.
                with self._lock:
                    if record.state == TaskState.CANCELLED:
                        continue

                # Cooperative cancellation checkpoint (before task starts)
                if record.cancel_event is not None and record.cancel_event.is_set():
                    with self._lock:
                        record.state = TaskState.CANCELLED
                        record.done_event.set()
                    continue

                # Mark as running
                with self._lock:
                    record.state = TaskState.RUNNING

                self._run_task(record)

            except queue.Empty:
                if self._shutdown_event.is_set():
                    break
                continue
            except Exception:
                # Rare: queue operation exception, record but don't crash worker
                continue

    def _run_task(self, record: _TaskRecord) -> None:
        """Execute a single task in the worker thread."""
        result = None
        exc: BaseException | None = None

        def progress_callback(percent: int, stage: str) -> None:
            """Wrap on_progress, exceptions do not propagate."""
            if record.on_progress:
                try:
                    record.on_progress(percent, stage)
                except Exception:
                    pass

        try:
            result = record.fn(progress_callback)
        except BaseException as e:
            exc = e

        # Cooperative cancellation checkpoint (after task body)
        if record.cancel_event is not None and record.cancel_event.is_set():
            with self._lock:
                record.state = TaskState.CANCELLED
                record.done_event.set()
            return

        if exc is not None:
            with self._lock:
                record.state = TaskState.ERROR
                record.exc = exc
                record.done_event.set()
            if record.on_error:
                try:
                    record.on_error(exc)
                except Exception:
                    pass
        else:
            with self._lock:
                record.state = TaskState.DONE
                record.result = result
                record.done_event.set()
            if record.on_done:
                try:
                    record.on_done(result)
                except Exception:
                    pass
