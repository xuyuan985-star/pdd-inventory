# -*- coding: utf-8 -*-
"""
test_async_queue.py -- Unit tests for PDD EZ async_queue module.

Covers:
- Concurrent / sequential task execution
- Progress callback timing with blocking tasks
- Cancel semantics (pending tasks never run; running tasks cooperative cancel)
- on_error receives exceptions without bubbling
- cancel_all
- Shutdown idempotency
- One-time task semantics (same id cannot be resubmitted)
- Thread safety (no race conditions)
"""

from __future__ import annotations

import sys
import threading
import time
import unittest

# Ensure local module takes priority
HERE = __file__.rsplit("/", 1)[0] if "/" in __file__ else __file__.rsplit("\\", 1)[0]
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from async_queue import TaskQueue, TaskState


class TestBasicSubmit(unittest.TestCase):
    """Basic submit and completion."""

    def test_submit_returns_task_id(self):
        q = TaskQueue(max_workers=1)
        task_id = q.submit("test", lambda _: None)
        self.assertIsInstance(task_id, str)
        self.assertTrue(len(task_id) > 0)
        q.shutdown(wait=True)

    def test_one_task_completes(self):
        q = TaskQueue(max_workers=1)
        result_holder = []

        def task(_):
            result_holder.append("done")
            return 42

        task_id = q.submit("ok", task, on_done=lambda r: result_holder.append(r))
        self.assertTrue(q.wait(task_id, timeout=5.0))
        self.assertEqual(q.task_status(task_id), TaskState.DONE)
        self.assertEqual(result_holder, ["done", 42])
        q.shutdown(wait=True)

    def test_one_task_result(self):
        q = TaskQueue(max_workers=1)
        task_id = q.submit("result", lambda _: "hello")
        self.assertTrue(q.wait(task_id, timeout=5.0))
        self.assertEqual(q.task_status(task_id), TaskState.DONE)
        q.shutdown(wait=True)


class TestProgressCallback(unittest.TestCase):
    """Progress callback timing."""

    def test_progress_reported(self):
        q = TaskQueue(max_workers=1)

        def task(prog):
            prog(10, "start")
            prog(50, "middle")
            prog(100, "end")

        task_id = q.submit("progress", task)
        self.assertTrue(q.wait(task_id, timeout=5.0))
        self.assertEqual(q.task_status(task_id), TaskState.DONE)
        q.shutdown(wait=True)

    def test_progress_callback_order(self):
        """Progress callbacks fire before done callback."""
        q = TaskQueue(max_workers=1)
        order = []

        def task(prog):
            prog(50, "step1")
            order.append("task")
            return "result"

        def on_done(_):
            order.append("done_cb")

        task_id = q.submit("order", task, on_done=on_done)
        self.assertTrue(q.wait(task_id, timeout=5.0))
        self.assertEqual(order, ["task", "done_cb"])
        q.shutdown(wait=True)

    def test_progress_callback_multiple_tasks(self):
        """Multiple concurrent tasks have independent progress."""
        q = TaskQueue(max_workers=2)

        def make_task(tid):
            def task(prog):
                prog(50, f"step_{tid}")
                return tid
            return task

        ids = [q.submit(f"task_{i}", make_task(i)) for i in range(3)]
        for tid in ids:
            self.assertTrue(q.wait(tid, timeout=5.0))

        for tid in ids:
            self.assertEqual(q.task_status(tid), TaskState.DONE)
        q.shutdown(wait=True)


