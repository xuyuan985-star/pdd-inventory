"""exception_guard 模块单测"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import exception_guard as eg


class TestSummaryForException(unittest.TestCase):
    def test_unknown_class_fallback(self):
        s = eg.summary_for_exception(ValueError('boom'))
        self.assertIn('ValueError', s)
        self.assertIn('boom', s)

    def test_empty_str_exception(self):
        s = eg.summary_for_exception(ValueError(''))
        self.assertIn('ValueError', s)

    def test_none_exception_message(self):
        s = eg.summary_for_exception(RuntimeError(None))
        self.assertTrue(s)

    def test_sanitize_secret_in_message(self):
        # 异常消息含 api_key 明文 → 摘要脱敏
        s = eg.summary_for_exception(ConnectionError('api_key=sk-abc12345 failed'))
        self.assertNotIn('sk-abc12345', s)

    def test_ocr_category_reuse(self):
        # KeyError 等无 ocr_review 分类的走回退；确认函数不抛即可
        s = eg.summary_for_exception(KeyError('missing'))
        self.assertIsInstance(s, str)
        self.assertTrue(s)

    def test_never_raises_on_weird_exc(self):
        # 构造一个 str() 抛异常的怪对象
        class Weird:
            def __str__(self):
                raise RuntimeError('inner')
        s = eg.summary_for_exception(Weird())
        self.assertIsInstance(s, str)
        self.assertTrue(s)


class TestRateLimiter(unittest.TestCase):
    def test_first_notify_true(self):
        rl = eg.ExceptionRateLimiter(window_s=600)
        self.assertTrue(rl.should_notify('A'))

    def test_same_class_limited_within_window(self):
        rl = eg.ExceptionRateLimiter(window_s=600)
        self.assertTrue(rl.should_notify('A'))
        self.assertFalse(rl.should_notify('A'))

    def test_different_classes_independent(self):
        rl = eg.ExceptionRateLimiter(window_s=600)
        self.assertTrue(rl.should_notify('A'))
        self.assertTrue(rl.should_notify('B'))

    def test_window_expiry(self):
        rl = eg.ExceptionRateLimiter(window_s=1)
        self.assertTrue(rl.should_notify('A'))
        self.assertFalse(rl.should_notify('A'))
        import time
        time.sleep(1.1)
        self.assertTrue(rl.should_notify('A'))

    def test_zero_window_always_notify(self):
        rl = eg.ExceptionRateLimiter(window_s=0)
        self.assertTrue(rl.should_notify('A'))
        self.assertTrue(rl.should_notify('A'))

    def test_reset(self):
        rl = eg.ExceptionRateLimiter(window_s=600)
        rl.should_notify('A')
        rl.reset()
        self.assertTrue(rl.should_notify('A'))

    def test_none_key(self):
        rl = eg.ExceptionRateLimiter(window_s=600)
        self.assertTrue(rl.should_notify(None))
        self.assertFalse(rl.should_notify(None))


class TestInstallUninstall(unittest.TestCase):
    def setUp(self):
        self._orig_sys = sys.excepthook
        self._notified = []
        try:
            import tkinter as tk
            self._orig_tk = tk.Tk.report_callback_exception
            self._has_tk = True
        except Exception:
            self._has_tk = False

    def tearDown(self):
        eg.uninstall(getattr(self, '_handles', None))
        sys.excepthook = self._orig_sys
        if self._has_tk:
            import tkinter as tk
            tk.Tk.report_callback_exception = self._orig_tk

    def test_install_replaces_hooks(self):
        self._handles = eg.install(self._notified.append)
        self.assertIsNot(sys.excepthook, self._orig_sys)
        if self._has_tk:
            import tkinter as tk
            self.assertIsNot(tk.Tk.report_callback_exception, self._orig_tk)

    def test_uninstall_restores(self):
        self._handles = eg.install(self._notified.append)
        eg.uninstall(self._handles)
        self.assertIs(sys.excepthook, self._orig_sys)
        if self._has_tk:
            import tkinter as tk
            self.assertIs(tk.Tk.report_callback_exception, self._orig_tk)

    def test_hook_notifies_and_limits(self):
        self._handles = eg.install(self._notified.append)
        # 手动触发 sys.excepthook（等价于子线程未捕获）
        sys.excepthook(ValueError, ValueError('boom-1'), None)
        sys.excepthook(ValueError, ValueError('boom-2'), None)
        self.assertEqual(len(self._notified), 1)
        self.assertIn('boom-1', self._notified[0])

    def test_hook_survives_notify_exception(self):
        # ui_notify 自身抛异常 → 钩子不传播
        def bad_notify(msg):
            raise RuntimeError('notify crashed')
        self._handles = eg.install(bad_notify)
        sys.excepthook(TypeError, TypeError('x'), None)  # 不应冒泡

    def test_uninstall_idempotent(self):
        eg.uninstall(None)
        eg.uninstall({})
        self._handles = eg.install(self._notified.append)
        eg.uninstall(self._handles)
        eg.uninstall(self._handles)

    def test_hook_logs_but_does_not_raise_with_none_tb(self):
        self._handles = eg.install(self._notified.append)
        sys.excepthook(RuntimeError, RuntimeError('tb-none'), None)


class TestModuleImport(unittest.TestCase):
    def test_module_constants(self):
        self.assertGreaterEqual(eg.DEFAULT_RATE_WINDOW_S, 60)


if __name__ == '__main__':
    unittest.main()