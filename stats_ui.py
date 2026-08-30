"""
PDD EZ — 数据页 UI 构建器 (Mixin)：📈 历史趋势页 + 💰 用量明细页

导航重构：把 v1.4.7 的两个弹窗（gui._show_history_dialog 历史趋势 Toplevel、
settings_ui._show_usage_detail 用量明细 Toplevel）整体迁移为导航独立页面，
与 settings_ui.SettingsUIMixin 对称。单一实现原则：旧入口只做 _show_page 跳转
（地区 tab 行尾「📈 历史」快捷方式）或直接移除（API 页底部「💰 用量明细」），
本模块是两页的唯一实现，不留第二个独立实现。

数据口径与弹窗版完全一致：
- 历史页：history_db.query_daily 按日汇总（地区含'全部' × 天数 30/90/180）；
  双击行 _history_day_detail 当日明细；明细行双击 _history_sku_chart Canvas
  折线（手绘零图表库依赖）；「清空全部历史」二次确认；首次启用一次性提示。
- 用量页：usage_store.usage_panel_summary 统一数据源（4 档聚合 + by_model +
  by_call_site + month_label）；估算行 '~' 前缀不计费；缺价显示 '?'
  （不内置默认价）；价格表写 Config['usage']['pricing']（含 image_per_call）；
  「重置本月数据」二次确认走 usage_store.reset_month 原子写路径；
  模型分布 Canvas 条图（本版新增，手绘零依赖）；预算输入框仍不做（P2）。

页面生命周期：页 Frame 常驻（gui._show_page 懒构建 + _built 标志），每次切入
由 _show_page 经 win.after_idle 调度 *_page_refresh 刷新数据——刷新全在主线程
事件队列，worker 线程绝不直调 Tk。
"""
from tkinter import messagebox, ttk
import tkinter as tk

# 与 gui.py 同款守护式导入：增量包缺文件等极端场景历史功能整体降级停用，主程序不受影响
try:
    import history_db
except Exception:
    history_db = None

# 多店铺隔离：店铺筛选数据源（store_registry）与选项组装（store_ui_logic）。
# 守护式导入：缺失时店铺筛选降级为「全部店铺」（=旧行为），历史功能不受影响。
try:
    import store_registry
except Exception:
    store_registry = None
try:
    import store_ui_logic
except Exception:
    store_ui_logic = None


