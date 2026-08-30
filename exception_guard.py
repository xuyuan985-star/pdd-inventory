"""
PDD EZ — 全局未捕获异常守卫

痛点：此前未捕获异常（Tk 回调 / 子线程）直接裸 traceback 或静默吞掉——
DESIGN §4「失败显式」在异常路径上没有统一收口。

本模块做三件事：
1. 覆写 tk.Tk.report_callback_exception（主线程回调异常）与 sys.excepthook
   （子线程/非 Tk 未捕获异常）——完整 traceback 进日志（脱敏），主线程侧
   收到一条友好摘要（复用 ocr_review 分类文案，兜底 str）。
2. 限流：同一种异常类 10 分钟内只提示一次，防弹窗风暴（批量识别中偶发
   异常不会刷屏）。
3. 钩子自身绝不再抛（任何异常仅 traceback.print_exception），杜绝递归风暴。

纯逻辑可单测（不依赖 Tk/窗口）：summary_for_exception / ExceptionRateLimiter /
install / uninstall。GUI 接线见 gui.py（install 时把 ui_notify 用 win.after
包回主线程，worker 线程不直调 Tk）。
"""
import sys
import time
import traceback


DEFAULT_RATE_WINDOW_S = 600
"""同类异常限流窗口（秒）：窗口期内只弹一次提示。"""


class ExceptionRateLimiter:
    """按异常类名限流的纯逻辑：window_s 内同类异常只放行一次。"""

    def __init__(self, window_s=DEFAULT_RATE_WINDOW_S):
        self.window_s = max(0, int(window_s))
        self._first_ts = {}

    def should_notify(self, exc_cls_name):
        """返回 True 表示本次应提示（并记录时间）；同一类在窗口期内只 True 一次。"""
        try:
            key = str(exc_cls_name) or 'Unknown'
        except Exception:
            key = 'Unknown'
        now = time.time()
        ts = self._first_ts.get(key)
        if ts is None or now - ts >= self.window_s:
            self._first_ts[key] = now
            return True
        return False

    def reset(self):
        """清空所有限流记录（如恢复备份/设置变更后可调用）。"""
        try:
            self._first_ts.clear()
        except Exception:
            pass


def summary_for_exception(exc) -> str:
    """把异常转成用户可读的友好摘要（长度受限、日志脱敏）。

    优先复用 ocr_review 的分类文案（user_msg）；取不到就回退
    'ClassName: str(exc)[:120]'，最后兜底固定文案——任何分支都不抛。
    """
    # 1) 复用 OCR 容错分类文案（v1.5.3 ocr_review.categorize_error
    # → (category, user_msg, title) 三元组；仅取分类命中的 user_msg）
    try:
        from ocr_review import categorize_error
        _cat, _user_msg, _title = categorize_error(exc)
        if _cat != 'unknown' and _user_msg:
            return _user_msg
    except Exception:
        pass
    # 2) 回退：类名 + 脱敏摘要
    try:
        from utils import _sanitize_for_log
        _msg = _sanitize_for_log(str(exc) or '') or exc.__class__.__name__
        return f'{exc.__class__.__name__}: {_msg[:120]}'
    except Exception:
        pass
    # 3) 终极兜底
    return '程序发生未预期异常（详见日志）'


def _log_traceback(tag, exc, val, tb):
    """完整 traceback 进日志（logger.log 全局单例），失败回退 print。"""
    text = ''.join(traceback.format_exception(exc, val, tb))
    try:
        from logger import log
        log.error(f'[{tag}] {text}')
        return
    except Exception:
        pass
    try:
        traceback.print_exception(exc, val, tb)
    except Exception:
        pass


def install(ui_notify, limiter=None):
    """安装守卫；返回 handles 供 uninstall 还原。

    ui_notify: callable(summary:str)——【必须】由调用方保证线程安全
      （gui 里用 win.after 包回主线程；本模块不碰任何 Tk）。
    limiter: 可注入 ExceptionRateLimiter（测试用）。
    钩子内部对任何意外只 traceback 一次，永不冒泡。
    """
    limiter = limiter if limiter is not None else ExceptionRateLimiter()
    handles = {'old_tk': None, 'old_sys': None, 'limiter': limiter}

    # ── Tk 回调异常（主线程 report_callback_exception）──
    try:
        import tkinter as tk
        handles['old_tk'] = tk.Tk.report_callback_exception

        def _tk_hook(root, exc, val, tb):
            try:
                _log_traceback('Tk回调异常', exc, val, tb)
            except Exception:
                pass
            try:
                _wrap_notify(ui_notify, limiter, val, tb)
            except Exception:
                pass

        tk.Tk.report_callback_exception = _tk_hook
    except Exception:
        handles['old_tk'] = None

    # ── sys.excepthook（子线程/非 Tk 未捕获异常）──
    try:
        handles['old_sys'] = sys.excepthook

        def _sys_hook(exc, val, tb):
            try:
                _log_traceback('线程未捕获异常', exc, val, tb)
            except Exception:
                pass
            try:
                _wrap_notify(ui_notify, limiter, val, tb)
            except Exception:
                pass

        sys.excepthook = _sys_hook
    except Exception:
        handles['old_sys'] = None

    return handles


def _wrap_notify(ui_notify, limiter, val, tb):
    """限流 + 通知；ui_notify 自身异常被吞（防钩子递归）。"""
    try:
        _cls = type(val).__name__ if val is not None else 'Unknown'
        if not limiter.should_notify(_cls):
            return
        try:
            summary = summary_for_exception(val)
        except Exception:
            summary = '程序发生未预期异常（详见日志）'
        ui_notify(summary)
    except Exception:
        pass


def uninstall(handles):
    """还原 Tk 回调钩子与 sys.excepthook（幂等；失败静默）。"""
    if not handles:
        return
    try:
        if handles.get('old_tk') is not None:
            import tkinter as tk
            tk.Tk.report_callback_exception = handles['old_tk']
    except Exception:
        pass
    try:
        if handles.get('old_sys') is not None:
            sys.excepthook = handles['old_sys']
    except Exception:
        pass