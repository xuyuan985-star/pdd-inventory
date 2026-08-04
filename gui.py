"""
PDD EZ — 补货排期助手
客户看后台页面，输入库存和预估销量，自动算补货时间
"""

import os, sys, threading
from datetime import datetime

from utils import get_base_dir, get_api_config, VERSION, version_newer
from settings_ui import SettingsUIMixin

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
    from tkinter import messagebox, ttk
except ImportError:
    print("tkinter 未安装（Python 自带），请检查 Python 安装")
    sys.exit(1)


from config import THEMES, load_theme_pref


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


class App(SettingsUIMixin):
    # Design system — New Minimalism / Flat Design
    C_PRIMARY = '#1E293B'      # Slate 800
    C_SECONDARY = '#64748B'    # Slate 500
    C_ACCENT = '#2563EB'       # Blue 600 — only accent
    C_BG = '#FFFFFF'           # Pure white
    C_SURFACE = '#F8FAFC'      # Slate 50
    C_TEXT = '#0F172A'         # Slate 900
    C_MUTED = '#94A3B8'        # Slate 400
    C_BORDER = '#E2E8F0'       # Slate 200
    C_RED = '#DC2626'
    C_YELLOW_BG = '#FEF9C3'
    C_GREEN_BG = '#DCFCE7'
    C_RED_BG = '#FEE2E2'
    C_BLUE_LIGHT = '#EFF6FF'
    FONT = ('Microsoft YaHei UI', 9)
    FONT_BOLD = ('Microsoft YaHei UI', 9, 'bold')
    FONT_TITLE = ('Microsoft YaHei UI', 14, 'bold')
    FONT_HEADING = ('Microsoft YaHei UI', 11, 'bold')
    
    def __init__(self):
        # 任务栏图标：必须在 Tk() 之前设置，否则源码运行时显示 python 图标
        if sys.platform == 'win32':
            import ctypes
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PDD.EZ")
            except Exception:
                pass
        self.win = tk.Tk()
        self.win.title("PDD EZ")
        self.win.geometry("900x620")
        self.win.resizable(True, True)
        self.win.minsize(750, 520)
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
        # 首次启动清理旧版本 EXE
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
        self._apply_theme(self._theme_name)
        self.rows = []
        self.plans = []  # 初始化，供 _export 防御性检查
        self._filter_warning_only = False  # 结果表"仅显示预警"筛选
        self._wh_filter = '全部仓库'       # 结果表"仓库筛选"（来自 OCR 仓库信息列）
        self._suppress_auto_append = False  # 清空输入时临时禁用自动加行
        self._batch_stop = threading.Event()  # 紧急停止信号
        self.status_text = tk.StringVar(self.win, value="就绪 — 输入库存和预估销量后点计算")
        self.regions = self._load_regions()
        first = list(self.regions.keys())[0] if self.regions else '（首次使用，截图后自动识别）'
        self.region_var = tk.StringVar(self.win, value=first)
        
        # 多地区缓存
        self.cache = {}  # {region: {'plans': [...], 'items': [...]}}
        self.active_region = None
        
        self._build_ui()
        self._check_update()  # 后台检查更新
        
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
        """从 GitHub API 获取最新 release 的 tag 和 body"""
        from urllib.request import urlopen, Request
        import json as _json
        req = Request("https://api.github.com/repos/xuyuan985-star/pdd-inventory/releases/latest",
                     headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "PDD-EZ"})
        with urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
            return data.get("tag_name", ""), data.get("body", "")
    
    def _do_check_update(self):
        try:
            latest, body = self._fetch_latest_release()
            if latest and version_newer(latest, VERSION):
                self._latest_tag = latest
                self._latest_body = body
                msg = f"🔄 有新版本 {latest}，点击「更新」查看详情"
                self.win.after(0, lambda: self.status_text.set(msg))
        except Exception:
            pass
    
    def _build_ui(self):
        # ── 全局热键 ──
        self.win.bind('<F9>', lambda e: self._emergency_stop())
        
        # ── 顶部 ──
        top_bar = tk.Frame(self.win)
        top_bar.pack(fill="x", padx=15, pady=(15, 2))
        # ☰ 导航按钮（左侧）
        tk.Button(top_bar, text="☰ 导航", relief='flat', command=self._toggle_nav,
                  font=(self.FONT[0], 9), bg=self.C_BLUE_LIGHT, fg=self.C_PRIMARY).pack(side="left")
        # 当前模型标签
        api_cfg = get_api_config()
        active = api_cfg.get('active_provider', 'doubao')
        providers = api_cfg.get('providers', {})
        provider = providers.get(active, {}) if isinstance(providers, dict) else {}
        bm = provider.get('model', '') or active
        is_free = active == 'glm'
        # 模型标识胶囊
        self.pill_frame = tk.Frame(top_bar, bg=self.C_SURFACE)
        self.pill_frame.pack(side="left", padx=12)
        self.pill_frame._skip_theme = True
        self.pill_name = tk.Label(self.pill_frame, text=bm, font=(self.FONT[0], 8, 'bold'),
                                   fg=self.C_TEXT, bg=self.C_SURFACE)
        self.pill_name.pack(side="left", padx=(10,4), pady=4)
        self.pill_name._skip_theme = True
        tag_bg = "#10B981" if is_free else "#8B5CF6"
        tag_text = "FREE" if is_free else "PRO"
        self.pill_tag = tk.Label(self.pill_frame, text=tag_text, font=(self.FONT[0], 7, 'bold'),
                                  fg="#FFFFFF", bg=tag_bg, padx=6)
        self.pill_tag.pack(side="left", padx=(0,8), pady=2)
        self.pill_tag._skip_theme = True
        tk.Button(top_bar, text="🏪 商家后台", relief='flat', command=self._open_backend,
                  font=(self.FONT[0], 9), bg=self.C_PRIMARY, fg="#FFFFFF").pack(side="right", padx=5)
        tk.Button(top_bar, text="🔄 更新", relief='flat', command=self._run_updater,
                  font=(self.FONT[0], 9), bg="#10B981", fg="#FFFFFF").pack(side="right", padx=5)
        
        # ── 主容器：左导航 + 右内容（可拖拽分割） ──
        self.main_paned = tk.PanedWindow(self.win, orient="horizontal", sashwidth=3, bg=self.C_BORDER)
        self.main_paned.pack(fill="both", expand=True, padx=15, pady=(2, 15))
        # 左侧导航栏
        self.nav_frame = tk.Frame(self.main_paned, width=170, bg=self.C_SURFACE)
        self.nav_frame._skip_theme = True  # 导航栏保持 C_SURFACE 区分色，主题切换不覆盖
        self.nav_frame.pack_propagate(False)
        self.nav_buttons = {}
        # 右侧内容
        self.content_frame = tk.Frame(self.main_paned)
        self.main_paned.add(self.content_frame, stretch="always")
        # 页面帧
        self.page_home = tk.Frame(self.content_frame)
        self.page_general = tk.Frame(self.content_frame)
        self.page_products = tk.Frame(self.content_frame)
        self.page_theme = tk.Frame(self.content_frame)
        self.page_backend = tk.Frame(self.content_frame)
        self.page_api = tk.Frame(self.content_frame)
        self._current_page = self.page_home
        
        # ── 输入表格 ──
        table_frame = tk.Frame(self.page_home, bg=self.C_SURFACE, highlightthickness=1,
                               highlightbackground=self.C_BORDER, highlightcolor=self.C_BORDER)
        table_frame.pack(fill="x", padx=15, pady=5)
        
        # 标题头
        hdr_bg = tk.Frame(table_frame, bg=self.C_PRIMARY, height=32)
        hdr_bg.pack(fill="x")
        hdr_bg.pack_propagate(False)
        tk.Label(hdr_bg, text="输入数据  —  照着 PDD 后台页面填写",
                 font=self.FONT, fg='#FFFFFF', bg=self.C_PRIMARY).pack(side="left", padx=12, pady=4)
        
        # 列头
        col_hdr = tk.Frame(table_frame, bg=self.C_BLUE_LIGHT)
        col_hdr.pack(fill="x")
        col_hdr.grid_columnconfigure(0, weight=1)
        col_hdr.grid_columnconfigure(1, minsize=80)
        col_hdr.grid_columnconfigure(2, minsize=80)
        tk.Label(col_hdr, text="商品名称", font=self.FONT_BOLD, bg=self.C_BLUE_LIGHT,
                 fg=self.C_TEXT, anchor="w").grid(row=0, column=0, sticky="w", padx=10, pady=4)
        tk.Label(col_hdr, text="总库存", font=self.FONT_BOLD, bg=self.C_BLUE_LIGHT,
                 fg=self.C_TEXT).grid(row=0, column=1, padx=4, pady=4)
        tk.Label(col_hdr, text="总销量", font=self.FONT_BOLD, bg=self.C_BLUE_LIGHT,
                 fg=self.C_TEXT).grid(row=0, column=2, padx=4, pady=4)
        
        # 数据行容器
        self.table_area = tk.Frame(table_frame)
        self.table_area.pack(fill="x")
        self.table_area.grid_columnconfigure(0, weight=1)
        self.table_area.grid_columnconfigure(1, minsize=80)
        self.table_area.grid_columnconfigure(2, minsize=80)
        
        # 初始 3 行
        for _ in range(3):
            self._add_row()
        
        # 按钮行
        btn_row = tk.Frame(table_frame)
        btn_row.pack(fill="x", padx=10, pady=5)
        tk.Button(btn_row, text="+ 加行", relief='flat', command=self._add_row,
                  font=(self.FONT[0], 8)).pack(side="left")
        tk.Button(btn_row, text="- 删行", relief='flat', command=self._del_row,
                  font=(self.FONT[0], 8)).pack(side="left", padx=5)
        tk.Button(btn_row, text="🔄 刷新计算", relief='flat', command=self._recalc_from_rows,
                  font=(self.FONT[0], 9, 'bold'), bg=self.C_SECONDARY, fg="#FFFFFF").pack(side="left", padx=15)
        tk.Button(btn_row, text="📋 批量识别", relief='flat', command=self._batch_scan,
                  font=(self.FONT[0], 8), bg="#8B5CF6", fg="#FFFFFF").pack(side="left", padx=8)
        # 单次识别双模型开关（v1.3：不在乎 token 成本，默认开，识别更准）
        self._single_dual_var = tk.BooleanVar(self.win, value=True)
        tk.Checkbutton(btn_row, text="🛡 双模型", variable=self._single_dual_var,
                       font=(self.FONT[0], 8), bg=self.C_SURFACE, fg=self.C_MUTED,
                       selectcolor=self.C_SURFACE, activebackground=self.C_SURFACE).pack(side="left", padx=10)
        tk.Button(btn_row, text="截图识别", relief='flat', command=self._ocr_fill,
                  font=(self.FONT[0], 8), bg="#FF9800", fg="#FFFFFF").pack(side="right")
        tk.Button(btn_row, text="实时截图", relief='flat', command=self._live_screenshot,
                  font=(self.FONT[0], 8), bg="#4CAF50", fg="#FFFFFF").pack(side="right", padx=5)
        
        # ── 当前地区（识别后自动显示）──
        region_frame = tk.Frame(self.page_home)
        region_frame.pack(pady=10)
        tk.Label(region_frame, text="当前地区:", font=self.FONT, fg=self.C_MUTED).pack(side="left")
        tk.Label(region_frame, textvariable=self.region_var,
                 font=self.FONT_BOLD, fg=self.C_PRIMARY).pack(side="left", padx=5)
        
        # ── 导出按钮 ──
        self.export_btn = tk.Button(self.page_home, text="导出 Excel",
                  font=self.FONT_HEADING, bg="#4CAF50", fg="#FFFFFF",
                  width=20, height=2, relief='flat', highlightthickness=0,
                  command=self._export, state="normal")
        self.export_btn.pack(pady=10)
        
        # ── 状态栏 ──
        tk.Label(self.page_home, textvariable=self.status_text,
                 font=(self.FONT[0], 8), fg="#64748B").pack(pady=(8,3))
        
        # ── 结果表 ──
        self.result_frame = tk.Frame(self.page_home, bg=self.C_SURFACE, highlightthickness=1,
                                highlightbackground=self.C_BORDER)
        self.result_frame.pack(fill="both", expand=True, padx=15, pady=(5,15))
        
        tk.Label(self.result_frame, text="计算结果", font=self.FONT_BOLD, bg=self.C_PRIMARY,
                 fg='#FFFFFF').pack(fill="x", pady=(0,0))
        
        # 地区切换标签
        self.tab_frame = tk.Frame(self.result_frame)
        self.tab_frame.pack(fill="x", padx=3, pady=(5,2))
        
        # 初始占位
        tk.Label(self.tab_frame, text="截图识别后此处显示地区标签",
                 font=(self.FONT[0], 8), fg=self.C_MUTED).pack(side="left")
        
        columns = ("商品", "库存", "预估销量", "可售卖天数", "状态", "补货量")
        # 结果表放入带滚动条的容器（勾选列多时右侧列不再被截断）
        tree_frame = tk.Frame(self.result_frame)
        tree_frame.pack(fill="both", expand=True, padx=3, pady=3)
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=15)
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        
        # 仅显示预警筛选
        filter_frame = tk.Frame(self.result_frame)
        filter_frame.pack(fill="x", padx=3, pady=(0,3))
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
        
        for col, w in zip(columns, [260, 110, 110, 110, 100, 90]):
            self.tree.heading(col, text=col, command=lambda c=col: self._sort_tree(c))
            self.tree.column(col, width=w, anchor="center")
        
        self.tree.tag_configure('urgent', background=self.C_RED_BG)
        self.tree.tag_configure('warning', background=self.C_YELLOW_BG)
        
        # 排序状态
        self._sort_col = None
        self._sort_reverse = False
        
        # Treeview 行高加大，避免计算结果条目上下拥挤
        style = ttk.Style()
        style.configure("Treeview", rowheight=28)
        
        self._apply_theme(self._theme_name)
        self._refresh_model_badge()
        self.page_home.pack(fill="both", expand=True)
        


    def _refresh_model_badge(self):
        api_cfg = get_api_config()
        active = api_cfg.get('active_provider', 'doubao')
        providers = api_cfg.get('providers', {})
        provider = providers.get(active, {}) if isinstance(providers, dict) else {}
        model_name = provider.get('model', '') or active
        is_free = active == 'glm'
        self.pill_frame.configure(bg=self.C_SURFACE)
        self.pill_name.configure(text=model_name, bg=self.C_SURFACE, fg=self.C_TEXT)
        tag_bg = "#10B981" if is_free else "#8B5CF6"
        tag_text = "FREE" if is_free else "PRO"
        self.pill_tag.configure(text=tag_text, bg=tag_bg, fg="#FFFFFF")

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
            btn = tk.Button(self.nav_frame, text=text, relief="flat",
                           font=(self.FONT[0], 9), anchor="w", padx=12, pady=6,
                           bg=self.C_SURFACE, fg=self.C_TEXT, activebackground=self.C_BLUE_LIGHT,
                           command=lambda p=page: self._show_page(p))
            btn._page = page
            btn.pack(fill="x")
            self.nav_buttons[text] = btn
        self._highlight_nav(self.page_home)

    def _highlight_nav(self, page):
        for btn in self.nav_buttons.values():
            if getattr(btn, '_page', None) == page:
                btn.configure(bg=self.C_PRIMARY, fg="#FFFFFF")
            else:
                btn.configure(bg=self.C_SURFACE, fg=self.C_TEXT)

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
        self.status_text.set("就绪 — 输入库存和预估销量后点计算")
    
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
            x = (self.win.winfo_screenwidth() - current_w) // 2
            y = max(0, (screen_h - target_h) // 3)
            self.win.geometry(f"{current_w}x{target_h}+{x}+{y}")
            self.win.update()  # 立即生效
    
    def _add_row(self):
        row = {}
        f = tk.Frame(self.table_area)
        f.pack(fill="x", pady=1)
        f.grid_columnconfigure(0, weight=1)
        f.grid_columnconfigure(1, minsize=80)
        f.grid_columnconfigure(2, minsize=80)
        
        row['name'] = tk.StringVar(self.win)
        row['stock'] = tk.StringVar(self.win)
        row['sales'] = tk.StringVar(self.win)
        
        # 主题感知的 Entry 样式
        e_kwargs = dict(font=self.FONT, relief="flat", highlightthickness=0,
                        bg=self.C_SURFACE, fg=self.C_TEXT, insertbackground=self.C_TEXT,
                        selectbackground=self.C_SECONDARY, selectforeground='#FFFFFF')
        
        # 数字输入校验：只允许数字（含空串），非法输入即时标红
        _bad_bg = '#FEE2E2'  # 浅红底提示非法

        def _make_num_entry(var, col):
            entry = tk.Entry(f, textvariable=var, width=10, justify="center", **e_kwargs)
            entry.grid(row=0, column=col, padx=4, pady=2)
            # 输入时校验：非法字符拒绝并标红，合法恢复
            def _on_key(p):
                ok = _validate_num_entry(p)
                try:
                    entry.configure(bg=self.C_SURFACE if ok else _bad_bg)
                except Exception:
                    pass
                return True
            entry.configure(validate='key', validatecommand=(self.win.register(_on_key), '%P'))
            return entry

        tk.Entry(f, textvariable=row['name'], **e_kwargs).grid(row=0, column=0, sticky="ew", padx=10, pady=2)
        _make_num_entry(row['stock'], 1)
        _make_num_entry(row['sales'], 2)
        
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
        # 兼容旧格式：值如果是数字，转为空 dict（运输天数走默认 3）
        result = {}
        for region, val in data.items():
            if isinstance(val, (int, float)):
                result[region] = {}
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
        """显示更新详情 + 进度 + 错误处理"""
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
        dlg.geometry("450x300")
        dlg.resizable(False, False)
        dlg.configure(bg=self.C_BG)
        dlg.transient(self.win)
        dlg.grab_set()
        
        tk.Label(dlg, text=f"发现新版本 {latest}", font=self.FONT_HEADING,
                bg=self.C_BG, fg=self.C_TEXT).pack(pady=(15,5))
        
        # 更新日志
        log_frame = tk.Frame(dlg, bg=self.C_SURFACE, highlightthickness=1, highlightbackground=self.C_BORDER)
        log_frame.pack(fill="both", expand=True, padx=15, pady=5)
        log_text = tk.Text(log_frame, font=(self.FONT[0], 8), wrap="word", height=8,
                          bg=self.C_SURFACE, fg=self.C_TEXT, relief="flat")
        log_text.insert("1.0", changelog)
        log_text.configure(state="disabled")
        log_text.pack(fill="both", expand=True, padx=5, pady=5)
        
        # 进度条
        progress = ttk.Progressbar(dlg, mode="indeterminate", length=380)
        progress.pack(pady=8)
        status_lbl = tk.Label(dlg, text="", font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_MUTED)
        status_lbl.pack()
        
        def do_update():
            progress.start(10)
            status_lbl.configure(text="正在下载...")
            dlg.update()
            
            import subprocess, tempfile
            updater = os.path.join(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else get_base_dir(),
                                  'PDD EZ Updater.exe')
            if not os.path.exists(updater):
                progress.stop()
                messagebox.showerror("更新失败", "未找到更新器，请重新下载完整安装包", parent=dlg)
                dlg.destroy()
                return
            
            try:
                # 兼容旧版更新器：先探测是否支持 --pid（v1.0 更新器不认识该参数会直接报错退出）
                _args = [updater, '--help']
                _probe = subprocess.run(_args, capture_output=True, timeout=5)
                _help_out = (_probe.stdout + _probe.stderr).decode('utf-8', errors='replace')
                _cmd = [updater, '--target', sys.executable, '--restart']
                if '--pid' in _help_out:
                    _cmd += ['--pid', str(os.getpid())]
                subprocess.Popen(_cmd)
                progress.stop()
                status_lbl.configure(text="更新器已启动，主程序即将关闭...")
                self.win.destroy()
            except Exception as e:
                progress.stop()
                messagebox.showerror("启动失败", f"无法启动更新器：{e}\n请手动下载最新版本", parent=dlg)
                dlg.destroy()
        
        btn_frame = tk.Frame(dlg, bg=self.C_BG)
        btn_frame.pack(pady=10)
        tk.Button(btn_frame, text="立即更新", command=do_update,
                 font=self.FONT_BOLD, bg="#10B981", fg="#FFFFFF", width=12).pack(side="left", padx=5)
        tk.Button(btn_frame, text="稍后再说", command=dlg.destroy,
                 font=self.FONT, bg=self.C_MUTED, fg="#FFFFFF", width=12).pack(side="left", padx=5)
    
    def _apply_theme(self, name):
        """应用皮肤：更新类属性 + 递归刷新所有控件颜色"""
        theme = THEMES.get(name, THEMES['极简白'])
        self._theme_name = name
        
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
        
        # 同步刷新设置窗口（如果打开着）
        if hasattr(self, '_settings_dlg') and self._settings_dlg and self._settings_dlg.winfo_exists():
            _walk_color(self._settings_dlg)
            _walk_system(self._settings_dlg)
            _walk_force(self._settings_dlg, theme['C_BG'])
            self._settings_dlg.configure(bg=theme['C_BG'])
        
        # ── ttk 皮肤 ──
        if hasattr(self, 'tree'):
            self._update_ttk_theme(theme)
            self._refresh_tree_tags()
    
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
            fieldbackground=theme['C_SURFACE'],
            troughcolor=theme['C_BG'],
            bordercolor=theme['C_BORDER'],
            lightcolor=theme['C_BG'],
            darkcolor=theme['C_MUTED'],
            arrowcolor=theme['C_TEXT'])
        
        # Treeview
        style.configure('Treeview',
            background=theme['C_SURFACE'],
            foreground=theme['C_TEXT'],
            fieldbackground=theme['C_SURFACE'])
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
        
        # Combobox
        style.configure('TCombobox',
            fieldbackground=theme['C_SURFACE'],
            background=theme['C_SURFACE'],
            foreground=theme['C_TEXT'],
            arrowcolor=theme['C_TEXT'])
        style.map('TCombobox',
            fieldbackground=[('readonly', theme['C_SURFACE'])],
            foreground=[('readonly', theme['C_TEXT'])],
            background=[('readonly', theme['C_SURFACE'])])
        
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
        
        # Scrollbar
        style.configure('Vertical.TScrollbar',
            background=theme['C_MUTED'],
            troughcolor=theme['C_BG'],
            arrowcolor=theme['C_TEXT'])
        style.configure('Horizontal.TScrollbar',
            background=theme['C_MUTED'],
            troughcolor=theme['C_BG'],
            arrowcolor=theme['C_TEXT'])
        
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
            return region_data.get(product_name, 3)
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
            _sel_cols = []
        if not _sel_cols:
            _sel_cols = ['商品信息', '仓库总库存', '仓库预估总销售数']

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
            shipping = self._get_shipping(region, name)  # 逐商品查运输时效
            
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
                'name': name, 'sku': name, 'stock': stock,
                'daily': daily, 'ratio': round(ratio, 1),
                'days_left': round(ratio, 1),
                'status': status, 'color': color, 'qty': qty,
                'stat_date': f'{today.month}.{today.day}',
                'warehouse': item.get('warehouse', ''),
                # 通用列原始数据：客户勾选列从 _raw 取原文显示
                '_raw': item.get('_raw') or {},
                '_sel_cols': _sel_cols,
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
                # 数字/状态列加宽到 110，防"1109份"被截断成"110份"误导；名称列 260
                width = 260 if col in ('商品名称', '商品') or '名称' in col else 110
                self.tree.heading(col, text=col, command=lambda c=col: self._sort_tree(c))
                self.tree.column(col, width=width, anchor="center")
        except Exception:
            pass
        self.tree.delete(*self.tree.get_children())
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
            raw = p['_raw'] or {}
            row_vals = []
            from ocr import strip_tail_noise  # 去「查看地址/查看」词条噪音（OCR 识别不稳定，展示层统一清）
            for col in _sel_cols:
                v = raw.get(col)
                if v is None or v == '':
                    v = p.get(col, '')
                if isinstance(v, str):
                    v = strip_tail_noise(v)
                row_vals.append(v)
            row_vals += [p['ratio'], p['status'], p['qty']]
            self.tree.insert("", "end", values=tuple(row_vals), tags=tags)
    
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
            bg = "#4CAF50" if is_active else self.C_BLUE_LIGHT
            fg = "white" if is_active else self.C_TEXT
            btn = tk.Button(self.tab_frame, text=reg, bg=bg, fg=fg,
                           font=("微软雅黑", 8, "bold" if is_active else "normal"),
                           command=lambda r=reg: self._switch_region(r))
            btn.pack(side="left", padx=2)
    
    def _switch_region(self, region):
        """切换到指定地区的缓存结果"""
        if region not in self.cache:
            return
        self.active_region = region
        data = self.cache[region]
        self.region_var.set(region)
        
        # 显示该地区的结果（v1.3 动态列 + 筛选：复用 _render_tree）
        self._render_tree(data['plans'])
        self.plans = data['plans']
        self._sort_col = None
        self._sort_reverse = False
        self._update_tabs()
        suffix = "（仅显示预警）" if self._filter_warning_only else ""
        self.status_text.set(f"已切换到 {region} — {len(data['plans'])} 个商品{suffix}")
        self._auto_expand(len(data['plans']))
    
    def _del_row(self):
        if len(self.rows) > 1:
            row = self.rows.pop()
            children = list(self.table_area.winfo_children())
            if children:
                children[-1].destroy()
    
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
            self._del_row()
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
            try:
                stock = int(stock_s) if stock_s else 0
            except ValueError:
                stock = 0
            try:
                sales = int(sales_s) if sales_s else 0
            except ValueError:
                sales = 0
            items.append({'name': name, 'stock': stock, 'sales': sales,
                         'region': self.region_var.get(),
                         # 手动输入路径也要带 _raw，否则动态列渲染空白（与 OCR 路径一致）
                         '_raw': self._build_raw_from_fields(name, stock, sales,
                                                             region=self.region_var.get())})
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
        dlg.geometry("400x500")
        dlg.minsize(380, 350)
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

        # 选项横排（测试模式 + 双模型），避免纵向堆叠把按钮挤出可视区
        opt_row = tk.Frame(bottom_frame, bg=self.C_BG)
        opt_row.pack(pady=(5,0))
        test_var = tk.BooleanVar(dlg, value=False)
        tk.Checkbutton(opt_row, text="🔍 测试模式",
                      variable=test_var, font=(self.FONT[0], 8),
                      bg=self.C_BG, fg=self.C_MUTED,
                      selectcolor=self.C_BG, activebackground=self.C_BG).pack(side="left", padx=10)
        dual_var = tk.BooleanVar(dlg, value=True)  # 默认开双模型（v1.3：不在乎 token 成本，识别更准）
        tk.Checkbutton(opt_row, text="🛡 双模型验证（慢一倍，更准）",
                      variable=dual_var, font=(self.FONT[0], 8),
                      bg=self.C_BG, fg=self.C_MUTED,
                      selectcolor=self.C_BG, activebackground=self.C_BG).pack(side="left", padx=10)
        
        # 地区勾选列表（可滚动，占剩余空间）
        canvas = tk.Canvas(dlg, bg=self.C_SURFACE, highlightthickness=0)
        scrollbar = tk.Scrollbar(dlg, orient="vertical", command=canvas.yview)
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
            debug_mode = test_var.get()
            # 测试模式：主线程创建 HUD
            hud = None; hud_text = None
            if debug_mode:
                hud = tk.Toplevel(self.win)
                hud.title("")
                hud.overrideredirect(True)
                hud.attributes('-topmost', True, '-alpha', 0.82)
                hud.configure(bg='#0F172A')
                sw_h, sh_h = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
                hud.geometry(f"400x250+{sw_h-420}+30")
                hud_text = tk.Text(hud, font=('Consolas', 9), bg='#0F172A', fg='#22D3EE',
                                  wrap='word', relief='flat', borderwidth=0, padx=10, pady=10)
                hud_text.pack(fill='both', expand=True)
                hud_text.insert('end', '🔍 测试模式启动\n')
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
                    self._run_batch_sequence(selected, hud, hud_text, _dual_mode)
                except Exception as _e:
                    import traceback
                    try:
                        with open(os.path.join(get_base_dir(), 'output', 'ocr_dlog.txt'),
                                  'a', encoding='utf-8') as _f:
                            _f.write('[batch] 线程异常: ' + traceback.format_exc() + '\n')
                    except Exception:
                        pass
                    self.win.after(0, lambda: self.status_text.set(f"❌ 批量识别异常: {str(_e)[:80]}"))
                    self.win.after(0, self.win.deiconify)
            threading.Thread(target=_batch_thread_wrapper, daemon=True).start()
        
        tk.Button(bottom_frame, text="开始批量识别", command=start_batch,
                  font=self.FONT_BOLD, bg=self.C_PRIMARY, fg="#FFFFFF",
                  width=18, height=2).pack(pady=(10,0))
        
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
        _cal_mode = 'ai'  # v1.4 起只保留 AI 定位（旧 absolute/offset 已废弃）
        import json as _json
        try:
            with open(os.path.join(get_base_dir(), 'settings.json'), 'r', encoding='utf-8') as _f:
                _cal = _json.load(_f).get('calibrate')
                if not isinstance(_cal, dict):
                    _cal = {}  # 畸形 calibrate 归一化，防后续 .get 崩
        except Exception: pass

        # AI 自动定位：AI 模式下，批量识别启动时实时定位按钮坐标
        if _cal_mode == 'ai':
            _ai_raw = _cal.get('ai')
            ai_data = _ai_raw if isinstance(_ai_raw, dict) else {}
            last_time = ai_data.get('last_time', 0)
            now = time.time()
            # 5 分钟内有缓存直接复用
            if not last_time or (now - last_time > 300):
                dlog("AI 自动定位页面元素...")
                try:
                    from vision import ai_locate_elements
                    result = ai_locate_elements()
                    if result:
                        _cal['ai'] = {
                            'last_time': now,
                            'dropdown': result['dropdown'],
                            'query': result['query'],
                            'confidence': result['confidence'],
                            'screen_width': result['screen_width'],
                            'screen_height': result['screen_height'],
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
                except Exception:
                    pass  # 失败静默回退，用旧坐标继续

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
            dlog(f"校准OK ({_cal_mode}): dd({dd_coord.get('x')},{dd_coord.get('y')}) q({qq_coord.get('x')},{qq_coord.get('y')})")
        else:
            dlog(f"未校准（{_cal_mode}模式），请先到设置→校准")
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
                tm_x = tm_y = None
                pos = locate_element(sp, 'region_dropdown', method='template', threshold=0.80)
                if pos:
                    tm_x, tm_y = pos[0], pos[1]
                    # 点击偏移比例制：90px 相对 1920 参考宽度，按当前分辨率缩放
                    dx = tm_x + int(90 * sw / 1920)
                    dy = tm_y
                    dlog(f"1.模板匹配({dx},{dy})")
                elif dd_coord:
                    dx, dy = dd_coord['x'], dd_coord['y']
                    dlog(f"1.校准坐标({dx},{dy})")
                else:
                    # v1.3 起完全依赖 AI 定位/模板匹配，无预设坐标兜底：
                    # 宁可显式失败让用户处理，也不猜测位置乱点
                    dlog(f"1.✗ 未定位到地区下拉框（模板匹配+AI校准均失败），跳过 {reg}")
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

                # 4. 找查询按钮
                if qq_coord:
                    qx, qy = qq_coord['x'], qq_coord['y']
                    dlog(f"4.{_cal_mode}坐标({qx},{qy})")
                else:
                    dlog("4.⚠ 未校准查询按钮，跳过"); continue
                pyautogui.click(qx, qy)
                # 5. 等待页面刷新：截图变化检测（最多 10 秒，检测到页面变化即提前继续）
                _w0 = os.path.join(get_base_dir(), 'output', f'_wait_{i}_0.png')
                _w1 = os.path.join(get_base_dir(), 'output', f'_wait_{i}_1.png')
                try:
                    ss(_w0)
                    changed = False
                    for _t in range(10):
                        time.sleep(1.0)
                        ss(_w1)
                        try:
                            im0 = PILImage.open(_w0).convert('L').resize((160, 90))
                            im1 = PILImage.open(_w1).convert('L').resize((160, 90))
                            diff = sum(1 for a, b in zip(im0.getdata(), im1.getdata()) if abs(a - b) > 12)
                            if diff > 40:  # 超过 40 个像素点差异视为页面已刷新
                                changed = True
                                break
                            _w0, _w1 = _w1, _w0  # 滚动基准
                        except Exception:
                            pass
                    dlog(f"5.页面刷新完成{'（变化检测）' if changed else '（超时兜底）'}")
                finally:
                    for _p in (_w0, _w1):
                        try: os.remove(_p)
                        except Exception: pass

                # 6. 截图 → AI 定位表格（bbox + has_more）→ OCR → 滚动循环
                table_bbox = None
                scroll_round = 0
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
                    if scroll_round == 0 or table_bbox is None or scroll_round % 3 == 0:
                        from vision import ai_locate_table
                        loc = ai_locate_table(sp2)
                        if loc:
                            table_bbox = loc.get('table')
                            ai_has_more = bool(loc.get('has_more', False))
                            if ai_has_more:
                                dlog(f"6.AI检测到还有更多商品，自动滚动加载...")
                            elif scroll_round > 0 and ai_has_more is not None:
                                dlog(f"6.AI确认滚动后已到底")
                    dlog(f"6.{'首屏' if scroll_round == 0 else f'滚动{scroll_round}'}OCR识别中({'双模型' if dual_verify else '单模型'})...")
                    items = None
                    for retry in range(3):
                        try:
                            # v1.3 通用列：走 _ocr_generic_to_items（勾选列 + 映射 + 可选双模型）
                            items = self._ocr_generic_to_items(sp2, table_bbox=table_bbox,
                                                              dual_verify=dual_verify)
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
        """即时截图：最小化窗口 → 立刻截全屏 → OCR → 恢复"""
        self.status_text.set("最小化窗口，请确认PDD页面在后面...")
        self._clear_input_rows()  # 先清旧数据
        self.win.update()
        
        def task():
            import time, os
            try:
                self.win.after(0, self.win.iconify)
                time.sleep(0.5)
                
                ss_path = os.path.join(get_base_dir(), 'output', '_live_screenshot.png')
                os.makedirs(os.path.dirname(ss_path), exist_ok=True)
                
                # 与批量识别完全一致的截图逻辑
                from utils import capture_pdd_screenshot
                found_window = capture_pdd_screenshot(ss_path)
                
                if not found_window:
                    self.win.after(0, self.win.deiconify)
                    self.win.after(0, lambda: (
                        self.status_text.set('❌ 未找到浏览器窗口，请先打开 PDD 后台页面'),
                        messagebox.showwarning('截图失败', '未找到拼多多或浏览器窗口。\n请先打开 PDD 商家后台 -> 订货管理页面。')))
                    return
                
                self.win.after(0, self.win.deiconify)
                self.win.after(0, lambda: self.status_text.set('OCR识别中...'))
                
                items = self._ocr_generic_to_items(ss_path, dual_verify=self._single_dual_var.get())
                
                if not items:
                    self.win.after(0, lambda: self.status_text.set('未识别到商品'))
                    return
                
                self.win.after(0, lambda i=items: self._fill_from_ocr(i))
            except Exception as e:
                self.win.after(0, self.win.deiconify)
                self.win.after(0, lambda err=str(e): self.status_text.set(f'识别失败: {err[:50]}'))
        
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
        
        def task():
            try:
                items = self._ocr_generic_to_items(path, dual_verify=self._single_dual_var.get())
                self.win.after(0, lambda i=items: self._fill_from_ocr(i))
            except Exception as e:
                self.win.after(0, self._show_error, str(e))
        
        import threading
        threading.Thread(target=task, daemon=True).start()
    
    def _ocr_generic_to_items(self, image_path, table_bbox=None, dual_verify=False):
        """
        通用列识别 → 业务字段 items（v1.3 主入口）。
        读取客户勾选列配置，ocr_table 指定列识别，parse_items_generic 映射为
        {name, stock, sales, region, warehouse, _raw} 供填充/计算/导出。
        未配置勾选列时回退默认商品字段列。
        dual_verify=True 时走双模型交叉验证（ocr_dual_verify_generic）。
        """
        from ocr import ocr_table, parse_items_generic
        from utils import get_ocr_columns
        cfg = get_ocr_columns()
        sel = [c for c in (cfg.get('selected') or []) if c]
        if not sel:
            sel = ['商品信息', '仓库总库存', '仓库预估总销售数']
        if dual_verify:
            from ocr import ocr_dual_verify_generic
            from utils import get_secondary_model, get_api_config
            _sec = get_secondary_model()
            _api = get_api_config()
            _act = _api.get('active_provider', '')
            _main = ((_api.get('providers') or {}).get(_act, {}) or {}).get('model', '')
            if _main and str(_main).strip().lower() == str(_sec).strip().lower():
                dlog(f"⚠ 主副模型相同（{_main}），双模型验证无意义，请更换副模型")
            return ocr_dual_verify_generic(image_path, columns=sel,
                                           mapping=cfg.get('mapping') or {},
                                           table_bbox=table_bbox,
                                           secondary_model=_sec)
        result = ocr_table(image_path, columns=sel, table_bbox=table_bbox)
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
        while len(self.rows) < len(items):
            self._add_row()
        # 填入数据
        detected_regions = set()
        low_conf_count = 0
        for i, item in enumerate(items):
            r = self.rows[i]
            # 双模型验证标记的低置信度商品：名称加 ⚠ 提示复核
            low_conf = item.get('_low_confidence', False)
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
            region = item.get('region', '')
            if region:
                from ocr import strip_region_suffix
                detected_regions.add(strip_region_suffix(region))
        if low_conf_count:
            self.status_text.set(f"⚠ {low_conf_count} 个商品双模型结果不一致，已取保守值，请重点核对")
        # 自动匹配地区
        msg = f"识别完成 — {len(items)} 个商品，请核对后点计算"
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
            msg = f"识别完成 — {len(items)} 个商品"
            if newly_added:
                msg += f"\n\n⚠ 新增地区：{'、'.join(newly_added)}，各商品运输时间默认3天"
                msg += "\n请点击「商品时效设置」按商品调整运输天数"
                self.win.after(500, lambda: messagebox.showinfo(
                    "发现新地区",
                    f"识别到新地区：{'、'.join(newly_added)}\n\n已自动添加到地区列表，各商品运输时间暂设为3天。\n请点击「商品时效设置」根据实际情况调整。",
                    parent=self.win))
        self.status_text.set(msg)
        # 直接用OCR结果计算，不依赖行数据
        # 按地区分组：多省份×多仓库批量时每个地区独立缓存（避免全混进第一个地区）
        try:
            by_region = {}
            from ocr import strip_region_suffix
            for it in items:
                reg = strip_region_suffix(it.get('region', '')) or self.region_var.get()
                by_region.setdefault(reg, []).append(it)
            for reg, sub in by_region.items():
                self.region_var.set(reg)
                self._calc_from_items(sub)
        except Exception as e:
            self._show_error(f"计算出错: {e}", popup=True)
            import traceback; traceback.print_exc()
    
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
                self.cache[self.region_var.get()] = {'plans': self.plans, 'items': []}
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
        self.win.mainloop()


if __name__ == "__main__":
    App().run()
