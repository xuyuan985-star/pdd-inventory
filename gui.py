"""
PDD EZ — 补货排期助手
客户看后台页面，输入库存和预估销量，自动算补货时间
"""

import os, sys, threading
from datetime import datetime

from utils import get_base_dir, get_api_config, VERSION
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


from config import RESOLUTION_PRESETS, THEMES, load_theme_pref, load_resolution_pref


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
            try:
                exe_dir = os.path.dirname(sys.executable)
                # 删旧 EXE
                for old in ['PDD EZ v2.2.exe', 'PDD EZ v2.1.exe', 'PDD补货助手.exe']:
                    old_path = os.path.join(exe_dir, old)
                    if os.path.exists(old_path) and old_path != sys.executable:
                        os.remove(old_path)
                # 删 _internal/ 废弃文件
                internal = os.path.join(exe_dir, "_internal")
                if os.path.isdir(internal):
                    for old in ['api_keys.py', 'dpi_utils.py', 'keys.enc', 'gui_bridge.py']:
                        old_path = os.path.join(internal, old)
                        if os.path.exists(old_path):
                            os.remove(old_path)
            except Exception:
                pass
        # 加载皮肤偏好
        self._theme_name = load_theme_pref()
        self._apply_theme(self._theme_name)
        # 记录初始几何，用于结果出来后自动展开
        self._initial_geometry = "900x620"
        
        self.rows = []
        self.plans = []  # 初始化，供 _export 防御性检查
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
            if latest and latest != VERSION:
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
        self.page_calibrate = tk.Frame(self.content_frame)
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
        self.tree = ttk.Treeview(self.result_frame, columns=columns, show="headings", height=15)
        self.tree.pack(fill="both", expand=True, padx=3, pady=3)
        
        for col, w in zip(columns, [260, 80, 80, 80, 100, 70]):
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
            ("📐 校准", self.page_calibrate),
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
        elif page == self.page_calibrate and not hasattr(page, '_built'):
            self._build_calibrate_tab(page)
        elif page == self.page_api and not hasattr(page, '_built'):
            self._build_api_page(page)
        page._built = True
        self._apply_theme(self._theme_name)
        self._refresh_model_badge()
        # (moved to _build_ui)

    
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
        ROW_HEIGHT = 20
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
        
        tk.Entry(f, textvariable=row['name'], **e_kwargs).grid(row=0, column=0, sticky="ew", padx=10, pady=2)
        tk.Entry(f, textvariable=row['stock'], width=10, justify="center", **e_kwargs).grid(row=0, column=1, padx=4, pady=2)
        tk.Entry(f, textvariable=row['sales'], width=10, justify="center", **e_kwargs).grid(row=0, column=2, padx=4, pady=2)
        
        self.rows.append(row)
    
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
        
        if not latest or latest == VERSION:
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
                subprocess.Popen([updater, '--target', sys.executable, '--restart'])
                progress.stop()
                status_lbl.configure(text="更新器已启动，主程序即将关闭...")
                dlg.after(500, self.win.destroy)
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
                    pass
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
                    w.configure(bg=parent_bg, fg=theme['C_TEXT'])
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
        """直接从OCR结果计算并显示"""
        today = datetime.now()
        region = self.region_var.get()
        plans = []
        
        for item in items:
            name = item.get('name', '')
            stock = int(item.get('stock', 0))
            daily = max(int(item.get('sales', 0)), 0)
            if daily <= 0:
                daily = 1  # 除零保护
            shipping = self._get_shipping(region, name)  # 逐商品查运输时效
            
            ratio = stock / daily
            lead_time = shipping + 1  # 补货时间 = 运输天数 + 1
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
            })
        
        # Sort
        priority = {'red': 0, 'yellow': 1, 'green': 2}
        plans.sort(key=lambda p: priority.get(p['color'], 99))
        
        # Show
        self.tree.delete(*self.tree.get_children())
        for p in plans:
            tags = ()
            if p['color'] == 'red': tags = ('urgent',)
            elif p['color'] == 'yellow': tags = ('warning',)
            self.tree.insert("", "end", values=(
                p['name'], p['stock'], p['daily'], p['ratio'],
                p['status'], p['qty'],
            ), tags=tags)
        
        self.plans = plans
        self.status_text.set(f"计算完成 — {len(plans)} 个商品")
        self.export_btn.config(state="normal")
        self._sort_col = None
        self._auto_expand(len(plans))
        
        # 保存到缓存
        region = self.region_var.get()
        self.active_region = region
        self.cache[region] = {'plans': plans, 'items': items}
        self._update_tabs()
    
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
        
        # 显示该地区的结果
        self.tree.delete(*self.tree.get_children())
        for p in data['plans']:
            tags = ()
            if p['color'] == 'red': tags = ('urgent',)
            elif p['color'] == 'yellow': tags = ('warning',)
            self.tree.insert("", "end", values=(
                p['name'], p['stock'], p['daily'], p['ratio'],
                p['status'], p['qty'],
            ), tags=tags)
        self.plans = data['plans']
        self._update_tabs()
        self.status_text.set(f"已切换到 {region} — {len(data['plans'])} 个商品")
        self._auto_expand(len(data['plans']))
    
    def _del_row(self):
        if len(self.rows) > 1:
            row = self.rows.pop()
            children = list(self.table_area.winfo_children())
            if children:
                children[-1].destroy()
    
    def _clear_input_rows(self):
        """清空所有输入行，同时清除 Treeview 结果"""
        for row in self.rows:
            row['name'].set('')
            row['stock'].set('')
            row['sales'].set('')
        # 也清掉 Treeview 旧结果
        self.tree.delete(*self.tree.get_children())
    
    def _recalc_from_rows(self):
        """从当前输入行读取数据，重新计算"""
        items = []
        for r in self.rows:
            name = r['name'].get().strip()
            stock_s = r['stock'].get().strip()
            sales_s = r['sales'].get().strip()
            if not name:
                continue
            try:
                stock = int(stock_s) if stock_s else 0
            except ValueError:
                stock = 0
            try:
                sales = int(sales_s) if sales_s else 1
            except ValueError:
                sales = 1
            if stock > 0 or sales > 0:
                items.append({'name': name, 'stock': stock, 'sales': max(sales, 1),
                             'region': self.region_var.get()})
        if not items:
            messagebox.showwarning("无数据", "请至少输入一个商品")
            return
        self._calc_from_items(items)
        self.status_text.set(f"已刷新 — {len(items)} 个商品")
    
    def _emergency_stop(self):
        """F9 紧急停止批量识别"""
        self._batch_stop.set()
        self.status_text.set("⏹ 紧急停止 — 正在终止批量识别...")
    
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
        
        result_label = tk.Label(bottom_frame, text="", font=(self.FONT[0], 8),
                               bg=self.C_BG, fg=self.C_MUTED)
        result_label.pack()
        
        test_var = tk.BooleanVar(dlg, value=False)
        tk.Checkbutton(bottom_frame, text="🔍 测试模式",
                      variable=test_var, font=(self.FONT[0], 8),
                      bg=self.C_BG, fg=self.C_MUTED,
                      selectcolor=self.C_BG, activebackground=self.C_BG).pack(pady=(5,0))
        
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
            dlg.destroy()
            # 禁用操作按钮防止并发
            for btn in [self.export_btn]:
                self.win.after(0, lambda b=btn: b.configure(state='disabled'))
            self.status_text.set("批量识别中 — 请不要操作")
            threading.Thread(target=self._run_batch_sequence, args=(selected, hud, hud_text), daemon=True).start()
        
        tk.Button(bottom_frame, text="开始批量识别", command=start_batch,
                  font=self.FONT_BOLD, bg=self.C_PRIMARY, fg="#FFFFFF",
                  width=18, height=2).pack(pady=(10,0))
        
        dlg.transient(self.win)
        dlg.grab_set()
    
    def _run_batch_sequence(self, regions, hud=None, hud_text=None):
        """批量识别：1.点文本框 2.粘贴省份 3.回车 4.点查询 5.等4秒 6.截图识别"""
        import time, pyautogui, pyperclip, threading
        from vision import locate_element
        from ocr import ocr_screenshot_crosscheck as ocr_screenshot
        from PIL import Image as PILImage
        
        def dlog(msg):
            if hud_text:
                self.win.after(0, lambda m=msg: (hud_text.insert('end', f'{m}\n'), hud_text.see('end')))
            self.win.after(0, lambda m=msg: self.status_text.set(f"🔍 {m}"))
        
        self.win.after(0, self.win.iconify); time.sleep(1.5)
        self._batch_stop.clear()
        total = len(regions); success = 0; total_items = 0
        def ss(path):
            """锁窗口截屏 → 按设置裁剪"""
            import pyautogui as pg, json
            # 读裁剪比例
            crop_cfg = {'left': 0.11, 'top': 0.40}
            try:
                sf = os.path.join(get_base_dir(), 'settings.json')
                if os.path.exists(sf):
                    with open(sf, 'r', encoding='utf-8') as f:
                        crop_cfg = json.load(f).get('crop', crop_cfg)
            except: pass
            try:
                import pygetwindow as gw
                for title in ['拼多多', 'pinduoduo', 'Microsoft Edge', 'Edge', 'Chrome', 'Firefox']:
                    wins = gw.getWindowsWithTitle(title)
                    if wins:
                        win = wins[0]
                        if win.isMinimized: win.restore()
                        win.activate(); time.sleep(0.2)
                        img = pg.screenshot(region=(win.left, win.top, win.width, win.height))
                        w, h = img.size
                        sidebar = int(w * crop_cfg['left'])
                        img = img.crop((sidebar, int(h * crop_cfg['top']), w, h))
                        if w > 2560:
                            img = img.resize((2560, int(img.size[1] * 2560 / w)), PILImage.LANCZOS)
                        img.save(path)
                        return
            except Exception: pass
            img = pg.screenshot()
            w, h = img.size
            sidebar = int(w * crop_cfg['left'])
            img = img.crop((sidebar, int(h * crop_cfg['top']), w, h))
            if w > 2560:
                img = img.resize((2560, int(img.size[1] * 2560 / w)), PILImage.LANCZOS)
            img.save(path)
        preset = RESOLUTION_PRESETS.get(load_resolution_pref(), RESOLUTION_PRESETS['1920×1080 (Full HD)'])
        sw, sh = pyautogui.size()
        # 加载校准坐标（一次性）
        import json as _json
        # 加载校准（多路径尝试）
        _cal = {}
        try:
            with open(os.path.join(get_base_dir(), 'settings.json'), 'r', encoding='utf-8') as _f:
                _cal = _json.load(_f).get('calibrate', {})
        except Exception: pass
        if _cal: dlog(f"校准OK: dd({_cal['dropdown']['x']},{_cal['dropdown']['y']}) q({_cal['query']['x']},{_cal['query']['y']})")
        else: dlog("未校准，请先到设置→校准")
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
        
        for i, reg in enumerate(regions):
            if self._batch_stop.is_set(): dlog("⏹ 停止"); break
            dlog(f"── [{reg}] ({i+1}/{total}) ──")
            try:
                # 1. 截图 → 找文本框 → 优先校准坐标
                sp = os.path.join(get_base_dir(), 'output', f'_vis_{i}.png')
                os.makedirs(os.path.dirname(sp), exist_ok=True)
                ss(sp)
                tm_x = tm_y = None
                pos = locate_element(sp, 'region_dropdown', method='template', threshold=0.80)
                if pos:
                    tm_x, tm_y = pos[0], pos[1]
                    dx, dy = tm_x + 90, tm_y
                    dlog(f"1.模板匹配({dx},{dy})")
                elif _cal.get('dropdown'):
                    dx, dy = _cal['dropdown']['x'], _cal['dropdown']['y']
                    dlog(f"1.校准兜底({dx},{dy})")
                else:
                    dx = int(sw * preset['dropdown_x']); dy = int(sh * preset['dropdown_y'])
                    dlog(f"1.预设({dx},{dy})")
                pyautogui.click(dx, dy); time.sleep(0.3); pyautogui.click(dx, dy); time.sleep(0.2)
                
                # 2. 粘贴省份 — 先复制到剪贴板，再三击选中+粘贴
                full = reg if reg in ('内蒙古','广西','西藏','宁夏','新疆','北京','上海','天津','重庆') else reg + '省'
                pyperclip.copy(full)
                pyautogui.tripleClick(dx, dy); time.sleep(0.15)
                pyautogui.hotkey('ctrl', 'v'); time.sleep(0.2)
                dlog(f"2.粘贴'{full}'")
                
                # 3. 回车
                pyautogui.press('enter'); time.sleep(1.0)
                dlog("3.回车确认")
                
                # 4. 找查询按钮 — 仅依靠校准系统
                if _cal.get('query') and _cal.get('mode','absolute') == 'absolute':
                    qx, qy = _cal['query']['x'], _cal['query']['y']
                    dlog(f"4.绝对坐标({qx},{qy})")
                elif _cal.get('dropdown') and _cal.get('query') and _cal.get('mode') == 'offset':
                    offset_x = _cal['query']['x'] - _cal['dropdown']['x']
                    offset_y = _cal['query']['y'] - _cal['dropdown']['y']
                    # 偏移模式：用模板匹配到的文本框位置 + 偏差 = 查询位置
                    if tm_x is not None:
                        qx = tm_x + offset_x; qy = tm_y + offset_y
                    else:
                        qx = dx + offset_x; qy = dy + offset_y
                    dlog(f"4.偏移推算({qx},{qy}) offset=({offset_x},{offset_y}) {'模板' if tm_x else '兜底'}")
                else:
                    dlog("4.⚠ 未校准查询按钮，跳过"); continue
                pyautogui.click(qx, qy)
                # 5. 等待页面刷新（加截图验证：拍两次对比是否变化）
                time.sleep(4.0)
                # 快速验证截图看页面是否真的刷新了
                sp_check = os.path.join(get_base_dir(), 'output', f'_check_{i}.png')
                ss(sp_check)
                dlog(f"5.页面刷新完成")
                
                # 6. 截图 → OCR识别（阻塞，API返回才继续）
                sp2 = os.path.join(get_base_dir(), 'output', f'_result_{i}.png')
                ss(sp2)
                try:
                    im = PILImage.open(sp2); w, h = im.size
                    if w > 2560: im = im.resize((2560, int(h*2560/w)), PILImage.LANCZOS); im.save(sp2)
                except: pass
                dlog("6.OCR识别中(约6s)...")
                items = None
                for retry in range(3):
                    try:
                        items = ocr_screenshot(sp2)
                        if items: break
                        dlog(f"  重试{retry+1}...")
                        time.sleep(2)
                    except Exception as ex:
                        dlog(f"  OCR异常: {ex}")
                        time.sleep(2)
                if items:
                    done = threading.Event()
                    for it in items: it['region'] = reg
                    self.win.after(0, lambda it=items, ev=done: (self._fill_from_ocr(it), ev.set()))
                    done.wait(timeout=10)
                    success += 1; total_items += len(items)
                    dlog(f"6.✓ {len(items)}个商品")
                else:
                    dlog("6.无数据")
            except Exception as e:
                dlog(f"✗ {e}")
        
        self.win.after(0, self.win.deiconify)
        if hud: time.sleep(1); self.win.after(0, hud.destroy)
        # 恢复按钮
        self.win.after(0, lambda: self.export_btn.configure(state='normal'))
        self.win.after(0, lambda: self.status_text.set("就绪 — 批量识别完成"))
        self.win.after(0, lambda: messagebox.showinfo("批量识别完成", f"成功 {success}/{total} 地区\n合计 {total_items} 商品"))
    
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
                import pyautogui as pg
                from PIL import Image as PILImage
                found_window = False
                try:
                    import pygetwindow as gw
                    for t in ['拼多多', 'pinduoduo', 'Edge', 'Chrome', 'Firefox']:
                        wins = gw.getWindowsWithTitle(t)
                        if wins:
                            win = wins[0]
                            found_window = True
                            if win.isMinimized: win.restore()
                            win.activate(); time.sleep(0.2)
                            img = pg.screenshot(region=(win.left, win.top, win.width, win.height))
                            break
                except Exception: pass
                if not found_window:
                    self.win.after(0, self.win.deiconify)
                    self.win.after(0, lambda: (
                        self.status_text.set('❌ 未找到浏览器窗口，请先打开 PDD 后台页面'),
                        messagebox.showwarning('截图失败', '未找到拼多多或浏览器窗口。\n请先打开 PDD 商家后台 -> 订货管理页面。')))
                    return
                # 裁剪
                import json
                crop = {'left':0.11, 'top':0.40}
                try:
                    sf = os.path.join(get_base_dir(), 'settings.json')
                    if os.path.exists(sf):
                        with open(sf,'r') as f: crop = json.load(f).get('crop', crop)
                except: pass
                w, h = img.size
                img = img.crop((int(w * crop['left']), int(h * crop['top']), w, h))
                if img.size[0] > 2560:
                    img = img.resize((2560, int(img.size[1]*2560/img.size[0])), PILImage.LANCZOS)
                img.save(ss_path)
                
                self.win.after(0, self.win.deiconify)
                self.win.after(0, lambda: self.status_text.set('OCR识别中...'))
                
                from ocr import ocr_screenshot_crosscheck as ocr_screenshot
                items = ocr_screenshot(ss_path)
                
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
        self._clear_input_rows()
        self.win.update()
        
        def task():
            try:
                from ocr import ocr_screenshot_crosscheck as ocr_screenshot
                items = ocr_screenshot(path)
                self.win.after(0, lambda i=items: self._fill_from_ocr(i))
            except Exception as e:
                self.win.after(0, self._show_error, str(e))
        
        import threading
        threading.Thread(target=task, daemon=True).start()
    
    def _fill_from_ocr(self, items):
        """用OCR结果填充表格"""
        self.status_text.set(f"OCR识别到 {len(items)} 项，计算中...")
        self._clear_error()
        self.win.update()
        
        if not items:
            self.status_text.set("OCR未识别到任何数据")
            return
        # 清空所有现有行
        for row in self.rows:
            row['name'].set('')
            row['stock'].set('')
            row['sales'].set('')
        # 确保有足够行
        while len(self.rows) < len(items):
            self._add_row()
        # 填入数据
        detected_regions = set()
        for i, item in enumerate(items):
            r = self.rows[i]
            r['name'].set(item.get('name', ''))
            r['stock'].set(str(item.get('stock', '')))
            r['sales'].set(str(item.get('sales', '')))
            region = item.get('region', '')
            if region:
                # 去后缀：云南省→云南，北京市→北京
                for suffix in ['省', '市', '自治区', '特别行政区']:
                    if region.endswith(suffix):
                        region = region[:-len(suffix)]
                        break
                detected_regions.add(region)
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
                    f"识别到新地区：{'、'.join(newly_added)}\n\n已自动添加到地区列表，各商品运输时间暂设为3天。\n请点击「商品时效设置」根据实际情况调整。"))
        self.status_text.set(msg)
        # 直接用OCR结果计算，不依赖行数据
        try:
            self._calc_from_items(items)
        except Exception as e:
            self._show_error(f"计算出错: {e}", popup=True)
            import traceback; traceback.print_exc()
    
    def _sort_tree(self, col):
        """点击列头排序"""
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = False
        
        # 列名到索引
        col_map = {'商品': 0, '库存': 1, '预估销量': 2, '可售卖天数': 3, '状态': 4, '补货量': 5}
        idx = col_map.get(col, 0)
        
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
        for c in col_map:
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
            from export_xlsx import export_cache_to_xlsx
            export_dir = self._get_export_path()
            path = export_cache_to_xlsx(self.cache, export_dir)
            self.status_text.set(f"已导出 {len(self.cache)} 个地区 → PDD补货记录.xlsx")
            os.startfile(export_dir)
            messagebox.showinfo("导出成功", f"已导出 {len(self.cache)} 个地区\n文件: {path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))
    
    def run(self):
        self.win.mainloop()


if __name__ == "__main__":
    App().run()