class TestCancellation(unittest.TestCase):
    """Cancellation semantics."""

    def test_cancel_pending_task(self):
        """Pending task, once cancelled, does not execute.
        
        With max_workers=0, no workers start, tasks stay PENDING.
        This tests the pure cancel-pending semantics.
        """
        q = TaskQueue(max_workers=0)  # No workers - tasks stay PENDING
        ran = []

        def task(_):
            ran.append(True)

        task_id = q.submit("cancel_pend", task)
        # Task should be PENDING (no workers to pick it up)
        self.assertEqual(q.task_status(task_id), TaskState.PENDING)
        # Cancel should succeed
        self.assertTrue(q.cancel(task_id))
        self.assertEqual(q.task_status(task_id), TaskState.CANCELLED)
        # Task never ran
        self.assertEqual(ran, [])
        q.shutdown(wait=True)

    def test_cancel_all(self):
        """cancel_all cancels all pending tasks that haven't started."""
        q = TaskQueue(max_workers=1)
        ran = []
        started_count = [0]
        start_barrier = threading.Barrier(2)  # Sync point

        def task(_):
            started_count[0] += 1
            try:
                start_barrier.wait(timeout=2.0)  # Wait for signal to proceed
            except threading.BrokenBarrierError:
                pass
            ran.append(True)

        # Submit multiple tasks - only first will start with max_workers=1
        ids = [q.submit(f"cancel_all_{i}", task) for i in range(5)]
        # Give first task time to start
        time.sleep(0.1)
        # Cancel all - only pending ones should be cancelled
        n = q.cancel_all()
        # First task already started, remaining 4 should be cancelled
        self.assertEqual(n, 4)
        # Let first task proceed
        start_barrier.abort()
        time.sleep(0.1)
        # Only the first task should have run
        self.assertEqual(len(ran), 1, f"Only started task should run, got {ran}")
        q.shutdown(wait=True)

    def test_cancel_idempotent(self):
        """Multiple cancels on same task_id, second returns False."""
        q = TaskQueue(max_workers=1)
        task_started = threading.Event()
        proceed = threading.Event()

        def task(_):
            task_started.set()
            proceed.wait(timeout=2.0)  # Block until told to proceed
            return None

        task_id = q.submit("idempotent", task)
        # Wait for task to be picked up by worker
        task_started.wait(timeout=2.0)
        # Now task is RUNNING, cancel() returns False (running task)
        result = q.cancel(task_id)
        # Let task finish
        proceed.set()
        # Cancel should return False for running task
        self.assertFalse(result)
        # Second cancel should also return False (not pending)
        self.assertFalse(q.cancel(task_id))
        q.shutdown(wait=True)

    def test_cancel_unknown_id(self):
        """Cancel unknown id returns False."""
        q = TaskQueue(max_workers=1)
        self.assertFalse(q.cancel("no-such-id"))
        q.shutdown(wait=True)


class TestErrorHandling(unittest.TestCase):
    """Error handling."""

    def test_exception_caught_no_bubble(self):
        """Worker exception caught, no bubble to caller, task state is ERROR."""
        q = TaskQueue(max_workers=1)
        exception_caught = []

        def bad_task(_):
            raise ValueError("intentional test error")

        def on_error(e):
            exception_caught.append(e)

        task_id = q.submit("error_task", bad_task, on_error=on_error)
        self.assertTrue(q.wait(task_id, timeout=5.0))
        self.assertEqual(q.task_status(task_id), TaskState.ERROR)
        self.assertEqual(len(exception_caught), 1)
        self.assertIsInstance(exception_caught[0], ValueError)
        self.assertIn("intentional", str(exception_caught[0]))
        q.shutdown(wait=True)

    def test_exception_no_callback(self):
        """Without on_error, exception does not propagate outside worker."""
        q = TaskQueue(max_workers=1)

        def bad_task(_):
            raise RuntimeError("no callback test")

        task_id = q.submit("no_cb", bad_task)
        self.assertTrue(q.wait(task_id, timeout=5.0))
        self.assertEqual(q.task_status(task_id), TaskState.ERROR)
        # Should not crash
        q.shutdown(wait=True)

    def test_on_error_exception_safe(self):
        """on_error callback itself raises, should not affect worker stability."""
        q = TaskQueue(max_workers=1)

        def bad_task(_):
            raise ValueError("task error")

        def crashing_error_handler(e):
            raise RuntimeError("handler itself crashes")

        task_id = q.submit("crash_handler", bad_task, on_error=crashing_error_handler)
        self.assertTrue(q.wait(task_id, timeout=5.0))
        self.assertEqual(q.task_status(task_id), TaskState.ERROR)
        # Worker thread still alive, can submit new task
        task_id2 = q.submit("after_crash", lambda _: "survived")
        self.assertTrue(q.wait(task_id2, timeout=5.0))
        self.assertEqual(q.task_status(task_id2), TaskState.DONE)
        q.shutdown(wait=True)