class StatsPagesMixin:
    """混入 App 类：历史趋势 / 用量明细 两个导航数据页的构建与刷新。"""

    # ─────────────── R1 布局优化：纯逻辑助手（无 Tk 依赖，便于单测） ───────────────

    @staticmethod
    def history_filter_segments():
        """历史页筛选条的三段元数据：用于统一栏宽/对齐（与 _build_history_page 一一对应）。

        每段返回 (label_text, combo_width, default_value)。
        - 'store'：店铺（含'全部店铺'），宽 12（双字符名够用，重命名长名会被截短显示）；
        - 'region'：地区（含'全部'），宽 14；
        - 'days'：天数 30/90/180，宽 6。
        """
        return (
            ('店铺', 12, '全部店铺'),
            ('地区', 14, '全部'),
            ('天数', 6, '90'),
        )

    @staticmethod
    def history_summary_text(rows_count, days, has_fail, fail_msg=''):
        """历史页汇总标签文案决策（与 _history_page_refresh 行为一致；可单测）。

        - has_fail → ⚠ 摘要前缀；
        - rows_count > 0 → 「共 N 条按日汇总（双击行看当日明细）」；
        - rows_count == 0 → 空态引导「暂无历史数据 — 识别或导入后会在此处出现」；
        - 无 days 入参按 90 兜底。
        """
        d = int(days) if days else 90
        if has_fail:
            snippet = (fail_msg or '')[:60]
            return f"⚠ 按日汇总查询失败：{snippet}"
        if rows_count and rows_count > 0:
            return f"共 {rows_count} 条按日汇总（近 {d} 天，双击行看当日明细）"
        return "暂无历史数据 — 识别或导入后会在此处出现"

    @staticmethod
    def history_empty_placeholder_row():
        """空态占位行（与 _history_page_refresh 一致）：('day', '--'), ('region', '暂无数据'), ...。"""
        return ('--', '暂无数据', '--', '--', '--')

    def _history_page_enter(self):
        """进入历史页：先与主页当前店铺联动一次，再刷新数据。

        旧路径每次刷新都保持 _hist_store_var 原值——用户切到店铺 B 后进历史页，
        筛选仍停在进入前的店铺（含默认'全部店铺'），历史趋势与当前操作店铺脱节。
        只在「进入页面」这一个时机同步；页内手动改店铺/地区/天数后的刷新
        （_history_page_refresh 直调）不会被本方法覆盖。
        """
        try:
            store_var = getattr(self, '_hist_store_var', None)
            if store_var is not None and store_registry is not None \
                    and store_ui_logic is not None:
                _sid = getattr(self, '_store_id', None) or 'default'
                try:
                    _sname = store_registry.get_store_name(_sid)
                except Exception:
                    _sname = ''
                if _sname:
                    try:
                        _labels, _name2id = store_ui_logic.store_choices(
                            store_registry.get_stores(), all_label='全部店铺')
                    except Exception:
                        _labels, _name2id = [], {}
                    if _sname in _name2id:
                        store_var.set(_sname)  # 联动到当前店铺；名称消失回落'全部店铺'
        except Exception:
            pass  # 联动失败不挡进页（宪法 §4：显式失败在 _history_page_refresh 有提示）
        self._history_page_refresh()

    # ═══════════════════════ 📈 历史趋势页 ═══════════════════════

    def _build_history_page(self, page):
        """📈 历史趋势页（原 gui._show_history_dialog Toplevel 整体迁移）。

        地区下拉（含'全部'）+ 天数 30/90/180 → query_daily 按日汇总 Treeview；
        双击行看该地区当日明细（_history_day_detail），明细行双击看单商品库存
        折线（_history_sku_chart）；「清空全部历史」二次确认；首次启用一次性
        提示。页面每次切入由 _show_page → _history_page_refresh 刷新数据。

        R1 布局优化：筛选条改用 grid 列对齐（标签右贴/Combo 左对齐 + 列宽
        固定），避免 side='left' 在长名 / 多 store 时的串位；空态文案与占位
        行统一走 history_summary_text / history_empty_placeholder_row 纯函数。
        """
        if history_db is None:
            self._lbl(page, text="history_db 模块缺失（增量更新不完整），历史趋势停用。",
                      font=(self.FONT[0], 10), fg=self.C_MUTED, bg=self.C_BG).pack(pady=48)
            return
        # 首次启用一次性提示（幂等：Config['history'].privacy_hint_shown 已置位即跳过）
        self._history_privacy_hint()

        # page 标题统一 FONT_HEADING + 无 emoji（emoji 保留在导航项）
        self._lbl(page, text="识别历史趋势", font=self.FONT_HEADING, bg=self.C_BG,
                  fg=self.C_TEXT).pack(anchor='w', padx=16, pady=(14, 2))
        self._lbl(page, text="识别数据按日汇总（仅保存在本机 history.db，不上传）",
                  font=(self.FONT[0], 8), fg=self.C_MUTED, bg=self.C_BG).pack(
            anchor='w', padx=16, pady=(0, 8))

        # ── 筛选行：店铺下拉+ 地区下拉（含'全部'）+ 天数 30/90/180 + 汇总提示 ──
        # R1：3 个筛选段用独立 Frame（side='left'，内部 label+Combo 同节奏），
        # 段间固定 padx；summary 留右侧贴边。Combo 列宽走 history_filter_segments()
        # 单一来源，避免硬编码漂移。
        bar = tk.Frame(page, bg=self.C_BG)
        bar.pack(fill="x", padx=16, pady=(0, 6))
        # 第一段：店铺
        store_seg = tk.Frame(bar, bg=self.C_BG)
        store_seg.pack(side='left')
        self._lbl(store_seg, text="店铺:", font=(self.FONT[0], 9), fg=self.C_MUTED,
                  bg=self.C_BG).pack(side='left')
        # 店铺筛选（'全部店铺' = 不过滤，与旧行为一致）；地区清单随店铺联动重建
        self._hist_store_var = tk.StringVar(page, value='全部店铺')
        self._hist_store_combo = ttk.Combobox(store_seg, textvariable=self._hist_store_var,
                                              values=['全部店铺'],
                                              width=self.history_filter_segments()[0][1],
                                              state="readonly", font=(self.FONT[0], 9))
        self._hist_store_combo.pack(side='left', padx=(4, 0))
        # 第二段：地区（段间 padx=12）
        reg_seg = tk.Frame(bar, bg=self.C_BG)
        reg_seg.pack(side='left', padx=(12, 0))
        self._lbl(reg_seg, text="地区:", font=(self.FONT[0], 9), fg=self.C_MUTED,
                  bg=self.C_BG).pack(side='left')
        self._hist_reg_var = tk.StringVar(page, value='全部')
        self._hist_reg_combo = ttk.Combobox(reg_seg, textvariable=self._hist_reg_var,
                                            values=['全部'],
                                            width=self.history_filter_segments()[1][1],
                                            state="readonly", font=(self.FONT[0], 9))
        self._hist_reg_combo.pack(side='left', padx=(4, 0))
        # 第三段：天数（段间 padx=12）
        days_seg = tk.Frame(bar, bg=self.C_BG)
        days_seg.pack(side='left', padx=(12, 0))
        self._lbl(days_seg, text="天数:", font=(self.FONT[0], 9), fg=self.C_MUTED,
                  bg=self.C_BG).pack(side='left')
        self._hist_days_var = tk.StringVar(page, value='90')
        days_combo = ttk.Combobox(days_seg, textvariable=self._hist_days_var,
                                  values=['30', '90', '180'], width=6, state="readonly",
                                  font=(self.FONT[0], 9))
        days_combo.pack(side='left', padx=(4, 0))
        # Summary 留右侧（段间 padx=12，与筛选段同节奏）
        self._hist_summary_label = self._lbl(bar, text="", font=(self.FONT[0], 8),
                                             fg=self.C_MUTED, bg=self.C_BG)
        self._hist_summary_label.pack(side='left', padx=(12, 0))

        # ── 按日汇总 Treeview（列与弹窗版一致）──
        cols = ('day', 'region', 'items', 'alerts', 'stock')
        heads = (('day', '日期', 110), ('region', '地区', 100), ('items', '商品数', 90),
                 ('alerts', '预警数', 90), ('stock', '库存合计', 100))
        tree = ttk.Treeview(page, columns=cols, show='headings', height=13)
        for cid, text, w in heads:
            tree.heading(cid, text=text)
            tree.column(cid, width=w, anchor='center')
        tree_wrap = tk.Frame(page, bg=self.C_BG)
        tree_wrap.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        vsb = ttk.Scrollbar(tree_wrap, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        self._hist_page = page
        self._hist_tree = tree

        def on_open(_event):
            sel = tree.selection()
            if not sel:
                return
            vals = tree.item(sel[0], 'values')
            # 修 v4f-F13：空态占位行（日期列为 '--'）不响应双击，
            # 否则会打开"暂无数据 · --"的空明细窗困扰用户
            if not vals or vals[0] == '--':
                return
            if len(vals) >= 2 and vals[1]:
                # 明细跟随当前店铺筛选（'全部店铺' → store=None 查全部店铺）
                _store_id = None
                try:
                    _n2i = getattr(self, '_hist_store_name2id', None) or {}
                    _sv = getattr(self, '_hist_store_var', None)
                    if _sv is not None and _n2i:
                        _store_id = _n2i.get(_sv.get())
                except Exception:
                    _store_id = None
                self._history_day_detail(self.win, vals[1], vals[0], store=_store_id)

        tree.bind('<Double-1>', on_open)

        def clear_all_hist():
            if not messagebox.askyesno(
                    "清空全部历史",
                    "将删除全部识别历史数据（含趋势明细），且不可恢复。\n确定继续？",
                    parent=self.win):
                return
            try:
                ok = history_db.clear_all()
            except Exception:
                ok = False
            messagebox.showinfo("已清空" if ok else "清空失败",
                                "全部历史已清空。" if ok else "清空失败，详情见日志（ocr_dlog.txt）。",
                                parent=self.win)
            self._history_page_refresh()

        btns = tk.Frame(page, bg=self.C_BG)
        btns.pack(fill="x", padx=16, pady=(4, 14))
        self._mk_btn(btns, "🗑 清空全部历史", clear_all_hist, kind='ghost',
                     font=(self.FONT[0], 9)).pack(side="right", padx=4)
        self._lbl(btns, text="双击行看当日明细；明细行双击看单商品库存趋势折线",
                  font=(self.FONT[0], 8), fg=self.C_MUTED, bg=self.C_BG).pack(
            side="left", padx=(0, 4))

        # 筛选变化即刷新（trace 防重入见 _history_page_refresh）
        self._hist_reg_var.trace('w', lambda *_: self._history_page_refresh())
        self._hist_store_var.trace('w', lambda *_: self._history_page_refresh())  # 切店铺联动重建地区清单
        days_combo.bind('<<ComboboxSelected>>', lambda _e: self._history_page_refresh())
        self._history_page_refresh()

    def _history_page_refresh(self):
        """历史页数据刷新（每次切入/筛选变化；_show_page 经 win.after_idle 调度，
        主线程事件队列执行，worker 不碰 Tk）。

        重建地区清单（保留当前选择，选项消失回落'全部'）+ 重查按日汇总；
        查询失败显式提示不静默空白（宪法 §4）。
        """
        page = getattr(self, '_hist_page', None)
        tree = getattr(self, '_hist_tree', None)
        if page is None or tree is None or history_db is None:
            return
        # v1.4.7 P3-R2-M2：after_idle 迟到守门。用户在 history 页快速切到 usage 页
        # 时，_current_page 已变成 usage 页，旧的 after_idle 回调即使触发也不应再
        # 跑 query_daily（浪费 + 可能被 _usage_page 后续操作踩到 tree 引用）。
        if getattr(self, '_current_page', None) is not page:
            return
        try:
            if not page.winfo_exists():
                return
        except Exception:
            return
        if getattr(self, '_hist_busy', False):
            return  # 地区清单 set() 会再触发 trace，防重入
        self._hist_busy = True
        try:
            # 店铺筛选（store 下拉重建，选择消失回落'全部店铺'）。
            # 缺 _hist_store_var（旧测试替身/降级包）时 store_id=None = 全部店铺 = 旧行为。
            store_id = None
            store_var = getattr(self, '_hist_store_var', None)
            if store_var is not None and store_registry is not None and store_ui_logic is not None:
                try:
                    _labels, _name2id = store_ui_logic.store_choices(
                        store_registry.get_stores(), all_label='全部店铺')
                except Exception:
                    _labels, _name2id = ['全部店铺'], {'全部店铺': None}
                cur_store = store_var.get()
                if cur_store not in _name2id:
                    cur_store = '全部店铺'
                combo = getattr(self, '_hist_store_combo', None)
                if combo is not None:
                    combo['values'] = _labels
                if store_var.get() != cur_store:
                    store_var.set(cur_store)
                store_id = _name2id.get(cur_store)
                self._hist_store_name2id = _name2id
            try:
                regions = ['全部'] + list(history_db.query_regions(store=store_id))
            except Exception:
                regions = ['全部']
            cur = self._hist_reg_var.get()
            if cur not in regions:
                cur = '全部'
            self._hist_reg_combo['values'] = regions
            if self._hist_reg_var.get() != cur:
                self._hist_reg_var.set(cur)
            try:
                reg = None if cur == '全部' else cur
                rows = history_db.query_daily(days=int(self._hist_days_var.get() or 90),
                                              region=reg, store=store_id)
                fail = None
            except Exception as e:
                rows = []
                fail = str(e)
            tree.delete(*tree.get_children())
            for r in rows:
                tree.insert('', 'end',
                            values=(r.get('day', ''), r.get('region', ''),
                                    r.get('items', 0), r.get('alerts', 0),
                                    r.get('stock_total', 0)))
            # R1：摘要文案与占位行统一走纯函数 history_summary_text /
            # history_empty_placeholder_row；与单测共享一份决策表。
            # getattr 兜底：旧测试替身 / 极端 stub 可能未注入静态方法，回退内联决策。
            try:
                days_v = int(self._hist_days_var.get() or 90)
            except (TypeError, ValueError):
                days_v = 90
            _sum_fn = getattr(self, 'history_summary_text', None)
            _pl_fn = getattr(self, 'history_empty_placeholder_row', None)
            if _sum_fn is None:
                # 兜底：旧行为（行内决策，与 test_smoke 旧契约一致）
                if fail is not None:
                    summary_text = f"⚠ 按日汇总查询失败：{(fail or '')[:60]}"
                elif rows:
                    summary_text = f"共 {len(rows)} 条按日汇总（双击行看当日明细）"
                else:
                    summary_text = "暂无历史数据 — 识别或导入后会在此处出现"
                placeholder = ('--', '暂无数据', '--', '--', '--')
            else:
                if fail is not None:
                    summary_text = _sum_fn(0, days_v, True, fail)
                else:
                    summary_text = _sum_fn(len(rows), days_v, False)
                placeholder = _pl_fn() if _pl_fn is not None else (
                    '--', '暂无数据', '--', '--', '--')
            self._hist_summary_label.config(text=summary_text)
            if not rows and fail is None:
                tree.insert('', 'end', values=placeholder)
            # R1：flush 一次 idletasks 让 Combo.values 重建 / Treeview 重绘在同一帧，
            # 避免「Combo 先空 → 数据行后到」的闪跳（用户切店铺/地区时观感更稳）。
            try:
                page.update_idletasks()
            except Exception:
                pass
        finally:
            self._hist_busy = False

    # ═══════════════════════ 💰 用量明细页 ═══════════════════════

    def _build_usage_page(self, page):
        """💰 用量明细页（原 settings_ui._show_usage_detail Toplevel 整体迁移）。

        4 档聚合大字（今日/本周/本月/总计）+ 按模型/按用途分布表 + 模型分布
        Canvas 条图 + 价格表编辑 Treeview（写 Config['usage']['pricing']，含
        image_per_call）+「重置本月数据」二次确认（usage_store.reset_month
        原子写）。口径：估算 '~' 前缀不计费；缺价显示 '?'。预算输入框不做（P2）。
        页面用滚动容器承载（内容高于主窗），每次切入 _usage_page_refresh 刷新
        聚合数据；价格表编辑态不因切页丢失（刷新不触碰价格表）。
        """
        import usage_store  # 闭包共用（reset_this_month / 刷新）：方法内导入，保持模块轻量
        from utils import get_usage_cfg

        # page 标题统一 FONT_HEADING + 无 emoji（emoji 保留在导航项）
        self._lbl(page, text="API 用量明细", font=self.FONT_HEADING, bg=self.C_BG,
                  fg=self.C_TEXT).pack(anchor='w', padx=16, pady=(14, 2))

        # ── 滚动容器（同 _build_general_page 模式）──
        canvas = tk.Canvas(page, highlightthickness=0, bg=self.C_BG)
        scroll = ttk.Scrollbar(page, orient='vertical', command=canvas.yview)
        content = tk.Frame(canvas, bg=self.C_BG)
        content.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        wid = canvas.create_window((0, 0), window=content, anchor='nw')
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(wid, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')

        def _mw(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), 'units')
        canvas.bind('<Enter>', lambda e: canvas.bind_all('<MouseWheel>', _mw))
        canvas.bind('<Leave>', lambda e: canvas.unbind_all('<MouseWheel>'))

        # ── 4 档聚合大字（统一走 usage_panel_summary；标签引用留给刷新）──
        self._usage_stat_labels = {}
        topbar = tk.Frame(content, bg=self.C_BG)
        topbar.pack(fill="x", padx=16, pady=(8, 8))
        for key, label in (('today', '今日'), ('week', '本周'), ('month', '本月'), ('all', '总计')):
            cell = tk.Frame(topbar, bg=self.C_BG, highlightthickness=1,
                            highlightbackground="#EAEAEA")
            cell.pack(side="left", expand=True, fill="x", padx=6)
            self._lbl(cell, text=label, font=(self.FONT[0], 8), fg=self.C_MUTED,
                      bg=self.C_BG).pack(pady=(8, 0))
            cost_lbl = self._lbl(cell, text="¥0.00", font=(self.FONT[0], 13, 'bold'),
                                 fg=self.C_TEXT, bg=self.C_BG)
            cost_lbl.pack()
            sub_lbl = self._lbl(cell, text=" ", font=(self.FONT[0], 7),
                                fg=self.C_MUTED, bg=self.C_BG)
            sub_lbl.pack(pady=(0, 6))
            self._usage_stat_labels[key] = (cost_lbl, sub_lbl)

        # ── 按模型 / 按用途（来自 usage_panel_summary；树引用留给刷新）──
        mid = tk.Frame(content, bg=self.C_BG)
        mid.pack(fill="x", padx=16, pady=8)
        left = tk.Frame(mid, bg=self.C_BG)
        left.pack(side='left', fill='both', expand=True, padx=(0, 6))
        right = tk.Frame(mid, bg=self.C_BG)
        right.pack(side='left', fill='both', expand=True, padx=(6, 0))
        self._usage_month_lbl = self._lbl(left, text="按模型", font=(self.FONT[0], 9, 'bold'),
                                          fg=self.C_TEXT, bg=self.C_BG)
        self._usage_month_lbl.pack(anchor='w')
        self._usage_model_tree = self._make_usage_tree(left)
        self._lbl(right, text="按用途（本次启动以来识别调用点）", font=(self.FONT[0], 9, 'bold'),
                  fg=self.C_TEXT, bg=self.C_BG).pack(anchor='w')
        self._usage_site_tree = self._make_usage_tree(right)

        # ── 模型分布 Canvas 条图（本版新增；手绘零图表库依赖，同 _history_sku_chart 风格）──
        cv = tk.Canvas(content, height=120, bg=self.C_BG, highlightthickness=0)
        cv._skip_theme = True  # 画布项颜色由重绘回调管理，walk 不碰
        cv.pack(fill='x', padx=16, pady=(2, 2))
        self._usage_chart_canvas = cv
        self._usage_panel = {}
        self._usage_chart_w = 0
        cv.bind('<Configure>', self._usage_chart_on_resize)
        self._register_redraw(self._usage_chart_redraw)  # 主题切换后按新 token 重画

        self._lbl(content, text="估算仅供参考：'~' 前缀行为兜底估算（不计费）；'?' 为未配置价格"
                                "——请在下方价格表填写官方刊例价（元/百万 token，图片价按张）",
                  font=(self.FONT[0], 7), fg=self.C_MUTED, bg=self.C_BG).pack(
            anchor='w', padx=16, pady=(0, 8))  # A7：统一垂直节奏 8px

        # ── 价格表编辑（双击单元格改值；「保存价格表」写 Config['usage']['pricing']）──
        self._lbl(content, text="价格表（元/百万 token；image_per_call 为每张图价，可留空）",
                  font=(self.FONT[0], 9, 'bold'), fg=self.C_TEXT, bg=self.C_BG).pack(
            anchor='w', padx=16, pady=(0, 8))  # A7：统一垂直节奏 8px
        pcols = ('provider', 'model', 'input', 'output', 'image')
        pheads = (('provider', '提供商', 90), ('model', '模型名', 220),
                  ('input', '输入价', 90), ('output', '输出价', 90),
                  ('image', '每张图价', 90))
        ptree = ttk.Treeview(content, columns=pcols, show='headings', height=5)
        for cid, text, w in pheads:
            ptree.heading(cid, text=text)
            ptree.column(cid, width=w, anchor='center')
        ptree.pack(fill='x', padx=16, pady=(0, 8))  # A7：统一垂直节奏 8px
        _PFIELDS = ('provider', 'model', 'input', 'output', 'image')
        rows_data = {}
        _row_seq = [0]

        def _add_row(prov='', mdl='', ent=None):
            ent = ent or {}
            _row_seq[0] += 1
            iid = f'R{_row_seq[0]}'
            rows_data[iid] = {
                'provider': prov or '', 'model': mdl or '',
                'input': ent.get('input_per_million', ''),
                'output': ent.get('output_per_million', ''),
                'image': ent.get('image_per_call', '')}
            return iid

        def _refill_ptree():
            ptree.delete(*ptree.get_children())
            for iid, rd in rows_data.items():
                ptree.insert('', 'end', iid=iid,
                             values=(rd['provider'], rd['model'], rd['input'],
                                     rd['output'], rd['image']))

        try:
            _pricing = get_usage_cfg().get('pricing') or {}
            for prov, models in _pricing.items():
                if isinstance(models, dict):
                    for mdl, ent in models.items():
                        if isinstance(ent, dict):
                            _add_row(prov, mdl, ent)
        except Exception:
            pass
        if not rows_data:
            _add_row()  # 空表给一行占位，双击即可填写
        _refill_ptree()

        def on_ptree_dbl(event):
            iid = ptree.identify_row(event.y)
            col = ptree.identify_column(event.x)
            if not iid or iid not in rows_data or col not in ('#1', '#2', '#3', '#4', '#5'):
                return
            field = _PFIELDS[int(col[1]) - 1]
            bbox = ptree.bbox(iid, col)
            if not bbox:
                return
            x, y, w, h = bbox
            # v1.4.7 P3-R2-L1 + P3-R2-M1：防御性销毁已存在的编辑 Entry。
            # 极快速双击 cell A→B 时，old Entry 的 FocusOut 回调与新 Double-1
            # 在同一事件循环窗口里抢资源，直接 e.destroy() 会与新 Entry.place/
            # focus_set 撞车出现 TclError 抖动。修复：destroy 全部走 after_idle
            # 延迟到当前事件链结束，且 _commit/_cancel 入口做 _edit_entry 引用
            # 检查确保只销毁"自己"（避免后到的 _commit 误杀新 Entry）。
            prev = getattr(ptree, '_edit_entry', None)
            if prev is not None:
                ptree._edit_entry = None  # 先清引用，再延迟销毁（旧回调走 finally 看到 None 跳过）
                try:
                    if prev.winfo_exists():
                        # after_idle 延迟到当前事件链结束，避免与新 Entry place 抢时序
                        self.win.after_idle(lambda p=prev: self._safe_destroy_widget(p))
                except Exception:
                    pass
            e = tk.Entry(ptree, font=(self.FONT[0], 8),
                         highlightthickness=1, highlightbackground=self.C_ACCENT)
            ptree._edit_entry = e  # L1：记录当前 Entry 给下次双击清理
            e.place(x=x, y=y, width=w, height=h)
            e.insert(0, str(rows_data[iid].get(field, '')))
            e.focus_set()

            def _commit(_e=None):
                try:
                    # v1.4.7 P3-R2-M2：输入校验——价格字段（input/output/image）必须是非负数；
                    # provider/model 是标识符字段只去空白。负价 clamp 为 0，非法输入
                    # （非数字串）显式 showwarning 后保留原值不静默丢。
                    raw = e.get().strip()
                    if field in ('input', 'output', 'image'):
                        if raw == '':
                            new_val = ''
                        else:
                            try:
                                _v = float(raw)
                                if _v < 0:
                                    try:
                                        messagebox.showwarning(
                                            "价格校验", f"「{field}」价格不能为负数，已自动 clamp 为 0（原值 {_v:.4f}）",
                                            parent=self.win)
                                    except Exception:
                                        pass
                                    new_val = '0'
                                else:
                                    new_val = str(_v)
                            except ValueError:
                                try:
                                    messagebox.showwarning(
                                        "价格校验",
                                        f"「{field}」价格输入「{raw}」不是有效数字，保持原值不变。",
                                        parent=self.win)
                                except Exception:
                                    pass
                                return  # 非法输入：保留原值不写回
                    else:
                        new_val = raw
                    rows_data[iid][field] = new_val
                    ptree.set(iid, field, new_val)
                finally:
                    # v1.4.7 P3-R2-M1：destroy 走 after_idle 避免与新事件抢时序；
                    # 仅当 _edit_entry 仍指向自己才清引用（后到回调可能已被新 Edit 替换）
                    try:
                        if ptree._edit_entry is e:
                            ptree._edit_entry = None
                            self.win.after_idle(lambda ee=e: self._safe_destroy_widget(ee))
                    except Exception:
                        pass

            def _cancel(_e=None):
                try:
                    if ptree._edit_entry is e:
                        ptree._edit_entry = None
                        self.win.after_idle(lambda ee=e: self._safe_destroy_widget(ee))
                except Exception:
                    pass

            e.bind('<Return>', _commit)
            e.bind('<Escape>', _cancel)
            e.bind('<FocusOut>', _commit)

        ptree.bind('<Double-1>', on_ptree_dbl)
        self._lbl(content, text="双击单元格修改；provider 如 doubao/qwen/glm，model 填完整模型名",
                  font=(self.FONT[0], 7), fg=self.C_MUTED, bg=self.C_BG).pack(
            anchor='w', padx=16, pady=(2, 4))

        def save_pricing():
            from utils import Config
            s = Config.load()
            if not isinstance(s, dict):
                s = {}
            out = {}

            def _num(v):
                try:
                    sv = str(v).strip()
                    return float(sv) if sv != '' else None
                except (TypeError, ValueError):
                    return None

            for rd in rows_data.values():
                prov = str(rd.get('provider', '')).strip()
                mdl = str(rd.get('model', '')).strip()
                if not prov or not mdl:
                    continue  # 缺 provider/model 的行不保存
                ent = {}
                for k, key in (('input', 'input_per_million'),
                               ('output', 'output_per_million'),
                               ('image', 'image_per_call')):
                    nv = _num(rd.get(k))
                    if nv is not None:
                        ent[key] = nv
                out.setdefault(prov, {})[mdl] = ent
            u = s.get('usage')
            u = u if isinstance(u, dict) else {}
            u['pricing'] = out
            s['usage'] = u
            Config.save(s)
            self.status_text.set("价格表已保存（元/百万 token；缺价模型费用仍显示 ?）")

        def reset_this_month():
            if not messagebox.askyesno(
                    "重置本月数据",
                    "将删除本月全部用量记录（含按模型/按用途明细），且不可恢复。\n确定继续？",
                    parent=self.win):
                return
            ok = usage_store.reset_month()  # C1 原子写路径（os.replace + pid 锁）
            if ok:
                self.status_text.set("本月用量数据已重置")
                messagebox.showinfo("已重置", "本月用量数据已重置。\n本页聚合已同步刷新。",
                                    parent=self.win)
                self._usage_page_refresh()
                try:
                    self._refresh_cost_label()  # 工具条「本月 ¥」同步归零（同源 usage_store）
                except Exception:
                    pass
            else:
                messagebox.showerror("重置失败", "重置失败，详情见日志（ocr_dlog.txt）。",
                                     parent=self.win)

        pbtns = tk.Frame(content, bg=self.C_BG)
        pbtns.pack(fill="x", padx=16, pady=(2, 14))
        self._mk_btn(pbtns, "＋ 添加型号", lambda: (_add_row(), _refill_ptree()),
                     kind='ghost', font=(self.FONT[0], 8)).pack(side="left", padx=3)

        def _del_selected():
            sel = ptree.selection()
            if not sel:
                return
            if len(rows_data) <= len(sel):
                _add_row()  # 不允许清空到零行（至少留一行占位）
            for iid in sel:
                rows_data.pop(iid, None)
            _refill_ptree()
        self._mk_btn(pbtns, "－ 删除选中", _del_selected, kind='ghost',
                     font=(self.FONT[0], 8)).pack(side="left", padx=3)
        self._mk_btn(pbtns, "保存价格表", save_pricing, kind='primary',
                     font=(self.FONT[0], 8, 'bold')).pack(side="right", padx=3)
        # 实施 A8：重置本月数据——危险操作·用 C_RED_BG 视觉区分（v4f 修正：不新建 _warn_btn，复用 _mk_btn 返回值 config）
        _reset_btn = self._mk_btn(pbtns, "重置本月数据", reset_this_month, kind='primary',
                     font=(self.FONT[0], 8))
        _reset_btn.configure(bg=self.C_RED_BG)
        _reset_btn.pack(side="right", padx=3)

        self._usage_page = page
        self._usage_page_refresh()

    def _make_usage_tree(self, parent):
        """按模型/按用途共用 Treeview（列结构与弹窗版一致；数据由刷新填充）。"""
        cols = ('name', 'cost', 'tokens', 'count')
        tree = ttk.Treeview(parent, columns=cols, show='headings', height=6)
        for cid, text, w in (('name', '名称', 130), ('cost', '费用(元)', 80),
                             ('tokens', 'token 数', 90), ('count', '次数', 50)):
            tree.heading(cid, text=text)
            tree.column(cid, width=w, anchor='center')
        tree.pack(fill='x')
        return tree

    @staticmethod
    def _refill_usage_tree(tree, data):
        """分布表填充：按 cost 降序；费用 0 显示 '?'（缺价口径，与弹窗版一致）。"""
        tree.delete(*tree.get_children())
        for nm, v in sorted((data or {}).items(),
                            key=lambda kv: -(kv[1] or {}).get('cost', 0)):
            v = v or {}
            cost_v = v.get('cost', 0.0) or 0.0
            tree.insert('', 'end', values=(
                nm, f"{cost_v:.4f}" if cost_v else '?',
                v.get('tokens', 0), v.get('count', 0)))

    @staticmethod
    def _safe_destroy_widget(w):
        """v1.4.7 P3-R2-M1：延迟销毁 widget 守卫。

        用于价格表 Entry 编辑的 _commit/__cancel 与 on_ptree_dbl 的 prev 清理。
        全部走 after_idle 延迟到当前事件链结束后再 destroy，避免与新 Double-1
        撞车出现 TclError 抖动。winfo_exists 双层防御（外层已检查，内层兜底）
        确保对已被 destroy 的 widget 不抛异常。
        """
        try:
            if w is not None and w.winfo_exists():
                w.destroy()
        except Exception:
            pass

    @staticmethod
    def _chart_empty_message(panel):
        """v1.4.7 P3-R2-L1：条图空数据/全 0 提示文案决策（与绘制解耦，便于单测）。

        三分支：
        - entries 空 → 「暂无模型费用数据」引导识别/调用
        - entries 非空但 max_cost=0 → 「有调用但未配价格」引导核对价格表
        - 其他 → None（正常绘制）
        """
        entries = (panel or {}).get('by_model') or {}
        if not entries:
            return "本月暂无模型费用数据（识别 / 调用后此处出图）"
        try:
            max_cost = max((v or {}).get('cost', 0.0) or 0.0 for v in entries.values())
        except ValueError:
            max_cost = 0.0
        if max_cost <= 0:
            n = len(entries)
            return (f"本月有 {n} 个模型调用记录，但价格表未配置或全为 0，无法显示费用分布。"
                    "请在下方价格表填写官方刊例价（双击单元格修改 → 保存价格表）")
        return None

    def _usage_page_refresh(self):
        """用量页数据刷新（每次切入；_show_page 经 win.after_idle 调度，主线程执行）。

        重读 usage_store.usage_panel_summary → 更新 4 档大字 / 按模型（含月份标签）/
        按用途两棵分布树 / 模型分布条图。不触碰价格表（保护未保存的编辑态）。
        """
        page = getattr(self, '_usage_page', None)
        if page is None:
            return
        # v1.4.7 P3-R2-M2：after_idle 迟到守门（见 _history_page_refresh 注释）。
        # 用量页切走时不应再读 jsonl 跑 panel summary 浪费 IO。
        if getattr(self, '_current_page', None) is not page:
            return
        try:
            if not page.winfo_exists():
                return
        except Exception:
            return
        import usage_store
        panel = usage_store.usage_panel_summary() or {}
        self._usage_panel = panel
        for key, (cost_lbl, sub_lbl) in getattr(self, '_usage_stat_labels', {}).items():
            d = panel.get(key) or {}
            cost_lbl.config(text=f"¥{(d.get('cost_cny') or 0.0):.2f}")
            sub = []
            if d.get('estimate_count'):
                sub.append(f"~{d['estimate_count']} 笔估算")
            if d.get('missing_count'):
                sub.append(f"{d['missing_count']} 笔缺价(?)")
            sub_lbl.config(text=" ".join(sub) or " ")
        self._usage_month_lbl.config(text=f"按模型（{panel.get('month_label', '')}）")
        self._refill_usage_tree(self._usage_model_tree, panel.get('by_model'))
        self._refill_usage_tree(self._usage_site_tree, panel.get('by_call_site'))
        self._usage_chart_redraw()

    def _usage_chart_on_resize(self, event):
        """条图容器宽度变化时重画（高度变化不重画，防 Configure 循环）。"""
        if abs(event.width - getattr(self, '_usage_chart_w', 0)) < 10:
            return
        self._usage_chart_w = event.width
        self._usage_chart_redraw()

    def _usage_chart_redraw(self):
        """模型分布条图（Canvas 手绘，零图表库依赖）：本月按模型 cost 水平条。

        缺价模型费用 0 → 条长 0 + '?' 标注；估算行不计费口径与分布表一致。
        颜色全部走主题 token，主题切换经 _register_redraw 回到此处重画：
        先把画布 bg 同步到当前主题的 C_BG（_skip_theme 守 walk 不刷这里，
        否则深/浅主题切换后画布底色会留旧值），再 delete+重绘所有 items。
        """
        cv = getattr(self, '_usage_chart_canvas', None)
        if cv is None:
            return
        try:
            if not cv.winfo_exists():
                return
        except Exception:
            return
        # v1.4.7 P3-R1'-修 L2：主题切换后画布 bg 联动（C_BG 走主题 token 变化，
        # _skip_theme 阻止 walk 刷新这里，必须在 redraw 入口显式同步）
        try:
            cv.configure(bg=self.C_BG)
        except Exception:
            pass
        cv.delete('all')
        panel = getattr(self, '_usage_panel', None) or {}
        entries_raw = panel.get('by_model') or {}
        entries = sorted(entries_raw.items(),
                         key=lambda kv: -(kv[1] or {}).get('cost', 0))
        w = cv.winfo_width()
        if w <= 2:
            w = 680  # 首次布局前 winfo_width 尚未生效，先按默认宽画，Configure 后重画
        mL, mR, mT = 132, 76, 28
        row_h = 24
        n = len(entries)
        h = mT + 10 + row_h * max(n, 1) + 8
        if int(cv.cget('height')) != h:
            cv.configure(height=h)
        cv.create_text(w // 2, 14,
                       text=f"本月模型费用分布（{panel.get('month_label', '')}，共 {n} 个模型）",
                       font=(self.FONT[0], 9, 'bold'), fill=self.C_TEXT)
        # v1.4.7 P3-R2-L1：友好提示分支。空 entries / 全 0 成本（用户未配价格表）
        # 时给明确引导，避免出现「N 行 '? '」让用户疑惑。决策由 _chart_empty_message
        # 静态方法产出（无 Tk 依赖，可单测），redraw 只负责画。
        empty_msg = self._chart_empty_message(panel)
        if empty_msg is not None:
            cv.create_text(w // 2, mT + 10 + row_h // 2,
                           text=empty_msg,
                           font=(self.FONT[0], 8), fill=self.C_MUTED, width=max(w - 32, 80))
            return
        max_cost = max((v or {}).get('cost', 0.0) or 0.0 for _nm, v in entries)
        plot_w = max(w - mL - mR, 10)
        for i, (nm, v) in enumerate(entries):
            v = v or {}
            cost = v.get('cost', 0.0) or 0.0
            y = mT + 10 + i * row_h + row_h // 2
            name = nm if len(nm) <= 16 else nm[:15] + '…'
            cv.create_text(8, y, text=name, anchor='w',
                           font=(self.FONT[0], 8), fill=self.C_TEXT)
            if cost > 0 and max_cost > 0:
                bw = max(int(plot_w * cost / max_cost), 3)
                cv.create_rectangle(mL, y - 7, mL + bw, y + 7,
                                    fill=self.C_ACCENT, outline='')
            label = f"¥{cost:.2f}" if cost > 0 else '?'
            cv.create_text(w - mR + 8, y, text=label, anchor='w',
                           font=(self.FONT[0], 8), fill=self.C_MUTED)
