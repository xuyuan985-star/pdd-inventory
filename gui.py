"""
PDD EZ — 补货排期助手
客户看后台页面，输入库存和预估销量，自动算补货时间
"""

import os, sys, threading, time
import re  # R1 布局B：批量进度阶段前缀解析（模块级预编译正则供纯函数复用）
from datetime import datetime

from utils import get_base_dir, get_api_config, VERSION, version_newer
from settings_ui import SettingsUIMixin
from stats_ui import StatsPagesMixin
from logger import log
import async_queue  # 全局任务队列
import store_ui_logic  # 店铺切换/入库组装纯逻辑（无 Tk 依赖，test_store_ui_logic 可单测）

# 多店铺隔离：店铺清单权威（store_registry， 产出）。
# 守护式导入同 history_db 模式：极端缺文件场景店铺功能降级为单机默认店铺，主程序不受影响。
try:
    import store_registry
except Exception:
    store_registry = None

# R1 流程效率：导入映射记忆——读写走 import_memory（gui 只在导入
# 对话框读上次映射/存回本次确认映射，清除入口在设置页 settings_ui）。
# 守护式导入：增量包缺文件时映射记忆整体降级为不可用（每次导入走 guess_mapping），
# 主程序不受影响（同 store_registry/history_db 模式）。
try:
    import import_memory
except Exception:
    import_memory = None

# v1.4.8 P1-A：EULA 文本常量（docs/EULA.md 的代码内嵌版，打包后无需读 docs/）
from eula_text import EULA_VERSION, EULA_TITLE, render_eula_text
# F2 清理：self._tier 死状态删除后，_auth_get_tier 不再有调用方，一并删除 import
# （未来若需要 get_tier 预热，按需重新 import）

# v1.4.7 WS-A：本地历史库（识别数据累积/趋势查询）。
# 守护式导入：增量包缺文件等极端场景历史功能整体降级停用，主程序不受影响。
try:
    import history_db
except Exception:
    history_db = None

# ── 动效基建（模块级，纯函数 + 总开关） ──
# ANIMATIONS_ENABLED 总开关：低配机/无障碍偏好/UI 性能问题改 False 全关（无需改业务代码）
ANIMATIONS_ENABLED = True


def _lerp_hex(a, b, t):
    """t9 基建：hex 颜色线性插值（纯函数，可单测）。t ∈ [0,1]，返回 '#RRGGBB'。

    容错：a/b 不是  # RRGGBB 6 位 hex 时返回 b（降级到终态）。
    """
    try:
        if not (isinstance(a, str) and isinstance(b, str)):
            return b
        if len(a) != 7 or len(b) != 7 or a[0] != '#' or b[0] != '#':
            return b
        ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
        br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
        t = max(0.0, min(1.0, float(t)))
        r = int(ar + (br - ar) * t)
        g = int(ag + (bg - ag) * t)
        bl = int(ab + (bb - ab) * t)
        return f'#{r:02X}{g:02X}{bl:02X}'
    except Exception:
        return b


def _cancel_after_jobs(win, job_ids):
    """t9 基建：取消 win.after 句柄列表，吞 TclError（widget 已销毁常见）。"""
    for jid in (job_ids or []):
        try:
            if jid:
                win.after_cancel(jid)
        except Exception:
            pass


def _meltdown_animations():
    """t11 ⑤修：动效熔断——异常时翻 ANIMATIONS_ENABLED=False（模块级），后续所有动效早 return。

    v4f 评审：原 except:pass 是注释谎言（无任何地方写 False）——动效持续异常时会卡死。
    本函数被任何动效回调的 except 分支调用，once-per-process 翻转，确保主流程零影响。
    """
    global ANIMATIONS_ENABLED
    if ANIMATIONS_ENABLED:
        ANIMATIONS_ENABLED = False
        try:
            from logger import log
            log.warn('[t11 ⑤] 动效回调连续异常，已熔断——后续动效全部跳过（业务零影响）')
        except Exception:
            pass

# ── 抢先设置 DPI 感知，防止 pyautogui 截图后窗口缩放 ──
if sys.platform == 'win32':
    import ctypes
    try:
        # Per-Monitor DPI V2 — Windows 10 1607+
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            # Fallback: 传统 SetProcessDPIAware
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

sys.path.insert(0, get_base_dir())

try:
    import tkinter as tk
    import tkinter.font as tkfont  # 按钮宽度精确测量（DPI/中文/emoji 自适应）
    from tkinter import messagebox, ttk
except ImportError:
    print("tkinter 未安装（Python 自带），请检查 Python 安装")
    sys.exit(1)


from config import THEMES, load_theme_pref, _merge_theme


def _validate_num_entry(p) -> bool:
    """数字输入校验：空串或纯数字（含整数/小数）通过，非法字符拒绝"""
    if p is None:
        return True
    s = str(p).strip()
    if not s:
        return True
    try:
        float(s)
        return True
    except ValueError:
        return False


def _strip_name_decor(name: str) -> str:
    """剥离 _fill_from_ocr 写入显示名时的装饰：
    '⚠示例商品A [示例仓库]' → '示例商品A'。
    ⚠ 为低置信前缀；[xxx] 为分仓库显示后缀。两者都可能进入 rows 供编辑/重算回读。"""
    if not name:
        return name
    s = str(name)
    # 剥离 ⚠ 前缀（可能有多个，如 ⚠⚠）
    while s.startswith('⚠'):
        s = s[1:]
    # 剥离尾部 [仓库名] 后缀（仓库名不含方括号；[xxx] 中 xxx 也可能带空格）
    import re as _re
    m = _re.search(r'\s*\[[^\]]*\]\s*$', s)
    if m:
        s = s[:m.start()].rstrip()
    return s


class _CanvasBtn:
    """Canvas 自绘切角按钮（终末地机能风）：几何微切角、完全扁平、无渐变无浮雕。
    模拟 tk.Button 的 config/configure/cget/pack/destroy 接口，兼容现有调用。"""

    def __init__(self, canvas, poly, text_item, text, command, kind, colors, owner=None):
        self.canvas = canvas
        self.poly = poly
        self.text_item = text_item
        self._text = text
        self._command = command
        self._kind = kind
        self._colors = colors
        self._state = 'normal'
        self.owner = owner

    def retheme(self):
        """主题切换时从 owner.tc() 重取配色并重绘"""
        if self.owner is None or not self._kind:
            return
        c = self.owner.tc(f'btn.{self._kind}', None)
        if c and isinstance(c, dict) and 'bg' in c:
            self._colors = c
            self._apply()

    def pack(self, *a, **kw):
        return self.canvas.pack(*a, **kw)

    def pack_configure(self, *a, **kw):
        return self.canvas.pack_configure(*a, **kw)

    def pack_forget(self, *a, **kw):
        return self.canvas.pack_forget(*a, **kw)

    def config(self, **kw):
        if 'state' in kw:
            self._state = kw.pop('state')
            self._apply()
        if 'text' in kw:
            self._text = kw.pop('text')
            self.canvas.itemconfigure(self.text_item, text=self._text)
        if 'command' in kw:
            self._command = kw.pop('command')
        if 'bg' in kw:
            self._colors['bg'] = kw.pop('bg')
            self._apply()
        if 'fg' in kw:
            self._colors['fg'] = kw.pop('fg')
            self._apply()
        if 'edge' in kw:
            self._colors['edge'] = kw.pop('edge')
        if kw:
            self.canvas.configure(**kw)
        self._apply()
        return self

    def configure(self, **kw):
        self.config(**kw)
        return self

    def cget(self, key):
        if key == 'state':
            return self._state
        if key == 'text':
            return self._text
        try:
            return self.canvas.cget(key)
        except Exception:
            return None

    def destroy(self):
        try:
            self.canvas.destroy()
        except Exception:
            pass

    def _apply(self):
        c = self._colors or {}
        if self._state == 'disabled':
            d = self.owner.tc('btn.disabled', {}) if self.owner else {}
            if self.poly is not None:
                self.canvas.itemconfigure(self.poly, fill=d.get('bg', '#E8E8E3'), outline=d.get('edge', '#C9C9C2'))
            if self.text_item is not None:
                self.canvas.itemconfigure(self.text_item, fill=d.get('fg', '#9E9E9E'))
        else:
            if self.poly is not None:
                self.canvas.itemconfigure(self.poly, fill=c.get('bg', '#FFE600'), outline=c.get('edge', c.get('bg', '#111111')))
            if self.text_item is not None:
                self.canvas.itemconfigure(self.text_item, fill=c.get('fg', '#111111'))

    def _click(self, e):
        if self._state != 'disabled' and self._command:
            # 防重入：Canvas 按钮同时绑了 item 级 tag_bind + widget 级 bind，
            # 一次点击会触发 2~3 次；三层防护
            # 1) 250ms 时间戳（常规快速连点）
            # 2) _executing 执行中标志（模态循环期间事件仍可派发的场景）
            # 3) 延迟复位 _executing：原生模态对话框（askopenfilename 等）会
            # 阻塞主线程，第二个排队事件在对话框关闭、_command 返回之后
            # 才派发——立即复位拦不住，改为 after(300ms) 复位
            now = time.time()
            if getattr(self, '_executing', False):
                return
            if now - getattr(self, '_last_click_ts', 0.0) < 0.25:
                return
            self._last_click_ts = now
            self._executing = True
            try:
                self._command()
            finally:
                # 延迟复位：捕获对话框关闭后立即派发的第二个事件（300ms 足够）
                # canvas 可能随命令销毁（如对话框内按钮），after 需容错
                try:
                    self.canvas.after(300, lambda: setattr(self, '_executing', False))
                except Exception:
                    self._executing = False

    def _hover(self, e):
        if self._state == 'disabled':
            return
        # A：按钮 hover 5 步插值（v4f：每按钮独立 after job 句柄，disabled 态抢先落终态）
        if ANIMATIONS_ENABLED and self.poly is not None and self.owner is not None and hasattr(self.owner, '_animate_btn_hover'):
            try:
                self.owner._animate_btn_hover(self, enter=True)
                return
            except Exception:
                pass
        # 降级：直接跳终态（兼容 ANIMATIONS_ENABLED=False 或 owner 异常）
        c = self._colors
        hov = c.get('bg_hover')
        if hov and self.poly is not None:
            self.canvas.itemconfigure(self.poly, fill=hov)
        # 文字按钮：hover 下划线略粗
        if getattr(self, 'underline_item', None) is not None:
            try:
                coords = self.canvas.coords(self.underline_item)
                h = getattr(self, '_btn_h', 26)
                if len(coords) == 4:
                    self.canvas.coords(self.underline_item, coords[0], h - 4, coords[2], h - 1)
            except Exception:
                pass

    def _leave(self, e):
        # A：leave 同样走 5 步插值回到 bg
        if ANIMATIONS_ENABLED and self.poly is not None and self.owner is not None and hasattr(self.owner, '_animate_btn_hover'):
            try:
                self.owner._animate_btn_hover(self, enter=False)
                return
            except Exception:
                pass
        # 降级
        self._apply()


# P3-B：模块级 helper（必须在 class App 之前定义，否则在类内引用失败）
def _to_float_safe(v, default=0.0):
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _fmt_yuan(v: float) -> str:
    try:
        if v >= 1.0:
            return f"¥{v:.2f}"
        return f"¥{v:.4f}".rstrip('0').rstrip('.')
    except Exception:
        return "¥0"


# ══ R1 布局优化B：布局相关纯逻辑（进度文案映射 / 列宽自适应 / 单元格省略 /
# 模型标签 / 按钮状态表 / 复核弹窗列规格）——抽为模块级函数供
# test_home_layout.py 抽测，GUI 各处只消费其结果 ══

# 批量 dlog 阶段编号上限（1 定位下拉框 / 2 粘贴省份 / 3 回车确认 / 4 点查询 /
# 5 等刷新 / 6 识别+滚动——阶段 6 最耗时，见 _run_batch_sequence 的 dlog 调用）
_BATCH_STAGE_MAX = 6
_BATCH_STAGE6_SHARE = 0.5  # 阶段 6 占单地区进度的一半（识别+滚动轮次不可预估）

# 商品名列显示字符预算：260px 列宽 ÷ 9pt 微软雅黑中文字宽（≈12px）≈ 20 字
_NAME_ELIDE_LIMIT = 20

# R1 布局优化B：批量忙时段受控按钮状态表 (self 属性名, 原文案兜底, busy 语义 key)。
# 文案映射走 home_actions.busy_label_for（可单测）；兜底仅在 cget('text') 异常时使用
# （live_btn 实际文案是「截图」，v1.4.5 起按钮文字压缩 2 字）。
# v1.5.6：img_batch_btn 已并入 live_btn（截图主入口菜单化，见 _open_shot_menu）。
# v1.5.7：布局定版 批量｜导入｜识图｜导出 / 刷新｜双模型——「识图」= _live_screenshot
# （截当前窗口），live_btn 变量名沿用防引用漂移。
# v1.5.8（BUG_HUNT_V157 A2）：import_btn 纳入忙禁表——批量期禁用+「导入中…」，
# 防忙态误点（其图片路径与批量共用队列语义；home_actions 'import' key 消费）。
_BATCH_BUSY_BTNS = (
    ('export_btn', '导出', 'export'),
    ('live_btn', '识图', 'image'),
    ('import_btn', '导入', 'import'),
)

# v1.5.9：识别前置 API 配置检查（用户反馈「点识别报错但没说清是 API 问题」）。
# 纯函数可单测：读 settings 的 active provider，返回缺失项清单（绝不抛）。
_PROVIDER_DISPLAY = {'doubao': '火山方舟/豆包', 'qwen': '阿里百炼', 'glm': '智谱'}


def api_config_status() -> dict:
    """识别前置 API 配置检查：{'ok': bool, 'provider': str, 'missing': [str,...]}。

    missing 元素 ∈ {'api_key','model'}——api_key 空（含 dpapi 解密后空）或
    model 为空白即视为缺失。配置读取失败 → ok=False + 双缺失（防识别静默失败）。
    v1.5.11：附 secondary 诊断（不参与 ok 判定——副模型问题只是提示级）：
    secondary: 副模型名；sec_issue: 'ocr_only'|'same_as_main'|'none'|None；sec_hint: 建议文案。
    """
    sec = ''
    sec_issue = None
    sec_hint = ''
    try:
        from utils import get_api_config, decrypt_secret, get_secondary_model
        cfg = get_api_config()
        active = str((cfg.get('active_provider') or 'doubao') or '')
        provs = (cfg.get('providers') or {}) if isinstance(cfg.get('providers'), dict) else {}
        p = provs.get(active) or {}
        if not isinstance(p, dict):
            p = {}
    except Exception:
        return {'ok': False, 'provider': 'unknown', 'missing': ['api_key', 'model'],
                'secondary': sec, 'sec_issue': sec_issue, 'sec_hint': sec_hint}
    missing = []
    try:
        k = p.get('api_key') or ''
        if k.startswith('dpapi:v1:'):
            k = decrypt_secret(k) or ''
        if not k:
            missing.append('api_key')
    except Exception:
        missing.append('api_key')
    if not str(p.get('model') or '').strip():
        missing.append('model')
    # 副模型诊断（不参与 ok 判定——副模型问题只是提示级）
    try:
        from ocr import _is_qwen_ocr
        sec = str(get_secondary_model() or '').strip()
        if sec:
            if _is_qwen_ocr(sec):
                sec_issue = 'ocr_only'
                sec_hint = ('副模型为 OCR 专用（文字/数字复核型），双模型表格交叉验证自动跳过'
                            '——想启用请把主副模型都换为 VL 通用视觉模型')
            elif str(p.get('model') or '').strip().lower() == sec.lower():
                sec_issue = 'same_as_main'
                sec_hint = '主副模型相同，双模型验证无意义——请配置不同的副模型'
        else:
            sec_issue = 'none'
    except Exception:
        sec_issue = None
    return {'ok': not missing, 'provider': active, 'missing': missing,
            'secondary': sec, 'sec_issue': sec_issue, 'sec_hint': sec_hint}

# R1 布局优化B：复核弹窗列规格 (cid, 标题, 初始宽, 最小宽, 随窗拉伸)——
# 商品名/异常原因拉伸跟随窗口加宽，字段/原文/解析值定宽；
# 结构供 _show_review_dialog 与 test_home_layout.py 共用
REVIEW_COLS = (
    ('name', '商品名', 200, 120, True),
    ('field', '字段', 70, 60, False),
    ('reason', '异常原因', 260, 140, True),
    ('raw', '原文', 150, 90, False),
    ('parsed', '解析值', 80, 60, False),
)

# 阶段前缀正则：进度百分比取数字（"3.回车确认" → 3）；短标签去前缀（含全角顿号）
_STAGE_DIGIT_RE = re.compile(r'^(\d+)')
_STAGE_PREFIX_RE = re.compile(r'^\d+[.、．]\s*')
# R2 批次C：批量地区标题行（_run_batch_sequence 的 dlog(f"── [{label}] ({i+1}/{total}) ──")）
_BATCH_REGION_HDR_RE = re.compile(r'──\s*\[(.+?)\]\s*\((\d+)\s*/\s*(\d+)\)')


def batch_region_header(stage):
    """批量 dlog 地区标题行 → (地区名, 序号, 总数)（纯函数；非标题行返回 None）。

    标题行形如「── [广东] (2/5) ──」，是「上一地区已完成、新地区开始」的天然
    分界——状态栏进度文案借此做「完成 N/M 地区 ▶ 开始：地区」联动。
    """
    m = _BATCH_REGION_HDR_RE.search(str(stage or ''))
    if not m:
        return None
    try:
        return (m.group(1), int(m.group(2)), int(m.group(3)))
    except (ValueError, IndexError):
        return None


def review_edit_btn_state(has_selection):
    """复核弹窗「修正选中行」按钮状态（纯函数）：有选中行 → normal，否则 disabled。"""
    return 'normal' if has_selection else 'disabled'


def batch_stage_percent(msg, region_idx=0, region_total=1, last=0):
    """批量 dlog 文案 → 0-99 进度百分比（纯函数，_run_batch_sequence.dlog 消费）。

    规则：
    - 带数字阶段前缀（如 "3.回车确认"）：百分比 = (地区序 + 阶段占比) / 地区总数 × 100。
      阶段 1-5 各占单地区 10%，阶段 6（识别+滚动，最耗时）占 50%——多地区批量全程
      单调递增，修复旧 stage_num*10 在每个地区从 10% 跳回的问题。
    - 无阶段前缀（如 "AI 自动定位页面元素..."）：沿用上次百分比（last），只刷新阶段文案。
    - 结果钳制 0-99（100% 留给批量完成态，由 _finish_batch 收尾）。
    """
    try:
        m = _STAGE_DIGIT_RE.match(str(msg or ''))
        if not m:
            return max(0, min(99, int(last)))
        stage = int(m.group(1))
        frac = (_BATCH_STAGE6_SHARE if stage >= _BATCH_STAGE_MAX
                else min(max(stage - 1, 0), _BATCH_STAGE_MAX - 1) / 10.0)
        total = max(1, int(region_total))
        idx = max(0, int(region_idx))
        return max(0, min(99, int((idx + frac) / total * 100)))
    except Exception:
        try:
            return max(0, min(99, int(last)))
        except Exception:
            return 0


def progress_stage_label(stage, limit=28):
    """批量阶段文案 → 状态栏短标签（纯函数）：去「3.」序号前缀、去尾省略号，
    超长截断加「…」；空值兜底「处理中」。"""
    s = str(stage or '').strip()
    if not s:
        return '处理中'
    m = _STAGE_PREFIX_RE.match(s)
    if m:
        s = s[m.end():]
    s = s.rstrip('.…。 ')
    if len(s) > limit:
        s = s[:limit] + '…'
    return s or '处理中'


def progress_status_text(percent, stage, region=''):
    """批量进度 → 状态栏单行文案（纯函数）。

    - 地区标题行（── [广东] (2/5) ──）→ 完成联动文案：
      首个地区「▶ 开始 1/5 地区：广东」；后续「✓ 已完成 1/5 地区 ▶ 开始 2/5：广东」
    - 普通阶段行 →「⏳ 批量识别 N%｜地区 · 阶段短语」（region 空或等于地区名占位时
      省略地区段）；百分比 ≤0/非法时省略百分比段。
    """
    hdr = batch_region_header(stage)
    if hdr:
        name, idx, total = hdr
        if idx > 1:
            return f"✓ 已完成 {idx - 1}/{max(total, idx)} 地区 ▶ 开始 {idx}/{total}：{name}"
        return f"▶ 开始 {idx}/{max(total, idx)} 地区：{name}"
    label = progress_stage_label(stage)
    try:
        pct = int(percent)
    except (TypeError, ValueError):
        pct = 0
    loc = f"{region} · " if str(region or '').strip() else ''
    if pct > 0:
        return f"⏳ 批量识别 {pct}%｜{loc}{label}"
    return f"⏳ 批量识别中｜{loc}{label}"


def tree_col_width(col):
    """识别结果表列宽自适应规则（纯函数，_render_tree 消费）：
    「预警」140（滞销⚠/超卖🔥 多标签不截断）、名称类列 260（含「商品/名称」字样）、
    「模型」100、「预测」90（R2：数值短，1 位小数可完整显示）、其余数字/状态列 110。"""
    c = str(col or '')
    if c == '预警':
        return 140
    if c == '模型':
        return 100
    if c == '预测':
        return 90
    if c in ('商品信息', '商品名称', '商品') or '名称' in c or '商品' in c:
        return 260
    return 110


def elide_cell(text, limit=None):
    """超长单元格文本显示省略：前 N 字符 + 「…」（纯函数）。

    仅作用于显示层：双击编辑 overlay 从 rows 取原值回写（gui._tree_edit_cell），
    Excel 导出用完整 name——显示截断不进任何数据流。
    """
    if limit is None:
        limit = _NAME_ELIDE_LIMIT
    s = str(text or '')
    if len(s) <= limit:
        return s
    return s[:max(1, int(limit))] + '…'


_MODEL_LABELS = {
    'classic': '经典',
    'weighted': '加权',
    'advanced': '高级',
}


def model_display_label(tag):
    """补货模型标注 → 表格短标签（纯函数）。

    plans['model'] 取值：classic / weighted / advanced / classic(no_history) /
    classic(error)（见 _calc_from_items）；Excel 导出仍用原始标注（export_xlsx），
    表格列宽 100px 下中文短标签可完整显示。
    """
    s = str(tag or '').strip()
    if s in _MODEL_LABELS:
        return _MODEL_LABELS[s]
    if s.startswith('classic(no_history'):
        return '经典·无历史'
    if s.startswith('classic(error'):
        return '经典·异常'
    return s


# ── R2 预测升级：纯逻辑函数（预测列文案 / 推荐简报文案）──
# 均无 Tk 依赖，test_forecast_gui.py 可单测。数据来源契约
# forecast_next_period(history_rows, alpha=0.5) -> float | None（SES 下一期日销）
# recommend_safety_days(history_rows, lead_days, z=1.65) -> int | None（数据不足不强给）


def forecast_cell_text(value):
    """plan['forecast'] → 结果表「预测」列文案（纯函数）。

    None / 非数值 / NaN → '—'（无历史或样本 <2 天，t5 返 None，不编数——§4）；
    数值 round 1 位并去掉尾 .0（12.0 → '12'，12.5 → '12.5'）。
    """
    try:
        if value is None:
            return '—'
        f = float(value)
    except (TypeError, ValueError):
        return '—'
    if f != f:  # NaN
        return '—'
    r = round(f, 1)
    if r == int(r):
        return str(int(r))
    return str(r)


def safety_brief_text(days):
    """安全库存推荐 → 状态栏简报文案（纯函数）。

    None / ≤0 / 非法 → ''（无推荐不加简报，状态栏保持原样）；
    有效 → '安全库存建议：N 天（基于近30天波动）'。
    """
    try:
        n = int(days)
    except (TypeError, ValueError):
        return ''
    if n <= 0:
        return ''
    return f'安全库存建议：{n} 天（基于近30天波动）'


def forecast_note_text(value):
    """单品历史趋势弹窗的预测标注文案（纯函数）。

    None / 非数值 → '历史数据不足（暂无预测日销）'（提示文字，§4 显式）；
    有效 → '预测日销 ≈ X'（X 格式同 forecast_cell_text）。
    """
    t = forecast_cell_text(value)
    if t == '—':
        return '历史数据不足（暂无预测日销）'
    return f'预测日销 ≈ {t}'


def busy_btn_text(orig_text):
    """批量忙时段按钮文案（纯函数）：原文案 + 「中…」（导出→导出中…；截图→截图中…）。"""
    t = str(orig_text or '').strip()
    return f'{t}中…' if t else '处理中…'


# ── R1 流程效率：纯逻辑函数（窗口记忆越界保护 / 导入映射预填对位 / 批量图片进度文案）──
# 均无 Tk 依赖，test_workflow_memory.py 可单测。

_GEOMETRY_RE = re.compile(r'^(\d+)x(\d+)([+-]\d+)([+-]\d+)$')
_GEOMETRY_SIZE_ONLY_RE = re.compile(r'^(\d+)x(\d+)$')


def clamp_geometry(geo, screen_w, screen_h):
    """窗口 geometry 越界保护（纯函数，R1 窗口状态记忆消费）。

    解析 Tk geometry 串 'WxH±X±Y'（或无位置的 'WxH'），以下任一命中即拒：
      - 宽或高 > 屏幕宽/高（换显示器 / DPI 变更后旧尺寸溢出）
      - X 或 Y 为负（窗口拖出左/上边缘）
      - X ≥ 屏幕宽 或 Y ≥ 屏幕高（窗口整体落在屏幕外）
      - 宽/高为 0、解析失败、屏幕尺寸非法
    拒 → 返回 None，调用方回落默认居中 geometry；合法 → 原样返回。
    不做部分钳制：贴边/部分出屏是用户的真实布局，原样保留。
    """
    try:
        sw, sh = int(screen_w), int(screen_h)
    except Exception:
        return None
    if sw <= 0 or sh <= 0:
        return None
    if not isinstance(geo, str):
        return None
    g = geo.strip()
    if not g:
        return None
    m = _GEOMETRY_RE.match(g)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        x, y = int(m.group(3)), int(m.group(4))
        if x < 0 or y < 0 or x >= sw or y >= sh:
            return None
    else:
        m2 = _GEOMETRY_SIZE_ONLY_RE.match(g)
        if not m2:
            return None
        w, h = int(m2.group(1)), int(m2.group(2))
    if w <= 0 or h <= 0 or w > sw or h > sh:
        return None
    return g


def resolve_last_mapping(headers, mapping):
    """上次导入映射 → 预填用 {field: 实际表头}（纯函数，导入映射预览对话框消费）。

    逐字段把 mapping 的目标列名与文件表头做 normalize 后精确对位（用同一份
    ocr.normalize_col_name 归一化规则，与 t2 import_memory.last_mapping_matches
    同源）；对上的字段返回表头原文（readonly 下拉 values 里的串，预填才能命中），
    对不上的字段不进结果（该下拉保持 guess_mapping 默认）。
    「上次映射整体是否可用」由 import_memory.last_mapping_matches 判定（核心
    name/stock/sales 全命中才预填），本函数只做机械对位——空输入返回 {}。
    """
    out = {}
    try:
        if not isinstance(mapping, dict) or not mapping:
            return out
        from ocr import normalize_col_name as _ncn
        norm_headers = {}
        for h in (headers or []):
            if isinstance(h, str) and h:
                norm_headers.setdefault(_ncn(h), h)
        if not norm_headers:
            return out
        for fid, col in mapping.items():
            if isinstance(fid, str) and fid and isinstance(col, str) and col:
                hit = norm_headers.get(_ncn(col))
                if hit is not None:
                    out[fid] = hit
    except Exception:
        return {}
    return out


def batch_images_progress_text(i, n):
    """批量图片识别进度 → 状态栏文案（纯函数）。

    i 为 1-based「第几张」、n 总张数：'批量图片识别 第 2/5 张…'；
    i 越界钳制到 [1, n]；n 非法（≤0 / 非数字）兜底 '批量图片识别 准备中…'。
    """
    try:
        n = int(n)
    except Exception:
        return '批量图片识别 准备中…'
    if n <= 0:
        return '批量图片识别 准备中…'
    try:
        i = int(i)
    except Exception:
        i = 1
    i = max(1, min(i, n))
    return f'批量图片识别 第 {i}/{n} 张…'


class App(SettingsUIMixin, StatsPagesMixin):
    # Design system — New Minimalism / Flat Design
    C_PRIMARY = '#111111'  # 近黑（主标题/文字）
    C_SECONDARY = '#333333'  # 深灰（次级文字）
    C_ACCENT = '#FFE600'  # 亮柠檬黄（accent / 高亮块）
    C_BG = '#FFFFFF'  # 纯白背景
    C_SURFACE = '#F7F7F2'  # 米白浅灰（卡片/底纹）
    C_TEXT = '#222222'  # 深灰正文（避免死黑）
    C_MUTED = '#6B6B6B'  # 中灰
    C_BORDER = '#EAEAEA'  # 浅灰细分割线（容器不画黑框）
    C_RED = '#DC2626'
    C_BTN_BLUE = '#1E88E5'  # 主操作按钮（亮蓝实心）
    C_CARD_HDR = '#1F1F1F'  # 卡片标题栏（深炭灰）
    C_YELLOW_BG = '#FFE600'  # 亮柠檬黄高亮块（配黑字）
    C_GREEN_BG = '#E8F5E9'
    C_RED_BG = '#FFEBEE'
    C_BLUE_LIGHT = '#FFF3B0'  # 浅黄（导航按钮/标签底，机能风）
    FONT = ('Microsoft YaHei UI', 9)
    FONT_BOLD = ('Microsoft YaHei UI', 9, 'bold')
    FONT_TITLE = ('Microsoft YaHei UI', 14, 'bold')
    FONT_HEADING = ('Microsoft YaHei UI', 11, 'bold')

    def _mk_btn(self, parent, text, command=None, kind='primary', font=None,
                width=None, height=None, padx=10, pady=3, pack_side=None,
                pack_padx=None, pack_pady=None, **pack_kw):
        """终末地机能风切角按钮（Canvas 自绘，完全扁平无渐变）：
        kind='primary' → 亮黄实心黑字细黑描边（一级主按钮）
        kind='dark'    → 炭黑底白字（二级功能按钮）
        kind='ghost'   → 白底黑字细黑描边（幽灵次要按钮）
        kind='text'    → 黑字 + 底部细黄下划线（文字型操作）
        kind='tag'     → 亮黄底黑粗字（角标标签）
        R1 布局B：pack_padx/pack_pady 只作用于 pack 布局——此前 padx 同时兼任
        按钮宽度语义（w = 文本宽 + padx*2 + 22），无法表达「按钮之间留白」；
        主页按钮行现用 pack_padx=(0, 8) 统一 8px 按钮间距。
        返回 _CanvasBtn（模拟 Button 接口）。"""
        colors = self.tc(f'btn.{kind}', {})
        # 高度按字体实际行高（metrics.linespace）换算，替代硬编码 24px/行——
        # 高 DPI/大字体下文字渲染变高，固定高度会导致文字顶框挤框
        _fnt = tkfont.Font(font=font or (self.FONT_BOLD if kind in ('tag',) else self.FONT))
        _line_h = _fnt.metrics('linespace')
        h = (height or 1) * _line_h + 8  # 行高 + 上下 padding
        # 文字实际像素宽度：tkfont.measure 精确测量（DPI 缩放/中文 12px 每字/emoji 更宽
        # 都自适应），替代 len(text)*(fs+1) 粗算——125% DPI 下原公式低估 ~20% 导致
        # 按钮文字溢出被压/被裁（v1.4 UI 排版修复）
        _tw = _fnt.measure(text)
        if width:
            # width 语义 = 字符数：9pt 中文约 12px/字，12px/字符 + measure 兜底取大
            w = max(width * 12, _tw + padx * 2 + 22)
        else:
            w = _tw + padx * 2 + 22
        canvas = tk.Canvas(parent, width=w, height=h,
                           bg=parent.cget('bg') if parent.winfo_class() == 'Frame' else self.C_BG,
                           highlightthickness=0, bd=0)
        canvas._skip_theme = True
        if kind == 'text':
            # 文字型操作：黑字 + 底部细黄下划线（下划线色随主题，hover 加粗）
            _ul = self.tc('btn.text.underline', '#FFE600')
            txt = canvas.create_text(w // 2, h // 2 - 2, text=text,
                                     fill=self.tc('btn.text.fg', '#222222'),
                                     font=font or self.FONT)
            ul_item = canvas.create_rectangle(4, h - 3, w - 4, h - 1, fill=_ul, outline='')
            btn = _CanvasBtn(canvas, None, txt, text, command, kind, colors, owner=self)
            btn.underline_item = ul_item
            btn._btn_h = h
            canvas.bind('<Button-1>', btn._click)
            canvas.tag_bind(txt, '<Button-1>', btn._click)
            canvas.bind('<Enter>', btn._hover)
            canvas.bind('<Leave>', btn._leave)
        else:
            # 微小圆角矩形：每角 2 控制点 + smooth → 1/4 圆弧，只弯角不弯边
            r = max(2, int(self.tc('btn.corner', 3)))  # 圆角半径（微小）
            poly = canvas.create_polygon(
                0, r, r, 0,
                w - r, 0, w, r,
                w, h - r, w - r, h,
                r, h, 0, h - r,
                smooth=True, splinesteps=12,
                fill=colors.get('bg', '#FFE600'),
                outline=(colors.get('edge') if colors.get('edge') and colors.get('edge') != colors.get('bg') else ''),
                width=1)
            fnt = font or (self.FONT_BOLD if kind in ('tag',) else self.FONT)
            txt = canvas.create_text(w // 2, h // 2, text=text, fill=colors.get('fg', '#111111'),
                                     font=fnt)
            btn = _CanvasBtn(canvas, poly, txt, text, command, kind, colors, owner=self)
            canvas.bind('<Button-1>', btn._click)
            canvas.tag_bind(poly, '<Button-1>', btn._click)
            canvas.tag_bind(txt, '<Button-1>', btn._click)
            canvas.bind('<Enter>', btn._hover)
            canvas.bind('<Leave>', btn._leave)
        btn._canvas = canvas
        if owner := getattr(self, '_register_redraw', None):
            owner(btn.retheme)
        if pack_side is not None:
            if pack_padx is not None:
                pack_kw['padx'] = pack_padx
            if pack_pady is not None:
                pack_kw['pady'] = pack_pady
            canvas.pack(side=pack_side, **pack_kw)
        else:
            canvas.pack(**pack_kw)
        return btn

    def __init__(self):
        # 日志：程序启动记录（按天分文件 logs/YYYY-MM-DD.log）
        try:
            log.hr(f"PDD EZ 启动 {VERSION}", 0)
            log.info(f"frozen={getattr(sys, 'frozen', False)}")
        except Exception:
            pass
        # 任务栏图标：必须在 Tk() 之前设置，否则源码运行时显示 python 图标
        if sys.platform == 'win32':
            import ctypes
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PDD.EZ")
            except Exception:
                pass
        self.win = tk.Tk()
        self.win.title("PDD EZ")
        # DPI 感知下 Tk 把 geometry 当逻辑像素：物理窗口 = 参数 ÷ (实际DPI/96)。
        # 本机 175% 缩放时 900x620 会被缩成 514x354，所有内容挤压被遮挡。
        # 换算系数 dpi_scale = 实际DPI/96，geometry 传 目标物理尺寸 × dpi_scale。
        try:
            _dpi = self.win.winfo_fpixels('1i')
            self.dpi_scale = max(1.0, _dpi / 96.0)
        except Exception:
            self.dpi_scale = 1.0
        def _geo(w, h):
            return f"{int(round(w * self.dpi_scale))}x{int(round(h * self.dpi_scale))}"
        self._geo = _geo
        self.win.geometry(_geo(900, 620))
        self.win.resizable(True, True)
        self.win.minsize(int(750 * self.dpi_scale), int(520 * self.dpi_scale))
        # R1 流程效率：窗口状态记忆——恢复上次保存的 geometry（越界保护走
        # clamp_geometry 纯函数：负坐标/超出屏幕一律拒绝，保持默认 _geo(900,620)）
        # R3 补充（BUG R1-Leftover-1）：尊重设置页「恢复上次窗口位置」开关
        # restore_last_pos=False 时不恢复（默认 True 保持原行为）。
        try:
            _win_cfg = Config.load().get('window') if hasattr(Config, 'load') else None
            _restore_pos = True
            if isinstance(_win_cfg, dict):
                _restore_pos = bool(_win_cfg.get('restore_last_pos', True))
        except Exception:
            _restore_pos = True
        if _restore_pos:
            try:
                _saved_geo = self._load_saved_geometry()
                if _saved_geo:
                    _fit_geo = clamp_geometry(_saved_geo, self.win.winfo_screenwidth(),
                                              self.win.winfo_screenheight())
                    if _fit_geo:
                        self.win.geometry(_fit_geo)
            except Exception:
                pass
        # v1.5.5 R3 健壮闭环：全局未捕获异常守卫（Tk 回调 + 子线程 excepthook）。
        # ui_notify 经 win.after 回主线程（worker 不直调 Tk）；钩子自身绝不抛。
        try:
            import exception_guard as _eg

            def _notify_ui_exc(msg):
                try:
                    if getattr(self, 'win', None) is None:
                        return
                    self.win.after(0, lambda m=msg: self._show_uncaught_hint(m))
                except Exception:
                    pass

            self._ex_handles = _eg.install(_notify_ui_exc)
        except Exception:
            self._ex_handles = None
        # v1.5.5 R3：启动时检测备份恢复残留（.pre_restore），状态栏提示一次
        # （恢复中断/手动备份残留的显式留痕，DESIGN §4）。
        try:
            import glob as _glob
            _pre = _glob.glob(os.path.join(get_base_dir(), '*.pre_restore'))
            if _pre:
                self.win.after(1500, lambda: self.status_text.set(
                    f"⚠ 检测到 {len(_pre)} 个 .pre_restore 备份残留——若上次恢复已成功可手动删除"))
        except Exception:
            pass
        # 窗口图标：打包后用 _MEIPASS，源码用脚本目录
        try:
            if getattr(sys, 'frozen', False):
                ico = os.path.join(sys._MEIPASS, 'icon.ico')
            else:
                ico = os.path.join(get_base_dir(), 'icon.ico')
            if os.path.exists(ico):
                self.win.iconbitmap(default=ico)
        except Exception:
            pass
        # 首次启动清理旧版本 EXE（v1.4+ 固定名 PDD EZ.exe，这里清理历史遗留的
        # PDD EZ vX.Y.exe——旧版升级客户首次启动自动清掉版本号 exe 残留）
        if getattr(sys, 'frozen', False):
            exe_dir = os.path.dirname(sys.executable)
            import re as _re
            # 正则匹配 PDD EZ vX.Y[.Z].exe，排除当前运行的
            pattern = _re.compile(r'^PDD EZ v\d+\.\d+(?:\.\d+)?\.exe$')
            for f in os.listdir(exe_dir):
                if not pattern.match(f):
                    continue
                old_path = os.path.join(exe_dir, f)
                if old_path == sys.executable:
                    continue
                try:
                    os.remove(old_path)
                except PermissionError:
                    pass  # 被占用，跳过
            # 删 _internal/ 废弃文件
            internal = os.path.join(exe_dir, "_internal")
            if os.path.isdir(internal):
                for old in ['api_keys.py', 'dpi_utils.py', 'keys.enc', 'gui_bridge.py']:
                    old_path = os.path.join(internal, old)
                    if os.path.exists(old_path):
                        try:
                            os.remove(old_path)
                        except PermissionError:
                            pass
        # 配置文件版本迁移
        self._migrate_config()
        # 加载皮肤偏好
        self._theme_name = load_theme_pref()
        self._theme_redraws = []  # 主题重绘注册表（Canvas 装饰/按钮）
        self._theme_spec = {}
        # 动效：按目标 widget 分键的 after job 注册表（取消时先 snap 终态再 cancel）
        self._anim_jobs = {}
        self._apply_theme(self._theme_name)
        self.rows = []
        self.plans = []  # 初始化，供 _export 防御性检查
        self._filter_warning_only = False  # 结果表"仅显示预警"筛选
        self._wh_filter = '全部仓库'  # 结果表"仓库筛选"（来自 OCR 仓库信息列）
        self._suppress_auto_append = False  # 清空输入时临时禁用自动加行
        self._batch_stop = threading.Event()  # 紧急停止信号
        self._batch_running = False  # v1.4.6 bug hunt F24 重入守卫：批量运行中标志（防双批并发）
        # 全局任务队列（单例，max_workers=1 防 API 并发抢额度）
        self._task_queue = async_queue.TaskQueue(max_workers=1)
        self._batch_task_id = None  # 当前批量任务 ID（用于 F9 停止）
        self.status_text = tk.StringVar(self.win, value="就绪｜确认数据后导出，识别结果表格可直接编辑，右键行可删除条目")
        # 多店铺隔离：店铺状态先于 regions 初始化（regions 已按店铺键读写，
        # _load_regions/_save_regions 走 store_registry 当前店铺）。store_registry
        # 读路径自愈保证「默认店铺」存在；get_active 失效自回落 default。
        self._store_id = (store_registry.get_active()
                          if store_registry is not None else 'default')
        self.store_var = tk.StringVar(self.win, value='')  # 店铺切换器显示名（_refresh_store_combo 填充）
        self._store_name2id = {}  # 店铺名 → id 反查（store_ui_logic.store_choices）
        self.regions = self._load_regions()
        # 当前地区由截图识别后确定；初始不预设配置表第一个地区（云南是时效配置，不是当前地区）
        self.region_var = tk.StringVar(self.win, value='未识别')
        
        # 多地区缓存
        self.cache = {}  # {region: {'plans': [...], 'items': [...]}}
        self.active_region = None

        # v1.4.8 P1-A：首启 EULA 强弹窗（必须在 _build_ui 之前，否则用户先看到主界面再被强行打断更不友好）
        # 拒绝则直接退出，符合"未同意不得使用"；模板自愈已加 eula_accepted_v1 字段。
        # 修复：弹窗必须阻塞（wait_window）才有效；仅当 _show_eula_dialog 返回 True 才继续。
        if not self._check_eula_accepted():
            if not self._show_eula_dialog():
                # 拒绝路径：销毁主窗口 + 退出进程（行为不变）
                try:
                    self.win.destroy()
                except Exception:
                    pass
                sys.exit(0)
            # 同意路径：再校验一次写盘结果（双保险，防 _show_eula_dialog 内部写盘失败但仍返回 True）
            if not self._check_eula_accepted():
                try:
                    self.win.destroy()
                except Exception:
                    pass
                sys.exit(0)

        # 删除 v1.5.0 P2-A 死代码 self._tier 赋值块
        # 根因：self._tier 只写不读；_auth_get_tier 调用结果无下游使用。
        # 实际授权门控路径（_live_screenshot → check_live_quota）每次现读 Config，
        # 不依赖 self._tier 缓存。 修复 问题 后 get_tier 自身带 300s TTL
        # 已足够覆盖 enforce 热切换场景，无需启动期预热。
        # （_auth_get_tier 的 import 在 L16 同批清理——若未来需要再 import）

        self._build_ui()
        self._check_update()  # 后台检查更新
        self._check_announcement()  # 后台检查公告（v1.4 新增，借鉴 March7th）
        self._check_secondary_config()  # v1.4.2：启动校验副模型配置（双模型失效提前提示）
        # v1.4.7 WS-A：启动按保留策略清理历史库（双阈值；低于阈值为廉价 no-op；失败仅日志）
        try:
            if history_db is not None:
                from utils import get_history_cfg
                _h = get_history_cfg()
                history_db.prune(retention_days=_h.get('retention_days', 180),
                                 max_rows=_h.get('max_rows', 200000))
        except Exception:
            pass
        # v1.4.7 WS-A：首次启用一次性隐私提示（识别数据仅本机持久化用于趋势展示，不上传）
        self._history_privacy_hint()
        # v1.4.7 WS-C：费用 Label 首刷 + 每分钟轮询（worker 线程不碰 Tk，刷新全走主线程 after）
        self._refresh_cost_label()
        try:
            self.win.after(60000, self._poll_cost_label)
        except Exception:
            pass

    def _check_secondary_config(self):
        """启动校验副模型配置（v1.4.2）：副模型是 doubao 裸模型名但缺 ep-xxx 推理
        接入点时双模型验证必然失效（客户日志坐实：InvalidEndpointOrModel.NotFound），
        零成本静态检测 → 状态栏提前提示，不让客户跑完才发现双模型没生效。"""
        try:
            from utils import get_secondary_model, get_api_config
            sec = get_secondary_model()
            if not sec:
                return
            api = get_api_config()
            providers = api.get('providers') or {}
            if not isinstance(providers, dict):
                return
            for _pn, _p in providers.items():
                if isinstance(_p, dict) and str(_p.get('model') or '') == sec:
                    _mdl = str(_p.get('model') or '')
                    _ep = str(_p.get('custom_endpoint') or '')
                    if _pn == 'doubao' and not _ep and not _mdl.startswith('ep-'):
                        self.win.after(800, lambda: self.status_text.set(
                            f"⚠ 副模型 {sec} 缺推理接入点(ep-xxx)，双模型验证可能失效，"
                            f"请在 API 管理填 ep 或换 glm-4v-flash"))
                    break
        except Exception:
            pass

    def _check_announcement(self):
        """后台拉取公告，有则弹窗展示（静默失败不打扰客户）"""
        from announcement import check_announcement
        try:
            check_announcement(self.win, self._show_announcement)
        except Exception:
            pass

    def _check_eula_accepted(self) -> bool:
        """读取 Config['eula_accepted_v1']，若为 True 则视为已同意；否则弹窗。

        失败安全：任何异常都返回 False（按 v1.4.x 失败哲学：宁可显式拒绝，不静默放行）。
        """
        try:
            from utils import Config
            cfg = Config.load()
            return bool(isinstance(cfg, dict) and cfg.get('eula_accepted_v1', False))
        except Exception:
            return False

    def _show_eula_dialog(self) -> bool:
        """EULA 首启强弹窗（局部事件循环阻塞，关闭后返回是否同意）。

        Returns:
            True  - 用户勾选"我已阅读并同意"且写盘成功（Config['eula_accepted_v1']=True）
            False - 用户拒绝（点"拒绝并退出"按钮）→ 弹窗已关闭，调用方应退出进程

        阻塞顺序（避免 TclError: grab failed: window not viewable）：
            update_idletasks() → wait_visibility() → grab_set() → wait_window()
        update_idletasks 先把几何/映射请求 flush，wait_visibility 等窗口真正 mapped，
        此时再 grab_set 才不会因"未映射窗口无法 grab"抛错。wait_window 进入局部事件循环，
        直到 dlg.destroy() 后才返回，__init__ 期间不会"无阻塞直接进 _build_ui"。
        """
        dlg = tk.Toplevel(self.win)
        dlg.title(EULA_TITLE)
        dlg.geometry(self._geo(720, 560))
        dlg.resizable(True, True)
        dlg.minsize(int(560 * self.dpi_scale), int(420 * self.dpi_scale))
        dlg.transient(self.win)
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)  # 屏蔽右上 X；必须点按钮

        # 顶部简短说明
        tk.Label(dlg, text=EULA_TITLE, font=(self.FONT[0] if hasattr(self, 'FONT') else 'Microsoft YaHei', 11, 'bold')
                 ).pack(pady=(12, 4))
        tk.Label(dlg, text=f"协议版本：{EULA_VERSION}（详细条款见 docs/EULA.md）",
                 font=(self.FONT[0] if hasattr(self, 'FONT') else 'Microsoft YaHei', 8),
                 fg='#888888').pack()

        # Text + Scrollbar 展示完整条款
        text_frame = tk.Frame(dlg)
        text_frame.pack(fill="both", expand=True, padx=16, pady=8)
        scrollbar = tk.Scrollbar(text_frame)
        scrollbar.pack(side="right", fill="y")
        text_widget = tk.Text(text_frame, wrap="word", yscrollcommand=scrollbar.set,
                              font=('Microsoft YaHei', 9), padx=8, pady=8)
        text_widget.insert("1.0", render_eula_text())
        text_widget.configure(state="disabled")  # 只读
        scrollbar.config(command=text_widget.yview)
        text_widget.pack(side="left", fill="both", expand=True)

        # 勾选框
        accepted_var = tk.BooleanVar(dlg, value=False)
        tk.Checkbutton(dlg, text="我已阅读并同意上述条款",
                       variable=accepted_var, font=('Microsoft YaHei', 10)
                       ).pack(pady=(4, 8))

        # 按钮区
        btn_frame = tk.Frame(dlg)
        btn_frame.pack(pady=(0, 12))

        # 用容器存结果，destroy 之后 wait_window 之前能读到最终值
        result = {"accepted": False, "saved": False}

        def on_continue():
            if not accepted_var.get():
                messagebox.showwarning("未同意", "请先勾选「我已阅读并同意」后再继续",
                                       parent=dlg)
                return
            # 写盘：eula_accepted_v1 = True
            try:
                from utils import Config
                cfg = Config.load()
                if not isinstance(cfg, dict):
                    cfg = {}
                cfg['eula_accepted_v1'] = True
                Config.save(cfg)
            except Exception as e:
                messagebox.showerror("保存失败",
                                     f"协议状态保存失败，将无法继续使用：{e}",
                                     parent=dlg)
                return
            result["accepted"] = True
            result["saved"] = True
            dlg.grab_release()
            dlg.destroy()

        def on_reject():
            # 用户拒绝 → 不写盘 → 关闭弹窗 → wait_window 返回 → 函数返回 False
            result["accepted"] = False
            result["saved"] = False
            dlg.grab_release()
            dlg.destroy()

        tk.Button(btn_frame, text="拒绝并退出", command=on_reject,
                  width=14, font=('Microsoft YaHei', 10)).pack(side="left", padx=8)
        tk.Button(btn_frame, text="同意并继续", command=on_continue,
                  width=14, font=('Microsoft YaHei', 10, 'bold')).pack(side="left", padx=8)

        # —— 阻塞序列：先让窗口真正可见，再 grab，再进入局部事件循环 ——
        try:
            dlg.update_idletasks()
            dlg.wait_visibility()  # 等到 dlg 在屏幕真正 mapped，grab_set 才不会失败
            dlg.grab_set()  # 模态：阻塞主窗口输入
        except Exception:
            # 极端场景下（无 X server / 测试环境）grab 失败也要继续走，不能让弹窗构造本身崩溃
            pass

        dlg.wait_window()  # ★ 关键：进入局部事件循环，dlg.destroy() 后才返回

        # wait_window 之后 result 已由 on_continue/on_reject 设置；
        # 仅当用户点了"同意并继续"且写盘成功才返回 True
        return bool(result.get("accepted") and result.get("saved"))

    def _show_announcement(self, title, content, image_url=''):
        """展示公告弹窗（Toplevel，支持可选图片）"""
        try:
            from PIL import Image as PILImage
            from PIL import ImageTk
            import urllib.request as _ur
            from io import BytesIO as _BytesIO
            dlg = tk.Toplevel(self.win)
            dlg.title(title or "公告")
            dlg.geometry(self._geo(480, 420))
            dlg.resizable(False, False)
            dlg.configure(bg=self.C_BG)
            dlg.transient(self.win)
            # 公告内容
            tk.Label(dlg, text=title or "公告", font=self.FONT_HEADING,
                    bg=self.C_BG, fg=self.C_TEXT).pack(pady=(15, 5))
            txt_frame = tk.Frame(dlg, bg=self.C_BG, highlightthickness=1,
                                 highlightbackground=self.C_BORDER)
            txt_frame.pack(fill="both", expand=True, padx=15, pady=5)
            txt = tk.Text(txt_frame, font=(self.FONT[0], 9), wrap="word",
                          bg=self.C_BG, fg=self.C_TEXT, relief="flat")
            txt.insert("1.0", content or "")
            txt.configure(state="disabled")
            txt.pack(fill="both", expand=True, padx=5, pady=5)
            # 可选图片（v1.4.5 bug hunt F29：下载移出主线程——urlopen 最长 10s 会冻结事件循环；
            # 验收回归：此前 `Thread(lambda: after(lambda: _apply_img(_fetch_img())))` 的内层
            # lambda 由主线程执行时仍调 _fetch_img()（urlopen 在主线程）——改为 worker 内取图
            if image_url:
                def _fetch_img():
                    try:
                        _req2 = _ur.Request(image_url, headers={"User-Agent": "PDD-EZ"})
                        _data = _ur.urlopen(_req2, timeout=10).read()
                        return PILImage.open(_BytesIO(_data))
                    except Exception:
                        return None
                def _apply_img(_img):
                    try:
                        if _img is None:
                            return
                        _img.thumbnail((440, 160))
                        _photo = ImageTk.PhotoImage(_img)
                        _lbl = tk.Label(dlg, image=_photo, bg=self.C_BG)
                        _lbl.image = _photo  # 防 GC
                        _lbl.pack(pady=5)
                    except Exception:
                        pass
                def _job():
                    _im = _fetch_img()  # worker 线程里 urlopen
                    self.win.after(0, lambda: _apply_img(_im))  # 主线程只创建 PhotoImage
                try:
                    import threading as _thr2
                    _thr2.Thread(target=_job, daemon=True).start()
                except Exception:
                    pass
            _btn_frame = tk.Frame(dlg, bg=self.C_BG)
            _btn_frame.pack(pady=10)
            self._mk_btn(_btn_frame, "知道了", dlg.destroy,
                         kind='primary', font=self.FONT_BOLD, width=12,
                         pack_side="left", padx=5)
        except Exception:
            pass  # 公告展示失败不影响主程序
        
    def _migrate_config(self):
        """配置文件版本迁移：阶梯式按版本号补全，每步立即原子写回"""
        import json as _json, shutil as _shutil
        sf = os.path.join(get_base_dir(), 'settings.json')
        if not os.path.exists(sf):
            return
        try:
            with open(sf, 'r', encoding='utf-8') as f:
                s = _json.load(f)
        except Exception:
            return

        CURRENT_CONFIG_VERSION = 4  # v1.4 移除绝对坐标模式
        ver = s.get('config_version', 0)
        if ver >= CURRENT_CONFIG_VERSION:
            return

        # 先备份
        try:
            _shutil.copy2(sf, sf + '.bak')
        except Exception:
            pass

        def _write():
            """原子写回——每步迁移完成后立即调用，防止崩溃导致半迁移状态"""
            try:
                with open(sf + '.tmp', 'w', encoding='utf-8') as f:
                    _json.dump(s, f, ensure_ascii=False, indent=2)
                os.replace(sf + '.tmp', sf)
            except Exception:
                pass

        # v0 → v1: 旧格式（mode/builtin_model）→ 新格式（active_provider/providers）
        if ver < 1:
            old_api = s.get('api', {})
            if 'active_provider' not in old_api and ('mode' in old_api or 'builtin_model' in old_api):
                s['api'] = {
                    'active_provider': 'doubao',
                    'providers': {'doubao': {}, 'qwen': {}, 'glm': {}}
                }
            s['config_version'] = 1
            _write()

        # v1 → v2: 预留（未来数据结构变更在此补充）
        if ver < 2:
            s['config_version'] = 2
            _write()

        # v2 → v3: 校准模块重构 — 相对偏移模式改为 AI 智能定位
        if ver < 3:
            cal = s.get('calibrate')
            # 畸形 calibrate（None/list/str 等）先归一化为空 dict，后续 .get 不再崩
            if not isinstance(cal, dict):
                cal = {}
            # 迁移旧 absolute 格式（dropdown/query 直接挂在 calibrate 下）
            if 'dropdown' in cal and 'query' in cal and 'absolute' not in cal:
                cal = {
                    'mode': cal.get('mode', 'absolute'),
                    'ai': cal.get('ai', {}),
                    'absolute': {
                        'dropdown': cal.get('dropdown', {}),
                        'query': cal.get('query', {})
                    }
                }
                s['calibrate'] = cal
            # 迁移旧 offset 模式
            if cal.get('mode') == 'offset':
                cal = {'mode': 'ai', 'ai': {}, 'absolute': cal.get('absolute', {})}
                s['calibrate'] = cal
            # 规范化 calibrate 结构（补充缺失字段）
            if 'calibrate' in s:
                cal = s['calibrate']
                if 'mode' not in cal:
                    cal['mode'] = 'ai'
                for key in ('ai', 'absolute'):
                    if key not in cal:
                        cal[key] = {}
                s['calibrate'] = cal
            s['config_version'] = 3
            _write()

        # v3 → v4: 移除绝对坐标模式，统一 AI 智能定位
        # 旧 absolute 数据仅作展示参考，不再作为定位来源（运行时 AI 实时定位覆盖）
        if ver < 4:
            cal = s.get('calibrate')
            if not isinstance(cal, dict):
                cal = {'mode': 'ai', 'ai': {}}
            cal['mode'] = 'ai'
            if not isinstance(cal.get('ai'), dict):
                cal['ai'] = {}  # 嵌套畸形兜底（程序自身不会写出，防御手改配置）
            s['calibrate'] = cal
            s['config_version'] = 4
            _write()
        
    def _check_update(self):
        """后台检查 GitHub 版本"""
        threading.Thread(target=self._do_check_update, daemon=True).start()
    
    def _fetch_latest_release(self):
        """从 GitHub API 获取最新 release 的 tag 和 body（多镜像测速选最快）"""
        from github_api import fetch_latest_release
        tag, body, _assets = fetch_latest_release(timeout=10)
        return tag, body
    
    def _do_check_update(self):
        try:
            latest, body = self._fetch_latest_release()
            if latest and version_newer(latest, VERSION):
                self._latest_tag = latest
                self._latest_body = body
                msg = f"🔄 有新版本 {latest}，点击「更新」查看详情"
                self.win.after(0, lambda: self.status_text.set(msg))
                # v1.4.1：更新按钮直接标注新版本号（build_ui 里持有引用；构建失败时静默）
                try:
                    self.win.after(0, lambda: self.update_btn.configure(text=f"🔄 更新 {latest}"))
                except Exception:
                    pass
            else:
                log.debug(f"更新检查: 已是最新 ({latest})")
        except Exception as e:
            # 静默 UI + 留日志（客户报"点更新没反应"时可定位网络/镜像问题）
            log.warning(f"更新检查失败: {e}")
    
    def _build_ui(self):
        # ── 全局热键 ──
        self.win.bind('<F9>', lambda e: self._emergency_stop())
        # R2 问题：主窗关闭先收队 TaskQueue（cancel_all + shutdown）再销毁——
        # 旧路径直接 destroy，后台 worker 的 in-flight API 请求被半截中断
        # （usage_log.jsonl 半截行），协作式取消钩子也永远不触发
        try:
            self.win.protocol("WM_DELETE_WINDOW", self._on_closing)
        except Exception:
            pass
        
        # ── 顶部：亮黄通栏（机能风，随主题 token）──
        top_bar = tk.Frame(self.win, bg=self.tc('decor.topbar.bg', '#FFE600'))
        top_bar.pack(fill="x")
        top_bar._skip_theme = True  # 通栏色由重绘表按 decor.topbar 刷新，walk 不碰
        _deco = tk.Canvas(top_bar, height=int(self.tc('decor.topbar.height', 66)),
                          bg=self.tc('decor.topbar.bg', '#FFE600'), highlightthickness=0)
        _deco.pack(fill="x")
        _deco._skip_theme = True
        self._deco = _deco
        # 黑色粗体大标题（左侧固定，色随主题）
        _deco.create_text(22, 16, text="PDD EZ", anchor='w',
                          fill=self.tc('decor.topbar.title_fg', '#111111'),
                          font=(self.FONT[0], 28, 'bold'))
        _deco.create_text(23, 50, text="补货助手 ｜ 自动计算", anchor='w',
                          fill=self.tc('decor.topbar.sub_fg', '#333333'), font=(self.FONT[0], 9))
        # 右侧斜切几何块 + 页码角标：按窗口实际宽度动态绘制（色随主题）
        def _redraw_deco(e=None):
            try:
                _deco.delete('deco')
            except Exception:
                return
            w = e.width if e is not None else _deco.winfo_width()
            if w < 200:
                return
            tb = self.tc('decor.topbar', {})
            _deco.create_polygon(w - 360, 0, w - 60, 0, w - 360, 66, fill=tb.get('block1', '#111111'),
                                 outline='', tags='deco')
            _deco.create_polygon(w - 230, 0, w - 30, 0, w - 230, 66, fill=tb.get('block2', '#333333'),
                                 outline='', tags='deco')
            _deco.create_line(22, 44, 420, 44, fill=tb.get('line', '#111111'), width=1, tags='deco')
            _deco.create_line(22, 48, 260, 48, fill=tb.get('line', '#111111'), width=1, tags='deco')
            _deco.create_polygon(w - 190, 10, w - 96, 10, w - 82, 24, w - 82, 32,
                                 w - 88, 38, w - 190, 38, w - 190, 18,
                                 fill=tb.get('ver_bg', '#111111'), outline=tb.get('ver_edge', '#FFE600'),
                                 width=1, tags='deco')
            _deco.create_text(w - 135, 24, text=VERSION.upper(), fill=tb.get('ver_fg', '#FFE600'),
                              font=(self.FONT[0], 9, 'bold'), tags='deco')
        _deco.bind('<Configure>', _redraw_deco)
        def _retheme_topbar():
            try:
                _bg = self.tc('decor.topbar.bg', '#FFE600')
                top_bar.configure(bg=_bg)
                _deco.configure(bg=_bg)
                _redraw_deco(None)
            except Exception:
                pass
        self._register_redraw(_retheme_topbar)
        # 工具条（白底 + 黑色细分割线，按钮行）
        tool_bar = tk.Frame(self.win, bg=self.C_BG)
        tool_bar.pack(fill="x", padx=15, pady=(8, 2))
        _ln = tk.Frame(tool_bar, bg=self.C_BORDER, height=1); _ln._skip_theme = True; _ln.pack(fill="x", pady=(0, 6)); self._register_redraw(lambda f=_ln: f.configure(bg=self.tc("decor.section.sep", "#E0E0E0")))
        # ☰ 导航按钮（幽灵：白底黄边）
        self._mk_btn(tool_bar, "☰ 导航", self._toggle_nav, kind='ghost', pack_side="left")
        # 当前模型标签
        api_cfg = get_api_config()
        active = api_cfg.get('active_provider', 'doubao')
        providers = api_cfg.get('providers', {})
        provider = providers.get(active, {}) if isinstance(providers, dict) else {}
        bm = provider.get('model', '') or active
        # 模型名超长截断：完整名（如 qwen3-omni-flash-2025-09-15 28字符）会撑满工具条
        # 挤压右侧按钮，只显示前 18 字符 + …（v1.4 UI 排版修复）
        _bm_full = bm
        if len(bm) > 18:
            bm = bm[:18] + '…'
        is_free = active == 'glm'
        # 模型标识胶囊（终末地：白底 + 切角标签）
        self.pill_frame = tk.Frame(tool_bar, bg=self.C_BG)
        self.pill_frame.pack(side="left", padx=12)
        self.pill_frame._skip_theme = True
        self.pill_name = tk.Label(self.pill_frame, text=bm, font=(self.FONT[0], 8, 'bold'),
                                   fg=self.C_TEXT, bg=self.C_BG)
        self.pill_name.pack(side="left", padx=(10,4), pady=4)
        self.pill_name._skip_theme = True
        tag_text = "FREE" if is_free else "PRO"
        _pill_cfg = self.tc('pill.free' if is_free else 'pill.pro', {'bg': '#FFE600', 'fg': '#111111'})
        tag_bg = _pill_cfg.get('bg', '#FFE600')
        tag_fg = _pill_cfg.get('fg', '#111111')
        # 切角角标（Canvas 多边形：左上/右下 45° 斜切，色随主题 token）
        _tcv = tk.Canvas(self.pill_frame, width=44, height=20, bg=self.C_BG,
                         highlightthickness=0, bd=0)
        _tcv._skip_theme = True
        _tcv.pack(side="left", padx=(0, 8), pady=2)
        _tcv.create_polygon(0, 2, 2, 0, 42, 0, 44, 2, 44, 18, 42, 20, 2, 20, 0, 18,
                            fill=tag_bg, outline=_pill_cfg.get('edge', '#111111'), width=1,
                            smooth=True, splinesteps=10)
        _tcv.create_text(22, 10, text=tag_text, fill=tag_fg,
                         font=(self.FONT[0], 7, 'bold'))
        self.pill_tag = _CanvasBtn(_tcv, None, None, tag_text, None, 'tag',
                                   _pill_cfg)
        self.pill_tag._canvas = _tcv
        self.pill_tag.text_item = list(_tcv.find_all())[1]
        def _retheme_pill():
            try:
                _pf = self.tc('pill.free' if self._pill_is_free else 'pill.pro', {'bg': '#FFE600', 'fg': '#111111'})
                _tcv.itemconfigure(list(_tcv.find_all())[0], fill=_pf.get('bg'), outline=_pf.get('edge', '#111111'))
                _tcv.itemconfigure(self.pill_tag.text_item, fill=_pf.get('fg'))
            except Exception:
                pass
        self._pill_is_free = is_free
        self._register_redraw(_retheme_pill)
        # v1.4.7 WS-C：API 消耗显示（本次/本月，元）——pill 旁小字 Label；
        # 数值来自 usage_store（估算行不计费），刷新一律 win.after 主线程调度
        self.cost_label = tk.Label(tool_bar, text="", font=(self.FONT[0], 8),
                                   fg=self.C_MUTED, bg=self.C_BG)
        self.cost_label._skip_theme = True
        self.cost_label.pack(side="left", padx=(0, 10))
        self._mk_btn(tool_bar, "🏪 商家后台", self._open_backend, kind='ghost',
                     pack_side="right", padx=5)
        # v1.4.1：持有更新按钮引用，检查到新版本时按钮直接标注版本号（客户无需盯状态栏）
        self.update_btn = self._mk_btn(tool_bar, "🔄 更新", self._run_updater, kind='ghost',
                                       pack_side="right", padx=5)
        
        # ── 主容器：左导航 + 右内容（可拖拽分割） ──
        # 导航默认展开——main_paned 初始即 add(nav_frame, before=content_frame)。
        # 修 P-A1 严重问题（新用户根本看不到 8 项功能）；_toggle_nav 保留提供折叠入口。
        self.main_paned = tk.PanedWindow(self.win, orient="horizontal", sashwidth=3, bg=self.C_BORDER)
        self.main_paned.pack(fill="both", expand=True, padx=15, pady=(2, 15))
        # 左侧导航栏（ width 随 dpi_scale 联动——nav 是全 UI 唯一
        # pack_propagate(False) 冻结像素宽的容器，字号随 tk scaling 缩放而 176px
        # 不缩，200% DPI 下「💰 用量明细」会被裁且导航占窗口比例减半）
        self.nav_frame = tk.Frame(self.main_paned, width=int(176 * self.dpi_scale), bg=self.C_BG)
        self.nav_frame._skip_theme = True  # 导航栏保持 C_SURFACE 区分色，主题切换不覆盖
        self.nav_frame.pack_propagate(False)
        self.nav_buttons = {}
        # 右侧内容
        self.content_frame = tk.Frame(self.main_paned)
        # ：默认 add nav_frame（before=content_frame，stretch="never" 固定宽度）
        # minsize 与 width 同步 DPI 联动（窗口 minsize 已在 L361 联动）
        self.main_paned.add(self.nav_frame, minsize=int(176 * self.dpi_scale), stretch="never")
        self.main_paned.add(self.content_frame, stretch="always")
        # 页面帧
        self.page_home = tk.Frame(self.content_frame, bg=self.C_BG)
        self.page_general = tk.Frame(self.content_frame, bg=self.C_BG)
        self.page_products = tk.Frame(self.content_frame, bg=self.C_BG)
        self.page_theme = tk.Frame(self.content_frame, bg=self.C_BG)
        self.page_backend = tk.Frame(self.content_frame, bg=self.C_BG)
        self.page_api = tk.Frame(self.content_frame, bg=self.C_BG)
        # v1.4.x 导航重构：历史趋势 / 用量明细 从弹窗独立成导航页（懒构建见 _show_page）
        self.page_history = tk.Frame(self.content_frame, bg=self.C_BG)
        self.page_usage = tk.Frame(self.content_frame, bg=self.C_BG)
        self._current_page = self.page_home

        # 冷启动修复（修 默认展开引入的空壳 bug）：导航默认展开后必须立即
        # 构建按钮内容，否则 nav_frame 展开是空壳（用户报告：启动时导航空白，点 ☰
        # 收起再展开按钮才出现）。_build_nav 引用 page_* 帧，必须在 8 个 page Frame
        # 全部创建之后、_current_page 赋值之后调用。
        # 修正注释失实：_build_ui 预构建后 nav_buttons 恒非空，
        # _toggle_nav 的懒构建守卫实际不再触发（仅作历史防御保留）。
        self._build_nav()

        # 初始 3 行数据对象（识别结果表承载显示/编辑，rows 仅存数据）
        for _ in range(3):
            self._add_row()
        
        # ── 全局工具栏（v1.5.7 用户拍板定版：批量｜导入｜识图｜导出 / 刷新｜双模型）──
        # 1) 第一排：批量（自动滚动整页采集）｜导入（文件数据源统一入口）居左，
        # 识图（截当前窗口）｜导出 居右；全部按钮统一宽度
        # （home_actions.BTN_WIDTH_HOME 单一常量驱动）。
        from home_actions import btn_width_for as _btn_w
        primary_row = tk.Frame(self.page_home, bg=self.C_BG)
        primary_row.pack(fill="x", padx=15, pady=(8, 4))
        # 批量 = 最左主入口（自动滚动采集整页、多省份独立；一键直达保留，用户拍板）
        self._mk_btn(primary_row, "批量", self._batch_scan, kind='dark',
                     font=(self.FONT[0], 9, 'bold'), width=_btn_w('dark'),
                     pack_side="left", padx=12, pack_padx=(0, 8))
        # 导入 = 文件数据源统一入口（v1.5.7：本地图片选择识别并入导入）——
        # 点击弹菜单二选一：导入表格文件 / 选择图片文件（1..N 张），见 _open_import_menu
        self.import_btn = self._mk_btn(primary_row, "导入", self._open_import_menu, kind='dark',
                                       font=(self.FONT[0], 9, 'bold'), width=_btn_w('dark'),
                                       pack_side="left", padx=12, pack_padx=(0, 8))
        # 导出 = 右侧结果动作终点（primary）
        self.export_btn = self._mk_btn(primary_row, "导出", self._export,
                                       kind='primary', font=(self.FONT[0], 9, 'bold'),
                                       width=_btn_w('primary'),
                                       pack_side="right", pack_padx=(8, 0))
        # 识图 = 右侧（v1.5.7 定版：直连截取当前窗口——最小化→截 PDD 窗口→恢复→识别；
        # 保留变量名 live_btn 供批量忙禁用/F9 恢复引用，注释防误解）
        self.live_btn = self._mk_btn(primary_row, "识图", self._live_screenshot, kind='text',
                                     width=_btn_w('text'), pack_side="right")

        # 2) 第二排：刷新 居左，🛡 双模型 居右（用户拍板定版）。
        btn_row = tk.Frame(self.page_home, bg=self.C_BG)
        btn_row.pack(fill="x", padx=15, pady=(0, 6))
        self._mk_btn(btn_row, "刷新", self._recalc_from_rows, kind='dark',
                     font=(self.FONT[0], 9), width=_btn_w('dark'),
                     pack_side="left", pack_padx=(0, 8))
        # 单次识别双模型开关（v1.3：不在乎 token 成本，默认开，识别更准）
        self._single_dual_var = tk.BooleanVar(self.win, value=True)
        tk.Checkbutton(btn_row, text="🛡 双模型", variable=self._single_dual_var,
                       font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_MUTED,
                       selectcolor=self.C_SURFACE, activebackground=self.C_SURFACE).pack(side="right", padx=(12, 0))
        
        # ── 当前地区（刷新计算按钮正下方一行，左对齐；识别后更新）──
        # 同排最左加店铺切换器（多店铺隔离主入口）。切换回调 _on_store_switch
        # 批量运行中互斥拒绝；同店幂等；跨店全量重建 regions/缓存/tab（DESIGN §3）。
        region_line = tk.Frame(self.page_home, bg=self.C_BG)
        region_line.pack(fill="x", padx=15, pady=(0, 4))  # R1 布局B：与上下行距统一 4px 节奏
        tk.Label(region_line, text="店铺:", font=(self.FONT[0], 8), fg=self.C_MUTED).pack(side="left")
        self.store_combo = ttk.Combobox(region_line, textvariable=self.store_var,
                                        values=[], width=10, state="readonly",
                                        font=(self.FONT[0], 8))
        self.store_combo.pack(side="left", padx=(2, 10))
        self.store_combo.bind('<<ComboboxSelected>>', self._on_store_switch)
        self._refresh_store_combo()
        tk.Label(region_line, text="当前地区:", font=(self.FONT[0], 8), fg=self.C_MUTED).pack(side="left")
        tk.Label(region_line, textvariable=self.region_var,
                 font=(self.FONT[0], 8), fg=self.C_MUTED).pack(side="left", padx=(0, 4))
        
        # ── 单条合并状态行（R1 布局B：导出按钮已并入上方 btn_row，本行只留状态）──
        # 动效 C：状态反馈脉冲——保留 Label 引用供 _pulse_status 调用
        self.status_label = tk.Label(self.page_home, textvariable=self.status_text,
                 font=(self.FONT[0], 8), fg=self.C_MUTED)
        self.status_label.pack(pady=(0, 4))
        # R1 布局B：批量进度条（常驻控件、按需 pack——批量开始 _begin_batch_progress
        # 插到状态栏上方；收尾/F9 取消由 _reset_batch_progress 隐藏复位。百分比来自
        # TaskQueue on_progress → _on_batch_progress，阶段文案映射 progress_status_text
        # 为模块级纯函数可单测；细条样式 Batch.* 前缀不影响更新弹窗默认进度条）
        self.batch_progress = ttk.Progressbar(self.page_home, mode='determinate',
                                              maximum=100,
                                              style='Batch.Horizontal.TProgressbar')
        
        # ── 结果表（纯炭黑卡片，无任何轮廓线）──
        self.result_frame = tk.Frame(self.page_home, bg=self.C_CARD_HDR)
        self.result_frame._skip_theme = True  # 深色卡片：_walk_force 不刷白
        self.result_frame.pack(fill="both", expand=True, padx=15, pady=(4, 10))
        self._register_redraw(lambda f=self.result_frame: f.configure(bg=self.tc('table.header_bg', '#1F1F1F')))
        
        tk.Label(self.result_frame, text="识别结果", font=(self.FONT[0], 11, 'bold'),
                 bg=self.C_CARD_HDR, fg='#FFFFFF').pack(fill="x", pady=(0,0))
        
        # 地区切换标签（无初始占位文字，识别出多地区后动态生成）
        self.tab_frame = tk.Frame(self.result_frame)
        self.tab_frame.pack(fill="x", padx=3, pady=(2,0))
        
        # 仅显示预警筛选（必须先于 tree pack：expand 控件优先分配空间，
        # 后 pack 的控件会被压缩到不可见——v1.4 修复）
        filter_frame = tk.Frame(self.result_frame)
        filter_frame.pack(side="bottom", fill="x", padx=3, pady=(0,3))
        self._filter_var = tk.BooleanVar(self.win, value=False)
        def toggle_filter():
            self._filter_warning_only = self._filter_var.get()
            if self.plans:
                self._render_tree(self.plans)
        tk.Checkbutton(filter_frame, text="仅显示预警（需补货/近期补货）", variable=self._filter_var,
                       command=toggle_filter, font=(self.FONT[0], 8),
                       bg=self.C_SURFACE, fg=self.C_TEXT, selectcolor=self.C_SURFACE,
                       activebackground=self.C_SURFACE).pack(side="left")
        # v1.5.13 结果列开关：预警/预测/模型三列可关（列表与导出共用配置，默认全开）
        try:
            from utils import get_result_cols_cfg as _grc, save_result_cols_cfg as _src
            self._result_cols = _grc()

            def _mk_col_toggle(label, key):
                _var = tk.BooleanVar(self.win, value=bool(self._result_cols.get(key, True)))

                def _on_toggle():
                    try:
                        self._result_cols[key] = bool(_var.get())
                        _src(self._result_cols)
                        if self.plans:
                            self._render_tree(self.plans)
                    except Exception:
                        pass
                tk.Checkbutton(filter_frame, text=label, variable=_var,
                               command=_on_toggle, font=(self.FONT[0], 8),
                               bg=self.C_SURFACE, fg=self.C_TEXT, selectcolor=self.C_SURFACE,
                               activebackground=self.C_SURFACE).pack(side="left", padx=(8, 0))
            tk.Label(filter_frame, text="列:", font=(self.FONT[0], 8), bg=self.C_SURFACE,
                     fg=self.C_MUTED).pack(side="left", padx=(14, 0))
            _mk_col_toggle('☑预警', 'warning')
            _mk_col_toggle('☑预测', 'forecast')
            _mk_col_toggle('☑模型', 'model')
        except Exception:
            try:
                from utils import get_result_cols_cfg as _grc2
                self._result_cols = _grc2()
            except Exception:
                self._result_cols = {'warning': True, 'forecast': True, 'model': True}
        tk.Label(filter_frame, text="商品过多时可筛选，减少渲染量",
                 font=(self.FONT[0], 8), fg=self.C_MUTED).pack(side="left", padx=8)
        # 仓库筛选（v1.3：识别全部商品后按 OCR 仓库信息列过滤展示）
        self._wh_filter_var = tk.StringVar(self.win, value='全部仓库')
        def toggle_wh_filter(*_a):
            self._wh_filter = self._wh_filter_var.get()
            if self.plans:
                self._render_tree(self.plans)
        tk.Label(filter_frame, text="仓库:", font=(self.FONT[0], 8), bg=self.C_SURFACE,
                 fg=self.C_TEXT).pack(side="left", padx=(14, 2))
        self.wh_combo = ttk.Combobox(filter_frame, textvariable=self._wh_filter_var,
                                     values=('全部仓库',), state='readonly', width=14,
                                     font=(self.FONT[0], 8))
        self.wh_combo.pack(side="left")
        self.wh_combo.bind('<<ComboboxSelected>>', toggle_wh_filter)
        
        columns = ("商品", "总库存", "总销量", "预估销量", "可售卖天数", "状态", "补货量")
        # 结果表放入带滚动条的容器（勾选列多时右侧列不再被截断）
        tree_frame = tk.Frame(self.result_frame, bg=self.C_CARD_HDR)
        tree_frame._skip_theme = True
        tree_frame.pack(fill="both", expand=True, padx=3, pady=3)
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=15)
        # clam 主题：Treeview rowheight 等样式可配置（原生 vista 主题 rowheight 失效）
        try:
            ttk.Style().theme_use('clam')
        except Exception:
            pass
        # 自定义纤细深色滚动条：Canvas 自绘（8px 深色滑块，替换 ttk/原生粗滚动条）
        self._vsb_canvas = tk.Canvas(tree_frame, width=9, bg=self.C_CARD_HDR, highlightthickness=0, bd=0)
        self._vsb_canvas._skip_theme = True
        self._hsb_canvas = tk.Canvas(tree_frame, height=9, bg=self.C_CARD_HDR, highlightthickness=0, bd=0)
        self._hsb_canvas._skip_theme = True
        self.tree.configure(yscrollcommand=self._on_tree_yscroll, xscrollcommand=self._on_tree_xscroll)
        self._vsb_first, self._vsb_last = 0.0, 1.0
        self._hsb_first, self._hsb_last = 0.0, 1.0
        self.tree.bind('<Configure>', lambda e: (self._draw_vsb(), self._draw_hsb()))
        self._vsb_canvas.bind('<Button-1>', self._click_vsb)
        self._vsb_canvas.bind('<B1-Motion>', self._drag_vsb)
        self._hsb_canvas.bind('<Button-1>', self._click_hsb)
        self._hsb_canvas.bind('<B1-Motion>', self._drag_hsb)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self._vsb_canvas.grid(row=0, column=1, sticky="ns")
        self._hsb_canvas.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        
        for col, w in zip(columns, [260, 110, 110, 100, 110, 100, 90]):
            self.tree.heading(col, text=col, command=lambda c=col: self._sort_tree(c))
            self.tree.column(col, width=w, anchor="center")
        
        self.tree.tag_configure('urgent', background=self.C_RED_BG)
        self.tree.tag_configure('warning', background=self.C_YELLOW_BG)
        
        # 排序状态
        self._sort_col = None
        self._sort_reverse = False
        
        # 可编辑表格：双击前 3 列（商品/总库存/总销量）→ overlay Entry → 回写 rows → 重算
        self.tree.bind("<Double-1>", self._tree_edit_cell)
        # 右键菜单：右键数据行删除该行；右键空白处新增空白行
        self.tree.bind("<Button-3>", self._tree_context_menu)
        
        # Treeview 行高加大，避免计算结果条目上下拥挤；表头黑底白字（不依赖主题）
        style = ttk.Style()
        style.configure("Treeview", rowheight=28)
        # R1 布局B：批量进度条细条样式（仅 Batch.* 前缀，不碰更新弹窗的默认进度条）
        style.configure("Batch.Horizontal.TProgressbar", thickness=8)
        try:
            style.configure("Treeview.Heading", background="#111111", foreground="#FFFFFF",
                            relief="flat", borderwidth=0, padding=(6, 4))
        except Exception:
            pass
        
        self._apply_theme(self._theme_name)
        self._refresh_model_badge()
        self.page_home.pack(fill="both", expand=True)
        


    def _refresh_model_badge(self):
        api_cfg = get_api_config()
        active = api_cfg.get('active_provider', 'doubao')
        providers = api_cfg.get('providers', {})
        provider = providers.get(active, {}) if isinstance(providers, dict) else {}
        model_name = provider.get('model', '') or active
        if len(model_name) > 18:
            model_name = model_name[:18] + '…'
        is_free = active == 'glm'
        self._pill_is_free = is_free
        self.pill_frame.configure(bg=self.C_BG)
        self.pill_name.configure(text=model_name, bg=self.C_BG, fg=self.C_TEXT)
        _pc = self.tc('pill.free' if is_free else 'pill.pro', {'bg': '#FFE600', 'fg': '#111111'})
        tag_text = "FREE" if is_free else "PRO"
        self.pill_tag.configure(text=tag_text, bg=_pc.get('bg', '#FFE600'), fg=_pc.get('fg', '#111111'))

    def _toggle_nav(self):
        if self.nav_frame.winfo_ismapped():
            self.main_paned.forget(self.nav_frame)
        else:
            self.main_paned.add(self.nav_frame, before=self.content_frame, minsize=int(176 * self.dpi_scale), stretch="never")  # 150→176（v4f P3）再 DPI 联动（）
            if not self.nav_buttons:
                self._build_nav()

    def _build_nav(self):
        # 修复：幂等守卫——重复调用会重建按钮并泄漏旧 widget（旧
        # command 仍可触发）。当前调用点均有外层守卫，此处兜底防御未来调用点。
        if self.nav_buttons:
            return
        # 8 项分 3 组——【工作区】【数据】【设置】
        # 组标 = self._lbl 8pt C_MUTED（pady=(10,2) 组前/ (0,4) 组后）
        # 组间 1px C_BORDER 分隔 Frame（fill x）
        # nav 按钮 padx=14 pady=7（保留语义化比旧 padx=12 略宽对齐 176px 宽）
        groups = [
            ('工作区', [
                ("🏠 首页", self.page_home),
                ("📦 商品", self.page_products),
            ]),
            ('数据', [
                ("📈 历史趋势", self.page_history),
                ("💰 用量明细", self.page_usage),
            ]),
            ('设置', [
                ("⚙ 通用", self.page_general),
                ("🔑 API", self.page_api),
                ("🎨 主题", self.page_theme),
                ("🔗 后台", self.page_backend),
            ]),
        ]
        first = True
        for group_name, items in groups:
            if not first:
                # ：组间 1px C_BORDER 分隔
                _sep = tk.Frame(self.nav_frame, bg=self.C_BORDER, height=1)
                _sep._skip_theme = True
                _sep.pack(fill="x", padx=10, pady=6)
                # （mmx-or A3 / gmi 共识）：_skip_theme 冻结的是 walk，
                # 分隔线底色须挂重绘表跟主题走（与工具栏分隔线 gui.py:857 同模式），
                # 否则切浅色主题后残留旧主题深色横杠。
                self._register_redraw(lambda f=_sep: f.configure(bg=self.tc("decor.section.sep", "#E0E0E0")))
            first = False
            # ：组标（8pt C_MUTED）
            self._lbl(self.nav_frame, text=group_name, font=(self.FONT[0], 8),
                      bg=self.C_BG, fg=self.C_MUTED).pack(anchor='w', padx=14, pady=(10, 2))
            for text, page in items:
                _nf = tk.Frame(self.nav_frame, bg=self.C_BG, bd=0, highlightthickness=0)
                _nf._skip_theme = True
                _ni = tk.Frame(_nf, bg=self.C_BG, bd=0, highlightthickness=0)
                _ni._skip_theme = True
                _ni.pack(side="left", padx=(0, 0), pady=0, fill="x", expand=True)
                # ：nav 按钮 padx=14 pady=7
                btn = tk.Button(_ni, text=text, relief="flat",
                               font=(self.FONT[0], 9), anchor="w", padx=14, pady=7,
                               bg=self.C_BG, fg=self.C_TEXT, activebackground=self.C_SURFACE,
                               bd=0, command=lambda p=page: self._show_page(p))
                btn._page = page
                btn._nf = _nf
                btn.pack(fill="x")
                _nf.pack(fill="x")
                self.nav_buttons[text] = btn
        self._highlight_nav(self.page_home)

    def _highlight_nav(self, page):
        for btn in self.nav_buttons.values():
            if getattr(btn, '_page', None) == page:
                # 选中项：立即跳变（一点即用优先，不动画）
                btn.configure(bg=self.C_CARD_HDR, fg="#FFFFFF")
            else:
                # 先判动画资格——_animate_nav_leave 的动画链
                # 自保证终态（_do 终步 configure + 异常熔断 + cancel snap 三重兜底），
                # 若先无条件 configure 再判 _cur==C_CARD_HDR 则永远读到 C_BG、
                # 渐隐动画永不触发（无害死代码）。非动画位显式 configure 兜底不漏配。
                try:
                    _cur = str(btn.cget('bg')).lower()
                except Exception:
                    _cur = ''
                if _cur == str(self.C_CARD_HDR).lower() and ANIMATIONS_ENABLED:
                    self._animate_nav_leave(btn)
                else:
                    btn.configure(bg=self.C_BG, fg=self.C_TEXT)

    def _show_page(self, page):
        if self._current_page:
            self._current_page.pack_forget()
        page.pack(fill="both", expand=True)
        self._current_page = page
        self._highlight_nav(page)
        if page == self.page_general and not hasattr(page, '_built'):
            self._build_general_page()
        elif page == self.page_products and not hasattr(page, '_built'):
            self._build_product_region_tab(page)
        elif page == self.page_theme and not hasattr(page, '_built'):
            self._build_skin_tab(page)
        elif page == self.page_backend and not hasattr(page, '_built'):
            self._build_backend_tab(page)
        elif page == self.page_api and not hasattr(page, '_built'):
            self._build_api_page(page)
        elif page == self.page_history and not hasattr(page, '_built'):
            self._build_history_page(page)
        elif page == self.page_usage and not hasattr(page, '_built'):
            self._build_usage_page(page)
        if not hasattr(page, '_built'):
            page._built = True
        # 导航重构：历史/用量数据页每次切入刷新（after 调度进主线程事件队列，
        # worker 线程不碰 Tk）；首次构建后同样走一遍，保证数据与切入时刻一致
        if page == self.page_history:
            try:
                # R2 问题：先联动主页店铺再刷新（_history_page_enter 内部调 _history_page_refresh）
                self.win.after_idle(getattr(self, '_history_page_enter', None) or self._history_page_refresh)
            except Exception:
                pass
        elif page == self.page_usage:
            try:
                self.win.after_idle(self._usage_page_refresh)
            except Exception:
                pass
        # 切页只刷新模型徽章；主题全量重涂仅在主题切换时执行（避免每次切页全树 walk）
        self._refresh_model_badge()

    
    def _show_error(self, msg, popup=False):
        """显示错误：状态栏 + 报错栏，可选弹窗"""
        self.status_text.set(f"❌ {msg[:50]}")
        if popup:
            messagebox.showerror("出错", msg)

    def _friendly_error(self, exc, popup=True, title=None):
        """v1.4.8 P2-OCR t9：把异常归类到 USER_MSG_*，统一用户可读提示。

        - 状态栏：始终写一句 ≤50 字的中文提示（不暴露 stracktrace/英文术语）。
        - 弹窗：默认 True，title 由 ocr_review.categorize_error 给出（按类别分）。
        - 失败安全：归类或弹窗失败 → 退化为通用提示，绝不抛错阻塞识别主流程。
        """
        try:
            from ocr_review import categorize_error as _ce
            _cat, _msg, _def_title = _ce(exc)
            _title = title or _def_title
        except Exception:
            _cat, _msg, _title = 'unknown', '识别或导入过程中出现异常，请重试或检查文件后重试', title or '出错'
        # 报错原文入日志（用户反馈"报错但不知内容"——可从日志直接定位；
        # 归类类别 + 原始异常摘要脱敏记录，方便远程诊断）
        try:
            from utils import _sanitize_for_log as _sfl
            log.warn(f"[_friendly_error/{_cat}] {_sfl(str(exc))[:300]}")
        except Exception:
            pass
        try:
            self.status_text.set(f"❌ {_msg[:50]}")
        except Exception:
            pass
        if popup:
            try:
                messagebox.showerror(_title, _msg, parent=self.win)
            except Exception:
                pass

    def _show_review_dialog(self, items):
        """v1.4.8 P2-OCR t9：低置信行人工复核弹窗（Toplevel + Treeview）。

        设计：非阻塞（主窗仍可点，但弹窗 modal 强制选择），
        用户三个出口：
          1) 「全部接受并计算」→ 返回 ('accept', []) —— 继续走 _calc_from_items
          2) 「取消」              → 返回 ('cancel', [])  —— 跳过计算/不入历史
          3) 双击/编辑某行的 stock/sales → 收集 edits 返回 ('edited', [{index,field,value}])
        模糊截图：弹窗顶部显眼横幅提示 + 「重新截图」按钮（关闭弹窗并返回 'cancel'，
                  由调用方决定是否再走截图路径；这里只暴露出口，不直接调截图线程）。

        Args:
            items: parse_items_generic 产出（已注入 confidence 元数据）。

        Returns:
            (action: 'accept'|'cancel'|'edited', edits: list)
            失败安全：任何异常 → ('cancel', [])
        """
        try:
            import tkinter as _tk
            from tkinter import ttk as _ttk
            from ocr_review import build_review_list as _brl
            _rows = _brl(items)
            if not _rows:
                return ('accept', [])
            # 是否有图片模糊
            _blur_alert = any(
                isinstance(it, dict) and it.get('confidence', {}).get('level') == 'low'
                and any('模糊' in r for r in (it.get('confidence', {}) or {}).get('reasons', []))
                for it in items
            )
            top = _tk.Toplevel(self.win)
            top.title("低置信识别 — 人工复核")
            top.geometry(self._geo(820, 480))
            top.configure(bg=self.C_BG)
            top.transient(self.win)
            top.minsize(int(720 * self.dpi_scale), int(360 * self.dpi_scale))

            # 顶部统计 + 模糊提示
            from ocr_review import summarize_review as _sr
            _sm = _sr(items)
            _hdr_txt = (f"共 {_sm['total']} 项：低置信 {_sm['low']}、"
                        f"中 {_sm['medium']}、高 {_sm['high']}；"
                        f"待复核 {_sm['need_review']} 项")
            _tk.Label(top, text=_hdr_txt, font=self.FONT_BOLD,
                      bg=self.C_BG, fg=self.C_TEXT).pack(
                anchor='w', padx=14, pady=(12, 4))
            if _blur_alert:
                _tk.Label(top,
                          text="⚠ 截图模糊，建议重新截图后重试（可点「重新截图」直接取消本次识别）",
                          font=(self.FONT[0], 9, 'bold'),
                          bg=self.C_RED_BG, fg=self.C_PRIMARY).pack(
                    fill='x', padx=14, pady=(0, 6))

            # 表格（行 = 待复核项；列：商品名/字段/原因/原文/解析值）
            # R1 布局B：列规格抽为模块级 REVIEW_COLS（可单测）——商品名/异常原因
            # stretch 跟随窗口加宽，字段/原文/解析值定宽；补横向滚动条（窄窗下
            # 长商品名/长原因不再被裁死，可横向滚动查看全文）
            _cols = tuple(c[0] for c in REVIEW_COLS)
            _wrap = _tk.Frame(top, bg=self.C_BG)
            _tree = _ttk.Treeview(_wrap, columns=_cols, show='headings', height=12)
            for _cid, _txt, _w, _min, _stretch in REVIEW_COLS:
                _tree.heading(_cid, text=_txt)
                _tree.column(_cid, width=_w, minwidth=_min, stretch=_stretch,
                             anchor='w' if _cid in ('name', 'reason', 'raw') else 'center')
            _vsb = _ttk.Scrollbar(_wrap, orient='vertical', command=_tree.yview)
            _hsb = _ttk.Scrollbar(_wrap, orient='horizontal', command=_tree.xview)
            _tree.configure(yscrollcommand=_vsb.set, xscrollcommand=_hsb.set)
            _tree.grid(row=0, column=0, sticky='nsew')
            _vsb.grid(row=0, column=1, sticky='ns')
            _hsb.grid(row=1, column=0, sticky='ew')
            _wrap.rowconfigure(0, weight=1)
            _wrap.columnconfigure(0, weight=1)
            _wrap.pack(fill='both', expand=True, padx=10, pady=(0, 8))
            # 写行
            for _r in _rows:
                _tree.insert('', 'end', iid=str(_r['index']),
                             values=(_r['name'], _r['field'], _r['reason'],
                                     _r['raw'], _r['parsed']))

            # 简单编辑：双击某行 → 弹小输入框 → 写回 items；这里只暴露给"修正后重新计算"按钮
            # 用 dict 收集 edits（在闭包内修改）
            _edits = []

            def _edit_selected():
                try:
                    _sel = _tree.selection()
                    if not _sel:
                        return
                    _iid = _sel[0]
                    if not _iid.isdigit():
                        return
                    _idx = int(_iid)
                    if _idx < 0 or _idx >= len(items):
                        return
                    _it = items[_idx]
                    if not isinstance(_it, dict):
                        return
                    _fld = _it.get('_review_field', 'stock')  # 优先用上次选中字段
                    _cur = _it.get(_fld, 0)
                    _dlg = _tk.Toplevel(top)
                    _dlg.title(f"修正 — 索引 {_idx}")
                    _dlg.geometry(self._geo(360, 200))
                    _dlg.transient(top)
                    _dlg.configure(bg=self.C_BG)
                    _tk.Label(_dlg, text=f"商品名：{_it.get('name','')[:30]}",
                              font=self.FONT, bg=self.C_BG, fg=self.C_TEXT).pack(
                        anchor='w', padx=14, pady=(12, 4))
                    _field_var = _tk.StringVar(value=_fld)
                    _row = _tk.Frame(_dlg, bg=self.C_BG)
                    _row.pack(fill='x', padx=14, pady=4)
                    # R2 问题 UI 配套：白名单已扩 region/warehouse（ocr_review.apply_user_edits），
                    # 编辑弹窗 radio 同步放开——识别错的销售区域/仓库可直接修正
                    for _opt in ('stock', 'sales', 'name', 'region', 'warehouse'):
                        _tk.Radiobutton(_row, text={'stock': '库存', 'sales': '销量',
                                                    'name': '商品名',
                                                    'region': '销售区域',
                                                    'warehouse': '仓库'}.get(_opt, _opt),
                                        variable=_field_var, value=_opt,
                                        font=self.FONT, bg=self.C_BG).pack(side='left', padx=6)
                    _val_var = _tk.StringVar(value=str(_cur))
                    _tk.Label(_dlg, text="新值：", font=self.FONT,
                              bg=self.C_BG).pack(anchor='w', padx=14, pady=(6, 2))
                    _tk.Entry(_dlg, textvariable=_val_var, font=self.FONT).pack(
                        fill='x', padx=14)
                    def _save():
                        _new_field = _field_var.get()
                        _new_val = _val_var.get()
                        _edits.append({'index': _idx, 'field': _new_field, 'value': _new_val})
                        # 立刻在 Treeview 显示编辑后值
                        _tree.set(_iid, 'field', _new_field)
                        if _new_field in ('stock', 'sales'):
                            try:
                                _new_int = int(_new_val)
                            except (ValueError, TypeError):
                                _new_int = _new_val
                            _tree.set(_iid, 'parsed', _new_int)
                        else:
                            _tree.set(_iid, 'raw', _new_val[:60])
                        _dlg.destroy()
                    _btns = _tk.Frame(_dlg, bg=self.C_BG)
                    _btns.pack(fill='x', padx=14, pady=(10, 12))
                    self._mk_btn(_btns, "取消", _dlg.destroy,
                                 kind='ghost', font=(self.FONT[0], 9)).pack(side='right', padx=4)
                    self._mk_btn(_btns, "保存", _save, kind='primary',
                                 font=(self.FONT[0], 9, 'bold')).pack(side='right', padx=4)
                    _dlg.grab_set()
                    _dlg.wait_window()
                except Exception:
                    pass
            _tree.bind('<Double-1>', lambda _e: _edit_selected())

            result = [('cancel', [])]  # 默认 cancel（关闭弹窗 = 取消）

            def _on_accept():
                result[0] = ('accept', list(_edits))
                top.destroy()

            def _on_apply_edits():
                result[0] = ('edited', list(_edits))
                top.destroy()

            def _on_cancel():
                result[0] = ('cancel', list(_edits))
                top.destroy()

            # 底部按钮栏（R1 布局B：三出口居右、编辑辅助居左，单栏归位——
            # 原「修正选中行」误挂在从未 pack 的孤儿帧上，弹窗里根本看不见）
            _btns_frame = _tk.Frame(top, bg=self.C_BG)
            _btns_frame.pack(fill='x', padx=14, pady=(0, 12))
            if _blur_alert:
                # 模糊时多一个"重新截图"快捷按钮：等同取消（不计算），由调用方决定
                self._mk_btn(_btns_frame, "重新截图", _on_cancel, kind='text',
                             font=(self.FONT[0], 9)).pack(side='left', padx=(0, 12))
            _edit_btn = self._mk_btn(_btns_frame, "修正选中行", _edit_selected, kind='dark',
                                     font=(self.FONT[0], 9))
            _edit_btn.pack(side='left')
            # R2 批次C：无选中行时「修正」禁用（看得见的状态，不靠点了没反应；
            # 状态映射 review_edit_btn_state 纯函数可单测）
            _edit_btn.configure(state=review_edit_btn_state(bool(_tree.selection())))

            def _sync_edit_btn(_e=None):
                try:
                    _edit_btn.configure(state=review_edit_btn_state(bool(_tree.selection())))
                except Exception:
                    pass
            _tree.bind('<<TreeviewSelect>>', _sync_edit_btn, add='+')
            self._mk_btn(_btns_frame, "取消（不计算）", _on_cancel, kind='ghost',
                         font=(self.FONT[0], 9)).pack(side='right', padx=4)
            self._mk_btn(_btns_frame, "修正后重新计算", _on_apply_edits, kind='dark',
                         font=(self.FONT[0], 9, 'bold')).pack(side='right', padx=4)
            self._mk_btn(_btns_frame, "全部接受并计算", _on_accept, kind='primary',
                         font=(self.FONT[0], 9, 'bold')).pack(side='right', padx=4)
            # R2 问题：X 关闭走显式取消路径——默认 result 本就是 cancel，这里把
            # 语义钉死，保证 wait_window 必有归宿、result 不悬空
            try:
                top.protocol("WM_DELETE_WINDOW", _on_cancel)
            except Exception:
                pass
            top.grab_set()
            top.wait_window()
            return result[0]
        except Exception:
            # 失败安全：弹窗打不开 → 默认接受（不阻塞主流程）
            return ('accept', [])

    def _clear_error(self):
        self.status_text.set("就绪｜确认数据后导出，识别结果表格可直接编辑，右键行可删除条目")
    
    def _auto_expand(self, row_count: int):
        """结果出来后自动展开窗口，动态测量确保 Treeview 可见，封顶屏幕 82%"""
        self.win.update_idletasks()  # 强制完成布局
        
        # 动态测量：结果区域顶部距离窗口顶部的实际像素
        result_top = self.result_frame.winfo_rooty() - self.win.winfo_rooty()
        if result_top <= 0:
            result_top = 400  # 窗口最小化或未完成布局时的默认值
        
        # Treeview 可见行数 + 列头 + 内边距
        ROW_HEIGHT = 28
        MIN_VISIBLE = 8
        visible_rows = max(row_count, MIN_VISIBLE)
        tree_needed = 25 + visible_rows * ROW_HEIGHT  # 列头 ~25px
        
        # 标签栏高度（有缓存数据时才占位）
        tab_needed = 28 if self.cache else 0
        
        # 理想窗口高度 = 结果区域顶部 + 所有子内容 + 底部留白
        ideal_height = result_top + tab_needed + tree_needed + 15
        
        screen_h = self.win.winfo_screenheight()
        max_h = int(screen_h * 0.82)
        
        target_h = min(ideal_height, max_h)
        current_h = self.win.winfo_height()
        
        if target_h > current_h:
            current_w = max(self.win.winfo_width(), 200)
            # DPI 感知下：geometry 参数与 winfo widget 坐标都是【逻辑单位】，
            # 但 winfo_screenwidth/screenheight 返回【物理】屏尺寸——max_h 按物理
            # 计算会与逻辑 target_h 混比，高 DPI 下窗口展开不足/越界（v1.4 审查修复）。
            # 统一换算：物理屏高 ÷ dpi_scale → 逻辑屏高，再算 82% 上限。
            _ds = getattr(self, 'dpi_scale', 1.0) or 1.0
            _logic_screen_h = screen_h / _ds if _ds else screen_h
            max_h = int(_logic_screen_h * 0.82)
            target_h = min(ideal_height, max_h)
            # 位置也按逻辑单位居中（winfo_screenwidth 是物理，转逻辑）
            _logic_sw = (self.win.winfo_screenwidth() / _ds) if _ds else self.win.winfo_screenwidth()
            x = int((_logic_sw - current_w) // 2)
            y = max(0, int((_logic_screen_h - target_h) // 3))
            self.win.geometry(f"{current_w}x{target_h}+{x}+{y}")
            self.win.update()  # 立即生效
    
    def _add_row(self):
        """新增一行数据（UI 输入卡已隐藏，只维护 rows 数据对象；表格显示/编辑走识别结果表）"""
        row = {}
        row['name'] = tk.StringVar(self.win)
        row['stock'] = tk.StringVar(self.win)
        row['sales'] = tk.StringVar(self.win)
        
        self.rows.append(row)
        
        # 自动加行：最后一行有数据时自动追加（监听三个输入框变化）
        # 注意：trace_add 对每行都注册，回调必须校验"触发者即末行"，否则回改中间行会误加空行
        def _auto_append(row, *_args):
            if getattr(self, '_suppress_auto_append', False):
                return  # 清空输入时禁用
            if not self.rows:
                return
            if row is not self.rows[-1]:
                return  # 只有末行输入才可能触发加行
            if row['name'].get().strip() or row['stock'].get().strip() or row['sales'].get().strip():
                self._add_row()
        row['name'].trace_add('write', lambda *a, r=row: _auto_append(r, *a))
        row['stock'].trace_add('write', lambda *a, r=row: _auto_append(r, *a))
        row['sales'].trace_add('write', lambda *a, r=row: _auto_append(r, *a))
        
        # 加行后立即反映到识别结果表格（UI 输入卡已隐藏，表格即唯一展示）。
        # 批量填充（_fill_from_ocr）期间 _suppress_auto_append=True，不排队重算——
        # 否则 N 个商品排队 N 次 after(0, _recalc_from_rows)，回调在填充返回后执行，
        # 全量混合数据会覆盖 cache[region_var]（最后省份），且 UI 反复全量重渲染卡顿（v1.4 修复）
        if getattr(self, '_suppress_auto_append', False):
            return
        if hasattr(self, 'tree') and self.tree.winfo_exists():
            self.win.after(0, self._recalc_from_rows)
    
    def _load_regions(self):
        """t6：当前店铺的 地区→商品运输时效 映射（store_registry 按店铺读写）。

        - regions.json 已升级为按店铺 {store_id: {region: {product: days}}}，
          旧顶层格式由 store_registry 自动迁移进「默认店铺」（{region: days} →
          {region: {"": days}}，与旧 _load_regions 兼容语义一致）；
        - store_registry 缺失（极端缺文件）时降级空配置并留日志，绝不抛。
        """
        try:
            if store_registry is not None:
                return store_registry.get_regions(getattr(self, '_store_id', None))
        except Exception as _e:
            try:
                log.warning(f"按店铺加载时效配置失败（降级空配置）: {_e}")
            except Exception:
                pass
        return {}
    
    def _get_backend_config(self):
        """读取商家后台配置（URL/账号/密码）"""
        import json
        settings_file = os.path.join(get_base_dir(), 'settings.json')
        try:
            with open(settings_file, 'r', encoding='utf-8') as f:
                s = json.load(f)
                return s.get('backend', {})
        except:
            return {}
    
    def _open_backend(self):
        """打开拼多多商家后台"""
        import webbrowser
        config = self._get_backend_config()
        url = config.get('url', 'https://mms.pinduoduo.com/')
        if not url.startswith('http'):
            url = 'https://' + url
        webbrowser.open(url)
        self.status_text.set("已打开商家后台 → 请手动登录")
    
    def _run_updater(self):
        """显示更新详情 + 进度 + 错误处理（v1.4 修复：下载在主程序内完成，进度实时显示）"""
        latest = getattr(self, '_latest_tag', '')
        body = getattr(self, '_latest_body', '')
        if not latest:
            try:
                latest, body = self._fetch_latest_release()
            except Exception as e:
                messagebox.showerror("检查失败", f"无法连接更新服务器：{e}")
                return
        
        if not latest or not version_newer(latest, VERSION):
            messagebox.showinfo("已是最新", f"当前已是最新版本 {VERSION}")
            return
        
        # 弹窗显示更新日志 + 确认
        changelog = body or "(无更新日志)"
        # 截断过长的日志
        if len(changelog) > 500:
            changelog = changelog[:500] + "..."
        
        dlg = tk.Toplevel(self.win)
        dlg.title("软件更新")
        dlg.geometry(self._geo(480, 360))
        dlg.resizable(False, False)
        dlg.configure(bg=self.C_BG)
        dlg.transient(self.win)
        dlg.grab_set()
        
        tk.Label(dlg, text=f"发现新版本 {latest}", font=self.FONT_HEADING,
                bg=self.C_BG, fg=self.C_TEXT).pack(pady=(15,5))
        
        # 更新日志
        log_frame = tk.Frame(dlg, bg=self.C_BG, highlightthickness=1, highlightbackground=self.C_BORDER)
        log_frame.pack(fill="both", expand=True, padx=15, pady=5)
        log_text = tk.Text(log_frame, font=(self.FONT[0], 8), wrap="word", height=6,
                          bg=self.C_BG, fg=self.C_TEXT, relief="flat")
        log_text.insert("1.0", changelog)
        log_text.configure(state="disabled")
        log_text.pack(fill="both", expand=True, padx=5, pady=5)
        
        # 状态 + 进度条（v1.4 修复：下载进度实时显示，替换旧版假进度）
        status_lbl = tk.Label(dlg, text="准备下载...", font=(self.FONT[0], 9),
                              bg=self.C_BG, fg=self.C_MUTED)
        status_lbl.pack(pady=(4, 0))
        progress = ttk.Progressbar(dlg, mode="determinate", length=440)
        progress.pack(pady=6)
        
        # 按钮区
        btn_frame = tk.Frame(dlg, bg=self.C_BG)
        btn_frame.pack(pady=8)
        
        state = {"cancelled": False, "running": False, "ready_install": False,
                 "downloaded_zip": "", "sha_ok": False}
        # v1.4.1：窗口关闭 = 取消进行中的下载（后台线程检查 state["cancelled"] 退出；
        # 不设的话下载中关窗 → 后续 after 回调操作已销毁控件 → TclError 刷屏）
        def _on_close():
            state["cancelled"] = True
            state["running"] = False
            try:
                dlg.destroy()
            except Exception:
                pass
        dlg.protocol("WM_DELETE_WINDOW", _on_close)
        
        def _set_status(text, pct=None):
            try:
                status_lbl.configure(text=text)
                if pct is not None:
                    progress.configure(value=pct)
                dlg.update_idletasks()
            except Exception:
                pass  # 窗口已关闭：静默（下载线程会随 cancelled 退出）
        
        def _fail(msg, err=""):
            state["running"] = False
            state["ready_install"] = False
            _set_status("")
            messagebox.showerror("更新失败", f"{msg}\n{err}" if err else msg, parent=dlg)
            # 恢复按钮
            for b in (btn_dl, btn_cancel):
                b.configure(state="normal")
            btn_dl.configure(text="重试")
        
        def _do_download():
            """后台线程：拉取资产 → 流式下载 → SHA256 校验 → 更新状态"""
            import subprocess, tempfile, json as _json, hashlib as _hashlib, re as _re
            from urllib.request import urlopen as _urlopen, Request as _Request
            
            class _CancelledDownload(Exception):
                """用户取消下载：静默中止，不弹错误"""
            
            try:
                # 1) 获取资产列表（多镜像测速选最快）
                from github_api import fetch_latest_release, mirror_download_url
                _tag, _body, assets = fetch_latest_release(timeout=15)
                
                exe_asset = None
                sha_asset = None
                # 跨版本策略：本地程序目录有固定名 PDD EZ.exe（v1.4+）→ 增量包；
                # 否则（旧版 PDD EZ vX.Y.exe）→ 全量包——旧版 _internal 结构可能不同，增量会崩
                local_main = os.path.join(
                    os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else get_base_dir(),
                    'PDD EZ.exe')
                use_incremental = os.path.exists(local_main)
                for a in assets:
                    name = a.get("name", "")
                    if use_incremental and name.endswith("_update.zip"):
                        exe_asset = a
                    elif not use_incremental and name.endswith(".zip") and "_update" not in name:
                        exe_asset = a
                if exe_asset:
                    for a in assets:
                        if a.get("name", "") == exe_asset["name"] + ".sha256":
                            sha_asset = a
                            break
                if not exe_asset:
                    # 兜底：增量找不到就全量，反之亦然（release 资产不全时保底）
                    for a in assets:
                        name = a.get("name", "")
                        if use_incremental and name.endswith(".zip") and "_update" not in name:
                            exe_asset = a
                            break
                        if not use_incremental and name.endswith("_update.zip"):
                            exe_asset = a
                            break
                    if exe_asset:
                        for a in assets:
                            if a.get("name", "") == exe_asset["name"] + ".sha256":
                                sha_asset = a
                                break
                if not exe_asset:
                    self.win.after(0, lambda: _fail("Release 中未找到更新包"))
                    return
                
                # 2) 下载到临时目录（流式 + 进度上报）
                tmp = os.path.join(tempfile.gettempdir(), "pdd_update")
                os.makedirs(tmp, exist_ok=True)
                asset_name = os.path.basename(exe_asset["name"].replace('\\', '/'))
                dest = os.path.join(tmp, asset_name)
                # 下载 URL：优先镜像（国内网络通常更快），失败回退官方直连
                url = mirror_download_url(exe_asset["browser_download_url"], prefer_mirror=True)
                fallback_url = exe_asset["browser_download_url"]
                total = exe_asset.get("size", 0)
                
                # 流式下载 + 进度上报（镜像/官方共用）
                def _download_stream(resp, dest, total, asset_name):
                    if total == 0:
                        try:
                            total = int(resp.headers.get('Content-Length', 0) or 0)
                        except Exception:
                            total = 0
                    downloaded = 0
                    last_pct = -1
                    with open(dest, 'wb') as f:
                        while True:
                            if state["cancelled"]:
                                f.close()
                                try:
                                    os.remove(dest)
                                except Exception:
                                    pass
                                raise _CancelledDownload()
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                pct = int(downloaded * 100 / total)
                                if pct != last_pct:
                                    last_pct = pct
                                    self.win.after(0, lambda p=pct, d=downloaded, t=total:
                                                   _set_status(f"正在下载 {asset_name} ({d//1024//1024}MB/{t//1024//1024}MB)...", p))
                    # v1.4.6（bug hunt F13 纵深）：记录实收字节数供大小终检（下载结束后）
                    state["downloaded_bytes"] = downloaded
                
                self.win.after(0, lambda: _set_status(f"正在下载 {asset_name}...", 0))
                try:
                    req = _Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "PDD-EZ"})
                    with _urlopen(req, timeout=120) as resp:
                        _download_stream(resp, dest, total, asset_name)
                except _CancelledDownload:
                    raise  # 取消透传，不尝试官方兜底
                except Exception:
                    # 镜像失败 → 官方直连兜底
                    req = _Request(fallback_url, headers={"Accept": "application/octet-stream", "User-Agent": "PDD-EZ"})
                    with _urlopen(req, timeout=120) as resp:
                        _download_stream(resp, dest, total, asset_name)
                
                # 3) SHA256 校验：发布物必须带 .sha256（v1.4.5 bug hunt F13 fail-closed）
                if not sha_asset:
                    # 无 .sha256 资产：拒绝安装（防截断/篡改包直通）
                    try:
                        os.remove(dest)
                    except Exception:
                        pass
                    self.win.after(0, lambda: _fail("缺少 SHA256 校验文件，已拒绝安装（安全策略）"))
                    return
                if sha_asset:
                    self.win.after(0, lambda: _set_status("正在校验下载完整性..."))
                    sha_path = dest + ".sha256"
                    try:
                        # v1.4.1 修复：sha 文件与主包同走镜像（国内客户 github 直连
                        # 不通时 sha 下载失败 → 之前直接拒绝安装，更新永远失败）
                        _sha_url = mirror_download_url(sha_asset["browser_download_url"], prefer_mirror=True)
                        _sha_fallback = sha_asset["browser_download_url"]
                        try:
                            req = _Request(_sha_url,
                                           headers={"Accept": "application/octet-stream", "User-Agent": "PDD-EZ"})
                            with _urlopen(req, timeout=30) as resp:
                                with open(sha_path, 'wb') as f:
                                    f.write(resp.read())
                        except Exception:
                            req = _Request(_sha_fallback,
                                           headers={"Accept": "application/octet-stream", "User-Agent": "PDD-EZ"})
                            with _urlopen(req, timeout=30) as resp:
                                with open(sha_path, 'wb') as f:
                                    f.write(resp.read())
                        expected = open(sha_path, 'r').read().strip().split()[0]
                        # v1.4.6（bug hunt F13 纵深）：sha 值必须是 64 位 hex（防 sha 文件是垃圾/非 hash 文本）
                        if not _re.fullmatch(r'[0-9a-fA-F]{64}', expected):
                            os.remove(dest)
                            self.win.after(0, lambda: _fail("SHA256 校验文件格式异常，已拒绝安装（安全策略）"))
                            return
                        h = _hashlib.sha256()
                        with open(dest, 'rb') as f:
                            while True:
                                if state["cancelled"]:
                                    raise _CancelledDownload()
                                c = f.read(65536)
                                if not c:
                                    break
                                h.update(c)
                        if h.hexdigest().lower() != expected.lower():
                            os.remove(dest)
                            self.win.after(0, lambda: _fail("SHA256 校验失败，更新包可能被篡改，已删除"))
                            return
                        os.remove(sha_path)
                    except _CancelledDownload:
                        raise  # 取消透传，不弹"校验文件下载失败"
                    except Exception as e:
                        os.remove(dest)
                        self.win.after(0, lambda _e=e: _fail("SHA256 校验文件下载失败，已拒绝安装（安全策略）", str(_e)))
                        return
                
                # v1.4.6（bug hunt F13 纵深）：声明大小 vs 实收大小终检（GitHub API size vs 下载字节数）
                _real_size = state.get("downloaded_bytes", 0)
                if total and _real_size and abs(_real_size - total) > 0:
                    os.remove(dest)
                    self.win.after(0, lambda: _fail(f"下载完整性异常（实收 {_real_size}B ≠ 声明 {total}B），已拒绝安装"))
                    return

                state["downloaded_zip"] = dest
                state["sha_ok"] = True
                self.win.after(0, lambda: _set_status("下载完成，准备安装...", 100))
                self.win.after(0, _ask_install)
                
            except _CancelledDownload:
                # 用户取消：静默返回，不弹错误
                return
            except Exception as e:
                self.win.after(0, lambda _e=e: _fail("下载失败，请检查网络", str(_e)))
        
        def _ask_install():
            """下载完成 → 确认安装 → 关窗拉起 updater finalize"""
            state["running"] = False
            state["ready_install"] = True
            for b in (btn_dl, btn_cancel):
                b.configure(state="normal")
            btn_dl.configure(text="立即安装")
            _set_status("下载完成，点击「立即安装」将关闭主程序并安装新版本", 100)
        
        def _do_install():
            """拉起 updater finalize：主程序即将退出，安装由独立更新器完成。
            关键：先把 updater 复制到 %TEMP% 再运行（对齐 auto 模式自我转移）——
            若直接在程序目录运行，更新包含新 updater 时 _ensure_self_renamed 无法
            rename 运行中的自身 exe（Windows ERROR_ACCESS_DENIED），更新会中止。"""
            import subprocess, tempfile, shutil
            updater = os.path.join(
                os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else get_base_dir(),
                'PDD EZ Updater.exe')
            if not os.path.exists(updater):
                messagebox.showerror("更新失败", "未找到更新器，请重新下载完整安装包", parent=dlg)
                return
            try:
                # 复制到 %TEMP% 再运行：target_dir 内无运行中的 updater，自身可被覆盖
                _tmp_updater = os.path.join(tempfile.gettempdir(), 'PDD_EZ_Updater_tmp.exe')
                shutil.copy2(updater, _tmp_updater)
                _cmd = [_tmp_updater, '--mode', 'finalize',
                        '--file', state["downloaded_zip"],
                        '--extract-dir', os.path.join(tempfile.gettempdir(), "pdd_update", "extracted"),
                        '--target', sys.executable,
                        '--wait-pid', str(os.getpid())]
                subprocess.Popen(_cmd)
                _set_status("更新器已启动，主程序即将关闭...")
                self.win.destroy()
            except Exception as e:
                messagebox.showerror("启动失败", f"无法启动更新器：{e}\n请手动下载最新版本", parent=dlg)
        
        def _do_click():
            if state["ready_install"]:
                _do_install()
                return
            if state["running"]:
                return
            state["running"] = True
            state["cancelled"] = False
            btn_dl.configure(state="disabled", text="更新中...")
            btn_cancel.configure(state="normal")
            _set_status("准备下载...", 0)
            threading.Thread(target=_do_download, daemon=True).start()
        
        def _cancel():
            if state["running"]:
                state["cancelled"] = True
                state["running"] = False
                _set_status("已取消")
                btn_dl.configure(state="normal", text="重试")
            else:
                dlg.destroy()
        
        btn_dl = self._mk_btn(btn_frame, "立即更新", _do_click, kind='primary',
                              font=self.FONT_BOLD, width=12, pack_side="left", padx=5)
        btn_cancel = self._mk_btn(btn_frame, "取消", _cancel, kind='ghost',
                                  font=self.FONT, width=12, pack_side="left", padx=5)
        
        # 下载完成时：按钮变为「立即安装」，点击拉起 updater
        btn_dl.configure(command=_do_click)
    
    def tc(self, path, default=None):
        """Token Resolver：读组件 token（'btn.primary.bg' → components.btn.primary.bg）、
        装饰 token（'decor.topbar.bg' → decor.topbar.bg）或语义色（'C_BG'），缺省兜底"""
        if path.startswith('C_'):
            return getattr(self, path, default)
        parts = path.split('.')
        if parts and parts[0] == 'decor':
            node = self._theme_spec.get('decor', {})
            parts = parts[1:]
        else:
            node = self._theme_spec.get('components', {})
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def _register_redraw(self, fn):
        """注册主题重绘回调（_apply_theme 末尾统一执行）"""
        if fn not in self._theme_redraws:
            self._theme_redraws.append(fn)

    # ── 动效 C/D/A/B 实现 + 熔断基建 ──

    def _pulse_status(self, accent_color=None):
        """t9 C：状态反馈脉冲——status_label fg 两跳（current→accent→current，~300ms）。

        必须用 _register_redraw 兼容主题：脉冲后回到**当前主题 fg**（_tc 实时查）而非构造时快照。
        全异常吞掉（动效绝不影响主流程）。
        """
        if not ANIMATIONS_ENABLED:
            return
        try:
            _lbl = getattr(self, 'status_label', None)
            if _lbl is None:
                return
            try:
                if not _lbl.winfo_exists():
                    return
            except Exception:
                return
            # 取消同 label 上挂着的旧 after job（v4f：先 snap 终态再 cancel）
            _key = ('pulse_status', id(_lbl))
            _cancel_after_jobs(self.win, self._anim_jobs.get(_key, []))
            # 终态色 = 当前主题 C_TEXT（实时查，主题切换不偏色）
            _end = self.tc('C_TEXT', '#1F1F1F')
            _accent = accent_color or self.tc('C_PRIMARY', '#FFE600')
            if not (_end.startswith('#') and _accent.startswith('#')):
                return
            # 两跳：T=0 步 end→accent；T=150ms 步 accent→end；每步 16ms 插值
            _steps = 6  # 6 步×16ms ≈ 100ms/跳，合计 ~200ms
            # b：起拍时捕获 label 当前 fg，终态回到该值（非硬编码 C_TEXT）——
            # 原 status_label fg=C_MUTED，脉冲回 C_TEXT 会让状态栏永久变深
            try:
                _start_fg = str(_lbl.cget('fg'))
            except Exception:
                _start_fg = _end
            def _do(step, sub):
                try:
                    if not _lbl.winfo_exists():
                        return
                    if step >= _steps:
                        # a：终态实时 self.tc 查主题，不复用启动快照
                        _cur_end = self.tc('C_TEXT', '#1F1F1F')
                        _cur_start = _start_fg if _start_fg.startswith('#') else _cur_end
                        _lbl.configure(fg=_cur_start if sub == 1 else _cur_end)
                        self._anim_jobs[_key] = []
                        return
                    t = step / float(_steps)
                    # a：每步实时 self.tc 查主题（200ms 内切主题不残留旧色）
                    _cur_end = self.tc('C_TEXT', '#1F1F1F')
                    _cur_accent = self.tc('C_PRIMARY', '#FFE600') if not accent_color else accent_color
                    if sub == 0:
                        c = _lerp_hex(_cur_end, _cur_accent, t)
                    else:
                        c = _lerp_hex(_cur_accent, _cur_end, t)
                    _lbl.configure(fg=c)
                    jid = self.win.after(16, lambda: _do(step + 1, sub))
                    self._anim_jobs.setdefault(_key, []).append(jid)
                except Exception:
                    # 修：真熔断——动效回调异常时翻 ANIMATIONS_ENABLED=False
                    try:
                        self._anim_jobs[_key] = []
                    except Exception:
                        pass
                    _meltdown_animations()
            _do(0, 0)
            # 第二跳：用 after 延迟 100ms 后启动
            _jid = self.win.after(100, lambda: _do(0, 1))
            self._anim_jobs.setdefault(_key, []).append(_jid)
        except Exception:
            _meltdown_animations()

    def _set_batch_btn_state(self, busy):
        """t9 D：批量期间按钮禁用+文案变化补全。

        busy=True：禁用 export_btn/live_btn，文案改"导出中…"/"识别中…"
        busy=False：恢复 normal 与原文案
        """
        try:
            # R1 布局B：受控按钮清单抽为模块级 _BATCH_BUSY_BTNS，忙时文案映射
            # busy_label_for 纯函数（导出→导出中…；截图→截图中…），test_home_layout 可单测
            for _attr, _orig_text, _bkey in _BATCH_BUSY_BTNS:
                _b = getattr(self, _attr, None)
                if _b is None:
                    continue
                try:
                    if not _b.winfo_exists():
                        continue
                except Exception:
                    continue
                if busy:
                    # 记录原文案供恢复（v4f：仅首次记录，避免重复 set 覆盖）
                    if not hasattr(_b, '_orig_text'):
                        try:
                            _b._orig_text = _b.cget('text')
                        except Exception:
                            _b._orig_text = _orig_text
                    _b.configure(state='disabled')
                    try:
                        from home_actions import busy_label_for
                        _b.configure(text=busy_label_for(_bkey, _b._orig_text))
                    except Exception:
                        _b.configure(text=busy_btn_text(_b._orig_text))
                else:
                    _b.configure(state='normal')
                    if hasattr(_b, '_orig_text'):
                        _b.configure(text=_b._orig_text)
                        try:
                            del _b._orig_text
                        except Exception:
                            pass
        except Exception:
            pass
        # R2 批次C：批量中店铺切换器同步禁用（互斥从「点了才拒绝」升级为「看得见
        # 不可点」）；_on_store_switch 的 resolve_store_switch busy 拒绝仍是权威守卫
        try:
            _combo = getattr(self, 'store_combo', None)
            if _combo is not None and _combo.winfo_exists():
                _combo.configure(state='disabled' if busy else 'readonly')
        except Exception:
            pass

    # ── R1 布局优化B：批量进度可视化（TaskQueue on_progress → 进度条 + 状态栏）──

    def _on_batch_progress(self, percent, stage):
        """t5/R1 布局B：TaskQueue on_progress 回调（worker 线程触发）。

        线程契约（async_queue）：所有回调在 worker 线程触发，Tk 操作必须
        win.after 调度回主线程——这里只做转发，落地在 _apply_batch_progress。
        """
        self.win.after(0, lambda p=percent, s=stage: self._apply_batch_progress(p, s))

    def _apply_batch_progress(self, percent, stage):
        """批量进度落地（主线程）：百分比进进度条，阶段文案映射进状态栏。

        R2 批次C：stage 与百分比联动——地区标题行（── [广东] (2/5) ──）更新当前
        地区上下文并显示「完成/开始」联动文案；普通阶段行带地区前缀
        「⏳ 批量识别 N%｜广东 · 阶段短语」。
        """
        try:
            pct = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            pct = 0
        try:
            bar = getattr(self, 'batch_progress', None)
            if bar is not None and bar.winfo_exists():
                bar.configure(value=pct)
        except Exception:
            pass
        try:
            _hdr = batch_region_header(stage)
            if _hdr:
                self._batch_region_label = _hdr[0]
            self.status_text.set(progress_status_text(
                pct, stage, getattr(self, '_batch_region_label', '')))
        except Exception:
            pass

    def _begin_batch_progress(self):
        """批量开始（主线程）：进度条归零并插到状态栏上方（未 pack 过则兜底 pack）。"""
        try:
            bar = getattr(self, 'batch_progress', None)
            if bar is None or not bar.winfo_exists():
                return
            bar.configure(value=0, maximum=100)
            self._batch_region_label = ''  # R2 批次C：地区上下文随批次复位
            try:
                bar.pack(fill='x', padx=15, pady=(0, 4), before=self.status_label)
            except Exception:
                bar.pack(fill='x', padx=15, pady=(0, 4))
        except Exception:
            pass

    def _reset_batch_progress(self):
        """批量收尾/异常/F9 取消（主线程）：隐藏进度条并复位 0。"""
        try:
            bar = getattr(self, 'batch_progress', None)
            if bar is None or not bar.winfo_exists():
                return
            bar.configure(value=0)
            bar.pack_forget()
        except Exception:
            pass

    def _poll_cancel_restore(self, task_id, ticks=0):
        """F9 紧急停止后的 UI 恢复监视（R1 布局B，主线程 after 轮询）。

        缺口：TaskQueue 对 cancelled 任务不再回调 on_done/on_error
        （async_queue._run_task 的协作取消检查点直接 return），而 _poll_batch_queue
        仅由 on_done 启动——F9 后导出/截图按钮会永远卡在「…中…」禁用态、进度条
        不复位。这里轮询 task_status 终态后统一复位（进度条隐藏归零 + 按钮恢复 +
        状态栏提示）；60 轮（≈24s）未见终态也强制复位，防状态查询异常卡死恢复。
        """
        try:
            st = self._task_queue.task_status(task_id)
        except Exception:
            st = 'cancelled'
        if st in ('cancelled', 'done', 'error') or ticks >= 60:
            self._reset_batch_progress()
            self._set_batch_btn_state(False)
            if st != 'done':
                # 'done'：on_done → _finish_batch 已正常收尾（含完成弹窗），不覆盖其文案
                self.status_text.set("⏹ 已紧急停止 — 批量识别已中断，可重新开始")
            return
        self.win.after(400, lambda: self._poll_cancel_restore(task_id, ticks + 1))

    def _animate_btn_hover(self, btn, enter):
        """t9 A：按钮 hover 5 步插值（_mk_btn 统一接入）。

        v4f 修正：每按钮独立 after job 句柄（id(btn) 为键）；disabled 态抢先落终态不动画。
        """
        if not ANIMATIONS_ENABLED:
            return
        try:
            if btn is None:
                return
            if getattr(btn, '_state', None) == 'disabled':
                return  # 禁用态抢先落终态不动画
            canvas = getattr(btn, 'canvas', None) or getattr(btn, '_canvas', None)
            poly = getattr(btn, 'poly', None)
            if canvas is None or poly is None:
                return
            _colors = btn._colors
            _end = _colors.get('bg', '#FFE600')
            _hov = _colors.get('bg_hover', _end)
            _a, _b = (_end, _hov) if enter else (_hov, _end)
            if _a == _b:
                return
            _key = ('btn_hover', id(btn))
            # 取消旧 after job（v4f：先 snap 终态再 cancel）
            _cancel_after_jobs(self.win, self._anim_jobs.get(_key, []))
            _steps = 5
            def _do(step):
                try:
                    if not canvas.winfo_exists():
                        return
                    if getattr(btn, '_state', None) == 'disabled':
                        # 修：disabled snap 到禁用色（tc 主题键+fallback 模式，非新 token），
                        # 避免动画中变禁用的按钮被涂回正常色
                        _disabled = self.tc('btn.disabled', self.C_SURFACE)
                        canvas.itemconfigure(poly, fill=_disabled)
                        self._anim_jobs[_key] = []
                        return
                    if step >= _steps:
                        canvas.itemconfigure(poly, fill=_b)
                        self._anim_jobs[_key] = []
                        return
                    t = step / float(_steps)
                    c = _lerp_hex(_a, _b, t)
                    canvas.itemconfigure(poly, fill=c)
                    jid = self.win.after(16, lambda: _do(step + 1))
                    self._anim_jobs.setdefault(_key, []).append(jid)
                except Exception:
                    # 修：真熔断
                    _meltdown_animations()
            _do(0)
        except Exception:
            _meltdown_animations()

    def _animate_nav_leave(self, btn):
        """t9 B：导航选中过渡——选中位立即跳变（一点即用），离开位 6 步 100ms 渐隐回 C_BG。

        调用时机：_highlight_nav 即将把旧高亮按钮改回 C_BG 时。
        t11 ③修：导航按钮是 tk.Button（无 canvas 属性），不能用 canvas gate；
        _do 内只用 btn.configure，对 tk.Button 本就可用。_highlight_nav 已先
        configure(bg=C_BG) 兜底，本函数失败也只是动画效果缺失，不影响最终视觉。
        """
        if not ANIMATIONS_ENABLED:
            return
        try:
            if btn is None:
                return
            try:
                if not btn.winfo_exists():
                    return
            except Exception:
                return
            _bg = self.tc('C_BG', '#FFFFFF')
            _key = ('nav_leave', id(btn))
            _cancel_after_jobs(self.win, self._anim_jobs.get(_key, []))
            _steps = 6
            def _do(step):
                try:
                    if not btn.winfo_exists():
                        return
                    if step >= _steps:
                        btn.configure(bg=_bg, fg=self.tc('C_TEXT', '#1F1F1F'))
                        self._anim_jobs[_key] = []
                        return
                    t = step / float(_steps)
                    c = _lerp_hex(self.tc('C_CARD_HDR', '#1F1F1F'), _bg, t)
                    btn.configure(bg=c)
                    jid = self.win.after(16, lambda: _do(step + 1))
                    self._anim_jobs.setdefault(_key, []).append(jid)
                except Exception:
                    # 修：真熔断
                    _meltdown_animations()
            _do(0)
        except Exception:
            _meltdown_animations()

    def _apply_theme(self, name):
        """应用皮肤：更新类属性 + 递归刷新所有控件颜色 + 重绘注册元素"""
        theme = THEMES.get(name, THEMES['终末地'])
        self._theme_name = name
        self._theme_spec = _merge_theme(theme)
        
        # 窗口可能已在切换/关闭过程中销毁，walk 前先确认存活（防 TclError）
        try:
            if not self.win.winfo_exists():
                return
        except Exception:
            return
        
        # 记录旧色 → 新色映射（用于 tk 控件递归替换）
        old_colors = {}
        for k in theme:
            if k.startswith('C_'):
                old_colors[k] = getattr(self, k, None)
        
        # 更新类属性
        for k, v in theme.items():
            if k.startswith('C_'):
                setattr(self, k, v)
        
        # 根窗口显式设色（Tk 默认系统色无法被 walk 匹配）
        self.win.configure(bg=theme['C_BG'],
                          highlightthickness=0)  # 去掉窗口白边
        
        # ── 第一遍：颜色映射替换 ──
        def _walk_color(w):
            if getattr(w, '_skip_theme', False):
                return
            # ttk 控件由 _update_ttk_theme 统一管理，跳过避免 TclError；
            # 但 Toplevel（弹窗）类名也以 T 开头，不是 ttk，需排除
            if w.winfo_class().startswith('T') and w.winfo_class() != 'Toplevel':
                return
            for attr in ('bg', 'fg', 'highlightbackground', 'highlightcolor',
                         'activebackground', 'selectbackground', 'selectforeground'):
                try:
                    cur = w.cget(attr)
                    if cur:
                        for a_key, old_v in old_colors.items():
                            if old_v and cur.upper() == old_v.upper():
                                w.configure(**{attr: theme[a_key]})
                                break
                except:
                    pass  # 个别控件不支持该属性，忽略
            for child in w.winfo_children():
                _walk_color(child)
        
        _walk_color(self.win)
        
        # ── 第二遍：系统默认控件强制设色 ──
        def _walk_system(w):
            if getattr(w, "_skip_theme", False):
                return
            cls = w.winfo_class()
            try:
                if cls in ('Entry', 'Spinbox'):
                    w.configure(bg=theme['C_SURFACE'], fg=theme['C_TEXT'],
                               insertbackground=theme['C_TEXT'],
                               selectbackground=theme['C_SECONDARY'],
                               selectforeground='#FFFFFF',
                               highlightbackground=theme['C_BORDER'])
                elif cls == 'Canvas':
                    w.configure(bg=theme['C_BG'])
                elif cls == 'Listbox':
                    w.configure(bg=theme['C_SURFACE'], fg=theme['C_TEXT'],
                               selectbackground=theme['C_SECONDARY'])
            except:
                pass
            for child in w.winfo_children():
                _walk_system(child)
        
        _walk_system(self.win)
        
        # ── 第三遍：强制覆盖继承/未匹配的控件 ──
        def _walk_force(w, parent_bg):
            cls = w.winfo_class()
            try:
                if cls == 'Frame':
                    if getattr(w, '_skip_theme', False):
                        # 子控件应跟随该 Frame 自身底色（如导航栏 C_SURFACE），而非外层 parent_bg
                        try:
                            actual_bg = w.cget('bg')
                        except Exception:
                            actual_bg = parent_bg
                    else:
                        try:
                            hl = w.cget('highlightthickness')
                            if hl and int(hl) > 0:
                                w.configure(bg=theme['C_SURFACE'])
                            else:
                                w.configure(bg=theme['C_BG'])
                        except:
                            w.configure(bg=theme['C_BG'])
                        actual_bg = theme['C_BG']
                elif cls == 'Label':
                    if getattr(w, '_skip_theme', False):
                        # 跳过标记的容器内 Label：保留其定制 fg（如标题栏白字、徽章色）
                        try:
                            w.configure(bg=parent_bg)
                        except Exception:
                            pass
                    else:
                        # 普通 Label：只设背景跟随父级；fg 已由 _walk_color 按旧→新映射处理，
                        # 不再强制覆盖 C_TEXT，避免标题栏白字被刷黑（白底/深色主题下看不清）
                        try:
                            w.configure(bg=parent_bg)
                        except Exception:
                            pass
                    actual_bg = parent_bg
                elif cls == 'Button':
                    # 保持功能性按钮颜色，但设默认底色
                    pass
                else:
                    actual_bg = parent_bg
            except:
                actual_bg = parent_bg
            for child in w.winfo_children():
                _walk_force(child, actual_bg)
        
        _walk_force(self.win, theme['C_BG'])
        
        # ── ttk 皮肤 ──
        if hasattr(self, 'tree'):
            self._update_ttk_theme(theme)
            self._refresh_tree_tags()
        
        # ── 重绘注册表（Canvas 装饰/按钮跟随主题）──
        for fn in self._theme_redraws:
            try:
                fn()
            except Exception:
                pass
    
    def _update_ttk_theme(self, theme):
        """更新全部 ttk 控件颜色（Treeview, Combobox, Notebook, Scrollbar 等）"""
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except:
            pass
        
        # 全局默认
        style.configure('.',
            background=theme['C_BG'],
            foreground=theme['C_TEXT'],
            fieldbackground=theme['C_BG'],
            troughcolor=theme['C_BG'],
            bordercolor=theme['C_BORDER'],
            lightcolor=theme['C_BG'],
            darkcolor=theme['C_MUTED'],
            arrowcolor=theme['C_TEXT'])
        
        # Treeview（终末地扁平：纯白内容区，细边框，无立体）
        style.configure('Treeview',
            background=theme['C_BG'],
            foreground=theme['C_TEXT'],
            fieldbackground=theme['C_BG'],
            borderwidth=1,
            relief='solid')
        style.configure('Treeview.Heading',
            background=theme['C_PRIMARY'],
            foreground='#FFFFFF',
            font=self.FONT_BOLD,
            relief='flat')
        style.map('Treeview',
            background=[('selected', theme['C_SECONDARY'])],
            foreground=[('selected', '#FFFFFF')])
        style.map('Treeview.Heading',
            background=[('active', theme['C_SECONDARY'])])
        
        # Combobox（白底统一：页面同色，边框区分输入区）
        style.configure('TCombobox',
            fieldbackground=theme['C_BG'],
            background=theme['C_BG'],
            foreground=theme['C_TEXT'],
            arrowcolor=theme['C_TEXT'])
        style.map('TCombobox',
            fieldbackground=[('readonly', theme['C_BG'])],
            foreground=[('readonly', theme['C_TEXT'])],
            background=[('readonly', theme['C_BG'])])
        
        # Notebook (设置里的标签栏)
        style.configure('TNotebook',
            background=theme['C_BG'],
            borderwidth=0,
            tabmargins=[2, 2, 2, 0])
        style.configure('TNotebook.Tab',
            background=theme['C_BLUE_LIGHT'],
            foreground=theme['C_TEXT'],
            padding=[12, 4],
            font=self.FONT)
        style.map('TNotebook.Tab',
            background=[('selected', theme['C_PRIMARY'])],
            foreground=[('selected', '#FFFFFF')],
            expand=[('selected', [1, 1, 1, 0])])
        
        # Scrollbar（终末地扁平：细黑线条，无箭头块）
        style.configure('Vertical.TScrollbar',
            background=theme['C_PRIMARY'],
            troughcolor=theme['C_BG'],
            arrowcolor=theme['C_BG'],
            borderwidth=0, relief='flat',
            arrowsize=8, width=8)
        style.configure('Horizontal.TScrollbar',
            background=theme['C_PRIMARY'],
            troughcolor=theme['C_BG'],
            arrowcolor=theme['C_BG'],
            borderwidth=0, relief='flat',
            arrowsize=8, width=8)
        
        # Frame / Label
        style.configure('TFrame', background=theme['C_BG'])
        style.configure('TLabelframe', background=theme['C_BG'])
        style.configure('TLabelframe.Label', background=theme['C_BG'], foreground=theme['C_TEXT'])
        style.configure('TLabel', background=theme['C_BG'], foreground=theme['C_TEXT'])
        
        # Spinbox
        style.configure('TSpinbox',
            fieldbackground=theme['C_SURFACE'],
            background=theme['C_SURFACE'],
            foreground=theme['C_TEXT'],
            arrowcolor=theme['C_TEXT'])
    
    def _refresh_tree_tags(self):
        """刷新 Treeview 行颜色标签"""
        self.tree.tag_configure('urgent', background=self.C_RED_BG)
        self.tree.tag_configure('warning', background=self.C_YELLOW_BG)
    
    def _save_regions(self):
        """t6：regions 原子写走 store_registry（按当前店铺键落盘，其他店铺数据保留）。

        旧实现（tmp + os.replace）已内聚进 store_registry._write_regions_file；
        所有既有调用点（设置页删地区 / 识别新增地区等）行为不变，只是落到当前店铺。
        失败显式提示状态栏（宪法 §4：不静默——store_registry 内部已写诊断日志）。
        """
        saved = False
        try:
            if store_registry is not None:
                saved = bool(store_registry.save_regions(
                    self.regions, getattr(self, '_store_id', None)))
        except Exception:
            saved = False
        if not saved:
            try:
                self.status_text.set("⚠ 时效配置保存失败（详见日志 ocr_dlog.txt）")
            except Exception:
                pass
        return saved
    
    def _get_shipping(self, region, product_name):
        """获取某个地区某个商品的运输天数，未设置则默认 3 天"""
        region_data = self.regions.get(region, {})
        if isinstance(region_data, dict):
            # product 名优先，回退旧格式默认天数（"" 键），再回退全局默认 3
            return region_data.get(product_name, region_data.get('', 3))
        return 3  # 兼容旧格式
    
    def _build_safety_recommendation(self, history_rows, lead_days, name='',
                                     sku_id='', region='', forecast=None):
        """R2 预测：单商品安全库存推荐 → 推荐缓存 payload（无推荐返回 None）。

        t5 契约：recommend_safety_days None=数据不足不强给（§4），本层不编数；
        σ/样本数经 t5 的日销聚合 _history_to_daily_series 取【同一份 30 天序列】
        计算（σ 用 ddof=1，与 t5 推荐公式同源），保证设置页展示与推荐值一致。
        payload 键结构 = algorithm_ui.save_recommendation_cache 白名单
        （settings['replenishment']['recommendation']），设置页读取展示 + 一键应用。
        """
        try:
            from algorithm_ui import (recommend_safety_days as _rsd,
                                      _history_to_daily_series as _h2s,
                                      DEFAULT_SAFETY_Z)
            sd = _rsd(history_rows, lead_days)
            if not sd:
                return None
            payload = {
                'safety_days': int(sd),
                'safety_days_lead': int(lead_days or 0),
                'sigma': 0.0,
                'forecast': float(forecast) if forecast is not None else 0.0,
                'n_samples': 0,
                'z': float(DEFAULT_SAFETY_Z),
                'computed_at': datetime.now().isoformat(timespec='seconds'),
                # sku_key：推荐基于哪个商品（无 ID 回退 region+name，与历史库关联键同语义）
                'sku_key': (sku_id or '') or f'{region}+{name}',
            }
            try:
                series = _h2s(history_rows, 30) or []
                payload['n_samples'] = len(series)
                if len(series) >= 2:
                    import statistics as _stat
                    payload['sigma'] = round(_stat.stdev(series), 4)  # ddof=1 同
            except Exception:
                pass
            return payload
        except Exception:
            return None

    def _calc_from_items(self, items):
        """直接从OCR结果计算并显示（v1.3 动态列：勾选列 + 固定计算列）"""
        today = datetime.now()
        region = self.region_var.get()
        plans = []
        # 读取客户勾选的识别列（未配置/空 → 回退默认商品字段列）
        try:
            from utils import get_ocr_columns
            _col_cfg = get_ocr_columns()
            _sel_cols = [c for c in (_col_cfg.get('selected') or []) if c]
        except Exception:
            _col_cfg = {}
            _sel_cols = []
        if not _sel_cols:
            _sel_cols = ['商品信息', '仓库总库存', '仓库预估总销售数']
        # mapping 反查：列名 → 业务字段（渲染动态列时把 name/stock/sales 放回对应勾选列）
        try:
            _sel_cols_map = {v: k for k, v in ((_col_cfg.get('mapping') or {}).items()) if v}
        except Exception:
            _sel_cols_map = {}

        # 防御性转换：兼容字符串/None/含单位文本，避免 ValueError/TypeError（循环外定义避免重复建函数）
        def _to_int(v, default=0):
            try:
                return int(v)
            except (ValueError, TypeError):
                return default

        # 补货时间偏移量：默认 1，可由 settings.replenishment_offset 覆盖（循环外读一次，避免每迭代 IO）
        try:
            from utils import Config as _Cfg
            _off = int(_Cfg.load().get('replenishment_offset', 1))
        except Exception:
            _off = 1

        # P3-A：补货模型分发（用户裁定：默认 classic，原公式一行不改）
        try:
            from utils import get_replenishment_cfg
            _rep_cfg = get_replenishment_cfg()
        except Exception:
            _rep_cfg = {'model': 'classic', 'safety_days': 2, 'in_transit_qty': 0}
        _rep_model = str(_rep_cfg.get('model') or 'classic')
        _rep_safety = int(_rep_cfg.get('safety_days', 2) or 0)
        _rep_intransit = int(_rep_cfg.get('in_transit_qty', 0) or 0)

        # history_lookup 适配 history_db.query_sku_history 签名。
        # 起加权/高级模式专用；R2 预测起经典模式也注入——「预测」列不依赖
        # 补货模型（每商品查近 30 天历史 → forecast_next_period）。查询体自带
        # try/except 兜底 []，经典公式本身一行未改（铁律不受影响）。
        def _history_lookup(sku_id, reg, days, name=None):
            try:
                import history_db as _hdb
                if sku_id:
                    return _hdb.query_sku_history(sku_key=sku_id, days=days, region=reg, name='') or []
                return _hdb.query_sku_history(sku_key='', days=days, region=reg, name=name or '') or []
            except Exception:
                return []

        # R2 预测：安全库存推荐缓存 payload（首个有足够历史的商品命中后不再覆盖——
        # 推荐基于"数据最充分的代表商品"，settings 设置页展示 + 一键应用）
        _rec_payload = None

        for item in items:
            name = item.get('name', '')
            stock = _to_int(item.get('stock', 0))
            daily = max(_to_int(item.get('sales', 0)), 0)
            calc_daily = daily if daily > 0 else 1  # 除法保护，显示保留原始值
            # R2 问题：_adv_plan 逐商品显式初始化（替代 dict 字面量里借
            # 局部命名空间做字符串查找的旧写法——改名即静默回退 1.0 的埋雷）
            _adv_plan = None
            # 每商品用自己的地区查时效（批量识别多省份各行 region 不同），
            # 无则回退当前地区——修复其他省份商品被按最后省份计算
            _it_region = item.get('region') or region
            shipping = self._get_shipping(_it_region, name)  # 逐商品查运输时效

            if _rep_model == 'weighted':
                # P3-A：加权模式——走 utils.calc_replenishment_weighted（任何异常回退经典）
                try:
                    from utils import calc_replenishment_weighted
                    _w = calc_replenishment_weighted(
                        item, _it_region, shipping, _rep_safety, _rep_intransit,
                        _history_lookup,
                    )
                    status = _w['status']
                    color = _w['color']
                    qty = _w['qty']
                    ratio = _w['ratio']
                    reorder = _w['reorder']
                    daily = _w.get('daily', daily)
                    _model_tag = _w.get('model', 'weighted')
                except Exception:
                    # 加权模式异常 → 经典公式兜底。
                    # 标注 'classic(error)' 与 'classic(no_history)' 区分
                    # - no_history：加权查到空结果，主动回退（已知语义）
                    # - error：加权抛异常（DB 损坏/字段缺失/未知），是被动回退
                    _model_tag = 'classic(error)'
                    if daily <= 0:
                        status = '无销量·观察'
                        color = 'gray'
                        qty = 0
                        ratio = 0.0
                        reorder = 0.0
                    else:
                        ratio = stock / calc_daily
                        lead_time = shipping + _off
                        reorder = ratio - lead_time
                        if reorder <= 0:
                            status = '立刻补货'
                            color = 'red'
                            qty = max(daily * 8, 100)
                            qty = ((qty + 99) // 100) * 100
                        elif reorder <= 2:
                            status = f'{reorder:.0f}天后下单'
                            color = 'yellow'
                            qty = max(daily * 8, 100)
                            qty = ((qty + 99) // 100) * 100
                        else:
                            status = f'{reorder:.0f}天后下单'
                            color = 'green'
                            qty = 0
            elif _rep_model == 'advanced':
                # 高级模式：走 algorithm_ui.dispatch_plan 统一封装
                # （内部调 utils.calc_replenishment_advanced，缺历史逐商品回退经典 + 标注 classic(error)）
                try:
                    from algorithm_ui import dispatch_plan
                    _adv_plan = dispatch_plan(
                        item, _it_region, shipping,
                        _rep_cfg, _history_lookup,
                    )
                    status = _adv_plan['status']
                    color = _adv_plan['color']
                    qty = _adv_plan['qty']
                    ratio = _adv_plan['ratio']
                    reorder = _adv_plan['reorder']
                    # 高级模式 daily 字段已等于 effective_daily（与基础 classic 兼容字段约定一致）
                    daily = _adv_plan.get('daily', daily)
                    _model_tag = _adv_plan.get('model', 'advanced')
                except Exception:
                    # dispatch_plan 已自带兜底；这里是双保险（理论上不该走到）
                    _model_tag = 'classic(error)'
                    if daily <= 0:
                        status = '无销量·观察'
                        color = 'gray'
                        qty = 0
                        ratio = 0.0
                        reorder = 0.0
                    else:
                        ratio = stock / calc_daily
                        lead_time = shipping + _off
                        reorder = ratio - lead_time
                        if reorder <= 0:
                            status = '立刻补货'
                            color = 'red'
                            qty = max(daily * 8, 100)
                            qty = ((qty + 99) // 100) * 100
                        elif reorder <= 2:
                            status = f'{reorder:.0f}天后下单'
                            color = 'yellow'
                            qty = max(daily * 8, 100)
                            qty = ((qty + 99) // 100) * 100
                        else:
                            status = f'{reorder:.0f}天后下单'
                            color = 'green'
                            qty = 0
            else:
                # 经典模式（用户裁定：一行公式都不许改）——原样保留
                _model_tag = 'classic'
                if daily <= 0:
                    # 无销量商品：不强制补货，标记观察（销量0可能数据未更新，交客户人工判断）
                    status = '无销量·观察'
                    color = 'gray'
                    qty = 0
                    ratio = 0.0
                    reorder = 0.0
                else:
                    ratio = stock / calc_daily
                    lead_time = shipping + _off
                    reorder = ratio - lead_time

                    if reorder <= 0:
                        status = '立刻补货'
                        color = 'red'
                        qty = max(daily * 8, 100)
                        qty = ((qty + 99) // 100) * 100
                    elif reorder <= 2:
                        status = f'{reorder:.0f}天后下单'
                        color = 'yellow'
                        qty = max(daily * 8, 100)
                        qty = ((qty + 99) // 100) * 100
                    else:
                        status = f'{reorder:.0f}天后下单'
                        color = 'green'
                        qty = 0

            plans.append({
                'name': name, 'stock': stock,
                'daily': daily, 'ratio': round(ratio, 1),
                'days_left': round(ratio, 1),
                'status': status, 'color': color, 'qty': qty,
                '_row_idx': len(plans),  # 原始 rows 索引（筛选/排序后编辑仍回写正确行）
                'warehouse': item.get('warehouse', ''),
                # v1.4.7 WS-A（A1）：sku_id 补进 plans——历史库 SKU 权威关联键
                # （与 ocr.dedup_items 同语义；无 ID 行由历史库回退 (region, name) 匹配）
                'sku_id': item.get('sku_id', '') or '',
                # 通用列原始数据：客户勾选列从 _raw 取原文显示
                '_raw': item.get('_raw') or {},
                '_sel_cols': _sel_cols,
                '_sel_cols_map': _sel_cols_map,
                # P3-A / F1：补货模型标注
                # classic = 经典模式（默认）
                # weighted = 加权模式（成功查到历史）
                # advanced = 高级模式（季节/大促/滞销/超卖四因子）
                # classic(no_history) = 加权模式主动回退（查到空结果）
                # classic(error) = 加权/高级模式被动回退（异常/DB 损坏）
                'model': _model_tag,
                # 预警列：表格 + 导出共用。滞销⚠/超卖🔥/超卖⚠/低置信⚠（不互斥，' / ' 分隔）
                'warning': '',
            })
            # 高级模式附加字段（R2 问题：_adv_plan 显式初始化 + isinstance 收敛——
            # dispatch_plan 异常或内部 classic(error) 回退时 _adv_plan 为 None/缺字段，
            # 占位值与 model 标注语义一致，不再借局部命名空间字符串查找静默兜底）
            _adv = _adv_plan if isinstance(_adv_plan, dict) else {}
            plans[-1]['season_factor'] = _adv.get('season_factor', 1.0) \
                if _rep_model == 'advanced' else 1.0
            plans[-1]['promo_multiplier'] = _adv.get('promo_multiplier', 1.0) \
                if _rep_model == 'advanced' else 1.0
            plans[-1]['effective_daily'] = _adv.get('effective_daily', daily) \
                if _rep_model == 'advanced' else daily
            plans[-1]['slow_moving'] = _adv.get('slow_moving', False) \
                if _rep_model == 'advanced' else False
            plans[-1]['oversell_risk'] = _adv.get('oversell_risk', False) \
                if _rep_model == 'advanced' else False
            plans[-1]['oversell_level'] = _adv.get('oversell_level', None) \
                if _rep_model == 'advanced' else None
            # 补齐 warning 字段：经典/加权模式不显示高级因子预警，只看低置信
            try:
                from algorithm_ui import warning_display as _wd
                plans[-1]['warning'] = _wd(plans[-1], item)
            except Exception:
                plans[-1]['warning'] = ''
            # R2 预测：每商品查近 30 天历史 → forecast_next_period
            # 预测下一期日销（plan['forecast'] float|None；None=样本不足显示 '—'）。
            # 顺带做安全库存推荐（首个数据足够的商品，供设置页展示+应用）。
            _fc = None
            try:
                from algorithm_ui import forecast_next_period as _fnp
                _hrows = _history_lookup(item.get('sku_id', '') or '', _it_region, 30,
                                         name=name)
                if _hrows:
                    _fc = _fnp(_hrows)
                    if _rec_payload is None:
                        _rec_payload = self._build_safety_recommendation(
                            _hrows, shipping, name=name,
                            sku_id=item.get('sku_id', '') or '', region=_it_region,
                            forecast=_fc)
            except Exception:
                _fc = None  # 预测失败不阻塞计算主链（§4：列显示 '—' 即显式语义）
            plans[-1]['forecast'] = _fc
        
        # Sort
        priority = {'red': 0, 'yellow': 1, 'green': 2}
        plans.sort(key=lambda p: priority.get(p['color'], 99))
        
        # Show（按筛选状态渲染，内部重建动态列）
        self._render_tree(plans)
        
        self.plans = plans
        # R2 预测：安全库存推荐缓存写入（键 settings['replenishment']['recommendation']，
        # save_recommendation_cache 白名单落盘）+ 状态栏简报一行。
        # 写失败不阻塞计算（设置页只是少一次"上次推荐"展示，§4 不弹窗打断）。
        _brief = ''
        if _rec_payload:
            try:
                from algorithm_ui import save_recommendation_cache as _src
                _src(_rec_payload)
            except Exception:
                pass
            # R3 遗留修复（R2-Leftover-1）：写缓存后刷新设置页「上次推荐」展示
            # （settings_ui.SettingsUIMixin._refresh_recommendation，hasattr 守卫
            # 兼容未挂 mixin/单测替身；设置页没构造时静默跳过）。
            try:
                if hasattr(self, '_refresh_recommendation'):
                    self._refresh_recommendation()
            except Exception:
                pass
            _brief = safety_brief_text(_rec_payload.get('safety_days'))
        self.status_text.set(f"计算完成 — {len(plans)} 个商品"
                             + ("（仅显示预警）" if self._filter_warning_only else "")
                             + (f"｜{_brief}" if _brief else ""))
        self.export_btn.config(state="normal")
        self._sort_col = None
        self._auto_expand(len(plans))
        
        # 保存到缓存
        region = self.region_var.get()
        self.active_region = region
        self.cache[region] = {'plans': plans, 'items': items}
        self._update_tabs()
    
    def _render_tree(self, plans):
        """按当前筛选状态把 plans 渲染到结果表（支持“仅显示预警”筛选）"""
        from utils import get_ocr_columns
        try:
            _col_cfg = get_ocr_columns()
            _sel_cols = [c for c in (_col_cfg.get('selected') or []) if c]
        except Exception:
            _sel_cols = []
        if not _sel_cols:
            _sel_cols = ['商品信息', '仓库总库存', '仓库预估总销售数']
        # R1 布局B：补「模型」列—— 起 plans 已带 model 补货模型标注、Excel 导出
        # 也有此列，但结果表一直没显示；GUI 用中文短标签（经典/加权/高级），导出仍用原始标注
        # R2 预测：补「预测」列—— forecast_next_period 下一期日销（None 显示 '—'），
        # 紧跟「可售卖天数」（同属日销/库存推算语义组）
        calc_cols = [('可售卖天数', 'ratio'), ('预测', 'forecast'), ('状态', 'status'),
                     ('补货量', 'qty'), ('预警', 'warning'), ('模型', 'rmodel')]
        # v1.5.13 结果列开关：预警/预测/模型可关（列表+导出联动，见 filter_row 勾选）
        try:
            from utils import filter_result_cols
            calc_cols = filter_result_cols(calc_cols, getattr(self, '_result_cols', None))
        except Exception:
            pass
        display_cols = list(_sel_cols) + [c[0] for c in calc_cols]
        try:
            self.tree.configure(columns=display_cols)
            for col in display_cols:
                # R1 布局B：列宽自适应规则抽为模块级 tree_col_width（可单测）——
                # 预警 140（多标签不截断）/ 名称类 260 / 模型 100 / 数字状态 110；
                # 数字/状态列加宽到 110，防"1109份"被截断成"110份"误导
                width = tree_col_width(col)
                self.tree.heading(col, text=col, command=lambda c=col: self._sort_tree(c))
                self.tree.column(col, width=width, anchor="center")
        except Exception:
            pass
        self.tree.delete(*self.tree.get_children())
        # iid → rows 索引映射（排序/筛选后编辑仍回写正确行）
        self._row_index_map = {}
        # 仓库筛选选项：从当前 plans 收集去重（每次渲染刷新，地区切换后自动更新）
        try:
            _whs = sorted({p.get('warehouse', '') for p in plans if p.get('warehouse')})
            _cur_wh = self._wh_filter_var.get() if hasattr(self, '_wh_filter_var') else '全部仓库'
            if _cur_wh not in ('全部仓库', *_whs):
                _cur_wh = '全部仓库'
                self._wh_filter = '全部仓库'
                self._wh_filter_var.set('全部仓库')
            self.wh_combo.configure(values=('全部仓库', *_whs))
        except Exception:
            pass
        # 筛选：仅显示预警（红/黄行）
        if getattr(self, '_filter_warning_only', False):
            plans = [p for p in plans if p.get('color') in ('red', 'yellow')]
        # 筛选：仓库（OCR 仓库信息列）
        _wf = getattr(self, '_wh_filter', '全部仓库')
        if _wf and _wf != '全部仓库':
            plans = [p for p in plans if (p.get('warehouse') or '') == _wf]
        for p in plans:
            tags = ()
            if p['color'] == 'red': tags = ('urgent',)
            elif p['color'] == 'yellow': tags = ('warning',)
            # 动态列渲染：display_cols = 勾选列 + 计算列，按列名逐列取值，
            # 不再硬编码旧固定 7 列（旧版 name/stock/daily/est_sales 顺序与新勾选列对不上 → 串位）
            _raw = p.get('_raw') or {}
            _map = p.get('_sel_cols_map') or {}
            row_vals = []
            for _c in display_cols:
                if _c == '可售卖天数':
                    row_vals.append(p.get('days_left', p.get('ratio', '')) or '')
                elif _c == '预测':
                    # R2 预测： forecast_next_period 下一期日销；
                    # None（无历史/样本<2 天）显示 '—'（§4：显式缺数据，不编 0）
                    row_vals.append(forecast_cell_text(p.get('forecast')))
                elif _c == '状态':
                    row_vals.append(p.get('status', '') or '')
                elif _c == '补货量':
                    row_vals.append(p.get('qty', '') or '')
                elif _c == '预警':
                    # 高级模式预警标签（滞销⚠/超卖🔥/超卖⚠/低置信⚠，' / '分隔）；
                    # 经典/加权模式空字符串（除非 _low_confidence=True 才有 低置信⚠）
                    row_vals.append(p.get('warning', '') or '')
                elif _c == '模型':
                    # R1 布局B：补货模型标注 → 中文短标签（经典/加权/高级/经典·无历史/经典·异常）
                    row_vals.append(model_display_label(p.get('model', '')))
                elif _map.get(_c) == 'name':
                    # 商品名：用解析后的 name（去掉 ID 后缀），比 _raw 原文干净；
                    # R1 布局B：超长名显示省略（elide_cell 纯函数）——双击编辑 overlay
                    # 从 rows 取原值、导出用完整 name，显示截断不进数据流
                    row_vals.append(elide_cell(p.get('name', '')))
                elif _map.get(_c) == 'stock':
                    # 仓库总库存：优先 _raw 原文（带单位'69份'），但去过尾部噪音
                    # （qwen3.5-ocr 会抄出 '69份 查看' / '108份 08-06 21:30 更新记录'）
                    _sv = _raw.get(_c) or p.get('stock', '') or ''
                    if isinstance(_sv, str):
                        from ocr import strip_tail_noise as _stn
                        _sv = _stn(_sv)
                    row_vals.append(_sv)
                elif _map.get(_c) == 'sales':
                    # 仓库预估总销售数：优先 _raw 原文（带单位），同样去过尾部噪音
                    _sv = _raw.get(_c) or p.get('daily', '') or ''
                    if isinstance(_sv, str):
                        from ocr import strip_tail_noise as _stn
                        _sv = _stn(_sv)
                    row_vals.append(_sv)
                elif _map.get(_c) == 'warehouse':
                    # 仓库信息：用 parse 已过滤的 warehouse（strip_tail_noise 已去"查看地址"等词条），
                    # 不用 _raw 原文——原文带词条噪音会显示成"示例仓库 查看地址"
                    row_vals.append(p.get('warehouse', '') or _raw.get(_c, '') or '')
                else:
                    # 其他勾选列（仓库销售库存等）：显示 OCR 原文
                    row_vals.append(_raw.get(_c, '') or '')
            iid = self.tree.insert("", "end", values=tuple(row_vals), tags=tags)
            self._row_index_map[iid] = p.get('_row_idx', len(self._row_index_map))
    
    def _update_tabs(self):
        """更新地区切换标签"""
        for w in self.tab_frame.winfo_children():
            w.destroy()
        if not self.cache:
            tk.Label(self.tab_frame, text="暂无缓存数据", font=(self.FONT[0], 8), fg=self.C_MUTED).pack(side="left")
            # v1.4.x 导航重构：历史趋势入口固定在 tab 行尾（无缓存时也可见）；
            # 只做导航页跳转，实现单一来源 = 导航历史页（stats_ui.StatsPagesMixin）
            self._mk_btn(self.tab_frame, "📈 历史", self._goto_history_page, kind='ghost',
                         font=("微软雅黑", 8), pack_side="right", padx=6)
            return

        tk.Label(self.tab_frame, text="地区: ", font=(self.FONT[0], 8),
                 fg=self.C_MUTED).pack(side="left")
        for reg in sorted(self.cache.keys()):
            is_active = reg == self.active_region
            self._mk_btn(self.tab_frame, reg, lambda r=reg: self._switch_region(r),
                         kind='tag' if is_active else 'ghost',
                         font=("微软雅黑", 8, "bold" if is_active else "normal"),
                         pack_side="left", padx=2)
        # v1.4.x 导航重构：历史趋势入口固定在 tab 行尾（复用地区 tab 行），
        # 点击跳转导航历史页（_show_page），不再弹 Toplevel——与导航页同源
        self._mk_btn(self.tab_frame, "📈 历史", self._goto_history_page, kind='ghost',
                     font=("微软雅黑", 8), pack_side="right", padx=6)
    
    def _switch_region(self, region):
        """切换到指定地区的缓存结果"""
        if region not in self.cache:
            return
        self.active_region = region
        data = self.cache[region]
        self.region_var.set(region)
        
        # 同步重建 rows：切地区后 rows 必须对应当前地区缓存，否则用户编辑/刷新计算
        # 会用上一地区的混合 rows 覆盖当前地区缓存（v1.4 修复 Bug#3）
        try:
            _items = data.get('items') or []
            self._suppress_auto_append = True
            try:
                for row in self.rows:
                    row['name'].set('')
                    row['stock'].set('')
                    row['sales'].set('')
                    row['_raw'] = {}
                while len(self.rows) < len(_items):
                    self._add_row()
                for i, it in enumerate(_items):
                    if i >= len(self.rows):
                        break
                    r = self.rows[i]
                    r['name'].set(it.get('name', ''))
                    r['stock'].set(str(it.get('stock', '')))
                    r['sales'].set(str(it.get('sales', '')))
                    r['_raw'] = it.get('_raw') or {}
            finally:
                self._suppress_auto_append = False
        except Exception as _e:
            # 缓存 items 缺失时保持现状（只切显示），但留日志便于排查
            log.warning(f"切地区重建 rows 失败({region}): {_e}")
        
        # 显示该地区的结果（v1.3 动态列 + 筛选：复用 _render_tree）
        self._render_tree(data['plans'])
        self.plans = data['plans']
        self._sort_col = None
        self._sort_reverse = False
        self._update_tabs()
        suffix = "（仅显示预警）" if self._filter_warning_only else ""
        self.status_text.set(f"已切换到 {region} — {len(data['plans'])} 个商品{suffix}")
        self._auto_expand(len(data['plans']))

    # ─────────────── 多店铺隔离：店铺切换器 ───────────────

    def _refresh_store_combo(self):
        """刷新店铺切换器（下拉值 + 当前店铺显示名；主界面/设置页共用）。

        store_registry 缺失时降级显示「默认店铺」，绝不抛（worker 禁 Tk 纪律下
        本方法只允许主线程调用）。
        """
        if store_registry is None or not hasattr(self, 'store_combo'):
            self._store_name2id = {'默认店铺': 'default'}
            try:
                self.store_var.set('默认店铺')
            except Exception:
                pass
            return
        stores = store_registry.get_stores()
        _labels, self._store_name2id = store_ui_logic.store_choices(stores)
        self.store_combo['values'] = list(self._store_name2id)
        cur = getattr(self, '_store_id', None) or 'default'
        cur_name = store_registry.get_store_name(cur)
        if cur_name not in self._store_name2id:
            # 当前店铺已不存在（如被其他入口删除）：回落默认店铺并修正状态
            cur, cur_name = 'default', '默认店铺'
            self._store_id = cur
        self.store_var.set(cur_name)

    def _on_store_switch(self, _event=None):
        """店铺切换器选中回调（t6）。

        决策走 store_ui_logic.resolve_store_switch 纯函数（可单测）：
        批量运行中互斥拒绝 / 目标不存在拒绝 / 同店幂等 / 跨店全量重建。
        拒绝时显式恢复当前店铺显示并说明原因（宪法 §4 不静默）。
        """
        target_id = (getattr(self, '_store_name2id', {}) or {}).get(self.store_var.get())
        try:
            stores = store_registry.get_stores() if store_registry is not None \
                else [{'id': 'default', 'name': '默认店铺'}]
        except Exception:
            stores = [{'id': 'default', 'name': '默认店铺'}]
        decision = store_ui_logic.resolve_store_switch(
            getattr(self, '_store_id', None), target_id, stores,
            # v1.5.8（BUG_HUNT_V157 A5）：busy 同时判批量图片运行态（v1.5.x R1 回归修复）
            busy=bool(getattr(self, '_batch_running', False)
                      or getattr(self, '_img_batch_running', False)))
        if not decision['ok']:
            self._refresh_store_combo()
            if decision['reason'] == 'rejected-busy':
                self.status_text.set("⚠ 批量识别进行中，禁止切换店铺（防止跨店数据错位）")
            else:
                self.status_text.set("⚠ 目标店铺不存在，已保持当前店铺")
            return
        if decision['reason'] == 'ok-idempotent':
            return  # 同店重复选择：幂等，零重建
        self._apply_store_switch(decision['store_id'])

    def _apply_store_switch(self, new_store_id):
        """落盘激活店 + 全量重建主界面状态（DESIGN §3：切店铺=切地区同款纪律）。

        重建即整体替换：regions/cache/active_region/region_var/plans/输入行全部
        按目标店铺全新构建（store_ui_logic.fresh_gui_state 规范形状），禁止在旧
        状态上原地改——残留即跨店污染。失败保持原店铺并显式提示。返回是否成功。
        """
        if store_registry is None:
            self.status_text.set("⚠ 店铺模块缺失，无法切换店铺")
            return False
        # R2 问题（Critical）：重入守卫 + _store_id 先行一致。
        # 旧时序：set_active 成功后才赋 self._store_id——中途任何重入（行编辑
        # trace / 二次 ComboboxSelected）看到的 _store_id 还是旧店，与底层
        # active 已分离，rows 重建与 _store_name2id 刷新交叠出现 0.5s 残留窗口。
        # 现在：先置 _store_id（后续重建一律按目标店语义），set_active 失败回滚。
        if getattr(self, '_store_switching', False):
            self.status_text.set("⚠ 店铺切换进行中，请稍候再试")
            return False
        self._store_switching = True
        try:
            return self._apply_store_switch_locked(new_store_id)
        finally:
            self._store_switching = False

    def _apply_store_switch_locked(self, new_store_id):
        """_apply_store_switch 的实际重建段（守卫内执行，见上）。"""
        prev_store_id = getattr(self, '_store_id', 'default')
        self._store_id = new_store_id  # 先行一致（问题）：失败再回滚
        had_unsaved = bool(self.cache)
        if not store_registry.set_active(new_store_id):
            self._store_id = prev_store_id  # 回滚：保持与底层 active 一致
            self.status_text.set("⚠ 店铺切换失败（详见日志 ocr_dlog.txt），已保持当前店铺")
            self._refresh_store_combo()
            return False
        state = store_ui_logic.fresh_gui_state(
            new_store_id, store_registry.get_regions(new_store_id))
        self.regions = state['regions']
        self.cache = state['cache']
        self.active_region = state['active_region']
        self.plans = state['plans']
        self.region_var.set(state['region_var'])
        # 输入行清空（_fill_from_ocr 同款禁自动加行模式；rows 归新店铺空态）
        self._suppress_auto_append = True
        try:
            for row in self.rows:
                row['name'].set('')
                row['stock'].set('')
                row['sales'].set('')
                row['_raw'] = {}
        finally:
            self._suppress_auto_append = False
        self._render_tree(self.plans)
        self._update_tabs()
        self._refresh_store_combo()
        msg = f"已切换到店铺「{self.store_var.get()}」— 时效配置/缓存/历史已按店铺隔离"
        if had_unsaved:
            msg += "（原店铺未导出的识别结果已清空，如需保留请先导出）"
        self.status_text.set(msg)
        return True
    
    def _del_row(self, force_last=False):
        """删行：优先删识别结果表格选中行（排序/筛选后经 _row_index_map 还原 rows 索引）；
        force_last=True 供清空逻辑删末尾行。至少保留 1 行。"""
        if len(self.rows) <= 1:
            return
        if force_last:
            idxs = [len(self.rows) - 1]
        else:
            sel = self.tree.selection() if hasattr(self, 'tree') else ()
            if sel:
                idxs = sorted({self._row_index_map.get(i) for i in sel
                               if self._row_index_map.get(i) is not None}, reverse=True)
            else:
                idxs = [len(self.rows) - 1]
        for idx in idxs:
            if len(self.rows) <= 1:
                break
            # 越界保护：表格选中行索引可能因排序/筛选已陈旧（v1.4 审查修复）
            if not (0 <= idx < len(self.rows)):
                continue
            self.rows.pop(idx)
        # 删除后重建 _row_index_map：旧映射里的索引整体后移已失效，
        # 残留会导致下次右键删除删错行（v1.4 审查修复）
        try:
            _old_map = dict(getattr(self, '_row_index_map', {}) or {})
            _del_set = set(idxs)
            self._row_index_map = {}
            for _iid, _iid_idx in _old_map.items():
                if _iid_idx is None or _iid_idx in _del_set:
                    continue  # 被删行自身的 iid 失效，必须丢弃（否则指向错行）
                # 平移：原 rows 索引 > 删除位置 → 减 1
                _new_idx = _iid_idx - sum(1 for _d in idxs if _d < _iid_idx)
                if 0 <= _new_idx < len(self.rows):
                    self._row_index_map[_iid] = _new_idx
        except Exception:
            pass
        if hasattr(self, 'tree') and self.tree.winfo_exists():
            self._recalc_from_rows()
    
    def _clear_input_rows(self):
        """清空所有输入行，同时清除 Treeview 结果"""
        # 临时禁用自动加行：set('') 触发 write trace 会追加空行，清空后多出一行
        self._suppress_auto_append = True
        try:
            for row in self.rows:
                row['name'].set('')
                row['stock'].set('')
                row['sales'].set('')
        finally:
            self._suppress_auto_append = False
        # 清理自动加行产生的多余空行（保留初始 3 行）
        while len(self.rows) > 3 and all(
                not r['name'].get().strip() and not r['stock'].get().strip()
                and not r['sales'].get().strip() for r in self.rows[-1:]):
            self._del_row(force_last=True)
        # 也清掉 Treeview 旧结果
        self.tree.delete(*self.tree.get_children())
    
    def _build_raw_from_fields(self, name, stock, sales, region='', warehouse=''):
        """
        按当前列配置把业务字段填回中文列名（手动输入路径构造 _raw 用）。
        从 selected 勾选列出发覆盖全部显示列，再按 mapping 填业务字段值，
        保证手动输入路径与 OCR 路径的 _raw key 一致、勾选列不空白。
        """
        try:
            from utils import get_ocr_columns
            cfg = get_ocr_columns()
            mapping = cfg.get('mapping') or {}
            selected = cfg.get('selected') or []
        except Exception:
            mapping, selected = {}, []
        if not selected:
            selected = ['商品信息', '仓库总库存', '仓库预估总销售数']
        # 覆盖所有勾选列（未填到的保持空字符串，渲染时显示空白而非缺失）
        raw = {col: '' for col in selected}
        for field, val in (('name', name), ('stock', stock),
                           ('sales', sales), ('region', region),
                           ('warehouse', warehouse)):
            col = mapping.get(field)
            if col and val != '':
                raw[col] = str(val)
        return raw

    def _recalc_from_rows(self):
        """从当前输入行读取数据，重新计算（name 非空即保留，包括售罄/零数据商品）"""
        items = []
        skipped = 0
        for r in self.rows:
            name = r['name'].get().strip()
            stock_s = r['stock'].get().strip()
            sales_s = r['sales'].get().strip()
            if not name:
                skipped += 1
                continue
            # 剥离 _fill_from_ocr 写入的显示装饰（⚠低置信前缀 / [仓库]后缀），
            # 保证计算/时效匹配/导出用干净原始 name（v1.4 修复）
            name = _strip_name_decor(name)
            try:
                stock = int(stock_s) if stock_s else 0
            except ValueError:
                stock = 0
            try:
                sales = int(sales_s) if sales_s else 0
            except ValueError:
                sales = 0
            _raw = dict(r.get('_raw') or {})
            # 每行自己的地区：OCR 行从 _raw 的「销售区域」列取（批量识别多省份时各行不同），
            # 手动行/取不到时回退当前地区——修复多省份批量全部按最后省份计算时效
            _row_region = ''
            try:
                from utils import get_ocr_columns as _goc
                # v1.4.5（bug hunt F2）：此函数此前未导入 strip_region_suffix → NameError 被裸 except 吞，
                # 每行 region 回退当前地区，多省刷新计算全按第一省算时效（违反 DESIGN §3）
                from ocr import strip_region_suffix
                _rmap = (_goc().get('mapping') or {})
                _rc = _rmap.get('region')
                if _rc:
                    _row_region = strip_region_suffix(str(_raw.get(_rc, ''))) if _raw else ''
            except Exception:
                _row_region = ''
            if not _row_region:
                _row_region = self.region_var.get()
            if _raw:
                # OCR 行：保留原始列（仓库信息/仓库销售库存等勾选列），
                # 仅用输入框当前值覆盖库存/销量列（用户可能改过）
                try:
                    from utils import get_ocr_columns
                    _mapping = (get_ocr_columns().get('mapping') or {})
                except Exception:
                    _mapping = {}
                for _field, _val in (('stock', stock), ('sales', sales)):
                    _col = _mapping.get(_field)
                    if _col:
                        _old = str(_raw.get(_col, ''))
                        # 保留原值单位（'85份' → 用户改 90 → '90份'），
                        # v1.4.5（bug hunt F25）：先 strip_tail_noise 再去尾部非数字串——
                        # 否则 '69份 查看地址' 会把'查看地址'当单位回写（_raw 污染）
                        # 验收回归（fix-review P0）：此处需自导 re——旧补丁用了未定义 _re2
                        # → NameError → 刷新计算对 OCR 数据整体失效
                        import re as _re2
                        from ocr import strip_tail_noise as _stn2
                        _unit = ''
                        _m2 = _re2.search(r'[^\d\s.,，、]+$', _stn2(_old))
                        if _m2:
                            _unit = _m2.group(0)
                        _raw[_col] = str(_val) + _unit
                # 从 _raw 提取仓库（勾选列值可能带「查看地址」噪音），供仓库筛选/显示
                from ocr import strip_warehouse_noise
                _wh_col = _mapping.get('warehouse')
                warehouse = strip_warehouse_noise(str(_raw.get(_wh_col, ''))) if _wh_col else ''
            else:
                # 纯手动行：按列配置补全（仓库无输入源，留空）
                _raw = self._build_raw_from_fields(name, stock, sales,
                                                   region=_row_region)
                warehouse = ''
            items.append({'name': name, 'stock': stock, 'sales': sales,
                         'region': _row_region,
                         'warehouse': warehouse, '_raw': _raw})
        if not items:
            messagebox.showwarning("无数据", "请至少输入一个商品")
            return
        self._calc_from_items(items)
        msg = f"已刷新 — {len(items)} 个商品"
        if skipped:
            msg += f"（已跳过 {skipped} 个空行）"
        self.status_text.set(msg)
    
    def _emergency_stop(self):
        """F9 紧急停止批量识别。
        v1.4.2：取消钩子注入后，当前/后续 API 请求在下一个检查点立即抛
        BatchCancelled/VisionCancelled 中断——不再等满 30~90s 超时。
        t5：同时通过 TaskQueue 取消任务（协作式取消）。
        R1 布局B：cancelled 任务无 on_done/on_error 回调（见 _poll_cancel_restore），
        取消后启动终态轮询，复位进度条/按钮/状态栏（进度复位）。"""
        self._batch_stop.set()
        # 协作式取消当前批量任务
        if self._batch_task_id is not None:
            self._task_queue.cancel(self._batch_task_id)
            self.win.after(400, lambda: self._poll_cancel_restore(self._batch_task_id))
        # R1 效率：批量图片识别运行中 → 取消全部图片任务并启动恢复监视
        # （图片任务是多个 task_id、无独立 _batch_task_id；cancelled 任务无回调，
        # F9 后同样要复位进度条/按钮，见 _img_batch_poll_cancel）
        if getattr(self, '_img_batch_running', False):
            for _tid in list(getattr(self, '_img_batch_task_ids', None) or []):
                try:
                    self._task_queue.cancel(_tid)
                except Exception:
                    pass
            self.win.after(400, self._img_batch_poll_cancel)
        self.status_text.set("⏹ 紧急停止 — 正在中断当前识别…")

    def _load_saved_geometry(self):
        """R1 效率：读取上次保存的窗口 geometry（settings['window']['geometry']）。

        返回 str 或 None；Config 读取/类型异常一律 None（启动时回落默认居中，绝不抛）。
        """
        try:
            from utils import Config as _Cfg
            node = _Cfg.load().get('window')
            if isinstance(node, dict):
                g = node.get('geometry')
                if isinstance(g, str) and g:
                    return g
        except Exception:
            pass
        return None

    def _save_window_geometry(self):
        """R1 效率：保存当前窗口 geometry 到 settings['window']['geometry']（原子写）。

        仅 win.state() == 'normal' 时写——最大化(zoomed)/最小化(iconic)的 geometry
        读数（负偏移/全屏值）不是用户日常布局，写了也会被 clamp_geometry 拒绝，
        不如不写、保留上一次正常布局。写失败仅记日志，不阻塞关闭（§4 显式留痕）。
        """
        try:
            if str(self.win.state()) != 'normal':
                return
        except Exception:
            pass
        try:
            geo = str(self.win.geometry())
            if not geo:
                return
            from utils import Config as _Cfg
            data = _Cfg.load()
            node = data.get('window')
            if not isinstance(node, dict):
                node = {}
            node['geometry'] = geo
            data['window'] = node
            _Cfg.save(data)
        except Exception as e:
            try:
                log.warn(f"窗口状态保存失败（不影响退出）: {str(e)[:80]}")
            except Exception:
                pass

    def _show_uncaught_hint(self, msg):
        """R3 异常守卫提示：状态栏 + 弹窗（限流判定已在 exception_guard 层完成）。"""
        try:
            self.status_text.set(f"⚠ {msg}")
        except Exception:
            pass
        try:
            from tkinter import messagebox
            messagebox.showwarning('程序异常', str(msg)[:300], parent=self.win)
        except Exception:
            pass

    def _on_closing(self):
        """主窗关闭：先收队 TaskQueue 再销毁窗口。

        旧路径「点 X = 直接 destroy」：daemon worker 仍在跑，in-flight API 请求
        被半截中断（usage_log.jsonl 写出半截 JSON 行）；排队任务被worker捞起继续
        跑已死的 Tk；协作式取消钩子永不触发。现路径：
          1) _batch_stop.set() —— 批量协作检查点立即生效；
          2) cancel_all() —— pending 任务全部 CANCELLED（async_queue R2 起同步标记）；
          3) shutdown(wait=False) —— 不挂 UI，worker 循环自然退出；
          4) destroy() —— 正常销毁主窗。
        R1 效率：收队之前先保存窗口 geometry（win 仍存活，读数有效；v1.5.4 的
        队列清理逻辑原样保留在其后）。
        R3 健壮：收队前还原全局异常钩子（防还原期钩子引用已死窗口）。
        """
        try:
            if getattr(self, '_ex_handles', None):
                import exception_guard as _eg
                _eg.uninstall(self._ex_handles)
                self._ex_handles = None
        except Exception:
            pass
        try:
            self._save_window_geometry()
        except Exception:
            pass
        try:
            self._batch_stop.set()
        except Exception:
            pass
        try:
            self._task_queue.cancel_all()
        except Exception:
            pass
        try:
            self._task_queue.shutdown(wait=False)
        except Exception:
            pass
        try:
            self._batch_running = False
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass
    
    def _batch_scan(self):
        """批量识别：对已知地区逐个引导截图识别"""
        # v1.5.9：识别前置 API 预检（未配置 → 明确引导弹窗 + 可跳转设置页）
        if not self._ensure_api_ready():
            return
        # v1.4.6 bug hunt F24 重入守卫 + R1 效率互斥：批量识别/批量图片共用
        # _batch_stop 取消通道与识别队列，任一运行中禁止再开（防取消钩子互相覆盖）
        if getattr(self, '_batch_running', False) or getattr(self, '_img_batch_running', False):
            messagebox.showinfo("批量识别", "批量任务正在进行中，请先等待完成或停止后再试")
            return
        known = sorted(self.regions.keys())
        if not known:
            # v1.5.9.3：首次使用引导——批量需要先有识别过的地区（用户反复误点批量被卡）
            try:
                log.warn('批量识别：暂无已识别地区（regions 为空），弹首次使用引导')
            except Exception:
                pass
            try:
                import webbrowser
                _go = messagebox.askyesno(
                    "还没有识别过任何地区",
                    "「批量识别」需要先有至少一个识别过的地区（发货省/市）。\n\n"
                    "第一次使用请这样开始：\n"
                    "  1. 打开浏览器登录拼多多商家后台\n"
                    "     （https://mms.pinduoduo.com → 订货管理页）\n"
                    "  2. 回到本程序点第一排的「识图」按钮\n"
                    "     ——它会自动最小化、截后台窗口、恢复并识别\n"
                    "  3. 识别成功后本页就会记住该地区，之后「批量」就能用了\n\n"
                    "也可以不走截图：点「导入」→「导入表格文件」\n"
                    "  ——用模板导入已有数据同样会建立地区\n\n"
                    "是否现在帮你打开商家后台？",
                    parent=self.win)
                if _go:
                    _url = 'https://mms.pinduoduo.com/'
                    try:
                        from utils import Config as _CfgB
                        _b = _CfgB.load().get('backend') or {}
                        if isinstance(_b, dict) and str(_b.get('url') or '').startswith('http'):
                            _url = str(_b['url'])
                    except Exception:
                        pass
                    webbrowser.open(_url)
            except Exception:
                try:
                    messagebox.showinfo("批量识别", "暂无知地区，请先手动「识图」识别一次")
                except Exception:
                    pass
            return
        
        # 选择地区对话框
        dlg = tk.Toplevel(self.win)
        dlg.title("批量识别")
        dlg.geometry(self._geo(400, 500))
        dlg.minsize(int(380 * self.dpi_scale), int(350 * self.dpi_scale))
        dlg.resizable(True, True)
        dlg.configure(bg=self.C_BG)
        
        tk.Label(dlg, text="选择要批量识别的地区", font=self.FONT_HEADING,
                bg=self.C_BG, fg=self.C_TEXT).pack(pady=(15,5))
        tk.Label(dlg, text="将依次引导您切换地区并截图识别", font=(self.FONT[0], 8),
                bg=self.C_BG, fg=self.C_MUTED).pack()
        
        # 底部控制区（先pack确保不被挤掉）
        bottom_frame = tk.Frame(dlg, height=130)
        bottom_frame.pack(side="bottom", fill="x", padx=20, pady=(5,10))
        bottom_frame.pack_propagate(False)

        # 选项横排（双模型），避免纵向堆叠把按钮挤出可视区
        opt_row = tk.Frame(bottom_frame, bg=self.C_BG)
        opt_row.pack(pady=(5,0))
        dual_var = tk.BooleanVar(dlg, value=True)  # 默认开双模型（v1.3：不在乎 token 成本，识别更准）
        tk.Checkbutton(opt_row, text="🛡 双模型验证（慢一倍，更准）",
                      variable=dual_var, font=(self.FONT[0], 8),
                      bg=self.C_BG, fg=self.C_MUTED,
                      selectcolor=self.C_BG, activebackground=self.C_BG).pack(side="left", padx=10)
        
        # 地区勾选列表（可滚动，占剩余空间）
        canvas = tk.Canvas(dlg, bg=self.C_SURFACE, highlightthickness=0)
        scrollbar = ttk.Scrollbar(dlg, orient="vertical", command=canvas.yview)
        list_frame = tk.Frame(canvas, bg=self.C_SURFACE)
        list_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=list_frame, anchor="nw", width=340)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(20,0), pady=(0,10))
        scrollbar.pack(side="right", fill="y", padx=(0,20), pady=(0,10))
        def _on_mousewheel(event): canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        canvas.bind("<MouseWheel>", _on_mousewheel)
        # 不再用 bind_all，避免影响主窗口
        
        vars_map = {}
        for reg in known:
            var = tk.BooleanVar(dlg, value=True)
            vars_map[reg] = var
            cb = tk.Checkbutton(list_frame, text=reg, variable=var,
                               font=(self.FONT[0], 8), bg=self.C_SURFACE, fg=self.C_TEXT,
                               selectcolor=self.C_SURFACE, activebackground=self.C_SURFACE)
            cb.pack(anchor="w", padx=8, pady=1)
            # 复选框也绑定滚轮，鼠标悬停在选项上时也能滚动列表
            cb.bind("<MouseWheel>", _on_mousewheel)
        
        def start_batch():
            # v1.4.6 bug hunt F24 重入守卫：批量运行中禁止再启（双批会互相覆盖取消钩子/物理争抢）
            if getattr(self, '_batch_running', False):
                messagebox.showwarning("批量识别", "已有批量任务正在进行，请先等待完成或停止", parent=dlg)
                return
            selected = [r for r, v in vars_map.items() if v.get()]
            if not selected:
                messagebox.showwarning("未选择", "请至少选择一个地区", parent=dlg)
                return

            # P3-B：批量前成本预估确认（用户裁定：仅在前置弹窗确认，与 F9/_api_fatal/预算三态无关）
            # 估算失败不阻塞批量（静默跳过+log）；取消则干净退出，不置任何熔断标志
            try:
                if not self._preview_batch_cost(len(selected), dual_verify=dual_var.get()):
                    return
            except Exception as _e:
                try:
                    from logger import log as _log
                    _log.warning(f"[batch] 成本预估异常：{str(_e)[:120]}")
                except Exception:
                    pass

            # HUD 实时日志窗（默认开启，替代原测试模式开关）：主线程创建
            hud = tk.Toplevel(self.win)
            hud.title("")
            hud.overrideredirect(True)
            hud.attributes('-topmost', True, '-alpha', 0.82)
            hud.configure(bg='#0F172A')
            sw_h, sh_h = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
            hud.geometry(f"{int(400*self.dpi_scale)}x{int(250*self.dpi_scale)}+{sw_h-int(420*self.dpi_scale)}+{int(30*self.dpi_scale)}")
            hud_text = tk.Text(hud, font=('Consolas', 9), bg='#0F172A', fg='#22D3EE',
                              wrap='word', relief='flat', borderwidth=0, padx=10, pady=10)
            hud_text.pack(fill='both', expand=True)
            hud_text.insert('end', '🔍 批量识别进行中…\n')
            hud_text.see('end')
            # 先缓存所有 UI 值再销毁对话框（destroy 后访问控件会 TclError）
            _dual_mode = dual_var.get()
            dlg.destroy()
            # 禁用操作按钮防止并发（v1.4.5 bug hunt F24：批量期间也禁用实时截图入口，
            # 防双批并发互相覆盖取消钩子/物理争抢鼠标键盘；fix-review C10：过滤 None）
            for btn in [b for b in [self.export_btn, getattr(self, 'live_btn', None)] if b]:
                self.win.after(0, lambda b=btn: b.configure(state='disabled'))
            # D：批量期间按钮文案变化补全（config(text=) 模式同 gui.py:122-124）
            self.win.after(0, lambda: self._set_batch_btn_state(True))
            self.status_text.set("批量识别中 — 请不要操作")

            # 注入取消钩子并提交到 TaskQueue
            self._batch_stop.clear()
            from ocr import set_cancel_check as _ocr_cc
            from vision import set_cancel_check as _vis_cc
            _ocr_cc(self._batch_stop.is_set)
            _vis_cc(self._batch_stop.is_set)

            # 任务结果通过队列传回主线程（保持原有轮询逻辑）
            result_queue = __import__('queue').Queue()

            def _batch_task_fn(progress):
                """t5: 批量任务函数（worker 线程执行）"""
                self._batch_running = True
                try:
                    log.hr(f"批量识别开始：{len(selected)} 个地区", 1)
                    self._run_batch_sequence(selected, hud, hud_text, _dual_mode, result_queue, progress)
                    log.hr("批量识别完成", 1)
                finally:
                    self._batch_running = False
                    try:
                        _ocr_cc(None); _vis_cc(None)
                    except Exception:
                        pass

            def _batch_on_done(_):
                """t5: 任务完成回调（worker 线程触发，需 after 调度）"""
                self.win.after(0, lambda: self._poll_batch_queue(result_queue, 0, len(selected), 0))

            def _batch_on_error(exc):
                """t5: 任务异常回调（worker 线程触发，需 after 调度）"""
                import traceback
                log.error("批量识别异常:\n" + traceback.format_exc())
                try:
                    with open(os.path.join(get_base_dir(), 'output', 'ocr_dlog.txt'),
                              'a', encoding='utf-8') as _f:
                        _f.write('[batch] 线程异常: ' + traceback.format_exc() + '\n')
                except Exception:
                    pass
                self.win.after(0, lambda: self.status_text.set(f"❌ 批量识别异常: {str(exc)[:80]}"))
                self.win.after(0, self.win.deiconify)
                self.win.after(0, lambda: self.export_btn.configure(state='normal'))
                self.win.after(0, lambda: (self.live_btn.configure(state='normal')
                                           if getattr(self, 'live_btn', None) else None))
                self.win.after(0, lambda: self._set_batch_btn_state(False))
                self.win.after(0, self._reset_batch_progress)  # R1 布局B：异常收尾进度条复位

            # 提交到 TaskQueue（保存 task_id 供 F9 使用）
            # R1 布局B：接 on_progress——批量进度百分比进进度条、阶段文案进状态栏
            self._batch_task_id = self._task_queue.submit(
                "批量识别",
                _batch_task_fn,
                on_done=_batch_on_done,
                on_progress=self._on_batch_progress,
                on_error=_batch_on_error,
                cancel_event=self._batch_stop,
            )
            self.win.after(0, self._begin_batch_progress)
        
        _sb = self._mk_btn(bottom_frame, "开始批量识别", start_batch, kind='primary',
                           font=self.FONT_BOLD, width=18, height=2)
        _sb.pack_configure(pady=(10,0))
        
        dlg.transient(self.win)
        dlg.grab_set()
    
    def _run_batch_sequence(self, regions, hud=None, hud_text=None, dual_verify=False,
                           result_queue=None, progress=None):
        """批量识别：1.点文本框 2.粘贴省份 3.回车 4.点查询 5.等刷新
        6.截图识别（AI 定位表格 + 滚动加载循环，直到无更多商品）
        不填仓库：依赖滚动检测识别该省份全部商品，仓库信息来自 OCR「仓库信息」列。

        t5: 新增 result_queue 和 progress 参数。
        result_queue: 可选，外部传入的队列（t5 使用 TaskQueue 时外部创建并传入）。
        progress: 可选，进度回调函数（t5 使用 TaskQueue 时用于进度上报）。
        """
        import time, threading, queue as _queue_mod
        # v1.4.2 紧急停止：取消异常类型（ocr/vision 请求层抛），用于快速中断滚动/省份循环
        from ocr import BatchCancelled
        from vision import VisionCancelled
        # 使用外部传入的队列，或内部创建（向后兼容未传入的场景）
        if result_queue is None:
            result_queue = _queue_mod.Queue()
        try:
            import pyautogui, pyperclip
            from vision import locate_element
            from PIL import Image as PILImage
        except ImportError as e:
            # 顶层依赖缺失：立即通知主线程收尾，避免用户白等 30 秒超时
            self.win.after(0, lambda _e=e: self.status_text.set(f"❌ 依赖缺失: {_e}"))
            self.win.after(0, self.win.deiconify)
            result_queue.put(None)
            self.win.after(0, lambda: self._finish_batch(0, len(regions), 0))
            return
        
        def dlog(msg):
            """批量识别过程日志：HUD 弹窗 + 状态栏 + ocr_dlog.txt 三路输出（线程安全，after 调度）
            t5: 同时通过 progress 回调上报进度（可选）。"""
            if hud_text:
                # HUD 可能被用户手动关闭：insert 前先确认窗口存活，防 TclError
                self.win.after(0, lambda m=msg: (
                    (hud_text.insert('end', f'{m}\n'), hud_text.see('end'))
                    if hud_text.winfo_exists() else None))
            self.win.after(0, lambda m=msg: self.status_text.set(f"🔍 {m}"))
            try:
                with open(os.path.join(get_base_dir(), 'output', 'ocr_dlog.txt'),
                          'a', encoding='utf-8') as _f:
                    _f.write('[batch] ' + msg + '\n')
            except Exception:
                pass
            # 进度上报（可选，TaskQueue 进度回调）
            # R1 布局B：百分比 = 地区序 + 阶段占比（batch_stage_percent 纯函数），
            # 多地区批量全程单调递增——旧 stage_num*10 会在每个地区从 10% 跳回；
            # 无阶段前缀的日志沿用上次百分比，只把阶段文案推给状态栏
            if progress is not None:
                try:
                    _pct = batch_stage_percent(msg, _prog_state['idx'],
                                               _prog_state['total'], _prog_state['last'])
                    _prog_state['last'] = _pct
                    progress(_pct, msg)
                except Exception:
                    pass
        
        self.win.after(0, self.win.iconify); time.sleep(1.5)
        # v1.4.4（dsh 报告 #1）：熔断标志跨批量复位——_api_fatal 置位后没有任何清零点，
        # 一次批量额度耗尽会让同一进程后续所有批量永久熔断（换 key 也不恢复）。
        # 批量开始清零：该批内部熔断语义保留，新一批从头可用。
        # ⚠（fix-review C4）：这里不再做 _batch_stop.clear() —— clear 已移到
        # 钩子注入之前（wrapper），此处再清会把注入后 ~1.5s 内按 F9 的停止信号抹掉。
        try:
            from ocr import _api_fatal as _af
            _af['flag'] = False
        except Exception:
            pass
        # v1.4.7 WS-C：「本次」口径——批量启动重置 session 累计
        # （月度口径由自然月 jsonl 决定，无累计器可清；batch_id P1 简化不串线）
        try:
            import usage_store as _us
            _us.session_reset()
        except Exception:
            pass
        self.win.after(0, self._refresh_cost_label)
        # 任务列表：每个省份一个任务（不填仓库，滚动加载识别全部商品，仓库信息来自 OCR 仓库列）
        tasks = list(regions)
        total = len(tasks); success = 0; total_items = 0
        # R1 布局B：进度状态（worker 线程内自用，dlog 闭包读写）——
        # idx=当前地区序 / total=地区总数 / last=最近一次百分比（无阶段前缀日志沿用）
        _prog_state = {'idx': 0, 'total': max(1, total), 'last': 0}
        # R2 问题：批量模糊证据标志（worker 采集，_fill_from_ocr 主线程消费）——
        # 批量收尾会清理 _result_*.png，blur 必须在识别现场逐轮采集
        self._batch_blur_seen = False
        from utils import capture_pdd_screenshot
        win_pos = {}  # 记录浏览器窗口左上角（全屏坐标），滚动换算用
        def ss(path):
            capture_pdd_screenshot(path, out_window_pos=win_pos)
        try:
            sw, sh = pyautogui.size()
        except Exception:
            sw, sh = 1920, 1080  # 兜底：屏幕探测失败用 FHD 默认，避免线程崩溃
        # 加载校准配置
        _cal = {}
        import json as _json
        try:
            with open(os.path.join(get_base_dir(), 'settings.json'), 'r', encoding='utf-8') as _f:
                _cal = _json.load(_f).get('calibrate')
                if not isinstance(_cal, dict):
                    _cal = {}  # 畸形 calibrate 归一化，防后续 .get 崩
        except Exception: pass

        # AI 自动定位：AI 模式下，批量识别启动时每次实时定位按钮坐标
        # （窗口位置/分辨率随时可能变化，坐标必须最新；定位失败时下方静默回退旧坐标）
        now = time.time()
        dlog("AI 自动定位页面元素...")
        try:
            import tempfile
            from vision import ai_locate_elements
            from utils import capture_pdd_screenshot
            # 窗口截图定位：锁定商家后台窗口截图（自动前置），坐标加窗口偏移转全屏；
            # 找不到后台窗口 → fallback 全屏（偏移 0），保持兼容
            _shot = os.path.join(tempfile.gettempdir(), 'pdd_calib_batch.png')
            _pos = {}
            capture_pdd_screenshot(_shot, _pos)
            result = ai_locate_elements(_shot)
            if result:
                _ox, _oy = _pos.get('left', 0), _pos.get('top', 0)
                # 4K/带鱼屏还原：AI 坐标基于保存后的截图（宽≤2560），
                # 先 ×scale 还原到原始窗口像素，再加窗口偏移转全屏（v1.4 审查修复）
                _sx = _pos.get('scale_x', 1.0) or 1.0
                _sy = _pos.get('scale_y', 1.0) or 1.0
                import pyautogui as _pg_batch
                _sw, _sh = _pg_batch.size()
                _cal['ai'] = {
                    'last_time': now,
                    'dropdown': {'x': int(result['dropdown']['x'] * _sx) + _ox,
                                 'y': int(result['dropdown']['y'] * _sy) + _oy},
                    'query': {'x': int(result['query']['x'] * _sx) + _ox,
                              'y': int(result['query']['y'] * _sy) + _oy},
                    'confidence': result['confidence'],
                    'screen_width': _sw,
                    'screen_height': _sh,
                }
                _cal['mode'] = 'ai'
                # 原子写回 settings.json，持久化 AI 定位结果（下次启动直接复用缓存）
                try:
                    from utils import Config as _Cfg
                    _full = _Cfg.load() or {}
                    _full['calibrate'] = _cal
                    _Cfg.save(_full)
                except Exception:
                    pass
                dlog(f"AI 定位完成 置信度:{result['confidence']:.0%}")
        except Exception as _e:
            # 失败静默回退旧坐标，但留日志（客户识别错乱时可定位是定位失败）
            log.warning(f"AI 定位失败，回退旧坐标: {_e}")

        # 获取有效坐标（v1.4 起只保留 AI 定位；绝对坐标模式已移除）
        def _get_coords():
            import pyautogui as _pg
            _ai_raw = _cal.get('ai')
            ai_data = _ai_raw if isinstance(_ai_raw, dict) else {}
            dd = ai_data.get('dropdown', {})
            qq = ai_data.get('query', {})
            # 分辨率适配
            orig_w = ai_data.get('screen_width') or _pg.size()[0]
            orig_h = ai_data.get('screen_height') or _pg.size()[1]
            curr_w, curr_h = _pg.size()
            scale_x = curr_w / orig_w if orig_w and curr_w != orig_w else 1
            scale_y = curr_h / orig_h if orig_h and curr_h != orig_h else 1
            if dd and (scale_x != 1 or scale_y != 1):
                dd = {'x': int(dd['x'] * scale_x), 'y': int(dd['y'] * scale_y)}
            if qq and (scale_x != 1 or scale_y != 1):
                qq = {'x': int(qq['x'] * scale_x), 'y': int(qq['y'] * scale_y)}
            return dd, qq

        dd_coord, qq_coord = _get_coords()
        if dd_coord and qq_coord:
            dlog(f"校准OK (ai): dd({dd_coord.get('x')},{dd_coord.get('y')}) q({qq_coord.get('x')},{qq_coord.get('y')})")
        else:
            dlog(f"未校准（ai模式），请先到设置→校准")
        # 打印当前API配置状态
        try:
            api_cfg = get_api_config()
            active = api_cfg.get('active_provider', '?')
            providers = api_cfg.get('providers', {})
            provider = providers.get(active, {}) if isinstance(providers, dict) else {}
            model = provider.get('model', '?')
            has_key = '✓' if provider.get('api_key', '') else '✗'
            dlog(f"API: {active}/{model} Key:{has_key}")
        except Exception as _e:
            dlog(f"API配置读取失败: {_e}")
        
        # 滚动加载保险丝：最多 16 轮 OCR（实际滚动 15 次 × 2 格 = 30 格覆盖，防 API 误判死循环）
        MAX_SCROLL_ROUNDS = 16
        for i, reg in enumerate(tasks):
            if self._batch_stop.is_set(): dlog("⏹ 停止"); break
            _prog_state['idx'] = i  # R1 布局B：进度百分比按当前地区序推进
            # v1.4.2 熔断：API 额度耗尽/鉴权失败 → 批量中止（每个省份白试无意义）
            try:
                from ocr import _api_fatal as _af
                if _af['flag']:
                    dlog("⏹ API 额度耗尽/鉴权失败，批量中止——请充值或更换 API key 后重试")
                    break
            except Exception:
                pass
            label = reg
            dlog(f"── [{label}] ({i+1}/{total}) ──")
            try:
                # 1. 截图 → 找文本框 → 优先校准坐标
                sp = os.path.join(get_base_dir(), 'output', f'_vis_{i}.png')
                os.makedirs(os.path.dirname(sp), exist_ok=True)
                ss(sp)
                # v1.4 状态机（借鉴 granblue）：省份开始前检查页面状态
                # login → 会话过期，中止整个批量；captcha/modal/empty → 跳过该省份
                from vision import ai_check_page_state as _check_state
                _st = _check_state(sp)
                if _st and _st.get('state') != 'normal':
                    _st_hint = _st.get('hint') or ''
                    if _st.get('state') == 'login':
                        dlog(f"1.✋ 页面状态=登录/会话过期：{_st_hint}，批量中止，请重新登录后重试")
                        break
                    elif _st.get('state') in ('captcha', 'modal', 'empty'):
                        dlog(f"1.✋ 页面状态={_st.get('state')}：{_st_hint}，跳过该省份")
                        continue
                tm_x = tm_y = None
                # v1.4：优先 AI 定位坐标（批量启动时实时 AI 定位，最新最准），
                # 模板匹配降为兜底——region_dropdown 模板在真实页面会误匹配
                # 「销售区域」等相似下拉框（实测偏差最大 743px），且模板坐标是
                # 窗口截图坐标，直接当全屏坐标用只在窗口最大化时近似成立。
                if dd_coord:
                    dx, dy = dd_coord['x'], dd_coord['y']
                    dlog(f"1.AI定位坐标({dx},{dy})")
                else:
                    pos = locate_element(sp, 'region_dropdown', threshold=0.80)
                    if pos:
                        tm_x, tm_y = pos[0], pos[1]
                        # 模板匹配坐标是窗口截图坐标（宽≤2560），还原到全屏
                        # ×(窗口原始宽/截图宽) + 窗口偏移；偏移量按全屏宽度比例
                        # （v1.4 全量审查修复：4K/带鱼屏下旧逻辑直接当全屏用整体偏左）
                        try:
                            _im = PILImage.open(sp)
                            _imw, _imh = _im.size
                        except Exception:
                            _imw = _imh = 0
                        _wl2 = int(win_pos.get('left', 0) or 0)
                        _wt2 = int(win_pos.get('top', 0) or 0)
                        _win_w = int(win_pos.get('width', _imw or sw) or sw)
                        _win_h = int(win_pos.get('height', _imh or sh) or sh)
                        if _imw > 0 and _imh > 0:
                            dx = int(tm_x * _win_w / _imw) + _wl2
                            dy = int(tm_y * _win_h / _imh) + _wt2
                        else:
                            dx, dy = tm_x, tm_y
                        # 点击偏移比例制：90px 相对 1920 参考宽度，按当前分辨率缩放
                        dx += int(90 * sw / 1920)
                        dlog(f"1.模板匹配({dx},{dy})")
                    else:
                        # v1.3 起完全依赖 AI 定位/模板匹配，无预设坐标兜底
                        # 宁可显式失败让用户处理，也不猜测位置乱点
                        dlog(f"1.✗ 未定位到地区下拉框（AI校准+模板匹配均失败），跳过 {reg}")
                        continue
                # 点击+粘贴+回车，最多重试 3 次（PyAutoGUI 偶发失败）
                op_ok = False
                for _attempt in range(3):
                    try:
                        pyautogui.click(dx, dy); time.sleep(0.3); pyautogui.click(dx, dy); time.sleep(0.2)
                        # 不加「省」后缀的地区（直辖市/自治区/特别行政区）
                        NO_SUFFIX = {'内蒙古','广西','西藏','宁夏','新疆',
                                     '北京','上海','天津','重庆','香港','澳门','台湾'}
                        full = reg if reg in NO_SUFFIX else reg + '省'
                        pyperclip.copy(full)
                        pyautogui.tripleClick(dx, dy); time.sleep(0.15)
                        pyautogui.hotkey('ctrl', 'v'); time.sleep(0.2)
                        dlog(f"2.粘贴'{full}'")
                        pyautogui.press('enter'); time.sleep(1.0)
                        dlog("3.回车确认")
                        op_ok = True
                        break
                    except Exception as ex:
                        if _attempt < 2:
                            dlog(f"  操作重试{_attempt+1}/3: {ex}")
                            time.sleep(0.5)
                        else:
                            dlog(f"操作失败(剪贴板/按键): {ex}")
                if not op_ok:
                    continue

                # 3.5 省份切换验证：粘贴+回车后确认筛选栏省份已切换为目标省份。
                # 页面省份没变 = 切换失败（下拉框没选上/粘贴失败）。旧版第5步只做像素
                # 变化检测，省份没变也照走，等于摆设——这里直接读回筛选栏值比对，
                # 不一致则重新走一遍「定位下拉框 → 清空 → 粘贴省份 → 回车」。
                from ocr import strip_region_suffix as _strip_region
                from vision import ai_read_selected_province as _read_province
                province_ok = False
                _last_sel = None
                _same_twice = False
                _dx2 = _dy2 = None  # AI 重定位坐标（重试时更新，裁剪区域优先用它）
                for _p_attempt in range(3):
                    _vshot = os.path.join(get_base_dir(), 'output', f'_wait_{i}_prov.png')
                    ss(_vshot)
                    # v1.4 bugfix：省份下拉框是页面顶部小控件，整页截图压缩后小字
                    # 糊成一团，模型读不出 → 粘贴成功也误报「无法识别」。按下拉框
                    # 坐标裁剪区域（全屏坐标 → 窗口截图坐标，含 4K/带鱼屏 scale
                    # 还原），特写放大后识别，识别率大幅提升。
                    # 坐标来源：用「本次实际点击粘贴的位置」（dx/dy 或重试的
                    # _dx2/_dy2）——粘贴成功说明该位置就是省份下拉框；模板匹配
                    # 原始 tm 坐标是窗口截图坐标且可能匹配到销售区域等相似框，
                    # 不能直接当裁剪中心。
                    _bx = _by = None
                    if _dx2 is not None:
                        _bx, _by = _dx2, _dy2  # 重试：AI 重定位点击坐标（全屏）
                    elif dx is not None:
                        _bx, _by = dx, dy  # 首次：本次点击粘贴位置（全屏）
                    _region = None
                    if _bx is not None and _by is not None:
                        _wl = win_pos.get('left', 0) or 0
                        _wt = win_pos.get('top', 0) or 0
                        _sx = win_pos.get('scale_x', 1.0) or 1.0
                        _sy = win_pos.get('scale_y', 1.0) or 1.0
                        _region = (int((_bx - _wl) / _sx), int((_by - _wt) / _sy),
                                   max(160, int(360 / _sx)), max(80, int(100 / _sy)))
                    _sel = _read_province(_vshot, region=_region)
                    if _sel is None:
                        # v1.4.2：无法识别且疑似 API 层失败（限流/网络抖动）——
                        # 等 5s 重截图重读一次，避免把 API 故障误报成"省份切换失败"
                        # 导致整个省份被跳过（客户日志：两省份全跳过的根因）
                        time.sleep(5)
                        ss(_vshot)
                        _sel = _read_province(_vshot, region=_region)
                    if _sel and _strip_region(_sel) == reg:
                        province_ok = True
                        dlog(f"3.✓ 省份已切换为「{_sel}」")
                        break
                    # 粘贴+回车后页面可能异步刷新（筛选栏值延迟更新）：读到值但
                    # 不匹配 → 等 0.8s 重截重读一次再判失败，避免误报触发重选
                    if _sel:
                        time.sleep(0.8)
                        ss(_vshot)
                        _sel = _read_province(_vshot, region=_region)
                        if _sel and _strip_region(_sel) == reg:
                            province_ok = True
                            dlog(f"3.✓ 省份已切换为「{_sel}」（刷新后确认）")
                            break
                    dlog(f"3.⚠ 省份验证失败（第{_p_attempt+1}次，显示:{_sel or '无法识别'}，期望:{reg}）")
                    # v1.4：检测是否验证码/弹窗/横幅（这类异常重试无效，需人工处理）
                    from vision import ai_detect_anomaly as _detect_anomaly
                    _anom = _detect_anomaly(_vshot)
                    if _anom and _anom.get('anomaly'):
                        _at = _anom.get('type') or '异常情况'
                        _ah = _anom.get('hint') or ''
                        dlog(f"3.✋ 检测到{_at}：{_ah}，需人工处理后重试（程序已暂停该省份）")
                        province_ok = False
                        break
                    # 连续两次显示值相同且未变化 → 重选机制无效，别浪费第 3 次
                    if _sel and _sel == _last_sel:
                        _same_twice = True
                        dlog("3.⚠ 显示值连续两次相同，重选无效，提前放弃")
                        break
                    _last_sel = _sel
                    # 保留失败现场截图（_prov_fail_ 前缀不走批量清理），供人工排查真因
                    try:
                        import shutil as _sh
                        _sh.copyfile(_vshot, os.path.join(get_base_dir(), 'output', f'_prov_fail_{i}_{_p_attempt}.png'))
                    except Exception:
                        pass
                    # 重新走一遍 AI 定位：后台页面可能变化（如突发横条弹窗）导致初始定位坐标偏移，
                    # 点击落在弹窗/错位上 → 粘贴没进下拉框 → 省份没变。不能用旧坐标重试。
                    import tempfile as _tf
                    from vision import ai_locate_elements as _relocate
                    _re_shot = os.path.join(_tf.gettempdir(), 'pdd_relocate_prov.png')
                    _re_pos = {}
                    capture_pdd_screenshot(_re_shot, _re_pos)
                    _re_loc = _relocate(_re_shot)
                    if _re_loc:
                        _ox2, _oy2 = _re_pos.get('left', 0), _re_pos.get('top', 0)
                        # 4K/带鱼屏还原：截图已缩到 ≤2560，AI 坐标先还原再偏移（v1.4 审查修复）
                        _rsx = _re_pos.get('scale_x', 1.0) or 1.0
                        _rsy = _re_pos.get('scale_y', 1.0) or 1.0
                        _dx2 = int(_re_loc['dropdown']['x'] * _rsx) + _ox2
                        _dy2 = int(_re_loc['dropdown']['y'] * _rsy) + _oy2
                        dlog(f"3.↻ 重新AI定位下拉框({_dx2},{_dy2}) 置信度:{_re_loc.get('confidence', 0):.0%}")
                        # 同时刷新坐标，后续省份/查询按钮也用新定位
                        dd_coord = {'x': _dx2, 'y': _dy2}
                        if _re_loc.get('query'):
                            qq_coord = {'x': int(_re_loc['query']['x'] * _rsx) + _ox2,
                                        'y': int(_re_loc['query']['y'] * _rsy) + _oy2}
                    else:
                        dlog("3.✗ 重新AI定位失败，跳过")
                        break
                    try:
                        # 正常操作：点下拉框（点开自动清空）→ 粘贴 → 回车
                        pyautogui.click(_dx2, _dy2); time.sleep(0.3)
                        pyautogui.click(_dx2, _dy2); time.sleep(0.2)
                        pyperclip.copy(full)
                        pyautogui.tripleClick(_dx2, _dy2); time.sleep(0.15)
                        pyautogui.hotkey('ctrl', 'v'); time.sleep(0.2)
                        dlog(f"  重选: 粘贴'{full}'")
                        pyautogui.press('enter'); time.sleep(1.0)
                        dlog("  重选: 回车确认")
                    except Exception as ex:
                        dlog(f"  省份重选失败: {ex}")
                        break
                if not province_ok:
                    dlog(f"3.✗ 省份切换确认失败（{reg}），跳过该省份")
                    continue

                # 4. 找查询按钮
                if qq_coord:
                    qx, qy = qq_coord['x'], qq_coord['y']
                    dlog(f"4.ai坐标({qx},{qy})")
                else:
                    dlog("4.⚠ 未校准查询按钮，跳过"); continue
                pyautogui.click(qx, qy)
                # 5. 等待页面刷新：截图变化检测（最多 10 秒，检测到页面变化即提前继续）
                _w0 = os.path.join(get_base_dir(), 'output', f'_wait_{i}_0.png')
                _w1 = os.path.join(get_base_dir(), 'output', f'_wait_{i}_1.png')
                _changed = False
                try:
                    for _rc in range(2):  # 点偏兜底：10 秒无变化 → 重定位 query 重点一次
                        if self._batch_stop.is_set():  # R2 问题：F9 在等待期内也即时生效
                            break
                        ss(_w0)
                        _changed = False
                        for _t in range(10):
                            if self._batch_stop.is_set():  # R2 问题：不再干等满 10s×2
                                break
                            time.sleep(1.0)
                            ss(_w1)
                            try:
                                im0 = PILImage.open(_w0).convert('L').resize((160, 90))
                                im1 = PILImage.open(_w1).convert('L').resize((160, 90))
                                diff = sum(1 for a, b in zip(im0.getdata(), im1.getdata()) if abs(a - b) > 12)
                                if diff > 40:  # 超过 40 个像素点差异视为页面已刷新
                                    _changed = True
                                    break
                                _w0, _w1 = _w1, _w0  # 滚动基准
                            except Exception:
                                pass
                        if _changed:
                            break
                        # 页面没变化 = 查询可能没点中（坐标偏移/弹窗遮挡）
                        # 重定位 query 按钮 → 重点 → 再等一轮（v1.4 修复
                        # 客户反馈 AI 定位后点查询偏左；点偏后果是识别到旧省份数据）
                        if _rc == 0:
                            dlog("5.⚠ 查询后页面未变化，重定位查询按钮重试")
                            try:
                                import tempfile as _tf5
                                from vision import ai_locate_elements as _reloc5
                                _rs = os.path.join(_tf5.gettempdir(), 'pdd_reloc_query.png')
                                _rp = {}
                                capture_pdd_screenshot(_rs, _rp)
                                _rl = _reloc5(_rs)
                                if _rl and _rl.get('query'):
                                    _rsx = _rp.get('scale_x', 1.0) or 1.0
                                    _rsy = _rp.get('scale_y', 1.0) or 1.0
                                    _wl5 = int(_rp.get('left', 0) or 0)
                                    _wt5 = int(_rp.get('top', 0) or 0)
                                    qx = int(_rl['query']['x'] * _rsx) + _wl5
                                    qy = int(_rl['query']['y'] * _rsy) + _wt5
                                    qq_coord = {'x': qx, 'y': qy}
                                    dlog(f"5.↻ 重定位查询按钮({qx},{qy}) 置信度:{_rl.get('confidence', 0):.0%}")
                                    pyautogui.click(qx, qy)
                                    continue
                                dlog("5.✗ 重定位查询按钮失败")
                            except Exception as _e5:
                                dlog(f"5.✗ 重试查询失败: {_e5}")
                        break
                    if not _changed:
                        # v1.4.2 防跨省串数据：查询后两轮（20s）页面均无变化 = 查询未生效
                        # （点偏/弹窗遮挡），此时列表还是上一省份数据——继续识别会把
                        # 上一省商品贴上本省标签（客户实测：省1数据 2 次出现在省2页面）。
                        # 显式失败跳过该省，绝不带病识别。
                        dlog("5.✗ 查询未生效（两轮检测页面无变化），跳过该省——避免识别到上一省份数据")
                        continue
                    dlog("5.页面刷新完成（变化检测）")
                finally:
                    for _p in (_w0, _w1):
                        try: os.remove(_p)
                        except Exception: pass

                # 6. 截图 → AI 定位表格（bbox + has_more）→ OCR → 滚动循环
                table_bbox = None
                scroll_round = 0
                _total_hint = None  # 页面统计总条数（首轮 AI 定位顺带读取，结束后对比识别量）
                seen_sku = {}  # 已见 sku_id → name（权威去重：滚动重识别/名字波动/ID错位都拦）
                seen_name_no_sku = set()  # 无 ID 商品登记过的 name
                seen_name_with_id = set()  # 有 ID 商品登记过的 name
                _fps = []  # 滚动内容指纹（每轮 stock 集合，滚动到底后稳定）
                round_items = []  # 该组合全部轮次的识别结果
                _retried_no_new = False  # 防误停：本轮已重试过无新增（v1.4.2 滚动可靠性）
                _round_fail = 0  # v1.4.2：滚动轮连续无数据计数（防死滚）
                while scroll_round < MAX_SCROLL_ROUNDS:
                    if self._batch_stop.is_set(): break
                    sp2 = os.path.join(get_base_dir(), 'output', f'_result_{i}_{scroll_round}.png')
                    ss(sp2)
                    try:
                        im = PILImage.open(sp2); w, h = im.size
                        if w > 2560: im = im.resize((2560, int(h*2560/w)), PILImage.LANCZOS); im.save(sp2)
                    except Exception as _e:
                        dlog(f"  截图压缩失败(继续): {_e}")
                    # R2 问题：识别现场逐轮模糊检测（detect_blur 失败安全；worker 线程
                    # 纯图像分析无 Tk 依赖）——批量收尾清理 _result_*.png 前必须采集，
                    # 否则 _fill_from_ocr 无截图源可判（旧实现误用残留 _last_ocr_image_path）
                    try:
                        from ocr import detect_blur as _dbg_blur
                        if _dbg_blur(sp2)[0]:
                            self._batch_blur_seen = True
                            dlog("6.⚠ 本轮截图模糊，识别结果建议复核")
                    except Exception:
                        pass
                    # 首轮：AI 定位表格；滚动轮每轮重新定位——滚动后表格内容/位置变化，
                    # 旧 bbox 失效是滚动轮识别错乱（串名/重复/JSON截断）的根因（v1.4.2 修复）。
                    # 滚动轮用 2 采样（1 采样失败率过高会读不到 has_more 导致滚动决策失效）
                    ai_has_more = None  # None=AI定位失败未知, True=还有更多, False=已到底
                    if scroll_round == 0 or table_bbox is None or scroll_round > 0:
                        from vision import ai_locate_table, ai_read_total_count
                        loc = ai_locate_table(sp2, samples=2 if scroll_round > 0 else 3)
                        if loc:
                            table_bbox = loc.get('table')
                            ai_has_more = bool(loc.get('has_more', False))
                            _total_hint = loc.get('total_count')
                            # v1.4.2 总数权威化 + 缓存（find-bugs ）：首轮用右下角分页栏
                            # 特写重读官方"共有N条"覆盖 loc 整屏读取（页面5个读成3的根因）；
                            # 滚动轮总数不变，仅当仍为 None（定位失败）时补读——避免每轮白耗一次 API
                            if scroll_round == 0 or not _total_hint:
                                try:
                                    _t3 = ai_read_total_count(sp2)
                                    if _t3:
                                        _total_hint = _t3
                                except Exception:
                                    pass
                            if ai_has_more:
                                dlog(f"6.AI检测到还有更多商品，自动滚动加载...")
                            elif scroll_round > 0 and ai_has_more is not None:
                                dlog(f"6.AI确认滚动后已到底")
                        else:
                            # 定位失败（如商品少表格过矮校验不过）也尽量读页面总数，供结束后对比
                            _total_hint = ai_read_total_count(sp2)
                        if _total_hint:
                            dlog(f"6.📋 页面共约{_total_hint}个商品（识别量将与此对比）")
                    dlog(f"6.{'首屏' if scroll_round == 0 else f'滚动{scroll_round}'}OCR识别中({'双模型' if dual_verify else '单模型'})...")
                    items = None
                    # v1.4.2 滚动轮降级：整表大图每轮识别 30s+，失败重试 3 次会卡 90s+——
                    # 滚动轮（scroll_round>0）失败上限 2 次（网络抖动常见，1 次太激进
                    # 客户实测 2 次全失败就停，页面9个只识别5个）；首轮 3 次
                    _rep = 2 if scroll_round > 0 else 3
                    _was_net_err = False
                    for retry in range(_rep):
                        try:
                            # v1.4.2 方案A（阿洋拍板）：批量识别改走「整表无 bbox」——
                            # 与实时截图同路径（模型自己数行）。三路径实测证明：AI 行边界
                            # 在大表上数错（9 商品数出 20 边界→行切分切碎→乱数据/幻觉），
                            # 整表无 bbox 反而 9 行全对且 ID 保留。滚动判断仍用
                            # ai_locate_table 的 has_more/bbox（下方滚动段），识别不依赖。
                            items = self._ocr_generic_to_items(sp2, table_bbox=None,
                                                              dual_verify=dual_verify,
                                                              row_bboxes=None)
                            # v1.4.2 幻觉行交叉校验：识别数 > 页面总数 → 模型编造了
                            # 不存在的商品行（省1 3商品识别5个的根因）。二次识别后
                            # 取两轮 name 交集——真商品两轮都出现（稳定），幻觉行
                            # 随机生成两轮不同 → 被剔除。⚠ 匹配用模糊（编辑距离≤2+前4字
                            # 相同）：整表 verify 与首轮 name 可能有 OCR 波动，
                            # 严格相等会误删真实行（find-bugs 审查发现）
                            # v1.4.2 总数可信度校准：AI 定位读右下角"共N条"偶发偏小/漏读
                            # （客户实测：页面5个读成3），首轮整表识别行数是直接证据——
                            # 识别数 > 总数时以识别数校准，避免误触幻觉过滤把真实行全删
                            try:
                                if items and _total_hint and len(items) > int(_total_hint):
                                    dlog(f"6.⚠ 页面总数{int(_total_hint)} < 首轮识别{len(items)}个，以识别量为准校准总数")
                                    _total_hint = len(items)
                            except (TypeError, ValueError):
                                pass
                            # 幻觉行交叉校验：总数校准后仍超出的才触发（识别数 > 总数为真）
                            try:
                                if items and _total_hint and len(items) > int(_total_hint):
                                    from ocr import ocr_table_verify, _lev as _lev2
                                    dlog(f"6.⚠ 识别{len(items)}个 > 页面总数{int(_total_hint)}，交叉校验剔除幻觉行")
                                    _vr = ocr_table_verify(sp2, table_bbox=None)['rows'] or []
                                    _vn = [str((it or {}).get('name', '')).replace(' ', '').lower()
                                           for it in _vr if (it or {}).get('name')]
                                    def _name_hit(_nm):
                                        _m = _nm.replace(' ', '').lower()
                                        for _v in _vn:
                                            if _v == _m:
                                                return True
                                            if (abs(len(_v) - len(_m)) <= 2 and _v[:4] == _m[:4]
                                                    and _lev2(_v, _m) <= 2):
                                                return True
                                        return False
                                    _before = len(items)
                                    _filtered = [it for it in items if _name_hit(str(it.get('name', '')))]
                                    # find-bugs ：verify 只认出一半以下 = verify 本身漏识别
                                    # （非幻觉），裁剪会误删真行——保留首轮全量并警告
                                    if _filtered and len(_filtered) < _before // 2 + 1:
                                        dlog(f"6.⚠ 交叉校验仅匹配 {len(_filtered)}/{_before} 行（verify 疑似漏识别），"
                                             f"保留首轮全量不裁剪")
                                    elif _filtered:
                                        items = _filtered
                                        if len(items) < _before:
                                            dlog(f"6.✓ 幻觉行过滤: {_before} → {len(items)} 个")
                                    else:
                                        # v1.4.2 全删保护（客户实测 5→0）：verify 与首轮 name 全不匹配
                                        # （verify返回空/OCR波动/总数误读）时，全删=真实数据全丢 +
                                        # items空 → 无效重试死循环。保底保留首轮全量，宁可多不可无。
                                        dlog(f"6.⚠ 交叉校验全不匹配（verify {len(_vr)}行 vs 首轮{_before}行），"
                                             f"疑似总数误读/OCR波动——保底保留首轮 {_before} 行")
                            except Exception:
                                pass  # 校验失败保留原结果（不阻断）
                            if items: break
                            dlog(f"  重试{retry+1}...")
                            time.sleep(2)
                        except (BatchCancelled, VisionCancelled):
                            dlog("⏹ 紧急停止")
                            break  # v1.4.2 紧急停止：取消异常立即中断，不再重试
                        except Exception as ex:
                            # v1.4.2 类型化重试：鉴权/额度等致命错误直接熔断停止；
                            # 限流（429）加长延时防连续触发；其余正常 2s 重试
                            try:
                                from ocr import _is_fatal_api_err, _mark_api_fatal
                                if _is_fatal_api_err(ex):
                                    _mark_api_fatal(ex)
                                    dlog(f"  OCR致命错误（额度/鉴权），停止重试: {str(ex)[:80]}")
                                    break
                            except Exception:
                                pass
                            _es = str(ex).lower()
                            # v1.4.2 错误分诊（客户实测大表必超时，并非网络抖动）
                            # - 读超时（ReadTimeout/read timed out）= 模型处理大图太慢，需等待而非重试
                            # - 连接失败（connection/pool/socket）= 网络不通
                            # - 限流（429）= 降速重试
                            _is_read_timeout = 'readtimedout' in _es.replace(' ', '') or ('read' in _es and ('timeout' in _es or 'timed out' in _es))
                            _is_conn_err = any(k in _es for k in ('connection', 'pool', 'socket')) and not _is_read_timeout
                            _es_429 = any(k in _es for k in ('429', 'rate ', 'too many', 'triggered rate', 'flow control'))
                            _was_net_err = _was_net_err or _is_conn_err or _is_read_timeout
                            # v1.4.2 读超时也归入"非无数据"：模型处理大图慢 ≠ 页面没有新行，
                            # 与断网一样不计入防死滚惩罚（find-bugs 审查 ）
                            _ed = 5 if _is_conn_err else (10 if _es_429 else 2)
                            if _is_read_timeout:
                                dlog(f"  OCR超时：模型处理大图超过预留时间（已延长至180s），若持续发生请降低勾选列数或分批识别（{str(ex)[:120]}）")
                            else:
                                dlog(f"  OCR异常（{_ed}s 后重试）: {str(ex)[:120]}{'（网络/连接）' if _is_conn_err else ''}")
                            time.sleep(_ed)
                    # 合并：同仓库内去重（sku_id 为权威锚点，无 ID 回退 name），跨仓库保留
                    new_in_round = 0
                    if items:
                        from ocr import dedup_items
                        for it in dedup_items(items, seen_sku, seen_name_no_sku, seen_name_with_id):
                            it['region'] = reg
                            # warehouse 保留 OCR 识别值（仓库信息列），不再手动覆盖
                            round_items.append(it)
                            new_in_round += 1
                    if items:
                        dlog(f"6.✓ 本轮{len(items)}个，新增{new_in_round}个")
                        _round_fail = 0  # v1.4.2：有数据重置失败计数
                    else:
                        dlog("6.无数据")
                        if not _was_net_err:
                            # v1.4.2：网络/连接失败不算"无数据"，不计入防死滚计数——
                            # 网络恢复后继续滚抓，不会提前'补滚无果'结束（9个只识别5个）
                            _round_fail += 1
                        else:
                            dlog("6.⏳ 本轮网络异常（不计入无数据），下轮继续尝试")
                    # 内容指纹：本轮识别商品的仓库总库存值集合（模型可能乱编名字/ID，
                    # 但总库存列相对稳定；滚动到底后集合不再变化 → 提前结束，防无限空转）
                    _fp = tuple(sorted(str(it.get('stock', '')) for it in (items or []) if it.get('stock') is not None))
                    _fps.append(_fp)
                    # 滚动决策
                    # - 首轮：AI has_more=True → 滚；AI 定位失败(未知)且本轮有商品 → 滚一次确认；AI 明确 False → 不滚
                    # - 后续轮：本轮有新商品 → 继续滚；连续无新增 → 结束（保险）
                    # v1.4.2 官方总数权威硬停：累计识别量 >= 右下角"共有N条" → 识别齐全，
                    # 立即结束滚动（不再靠"无新增/重试撞"来判定结没结束——客户要求直接用
                    # 官方实际数据对比确认）。N 来自右下角分页栏特写，权威可信。
                    try:
                        if (round_items and _total_hint
                                and len(round_items) >= int(_total_hint)):
                            dlog(f"6.✓ 累计{len(round_items)}个 >= 页面总数{int(_total_hint)}，识别齐全，结束滚动")
                            break
                    except (TypeError, ValueError):
                        pass
                    if scroll_round == 0:
                        if ai_has_more is False:
                            dlog("6.✓ AI确认表格已到底，无需滚动")
                            break
                        should_scroll = bool(items) and (ai_has_more is not False)
                    else:
                        should_scroll = new_in_round > 0
                        # v1.4.2 右下角总数权威：滚动停止前对比累计识别量 vs 页面总数
                        # （右下角"共有N条"分页统计，后端渲染权威）——累计 < 总数 → 疑似
                        # 还有商品没滚出来（滚动轮漏识别），即使本轮无新增也继续滚补抓
                        _under_target = False
                        try:
                            if round_items and _total_hint and len(round_items) < int(_total_hint):
                                # 仅在 AI 没明确确认到底时按数量补滚（已到底则数量可能虚高，勿强求）
                                _under_target = (ai_has_more is not False)
                        except (TypeError, ValueError):
                            _under_target = False
                        if not should_scroll:
                            # v1.4.2 防误停：滚动轮无新增但 AI 未确认到底（has_more
                            # 未知或 True）→ 单次 OCR 质量波动可能漏识别新商品，直接
                            # 停会漏数据（客户反馈滚动机制"无法正常调用"即此类）。
                            # 给一次重试：重新截图+识别本轮，仍无新增才停止。
                            if ai_has_more is not False and not _retried_no_new:
                                _retried_no_new = True
                                dlog("6.⚠ 本轮无新增但未确认到底，重试识别一次防误停")
                                time.sleep(1.0)
                                continue  # 重走本轮（scroll_round 不变，_retried 防死循环）
                            if _under_target and _round_fail < 2:
                                dlog(f"6.⚠ 累计{len(round_items)}个 < 总数{int(_total_hint)}，继续滚动补抓")
                                should_scroll = True
                            elif _under_target:
                                dlog(f"6.⏹ 连续{_round_fail}轮无数据，补滚无果，结束滚动（累计{len(round_items)}/{int(_total_hint)}）")
                                break
                            else:
                                dlog(f"6.⏹ 滚动{scroll_round}轮后无新增，结束")
                                break
                        elif _under_target:
                            dlog(f"6.↻ 累计{len(round_items)}个 < 总数{int(_total_hint)}，继续滚动")
                        # 连续3轮页面内容无变化 → 已到底，结束（doubao 等模型每轮"新增"可能永远>0）
                        if len(_fps) >= 3 and _fps[-1] == _fps[-2] == _fps[-3]:
                            dlog("6.⏹ 连续3轮页面内容无变化，结束滚动")
                            break
                        # 滚动轮每轮重新定位（v1.4.2）→ has_more 是新一轮准确判断
                        if ai_has_more is False and scroll_round > 0:
                            dlog("6.✓ AI确认已到底，结束滚动")
                            break
                    scroll_round += 1
                    if scroll_round >= MAX_SCROLL_ROUNDS:
                        dlog(f"6.⏹ 达到最大滚动轮次({MAX_SCROLL_ROUNDS})，结束")
                        break
                    # 滚动：在表格区域向下滚动 2 格，等待加载
                    # 坐标换算：capture_pdd_screenshot 内部把窗口截图缩到宽≤2560 保存，
                    # AI bbox 是相对该缩放图的比例；滚动作用于真实屏幕，用比例×当前屏
                    # 幕尺寸还原（窗口通常最大化/居中，落在表格区域足够触发滚轮）。
                    try:
                        if table_bbox:
                            try:
                                _im_orig = PILImage.open(sp2)
                                _ow, _oh = _im_orig.size
                            except Exception:
                                _ow = _oh = 0
                            if _ow > 0:
                                # bbox 是截图（窗口区域）内坐标；截图已被 capture 缩放到宽≤2560，
                                # 用 win_pos['width']（窗口原始宽）还原回窗口像素，再加窗口左上角偏移
                                _wl = int(win_pos.get('left', 0) or 0)
                                _wt = int(win_pos.get('top', 0) or 0)
                                _win_w = int(win_pos.get('width', _ow) or _ow)
                                _win_h = int(win_pos.get('height', _oh) or _oh)
                                _sx = _win_w / _ow if _ow > 0 else 1.0
                                _sy = _win_h / _oh if _oh > 0 else 1.0
                                cx = int(((table_bbox['left'] + table_bbox['right']) / 2) * _sx) + _wl
                                cy = int((((table_bbox['top'] + table_bbox['bottom']) * 0.82) * _sy)) + _wt
                            else:
                                cx = sw // 2
                                cy = int(sh * 0.62)
                            # v1.4.2 滚动修复：浏览器在前台假设成立（PDD EZ 已最小化），
                            # 真实失效点 = HUD 是 -topmost 永远压着浏览器 + 光标落点可能
                            # 不在表格可滚区 + 无像素验证无法判断是否真滚了（盲滚）。
                            # 方案：多位置尝试 + 滚动前后像素验证，失败明确提示。
                            pass
                        # 滚动：v1.4.2 大修—— 先激活浏览器前台（根治 scroll 被别的
                        # 顶层窗口吃掉 →"跑其他页面"）； 落点全部 clamp 进浏览器窗口
                        # 内、避开 HUD 右上 topmost 区与任务栏(y<sh-90)； 滚动前后只对比
                        # 窗口中部横带（排除 HUD/页码/加载动画的假阳）； 鼠标通道全部
                        # 失败 → 降级键盘 pagedown，仍失败明确提示不盲滚。
                        def _activate_browser():
                            """把商家后台浏览器窗口激活到前台；失败返回 False"""
                            try:
                                import pygetwindow as gw
                                for title in ('拼多多', 'pinduoduo', 'Microsoft Edge', 'Edge', 'Chrome', 'Firefox'):
                                    wins = gw.getWindowsWithTitle(title)
                                    if not wins:
                                        continue
                                    _w = wins[0]
                                    if '拼多多' not in title and 'pinduoduo' not in title.lower():
                                        for w in wins:
                                            try:
                                                if w.isActive:
                                                    _w = w
                                                    break
                                            except Exception:
                                                pass
                                    if getattr(_w, 'isMinimized', False):
                                        try:
                                            _w.restore()
                                            time.sleep(0.3)
                                        except Exception:
                                            pass
                                    try:
                                        _w.activate()
                                    except Exception:
                                        import ctypes
                                        try:
                                            ctypes.windll.user32.SetForegroundWindow(int(_w._hWnd))
                                        except Exception:
                                            return False
                                    time.sleep(0.4)
                                    return True
                            except Exception:
                                return False
                            return False
                        _bw = _activate_browser()
                        if not _bw:
                            dlog("6.⚠ 找不到浏览器窗口，跳过本轮滚动")
                            time.sleep(1.0)
                        else:
                            # 滚动前刷新窗口矩形：光标落点不能超出窗口/屏幕范围
                            # （v1.4.2：PrintWindow 后台截图不抢焦点，滚动前必须重取当前矩形）
                            _win_rect = {}
                            try:
                                capture_pdd_screenshot(
                                    os.path.join(get_base_dir(), 'output', f'_wr_{i}_{scroll_round}.png'), _win_rect)
                                _WL = int(_win_rect.get('left', 0) or 0)
                                _WT = int(_win_rect.get('top', 0) or 0)
                                _WW = int(_win_rect.get('width', _ow) or _ow)
                                _WH = int(_win_rect.get('height', _oh) or _oh)
                            except Exception:
                                _WL = int(win_pos.get('left', 0) or 0)
                                _WT = int(win_pos.get('top', 0) or 0)
                                _WW = int(win_pos.get('width', _ow) or _ow)
                                _WH = int(win_pos.get('height', _oh) or _oh)
                            _max_y = sh - 90  # 避开任务栏
                            # 落点候选：表格中央多档 + 窗口左列中下部；全部 clamp 进窗口、避开 HUD 右上
                            _cands = []
                            for _px, _py in [(cx, cy), (sw // 2, int(sh * 0.62)),
                                             (cx, int(sh * 0.72)),
                                             (int(_WL + _WW * 0.40), int(_WT + _WH * 0.55)),
                                             (int(_WL + _WW * 0.30), int(_WT + _WH * 0.60))]:
                                _x = max(_WL + 6, min(int(_px), _WL + max(_WW - 6, 6)))
                                _y = max(_WT + 6, min(int(_py), min(_max_y, _WT + max(_WH - 6, 6))))
                                if not (0 < _x < sw and 0 < _y < sh):
                                    continue
                                # 避开 HUD topmost 区（右上角 sw-430..sw, 20..280）
                                if sw - 430 <= _x <= sw and 20 <= _y <= 280:
                                    continue
                                _cands.append((_x, _y))
                            # 区域化快照：窗口中部横带（表格主体区域，排除 HUD/页码/加载动画）
                            def _snap_region(_tag):
                                _sp3 = os.path.join(get_base_dir(), 'output', f'_scroll_c_{i}_{scroll_round}_{_tag}.png')
                                capture_pdd_screenshot(_sp3)
                                try:
                                    _im = PILImage.open(_sp3)
                                    _w3, _h3 = _im.size
                                    # 中部 8%~62% 高度横带：滚动表格必经区域，
                                    # HUD(右上)/页码(右下)/加载动画被排除 → 验证更贴近真实滚动
                                    return _im.convert('L').crop((0, int(_h3 * 0.08), _w3, int(_h3 * 0.62))).resize((240, 80))
                                except Exception:
                                    return None
                            _scrolled = False
                            # 鼠标滚轮主通道：多位置尝试
                            for _n, (_px2, _py2) in enumerate(_cands):
                                if self._batch_stop.is_set():
                                    break
                                _s0 = _snap_region('a')
                                try:
                                    pyautogui.moveTo(_px2, _py2); time.sleep(0.25)
                                    # v1.4.2 力度：-4(无效)→-40→-200→-300（客户实测：PDD 单页 10 项，
                                    # 需一次滚过整屏高度；当前总力度 600 = -300×2，仍不够继续加）
                                    pyautogui.scroll(-300); time.sleep(0.15)
                                    pyautogui.scroll(-300)
                                except Exception:
                                    continue
                                time.sleep(0.9)
                                _s1 = _snap_region('b')
                                if _s0 is not None and _s1 is not None:
                                    _df = sum(1 for a, b in zip(_s0.getdata(), _s1.getdata()) if abs(a - b) > 12)
                                    if _df > 24:  # 中部横带变化 = 表格确实滚了
                                        _scrolled = True
                                        dlog(f"6.↘ 滚动生效（变化{_df}px @位置{_n + 1}）")
                                        break
                            # 键盘降级通道：鼠标通道全失败 → pagedown（发给前台浏览器）
                            if not _scrolled and not self._batch_stop.is_set():
                                _s0 = _snap_region('a')
                                try:
                                    pyautogui.press('pagedown')
                                except Exception:
                                    pass
                                time.sleep(0.9)
                                _s1 = _snap_region('b')
                                if _s0 is not None and _s1 is not None:
                                    _df = sum(1 for a, b in zip(_s0.getdata(), _s1.getdata()) if abs(a - b) > 12)
                                    if _df > 24:
                                        _scrolled = True
                                        dlog("6.↘ 滚动生效（键盘 pagedown 降级通道）")
                            if not _scrolled:
                                dlog("6.⚠ 滚动未生效——浏览器未在前台/表格无内容可滚/页面被遮挡，"
                                     "请确认浏览器窗口在前台、HUD 未遮挡表格")
                            # 清理验证截图
                            for _tag in ('a', 'b'):
                                try:
                                    os.remove(os.path.join(get_base_dir(), 'output', f'_scroll_c_{i}_{scroll_round}_{_tag}.png'))
                                except Exception:
                                    pass
                            try:
                                os.remove(os.path.join(get_base_dir(), 'output', f'_wr_{i}_{scroll_round}.png'))
                            except Exception:
                                pass
                            time.sleep(1.0)  # 等滚动加载渲染
                    except Exception as ex:
                        dlog(f"  滚动失败: {ex}")
                        break
                # 页面总数对比：确认开始前读到的总条数与实际识别量一致，防假数据虚增/漏识别
                if _total_hint and round_items:
                    _diff = '' if len(round_items) == _total_hint else '（数量不一致，请核对）'
                    dlog(f"6.✓ 页面共{_total_hint}个商品，识别到{len(round_items)}个{_diff}")
                elif _total_hint and not round_items:
                    dlog(f"6.⚠ 页面显示{_total_hint}个商品，但未识别到任何数据")
                if round_items:
                    result_queue.put(round_items)
                    success += 1; total_items += len(round_items)
                    dlog(f"6.✓ 合计{len(round_items)}个商品")
                else:
                    dlog("6.无数据")
            except (BatchCancelled, VisionCancelled):
                dlog("⏹ 紧急停止")
                break  # v1.4.2 紧急停止：立即中断整个省份任务
            except Exception as e:
                dlog(f"✗ {e}")
        
        # 发送结束信号 + 集中清理临时截图（_vis_/_wait_/_result_ 前缀）
        # finally 保证异常路径也会清理，防止临时文件持续堆积
        try:
            result_queue.put(None)
        finally:
            try:
                _out_dir = os.path.join(get_base_dir(), 'output')
                for _f in os.listdir(_out_dir):
                    if _f.startswith(('_vis_', '_wait_', '_result_')) and _f.endswith('.png'):
                        try:
                            os.remove(os.path.join(_out_dir, _f))
                        except Exception:
                            pass
            except Exception:
                pass
        
        self.win.after(0, self.win.deiconify)
        # 不再在这里启动轮询——由 TaskQueue 的 on_done 回调触发
        if hud:
            time.sleep(1)
            def _safe_destroy():
                try:
                    if hud.winfo_exists():
                        hud.destroy()
                except Exception:
                    pass  # 窗口已被用户手动关闭
            self.win.after(0, _safe_destroy)
    
    def _poll_batch_queue(self, q, success, total, total_items, idle=0):
        """主线程每 100ms 轮询队列，逐批刷新 UI（避免一次性创建大量控件导致假死）。
        idle 为『连续空闲』计数：收到新数据立即清零，连续 6000 次空闲（=10 分钟）
        视为后台线程已异常终止，强制收尾——不会截断仍在正常产出的批量任务。
        多批次（分仓库）结果累积后一次性填充，避免后批覆盖前批。"""
        import queue as _queue
        got_data = False
        all_batch = []
        try:
            while True:
                items = q.get_nowait()
                if items is None:
                    # 后台线程已完成所有任务：统一填充累积结果
                    if got_data:
                        try:
                            self._fill_from_ocr(all_batch, source='batch')
                        except Exception as e:
                            self.status_text.set(f"❌ 批量数据处理失败: {str(e)[:50]}")
                    self.win.after(100, lambda: self._finish_batch(success, total, total_items))
                    return
                all_batch.extend(items)
                got_data = True
        except Exception as _e:
            # 仅队列空继续轮询；其他异常（如队列对象异常）提示后继续
            if not isinstance(_e, _queue.Empty):
                self.status_text.set(f"❌ 批量轮询异常: {str(_e)[:50]}")
        
        idle = 0 if got_data else idle + 1
        if idle >= 6000:
            # 后台线程可能已崩溃：强制收尾，避免死循环。
            # 阈值 6000 × 100ms = 10 分钟 —— 一个地区完整滚动识别（最多 16 轮截图/OCR，
            # 每轮含 180s 读超时 × 2~3 次重试，v1.4.5 起）在弱网/长表格下可能超过 3 分钟，需给足余量。
            self.win.after(100, lambda: self._finish_batch(success, total, total_items))
            return
        self.win.after(100, lambda: self._poll_batch_queue(q, success, total, total_items, idle))
    
    def _finish_batch(self, success, total, total_items):
        """批量识别收尾：恢复按钮 + 显示结果"""
        self.export_btn.configure(state='normal')
        try:
            if getattr(self, 'live_btn', None):
                self.live_btn.configure(state='normal')  # v1.4.5 bug hunt F24
        except Exception:
            pass
        # D：恢复按钮文案（与禁用态对称）
        self._set_batch_btn_state(False)
        self._reset_batch_progress()  # R1 布局B：批量收尾进度条隐藏复位
        self._batch_running = False  # v1.4.6 bug hunt F24 重入守卫：UI 真正收尾时清除标志
        self._refresh_cost_label()  # v1.4.7 WS-C：批量收尾主动刷费用 Label
        self.status_text.set("就绪 — 批量识别完成")
        if success > 0:
            messagebox.showinfo("批量识别完成", f"成功 {success}/{total} 地区\n合计 {total_items} 商品")
        else:
            messagebox.showwarning("批量识别失败",
                                   "未成功识别任何地区\n\n请检查：\n1. 网络是否正常\n2. API Key / 模型配置\n3. PDD 页面是否在前台显示")

    def _preview_batch_cost(self, region_count: int, dual_verify: bool = False) -> bool:
        """t14 P3-B：批量前成本预估确认。

        按「省份数 × 预计轮次」估算调用次数区间：
        - 每次省份：1 次总数读取 + 1 次/轮表格定位 + 1 次/轮 OCR × 模型数
        - 轮次区间：3 ~ 8 轮（保守估计，每轮 ~10-20 商品滚动加载）

        用 usage.pricing 折算金额（CNY 区间）：
        - input_per_million: input token 单价 (CNY/M tokens)
        - output_per_million: output token 单价
        - image_per_call: 图片调用单价（按次计费模型）

        价格表为空或缺价 → 弹窗标题「（价格未配置，仅按 token 数提示）」，
        显示预计调用次数而非金额。

        用户取消 → 返回 False（不置任何熔断标志，纯前置确认）。
        估算逻辑任何异常 → 吞掉 + log + 返回 True（绝不阻塞批量）。

        :return: True=用户确认继续；False=用户取消
        """
        try:
            # 1. 估算轮次（保守：3 ~ 8 轮）
            min_rounds, max_rounds = 3, 8
            n_models = 2 if dual_verify else 1
            # 每次省份每轮调用：1 定位 + 1 OCR × 模型数
            per_round_calls_per_region = 1 + 1 * n_models
            min_calls = region_count * (1 + min_rounds * per_round_calls_per_region)  # 含总数读取 1 次
            max_calls = region_count * (1 + max_rounds * per_round_calls_per_region)

            # 2. 读价格表
            try:
                from utils import get_usage_cfg
                cfg = get_usage_cfg()
                pricing = (cfg or {}).get('pricing', {}) or {}
            except Exception:
                pricing = {}
            # 价格表是否完整（至少需要 input 或 output 或 image 之一）
            has_pricing = bool(
                pricing.get('input_per_million')
                or pricing.get('output_per_million')
                or pricing.get('image_per_call')
            )

            # 3. 计算金额区间
            in_price = _to_float_safe(pricing.get('input_per_million'))
            out_price = _to_float_safe(pricing.get('output_per_million'))
            img_price = _to_float_safe(pricing.get('image_per_call'))
            # 每调用默认 token：input 1500 / output 800（保守估计；多模态图片通常 1000-2000）
            avg_input_tokens = 1500
            avg_output_tokens = 800

            def _cost(calls):
                # 假设每调用既有 input 也有 output；按 image 模型时仅算 image
                in_c = calls * avg_input_tokens / 1_000_000 * in_price if in_price else 0
                out_c = calls * avg_output_tokens / 1_000_000 * out_price if out_price else 0
                img_c = calls * img_price if img_price else 0
                return in_c + out_c + img_c

            title_suffix = ""
            if has_pricing:
                min_cny = _cost(min_calls)
                max_cny = _cost(max_calls)
                cost_text = f"预计金额：{_fmt_yuan(min_cny)} ~ {_fmt_yuan(max_cny)} CNY"
            else:
                title_suffix = "（价格未配置，仅按 token 数提示）"
                cost_text = f"预计调用：{min_calls} ~ {max_calls} 次"

            # 4. 弹窗确认
            msg = (
                f"批量识别即将开始：\n"
                f"  地区数：{region_count}\n"
                f"  模式：{'双模型' if dual_verify else '单模型'}\n"
                f"  预计轮次/省：{min_rounds} ~ {max_rounds}\n"
                f"  {cost_text}\n\n"
                f"是否继续？"
            )
            try:
                from tkinter import messagebox as _mb
                ok = _mb.askyesno(f"批量前成本预估 {title_suffix}".strip(), msg, parent=self.win)
            except Exception:
                # 极罕见：messagebox 不可用时默认放行（绝不阻塞）
                return True
            return bool(ok)
        except Exception:
            # 估算逻辑失败 → 静默放行（用户裁定：绝不阻塞批量）
            return True


    def _ensure_api_ready(self):
        """识别入口预检（v1.5.9）：API 未配置 → 标题明示「API 未配置」的引导弹窗。

        用户反馈「点识别弹错但没说清是 API 问题」——此前靠 API 调用抛错后的
        归类兜底（标题泛化为『识别失败』）。现在入口前置检查：缺失 key/模型时
        直接告诉用户缺什么、去哪填，并提供一键跳转「API 管理」页。返回 False
        表示未就绪（调用方直接 return，不启动识别）。
        """
        st = api_config_status()
        if st.get('ok'):
            # v1.5.11：主配置就绪时，副模型配置问题以状态栏建议呈现（不阻塞识别）
            _sec_hint = st.get('sec_hint') or ''
            if _sec_hint and st.get('sec_issue') in ('ocr_only', 'same_as_main'):
                try:
                    self.status_text.set(f"⚙ {_sec_hint}（不影响本次单模型识别）")
                    try:
                        log.info(f"副模型提示（{st.get('sec_issue')}）：{_sec_hint}")
                    except Exception:
                        pass
                except Exception:
                    pass
            return True
        prov = st.get('provider', 'unknown')
        _disp = _PROVIDER_DISPLAY.get(prov, prov)
        _mapping = {'api_key': 'API Key', 'model': '识别模型'}
        _miss_names = '、'.join(_mapping.get(m, m) for m in (st.get('missing') or [])) or 'API 配置'
        try:
            from tkinter import messagebox as _mb
            _go = _mb.askyesno(
                "API 未配置",
                f"当前识别通道（{_disp}）还没有配置：{_miss_names}。\n\n"
                "识别功能需要先在「设置 → API 管理」填写：\n"
                "  · API Key —— 在对应平台申请的密钥\n"
                "  · 模型 —— 视觉模型（如 qwen3-vl-plus / glm-4.6v / Doubao-Seed）\n\n"
                "是否现在打开「API 管理」页面？",
                parent=self.win)
        except Exception:
            _go = False
        if _go:
            try:
                self._show_page(self.page_api)
            except Exception:
                pass
        return False

    def _open_import_menu(self):
        """v1.5.7 导入入口：点击弹菜单二选一（导入表格文件 / 选择图片文件）。

        菜单数据来自 home_actions.IMPORT_MENU_ITEMS（单一事实源，find_menu_item
        消费，防契约漂移）。仅 tk.TclError（grab/widget 交互失败）兜底到表格导入
        并显式提示；其他异常原样冒泡（DESIGN §4 显式失败，绝不静默跳转）。
        v1.5.8（F2）：菜单用后即 destroy，防 widget 树累积与 grab 残留。
        """
        try:
            from home_actions import IMPORT_MENU_ITEMS, menu_labels
        except Exception as _e:
            self.status_text.set(f"⚠ 导入菜单初始化失败，已回退表格导入：{str(_e)[:60]}")
            self._import_table()
            return
        import tkinter.messagebox as _tkmsg
        menu = None
        try:
            menu = tk.Menu(self.win, tearoff=0)
            for _label, _it in zip(menu_labels(), IMPORT_MENU_ITEMS):
                _k = _it['key']
                menu.add_command(label=_label,
                                 command=lambda k=_k: self._dispatch_import(k))
            try:
                menu.tk_popup(*(self.win.winfo_pointerxy()))
            finally:
                try:
                    menu.grab_release()
                except Exception:
                    pass
        except tk.TclError as _e:
            try:
                self.status_text.set(f"⚠ 导入菜单弹出失败：{str(_e)[:60]}")
                _tkmsg.showwarning("导入菜单", f"菜单弹出失败（{str(_e)[:80]}），已回退表格导入。",
                                   parent=self.win)
                self._import_table()
            except Exception:
                pass
        finally:
            # F2：menu 用后即毁（tk_popup 已返回；destroy 失败静默）
            if menu is not None:
                try:
                    menu.destroy()
                except Exception:
                    pass

    def _dispatch_import(self, key):
        """导入菜单项分派：pick_images → 批量图片路径；import_table → 表格导入。

        v1.5.8（BUG_HUNT_V157 ★条）：未知 key/路径异常**不再静默跳转表格导入**——
        未知 key 记日志并显式提示（契约漂移早发现）；pick_images 业务异常原样冒泡
        由调用方（async_queue/异常守卫）显式呈现（DESIGN §4）。
        """
        if key == 'pick_images':
            self._batch_images()
            return
        if key == 'import_table':
            self._import_table()
            return
        # 未知 key：契约漂移信号——显式留痕不猜（§4）
        try:
            from utils import _sanitize_for_log
            log.warn(f"导入菜单未知 key：{_sanitize_for_log(str(key))[:40]}，已忽略")
        except Exception:
            pass

    def _live_screenshot(self):
        # v1.5.9：识别前置 API 预检（未配置 → 明确引导弹窗 + 可跳转设置页）
        if not self._ensure_api_ready():
            return
        # v1.4.7 WS-C：单次识别入口重置「本次」消耗口径
        try:
            import usage_store as _us
            _us.session_reset()
        except Exception:
            pass

        # P2-C：免费版每日 50 次实时截图识别门控（enforce=false 时跳过）
        # 用户裁定：表格导入/手动输入永不限制；批量识别/双模型/Excel 导出也不在门控范围
        try:
            from utils import Config
            _cfg = Config.load() if hasattr(Config, "load") else {}
            _lic_cfg = _cfg.get("license", {}) if isinstance(_cfg, dict) else {}
            _lic_key = _lic_cfg.get("key", "") or ""
            _lic_enforce = bool(_lic_cfg.get("enforce", False))
            if _lic_enforce:
                from auth.license import check_live_quota
                import usage_store as _us2
                _used = _us2.count_today_live_screenshot()
                _gate = check_live_quota(_used, _lic_key, enforce=True)
                if not _gate.get("allowed", True):
                    try:
                        from tkinter import messagebox
                        messagebox.showwarning(
                            "实时截图识别次数已用完",
                            _gate.get("reason", "已达上限") + "\n\n"
                            "表格导入与手动输入不受此限制。\n"
                            "升级 Pro 或在「设置 → 授权管理」切换 enforce=false。",
                        )
                    except Exception:
                        pass
                    return
        except Exception:
            pass  # 失败安全：永不阻塞

        self.status_text.set("最小化窗口，请确认PDD页面在后面...")
        self._clear_input_rows()  # 先清旧数据
        self.win.update()
        
        # 主线程 2 秒后无条件恢复窗口（符合设计：最小化最多 2 秒）。
        # 截图线程只负责截图+OCR，不参与窗口恢复——不依赖子线程 after，
        # 也不管截图是否卡住，窗口 2 秒必弹回来。
        # 注意：截图函数 win.activate() 会把浏览器拉到前台，deiconify 后 PDD EZ
        # 可能被浏览器盖住（看着像没恢复）→ 恢复时短暂置顶确保可见。
        def _auto_restore():
            try:
                if self.win.state() == 'iconic':
                    self.win.deiconify()
                self.win.lift()
                self.win.attributes('-topmost', True)
                self.win.after(500, lambda: self.win.attributes('-topmost', False))
            except Exception:
                pass
        try:
            self.win.after(2000, _auto_restore)
        except Exception:
            pass
        
        # 子线程启动前缓存 Tk 变量（_single_dual_var.get() 是 Tcl 调用，子线程读未定义行为）
        _dual_mode_cache = self._single_dual_var.get()
        
        def task(_progress=None):
            # v1.5.9.3：识别全流程步骤日志（远程诊断用：截图/OCR/结果每一步可见）
            # v1.5.9.4-hotfix：修复致命签名 bug——TaskQueue 契约 fn(progress)，
            # 旧版 def task() 无参导致 worker 一执行即 TypeError（识别从未开始）
            try:
                log.info('识图：任务开始（双模型=%s）', _dual_mode_cache)
            except Exception:
                pass
            # 异常由 TaskQueue 捕获并通过 on_error 回调
            self.win.after(0, self.win.iconify)
            time.sleep(0.5)
            
            ss_path = os.path.join(get_base_dir(), 'output', '_live_screenshot.png')
            os.makedirs(os.path.dirname(ss_path), exist_ok=True)
            
            # 与批量识别完全一致的截图逻辑
            from utils import capture_pdd_screenshot
            found_window = capture_pdd_screenshot(ss_path)
            try:
                log.info('识图：截图完成 found_window=%s path=%s', found_window, ss_path)
            except Exception:
                pass
            
            # 截图完成立即恢复窗口（OCR 可能耗时数秒，不必等）
            # 注意：win.state() 是 Tk 调用，必须放主线程（after）——子线程直接读 Tcl 未定义行为
            def _restore_if_iconic():
                try:
                    if self.win.state() == 'iconic':
                        self.win.deiconify()
                        self.win.lift()
                        self.win.attributes('-topmost', True)
                        self.win.after(500, lambda: self.win.attributes('-topmost', False))
                except Exception:
                    pass
            self.win.after(0, _restore_if_iconic)
            
            if not found_window:
                # v1.5.9.2：未找到 PDD 窗口 → 日志留痕 + 明确引导（带「打开商家后台」跳转）
                try:
                    log.warn('识图：未找到拼多多/浏览器窗口，已提示用户打开商家后台')
                except Exception:
                    pass

                def _no_window_hint():
                    try:
                        import webbrowser
                        from tkinter import messagebox as _mb
                        _backend_url = 'https://mms.pinduoduo.com/'
                        try:
                            from utils import Config as _Cfg2
                            _b = (_Cfg2.load().get('backend') or {})
                            if isinstance(_b, dict) and str(_b.get('url') or '').startswith('http'):
                                _backend_url = str(_b['url'])
                        except Exception:
                            pass
                        self.status_text.set('❌ 未找到浏览器窗口，请先打开 PDD 后台页面')
                        _go = _mb.askyesno(
                            '没有找到拼多多窗口',
                            '没有在屏幕上找到拼多多商家后台窗口。\n\n'
                            f'「识图」是截取当前屏幕上的后台页面，请先：\n'
                            f'  1. 打开浏览器登录 {_backend_url}\n'
                            f'  2. 进入「订货管理」页面（截图会截这个窗口）\n'
                            f'  3. 再点一次「识图」\n\n'
                            f'是否现在帮你打开商家后台？',
                            parent=self.win)
                        if _go:
                            webbrowser.open(_backend_url)
                    except Exception:
                        pass
                self.win.after(0, _no_window_hint)
                return
            
            self.win.after(0, lambda: self.status_text.set('OCR识别中...'))
            
            items = self._ocr_generic_to_items(ss_path, dual_verify=_dual_mode_cache)
            try:
                log.info('识图：OCR 完成 items=%d', len(items or []))
            except Exception:
                pass

            if not items:
                # v1.5.11：空结果提示归一（不再裸「未识别到商品」）
                self.win.after(0, lambda: self.status_text.set(
                    '未识别到表格数据——请确认截图包含完整的订货管理表格后重试'))
                return

            # 缓存最近一次截图源文件，供 _fill_from_ocr 模糊检测使用
            def _fill():
                try:
                    self._last_ocr_image_path = ss_path
                except Exception:
                    pass
                self._fill_from_ocr(items)
            self.win.after(0, _fill)
            # 窗口恢复由主线程 _auto_restore 负责（2 秒后无条件恢复），子线程不再干预
        
        # 使用 TaskQueue 执行任务，异常通过 on_error 回调
        # on_error 改用 _friendly_error 把异常归类到 USER_MSG_*（OCR 异常常见
        # 失败哲学：API key 空 / 超时 / 额度耗尽 / JSON 截断 → 弹窗给用户可读中文）。
        self._task_queue.submit(
            "实时截图OCR",
            task,
            on_error=lambda e: self.win.after(0, lambda exc=e: self._friendly_error(exc, popup=True)),
        )
    
    # v1.5.8（BUG_HUNT_V157 A3）：_ocr_fill 已删除——v1.5.7 定版后为死代码
    # （单张图片识别由「导入」菜单→选择图片文件 覆盖；识图按钮=_live_screenshot）。
    
    def _batch_images(self):
        """R1 流程效率：批量图片识别入口（「批量图片」按钮）。

        多选图片 → 逐张 submit TaskQueue（每张一个任务、串行执行，on_progress
        状态栏「第 i/N 张」）→ 单张结果缓冲，全部完成后一次性 _fill_from_ocr
        (source='file') 收口：清洗/地区分组/低置信复核/计算/历史全部复用单图
        同款流程（§3 多地区按 item.region 独立分组，不受多张合并影响）。
        单张失败不中断（t2 引擎 batch_ocr_images 把异常收进 errors，§4 收尾
        汇总失败张数显式提示）。F9 复用 _batch_stop 通道；停止后不回填表格
        （避免半批数据误导），状态栏报告已完成进度。
        依赖契约（t2）：ocr.batch_ocr_images(image_paths, mapping=None,
        recognizer=None, **kwargs) -> (results[{path,items,mapping}], errors[(path,reason)])。
        """
        # v1.5.9：识别前置 API 预检（未配置 → 明确引导弹窗 + 可跳转设置页）
        if not self._ensure_api_ready():
            return
        # 重入/互斥守卫：与「批量识别」互斥（共用 _batch_stop 取消通道与 API 队列，
        # 双批并发会互相覆盖取消钩子——同 bug hunt F24 纪律）
        if getattr(self, '_batch_running', False) or getattr(self, '_img_batch_running', False):
            messagebox.showinfo("批量图片", "批量任务正在进行中，请先等待完成或停止后再试")
            return
        import ocr as _ocr_mod
        from tkinter import filedialog
        # 引擎显式存在性校验（§4：缺失显式报错，不静默降级成别的行为）
        if not hasattr(_ocr_mod, 'batch_ocr_images'):
            messagebox.showerror(
                "批量图片", "批量识别引擎缺失（ocr.batch_ocr_images），请安装完整版本后重试")
            return
        paths = filedialog.askopenfilenames(
            title="选择要批量识别的PDD后台截图（可多选）",
            filetypes=[("图片文件", "*.jpg *.jpeg *.png"), ("所有", "*.*")])
        paths = [str(p) for p in (paths or []) if p]
        if not paths:
            return
        n = len(paths)
        # 单次识别入口重置「本次」消耗口径（与 _ocr_fill 同款）
        try:
            import usage_store as _us
            _us.session_reset()
        except Exception:
            pass
        # 子线程启动前缓存 Tk 变量（子线程读 Tcl 变量未定义行为）
        _dual_mode_cache = self._single_dual_var.get()
        # F9 协作取消钩子（与批量识别同通道；收尾解除）
        self._batch_stop.clear()
        try:
            from ocr import set_cancel_check as _ocr_cc
            from vision import set_cancel_check as _vis_cc
            _ocr_cc(self._batch_stop.is_set)
            _vis_cc(self._batch_stop.is_set)
        except Exception:
            pass
        # 批次状态（主线程独占读写：on_done/on_error 均 after 回主线程后才动它）
        self._img_batch = {'total': n, 'done': 0, 'failed': 0, 'items': [], 'errors': []}
        self._img_batch_running = True
        self._img_batch_task_ids = []
        self._set_batch_btn_state(True)  # 批量期间禁用 导出/截图/批量图片（同批量识别）
        self._begin_batch_progress()
        self.status_text.set(batch_images_progress_text(1, n))
        log.hr(f"批量图片识别开始：{n} 张", 1)

        def _recognizer(p, _m, **_kw):
            # 与单图「识图」同款管线（columns=None 全列识别，宪法 §1）；识别器
            # 内部异常 → 引擎捕获转 errors，不中断整批
            return self._ocr_generic_to_items(p, dual_verify=_dual_mode_cache)

        def _img_on_progress(pct, stage):
            # async_queue 线程契约：回调在 worker 线程触发，Tk 操作必须 after 回主线程
            def _apply():
                try:
                    bar = getattr(self, 'batch_progress', None)
                    if bar is not None and bar.winfo_exists():
                        bar.configure(value=max(0, min(100, int(pct))))
                except Exception:
                    pass
                try:
                    self.status_text.set(str(stage))
                except Exception:
                    pass
            self.win.after(0, _apply)

        def _make_task(idx, path):
            def task(progress):
                # 每张一个任务：进度上报「第 i/N 张」（batch_images_progress_text 纯函数）
                progress(int(idx / n * 100), batch_images_progress_text(idx + 1, n))
                results, errors = _ocr_mod.batch_ocr_images([path], recognizer=_recognizer)
                return (idx, path, results, errors)
            return task

        def _img_on_done(result):
            self.win.after(0, lambda r=result: self._img_batch_one_done(r))

        def _img_on_error(exc):
            # 任务体意外崩溃（引擎兜不住的）：计失败，不中断整批
            import traceback
            try:
                log.error("批量图片单张任务异常:\n" + traceback.format_exc())
            except Exception:
                pass
            self.win.after(0, lambda e=exc: self._img_batch_one_error(e))

        for idx, p in enumerate(paths):
            try:
                tid = self._task_queue.submit(
                    f"批量图片{idx + 1}/{n}",
                    _make_task(idx, p),
                    on_done=_img_on_done,
                    on_progress=_img_on_progress,
                    on_error=_img_on_error,
                    cancel_event=self._batch_stop,
                )
                self._img_batch_task_ids.append(tid)
            except Exception as e:
                # 队列拒绝（已 shutdown 等）：按该张失败计，不中断其余提交（§4）
                self._img_batch['done'] += 1
                self._img_batch['failed'] += 1
                self._img_batch['errors'].append((os.path.basename(p), str(e)[:120]))
                try:
                    log.warn(f"批量图片任务提交失败: {str(e)[:120]}")
                except Exception:
                    pass
        if self._img_batch['done'] >= n:
            # 全部提交即失败（队列不可用）：直接收尾，显式报错
            self._img_batch_finish()

    def _img_batch_one_done(self, result):
        """批量图片单张收口（主线程）：缓冲 items / 累计失败；全部完成后统一填充。"""
        try:
            if not self.win.winfo_exists():
                return
        except Exception:
            return
        state = getattr(self, '_img_batch', None)
        if not state or not getattr(self, '_img_batch_running', False):
            return
        _idx, path, results, errors = result
        state['done'] += 1
        if errors:
            # 单张失败（含 F9 取消被引擎转为 errors 的情况）：不中断，收尾汇总（§4）
            state['failed'] += 1
            for _p, _reason in (errors or []):
                state['errors'].append(
                    (os.path.basename(str(_p) or str(path)), str(_reason)[:160]))
        elif results:
            state['items'].extend((results[0] or {}).get('items') or [])
            # 模糊检测源（_fill_from_ocr 对 file 路径读 _last_ocr_image_path）
            # 多张时取最后一张成功图，与单图语义一致的近似
            try:
                self._last_ocr_image_path = (results[0] or {}).get('path') or path
            except Exception:
                pass
        # else: 识别成功但 0 项（引擎空结果）——不计失败，收尾统计中体现
        try:
            bar = getattr(self, 'batch_progress', None)
            if bar is not None and bar.winfo_exists():
                bar.configure(value=int(state['done'] / max(1, state['total']) * 100))
        except Exception:
            pass
        if state['done'] >= state['total']:
            self._img_batch_finish()
        else:
            self.status_text.set(
                batch_images_progress_text(state['done'] + 1, state['total'])
                + f"（已完成 {state['done']}/{state['total']}，失败 {state['failed']}）")

    def _img_batch_one_error(self, exc):
        """批量图片单张意外异常收口（主线程）：计失败不中断整批（§4 显式留痕）。"""
        try:
            if not self.win.winfo_exists():
                return
        except Exception:
            return
        state = getattr(self, '_img_batch', None)
        if not state or not getattr(self, '_img_batch_running', False):
            return
        state['done'] += 1
        state['failed'] += 1
        state['errors'].append(('（任务异常）', str(exc)[:160]))
        if state['done'] >= state['total']:
            self._img_batch_finish()

    def _img_batch_poll_cancel(self, ticks=0):
        """F9 后批量图片恢复监视（主线程 after 轮询）：等全部图片任务终态再收尾。

        cancelled 任务无 on_done/on_error（async_queue 协作取消检查点直接 return），
        与 _poll_cancel_restore 同思路；60 轮（≈24s）未见终态也强制收尾防卡死。
        """
        try:
            all_terminal = True
            for tid in list(getattr(self, '_img_batch_task_ids', None) or []):
                try:
                    st = self._task_queue.task_status(tid)
                except Exception:
                    st = 'cancelled'
                stv = str(getattr(st, 'value', st))
                if stv in ('pending', 'running'):
                    all_terminal = False
                    break
            if not all_terminal and ticks < 60:
                self.win.after(400, lambda t=ticks + 1: self._img_batch_poll_cancel(t))
                return
        except Exception:
            pass
        self._img_batch_finish(cancelled=True)

    def _img_batch_finish(self, cancelled=False):
        """批量图片收尾（主线程）：解除取消钩子 + 恢复按钮/进度条 + 结果汇总。

        cancelled=True（F9/关窗）：已完成的 items 不回填表格（避免半批数据误导），
        状态栏显式报告完成进度；正常完成：一次性 _fill_from_ocr 合并填充 +
        失败张数显式弹窗（§4）。
        """
        if not getattr(self, '_img_batch_running', False):
            return  # 已收尾（正常完成/F9 恢复监视竞态）——幂等保护
        self._img_batch_running = False
        state = getattr(self, '_img_batch', None) or {}
        try:
            from ocr import set_cancel_check as _ocr_cc
            from vision import set_cancel_check as _vis_cc
            _ocr_cc(None)
            _vis_cc(None)
        except Exception:
            pass
        self._reset_batch_progress()
        self._set_batch_btn_state(False)
        try:
            self._refresh_cost_label()  # 与 _finish_batch 同款：收尾刷费用 Label
        except Exception:
            pass
        total = state.get('total', 0)
        done = state.get('done', 0)
        failed = state.get('failed', 0)
        items = state.get('items') or []
        errors = state.get('errors') or []
        try:
            if cancelled:
                self.status_text.set(
                    f"⏹ 批量图片识别已停止 — 已完成 {done}/{total} 张（失败 {failed}），未回填表格")
                log.hr(f"批量图片识别停止：完成 {done}/{total}", 1)
                return
            log.hr(f"批量图片识别完成：成功 {total - failed}/{total} 张，商品 {len(items)} 个", 1)
            if items:
                # 复用单图同款收口：清洗/地区分组/复核/计算/历史全在 _fill_from_ocr
                self._fill_from_ocr(items, source='file')
                if failed:
                    self.status_text.set(
                        f"⚠ {failed}/{total} 张识别失败（详见弹窗）｜{self.status_text.get()}")
            else:
                self.status_text.set(
                    f"批量图片识别完成：0 个商品（成功 {total - failed}/{total} 张）")
            if failed:
                detail = '\n'.join(f"· {p}：{r}" for p, r in errors[:3])
                more = f"\n… 等共 {failed} 张失败" if failed > 3 else ''
                messagebox.showwarning(
                    "批量图片识别", f"{failed}/{total} 张识别失败：\n{detail}{more}")
            elif not items:
                messagebox.showinfo(
                    "批量图片识别", "图片中未识别到表格数据，请确认截图包含完整表格")
        except Exception as e:
            try:
                log.error("批量图片收尾异常: " + str(e)[:200])
            except Exception:
                pass
            self._show_error(f"批量图片收尾失败: {str(e)[:80]}", popup=True)

    def _ocr_generic_to_items(self, image_path, table_bbox=None, dual_verify=False,
                              row_bboxes=None):
        """
        通用列识别 → 业务字段 items（v1.3 主入口）。
        **设计初衷（阿洋定）：模型识别整张表所有列 → 程序端按 mapping/勾选列筛选显示。**
        因此 OCR 始终全列识别（columns=None），不把勾选列丢给模型；
        parse_items_generic 按 mapping 取业务字段，_raw 保留全列原文供显示/导出时筛选。
        dual_verify=True 时走双模型交叉验证（ocr_dual_verify_generic）。
        row_bboxes（v1.4）：表格行级边界 [(top,bottom),...]，优先走行级切分识别
        （防整表乱编），失败自动回退整表。
        """
        from ocr import ocr_table, parse_items_generic
        from utils import get_ocr_columns
        cfg = get_ocr_columns()
        _mapping = cfg.get('mapping') or {}
        if dual_verify:
            from ocr import ocr_dual_verify_generic
            from utils import get_secondary_model, get_api_config
            _sec = get_secondary_model()
            _api = get_api_config()
            _act = _api.get('active_provider', '')
            _main = ((_api.get('providers') or {}).get(_act, {}) or {}).get('model', '')
            if _main and str(_main).strip().lower() == str(_sec).strip().lower():
                from ocr import _ocr_dlog, USER_MSG_DUAL_MAIN_EQ_SEC
                # v1.5.11：主副相同 → 不再空耗一次双模型 API，直接单模型识别 +
                # 可行动的引导文案（用户曾把"相同提示"误读为识别失败）
                _ocr_dlog(f"⚠ 主副模型相同（{_main}），双模型无意义——本次按单模型识别（省一次 API）")

                def _hint_main_eq_sec(m=_main):
                    try:
                        self.status_text.set(
                            f"⚠ 主副模型相同（{m}）已按单模型识别——双模型请配置不同副模型（设置→API 管理）")
                    except Exception:
                        pass
                self.win.after(0, _hint_main_eq_sec)
                # 相同则双验证无意义：降级走单模型表格识别路径
                if row_bboxes:
                    from ocr import ocr_table_row_split
                    try:
                        result = ocr_table_row_split(image_path, columns=None,
                                                     table_bbox=table_bbox,
                                                     row_bboxes=row_bboxes)
                    except Exception:
                        result = ocr_table(image_path, columns=None, table_bbox=table_bbox)
                else:
                    result = ocr_table(image_path, columns=None, table_bbox=table_bbox)
                rows = result.get('rows') or []
                items = parse_items_generic(rows, cfg.get('mapping') or {})
                for _it in items:
                    _it['_dual_skipped_ocr'] = True  # 复用"设计跳过"标记，UI 显示说明文案
                return items
            return ocr_dual_verify_generic(image_path, columns=None,
                                           mapping=_mapping,
                                           table_bbox=table_bbox,
                                           secondary_model=_sec,
                                           row_bboxes=row_bboxes)
        if row_bboxes:
            from ocr import ocr_table_row_split
            try:
                result = ocr_table_row_split(image_path, columns=None,
                                             table_bbox=table_bbox,
                                             row_bboxes=row_bboxes)
            except Exception:
                # 行切分失败（rows 无效/API 异常）回退整表，保证识别不中断
                result = ocr_table(image_path, columns=None, table_bbox=table_bbox)
        else:
            result = ocr_table(image_path, columns=None, table_bbox=table_bbox)
        rows = result.get('rows') or []
        items = parse_items_generic(rows, cfg.get('mapping') or {})
        # v1.4.2 手机流程【7】容错机制：主识别出现无 ID / 低置信列 / 数字可疑
        # （年份/日期串串位，如行切分把"商品创建时间 2026-08-04"抄进 stock）的行 →
        # 自动二次推理择优（强化 prompt 专注 ID 与数字完整性，按 name 匹配补全）。
        # 只在质量信号触发时调用，常规路径零额外成本；失败保留首轮结果。
        try:
            from ocr import _suspect_number
            if any(it.get('_missing_id') or it.get('_low_conf_col')
                   or _suspect_number(it.get('stock')) or _suspect_number(it.get('sales'))
                   for it in items):
                from ocr import ocr_table_verify, merge_verify_items
                _vrows = ocr_table_verify(image_path, table_bbox=table_bbox)['rows'] or []
                _vitems = parse_items_generic(_vrows, cfg.get('mapping') or {})
                items = merge_verify_items(items, _vitems)
                _fixed = sum(1 for it in items if it.get('_verify_fixed'))
                if _fixed:
                    from ocr import _ocr_dlog
                    _ocr_dlog(f"OK 二次识别补全 {_fixed} 行（ID/数字）")
        except Exception as _e:
            from ocr import _ocr_dlog
            _ocr_dlog(f"WARN 二次识别择优失败（保留首轮结果）: {str(_e)[:100]}")
        return items

    def _fill_from_ocr(self, items, source='live'):
        """用OCR结果填充表格

        v1.4.7 T-B3：source 标记数据来源（'live' 实时截图 | 'batch' 批量 |
        'file' 图片文件 | 'import' 表格导入），供历史采集区分来源；带默认值，
        既有调用零影响。
        """
        self._clear_error()  # 先重置状态，再设置识别进度提示（避免被覆盖）
        self.status_text.set(f"OCR识别到 {len(items)} 项，计算中...")
        self.win.update()
        
        if not items:
            self.status_text.set("未识别到表格数据——请确认截图包含完整的订货管理表格后重试")
            return
        # 按地区分组：多省份×多仓库批量时每个地区独立缓存（避免全混进第一个地区）
        from ocr import strip_region_suffix as _srs
        by_region = {}
        detected_regions = set()
        for it in items:
            reg = _srs(it.get('region', '')) or self.region_var.get()
            by_region.setdefault(reg, []).append(it)
            if reg:
                detected_regions.add(reg)
        # v1.4 bugfix：表格只显示第一个地区的商品——之前把所有省份商品顺序
        # 填进同一张表，第二个省份看起来"叠加"了前面省份的商品信息。
        # 批量多省份时其余地区通过顶部地区 tab 切换查看（数据已按地区独立入缓存）
        _first_reg = next(iter(by_region)) if by_region else ''
        _display = by_region.get(_first_reg) or items
        # 清空所有现有行（临时禁用自动加行，避免 set('') 触发追加空行）
        self._suppress_auto_append = True
        try:
            for row in self.rows:
                row['name'].set('')
                row['stock'].set('')
                row['sales'].set('')
        finally:
            self._suppress_auto_append = False
        # 确保有足够行
        while len(self.rows) < len(_display):
            self._add_row()
        # 填入数据（detected_regions 已在分组时收集全部地区）
        low_conf_count = 0
        name_unmatched_count = 0
        dual_degraded = False
        dual_skipped_ocr = False  # v1.5.11：副模型 OCR 专用/主副相同 → 设计跳过（非失败）
        verify_fixed_count = 0  # v1.4.2：二次识别补全行数（_verify_fixed 标记）
        for i, item in enumerate(_display):
            r = self.rows[i]
            # 双模型验证标记的低置信度商品：名称加 ⚠ 提示复核
            low_conf = item.get('_low_confidence', False)
            if item.get('_name_unmatched'):
                name_unmatched_count += 1
            if item.get('_dual_degraded'):
                dual_degraded = True
            if item.get('_dual_skipped_ocr'):
                dual_skipped_ocr = True
            name_disp = item.get('name', '')
            if low_conf:
                low_conf_count += 1
                name_disp = f"⚠{name_disp}"
            # 分仓库识别：显示时附加 [仓库名]，计算仍用原始 name（避免时效匹配失败）
            wh = item.get('warehouse', '')
            if wh:
                name_disp = f"{name_disp} [{wh}]"
            r['name'].set(name_disp)
            r['stock'].set(str(item.get('stock', '')))
            r['sales'].set(str(item.get('sales', '')))
            # 保留 OCR 原始列（仓库信息/仓库销售库存等勾选列），
            # 否则刷新计算时 _recalc_from_rows 只能回填 name/stock/sales，其他列全空白
            r['_raw'] = item.get('_raw') or {}
            if item.get('_verify_fixed'):
                verify_fixed_count += 1
        if low_conf_count:
            self.status_text.set(f"⚠ {low_conf_count} 个商品双模型结果不一致，已取保守值，请重点核对")
        elif name_unmatched_count:
            self.status_text.set(f"⚠ {name_unmatched_count} 个商品双模型识别名称不一致，已标记请复核")
        elif dual_skipped_ocr:
            # v1.5.11：设计跳过的场景（副模型 OCR 专用 / 主副相同）——明确"不是失败，是配置语义"
            try:
                from ocr import USER_MSG_DUAL_SEC_OCR, USER_MSG_DUAL_MAIN_EQ_SEC
                self.status_text.set("⚠ " + USER_MSG_DUAL_MAIN_EQ_SEC)
            except Exception:
                self.status_text.set("⚠ 双模型未启用交叉验证（副模型配置为 OCR 专用或与主模型相同）")
            # 附诊断日志，避免用户误读为失败
            try:
                log.info('双模型：本次为设计跳过（_dual_skipped_ocr），非识别失败')
            except Exception:
                pass
        elif dual_degraded:
            self.status_text.set(
                "⚠ 副模型本次调用失败，已按单模型识别（主结果不受影响）——若模型名无效请到「设置→API 管理」更换副模型")
        elif verify_fixed_count:
            # v1.4.2：二次识别补全提示（ID/数字经择优补全）
            self.status_text.set(f"✓ {verify_fixed_count} 个商品经二次识别补全（ID/数字），数据已完善")
        # 自动匹配地区
        # v1.4：表格只显示第一个地区，多地区时提示其余走地区 tab 查看
        if len(by_region) > 1:
            msg = (f"识别完成 — 当前地区 {len(_display)} 个商品"
                   f"（共{len(by_region)}个地区，其余见顶部地区切换）")
        else:
            msg = f"识别完成 — {len(_display)} 个商品，请核对后点计算"
        if detected_regions:
            newly_added = []
            for reg in detected_regions:
                if reg and reg not in self.regions:
                    # 新地区：自动加入，商品运输时效留空（默认3天）
                    self.regions[reg] = {}
                    newly_added.append(reg)
            if newly_added:
                self._save_regions()
            # 选中第一个匹配的地区
            for reg in detected_regions:
                if reg in self.regions:
                    self.region_var.set(reg)
                    break
            # 提示新地区
            msg = f"识别完成 — {len(_display)} 个商品"
            if newly_added:
                msg += f"\n\n⚠ 新增地区：{'、'.join(newly_added)}，各商品运输时间默认3天"
                msg += "\n请点击「商品时效设置」按商品调整运输天数"
                self.win.after(500, lambda: messagebox.showinfo(
                    "发现新地区",
                    f"识别到新地区：{'、'.join(newly_added)}\n\n已自动添加到地区列表，各商品运输时间暂设为3天。\n请点击「商品时效设置」根据实际情况调整。",
                    parent=self.win))
        self.status_text.set(msg)
        # 计算前 OCR 复核—— 存在 low 置信行 → 弹窗
        # 让用户确认/修正。低置信 = 任一 confidence.level == 'low'（含模糊/双模型
        # 差异>30%/数字异常/名字配对异常等维度）。所有 items 在弹窗前先注入
        # confidence 元数据（blur 信息用最近一次截图源文件做 Laplacian 检测）。
        try:
            from ocr import build_confidence_meta as _bcm
            # 用本次 items 源文件做模糊检测（live/file 路径都有 image_path）
            # R2 问题：批量路径改用识别现场采集的 _batch_blur_seen——批量的
            # _last_ocr_image_path 是上一会话残留截图（来源错，曾导致整批漏判/误判）
            _blur_info = None
            try:
                if source == 'batch':
                    if getattr(self, '_batch_blur_seen', False):
                        _blur_info = (True, 0.0)
                else:
                    _src = getattr(self, '_last_ocr_image_path', None)
                    if _src and isinstance(_src, str) and os.path.isfile(_src):
                        from ocr import detect_blur as _db
                        _blur_info = _db(_src)
            except Exception:
                _blur_info = None
            _bcm(items, blur_info=_blur_info)
        except Exception:
            pass
        # 复核：_fill_from_ocr 主入口触发。
        # R2 问题：import 路径纳入复核——表格导入的编码错/列错位/
        # 数字解析错同样值得行级复核；_bcm 的数值审计对 import items 同样有效，
        # 弹窗仍仅在 has_low_confidence 命中时出现，无 OCR 元数据的干净导入不受影响。
        if source in ('live', 'file', 'batch', 'import'):
            try:
                from ocr_review import has_low_confidence as _hlc
                if _hlc(items):
                    _action, _edits = self._show_review_dialog(items)
                    if _action == 'cancel':
                        # 取消：清掉刚写入的表格行 + 状态栏提示，不入历史
                        self.status_text.set(
                            f"⚠ 用户取消复核（{len(items)} 项未计算，未入历史）")
                        try:
                            self._suppress_auto_append = True
                            for _r in self.rows:
                                _r['name'].set('')
                                _r['stock'].set('')
                                _r['sales'].set('')
                        finally:
                            self._suppress_auto_append = False
                        return
                    elif _action == 'edited' and _edits:
                        # 修正后：写回 items + 同步 self.rows 单元格显示
                        from ocr_review import apply_user_edits as _aue
                        _aue(items, _edits)
                        for _ed in _edits:
                            _idx = _ed.get('index')
                            _fld = _ed.get('field')
                            _val = _ed.get('value')
                            if not isinstance(_idx, int):
                                continue
                            if _idx < 0 or _idx >= len(self.rows):
                                continue
                            if _fld in ('stock', 'sales'):
                                try:
                                    self.rows[_idx][_fld].set(str(int(_val)))
                                except (ValueError, TypeError):
                                    self.rows[_idx][_fld].set(str(_val))
                            elif _fld == 'name':
                                self.rows[_idx]['name'].set(str(_val))
                        # 修正后 items 状态已变 → 重建 by_region
                        by_region = {}
                        detected_regions = set()
                        for it in items:
                            reg = _srs(it.get('region', '')) or self.region_var.get()
                            by_region.setdefault(reg, []).append(it)
                            if reg:
                                detected_regions.add(reg)
                        _first_reg = next(iter(by_region)) if by_region else ''
                        self.status_text.set(
                            f"✓ 修正 {len(_edits)} 项后继续计算")
            except Exception:
                # 复核弹窗失败 → 静默放行（不阻塞主流程）
                pass
        # 直接用OCR结果计算，不依赖行数据
        # 按地区分组（上面已构建 by_region）：多省份批量时每个地区独立缓存
        try:
            for reg, sub in by_region.items():
                self.region_var.set(reg)
                self._calc_from_items(sub)
            # 表格显示与结果表统一切回第一个地区（其余地区走地区 tab 查看）
            if _first_reg:
                self.region_var.set(_first_reg)
                self.active_region = _first_reg
                _fd = self.cache.get(_first_reg)
                if _fd:
                    self._render_tree(_fd['plans'])
                    self.plans = _fd['plans']
                self._update_tabs()
        except Exception as e:
            self._show_error(f"计算出错: {e}", popup=True)
            import traceback; traceback.print_exc()
        # v1.4.7 WS-A（A2 唯一采集挂点）：逐地区计算完成后，把各地区 plans 组装
        # 落入本地历史库（{region: cache[region]['plans']}）。
        # 铁律（R8）：任何异常仅日志，绝不中断识别主流程；
        # 不挂 _calc_from_items——手动编辑触发的重算不产生噪音快照。
        try:
            if history_db is not None:
                _plans_by_region = {}
                for _reg in by_region:
                    _cd = self.cache.get(_reg)
                    if isinstance(_cd, dict) and _cd.get('plans'):
                        _plans_by_region[_reg] = _cd['plans']
                if _plans_by_region:
                    # 按当前店铺组装双层入参 {store_id: {region: [plans]}}
                    # （store_ui_logic.group_plans_by_store，单测覆盖；入参形状归
                    # store_registry/history_db 的 契约）。
                    _store = getattr(self, '_store_id', None) or 'default'
                    history_db.record_capture(
                        store_ui_logic.group_plans_by_store(_plans_by_region, _store),
                        source=source)
        except Exception as _hist_e:
            try:
                from ocr import _ocr_dlog
                _ocr_dlog(f"[history] 采集挂点异常（不影响识别）: {_hist_e}")
            except Exception:
                pass
        # v1.4.7 WS-C：回填收尾点主动刷新费用 Label（此处为主线程上下文，直接调）
        self._refresh_cost_label()

    # ─────────────────── v1.4.7 WS-C：费用显示（T-C5 GUI 侧）───────────────────

    def _refresh_cost_label(self):
        """刷新工具条费用 Label：`本次 ¥X.XX｜本月 ¥Y.YY`。

        数据源 usage_store（估算行不计费、缺价按 0——口径见面板"估算仅供参考"）。
        任何异常吞掉；主线程外调用只允许经 win.after 调度。

        v1.4.7 P3-R1-L3：本月 cost 走 usage_store.get_month_cost() 内存增量（O(1)），
        替代 aggregate('month') 全量读 jsonl（10 万行级别从 ~50ms 降到常数时间）。
        """
        try:
            lbl = getattr(self, 'cost_label', None)
            if lbl is None:
                return
            try:
                if not lbl.winfo_exists():
                    return
            except Exception:
                return
            import usage_store as _us
            _sess = _us.session_total()
            _mon = _us.get_month_cost()  # 内存缓存：跨月时一次性全量重算
            lbl.config(text=f"本次 ¥{_sess:.2f}｜本月 ¥{_mon:.2f}")
            # P3-R1-L2：写盘连续失败达阈值 → 状态栏一次性提示（不打扰、不重复）
            try:
                _fs = _us.get_write_failure_state()
                if _fs and _fs.get('should_alert'):
                    _err = _fs.get('last_error') or '未知'
                    self.status_text.set(
                        f"⚠ 用量记录已连续 {_fs['consecutive']} 次写入失败（{_err}），"
                        f"请检查磁盘空间或权限。详见日志。"
                    )
                    _us.ack_write_failure_alert()
            except Exception:
                pass
        except Exception:
            pass

    def _poll_cost_label(self):
        """每 60s 轮询刷新费用 Label（主线程事件调度，worker 线程绝不直调 Tk）。"""
        self._refresh_cost_label()
        try:
            self.win.after(60000, self._poll_cost_label)
        except Exception:
            pass  # 窗口已销毁（程序退出）时停止轮询

    # ─────────────────── v1.4.7 WS-A：历史趋势（T-A3 GUI 侧）───────────────────

    def _history_privacy_hint(self):
        """首次启用一次性提示（A6/§2.1.5）：识别数据仅本机持久化，不上传。"""
        try:
            if history_db is None:
                return
            from utils import Config
            s = Config.load()
            h = s.get('history')
            h = h if isinstance(h, dict) else {}
            if h.get('privacy_hint_shown'):
                return
            h['privacy_hint_shown'] = True
            s['history'] = h
            Config.save(s)
            messagebox.showinfo(
                "识别历史功能已启用",
                "为支持「历史趋势」展示，识别结果将持久化保存到本机数据目录（history.db）。\n\n"
                "· 数据仅保存在本机，不会上传；\n"
                "· 可随时在导航「📈 历史趋势」页点「清空全部历史」删除；\n"
                "· 历史库故障不影响识别主流程。",
                parent=self.win)
        except Exception:
            pass  # 提示失败不影响启动

    def _goto_history_page(self):
        """地区 tab 行尾「📈 历史」快捷入口 → 导航「📈 历史趋势」页。

        v1.4.x 导航重构：原 _show_history_dialog Toplevel 已整体迁移为导航页
        （stats_ui.StatsPagesMixin._build_history_page，唯一实现）；此处只做
        _show_page 跳转，不留第二个独立实现。
        """
        self._show_page(self.page_history)

    def _history_day_detail(self, parent, region, day, store=None):
        """某地区某日明细（query_region_days）；双击行看单商品趋势折线。

        t6：store 透传（None/'' = 全部店铺；来自历史页店铺筛选，见 stats_ui on_open）。
        """
        if history_db is None:
            return
        top = tk.Toplevel(parent)
        top.title(f"{region} · {day} 明细")
        top.geometry(self._geo(820, 420))
        top.configure(bg=self.C_BG)
        top.transient(parent)
        cols = ('time', 'name', 'sku', 'stock', 'sales', 'status', 'qty', 'warehouse')
        heads = (('time', '识别时间', 130), ('name', '商品名', 190), ('sku', 'SKU', 110),
                 ('stock', '库存', 60), ('sales', '销量', 60), ('status', '状态', 90),
                 ('qty', '补货量', 70), ('warehouse', '仓库', 100))
        tree = ttk.Treeview(top, columns=cols, show='headings', height=12)
        for cid, text, w in heads:
            tree.heading(cid, text=text)
            tree.column(cid, width=w, anchor='center')
        vsb = ttk.Scrollbar(top, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y", padx=(0, 10), pady=(0, 8))
        tree.pack(fill="both", expand=True, padx=(10, 0), pady=(0, 8))
        try:
            rows = history_db.query_region_days(region, day, store=store)
        except Exception:
            rows = []
        for r in rows:
            tree.insert('', 'end', values=(
                r.get('captured_at', ''), r.get('name', ''), r.get('sku_id', ''),
                r.get('stock', 0), r.get('sales', 0), r.get('status', ''),
                r.get('qty', 0), r.get('warehouse', '')))
        if not rows:
            tk.Label(top, text="该地区当日无历史明细", font=(self.FONT[0], 9),
                     fg=self.C_MUTED, bg=self.C_BG).pack(pady=20)

        def on_open(_event):
            sel = tree.selection()
            if not sel:
                return
            vals = tree.item(sel[0], 'values')
            if len(vals) >= 3:
                sku = vals[2]
                name = vals[1]
                self._history_sku_chart(top, sku, region, name, store=store)

        tree.bind('<Double-1>', on_open)
        tk.Label(top, text="双击商品行查看单商品库存趋势折线", font=(self.FONT[0], 8),
                 fg=self.C_MUTED, bg=self.C_BG).pack(pady=(0, 8))

    def _history_sku_chart(self, parent, sku, region, name, store=None):
        """单商品库存趋势折线（Canvas 手绘，零图表库依赖）。

        sku 为空时按 (region, name) 精确回退（与 history_db 关联键语义一致）。
        t6：store 透传（None/'' = 全部店铺；明细窗继承历史页店铺筛选）。
        """
        if history_db is None:
            return
        try:
            rows = history_db.query_sku_history(sku, days=3650, store=store) if sku else \
                history_db.query_sku_history('', days=3650, region=region, name=name,
                                             store=store)
        except Exception:
            rows = []
        rows = rows or []
        if not rows:
            messagebox.showinfo("无趋势数据", "该商品暂无足够的历史记录。", parent=parent)
            return
        # R2 预测：单品预测段—— forecast_next_period 预测下一期日销；
        # 数据不足（<2 天）→ None，图表只显示提示文字（§4 显式，不编数）。
        _fc = None
        try:
            from algorithm_ui import forecast_next_period as _fnp
            _fc = _fnp(rows)
        except Exception:
            _fc = None
        stocks = [float(r.get('stock') or 0) for r in rows]
        times = [str(r.get('captured_at', '')) for r in rows]
        top = tk.Toplevel(parent)
        top.title(f"库存趋势 · {name[:20]}")
        top.geometry(self._geo(760, 380))
        top.configure(bg=self.C_BG)
        top.transient(parent)
        w, h = 720, 300
        mL, mR, mT, mB = 64, 24, 34, 44
        cv = tk.Canvas(top, width=w, height=h, bg=self.C_BG, highlightthickness=0)
        cv._skip_theme = True
        cv.pack(padx=12, pady=10)
        n = len(stocks)
        ymin, ymax = min(stocks), max(stocks)
        if ymax <= ymin:
            ymax = ymin + 1
        xs = [mL + i * (w - mL - mR) / max(n - 1, 1) for i in range(n)]
        ys = [mT + (h - mT - mB) * (1 - (s - ymin) / (ymax - ymin)) for s in stocks]
        cv.create_text(w // 2, 14, text=f"{name[:28]} 库存趋势（{n} 次识别）",
                       font=(self.FONT[0], 10, 'bold'), fill=self.C_TEXT)
        cv.create_line(mL, mT, mL, h - mB, fill=self.C_MUTED)
        cv.create_line(mL, h - mB, w - mR, h - mB, fill=self.C_MUTED)
        cv.create_text(mL - 8, mT, text=f"{ymax:.0f}", anchor='e',
                       font=(self.FONT[0], 7), fill=self.C_MUTED)
        cv.create_text(mL - 8, h - mB, text=f"{ymin:.0f}", anchor='e',
                       font=(self.FONT[0], 7), fill=self.C_MUTED)
        for i in range(n - 1):
            cv.create_line(xs[i], ys[i], xs[i + 1], ys[i + 1],
                           fill=self.C_ACCENT, width=2)
        for i in range(n):
            cv.create_oval(xs[i] - 2, ys[i] - 2, xs[i] + 2, ys[i] + 2,
                           fill=self.C_ACCENT, outline='')
        cv.create_text(mL, h - mB + 14, text=times[0][:16], anchor='w',
                       font=(self.FONT[0], 7), fill=self.C_MUTED)
        cv.create_text(w - mR, h - mB + 14, text=times[-1][:16], anchor='e',
                       font=(self.FONT[0], 7), fill=self.C_MUTED)
        # R2 预测：预测段可视化——预测值落在当前库存纵轴量程内时画虚线参考段
        # （跨度和量纲可能与库存不同，出量程就不硬画，靠文字标注承载，§4 不误导）
        if _fc is not None:
            try:
                _fcv = float(_fc)
                if ymin <= _fcv <= ymax:
                    _fy = mT + (h - mT - mB) * (1 - (_fcv - ymin) / (ymax - ymin))
                    cv.create_line(w - mR - 110, _fy, w - mR, _fy,
                                   fill=self.C_ACCENT, dash=(4, 3))
                    cv.create_text(w - mR - 114, _fy, text=f"预测 {forecast_cell_text(_fc)}",
                                   anchor='e', font=(self.FONT[0], 7), fill=self.C_MUTED)
            except Exception:
                pass
        cv.create_text(w // 2, h - mB + 30,
                       text=(f"最新库存 {stocks[-1]:.0f}（{times[-1][5:16]}）"
                             f"｜{forecast_note_text(_fc)}"),
                       font=(self.FONT[0], 8), fill=self.C_TEXT)

    # ─────────────────── v1.4.7 WS-B：表格导入（T-B2 GUI 侧）───────────────────

    def _import_table(self):
        """CSV/XLSX 结构化导入入口：filedialog → 映射预览 → worker 导入 → 报告+清洗+收口。

        流程（T-B2）：线程内不碰 Tk；结果经 win.after 回主线程；name/region/warehouse
        过 export_xlsx._sanitize_cell（强制复用点②）后 _fill_from_ocr(source='import') 收口。
        """
        # v1.5.8（BUG_HUNT_V157 A1）：批量互斥守卫——批量识别/批量图片运行中禁止导入表格
        # （导入也走 TaskQueue/识别队列，双批并发会串台状态；与 _batch_images 同款提示）
        if getattr(self, '_batch_running', False) or getattr(self, '_img_batch_running', False):
            messagebox.showinfo("导入", "批量任务正在进行中，请先等待完成或停止后再试",
                                parent=self.win)
            return
        import table_import
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="选择要导入的表格（CSV / XLSX）",
            filetypes=[("表格文件", "*.csv *.xlsx"), ("所有", "*.*")])
        if not path:
            return
        if str(path).lower().endswith('.xls'):
            # 归类为 legacy_xls 文案（与 table_import 一致）
            from ocr_review import categorize_error as _ce
            _cat, _msg, _title = _ce('暂不支持 .xls 老格式')
            messagebox.showerror(_title, _msg, parent=self.win)
            return
        try:
            headers, _rows = table_import.read_table_rows(path)
        except Exception as e:
            # 异常归类（编码失败 / XLSX 损坏 / 文件不存在 / 行超限）
            self._friendly_error(e, popup=True)
            return
        if not headers:
            # 文件首个非空行没有表头：归类为 xlsx_corrupt / mapping_missing
            self._friendly_error('文件首个非空行没有表头，无法识别列映射',
                                 popup=True, title='导入失败')
            return
        mapping, has_region = self._import_preview_dialog(headers, path)
        if mapping is None:
            return  # 用户取消
        self.status_text.set("导入中...")
        self.win.update()

        def task(_progress=None):
            # v1.5.9.4-hotfix：TaskQueue 契约 fn(progress)——旧 def task() 无参
            # 导致表格导入任务一执行即 TypeError（导入一直静默失败的真凶）
            # 异常由 TaskQueue 捕获并通过 on_error 回调
            items, issues = table_import.import_items(path, mapping=mapping)
            self.win.after(0, lambda i=items, s=issues: self._import_done(i, s, has_region))

        # 使用 TaskQueue 执行任务
        # 导入异常用 _friendly_error 归类（编码失败 / 损坏 / 行超限 / 列映射缺失）
        self._task_queue.submit(
            "表格导入",
            task,
            on_error=lambda e: self.win.after(0, lambda exc=e: self._friendly_error(exc, popup=True)),
        )

    def _import_preview_dialog(self, headers, path):
        """映射预览对话框：文件表头 ↔ 业务字段对位 + 缺失清单 + 可改下拉 + 生成模板。

        返回 (mapping|None, has_region)：None=用户取消；确认时 mapping 必含
        name/stock/sales（缺任一不允许导入，宪法 §1 同纪律——不静默 fallback）。
        """
        import table_import
        try:
            found, missing = table_import.guess_mapping(headers)
        except Exception:
            found, missing = {}, ['name', 'stock', 'sales']
        # R1 效率：导入映射记忆——读上次确认过的映射，文件表头与之一致
        # （import_memory.last_mapping_matches：核心 name/stock/sales 经
        # normalize_col_name 全命中）则用 resolve_last_mapping 对位预填下拉；
        # 读取失败/模块缺失降级为无记忆（guess_mapping 结果照旧），不阻塞导入。
        # 清除入口在设置页，gui 这里只读写。
        _prefill = {}
        try:
            if import_memory is not None:
                _last_map = import_memory.get_last_mapping()
                if _last_map and import_memory.last_mapping_matches(headers, _last_map)[0]:
                    _prefill = resolve_last_mapping(headers, _last_map)
        except Exception:
            _prefill = {}
        # 预填已覆盖的字段不再算「未自动识别」（记忆命中也算识别，提示不再吓人）
        if _prefill:
            missing = [f for f in missing if f not in _prefill]
        top = tk.Toplevel(self.win)
        top.title("导入映射预览")
        top.geometry(self._geo(500, 400))
        top.configure(bg=self.C_BG)
        top.transient(self.win)
        tk.Label(top, text=f"文件：{os.path.basename(path)}（{len(headers)} 列）",
                 font=(self.FONT[0], 9), fg=self.C_TEXT, bg=self.C_BG).pack(
            anchor='w', padx=16, pady=(14, 2))
        if missing:
            tk.Label(top, text=f"⚠ 未自动识别关键列：{'、'.join(missing)} — 请在下方下拉手工指定",
                     font=(self.FONT[0], 8), fg='#C62828', bg=self.C_BG).pack(
                anchor='w', padx=16, pady=2)
        else:
            tk.Label(top, text=("✓ 已按上次导入映射预填（表头一致），可调整后确认导入"
                                if _prefill else "✓ 关键列已自动识别，可调整后确认导入"),
                     font=(self.FONT[0], 8), fg=self.C_MUTED, bg=self.C_BG).pack(
                anchor='w', padx=16, pady=2)
        fields = [('name', '商品名(必填)'), ('stock', '库存(必填)'), ('sales', '销量(必填)'),
                  ('region', '销售区域(可选)'), ('warehouse', '仓库(可选)')]
        combo_vars = {}
        for fid, label in fields:
            row = tk.Frame(top, bg=self.C_BG)
            row.pack(fill="x", padx=16, pady=3)
            tk.Label(row, text=label, width=13, anchor='e', font=(self.FONT[0], 9),
                     fg=self.C_TEXT, bg=self.C_BG).pack(side="left")
            v = tk.StringVar(top, value=(_prefill.get(fid) or found.get(fid, '(不使用)')))
            ttk.Combobox(row, textvariable=v, values=['(不使用)'] + list(headers),
                         state='readonly', width=26,
                         font=(self.FONT[0], 9)).pack(side="left", padx=8)
            combo_vars[fid] = v

        def gen_template():
            try:
                out_dir = os.path.join(get_base_dir(), 'output')
                os.makedirs(out_dir, exist_ok=True)
                tpath = table_import.write_template(
                    os.path.join(out_dir, 'PDD导入模板.xlsx'))
                try:
                    os.startfile(tpath)
                except Exception:
                    messagebox.showinfo("模板已生成", f"模板文件：\n{tpath}", parent=top)
            except Exception as e:
                messagebox.showerror("模板生成失败", str(e)[:200], parent=top)

        result = []

        def confirm():
            mapping = {}
            for fid, _label in fields:
                col = combo_vars[fid].get()
                if col and col != '(不使用)':
                    mapping[fid] = col
            absent = [f for f in ('name', 'stock', 'sales') if not mapping.get(f)]
            if absent:
                messagebox.showwarning(
                    "缺少关键列",
                    f"{'、'.join(absent)} 为必填映射，请选择对应列后再导入。",
                    parent=top)
                return
            # R1 效率：用户确认的映射存回记忆（下次同结构文件自动预填）。
            # 写失败仅记日志、不阻塞本次导入（§4 显式留痕，不静默）。
            try:
                if import_memory is not None and not import_memory.save_last_mapping(mapping):
                    log.warn("导入映射记忆保存失败（settings.json 写盘异常），本次导入不受影响")
            except Exception:
                pass
            result.append((mapping, 'region' in mapping))
            top.destroy()

        btns = tk.Frame(top, bg=self.C_BG)
        btns.pack(fill="x", padx=16, pady=(12, 14))
        self._mk_btn(btns, "生成模板", gen_template, kind='ghost',
                     font=(self.FONT[0], 9)).pack(side="left", padx=4)
        self._mk_btn(btns, "取消", top.destroy, kind='ghost',
                     font=(self.FONT[0], 9)).pack(side="right", padx=4)
        self._mk_btn(btns, "确认导入", confirm, kind='primary',
                     font=(self.FONT[0], 9, 'bold')).pack(side="right", padx=4)
        top.grab_set()
        top.wait_window()
        return result[0] if result else (None, False)

    def _import_done(self, items, issues, has_region):
        """主线程收口：导入报告 → 程序端清洗（_sanitize_cell）→ _fill_from_ocr。"""
        try:
            if issues:
                self._import_report_dialog(issues)
            # 强制复用点 （R7）：导入侧公式注入清洗——name/region/warehouse 过 _sanitize_cell
            from export_xlsx import _sanitize_cell
            for p in items:
                if isinstance(p, dict):
                    p['name'] = _sanitize_cell(str(p.get('name', '') or ''))
                    p['region'] = _sanitize_cell(str(p.get('region', '') or ''))
                    p['warehouse'] = _sanitize_cell(str(p.get('warehouse', '') or ''))
            if not items:
                self.status_text.set("导入完成：0 条有效数据（详见导入报告）")
                return
            self._fill_from_ocr(items, source='import')
            if not has_region:
                self.status_text.set(
                    f"⚠ 未识别销售区域列，全部商品已归入当前地区「{self.region_var.get()}」")
        except Exception as e:
            self._show_error(f"导入数据处理失败: {str(e)[:80]}", popup=True)

    def _import_report_dialog(self, issues):
        """导入报告：行号/商品/级别/原因（前 200 条 + 计数汇总）。"""
        top = tk.Toplevel(self.win)
        top.title("导入报告")
        top.geometry(self._geo(620, 400))
        top.configure(bg=self.C_BG)
        top.transient(self.win)
        n_err = sum(1 for i in issues if i.get('level') == 'error')
        n_warn = len(issues) - n_err
        tk.Label(top, text=f"共 {len(issues)} 条提示：错误 {n_err}，警告 {n_warn}"
                           + ("（仅显示前 200 条）" if len(issues) > 200 else ""),
                 font=(self.FONT[0], 9), fg=self.C_TEXT, bg=self.C_BG).pack(
            anchor='w', padx=12, pady=(10, 4))
        cols = ('row', 'name', 'level', 'reason')
        heads = (('row', '行号', 60), ('name', '商品', 200), ('level', '级别', 60),
                 ('reason', '原因', 240))
        tree = ttk.Treeview(top, columns=cols, show='headings', height=12)
        for cid, text, w in heads:
            tree.heading(cid, text=text)
            tree.column(cid, width=w, anchor='w' if cid in ('name', 'reason') else 'center')
        vsb = ttk.Scrollbar(top, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y", padx=(0, 10), pady=(0, 8))
        tree.pack(fill="both", expand=True, padx=(10, 0), pady=(0, 8))
        for i in issues[:200]:
            tree.insert('', 'end', values=(
                i.get('row', ''), i.get('name', ''), i.get('level', ''),
                i.get('reason', '')))
        self._mk_btn(top, "关闭", top.destroy, kind='dark',
                     font=(self.FONT[0], 9)).pack(pady=(0, 10))

    def _on_tree_yscroll(self, first, last):
        self._vsb_first = float(first); self._vsb_last = float(last)
        self._draw_vsb()

    def _on_tree_xscroll(self, first, last):
        self._hsb_first = float(first); self._hsb_last = float(last)
        self._draw_hsb()

    def _draw_vsb(self):
        """纤细深色纵向滚动条：3px 滑轨 + 深灰滑块（深炭表头区内的暗色系，随主题 token）"""
        c = self._vsb_canvas
        c.delete('all')
        h = c.winfo_height()
        if h <= 0:
            return
        # 深炭 #1F1F1F 表头区内：滑轨/滑块保持暗色语义（终末地风格不变），
        # 色值走主题 token 便于其他主题覆写
        _track = self.tc('table.scroll_track', '#3A3A3A')
        _thumb = self.tc('table.scroll_thumb', '#5A5A5A')
        c.create_rectangle(4, 0, 5, h, fill=_track, outline='')  # 滑轨
        y0 = self._vsb_first * h; y1 = self._vsb_last * h
        if y1 - y0 >= 4:
            c.create_rectangle(3, y0, 6, y1, fill=_thumb, outline='')  # 滑块

    def _draw_hsb(self):
        c = self._hsb_canvas
        c.delete('all')
        w = c.winfo_width()
        if w <= 0:
            return
        _track = self.tc('table.scroll_track', '#3A3A3A')
        _thumb = self.tc('table.scroll_thumb', '#5A5A5A')
        c.create_rectangle(0, 4, w, 5, fill=_track, outline='')
        x0 = self._hsb_first * w; x1 = self._hsb_last * w
        if x1 - x0 >= 4:
            c.create_rectangle(x0, 3, x1, 6, fill=_thumb, outline='')

    def _click_vsb(self, event):
        self._scroll_vsb_to(event.y)

    def _drag_vsb(self, event):
        self._scroll_vsb_to(event.y)

    def _scroll_vsb_to(self, y):
        h = self._vsb_canvas.winfo_height()
        if h <= 0:
            return
        total = self._vsb_last - self._vsb_first
        if total <= 0:
            return
        frac = y / h
        self.tree.yview_moveto(max(0.0, min(1.0, frac - total / 2)))

    def _click_hsb(self, event):
        self._scroll_hsb_to(event.x)

    def _drag_hsb(self, event):
        self._scroll_hsb_to(event.x)

    def _scroll_hsb_to(self, x):
        w = self._hsb_canvas.winfo_width()
        if w <= 0:
            return
        total = self._hsb_last - self._hsb_first
        if total <= 0:
            return
        frac = x / w
        self.tree.xview_moveto(max(0.0, min(1.0, frac - total / 2)))

    def _tree_context_menu(self, event):
        """右键表格：数据行→删除该行；空白处→新增空白行"""
        menu = tk.Menu(self.win, tearoff=0)
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            menu.add_command(label="删除该行", command=self._del_row)
            menu.add_command(label="新增空白行", command=self._add_row)
        else:
            menu.add_command(label="新增空白行", command=self._add_row)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _tree_edit_cell(self, event):
        """双击可编辑列（商品名/仓库总库存/仓库预估总销售数，由动态列 mapping 决定）
        → overlay Entry 编辑 → 回写 rows → 重算。其他列（仓库信息/销售库存/计算列）只读。"""
        iid = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)
        if not iid or not col_id:
            return
        col_idx = int(col_id[1:]) - 1
        # 动态列：按当前列名反查业务字段（旧版固定前3列映射在动态列下会错位）
        try:
            _cols = list(self.tree['columns'])
            if col_idx < 0 or col_idx >= len(_cols):
                return
            _col_name = _cols[col_idx]
            from utils import get_ocr_columns
            _m = (get_ocr_columns().get('mapping') or {})
            # 缓存按 mapping 内容做 key：用户改「识别列配置」后 mapping 变化，
            # 旧缓存自动失效重建——否则双击新勾选列被误判只读（v1.4 审查修复）
            _map_key = tuple(sorted((str(k), str(v)) for k, v in _m.items() if v))
            _cached = getattr(self, '_tree_col_map', None)
            if not _cached or _cached.get('_key') != _map_key:
                _map = {v: k for k, v in _m.items() if v}
                _map['_key'] = _map_key
                self._tree_col_map = _map
            else:
                _map = _cached
            _field = _map.get(_col_name)
        except Exception:
            return
        if _field not in ('name', 'stock', 'sales'):
            return  # 非可编辑字段（仓库信息/销售库存/计算列）只读
        row_idx = getattr(self, '_row_index_map', {}).get(iid)
        if row_idx is None or row_idx >= len(self.rows):
            return
        bbox = self.tree.bbox(iid, col_id)
        if not bbox:
            return
        x, y, w, h = bbox
        var = self.rows[row_idx][_field]
        entry = tk.Entry(self.tree, font=self.FONT, relief='flat', bd=0,
                         highlightthickness=1, highlightbackground='#CCCCCC',
                         highlightcolor='#FFE600',
                         bg='#FFFFFF', fg='#111111', insertbackground='#111111')
        entry.place(x=x, y=y, width=w, height=h)
        entry.insert(0, var.get())
        entry.focus_set()
        entry.select_range(0, 'end')
        
        def _commit(*_a):
            # 防重入：destroy 可能触发二次 <FocusOut>，第二次 entry 已销毁，
            # entry.get() 会抛 TclError（destroy 在 try 内、get 在 try 外）→ 加提交标志
            if getattr(entry, '_committed', False):
                return
            entry._committed = True
            try:
                val = entry.get().strip()
            except Exception:
                return
            ok = True
            if _field == 'name':
                var.set(val)
            else:
                ok = (val == '' or _validate_num_entry(val))
                if ok:
                    var.set(val)
            try:
                entry.destroy()
            except Exception:
                pass
            if ok:
                self._recalc_from_rows()
        
        entry.bind('<Return>', _commit)
        entry.bind('<FocusOut>', _commit)
        entry.bind('<Escape>', lambda *_a: entry.destroy())
    
    def _sort_tree(self, col):
        """点击列头排序（v1.3 动态列：按当前 tree 列名找索引）"""
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = False
        
        # 列名到索引：动态取 tree 当前列顺序（v1.3 起列不固定）
        try:
            idx = self.tree['columns'].index(col)
        except (ValueError, AttributeError):
            idx = 0
        
        # 获取所有行数据
        items = [(self.tree.set(child, col), child) for child in self.tree.get_children()]
        
        # 尝试数字排序
        def sort_key(item):
            val = item[0]
            try:
                return (0, float(val), val)
            except ValueError:
                return (1, 0, val)
        
        items.sort(key=sort_key, reverse=self._sort_reverse)
        
        # 重新排列
        for i, (_, child) in enumerate(items):
            self.tree.move(child, '', i)
        
        # 更新表头箭头
        arrow = ' ▼' if self._sort_reverse else ' ▲'
        for c in self.tree['columns']:
            text = c
            if c == col:
                text += arrow
            self.tree.heading(c, text=text, command=lambda cc=c: self._sort_tree(cc))
    
    def _export(self):
        """导出所有缓存地区到 Excel"""
        if not self.cache:
            if hasattr(self, 'plans') and self.plans:
                # 兜底：cache 为空但有计算结果时补一份（items 用 plans 反填，
                # 避免 items=[] 导致切地区 tab 时 rows 被清空——v1.4 全量审查修复）
                self.cache[self.region_var.get()] = {'plans': self.plans, 'items': self.plans}
            else:
                messagebox.showwarning("无数据", "请先识别至少一个地区")
                return
        try:
            import openpyxl
        except ImportError:
            messagebox.showerror("缺少依赖", "请安装 openpyxl: pip install openpyxl")
            return
        try:
            from export_xlsx import export_cache_to_xlsx, _get_default_export_dir
            export_dir = _get_default_export_dir()
            # 导出带「店铺」列（当前店铺名；store_registry 缺失/异常时空串兜底）
            _store_name = ''
            try:
                if store_registry is not None:
                    _store_name = store_registry.get_store_name(
                        getattr(self, '_store_id', None) or 'default')
            except Exception:
                _store_name = ''
            path = export_cache_to_xlsx(self.cache, export_dir, store_name=_store_name)
            self.status_text.set(f"已导出 {len(self.cache)} 个地区 → PDD补货记录.xlsx")
            # C：导出完成状态反馈脉冲
            self._pulse_status()
            try:
                os.startfile(export_dir)
            except OSError as e:
                messagebox.showwarning("无法打开目录", f"导出成功，但打开目录失败：{e}\n文件位置: {path}")
                return
            messagebox.showinfo("导出成功", f"已导出 {len(self.cache)} 个地区\n文件: {path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))
    
    def run(self):
        """程序主入口：启动 Tk 主循环"""
        self.win.mainloop()


if __name__ == "__main__":
    App().run()