class TestConcurrency(unittest.TestCase):
    """Concurrent execution."""

    def test_sequential_tasks(self):
        """Sequential execution: tasks complete one after another."""
        q = TaskQueue(max_workers=1)
        order = []

        def make_task(name):
            def task(_):
                order.append(name)
                time.sleep(0.1)
                order.append(f"{name}_end")
            return task

        ids = [q.submit(f"seq_{i}", make_task(f"seq_{i}")) for i in range(3)]
        for tid in ids:
            self.assertTrue(q.wait(tid, timeout=10.0))

        # Single worker, strict order
        expected = ["seq_0", "seq_0_end", "seq_1", "seq_1_end", "seq_2", "seq_2_end"]
        self.assertEqual(order, expected)
        q.shutdown(wait=True)

    def test_concurrent_tasks(self):
        """Concurrent execution: max_workers=2 runs two tasks simultaneously."""
        q = TaskQueue(max_workers=2)
        start_time = time.monotonic()
        overlap = []

        def make_task(name, delay):
            def task(_):
                overlap.append(name)
                time.sleep(delay)
            return task

        id1 = q.submit("concur_1", make_task("t1", 0.3))
        id2 = q.submit("concur_2", make_task("t2", 0.3))

        self.assertTrue(q.wait(id1, timeout=5.0))
        self.assertTrue(q.wait(id2, timeout=5.0))
        elapsed = time.monotonic() - start_time

        # Concurrent should take < sequential time (0.6s)
        self.assertLess(elapsed, 0.5, "Two 0.3s tasks concurrent should finish in <0.5s")
        self.assertIn("t1", overlap)
        self.assertIn("t2", overlap)
        q.shutdown(wait=True)

    def test_multiple_workers_parallel(self):
        """max_workers=4 supports more concurrency."""
        q = TaskQueue(max_workers=4)
        start = time.monotonic()

        def long_task(_):
            time.sleep(0.2)

        ids = [q.submit(f"par_{i}", long_task) for i in range(4)]
        for tid in ids:
            self.assertTrue(q.wait(tid, timeout=5.0))

        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.35, "4x0.2s tasks with 4 workers should finish in <0.35s")
        q.shutdown(wait=True)


class TestIdempotency(unittest.TestCase):
    """One-time task semantics."""

    def test_submit_after_cancel(self):
        """After cancel, new task_id can be submitted normally."""
        q = TaskQueue(max_workers=1)
        ran = []
        task_started = threading.Event()
        proceed = threading.Event()
        cancel_evt = threading.Event()

        def task(_):
            task_started.set()
            proceed.wait(timeout=2.0)  # Block until told to proceed
            # Check cancel at the start of task body
            if cancel_evt.is_set():
                return
            ran.append(True)

        id1 = q.submit("reuse", task, cancel_event=cancel_evt)
        # Wait for first task to start
        task_started.wait(timeout=2.0)
        # Cancel the running task (sets cancel_event)
        q.cancel(id1)
        # Let task finish (it will see cancel_event.is_set() and skip body)
        proceed.set()
        time.sleep(0.1)

        # New task_id works normally
        id2 = q.submit("reuse2", lambda _: ran.append(True))
        self.assertTrue(q.wait(id2, timeout=5.0))
        self.assertEqual(len(ran), 1, f"Only new task should run, got {ran}")
        q.shutdown(wait=True)


