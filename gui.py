"""
PDD EZ — 补货排期助手
客户看后台页面，输入库存和预估销量，自动算补货时间
"""

import os, sys, threading, time
from datetime import datetime

from utils import get_base_dir, get_api_config, VERSION, version_newer
from settings_ui import SettingsUIMixin
from logger import log

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
            # 一次点击会触发 2~3 次；三层防护：
            #  1) 250ms 时间戳（常规快速连点）
            #  2) _executing 执行中标志（模态循环期间事件仍可派发的场景）
            #  3) 延迟复位 _executing：原生模态对话框（askopenfilename 等）会
            #     阻塞主线程，第二个排队事件在对话框关闭、_command 返回之后
            #     才派发——立即复位拦不住，改为 after(300ms) 复位
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
        self._apply()


class App(SettingsUIMixin):
    # Design system — New Minimalism / Flat Design
    C_PRIMARY = '#111111'      # 近黑（主标题/文字）
    C_SECONDARY = '#333333'    # 深灰（次级文字）
    C_ACCENT = '#FFE600'       # 亮柠檬黄（accent / 高亮块）
    C_BG = '#FFFFFF'           # 纯白背景
    C_SURFACE = '#F7F7F2'      # 米白浅灰（卡片/底纹）
    C_TEXT = '#222222'         # 深灰正文（避免死黑）
    C_MUTED = '#6B6B6B'        # 中灰
    C_BORDER = '#EAEAEA'       # 浅灰细分割线（容器不画黑框）
    C_RED = '#DC2626'
    C_BTN_BLUE = '#1E88E5'     # 主操作按钮（亮蓝实心）
    C_CARD_HDR = '#1F1F1F'     # 卡片标题栏（深炭灰）
    C_YELLOW_BG = '#FFE600'    # 亮柠檬黄高亮块（配黑字）
    C_GREEN_BG = '#E8F5E9'
    C_RED_BG = '#FFEBEE'
    C_BLUE_LIGHT = '#FFF3B0'   # 浅黄（导航按钮/标签底，机能风）
    FONT = ('Microsoft YaHei UI', 9)
    FONT_BOLD = ('Microsoft YaHei UI', 9, 'bold')
    FONT_TITLE = ('Microsoft YaHei UI', 14, 'bold')
    FONT_HEADING = ('Microsoft YaHei UI', 11, 'bold')

    def _mk_btn(self, parent, text, command=None, kind='primary', font=None,
                width=None, height=None, padx=10, pady=3, pack_side=None, **pack_kw):
        """终末地机能风切角按钮（Canvas 自绘，完全扁平无渐变）：
        kind='primary' → 亮黄实心黑字细黑描边（一级主按钮）
        kind='dark'    → 炭黑底白字（二级功能按钮）
        kind='ghost'   → 白底黑字细黑描边（幽灵次要按钮）
        kind='text'    → 黑字 + 底部细黄下划线（文字型操作）
        kind='tag'     → 亮黄底黑粗字（角标标签）
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
        self._apply_theme(self._theme_name)
        self.rows = []
        self.plans = []  # 初始化，供 _export 防御性检查
        self._filter_warning_only = False  # 结果表"仅显示预警"筛选
        self._wh_filter = '全部仓库'       # 结果表"仓库筛选"（来自 OCR 仓库信息列）
        self._suppress_auto_append = False  # 清空输入时临时禁用自动加行
        self._batch_stop = threading.Event()  # 紧急停止信号
        self.status_text = tk.StringVar(self.win, value="就绪｜确认数据后导出，识别结果表格可直接编辑，右键行可删除条目")
        self.regions = self._load_regions()
        # 当前地区由截图识别后确定；初始不预设配置表第一个地区（云南是时效配置，不是当前地区）
        self.region_var = tk.StringVar(self.win, value='未识别')
        
        # 多地区缓存
        self.cache = {}  # {region: {'plans': [...], 'items': [...]}}
        self.active_region = None
        
        self._build_ui()
        self._check_update()  # 后台检查更新
        self._check_announcement()  # 后台检查公告（v1.4 新增，借鉴 March7th）

    def _check_announcement(self):
        """后台拉取公告，有则弹窗展示（静默失败不打扰客户）"""
        from announcement import check_announcement
        try:
            check_announcement(self.win, self._show_announcement)
        except Exception:
            pass

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
            # 可选图片
            if image_url:
                try:
                    req = _ur.Request(image_url, headers={"User-Agent": "PDD-EZ"})
                    img_data = _ur.urlopen(req, timeout=10).read()
                    img = PILImage.open(_BytesIO(img_data))
                    img.thumbnail((440, 160))
                    photo = ImageTk.PhotoImage(img)
                    lbl = tk.Label(dlg, image=photo, bg=self.C_BG)
                    lbl.image = photo  # 防 GC
                    lbl.pack(pady=5)
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
            else:
                log.debug(f"更新检查: 已是最新 ({latest})")
        except Exception as e:
            # 静默 UI + 留日志（客户报"点更新没反应"时可定位网络/镜像问题）
            log.warning(f"更新检查失败: {e}")
    
    def _build_ui(self):
        # ── 全局热键 ──
        self.win.bind('<F9>', lambda e: self._emergency_stop())
        
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
        self._mk_btn(tool_bar, "🏪 商家后台", self._open_backend, kind='ghost',
                     pack_side="right", padx=5)
        self._mk_btn(tool_bar, "🔄 更新", self._run_updater, kind='ghost',
                     pack_side="right", padx=5)
        
        # ── 主容器：左导航 + 右内容（可拖拽分割） ──
        self.main_paned = tk.PanedWindow(self.win, orient="horizontal", sashwidth=3, bg=self.C_BORDER)
        self.main_paned.pack(fill="both", expand=True, padx=15, pady=(2, 15))
        # 左侧导航栏
        self.nav_frame = tk.Frame(self.main_paned, width=170, bg=self.C_BG)
        self.nav_frame._skip_theme = True  # 导航栏保持 C_SURFACE 区分色，主题切换不覆盖
        self.nav_frame.pack_propagate(False)
        self.nav_buttons = {}
        # 右侧内容
        self.content_frame = tk.Frame(self.main_paned)
        self.main_paned.add(self.content_frame, stretch="always")
        # 页面帧
        self.page_home = tk.Frame(self.content_frame, bg=self.C_BG)
        self.page_general = tk.Frame(self.content_frame, bg=self.C_BG)
        self.page_products = tk.Frame(self.content_frame, bg=self.C_BG)
        self.page_theme = tk.Frame(self.content_frame, bg=self.C_BG)
        self.page_backend = tk.Frame(self.content_frame, bg=self.C_BG)
        self.page_api = tk.Frame(self.content_frame, bg=self.C_BG)
        self._current_page = self.page_home
        
        # 初始 3 行数据对象（识别结果表承载显示/编辑，rows 仅存数据）
        for _ in range(3):
            self._add_row()
        
        # ── 全局工具栏（单行：左组功能 / 右组截图+当前地区，不再放置加行/删行按钮）──
        btn_row = tk.Frame(self.page_home, bg=self.C_BG)
        btn_row.pack(fill="x", padx=15, pady=(8, 6))
        # 左组：功能按钮（间距统一，防视觉不均）
        self._mk_btn(btn_row, "🔄 刷新计算", self._recalc_from_rows, kind='dark',
                     font=(self.FONT[0], 9, 'bold'), pack_side="left")
        self._mk_btn(btn_row, "📋 批量识别", self._batch_scan, kind='dark',
                     pack_side="left", padx=10)
        # 单次识别双模型开关（v1.3：不在乎 token 成本，默认开，识别更准）
        self._single_dual_var = tk.BooleanVar(self.win, value=True)
        tk.Checkbutton(btn_row, text="🛡 双模型", variable=self._single_dual_var,
                       font=(self.FONT[0], 8), bg=self.C_SURFACE, fg=self.C_MUTED,
                       selectcolor=self.C_SURFACE, activebackground=self.C_SURFACE).pack(side="left", padx=10)
        # 右组：截图识别 + 实时截图
        self._mk_btn(btn_row, "截图识别", self._ocr_fill, kind='text', pack_side="right")
        self._mk_btn(btn_row, "实时截图", self._live_screenshot, kind='text',
                     pack_side="right", padx=5)
        
        # ── 当前地区（刷新计算按钮正下方一行，左对齐；识别后更新）──
        region_line = tk.Frame(self.page_home, bg=self.C_BG)
        region_line.pack(fill="x", padx=15, pady=(0, 2))
        tk.Label(region_line, text="当前地区:", font=(self.FONT[0], 8), fg=self.C_MUTED).pack(side="left")
        tk.Label(region_line, textvariable=self.region_var,
                 font=(self.FONT[0], 8), fg=self.C_MUTED).pack(side="left", padx=(0, 4))
        
        # ── 导出按钮 + 单条合并状态行（导出正下方，不拆分）──
        self.export_btn = self._mk_btn(self.page_home, "导出 Excel", self._export,
                  kind='primary', font=(self.FONT[0], 13, 'bold'), width=16, height=2,
                  pack_side=None)
        self.export_btn.pack(pady=(12, 4))
        tk.Label(self.page_home, textvariable=self.status_text,
                 font=(self.FONT[0], 8), fg=self.C_MUTED).pack(pady=(0, 4))
        
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
            self.main_paned.add(self.nav_frame, before=self.content_frame, minsize=150, stretch="never")
            if not self.nav_buttons:
                self._build_nav()

    def _build_nav(self):
        items = [
            ("🏠 首页", self.page_home),
            ("⚙ 通用", self.page_general),
            ("📦 商品", self.page_products),
            ("🔑 API", self.page_api),
            ("🎨 主题", self.page_theme),
            ("🔗 后台", self.page_backend),
        ]
        for text, page in items:
            _nf = tk.Frame(self.nav_frame, bg=self.C_BG, bd=0, highlightthickness=0)
            _nf._skip_theme = True
            _ni = tk.Frame(_nf, bg=self.C_BG, bd=0, highlightthickness=0)
            _ni._skip_theme = True
            _ni.pack(side="left", padx=(0, 0), pady=0, fill="x", expand=True)
            btn = tk.Button(_ni, text=text, relief="flat",
                           font=(self.FONT[0], 9), anchor="w", padx=12, pady=6,
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
                # 选中项：深炭黑背景高亮（参考站，不用黄竖线）
                btn.configure(bg=self.C_CARD_HDR, fg="#FFFFFF")
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
        if not hasattr(page, '_built'):
            page._built = True
        # 切页只刷新模型徽章；主题全量重涂仅在主题切换时执行（避免每次切页全树 walk）
        self._refresh_model_badge()

    
    def _show_error(self, msg, popup=False):
        """显示错误：状态栏 + 报错栏，可选弹窗"""
        self.status_text.set(f"❌ {msg[:50]}")
        if popup:
            messagebox.showerror("出错", msg)
    
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
        """加载地区→商品运输时效映射，兼容旧格式 {region: days} → {region: {product: days}}"""
        import json, shutil
        path = os.path.join(get_base_dir(), 'regions.json')
        # EXE 首次运行：从内置资源复制模板
        if not os.path.exists(path) and getattr(sys, 'frozen', False):
            bundled = os.path.join(sys._MEIPASS, 'regions.json')
            if os.path.exists(bundled):
                shutil.copy(bundled, path)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except:
            return {}
        # 兼容旧格式：值如果是数字（旧 {region: days}），存到 "" 键保留默认天数，
        # 新格式 product 名优先，无匹配回退 ""（旧默认），再回退全局默认 3
        result = {}
        for region, val in data.items():
            if isinstance(val, (int, float)):
                result[region] = {"": val}
            elif isinstance(val, dict):
                result[region] = val
            else:
                result[region] = {}
        return result
    
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
        
        def _set_status(text, pct=None):
            status_lbl.configure(text=text)
            if pct is not None:
                progress.configure(value=pct)
            dlg.update_idletasks()
        
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
            import subprocess, tempfile, json as _json, hashlib as _hashlib
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
                
                # 3) SHA256 校验（有 sha256 资产时）
                if sha_asset:
                    self.win.after(0, lambda: _set_status("正在校验下载完整性..."))
                    sha_path = dest + ".sha256"
                    try:
                        req = _Request(sha_asset["browser_download_url"],
                                       headers={"Accept": "application/octet-stream", "User-Agent": "PDD-EZ"})
                        with _urlopen(req, timeout=30) as resp:
                            with open(sha_path, 'wb') as f:
                                f.write(resp.read())
                        expected = open(sha_path, 'r').read().strip().split()[0]
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
                        self.win.after(0, lambda: _fail("SHA256 校验文件下载失败，已拒绝安装（安全策略）", str(e)))
                        return
                
                state["downloaded_zip"] = dest
                state["sha_ok"] = True
                self.win.after(0, lambda: _set_status("下载完成，准备安装...", 100))
                self.win.after(0, _ask_install)
                
            except _CancelledDownload:
                # 用户取消：静默返回，不弹错误
                return
            except Exception as e:
                self.win.after(0, lambda: _fail("下载失败，请检查网络", str(e)))
        
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
        import json
        path = os.path.join(get_base_dir(), 'regions.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.regions, f, ensure_ascii=False, indent=2)
    
    def _get_shipping(self, region, product_name):
        """获取某个地区某个商品的运输天数，未设置则默认 3 天"""
        region_data = self.regions.get(region, {})
        if isinstance(region_data, dict):
            # product 名优先，回退旧格式默认天数（"" 键），再回退全局默认 3
            return region_data.get(product_name, region_data.get('', 3))
        return 3  # 兼容旧格式
    
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

        for item in items:
            name = item.get('name', '')
            stock = _to_int(item.get('stock', 0))
            daily = max(_to_int(item.get('sales', 0)), 0)
            calc_daily = daily if daily > 0 else 1  # 除法保护，显示保留原始值
            # 每商品用自己的地区查时效（批量识别多省份各行 region 不同），
            # 无则回退当前地区——修复其他省份商品被按最后省份计算
            _it_region = item.get('region') or region
            shipping = self._get_shipping(_it_region, name)  # 逐商品查运输时效
            
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
                # 通用列原始数据：客户勾选列从 _raw 取原文显示
                '_raw': item.get('_raw') or {},
                '_sel_cols': _sel_cols,
                '_sel_cols_map': _sel_cols_map,
            })
        
        # Sort
        priority = {'red': 0, 'yellow': 1, 'green': 2}
        plans.sort(key=lambda p: priority.get(p['color'], 99))
        
        # Show（按筛选状态渲染，内部重建动态列）
        self._render_tree(plans)
        
        self.plans = plans
        self.status_text.set(f"计算完成 — {len(plans)} 个商品"
                             + ("（仅显示预警）" if self._filter_warning_only else ""))
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
        calc_cols = [('可售卖天数', 'ratio'), ('状态', 'status'), ('补货量', 'qty')]
        display_cols = list(_sel_cols) + [c[0] for c in calc_cols]
        try:
            self.tree.configure(columns=display_cols)
            for col in display_cols:
                # 数字/状态列加宽到 110，防"1109份"被截断成"110份"误导；
                # 名称类列（商品信息/商品名称/商品等）260 宽防截断（v1.4 修复：默认列"商品信息"此前判不到）
                width = 260 if col in ('商品信息', '商品名称', '商品') or '名称' in col or '商品' in col else 110
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
                elif _c == '状态':
                    row_vals.append(p.get('status', '') or '')
                elif _c == '补货量':
                    row_vals.append(p.get('qty', '') or '')
                elif _map.get(_c) == 'name':
                    # 商品名：用解析后的 name（去掉 ID 后缀），比 _raw 原文干净
                    row_vals.append(p.get('name', '') or '')
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
            return
        
        tk.Label(self.tab_frame, text="地区: ", font=(self.FONT[0], 8),
                 fg=self.C_MUTED).pack(side="left")
        for reg in sorted(self.cache.keys()):
            is_active = reg == self.active_region
            self._mk_btn(self.tab_frame, reg, lambda r=reg: self._switch_region(r),
                         kind='tag' if is_active else 'ghost',
                         font=("微软雅黑", 8, "bold" if is_active else "normal"),
                         pack_side="left", padx=2)
    
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
                        # 且避免纯数字值被 strip_tail_noise 的尾部数字规则误剥
                        import re as _re2
                        _unit = ''
                        _m2 = _re2.search(r'[^\d\s.,，、]+$', _old)
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
        """F9 紧急停止批量识别"""
        self._batch_stop.set()
        self.status_text.set("⏹ 紧急停止 — 等待当前识别结束（API 请求最长 60s），随后自动收尾")
    
    def _batch_scan(self):
        """批量识别：对已知地区逐个引导截图识别"""
        known = sorted(self.regions.keys())
        if not known:
            messagebox.showinfo("批量识别", "暂无知地区，请先手动「实时截图」识别一次")
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
            selected = [r for r, v in vars_map.items() if v.get()]
            if not selected:
                messagebox.showwarning("未选择", "请至少选择一个地区", parent=dlg)
                return
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
            # 禁用操作按钮防止并发
            for btn in [self.export_btn]:
                self.win.after(0, lambda b=btn: b.configure(state='disabled'))
            self.status_text.set("批量识别中 — 请不要操作")
            def _batch_thread_wrapper():
                """线程包装：任何异常都写日志 + 提示，避免静默死掉（窗口不恢复）"""
                try:
                    log.hr(f"批量识别开始：{len(selected)} 个地区", 1)
                    self._run_batch_sequence(selected, hud, hud_text, _dual_mode)
                    log.hr("批量识别完成", 1)
                except Exception as _e:
                    import traceback
                    log.error("批量识别异常:\n" + traceback.format_exc())
                    try:
                        with open(os.path.join(get_base_dir(), 'output', 'ocr_dlog.txt'),
                                  'a', encoding='utf-8') as _f:
                            _f.write('[batch] 线程异常: ' + traceback.format_exc() + '\n')
                    except Exception:
                        pass
                    try:
                        self.win.after(0, lambda: self.status_text.set(f"❌ 批量识别异常: {str(_e)[:80]}"))
                        self.win.after(0, self.win.deiconify)
                        # 异常路径立即恢复导出按钮，不等 10 分钟 idle 兜底（result_queue 无 None 信号）
                        self.win.after(0, lambda: self.export_btn.configure(state='normal'))
                    except Exception:
                        pass  # 主窗口可能已销毁/关闭，UI 恢复失败无妨（程序正在退出）
            threading.Thread(target=_batch_thread_wrapper, daemon=True).start()
        
        _sb = self._mk_btn(bottom_frame, "开始批量识别", start_batch, kind='primary',
                           font=self.FONT_BOLD, width=18, height=2)
        _sb.pack_configure(pady=(10,0))
        
        dlg.transient(self.win)
        dlg.grab_set()
    
    def _run_batch_sequence(self, regions, hud=None, hud_text=None, dual_verify=False):
        """批量识别：1.点文本框 2.粘贴省份 3.回车 4.点查询 5.等刷新
        6.截图识别（AI 定位表格 + 滚动加载循环，直到无更多商品）
        不填仓库：依赖滚动检测识别该省份全部商品，仓库信息来自 OCR「仓库信息」列。"""
        import time, threading, queue
        result_queue = queue.Queue()  # 后台线程 → 主线程数据通道
        try:
            import pyautogui, pyperclip
            from vision import locate_element
            from PIL import Image as PILImage
        except ImportError as e:
            # 顶层依赖缺失：立即通知主线程收尾，避免用户白等 30 秒超时
            self.win.after(0, lambda: self.status_text.set(f"❌ 依赖缺失: {e}"))
            self.win.after(0, self.win.deiconify)
            result_queue.put(None)
            self.win.after(0, lambda: self._finish_batch(0, len(regions), 0))
            return
        
        def dlog(msg):
            """批量识别过程日志：HUD 弹窗 + 状态栏 + ocr_dlog.txt 三路输出（线程安全，after 调度）"""
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
        
        self.win.after(0, self.win.iconify); time.sleep(1.5)
        self._batch_stop.clear()
        # 任务列表：每个省份一个任务（不填仓库，滚动加载识别全部商品，仓库信息来自 OCR 仓库列）
        tasks = list(regions)
        total = len(tasks); success = 0; total_items = 0
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
                        # 模板匹配坐标是窗口截图坐标（宽≤2560），还原到全屏：
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
                        # v1.3 起完全依赖 AI 定位/模板匹配，无预设坐标兜底：
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
                        _bx, _by = _dx2, _dy2          # 重试：AI 重定位点击坐标（全屏）
                    elif dx is not None:
                        _bx, _by = dx, dy              # 首次：本次点击粘贴位置（全屏）
                    _region = None
                    if _bx is not None and _by is not None:
                        _wl = win_pos.get('left', 0) or 0
                        _wt = win_pos.get('top', 0) or 0
                        _sx = win_pos.get('scale_x', 1.0) or 1.0
                        _sy = win_pos.get('scale_y', 1.0) or 1.0
                        _region = (int((_bx - _wl) / _sx), int((_by - _wt) / _sy),
                                   max(160, int(360 / _sx)), max(80, int(100 / _sy)))
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
                        ss(_w0)
                        _changed = False
                        for _t in range(10):
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
                        # 页面没变化 = 查询可能没点中（坐标偏移/弹窗遮挡）：
                        # 重定位 query 按钮 → 重点 → 再等一轮（v1.4 修复：
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
                    dlog(f"5.页面刷新完成{'（变化检测）' if _changed else '（超时兜底）'}")
                finally:
                    for _p in (_w0, _w1):
                        try: os.remove(_p)
                        except Exception: pass

                # 6. 截图 → AI 定位表格（bbox + has_more）→ OCR → 滚动循环
                table_bbox = None
                scroll_round = 0
                _total_hint = None        # 页面统计总条数（首轮 AI 定位顺带读取，结束后对比识别量）
                seen_sku = {}            # 已见 sku_id → name（权威去重：滚动重识别/名字波动/ID错位都拦）
                seen_name_no_sku = set()    # 无 ID 商品登记过的 name
                seen_name_with_id = set()   # 有 ID 商品登记过的 name
                _fps = []               # 滚动内容指纹（每轮 stock 集合，滚动到底后稳定）
                round_items = []        # 该组合全部轮次的识别结果
                while scroll_round < MAX_SCROLL_ROUNDS:
                    if self._batch_stop.is_set(): break
                    sp2 = os.path.join(get_base_dir(), 'output', f'_result_{i}_{scroll_round}.png')
                    ss(sp2)
                    try:
                        im = PILImage.open(sp2); w, h = im.size
                        if w > 2560: im = im.resize((2560, int(h*2560/w)), PILImage.LANCZOS); im.save(sp2)
                    except Exception as _e:
                        dlog(f"  截图压缩失败(继续): {_e}")
                    # 首轮：AI 定位表格；后续轮复用 bbox，但每 3 轮重新定位一次
                    # （滚动加载可能改变表格容器高度，且需刷新 has_more 状态）
                    ai_has_more = None  # None=AI定位失败未知, True=还有更多, False=已到底
                    _row_bboxes = None   # v1.4：表格行级边界（供首轮行切分识别）
                    if scroll_round == 0 or table_bbox is None or scroll_round % 3 == 0:
                        from vision import ai_locate_table, ai_read_total_count
                        loc = ai_locate_table(sp2)
                        if loc:
                            table_bbox = loc.get('table')
                            ai_has_more = bool(loc.get('has_more', False))
                            _total_hint = loc.get('total_count')
                            _row_bboxes = loc.get('rows')
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
                    for retry in range(3):
                        try:
                            # v1.3 通用列：走 _ocr_generic_to_items（勾选列 + 映射 + 可选双模型）
                            # v1.4 修复：AI 行边界对 3 行小表格会漏最后一行（行切分只按边界
                            # 切 → 静默丢行）。有页面总数时校验：行边界数据行数 < 总数 →
                            # 回退整表识别（整表模型自己数行，与实时截图同路径，可靠）
                            _use_split = scroll_round == 0 and bool(_row_bboxes)
                            if _use_split and _total_hint:
                                try:
                                    # 行边界可能含/不含表头行：首边界 top 与表格 bbox top
                                    # 间隙 < 0.5 行高 → 视为含表头（v1.4 修正：不减表头
                                    # 会把"仅内容行边界"误判漏行，误回退整表）
                                    _bd0 = _row_bboxes[0][0]
                                    _avg_h = max(1, _row_bboxes[1][0] - _bd0)
                                    _has_hdr = (table_bbox or {}).get('top', 0) is not None \
                                        and (_bd0 - int(table_bbox.get('top', 0))) < _avg_h * 0.5
                                    _bd_rows = max(0, len(_row_bboxes) - (1 if _has_hdr else 0))
                                    if _bd_rows < int(_total_hint):
                                        dlog(f"6.⚠ 行边界{_bd_rows}数据行 < 页面总数{int(_total_hint)}，改整表识别防漏行")
                                        _use_split = False
                                except (TypeError, ValueError, IndexError):
                                    pass
                            items = self._ocr_generic_to_items(sp2, table_bbox=table_bbox,
                                                              dual_verify=dual_verify,
                                                              # 首轮全量数据走行切分防乱编；滚动轮次行少回整表控成本
                                                              row_bboxes=_row_bboxes if _use_split else None)
                            if items: break
                            dlog(f"  重试{retry+1}...")
                            time.sleep(2)
                        except Exception as ex:
                            dlog(f"  OCR异常: {ex}")
                            time.sleep(2)
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
                    else:
                        dlog("6.无数据")
                    # 内容指纹：本轮识别商品的仓库总库存值集合（模型可能乱编名字/ID，
                    # 但总库存列相对稳定；滚动到底后集合不再变化 → 提前结束，防无限空转）
                    _fp = tuple(sorted(str(it.get('stock', '')) for it in (items or []) if it.get('stock') is not None))
                    _fps.append(_fp)
                    # 滚动决策：
                    # - 首轮：AI has_more=True → 滚；AI 定位失败(未知)且本轮有商品 → 滚一次确认；AI 明确 False → 不滚
                    # - 后续轮：本轮有新商品 → 继续滚；连续无新增 → 结束（保险）
                    if scroll_round == 0:
                        if ai_has_more is False:
                            dlog("6.✓ AI确认表格已到底，无需滚动")
                            break
                        should_scroll = bool(items) and (ai_has_more is not False)
                    else:
                        should_scroll = new_in_round > 0
                        if not should_scroll:
                            dlog(f"6.⏹ 滚动{scroll_round}轮后无新增，结束")
                            break
                        # 连续3轮页面内容无变化 → 已到底，结束（doubao 等模型每轮"新增"可能永远>0）
                        if len(_fps) >= 3 and _fps[-1] == _fps[-2] == _fps[-3]:
                            dlog("6.⏹ 连续3轮页面内容无变化，结束滚动")
                            break
                        # 周期性重新定位后 AI 明确到底 → 提前结束（防跳屏漏商品后空转）
                        if ai_has_more is False and scroll_round % 3 == 0:
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
                                cy = int((((table_bbox['top'] + table_bbox['bottom']) * 0.7) * _sy)) + _wt
                            else:
                                cx = sw // 2
                                cy = int(sh * 0.6)
                            pyautogui.moveTo(cx, cy); time.sleep(0.3)
                            pyautogui.scroll(-2)
                        else:
                            pyautogui.moveTo(sw // 2, int(sh * 0.6)); time.sleep(0.3)
                            pyautogui.scroll(-2)
                        time.sleep(1.5)  # 等滚动加载渲染
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
        # 启动主线程轮询：切回主线程再调用，避免子线程直接操作 Tkinter 控件
        self.win.after(0, lambda: self._poll_batch_queue(result_queue, success, total, total_items))
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
                            self._fill_from_ocr(all_batch)
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
            # 每轮含 60s 超时 × 3 次重试）在弱网/长表格下可能超过 3 分钟，需给足余量。
            self.win.after(100, lambda: self._finish_batch(success, total, total_items))
            return
        self.win.after(100, lambda: self._poll_batch_queue(q, success, total, total_items, idle))
    
    def _finish_batch(self, success, total, total_items):
        """批量识别收尾：恢复按钮 + 显示结果"""
        self.export_btn.configure(state='normal')
        self.status_text.set("就绪 — 批量识别完成")
        if success > 0:
            messagebox.showinfo("批量识别完成", f"成功 {success}/{total} 地区\n合计 {total_items} 商品")
        else:
            messagebox.showwarning("批量识别失败",
                                   "未成功识别任何地区\n\n请检查：\n1. 网络是否正常\n2. API Key / 模型配置\n3. PDD 页面是否在前台显示")
    
    def _live_screenshot(self):
        """即时截图：最小化窗口 → 立刻截全屏 → OCR → 恢复（最小化最多 2 秒）"""
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
        
        def task():
            try:
                self.win.after(0, self.win.iconify)
                time.sleep(0.5)
                
                ss_path = os.path.join(get_base_dir(), 'output', '_live_screenshot.png')
                os.makedirs(os.path.dirname(ss_path), exist_ok=True)
                
                # 与批量识别完全一致的截图逻辑
                from utils import capture_pdd_screenshot
                found_window = capture_pdd_screenshot(ss_path)
                
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
                    self.win.after(0, lambda: (
                        self.status_text.set('❌ 未找到浏览器窗口，请先打开 PDD 后台页面'),
                        messagebox.showwarning('截图失败', '未找到拼多多或浏览器窗口。\n请先打开 PDD 商家后台 -> 订货管理页面。')))
                    return
                
                self.win.after(0, lambda: self.status_text.set('OCR识别中...'))
                
                items = self._ocr_generic_to_items(ss_path, dual_verify=_dual_mode_cache)
                
                if not items:
                    self.win.after(0, lambda: self.status_text.set('未识别到商品'))
                    return
                
                self.win.after(0, lambda i=items: self._fill_from_ocr(i))
            except Exception as e:
                self.win.after(0, lambda err=str(e): self.status_text.set(f'识别失败: {err[:50]}'))
            # 窗口恢复由主线程 _auto_restore 负责（2 秒后无条件恢复），子线程不再干预
        
        import threading
        threading.Thread(target=task, daemon=True).start()
    
    def _ocr_fill(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="选择PDD后台截图",
            filetypes=[("图片文件", "*.jpg *.jpeg *.png"), ("所有", "*.*")])
        if not path:
            return
        
        self.status_text.set("识别中...")
        self.win.update()
        # 子线程启动前缓存 Tk 变量（子线程读 Tcl 变量未定义行为）
        _dual_mode_cache = self._single_dual_var.get()
        
        def task():
            try:
                items = self._ocr_generic_to_items(path, dual_verify=_dual_mode_cache)
                self.win.after(0, lambda i=items: self._fill_from_ocr(i))
            except Exception as e:
                self.win.after(0, self._show_error, str(e))
        
        import threading
        threading.Thread(target=task, daemon=True).start()
    
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
                from ocr import _ocr_dlog
                _ocr_dlog(f"⚠ 主副模型相同（{_main}），双模型验证无意义，请更换副模型")
                self.status_text.set(f"⚠ 主副模型相同（{_main}），双模型验证无意义，请更换副模型")
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
        return parse_items_generic(rows, cfg.get('mapping') or {})

    def _fill_from_ocr(self, items):
        """用OCR结果填充表格"""
        self._clear_error()  # 先重置状态，再设置识别进度提示（避免被覆盖）
        self.status_text.set(f"OCR识别到 {len(items)} 项，计算中...")
        self.win.update()
        
        if not items:
            self.status_text.set("OCR未识别到任何数据")
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
        for i, item in enumerate(_display):
            r = self.rows[i]
            # 双模型验证标记的低置信度商品：名称加 ⚠ 提示复核
            low_conf = item.get('_low_confidence', False)
            if item.get('_name_unmatched'):
                name_unmatched_count += 1
            if item.get('_dual_degraded'):
                dual_degraded = True
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
        if low_conf_count:
            self.status_text.set(f"⚠ {low_conf_count} 个商品双模型结果不一致，已取保守值，请重点核对")
        elif name_unmatched_count:
            self.status_text.set(f"⚠ {name_unmatched_count} 个商品双模型识别名称不一致，已标记请复核")
        elif dual_degraded:
            self.status_text.set("⚠ 双模型副模型识别失败，本次为单模型结果，请留意准确性")
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
            path = export_cache_to_xlsx(self.cache, export_dir)
            self.status_text.set(f"已导出 {len(self.cache)} 个地区 → PDD补货记录.xlsx")
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
