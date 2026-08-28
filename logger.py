"""
PDD EZ — 日志模块（借鉴 March7thAssistant utils/logger/logger.py）
按天分文件 + 保留天数自动清理 + 控制台彩色输出 + 标题分隔符。
用法：
    from logger import log
    log.info("...")
    log.hr("开始批量识别")

v1.4.8 P1-C：所有 Formatter 落盘前对 api_key / password / Authorization / Bearer 等
敏感字段脱敏（替换为 ***），防止日志文件被分享/上传时泄露凭据。
脱敏实现：from utils import _sanitize_for_log（utils.py 是任务允许改的文件之一），
这里通过延迟 import 避免 logger.py 在启动期就依赖 utils（utils 自身不依赖 logger，
但保持 logger 独立可移植仍然有价值）。
"""
import os
import sys
import logging
from datetime import datetime, timedelta


def _get_sanitizer():
    """延迟 import utils._sanitize_for_log——logger.py 顶层不依赖 utils。
    utils.py 不可用（极早期 import 阶段）时回退到 identity（绝不阻塞日志）。"""
    try:
        from utils import _sanitize_for_log
        return _sanitize_for_log
    except Exception:
        return lambda x: x


# ── 控制台彩色（Windows 10+ ANSI 支持）──
class _ColoredFormatter(logging.Formatter):
    """控制台彩色 Formatter：INFO 绿 / WARNING 黄 / ERROR 红 / CRITICAL 红粗"""
    COLORS = {
        'DEBUG': '\033[90m',     # 灰
        'INFO': '\033[92m',      # 绿
        'WARNING': '\033[93m',   # 黄
        'ERROR': '\033[91m',     # 红
        'CRITICAL': '\033[91;1m' # 红粗
    }
    RESET = '\033[0m'

    def format(self, record):
        prefix = self.COLORS.get(record.levelname, '')
        sanitize = _get_sanitizer()
        try:
            record.msg = sanitize(record.msg)
        except Exception:
            pass
        if record.args:
            try:
                record.args = tuple(sanitize(a) if isinstance(a, str) else a for a in record.args)
            except Exception:
                pass
        msg = super().format(record)
        return f"{prefix}{msg}{self.RESET}"


# ── 文件无色 Formatter（日志文件不带 ANSI 转义）──
class _PlainFormatter(logging.Formatter):
    def format(self, record):
        sanitize = _get_sanitizer()
        try:
            record.msg = sanitize(record.msg)
        except Exception:
            pass
        if record.args:
            try:
                record.args = tuple(sanitize(a) if isinstance(a, str) else a for a in record.args)
            except Exception:
                pass
        return super().format(record)


class Logger:
    """PDD EZ 日志管理：按天分文件 + 保留天数清理 + 控制台彩色 + hr 标题"""

    def __init__(self, name="PDD_EZ", level="INFO", retention_days=30,
                 log_dir=None, console=True):
        self._name = name
        self._level = level
        self._retention_days = retention_days
        # 日志目录：默认程序目录/logs；源码运行时可传自定义
        self._log_dir = log_dir or os.path.join(
            os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, 'frozen', False) else __file__)),
            'logs')
        self._console = console
        self._init_logger()
        self._cleanup_old_logs()

    # ── 初始化 ─────────────────────────────────────────────────────

    def _init_logger(self):
        self.logger = logging.getLogger(self._name)
        self.logger.propagate = False
        self.logger.setLevel(self._level)
        # 关闭并清掉旧 handler（防重复注册 + 文件句柄泄漏）
        for _h in list(self.logger.handlers):
            try:
                _h.close()
            except Exception:
                pass
        self.logger.handlers.clear()

        fmt = '%(asctime)s | %(levelname)s | %(message)s'

        # 控制台 handler（彩色）
        if self._console:
            ch = logging.StreamHandler()
            ch.setFormatter(_ColoredFormatter(fmt))
            self.logger.addHandler(ch)

        # 文件 handler（无色，按天）——目录不可写/只读时降级仅控制台，绝不让 import 崩主程序
        self._ensure_log_dir()
        try:
            fh = logging.FileHandler(
                os.path.join(self._log_dir, f"{datetime.now().strftime('%Y-%m-%d')}.log"),
                encoding="utf-8")
            fh.setFormatter(_PlainFormatter(fmt))
            self.logger.addHandler(fh)
        except Exception:
            pass  # 日志不可写：仅控制台，不影响程序

    def _ensure_log_dir(self):
        try:
            os.makedirs(self._log_dir, exist_ok=True)
        except Exception:
            pass

    # ── 保留天数清理（抄 March7th：mtime 早于截止时间即删，含目录遍历防护）──
    def _cleanup_old_logs(self):
        try:
            if not os.path.isdir(self._log_dir):
                return
            cutoff = datetime.now() - timedelta(days=self._retention_days)
            log_dir_abs = os.path.abspath(self._log_dir)
            for fn in os.listdir(self._log_dir):
                if not fn.endswith('.log'):
                    continue
                fp = os.path.join(self._log_dir, fn)
                # 目录遍历防护：验证路径仍在日志目录内
                if not os.path.abspath(fp).startswith(log_dir_abs):
                    continue
                if not os.path.isfile(fp):
                    continue
                try:
                    mtime = datetime.fromtimestamp(os.path.getmtime(fp))
                    if mtime < cutoff:
                        os.remove(fp)
                except Exception:
                    pass
        except Exception:
            pass

    # ── 级别方法 ───────────────────────────────────────────────────

    def debug(self, msg):
        self.logger.debug(msg)

    def info(self, msg):
        self.logger.info(msg)

    def warning(self, msg):
        self.logger.warning(msg)

    def error(self, msg):
        self.logger.error(msg)

    def critical(self, msg):
        self.logger.critical(msg)

    # ── 标题分隔（抄 March7th hr：level 0 方框 / 1 等号 / 2 减号）──
    def hr(self, title, level=0):
        """格式化标题并写入日志。level: 0 方框 / 1 等号 / 2 减号"""
        try:
            sep_len = 80
            lines = str(title).split('\n')
            if level == 0:
                sep = '+' + '-' * sep_len + '+'
                out_lines = []
                for line in lines:
                    tlen = self._custom_len(line)
                    left = (sep_len - tlen) // 2
                    right = sep_len - tlen - left
                    out_lines.append('|' + ' ' * left + line + ' ' * right + '|')
                formatted = sep + '\n' + '\n'.join(out_lines) + '\n' + sep
            elif level == 1:
                tlen = self._custom_len(title)
                left = (sep_len - tlen) // 2
                right = sep_len - tlen - left
                formatted = '=' * left + ' ' + title + ' ' + '=' * right
            else:
                tlen = self._custom_len(title)
                left = (sep_len - tlen) // 2
                right = sep_len - tlen - left
                formatted = '-' * left + ' ' + title + ' ' + '-' * right
            self.logger.info('\n' + formatted)
        except Exception:
            pass

    @staticmethod
    def _custom_len(text):
        """计算显示宽度（中文/全角占 2，防对齐错位）"""
        import unicodedata
        return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text)


# ── 全局单例（各模块直接 import log 使用）──
log = Logger()