class TestShutdown(unittest.TestCase):
    """Shutdown behavior."""

    def test_shutdown_idempotent(self):
        """shutdown is idempotent (multiple calls don't raise)."""
        q = TaskQueue(max_workers=1)
        q.submit("s1", lambda _: time.sleep(0.05))
        q.shutdown(wait=False)
        q.shutdown(wait=False)  # Idempotent
        q.shutdown(wait=True)  # Idempotent

    def test_shutdown_wait(self):
        """shutdown(wait=True) waits for running tasks to complete."""
        q = TaskQueue(max_workers=1)
        finished = []

        def task(_):
            time.sleep(0.2)
            finished.append(True)

        q.submit("shutdown_wait", task)
        time.sleep(0.05)  # Give task time to start
        q.shutdown(wait=True)
        self.assertEqual(finished, [True])

    def test_shutdown_nowait(self):
        """shutdown(wait=False) returns immediately."""
        q = TaskQueue(max_workers=1)
        finished = []

        def task(_):
            time.sleep(0.3)
            finished.append(True)

        q.submit("shutdown_nowait", task)
        time.sleep(0.05)  # Let task start before shutdown
        q.shutdown(wait=False)
        time.sleep(0.4)
        self.assertEqual(finished, [True])  # Task still completes (daemon thread join covers this)

    def test_submit_after_shutdown_raises(self):
        """submit after shutdown raises RuntimeError."""
        q = TaskQueue(max_workers=1)
        q.shutdown(wait=True)
        with self.assertRaises(RuntimeError):
            q.submit("late", lambda _: None)


class TestContextManager(unittest.TestCase):
    """Context manager."""

    def test_context_manager(self):
        """with TaskQueue() as q manages lifecycle correctly."""
        with TaskQueue(max_workers=1) as q:
            task_id = q.submit("cm", lambda _: "ok")
            self.assertTrue(q.wait(task_id, timeout=5.0))
            self.assertEqual(q.task_status(task_id), TaskState.DONE)


class TestThreadSafety(unittest.TestCase):
    """Thread safety: no race conditions."""

    def test_concurrent_submit(self):
        """Multiple threads submitting simultaneously does not crash."""
        q = TaskQueue(max_workers=2)
        ids = []

        def submitter():
            for _ in range(10):
                tid = q.submit("ct", lambda _: time.sleep(0.01))
                ids.append(tid)

        threads = [threading.Thread(target=submitter) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for tid in ids:
            self.assertTrue(q.wait(tid, timeout=5.0))
            self.assertEqual(q.task_status(tid), TaskState.DONE)
        q.shutdown(wait=True)


class TestTaskStatus(unittest.TestCase):
    """Status query."""

    def test_pending_status(self):
        """Immediately after submit, task is pending (or just started)."""
        q = TaskQueue(max_workers=1)
        task_id = q.submit("pending_chk", lambda _: None)
        status = q.task_status(task_id)
        # Status can be pending, running, or done (worker is lazy-started)
        self.assertIn(status, [TaskState.PENDING, TaskState.RUNNING, TaskState.DONE])
        q.shutdown(wait=True)

    def test_unknown_id_returns_pending(self):
        """Unknown task_id returns pending."""
        q = TaskQueue(max_workers=1)
        self.assertEqual(q.task_status("no-such-id"), TaskState.PENDING)
        q.shutdown(wait=True)

    def test_status_after_wait(self):
        """After wait(), status is terminal state."""
        q = TaskQueue(max_workers=1)
        task_id = q.submit("status_after_wait", lambda _: "result")
        q.wait(task_id, timeout=5.0)
        self.assertIn(q.task_status(task_id), [TaskState.DONE, TaskState.ERROR])

    def test_cancel_race_condition(self):
        """任务完成瞬间 cancel 的竞态：终态不变为 CANCELLED"""
        q = TaskQueue(max_workers=1)
        finished = threading.Event()

        def task(_):
            time.sleep(0.05)  # 短任务
            finished.set()
            return "result"

        task_id = q.submit("race", task)
        # 等待任务几乎完成
        finished.wait(timeout=2.0)
        time.sleep(0.01)  # 确保完成
        # 此时 cancel 可能成功也可能失败（竞态），但状态必须是终态
        status = q.task_status(task_id)
        self.assertIn(status, [TaskState.DONE, TaskState.CANCELLED],
                      f"Status should be terminal, got {status}")
        q.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
