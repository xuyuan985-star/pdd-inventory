"""
PDD EZ — 设置页 UI 构建器 (Mixin)
从 gui.py 拆分：通用/商品/皮肤/校准/分辨率/后台 六个设置页面的构建逻辑。
"""
from datetime import datetime
from tkinter import messagebox, ttk
import tkinter as tk

from config import THEMES, save_theme_pref
from utils import get_base_dir, get_api_config, Config

# 多店铺隔离：店铺管理数据源（store_registry， 产出）。
# 守护式导入：缺失时店铺管理卡片显示降级提示，设置页其余功能不受影响。
try:
    import store_registry
except Exception:
    store_registry = None


class SettingsUIMixin:
    """混入 App 类，提供所有设置页面构建方法。"""

    # ─────────────── R1 布局优化：纯逻辑助手（无 Tk 依赖，便于单测） ───────────────
    # 这些 helper 与 widget 解耦——只接受 plain 数据 → 返回 plain 结果；test_layout_logic
    # 直接 import settings_ui 即可断言（不需要创建 Tk 根窗口）。

    @staticmethod
    def store_list_row_label(store_name, store_id, active_id):
        """店铺列表行文本：「name  ★ 当前」/「name」。active 不匹配则不加 ★。"""
        nm = str(store_name or '').strip() or str(store_id or '')
        if active_id and store_id and str(store_id) == str(active_id):
            return f"{nm}  ★ 当前"
        return nm

    @staticmethod
    def store_list_active_index(stores, active_id):
        """返回 active 店在 stores 列表里的索引；未命中/单家 -1。
        用于 Listbox 选中态高亮（★ 当前）；store id 权威，name 仅展示。
        """
        if not isinstance(stores, (list, tuple)) or not stores:
            return -1
        for i, s in enumerate(stores):
            if isinstance(s, dict) and str(s.get('id') or '') == str(active_id or ''):
                return i
        return -1

    @staticmethod
    def store_button_disabled_state(store_count, selected_idx):
        """店铺管理 4 个动作按钮的禁用态表（基于店铺总数 + 当前列表选中）。

        返回 dict {'add': bool, 'rename': bool, 'activate': bool, 'delete': bool}。
        - add 永远可点（加新店无前置依赖）；
        - rename / activate / delete 都需要列表选中；delete 还需选中非 default；
        - 当只有 1 家店时，activate / delete 也禁掉（删完无店 / 切同店无意义）。
        """
        n = int(store_count or 0)
        sel = int(selected_idx) if selected_idx is not None and int(selected_idx) >= 0 else -1
        only_one = n <= 1
        return {
            'add': False,  # 新增永远可点
            'rename': only_one or sel < 0,  # 单店/未选 → 禁
            'activate': only_one or sel < 0,  # 单店/未选 → 禁
            'delete': only_one or sel < 0,  # 单店/未选 → 禁
        }

    @staticmethod
    def adv_frame_visibility_for_model(model):
        """补货策略卡高级因子编辑区的显隐表（基于当前 model）。

        高级（'advanced'）→ 全展开；其它 → 全折叠。仅返回 visibility 表，调用方按表操作。
        """
        return {'advanced_frame': str(model or '') == 'advanced'}

    def _lbl(self, parent, *args, **kwargs):
        """Label 创建 helper：未显式指定 bg 时继承父容器背景色，杜绝异色文字块"""
        if 'bg' not in kwargs:
            try:
                kwargs['bg'] = parent.cget('bg')
            except Exception:
                kwargs['bg'] = self.C_BG
        return tk.Label(parent, *args, **kwargs)

    def _build_general_page(self):
        """通用设置：导出路径 + API配置"""
        canvas = tk.Canvas(self.page_general, highlightthickness=0, bg=self.C_BG)
        scroll = ttk.Scrollbar(self.page_general, orient='vertical', command=canvas.yview)
        content = tk.Frame(canvas, bg=self.C_BG)
        content.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        wid = canvas.create_window((0, 0), window=content, anchor='nw')
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(wid, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        def _mw(e): canvas.yview_scroll(int(-1*(e.delta/120)), 'units')
        canvas.bind('<Enter>', lambda e: canvas.bind_all('<MouseWheel>', _mw))
        canvas.bind('<Leave>', lambda e: canvas.unbind_all('<MouseWheel>'))
        # 实施 B1：canvas 提升为 self 属性，供 5 段锚点按钮实时计算 winfo_y
        self._general_canvas = canvas
        # 实施 B1：5 段段名→锚点 Frame 引用字典（build 时填，click 时实时算 y）
        self._general_anchors = {}

        # ── 导出路径模块（浅灰白卡片容器）──
        _m1 = tk.Frame(content, bg=self.C_BG, highlightthickness=1, highlightbackground=self.C_BORDER)
        _m1.pack(fill="x", padx=20, pady=8)
        # B1：本段段名锚点（用于顶部 _anchor_btns 跳锚；取卡片自身即可，winfo_y 即顶）
        self._general_anchors['导出路径'] = _m1
        self._lbl(_m1, text='导出路径', font=self.FONT_HEADING, bg=self.C_BG,
                 fg=self.C_SECONDARY).pack(pady=(12,2))
        self._lbl(_m1, text="Excel 导出文件的保存位置", font=(self.FONT[0], 8),
                 fg=self.C_MUTED, bg=self.C_BG).pack()
        pf = tk.Frame(_m1, bg=self.C_BG); pf.pack(pady=8, padx=20, fill='x')
        self.export_path_var = tk.StringVar(self.win, value=self._get_export_path())
        _ep = tk.Entry(pf, textvariable=self.export_path_var, font=self.FONT, width=50,
                      relief='flat', bd=0, highlightthickness=1,
                      highlightbackground="#EAEAEA", highlightcolor="#EAEAEA",
                      bg="#FFFFFF", fg=self.C_TEXT, insertbackground=self.C_TEXT)
        _ep.pack(side='left')
        self._mk_btn(pf, '浏览', lambda: self._pick_export_path(None), kind='ghost', font=(self.FONT[0], 8), pack_side='left', padx=5)
        def open_export_dir():
            """打开导出目录；不存在/不可写时给出明确提示"""
            import os as _os
            path = self.export_path_var.get().strip()
            if not path:
                messagebox.showwarning("路径为空", "请先选择导出路径", parent=self.win)
                return
            if not _os.path.isdir(path):
                try:
                    _os.makedirs(path, exist_ok=True)
                except OSError as e:
                    messagebox.showerror("路径不可用", f"无法创建目录：{e}", parent=self.win)
                    return
            try:
                _os.startfile(path)
            except OSError as e:
                messagebox.showwarning("无法打开", f"打开文件夹失败：{e}", parent=self.win)
        self._mk_btn(pf, '打开文件夹', open_export_dir, kind='ghost', font=(self.FONT[0], 8), pack_side='left', padx=5)
        self._mk_btn(_m1, '保存', lambda: self._save_settings(None), kind='primary', font=(self.FONT[0], 9, 'bold'), width=12).pack_configure(pady=(5,12))

        # ── 定位校准（AI 智能定位，v1.3 起唯一模式）──
        # ：原 _build_calibrate_tab 改名为 _build_calibrate_inline
        # B1：校准段锚点——取该函数内创建并 pack 的 ai_card 作为锚
        self._build_calibrate_inline(content)
        try:
            _cal_anchor = None
            for _c in reversed(content.winfo_children()):
                if isinstance(_c, tk.Frame):
                    _info = _c.pack_info()
                    if _info.get('fill') == 'x' and _c not in self._general_anchors.values():
                        _cal_anchor = _c
                        break
            if _cal_anchor is not None:
                self._general_anchors['定位校准'] = _cal_anchor
        except Exception:
            pass
        # 实施 A1：删两条 ttk.Separator，padding 8 承担段间距

        # ── 识别列配置模块（浅灰白卡片容器）──
        _m3 = tk.Frame(content, bg=self.C_BG, highlightthickness=1, highlightbackground=self.C_BORDER)
        _m3.pack(fill="x", padx=20, pady=8)
        # B1：识别列配置段名锚点
        self._general_anchors['识别列配置'] = _m3
        self._lbl(_m3, text='识别列配置', font=self.FONT_HEADING, bg=self.C_BG,
                 fg=self.C_SECONDARY).pack(pady=(12,2))
        self.col_status_var = tk.StringVar(self.win, value='')
        self._lbl(_m3, text="先「探测列」识别后台表格的所有列，再勾选要识别的列（库存/销量列为计算必需）",
                 font=(self.FONT[0], 8), fg=self.C_MUTED, bg=self.C_BG).pack()
        col_btn_row = tk.Frame(_m3, bg=self.C_BG); col_btn_row.pack(pady=8)
        self._mk_btn(col_btn_row, '🔍 探测全部列', self._probe_columns, kind='dark',
                  font=(self.FONT[0], 8)).pack(side='left', padx=5)
        self._mk_btn(col_btn_row, '⚙ 配置识别列', self._config_columns, kind='dark',
                  font=(self.FONT[0], 8)).pack(side='left', padx=5)
        self._lbl(_m3, textvariable=self.col_status_var,
                 font=(self.FONT[0], 8), fg=self.C_MUTED, bg=self.C_BG).pack(pady=(0,10))

        # ── 副模型（双模型验证用，🛡 勾选时生效）──
        # 实施 A2：sec_row 裸行包 C_BORDER 卡片，与上方识别列配置同形
        _sec_card = tk.Frame(content, bg=self.C_BG, highlightthickness=1, highlightbackground=self.C_BORDER)
        _sec_card.pack(fill="x", padx=20, pady=8)
        self._general_anchors['副模型'] = _sec_card
        sec_row = tk.Frame(_sec_card, bg=self.C_BG); sec_row.pack(padx=16, pady=10, fill='x')
        self._lbl(sec_row, text="副模型（双模型验证）:", font=(self.FONT[0], 8),
                 fg=self.C_TEXT).pack(side="left")
        # v1.5.11 报错提示体系：副模型能力说明（OCR 专用 vs VL 通用，防用户配错后困惑）
        try:
            _sec_hint_lbl = self._lbl(_sec_card, text="",
                                      font=(self.FONT[0], 7), fg=self.C_MUTED, bg=self.C_BG)
            _sec_hint_lbl.pack(padx=16, anchor='w', pady=(0, 6))

            def _sec_hint(text_var):
                """副模型能力提示：随输入实时切换说明文案（纯函数化，可单测）。"""
                import re
                _m = str(text_var.get() or '').strip().lower()
                if re.search(r'qwen[0-9.]*[-_]?(vl)?[-_]?ocr', _m) or 'ocr' in _m:
                    return 'OCR 专用副模型：只做文字/数字复核，不参与表格交叉验证（识别时按单模型，数字更准）；想双验证请用两个 VL 模型'
                if _m:
                    return 'VL 通用视觉副模型：可与主模型做双模型交叉验证（不一致标 ⚠）'
                return '未配置副模型时，识别始终为单模型'
            from tkinter import trace as _tk_trace  # noqa: F401
            _sec_hint_lbl.configure(text=_sec_hint(sec_var))
            sec_var.trace_add('write', lambda *_a: _sec_hint_lbl.configure(text=_sec_hint(sec_var)))
        except Exception:
            pass
        from utils import get_secondary_model, save_secondary_model, get_api_config
        sec_var = tk.StringVar(self.win, value=get_secondary_model())
        # 副模型下拉框动态生成：从 providers 配置收集所有模型（model/custom_endpoint/history），
        # 合并常用默认去重——供应商新增模型（如 qwen3.5-ocr/qwen-vl-ocr 系列）自动同步，
        # 不再硬编码列表导致新模型选不到（v1.4 修复）
        _sec_models = ['glm-4v-flash', 'glm-4.6v', 'Doubao-Seed-2.1-pro',
                       'qwen3-omni-flash', 'qwen3.5-omni-flash', 'qwen3.5-ocr',
                       'qwen-vl-ocr', 'qwen-vl-ocr-latest']
        try:
            _acfg = get_api_config()
            _provs = (_acfg.get('providers') or {}) if isinstance(_acfg.get('providers'), dict) else {}
            for _pn, _pp in _provs.items():
                if not isinstance(_pp, dict):
                    continue
                for _k in ('model', 'custom_endpoint'):
                    _v = str(_pp.get(_k, '') or '').strip()
                    if _v and _v not in _sec_models:
                        _sec_models.append(_v)
                for _v in (_pp.get('model_history') or []):
                    _v = str(_v).strip()
                    if _v and _v not in _sec_models:
                        _sec_models.append(_v)
        except Exception:
            pass
        ttk.Combobox(sec_row, textvariable=sec_var, state='normal', width=20,  # (b-1): 22→20
                     values=_sec_models,
                     font=(self.FONT[0], 8)).pack(side="left", padx=8)
        def _save_sec():
            _v = sec_var.get().strip() or 'glm-4v-flash'
            save_secondary_model(_v)
            self.col_status_var.set(f"副模型已保存：{_v}")
            self.status_text.set(f"副模型已保存：{_v}")
        self._mk_btn(sec_row, '保存', _save_sec, kind='primary',
                  font=(self.FONT[0], 8)).pack(side="left")  # (b-1): 7→8 视觉对齐
        self._lbl(content, text="双模型验证时主模型识别后由副模型复核（不一致标 ⚠）",
                 font=(self.FONT[0], 8), fg=self.C_MUTED).pack(padx=20, pady=(0, 8))  # (b-1/a-3): 左缘对齐+8px

        # ── 授权管理──
        # B1：授权管理段锚点——取该函数创建的最后一个 pack(fill=x) 子 Frame
        self._build_license_card(content)
        try:
            _last_lic = None
            for _c in reversed(content.winfo_children()):
                if isinstance(_c, tk.Frame):
                    _info = _c.pack_info()
                    if _info.get('fill') == 'x' and _c not in self._general_anchors.values():
                        _last_lic = _c
                        break
            if _last_lic is not None:
                self._general_anchors['授权管理'] = _last_lic
        except Exception:
            pass

        # ── 补货策略──
        # B1：补货策略段锚点——同上取最后一个 fill=x 卡片
        self._build_replenishment_card(content)
        try:
            _last_rep = None
            for _c in reversed(content.winfo_children()):
                if isinstance(_c, tk.Frame):
                    _info = _c.pack_info()
                    if _info.get('fill') == 'x' and _c not in self._general_anchors.values():
                        _last_rep = _c
                        break
            if _last_rep is not None:
                self._general_anchors['补货策略'] = _last_rep
        except Exception:
            pass

        # ── 店铺管理──
        # B1：店铺管理段锚点——同上取最后一个 fill=x 卡片
        self._build_store_card(content)
        try:
            _last_store_card = None
            for _c in reversed(content.winfo_children()):
                if isinstance(_c, tk.Frame):
                    _info = _c.pack_info()
                    if _info.get('fill') == 'x' and _c not in self._general_anchors.values():
                        _last_store_card = _c
                        break
            if _last_store_card is not None:
                self._general_anchors['店铺管理'] = _last_store_card
        except Exception:
            pass

        # ── 导入与窗口（R1 流程效率 产出）──
        # 设置页卡：上次导入映射摘要 + 清除映射按钮 + 恢复上次窗口位置开关。
        # 状态变更走 Config.save（失败静默 + 状态栏提示）；settings_ui 本身
        # 在主线程可直调——不开 worker 线程。
        self._build_import_window_card(content)
        try:
            _last_iw = None
            for _c in reversed(content.winfo_children()):
                if isinstance(_c, tk.Frame):
                    _info = _c.pack_info()
                    if _info.get('fill') == 'x' and _c not in self._general_anchors.values():
                        _last_iw = _c
                        break
            if _last_iw is not None:
                self._general_anchors['导入与窗口'] = _last_iw
        except Exception:
            pass

        # ── 备份与恢复（R3 健壮闭环 产出）──
        # 三个按钮：导出设置备份 / 从备份恢复 / 历史库快照；恢复前 confirm 弹窗。
        self._build_backup_card(content)
        try:
            _last_bk = None
            for _c in reversed(content.winfo_children()):
                if isinstance(_c, tk.Frame):
                    _info = _c.pack_info()
                    if _info.get('fill') == 'x' and _c not in self._general_anchors.values():
                        _last_bk = _c
                        break
            if _last_bk is not None:
                self._general_anchors['备份与恢复'] = _last_bk
        except Exception:
            pass

        # B1：5 段段名锚点按钮行（页顶·滚动直达）
        try:
            _anchor_bar = tk.Frame(self.page_general, bg=self.C_BG)
            _anchor_bar.pack(side='top', fill='x', padx=16, pady=(8, 4), before=canvas)
            self._general_anchor_bar = _anchor_bar
            for _name in self._general_anchors.keys():
                self._mk_btn(_anchor_bar, _name,
                             lambda n=_name: self._jump_to_general_anchor(n),
                             kind='ghost', font=(self.FONT[0], 8)).pack(side='left', padx=2)
        except Exception:
            pass

    def _jump_to_general_anchor(self, name):
        """t8 B1：点击锚点按钮→实时计算目标段在 canvas scrollregion 内的 fraction，
        调 yview_moveto 跳转。免 resize 重算（v4f 修正）。"""
        try:
            _cv = getattr(self, '_general_canvas', None)
            _anchor = self._general_anchors.get(name)
            if _cv is None or _anchor is None:
                return
            # 实时计算：scrollregion 是 (x, y, w, h)；anchor.y 是相对 content 的 y；
            # fraction = anchor.y / scrollregion.h
            _cv.update_idletasks()
            _sr = _cv.cget('scrollregion')
            if not _sr:
                return
            _x, _y, _w, _h = (float(v) for v in str(_sr).split())
            if _h <= 0:
                return
            _ay = _anchor.winfo_y()
            _frac = _ay / _h
            # 留 8px 视觉余量（向上稍微多滚一点，让段名不被锚按钮行遮挡）
            _frac = max(0.0, _frac - 0.01)
            _cv.yview_moveto(_frac)
        except Exception:
            pass

    # ─────────────── 多店铺隔离：店铺管理卡片 ───────────────

    def _build_store_card(self, parent):
        """店铺管理卡片（t6 R1 布局优化版）：店铺列表（★=当前 + 行高亮）/
        新增 / 重命名 / 删除 / 设为当前。

        删除三选（askyesnocancel）：【是】删配置并清该店识别历史（history_db.
        delete_store，t1 产出）；【否】仅删配置保留历史；【取消】放弃。
        default 店铺禁删（store_registry 拒绝 + UI 前置提示，双保险）。

        布局要点：
        - 列表/按钮两段统一 padx=16，垂直节奏 4/6/8px 三档；
        - 4 个动作按钮按「操作对象在左、操作引导在右」分两行（新增/重命名 vs 设为当前/删除）；
        - 单店 / 未选中时 rename/activate/delete 自动禁掉（pure helper
          store_button_disabled_state 决定态，_refresh_store_card 同步）；
        - 当前店铺用 selectbackground (#FFF3B0) 高亮，比 ★ 字符更显眼。
        """
        card = tk.Frame(parent, bg=self.C_BG, highlightthickness=1,
                        highlightbackground=self.C_BORDER)
        card.pack(fill="x", padx=20, pady=8)
        self._lbl(card, text='店铺管理', font=self.FONT_HEADING, bg=self.C_BG,
                  fg=self.C_SECONDARY).pack(pady=(12, 2))
        self._lbl(card, text="多店铺数据隔离：各店铺的运输时效配置与识别历史分开保存",
                  font=(self.FONT[0], 8), fg=self.C_MUTED, bg=self.C_BG).pack()
        self._store_cur_lbl = self._lbl(card, text='', font=(self.FONT[0], 8),
                                        fg=self.C_TEXT, bg=self.C_BG)
        self._store_cur_lbl.pack(pady=(4, 2))

        mid = tk.Frame(card, bg=self.C_BG)
        mid.pack(fill='x', padx=16, pady=(4, 6))
        self._store_listbox = tk.Listbox(
            mid, height=5, font=(self.FONT[0], 9), relief='flat',
            highlightthickness=1, highlightbackground="#EAEAEA",
            highlightcolor="#EAEAEA", bg="#FFFFFF", fg=self.C_TEXT,
            exportselection=False, selectbackground="#FFF3B0")
        self._store_listbox.pack(side='left', fill='both', expand=True)
        # R1：列表选中态 → 重算 4 按钮禁用态（焦点切换时也响应）
        self._store_listbox.bind('<<ListboxSelect>>',
                                  lambda _e: self._refresh_store_button_states())

        # 新增 / 重命名 行：标签 + 输入 + 两个操作按钮（统一 grid 列对齐）
        new_row = tk.Frame(card, bg=self.C_BG)
        new_row.pack(fill='x', padx=16, pady=(2, 4))
        self._lbl(new_row, text="店铺名称:", font=(self.FONT[0], 8),
                  fg=self.C_MUTED, bg=self.C_BG).pack(side='left')
        self._store_new_var = tk.StringVar(self.win, value='')
        tk.Entry(new_row, textvariable=self._store_new_var, font=self.FONT, width=22,
                 relief='flat', bd=0, highlightthickness=1,
                 highlightbackground="#EAEAEA", highlightcolor="#EAEAEA",
                 bg="#FFFFFF", fg=self.C_TEXT,
                 insertbackground=self.C_TEXT).pack(side='left', padx=(6, 8))
        self._store_add_btn = self._mk_btn(new_row, '＋ 新增店铺', self._store_add,
                                            kind='primary', font=(self.FONT[0], 8))
        self._store_add_btn.pack(side='left', padx=(0, 4))
        self._store_rename_btn = self._mk_btn(new_row, '✎ 重命名选中', self._store_rename,
                                               kind='ghost', font=(self.FONT[0], 8))
        self._store_rename_btn.pack(side='left', padx=4)

        # 设为当前 / 删除行：两按钮 + 右侧引导文案
        btn_row = tk.Frame(card, bg=self.C_BG)
        btn_row.pack(fill='x', padx=16, pady=(0, 12))
        self._store_activate_btn = self._mk_btn(btn_row, '★ 设为当前店铺', self._store_activate,
                                                 kind='ghost', font=(self.FONT[0], 8))
        self._store_activate_btn.pack(side='left', padx=(0, 4))
        self._store_delete_btn = self._mk_btn(btn_row, '🗑 删除选中店铺', self._store_delete,
                                               kind='ghost', font=(self.FONT[0], 8))
        self._store_delete_btn.pack(side='left', padx=4)
        self._lbl(btn_row, text="重命名/删除前先在列表选中店铺；重命名需在上方输入新名称",
                  font=(self.FONT[0], 7), fg=self.C_MUTED, bg=self.C_BG).pack(
            side='left', padx=(8, 0))
        self._refresh_store_card()

    def _refresh_store_button_states(self):
        """R1：根据店铺总数 + 当前列表选中 → 同步 4 按钮禁用态（不依赖业务回调）。"""
        try:
            # 店铺数
            if store_registry is None:
                n = 0
            else:
                n = len(store_registry.get_stores() or [])
            # 当前选中
            sel_idx = -1
            try:
                sel = self._store_listbox.curselection()
                if sel:
                    sel_idx = int(sel[0])
            except Exception:
                sel_idx = -1
            st = self.store_button_disabled_state(n, sel_idx)
        except Exception:
            return
        for btn, key in ((getattr(self, '_store_add_btn', None), 'add'),
                         (getattr(self, '_store_rename_btn', None), 'rename'),
                         (getattr(self, '_store_activate_btn', None), 'activate'),
                         (getattr(self, '_store_delete_btn', None), 'delete')):
            if btn is None:
                continue
            try:
                btn.configure(state=('disabled' if st[key] else 'normal'))
            except Exception:
                pass

    def _store_selected(self):
        """列表选中项 → (store_id, store_name)；未选中/越界返回 (None, None)。"""
        try:
            sel = self._store_listbox.curselection()
        except Exception:
            return None, None
        if not sel or store_registry is None:
            return None, None
        stores = store_registry.get_stores()
        idx = sel[0]
        if 0 <= idx < len(stores):
            return stores[idx]['id'], stores[idx]['name']
        return None, None

    def _refresh_store_card(self):
        """重绘店铺列表 + 当前店铺显示；同步主界面切换器（App 方法，容错）。

        R1：列表行文本走 store_list_row_label 统一（与 ★ 高亮规则一致），
        当前店铺直接 selection_set 让 selectbackground 立即生效（用户进设置页
        就能看见高亮，不用先点一次），并触发按钮禁用态同步。
        """
        if store_registry is None:
            try:
                self._store_cur_lbl.config(text='店铺模块缺失（增量更新不完整），店铺管理停用')
            except Exception:
                pass
            return
        stores = store_registry.get_stores()
        active = store_registry.get_active()
        # R1：列表填充 + 当前店铺高亮（selectbackground）
        try:
            self._store_listbox.delete(0, 'end')
            for s in stores:
                txt = self.store_list_row_label(s.get('name', ''), s.get('id', ''),
                                                active)
                self._store_listbox.insert('end', txt)
            act_idx = self.store_list_active_index(stores, active)
            if act_idx >= 0:
                # 仅首次设置选中（用户切走时不强制抢回焦点）
                if not self._store_listbox.curselection():
                    try:
                        self._store_listbox.selection_set(act_idx)
                        self._store_listbox.activate(act_idx)
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            self._store_cur_lbl.config(
                text=f"当前店铺：{store_registry.get_store_name(active) or '默认店铺'}"
                     f"（共 {len(stores)} 家）")
        except Exception:
            pass
        # 按钮禁用态同步（单店/未选时锁住 rename/activate/delete）
        self._refresh_store_button_states()
        # 主界面切换器同步（主窗口一定已构建；widget 缺失时静默跳过）
        try:
            self._refresh_store_combo()
        except Exception:
            pass

    def _store_add(self):
        """新增店铺（名称取输入框；允许重名，id 权威）。"""
        if store_registry is None:
            return
        nm = self._store_new_var.get().strip()
        if not nm:
            messagebox.showwarning("名称为空", "请先在输入框填写新店铺名称。", parent=self.win)
            return
        item = store_registry.add_store(nm)
        if not item:
            messagebox.showerror("新增失败", "店铺新增失败（详见日志 ocr_dlog.txt）。",
                                 parent=self.win)
            return
        self._store_new_var.set('')
        self.status_text.set(f"已新增店铺「{item['name']}」")
        self._refresh_store_card()

    def _store_rename(self):
        """重命名选中店铺（输入框为空则提示；id 永不变）。"""
        if store_registry is None:
            return
        sid, name = self._store_selected()
        if not sid:
            messagebox.showwarning("未选中", "请先在列表中选中要重命名的店铺。", parent=self.win)
            return
        new_name = self._store_new_var.get().strip()
        if not new_name:
            messagebox.showwarning("名称为空", "请先在输入框填写新名称（重命名不改 id）。",
                                   parent=self.win)
            return
        if not store_registry.rename_store(sid, new_name):
            messagebox.showerror("重命名失败", "店铺重命名失败（详见日志 ocr_dlog.txt）。",
                                 parent=self.win)
            return
        self._store_new_var.set('')
        self.status_text.set(f"店铺「{name}」已重命名为「{new_name}」")
        self._refresh_store_card()

    def _store_activate(self):
        """把选中店铺设为当前店铺（走 _apply_store_switch：全量重建主界面，DESIGN §3）。"""
        if store_registry is None:
            return
        sid, name = self._store_selected()
        if not sid:
            messagebox.showwarning("未选中", "请先在列表中选中要启用的店铺。", parent=self.win)
            return
        if sid == getattr(self, '_store_id', None):
            messagebox.showinfo("已是当前店铺", f"「{name}」已经是当前店铺。", parent=self.win)
            return
        if self._apply_store_switch(sid):
            self.status_text.set(f"已切换到店铺「{name}」")
            self._refresh_store_card()
        else:
            messagebox.showerror("切换失败", "店铺切换失败（详见日志 ocr_dlog.txt）。",
                                 parent=self.win)

    def _store_delete(self):
        """删除选中店铺：三选（删配置+清历史 / 仅删配置保留历史 / 取消）。

        - 【是】store_registry.delete_store 成功后调 history_db.delete_store(id)
          联动清历史（t1 契约：返回删行数）；
        - 若删的是当前店铺，注册表已把 active 回落 default，主界面同步切换重建；
        - default 店铺 UI 前置拒绝 + store_registry 兜底拒绝（双保险）。
        """
        if store_registry is None:
            return
        sid, name = self._store_selected()
        if not sid:
            messagebox.showwarning("未选中", "请先在列表中选中要删除的店铺。", parent=self.win)
            return
        if sid == 'default':
            messagebox.showwarning("不可删除", "「默认店铺」是系统内置店铺，不能删除。",
                                   parent=self.win)
            return
        ans = messagebox.askyesnocancel(
            "确认删除",
            f"确定删除店铺「{name}」？\n\n"
            "【是】删除配置，并同时清除该店铺的全部识别历史；\n"
            "【否】仅删除配置，保留历史数据（趋势仍可见）；\n"
            "【取消】放弃删除。",
            parent=self.win)
        if ans is None:
            return
        deleted = store_registry.delete_store(sid)
        if deleted is None:
            messagebox.showerror("删除失败", "店铺删除失败（详见日志 ocr_dlog.txt）。",
                                 parent=self.win)
            return
        if ans is True:
            try:
                import history_db as _hdb
                _n = _hdb.delete_store(sid)
                msg = (f"店铺「{deleted}」已删除（含历史 {_n} 行）"
                       if _n >= 0 else f"店铺「{deleted}」已删除（历史清理失败，见日志）")
            except Exception:
                msg = f"店铺「{deleted}」已删除（历史清理失败，见日志）"
        else:
            msg = f"店铺「{deleted}」已删除（历史数据已保留）"
        self.status_text.set(msg)
        # 删的是当前店铺 → 注册表已回落 default，主界面同步切换重建
        if sid == getattr(self, '_store_id', None):
            self._apply_store_switch(store_registry.get_active())
        self._refresh_store_card()

    # ─────────────── R1 流程效率：导入与窗口卡片 ───────────────

    @staticmethod
    def _import_summary_lines(mapping, saved_at):
        """上次导入映射摘要（多行），供 UI 渲染。

        纯函数 / 无 Tk 依赖，便于单测。
        - mapping 非 dict 或为空 → 始终返回「暂无上次导入记录」。
        - 否则每行一条「字段: 列名」，字段名走中文显示；尾部追加保存时间（可省略）。
        """
        # 字段 → 中文标签（与导入向导/识别列配置用词保持一致）
        _LABELS = {
            'name': '名称', 'stock': '库存', 'sales': '销量',
            'region': '销售区域', 'warehouse': '仓库',
        }
        if not isinstance(mapping, dict) or not mapping:
            return ['（暂无上次导入记录）']
        lines = []
        # 顺序固定：核心字段优先（name/stock/sales），其他字段按字母序追加
        keys = [k for k in ('name', 'stock', 'sales') if k in mapping]
        keys += sorted(k for k in mapping.keys() if k not in keys)
        for k in keys:
            v = mapping.get(k)
            if not (isinstance(v, str) and v.strip()):
                continue
            label = _LABELS.get(k, k)
            lines.append(f'{label}:{v}')
        if saved_at and isinstance(saved_at, str):
            lines.append(f'（保存于 {saved_at}）')
        if not lines:
            return ['（暂无上次导入记录）']
        return lines

    def _build_import_window_card(self, parent):
        """R1 流程效率 t2：导入与窗口设置卡。

        内容：
        - 上次导入映射摘要（多行 Label，刷新逻辑内置）。
        - 「清除映射」按钮：调 import_memory.clear_last_mapping → 状态栏提示 + 摘要刷新。
        - 「恢复上次窗口位置」开关：走 Config.save('restore_window_pos'=bool)。
          默认开（与现有「实时截图窗口恢复」（DESIGN §7）一致——主线程 2 秒无条件恢复）。
          关掉后窗口位置不主动恢复（沿用默认几何）。
        """
        card = tk.Frame(parent, bg=self.C_BG, highlightthickness=1,
                        highlightbackground=self.C_BORDER)
        card.pack(fill='x', padx=20, pady=8)
        self._lbl(card, text='导入与窗口', font=self.FONT_HEADING, bg=self.C_BG,
                  fg=self.C_SECONDARY).pack(pady=(12, 2))
        self._lbl(card, text='上次导入的列映射（CSV/XLSX 导入后自动记忆，下次复用）',
                  font=(self.FONT[0], 8), fg=self.C_MUTED, bg=self.C_BG).pack()

        # ── 摘要区 ──
        summary_row = tk.Frame(card, bg=self.C_BG)
        summary_row.pack(fill='x', padx=16, pady=(8, 4))
        self._import_summary_var = tk.StringVar(self.win, value='')
        # 用单 Label 显示多行（wraplength 控制宽度，justify 左对齐）
        self._import_summary_lbl = self._lbl(
            summary_row, textvariable=self._import_summary_var,
            font=(self.FONT[0], 9), fg=self.C_TEXT, bg=self.C_BG,
            justify='left', anchor='w', wraplength=520)
        self._import_summary_lbl.pack(side='left', fill='x', expand=True)

        def _refresh_import_summary():
            """读 import_memory.get_last_mapping + saved_at → 摘要文案。"""
            try:
                from import_memory import get_last_mapping
                mapping = get_last_mapping() or {}
            except Exception:
                mapping = {}
            saved_at = ''
            try:
                cfg = Config.load() if hasattr(Config, 'load') else {}
                node = cfg.get('import_memory') if isinstance(cfg, dict) else None
                if isinstance(node, dict):
                    saved_at = str(node.get('saved_at') or '')
            except Exception:
                saved_at = ''
            try:
                lines = self._import_summary_lines(mapping, saved_at)
            except Exception:
                lines = ['（暂无上次导入记录）']
            self._import_summary_var.set('\n'.join(lines))

        _refresh_import_summary()

        # ── 清除映射按钮 ──
        btn_row = tk.Frame(card, bg=self.C_BG)
        btn_row.pack(fill='x', padx=16, pady=(2, 6))
        def _on_clear_mapping():
            try:
                from import_memory import clear_last_mapping
                ok = bool(clear_last_mapping())
            except Exception:
                ok = False
            try:
                _refresh_import_summary()
            except Exception:
                pass
            if ok:
                try:
                    self.status_text.set('已清除上次导入映射')
                except Exception:
                    pass
            else:
                try:
                    messagebox.showwarning(
                        '清除失败', '清除映射失败（详见日志 ocr_dlog.txt）',
                        parent=self.win)
                except Exception:
                    pass
        self._mk_btn(btn_row, '清除映射', _on_clear_mapping, kind='ghost',
                  font=(self.FONT[0], 8)).pack(side='left')

        # 把 refresh 挂到 self 上，外部导入后也可触发刷新（其它设置卡会读到）
        self._refresh_import_summary = _refresh_import_summary

        # ── 恢复上次窗口位置 开关 ──
        win_row = tk.Frame(card, bg=self.C_BG)
        win_row.pack(fill='x', padx=16, pady=(4, 4))
        self._restore_win_pos_var = tk.BooleanVar(self.win, value=True)
        # 读当前值（首次构建卡时）
        try:
            _cur = Config.load() if hasattr(Config, 'load') else {}
            _val = (_cur.get('window') or {}).get('restore_last_pos', True) if isinstance(_cur, dict) else True
            self._restore_win_pos_var.set(bool(_val))
        except Exception:
            self._restore_win_pos_var.set(True)
        def _on_restore_toggle():
            try:
                cfg = Config.load() if hasattr(Config, 'load') else {}
                if not isinstance(cfg, dict):
                    cfg = {}
                win_node = cfg.get('window') or {}
                if not isinstance(win_node, dict):
                    win_node = {}
                win_node['restore_last_pos'] = bool(self._restore_win_pos_var.get())
                cfg['window'] = win_node
                Config.save(cfg)
                try:
                    self.status_text.set(
                        f"恢复窗口位置：{'开启' if bool(self._restore_win_pos_var.get()) else '关闭'}")
                except Exception:
                    pass
            except Exception:
                # -13) 同款风格：写盘失败回滚 UI（避免"显示新值但磁盘未写入"）
                try:
                    self._restore_win_pos_var.set(not bool(self._restore_win_pos_var.get()))
                except Exception:
                    pass
                try:
                    self.status_text.set('恢复窗口位置设置保存失败（详见日志 ocr_dlog.txt）')
                except Exception:
                    pass
        tk.Checkbutton(win_row, text='恢复上次窗口位置（关闭后启动用默认位置）',
                       variable=self._restore_win_pos_var, command=_on_restore_toggle,
                       font=(self.FONT[0], 9), bg=self.C_BG, fg=self.C_TEXT,
                       selectcolor=self.C_BG, activebackground=self.C_BG).pack(side='left')

        self._lbl(card,
                  text=('说明：导入 CSV/XLSX 后会自动记忆列映射，下次同结构文件导入时直接复用；'
                        '窗口位置开关仅影响启动时是否恢复（最小化最多 2 秒无条件恢复，遵循 DESIGN §7）。'),
                  font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_MUTED,
                  wraplength=560, justify='left').pack(pady=(4, 12), padx=20, anchor='w')

    # ─────────────── R3 健壮闭环：备份与恢复卡片 ───────────────

    def _build_backup_card(self, parent):
        """R3 健壮闭环 t9：备份与恢复卡。

        三个动作按钮 + 结果状态行：
        - 「导出设置备份」：弹保存对话框 → 调 backup_store.export_settings_zip
          （含 history.db 快照开关）→ 状态栏反馈。
        - 「从备份恢复」：弹打开对话框 → confirm 弹窗 → 调 backup_store.restore_settings_zip
          → 状态栏反馈（含 .pre_restore 文件提示）。
        - 「历史库快照」：调 backup_store.snapshot_history_db → 状态栏反馈路径。

        全部 IO 走 backup_store 纯逻辑模块；状态栏 + 错误弹窗由本卡负责
        （不弹、不写文件、不调 UI）。
        """
        card = tk.Frame(parent, bg=self.C_BG, highlightthickness=1,
                        highlightbackground=self.C_BORDER)
        card.pack(fill='x', padx=20, pady=8)
        self._lbl(card, text='备份与恢复', font=self.FONT_HEADING, bg=self.C_BG,
                  fg=self.C_SECONDARY).pack(pady=(12, 2))
        self._lbl(card, text='一键打包配置 / 从历史包恢复 / 单独历史库快照',
                  font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_MUTED).pack()

        # 历史库快照选项（导出时可同时打包 history.db）
        include_hist_row = tk.Frame(card, bg=self.C_BG)
        include_hist_row.pack(fill='x', padx=16, pady=(8, 2))
        _include_hist_var = tk.BooleanVar(self.win, value=False)
        tk.Checkbutton(include_hist_row,
                       text='导出时包含 history.db 快照（体积较大，按需开启）',
                       variable=_include_hist_var,
                       font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_TEXT,
                       selectcolor=self.C_BG,
                       activebackground=self.C_BG).pack(side='left')

        # 三个按钮行
        btn_row = tk.Frame(card, bg=self.C_BG)
        btn_row.pack(fill='x', padx=16, pady=(4, 4))

        def _set_status(msg):
            try:
                _status_var.set(str(msg or ''))
            except Exception:
                pass

        _status_var = tk.StringVar(self.win, value='')

        def _on_export():
            """导出设置备份（弹保存对话框 → export_settings_zip）。"""
            try:
                from tkinter import filedialog
                from backup_store import export_settings_zip
                default_name = f'pdd_ez_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip'
                target = filedialog.asksaveasfilename(
                    title='导出设置备份',
                    defaultextension='.zip',
                    initialfile=default_name,
                    filetypes=[('Zip 备份', '*.zip'), ('所有文件', '*.*')],
                    parent=self.win)
                if not target:
                    return  # 用户取消
                r = export_settings_zip(
                    target, include_history_db=bool(_include_hist_var.get()))
            except Exception as e:
                try:
                    messagebox.showerror('导出失败', str(e)[:200],
                                         parent=self.win)
                except Exception:
                    pass
                return
            if r is None:
                _set_status('导出失败：路径无效')
                try:
                    messagebox.showerror('导出失败', '路径无效或不可写',
                                         parent=self.win)
                except Exception:
                    pass
                return
            err = r.get('error')
            if err:
                _set_status(f'⚠ 导出部分成功：{err}')
                try:
                    messagebox.showwarning(
                        '导出（部分成功）',
                        f'已生成备份但有警告：\n{err}\n\n'
                        f'路径：{r.get("path")}\n'
                        f'文件：{", ".join(r.get("files") or []) or "（无）"}',
                        parent=self.win)
                except Exception:
                    pass
            else:
                n = len(r.get('files') or [])
                size = r.get('size_bytes', 0)
                _set_status(
                    f'✅ 已导出 {n} 个文件 ({size} 字节) → {r.get("path")}')
                try:
                    self.status_text.set(
                        f'设置备份已导出：{r.get("path")}')
                except Exception:
                    pass

        def _on_restore():
            """从备份恢复（弹打开对话框 → confirm → restore_settings_zip）。"""
            try:
                from tkinter import filedialog
                from backup_store import restore_settings_zip
                target = filedialog.askopenfilename(
                    title='选择备份 zip',
                    filetypes=[('Zip 备份', '*.zip'), ('所有文件', '*.*')],
                    parent=self.win)
                if not target:
                    return  # 用户取消
                ans = messagebox.askyesnocancel(
                    '确认恢复',
                    f'从备份恢复会覆盖当前 settings.json / regions.json。\n'
                    f'恢复前会自动备份现有文件为 .pre_restore 后缀。\n\n'
                    f'备份：{target}\n\n【是】覆盖现有配置；【否】取消。',
                    parent=self.win)
                if not ans:
                    return
                r = restore_settings_zip(target)
            except Exception as e:
                try:
                    messagebox.showerror('恢复失败', str(e)[:200],
                                         parent=self.win)
                except Exception:
                    pass
                return
            err = r.get('error')
            restored = r.get('restored') or []
            pre = r.get('pre_restore') or []
            if err:
                _set_status(f'❌ 恢复失败：{err}')
                try:
                    messagebox.showerror(
                        '恢复失败',
                        f'{err}\n\n当前配置未改动。',
                        parent=self.win)
                except Exception:
                    pass
                return
            # 成功
            pre_txt = f'（.pre_restore: {", ".join(pre)}）' if pre else '（无原文件）'
            _set_status(
                f'✅ 已恢复 {len(restored)} 个文件 {pre_txt}')
            try:
                self.status_text.set(
                    f'配置已从备份恢复（{len(restored)} 个文件）')
            except Exception:
                pass
            try:
                messagebox.showinfo(
                    '恢复完成',
                    f'已恢复 {len(restored)} 个文件：\n  '
                    + '\n  '.join(restored)
                    + (f'\n\n原文件已备份为：\n  {", ".join(pre)}'
                       if pre else '\n\n（无原文件需备份）')
                    + '\n\n部分设置（如 DPI/窗口位置）可能需重启程序生效。',
                    parent=self.win)
            except Exception:
                pass
            # R3 遗留修复（R3-Risk-B）：恢复写盘后显式失效进程内缓存——
            # license tier 缓存（TTL 300s，reset_cache 显式清空）、usage enabled 缓存、
            # Config mtime 缓存由 save 自清。均失败安全（缓存不失效也只是短暂陈旧）。
            try:
                from auth import license as _lic
                _lic.reset_cache()
            except Exception:
                pass
            try:
                import usage_store as _us
                _us._invalidate_enabled_cache()
            except Exception:
                pass

        def _on_snapshot():
            """单独历史库快照（snapshot_history_db → <base>/backups/history_<stamp>.db）。"""
            try:
                from backup_store import snapshot_history_db
                snap = snapshot_history_db()
            except Exception as e:
                try:
                    messagebox.showerror('快照失败', str(e)[:200],
                                         parent=self.win)
                except Exception:
                    pass
                return
            if not snap:
                _set_status('⚠ 快照失败或 history.db 不存在')
                try:
                    messagebox.showwarning(
                        '快照失败',
                        'history.db 不存在或快照失败（详见日志）。',
                        parent=self.win)
                except Exception:
                    pass
                return
            try:
                size = os.path.getsize(snap) if os.path.isfile(snap) else 0
            except Exception:
                size = 0
            _set_status(f'✅ 历史库快照 ({size} 字节) → {snap}')
            try:
                self.status_text.set(f'历史库快照：{snap}')
            except Exception:
                pass

        self._mk_btn(btn_row, '📦 导出设置备份', _on_export, kind='primary',
                  font=(self.FONT[0], 8)).pack(side='left', padx=(0, 4))
        self._mk_btn(btn_row, '♻ 从备份恢复', _on_restore, kind='ghost',
                  font=(self.FONT[0], 8)).pack(side='left', padx=4)
        self._mk_btn(btn_row, '📸 历史库快照', _on_snapshot, kind='ghost',
                  font=(self.FONT[0], 8)).pack(side='left', padx=4)

        # 状态行
        status_row = tk.Frame(card, bg=self.C_BG)
        status_row.pack(fill='x', padx=16, pady=(2, 8))
        self._lbl(status_row, textvariable=_status_var,
                  font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_TEXT,
                  justify='left', anchor='w', wraplength=520).pack(
            side='left', fill='x', expand=True)

        # 说明
        self._lbl(card,
                  text=('说明：导出 → 把当前 settings.json / regions.json（可选 history.db）'
                        '打包到一个 zip；恢复 → 校验 zip + JSON 合法性后原子写回，'
                        '覆盖前会自动备份原文件为 .pre_restore；快照 → SQLite VACUUM INTO '
                        '一致性快照（不锁库）。所有异常路径绝不外抛，由本卡弹窗/状态栏反馈。'),
                  font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_MUTED,
                  wraplength=560, justify='left').pack(pady=(0, 12), padx=20,
                                                       anchor='w')

    def _build_replenishment_card(self, parent):
        """t13 P3-A 补货策略卡片：模型单选（经典/加权/高级）+ safety_days + in_transit_qty。

        t8 P2-C：新增「高级」选项（calc_replenishment_advanced），展开四因子编辑区：
        - 大促日历（日期文本框 + boost + lead_days）
        - 滞销阈值（threshold_per_day + stock_ratio）
        - 季节系数（开关）
        - 超卖（high_ratio）
        高级模式下默认全部 enabled=False 静默关闭——用户勾选/填值才生效。
        用户裁定：默认 'classic'（一行公式逻辑都不许改）；切到 'weighted' 时
        会按 sku_id 关联历史库做加权日销，无历史自动回退经典并标注「经典(无历史)」。
        """
        try:
            from utils import get_replenishment_cfg, MODEL_CLASSIC, MODEL_WEIGHTED, MODEL_ADVANCED
        except Exception:
            self._lbl(parent, text="补货策略模块加载失败（utils）",
                     fg=self.C_MUTED).pack(pady=8)
            return

        card = tk.Frame(parent, bg=self.C_BG, highlightthickness=1,
                        highlightbackground=self.C_BORDER)
        card.pack(fill="x", padx=20, pady=8)
        self._lbl(card, text='补货策略', font=self.FONT_HEADING, bg=self.C_BG,
                  fg=self.C_SECONDARY).pack(pady=(12, 2))

        # 当前配置读出
        try:
            cur = get_replenishment_cfg()
        except Exception:
            cur = {'model': 'classic', 'safety_days': 2, 'in_transit_qty': 0,
                   'advanced': {
                       'promo': {'dates': [], 'boost': 1.5, 'lead_days': 3, 'enabled': False},
                       'slow': {'threshold_per_day': 1.0, 'stock_ratio': 5.0, 'enabled': False},
                       'season': {'enabled': False},
                       'oversell': {'high_ratio': 0.5, 'enabled': False},
                   }}
        cur_adv = cur.get('advanced') if isinstance(cur.get('advanced'), dict) else {
            'promo': {'dates': [], 'boost': 1.5, 'lead_days': 3, 'enabled': False},
            'slow': {'threshold_per_day': 1.0, 'stock_ratio': 5.0, 'enabled': False},
            'season': {'enabled': False},
            'oversell': {'high_ratio': 0.5, 'enabled': False},
        }
        cur_promo = cur_adv.get('promo') or {}
        cur_slow = cur_adv.get('slow') or {}
        cur_season = cur_adv.get('season') or {}
        cur_over = cur_adv.get('oversell') or {}

        # 模型单选
        _model_var = tk.StringVar(self.win, value=str(cur.get('model', 'classic')))
        model_row = tk.Frame(card, bg=self.C_BG); model_row.pack(pady=(6, 2), padx=20, fill='x')
        self._lbl(model_row, text="补货模型：", font=(self.FONT[0], 9),
                  bg=self.C_BG, fg=self.C_TEXT).pack(side='left', padx=(0, 6))
        tk.Radiobutton(model_row, text='经典（原公式，默认）', variable=_model_var, value='classic',
                       font=(self.FONT[0], 9), bg=self.C_BG, fg=self.C_TEXT,
                       selectcolor=self.C_BG, activebackground=self.C_BG).pack(side='left')
        tk.Radiobutton(model_row, text='加权（0.5×7日+0.3×14日+0.2×30日）',
                       variable=_model_var, value='weighted',
                       font=(self.FONT[0], 9), bg=self.C_BG, fg=self.C_TEXT,
                       selectcolor=self.C_BG, activebackground=self.C_BG).pack(side='left', padx=(8, 0))
        # 高级模式 radio
        tk.Radiobutton(model_row, text='高级（季节/大促/滞销/超卖）',
                       variable=_model_var, value=MODEL_ADVANCED,
                       font=(self.FONT[0], 9), bg=self.C_BG, fg=self.C_TEXT,
                       selectcolor=self.C_BG, activebackground=self.C_BG).pack(side='left', padx=(8, 0))

        # safety_days spinbox
        sd_row = tk.Frame(card, bg=self.C_BG); sd_row.pack(pady=(6, 2), padx=20, fill='x')
        self._lbl(sd_row, text="安全库存天数：", font=(self.FONT[0], 9),
                  bg=self.C_BG, fg=self.C_TEXT).pack(side='left', padx=(0, 6))
        _sd_var = tk.IntVar(self.win, value=int(cur.get('safety_days', 2) or 0))
        tk.Spinbox(sd_row, from_=0, to=30, textvariable=_sd_var, width=8,  # (e-2): 6→8
                   font=(self.FONT[0], 9), relief='flat', bd=0, highlightthickness=1,
                   highlightbackground="#EAEAEA", highlightcolor="#EAEAEA",
                   bg="#FFFFFF", fg=self.C_TEXT, buttonbackground=self.C_BG).pack(side='left')
        self._lbl(sd_row, text="（加权/高级模式：运输+此值=到货覆盖天数）",
                  font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_MUTED).pack(side='left', padx=(8, 0))

        # R2 安全库存推荐 — 显示上次缓存 + 一键应用按钮
        # 写入方：fix-glm 在 _calc_from_items 后把 recommend_safety_days 结果写
        # settings['replenishment']['recommendation']；本卡读出展示。
        rec_row = tk.Frame(card, bg=self.C_BG)
        rec_row.pack(pady=(2, 2), padx=20, fill='x')
        rec_var = tk.StringVar(self.win, value='')

        def _refresh_recommendation():
            """读 algorithm_ui.load_recommendation_cache → 更新摘要 + 按钮态。"""
            try:
                from algorithm_ui import load_recommendation_cache
                node = load_recommendation_cache()
            except Exception:
                node = None
            if not isinstance(node, dict):
                try:
                    rec_var.set('（无上次推荐——点「识别+补货」后自动计算）')
                except Exception:
                    pass
                try:
                    _apply_btn.configure(state='disabled')
                except Exception:
                    pass
                return
            try:
                sd = int(node.get('safety_days') or 0)
            except Exception:
                sd = 0
            try:
                lead = int(node.get('safety_days_lead') or 0)
            except Exception:
                lead = 0
            sigma = node.get('sigma') or 0.0
            forecast = node.get('forecast') or 0.0
            n_samples = node.get('n_samples') or 0
            computed_at = str(node.get('computed_at') or '')
            try:
                rec_var.set(
                    f'上次推荐：{sd} 天（基于运输 {lead} 天 · σ≈{float(sigma):.2f} · '
                    f'样本 {n_samples} 天 · 预测日销 ≈{float(forecast):.2f}'
                    + (f' · {computed_at}' if computed_at else '')
                    + '）'
                )
            except Exception:
                pass
            try:
                _apply_btn.configure(state='normal')
            except Exception:
                pass

        self._lbl(rec_row, textvariable=rec_var, font=(self.FONT[0], 8),
                  bg=self.C_BG, fg=self.C_MUTED, justify='left',
                  anchor='w', wraplength=420).pack(side='left', fill='x', expand=True)

        def _on_apply_recommendation():
            """把缓存推荐值一键写入 _sd_var + 状态栏提示。"""
            try:
                from algorithm_ui import load_recommendation_cache
                node = load_recommendation_cache()
            except Exception:
                node = None
            if not isinstance(node, dict):
                try:
                    self.status_text.set('当前无推荐缓存')
                except Exception:
                    pass
                return
            try:
                sd = int(node.get('safety_days') or 0)
            except Exception:
                sd = 0
            if sd <= 0:
                try:
                    self.status_text.set('推荐缓存无效')
                except Exception:
                    pass
                return
            sd = max(0, min(30, sd))
            try:
                _sd_var.set(sd)
            except Exception:
                pass
            try:
                self.status_text.set(
                    f'已应用推荐 safety_days = {sd}（记得点「保存」持久化）')
            except Exception:
                pass

        _apply_btn = self._mk_btn(rec_row, '一键应用', _on_apply_recommendation,
                                  kind='ghost', font=(self.FONT[0], 8))
        _apply_btn.pack(side='left', padx=(6, 0))
        self._refresh_recommendation = _refresh_recommendation
        _refresh_recommendation()

        # in_transit_qty spinbox
        it_row = tk.Frame(card, bg=self.C_BG); it_row.pack(pady=(6, 2), padx=20, fill='x')
        self._lbl(it_row, text="在途库存：", font=(self.FONT[0], 9),
                  bg=self.C_BG, fg=self.C_TEXT).pack(side='left', padx=(0, 6))
        _it_var = tk.IntVar(self.win, value=int(cur.get('in_transit_qty', 0) or 0))
        tk.Spinbox(it_row, from_=0, to=100000, textvariable=_it_var, width=8,
                   font=(self.FONT[0], 9), relief='flat', bd=0, highlightthickness=1,
                   highlightbackground="#EAEAEA", highlightcolor="#EAEAEA",
                   bg="#FFFFFF", fg=self.C_TEXT, buttonbackground=self.C_BG).pack(side='left')
        self._lbl(it_row, text="（加权/高级模式：补货量 = (运输+安全)×日销 − 在途 − 库存，100 取整）",
                  font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_MUTED).pack(side='left', padx=(8, 0))

        # 高级模式编辑区（折叠在独立 Frame，按 radio 切换可见）
        # R1 布局优化：四个 LabelFrame 走统一 padx=8 / pady=(0, 6) + 内层 padx=8 / pady=4
        # 三档节奏；标题统一前缀「▌ <name>（<desc>）」便于快速扫读；季节+超卖同
        # 行仍保留（节省垂直空间，但季节/超卖 box 内部都用 pack，与其他两卡同节奏）。
        adv_frame = tk.Frame(card, bg=self.C_BG)
        adv_frame.pack(pady=(6, 4), padx=20, fill='x')
        self._lbl(adv_frame, text="高级因子（仅「高级」模型生效，缺省全部关闭）",
                  font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_MUTED).pack(
            anchor='w', pady=(2, 4))

        # 公共 LabelFrame 风格：padding 内 padx=8 / pady=4，标题前缀统一
        def _make_factor_box(parent, title, desc):
            """构造一个高级因子卡片——统一标题风格 + 内层节奏（便于四卡视觉对齐）。"""
            box = tk.LabelFrame(
                parent, text=f" {title}（{desc}）",
                bg=self.C_BG, fg=self.C_TEXT, font=(self.FONT[0], 8, 'bold'),
                bd=1, relief='solid', labelanchor='nw', padx=8, pady=6)
            return box

        # ── 大促日历 ──
        promo_box = _make_factor_box(
            adv_frame, "▌ 大促日历", "命中日期 ±lead_days 窗口内 → boost 倍")
        promo_box.pack(fill='x', padx=8, pady=(0, 6))
        _promo_enabled_var = tk.BooleanVar(self.win, value=bool(cur_promo.get('enabled', False)))
        tk.Checkbutton(promo_box, text='启用', variable=_promo_enabled_var,
                       font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_TEXT,
                       selectcolor=self.C_BG, activebackground=self.C_BG).pack(
            anchor='w', padx=4, pady=(0, 4))
        date_row = tk.Frame(promo_box, bg=self.C_BG); date_row.pack(fill='x', pady=2)
        self._lbl(date_row, text="日期（逗号/空格分隔）:", font=(self.FONT[0], 8),
                  bg=self.C_BG, fg=self.C_TEXT).pack(side='left')
        _promo_dates_text = ', '.join(cur_promo.get('dates') or [])
        _promo_dates_var = tk.StringVar(self.win, value=_promo_dates_text)
        tk.Entry(date_row, textvariable=_promo_dates_var, font=(self.FONT[0], 9), width=36,
                 relief='flat', bd=0, highlightthickness=1,
                 highlightbackground="#EAEAEA", highlightcolor="#EAEAEA",
                 bg="#FFFFFF", fg=self.C_TEXT,
                 insertbackground=self.C_TEXT).pack(side='left', padx=(6, 0))

        # R2 批量粘贴入口（多行 YYYY-MM-DD 一次粘入）——
        # 弹文本域对话框，按行解析 + 逐行错误标注；合法行替换到 _promo_dates_var。
        def _on_bulk_paste():
            try:
                from algorithm_ui import parse_bulk_promo_dates
                dlg = tk.Toplevel(self.win)
                dlg.title('批量粘贴大促日期')
                dlg.configure(bg=self.C_BG)
                try:
                    dlg.geometry(self._geo(420, 320))
                except Exception:
                    dlg.geometry('420x320')
                self._lbl(dlg, text='每行一个 YYYY-MM-DD（可混合逗号/空格/分号）',
                          font=(self.FONT[0], 8), bg=self.C_BG,
                          fg=self.C_MUTED).pack(pady=(8, 2))
                txt = tk.Text(dlg, font=(self.FONT[0], 9), height=10, width=46,
                              relief='flat', bd=0, highlightthickness=1,
                              highlightbackground="#EAEAEA", highlightcolor="#EAEAEA",
                              bg="#FFFFFF", fg=self.C_TEXT,
                              insertbackground=self.C_TEXT, wrap='word')
                txt.pack(padx=10, pady=(2, 6), fill='both', expand=True)
                # 预填当前已有日期（每行一个），方便追加
                try:
                    existing = _promo_dates_var.get()
                    if existing:
                        txt.insert('1.0', existing.replace(', ', '\n'))
                except Exception:
                    pass
                status_var = tk.StringVar(dlg, value='')
                status_lbl = self._lbl(dlg, textvariable=status_var,
                                       font=(self.FONT[0], 8), bg=self.C_BG,
                                       fg=self.C_TEXT, justify='left', anchor='w',
                                       wraplength=400)
                status_lbl.pack(padx=10, fill='x')

                def _apply():
                    try:
                        raw = txt.get('1.0', 'end')
                        valid, invalid, total = parse_bulk_promo_dates(raw)
                    except Exception as e:
                        try:
                            messagebox.showerror('解析失败', str(e)[:200],
                                                 parent=dlg)
                        except Exception:
                            pass
                        return
                    try:
                        _promo_dates_var.set(', '.join(valid))
                    except Exception:
                        pass
                    msg = f'已应用 {len(valid)} 个日期'
                    if total > 0:
                        msg += f'（共 {total} 行'
                        if invalid:
                            msg += f'，非法 {len(invalid)} 行：'
                            shown = invalid[:5]
                            msg += '; '.join(
                                f'第{ln}行:{t[:30]}' for ln, t in shown)
                            if len(invalid) > 5:
                                msg += f' 等{len(invalid)}行'
                            msg += '）'
                        else:
                            msg += '，全部合法）'
                    try:
                        status_var.set(msg)
                    except Exception:
                        pass
                    if invalid:
                        try:
                            preview = '\n'.join(
                                f'第{ln}行: {t[:40]}' for ln, t in invalid[:8])
                            if len(invalid) > 8:
                                preview += f'\n…等 {len(invalid)} 行'
                            messagebox.showwarning(
                                '部分行非法',
                                f'以下行无法解析为 YYYY-MM-DD：\n{preview}\n\n'
                                f'合法行已应用，是否继续？',
                                parent=dlg)
                        except Exception:
                            pass
                    try:
                        dlg.after(1500, dlg.destroy)
                    except Exception:
                        pass

                btn_row = tk.Frame(dlg, bg=self.C_BG)
                btn_row.pack(pady=(0, 10))
                self._mk_btn(btn_row, '解析并应用', _apply, kind='primary',
                          font=(self.FONT[0], 8)).pack(side='left', padx=4)
                self._mk_btn(btn_row, '取消', dlg.destroy, kind='ghost',
                          font=(self.FONT[0], 8)).pack(side='left', padx=4)
            except Exception as e:
                try:
                    messagebox.showerror('批量粘贴失败', str(e)[:200],
                                         parent=self.win)
                except Exception:
                    pass
        self._mk_btn(date_row, '📋 批量粘贴', _on_bulk_paste, kind='ghost',
                  font=(self.FONT[0], 8)).pack(side='left', padx=(6, 0))
        boost_row = tk.Frame(promo_box, bg=self.C_BG); boost_row.pack(fill='x', pady=2)
        self._lbl(boost_row, text="boost 权重:", font=(self.FONT[0], 8),
                  bg=self.C_BG, fg=self.C_TEXT).pack(side='left')
        _promo_boost_var = tk.DoubleVar(self.win, value=float(cur_promo.get('boost', 1.5) or 1.5))
        tk.Spinbox(boost_row, from_=1.0, to=10.0, increment=0.1,
                   textvariable=_promo_boost_var, width=6,
                   font=(self.FONT[0], 9), relief='flat', bd=0, highlightthickness=1,
                   highlightbackground="#EAEAEA", highlightcolor="#EAEAEA",
                   bg="#FFFFFF", fg=self.C_TEXT, buttonbackground=self.C_BG).pack(
            side='left', padx=(6, 12))
        self._lbl(boost_row, text="lead_days 窗口:", font=(self.FONT[0], 8),
                  bg=self.C_BG, fg=self.C_TEXT).pack(side='left')
        _promo_lead_var = tk.IntVar(self.win, value=int(cur_promo.get('lead_days', 3) or 3))
        tk.Spinbox(boost_row, from_=0, to=30, textvariable=_promo_lead_var, width=6,
                   font=(self.FONT[0], 9), relief='flat', bd=0, highlightthickness=1,
                   highlightbackground="#EAEAEA", highlightcolor="#EAEAEA",
                   bg="#FFFFFF", fg=self.C_TEXT, buttonbackground=self.C_BG).pack(
            side='left', padx=(6, 0))

        # ── 滞销阈值 ──
        slow_box = _make_factor_box(
            adv_frame, "▌ 滞销阈值", "近14日均销<阈值 且 库存/日销>比例")
        slow_box.pack(fill='x', padx=8, pady=(0, 6))
        _slow_enabled_var = tk.BooleanVar(self.win, value=bool(cur_slow.get('enabled', False)))
        tk.Checkbutton(slow_box, text='启用', variable=_slow_enabled_var,
                       font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_TEXT,
                       selectcolor=self.C_BG, activebackground=self.C_BG).pack(
            anchor='w', padx=4, pady=(0, 4))
        slow_row = tk.Frame(slow_box, bg=self.C_BG); slow_row.pack(fill='x', pady=2)
        self._lbl(slow_row, text="均销阈值(件/日):", font=(self.FONT[0], 8),
                  bg=self.C_BG, fg=self.C_TEXT).pack(side='left')
        _slow_thr_var = tk.DoubleVar(self.win, value=float(cur_slow.get('threshold_per_day', 1.0) or 1.0))
        tk.Spinbox(slow_row, from_=0.0, to=100.0, increment=0.1,
                   textvariable=_slow_thr_var, width=6,
                   font=(self.FONT[0], 9), relief='flat', bd=0, highlightthickness=1,
                   highlightbackground="#EAEAEA", highlightcolor="#EAEAEA",
                   bg="#FFFFFF", fg=self.C_TEXT, buttonbackground=self.C_BG).pack(
            side='left', padx=(6, 12))
        self._lbl(slow_row, text="库存/日销比例阈值:", font=(self.FONT[0], 8),
                  bg=self.C_BG, fg=self.C_TEXT).pack(side='left')
        _slow_ratio_var = tk.DoubleVar(self.win, value=float(cur_slow.get('stock_ratio', 5.0) or 5.0))
        tk.Spinbox(slow_row, from_=0.0, to=100.0, increment=0.5,
                   textvariable=_slow_ratio_var, width=6,
                   font=(self.FONT[0], 9), relief='flat', bd=0, highlightthickness=1,
                   highlightbackground="#EAEAEA", highlightcolor="#EAEAEA",
                   bg="#FFFFFF", fg=self.C_TEXT, buttonbackground=self.C_BG).pack(
            side='left', padx=(6, 0))

        # ── 季节系数 + 超卖 同一行（节省空间，但卡片标题/内层节奏统一） ──
        season_over_row = tk.Frame(adv_frame, bg=self.C_BG)
        season_over_row.pack(fill='x', padx=8, pady=(0, 6))
        season_box = _make_factor_box(
            season_over_row, "▌ 季节系数", "近4周/近12周 均值比，钳制[0.5, 2.0]")
        season_box.pack(side='left', fill='both', expand=True, padx=(0, 4))
        _season_enabled_var = tk.BooleanVar(self.win, value=bool(cur_season.get('enabled', False)))
        tk.Checkbutton(season_box, text='启用', variable=_season_enabled_var,
                       font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_TEXT,
                       selectcolor=self.C_BG, activebackground=self.C_BG).pack(
            anchor='w', padx=4, pady=2)
        over_box = _make_factor_box(
            season_over_row, "▌ 超卖等级", "stock<high_ratio×需求=🔥重")
        over_box.pack(side='left', fill='both', expand=True, padx=(4, 0))
        # 超卖卡：启用 + high_ratio + Spinbox 三件同行（grid 列对齐）
        _over_enabled_var = tk.BooleanVar(self.win, value=bool(cur_over.get('enabled', False)))
        tk.Checkbutton(over_box, text='启用', variable=_over_enabled_var,
                       font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_TEXT,
                       selectcolor=self.C_BG, activebackground=self.C_BG).grid(
            row=0, column=0, padx=(4, 6), pady=4, sticky='w')
        self._lbl(over_box, text="high_ratio:", font=(self.FONT[0], 8),
                  bg=self.C_BG, fg=self.C_TEXT).grid(row=0, column=1, padx=(0, 4), pady=4, sticky='w')
        _over_hr_var = tk.DoubleVar(self.win, value=float(cur_over.get('high_ratio', 0.5) or 0.5))
        tk.Spinbox(over_box, from_=0.05, to=0.95, increment=0.05,
                   textvariable=_over_hr_var, width=6,
                   font=(self.FONT[0], 9), relief='flat', bd=0, highlightthickness=1,
                   highlightbackground="#EAEAEA", highlightcolor="#EAEAEA",
                   bg="#FFFFFF", fg=self.C_TEXT, buttonbackground=self.C_BG).grid(
            row=0, column=2, padx=(0, 4), pady=4, sticky='w')

        # 高级区块的显示切换：根据当前 model
        # R1：visible → pack(pady=(6,4), padx=20, fill='x')；hidden → pack_forget。
        # 显隐决策由 adv_frame_visibility_for_model 统一，调用方按表操作。
        def _toggle_adv_visibility(*_):
            try:
                vis = self.adv_frame_visibility_for_model(_model_var.get())
                if vis.get('advanced_frame'):
                    adv_frame.pack(pady=(6, 4), padx=20, fill='x')
                else:
                    adv_frame.pack_forget()
            except Exception:
                pass
        _model_var.trace_add('write', _toggle_adv_visibility)
        _toggle_adv_visibility()

        # 保存
        def _on_save():
            try:
                cfg = Config.load() if hasattr(Config, "load") else {}
                if not isinstance(cfg, dict):
                    cfg = {}
                rep = cfg.get("replenishment", {}) or {}
                rep["model"] = str(_model_var.get() or 'classic')
                rep["safety_days"] = max(0, int(_sd_var.get() or 0))
                rep["in_transit_qty"] = max(0, int(_it_var.get() or 0))
                # 高级子配置：仅当 model='advanced' 时落盘；其他模式清空（用户切走即失效）
                if rep["model"] == MODEL_ADVANCED:
                    from algorithm_ui import collect_advanced_cfg_from_form
                    rep["advanced"] = collect_advanced_cfg_from_form({
                        'promo': {
                            'enabled': bool(_promo_enabled_var.get()),
                            'dates_text': str(_promo_dates_var.get() or ''),
                            'boost': float(_promo_boost_var.get() or 1.5),
                            'lead_days': int(_promo_lead_var.get() or 3),
                        },
                        'slow': {
                            'enabled': bool(_slow_enabled_var.get()),
                            'threshold_per_day': float(_slow_thr_var.get() or 1.0),
                            'stock_ratio': float(_slow_ratio_var.get() or 5.0),
                        },
                        'season': {'enabled': bool(_season_enabled_var.get())},
                        'oversell': {
                            'enabled': bool(_over_enabled_var.get()),
                            'high_ratio': float(_over_hr_var.get() or 0.5),
                        },
                    })
                else:
                    # 切回非高级模式：保留 advanced 节点（防止反复切丢失用户配置），但关闭全部
                    from algorithm_ui import collect_advanced_cfg_from_form
                    rep["advanced"] = collect_advanced_cfg_from_form({
                        'promo': {
                            'enabled': False,
                            'dates_text': str(_promo_dates_var.get() or ''),
                            'boost': float(_promo_boost_var.get() or 1.5),
                            'lead_days': int(_promo_lead_var.get() or 3),
                        },
                        'slow': {
                            'enabled': False,
                            'threshold_per_day': float(_slow_thr_var.get() or 1.0),
                            'stock_ratio': float(_slow_ratio_var.get() or 5.0),
                        },
                        'season': {'enabled': False},
                        'oversell': {
                            'enabled': False,
                            'high_ratio': float(_over_hr_var.get() or 0.5),
                        },
                    })
                cfg["replenishment"] = rep
                Config.save(cfg)
                adv_summary = ''
                if rep["model"] == MODEL_ADVANCED:
                    a = rep.get("advanced") or {}
                    p_on = a.get('promo', {}).get('enabled')
                    s_on = a.get('slow', {}).get('enabled')
                    sn_on = a.get('season', {}).get('enabled')
                    o_on = a.get('oversell', {}).get('enabled')
                    adv_summary = (f"  高级: 大促{'✓' if p_on else '×'} "
                                   f"滞销{'✓' if s_on else '×'} "
                                   f"季节{'✓' if sn_on else '×'} "
                                   f"超卖{'✓' if o_on else '×'}")
                self.status_text.set(
                    f"补货策略已保存：model={rep['model']}  safety_days={rep['safety_days']}  "
                    f"in_transit={rep['in_transit_qty']}{adv_summary}")
            except Exception as e:
                try:
                    messagebox.showerror("保存失败", str(e)[:200])
                except Exception:
                    pass
        self._mk_btn(card, "保存", _on_save, kind='primary',
                  font=(self.FONT[0], 9)).pack(pady=(8, 4))
        # 说明
        self._lbl(card,
                  text=("经典模式 = 现行公式原样保留（默认，一行公式逻辑都不改）。"
                        "加权模式按 sku_id 从 history_db 读近 7/14/30 日销量做加权日销，"
                        "无历史数据时自动回退经典并标注「经典(无历史)」。"
                        "高级模式叠加季节系数/大促倍数/滞销预警/超卖预警四因子，"
                        "可在下方编辑区单独启用（默认全关闭）。"),
                  font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_MUTED,
                  wraplength=560, justify='left').pack(pady=(0, 12), padx=20, anchor='w')

    def _build_license_card(self, parent):
        """t12 P2-C 授权管理卡片：显示 tier/到期/导入 license 文本/enforce 开关。

        用户裁定：enforce=false 默认全免，状态下显示「试用期：所有限制未启用」；
        enforce=true + 无 license → 免费版；
        enforce=true + 有效 Pro license → 显示到期日。
        """
        try:
            from auth.license import get_license_info, is_pro, reset_cache, FREE_DAILY_LIVE_SCREENSHOT, FREE_HISTORY_DAYS
        except Exception:
            self._lbl(parent, text="授权模块加载失败（auth.license）",
                     fg=self.C_MUTED).pack(pady=8)
            return

        card = tk.Frame(parent, bg=self.C_BG, highlightthickness=1,
                        highlightbackground=self.C_BORDER)
        card.pack(fill="x", padx=20, pady=8)
        self._lbl(card, text='授权管理', font=self.FONT_HEADING, bg=self.C_BG,
                  fg=self.C_SECONDARY).pack(pady=(12, 2))

        # 当前状态行
        _status_var = tk.StringVar(self.win, value='')
        _tier_var = tk.StringVar(self.win, value='')
        _expire_var = tk.StringVar(self.win, value='')
        self._lbl(card, textvariable=_tier_var, font=(self.FONT[0], 10, 'bold'),
                  bg=self.C_BG, fg=self.C_TEXT).pack(pady=(2, 0))
        self._lbl(card, textvariable=_expire_var, font=(self.FONT[0], 8),
                  bg=self.C_BG, fg=self.C_MUTED).pack()
        self._lbl(card, textvariable=_status_var, font=(self.FONT[0], 8),
                  bg=self.C_BG, fg=self.C_MUTED).pack(pady=(2, 4))

        def _refresh():
            try:
                cfg = Config.load() if hasattr(Config, "load") else {}
                lic_cfg = cfg.get("license", {}) if isinstance(cfg, dict) else {}
                key = lic_cfg.get("key", "") or ""
                enforce = bool(lic_cfg.get("enforce", False))
                info = get_license_info(key, enforce=enforce)
                _tier_var.set("Pro" if info.get("is_pro") else "免费版")
                _expire_var.set(info.get("status_text", ""))
                _status_var.set(
                    f"enforce={enforce}  ·  实时截图免费 {FREE_DAILY_LIVE_SCREENSHOT} 次/日"
                    f"  ·  历史趋势免费 {FREE_HISTORY_DAYS} 天"
                )
            except Exception as e:
                _status_var.set(f"读 license 状态失败：{str(e)[:80]}")
        _refresh()

        # enforce 开关
        enf_row = tk.Frame(card, bg=self.C_BG); enf_row.pack(pady=(6, 2), padx=20, fill='x')  # (b-4): (8,2)→(6,2) 紧凑
        _enforce_var = tk.BooleanVar(self.win, value=False)
        def _on_enforce_toggle():
            try:
                cfg = Config.load() if hasattr(Config, "load") else {}
                if not isinstance(cfg, dict):
                    cfg = {}
                lic = cfg.get("license", {}) or {}
                lic["enforce"] = bool(_enforce_var.get())
                cfg["license"] = lic
                Config.save(cfg)
                reset_cache()
                _refresh()
            except Exception as e:
                # -13)：写盘失败时回滚 UI 到旧值，
                # 避免「UI 显示新值但磁盘未写入」的不一致状态
                try:
                    _enforce_var.set(not bool(_enforce_var.get()))
                except Exception:
                    pass
                try:
                    messagebox.showerror("保存失败", str(e)[:200])
                except Exception:
                    pass
        # 读当前值
        try:
            _cur = Config.load() if hasattr(Config, "load") else {}
            _enforce_var.set(bool((_cur.get("license") or {}).get("enforce", False)))
        except Exception:
            pass
        tk.Checkbutton(enf_row, text="启用门控（关闭时所有 Pro 功能无限制）",
                       variable=_enforce_var, command=_on_enforce_toggle,
                       font=(self.FONT[0], 9), bg=self.C_BG, fg=self.C_TEXT,
                       selectcolor=self.C_BG, activebackground=self.C_BG).pack(side='left')

        # license 输入框 + 导入按钮
        in_row = tk.Frame(card, bg=self.C_BG); in_row.pack(pady=(6, 4), padx=20, fill='x')
        self._lbl(in_row, text="License 文本：", font=(self.FONT[0], 9),
                  bg=self.C_BG, fg=self.C_TEXT).pack(side='left', padx=(0, 6))
        _key_var = tk.StringVar(self.win, value='')
        try:
            _cur2 = Config.load() if hasattr(Config, "load") else {}
            _key_var.set((_cur2.get("license") or {}).get("key", "") or "")
        except Exception:
            pass
        _key_entry = tk.Entry(in_row, textvariable=_key_var, font=self.FONT, width=48,  # (b-4): 60→48 留位按钮
                              relief='flat', bd=0, highlightthickness=1,
                              highlightbackground="#EAEAEA", highlightcolor="#EAEAEA",
                              bg="#FFFFFF", fg=self.C_TEXT, insertbackground=self.C_TEXT)
        _key_entry.pack(side='left', padx=(0, 6), fill='x', expand=True)

        def _on_import():
            try:
                # -14)：写盘失败时回滚 _key_var 为旧值；
                # 进入 try 前快照旧文本，失败时还原（避免「UI 显示新文本但磁盘未写入」）
                _old_key = _key_var.get()
                text = _key_var.get().strip()
                if not text:
                    messagebox.showinfo("导入 license", "请先粘贴 license 文本")
                    return
                from auth.license import verify_license, reset_cache
                lic = verify_license(text)
                if not lic:
                    messagebox.showerror("导入 license",
                                         "license 文本无效（验签失败/已过期/指纹不匹配）")
                    return
                cfg = Config.load() if hasattr(Config, "load") else {}
                if not isinstance(cfg, dict):
                    cfg = {}
                lic_cfg = cfg.get("license", {}) or {}
                lic_cfg["key"] = text
                cfg["license"] = lic_cfg
                Config.save(cfg)
                reset_cache()
                _refresh()
                messagebox.showinfo("导入 license",
                                    f"已导入：tier={lic.get('tier')}  到期={lic.get('expire_at')}")
            except Exception as e:
                # -14)：写盘失败时回滚 _key_var 为旧值
                try:
                    _key_var.set(_old_key)
                except Exception:
                    pass
                try:
                    messagebox.showerror("导入失败", str(e)[:200])
                except Exception:
                    pass
        self._mk_btn(in_row, "导入并验证", _on_import, kind='primary',
                  font=(self.FONT[0], 9)).pack(side='left')

        # 永久免费声明
        self._lbl(card,
                  text=("永久免费功能：表格导入 · 手动输入 · 批量识别 · 双模型验证 · Excel 导出。"
                        "门控只针对新增高级功能：实时截图识别次数 / 历史趋势窗口。"),
                  font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_MUTED,
                  wraplength=560, justify='left').pack(pady=(4, 12), padx=20, anchor='w')

    def _probe_columns(self):
        """探测：截图 → AI 定位表格 → 全量识别所有列 → 保存 all 列清单"""
        import time, threading
        self.col_status_var.set("探测中 — 请确保 PDD 后台订货管理页面在前台…")
        def task():
            try:
                from utils import capture_pdd_screenshot, get_base_dir as _gbd
                import os as _os
                shot = _os.path.join(_gbd(), 'output', '_probe_cols.png')
                capture_pdd_screenshot(shot)
                # AI 定位表格（失败则全图识别，由 ocr_table 内部回退）
                bbox = None
                try:
                    from vision import ai_locate_table
                    loc = ai_locate_table(shot)
                    if loc:
                        bbox = loc.get('table')
                except Exception:
                    bbox = None
                from ocr import ocr_table
                # 探测列名是"结构理解"任务（列名汇总），必须用通用视觉模型——
                # OCR 专用模型只做文字提取不汇总列名，会返回文字定位结果（v1.4 修复）
                result = ocr_table(shot, columns=None, table_bbox=bbox, prefer_general=True)
                cols = result.get('columns') or []
                if not cols:
                    self.win.after(0, lambda: self.col_status_var.set(
                        "❌ 未识别到表格列，请确认页面已打开订货管理表格"))
                    return
                from utils import save_ocr_columns, get_ocr_columns
                cfg = get_ocr_columns()
                save_ocr_columns(all_cols=cols, selected=cfg['selected'], mapping=cfg['mapping'])
                self.win.after(0, lambda: self.col_status_var.set(
                    f"✅ 探测到 {len(cols)} 列：{'、'.join(cols[:8])}{'…' if len(cols)>8 else ''}\n"
                    f"（已保存，点「配置识别列」勾选要识别的列）"))
                self.win.after(0, lambda: messagebox.showinfo(
                    "探测完成", f"识别到 {len(cols)} 列：\n{'、'.join(cols)}\n\n"
                    f"点「配置识别列」勾选需要识别的列。\n库存/销量列用于补货计算，建议保留。",
                    parent=self.win))
            except Exception as e:
                self.win.after(0, lambda e=e: self.col_status_var.set(f"❌ 探测失败: {str(e)[:60]}"))
        threading.Thread(target=task, daemon=True).start()

    def _config_columns(self):
        """配置识别列：勾选要识别的列 + 核心列映射下拉"""
        from utils import get_ocr_columns, save_ocr_columns
        cfg = get_ocr_columns()
        all_cols = cfg['all'] or []
        if not all_cols:
            messagebox.showwarning("未探测", "请先点「探测全部列」识别后台表格的所有列",
                                   parent=self.win)
            return
        dlg = tk.Toplevel(self.win)
        dlg.title("配置识别列")
        dlg.geometry(self._geo(480, 560))
        dlg.configure(bg=self.C_BG)
        self._lbl(dlg, text="勾选要识别的列", font=self.FONT_HEADING,
                 bg=self.C_BG, fg=self.C_TEXT).pack(pady=(10,2))
        self._lbl(dlg, text="库存/销量列为补货计算必需，取消后计算列将为空",
                 font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_MUTED).pack()

        # 列勾选（可滚动）
        canvas = tk.Canvas(dlg, bg=self.C_BG, highlightthickness=0)
        sb = ttk.Scrollbar(dlg, orient="vertical", command=canvas.yview)
        list_frame = tk.Frame(canvas, bg=self.C_BG)
        list_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=list_frame, anchor="nw", width=430)
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(15,0), pady=5)
        sb.pack(side="right", fill="y", padx=(0,15), pady=5)

        selected_set = set(cfg['selected'])
        col_vars = {}
        for c in all_cols:
            var = tk.BooleanVar(dlg, value=(c in selected_set))
            col_vars[c] = var
            tk.Checkbutton(list_frame, text=c, variable=var, font=(self.FONT[0], 8),
                           bg=self.C_BG, fg=self.C_TEXT,
                           selectcolor=self.C_BG, activebackground=self.C_BG,
                           anchor="w").pack(fill="x", padx=8, pady=1)

        # 核心列映射
        map_frame = tk.Frame(dlg, bg=self.C_BG)
        map_frame.pack(fill="x", padx=15, pady=(8,2))
        self._lbl(map_frame, text="核心列映射（后台列名变化时修改）",
                 font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_MUTED).pack(anchor="w")
        mapping = cfg['mapping']
        map_vars = {}
        for field, label in [('name', '商品信息列(含ID)'), ('stock', '库存列'),
                             ('sales', '销量列'), ('region', '销售区域列'), ('warehouse', '仓库信息列')]:
            row = tk.Frame(map_frame, bg=self.C_BG)
            row.pack(fill="x", pady=1)
            self._lbl(row, text=label, font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_TEXT,
                     width=10, anchor="w").pack(side="left")
            var = tk.StringVar(dlg, value=mapping.get(field, ''))
            map_vars[field] = var
            tk.OptionMenu(row, var, *all_cols).pack(side="left", fill="x", expand=True)

        def save():
            selected = [c for c, v in col_vars.items() if v.get()]
            new_map = {f: v.get() for f, v in map_vars.items()}
            # 强制必选核心列：商品名称/库存/销量（含映射对应的列）
            missing = []
            for f, label in [('name', '商品名称列'), ('stock', '库存列'), ('sales', '销量列')]:
                col = new_map.get(f, '')
                if not col:
                    missing.append(label)
            if missing:
                messagebox.showwarning("核心列缺失", "请为「" + "、".join(missing) + "」指定对应列\n"
                                        "（商品名称/库存/销量为识别与补货计算必需）", parent=dlg)
                return
            # 核心列映射列强制加入 selected（识别必需）
            for f in ('name', 'stock', 'sales'):
                col = new_map.get(f, '')
                if col and col not in selected:
                    selected.append(col)
            save_ocr_columns(all_cols=all_cols, selected=selected, mapping=new_map)
            self.col_status_var.set(f"已保存识别列：{len(selected)} 列")
            self.status_text.set(f"识别列配置已保存 — {len(selected)} 列")
            dlg.destroy()

        self._mk_btn(dlg, "保存", save, kind='primary', font=self.FONT_BOLD,
                  width=14).pack(pady=(8,12))
        dlg.transient(self.win)
        dlg.grab_set()

    def _pick_export_path(self, parent):
        from tkinter import filedialog
        path = filedialog.askdirectory(title="选择导出文件夹")
        if path:
            self.export_path_var.set(path)

    def _get_export_path(self):
        from export_xlsx import _get_default_export_dir
        return _get_default_export_dir()

    def _save_settings(self, dlg):
        import json, os as _os, tempfile
        settings_file = _os.path.join(get_base_dir(), 'settings.json')
        path = self.export_path_var.get().strip()
        if not path:
            messagebox.showwarning("路径为空", "请先选择或输入导出路径", parent=dlg)
            return
        # 路径可写性即时验证：目录不存在则创建，测试能否写入文件
        try:
            if not _os.path.isdir(path):
                _os.makedirs(path, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path, prefix='.write_test_', delete=True):
                pass  # 能创建即说明可写，自动清理不留残留
        except OSError as e:
            messagebox.showerror("路径不可用", f"导出路径无法写入：\n{path}\n\n{str(e)}\n"
                                "请检查目录权限或换个路径", parent=dlg)
            return
        from utils import Config
        s = Config.load()  # 安全回退：文件损坏返回 {}，不抛异常不清空
        s['export_path'] = path
        try:
            Config.save(s)  # 原子写入
        except Exception as e:
            messagebox.showerror("保存失败", f"无法写入配置文件：\n{settings_file}\n\n{str(e)}", parent=dlg)
            return
        self.status_text.set(f"导出路径已更新 → {path}")
        messagebox.showinfo("已保存", f"导出路径已设置为：\n{path}")
        if dlg: dlg.destroy()

    def _build_product_region_tab(self, parent, dlg=None):
        """商品运输时效设置：选地区 → 显示商品列表 → 逐商品调运输天数"""
        self._lbl(parent, text="商品运输时效设置", font=self.FONT_HEADING).pack(padx=16, pady=(14, 2))  # 页标题边距统一 16/(14,2)
        self._lbl(parent, text="不同商品发往不同地区，运输时间可能不同", font=(self.FONT[0], 8), fg=self.C_MUTED).pack()

        # 地区选择
        sel_frame = tk.Frame(parent, bg=self.C_BG)
        sel_frame.pack(fill="x", padx=20, pady=(12,5))
        self._lbl(sel_frame, text="选择地区:", font=self.FONT).pack(side="left")
        region_names = sorted(self.cache.keys()) if self.cache else sorted(self.regions.keys())
        if not region_names:
            region_names = ['（暂无识别数据）']
        self._settings_region_var = tk.StringVar(self.win, value=region_names[0] if region_names else '')
        region_combo = ttk.Combobox(sel_frame, textvariable=self._settings_region_var,
            values=region_names, width=18, font=self.FONT, state="readonly")
        region_combo.pack(side="left", padx=8)

        def delete_region():
            region = self._settings_region_var.get()
            if not region or region.startswith('（'):
                return
            # v1.4.7 WS-A（A5）：升级为三选——删除并清除历史 / 仅删配置保留历史 / 取消
            _wipe_hist = messagebox.askyesnocancel(
                "确认删除",
                f"确定删除地区「{region}」及其所有商品时效设置？\n已识别的缓存数据也会一并清除。\n\n"
                "【是】删除配置，并同时清除该地区的全部识别历史；\n"
                "【否】仅删除配置与缓存，保留历史数据（趋势仍可见）；\n"
                "【取消】放弃删除。")
            if _wipe_hist is None:
                return
            if region in self.regions:
                del self.regions[region]
            if region in self.cache:
                del self.cache[region]
            if _wipe_hist is True:
                try:
                    import history_db as _hdb
                    _deleted = _hdb.delete_region(region)
                    self.status_text.set(
                        f"地区「{region}」已删除（含历史 {_deleted} 行）"
                        if _deleted >= 0 else f"地区「{region}」已删除（历史清理失败，见日志）")
                except Exception:
                    self.status_text.set(f"地区「{region}」已删除（历史清理失败，见日志）")
            else:
                self.status_text.set(f"地区「{region}」已删除（历史数据已保留）")
            self._save_regions()
            new_names = sorted(self.cache.keys()) if self.cache else sorted(self.regions.keys())
            if not new_names:
                new_names = ['（暂无识别数据）']
            region_combo['values'] = new_names
            self._settings_region_var.set(new_names[0])
            for w in self._settings_list_frame.winfo_children():
                w.destroy()
            self._lbl(self._settings_list_frame, text="地区已删除",
                     font=(self.FONT[0], 8), fg=self.C_MUTED).pack(pady=20)
            self._update_tabs()
            # （状态栏文案已在上方按「是否清除历史」分支设置，此处不再覆盖）

        self._mk_btn(sel_frame, "删除地区", delete_region, kind='ghost',
                  font=(self.FONT[0], 8)).pack(side="left", padx=5)

        # 商品列表区（可滚动）
        canvas_frame = tk.Frame(parent, bg=self.C_BG)
        canvas_frame.pack(fill="both", expand=True, padx=20, pady=5)

        # 实施 A6：删 height=220 改 fill+expand 自适应（v4f 修正：禁用 winfo_height//2 方案）
        canvas = tk.Canvas(canvas_frame, highlightthickness=0, bg=self.C_BG)
        scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
        self._settings_list_frame = tk.Frame(canvas, bg=self.C_BG)

        self._settings_list_frame.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self._settings_list_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def refresh_products(*args):
            for w in self._settings_list_frame.winfo_children():
                w.destroy()
            region = self._settings_region_var.get()
            if not region or region.startswith('（'):
                self._lbl(self._settings_list_frame, text="暂无识别数据，请先截图识别",
                         font=(self.FONT[0], 8), fg=self.C_MUTED).pack(pady=20)
                return

            products = []
            if region in self.cache:
                for item in self.cache[region].get('items', []):
                    name = item.get('name', '')
                    if name and name not in products:
                        products.append(name)

            if not products:
                self._lbl(self._settings_list_frame, text="该地区暂无商品，请先截图识别",
                         font=(self.FONT[0], 8), fg=self.C_MUTED).pack(pady=20)
                return

            hdr = tk.Frame(self._settings_list_frame, bg=self.C_BG)
            hdr.pack(fill="x", pady=(0,4))
            self._lbl(hdr, text="商品名称", font=self.FONT_BOLD, width=22, anchor="w").pack(side="left")
            self._lbl(hdr, text="运输天数", font=self.FONT_BOLD, width=10).pack(side="left", padx=5)

            spinboxes = {}
            current_settings = self.regions.get(region, {})
            if not isinstance(current_settings, dict):
                current_settings = {}

            for prod in products:
                row = tk.Frame(self._settings_list_frame, bg=self.C_BG)
                row.pack(fill="x", pady=1)
                self._lbl(row, text=prod, font=self.FONT, width=22, anchor="w").pack(side="left")
                spin = tk.Spinbox(row, from_=1, to=30, width=8, font=self.FONT,
                                  bg=self.C_BG, fg=self.C_TEXT, insertbackground=self.C_TEXT,
                                  buttonbackground=self.C_BG, relief='flat', bd=0,
                                  highlightthickness=1, highlightbackground="#EAEAEA")
                spin.delete(0, "end")
                spin.insert(0, str(current_settings.get(prod, 3)))
                spin.pack(side="left", padx=5)
                spinboxes[prod] = spin

            self._settings_spinboxes = spinboxes

        self._settings_region_var.trace('w', refresh_products)

        if region_names and region_names[0] and not region_names[0].startswith('（'):
            refresh_products()

        def save_all():
            region = self._settings_region_var.get()
            if not region or region.startswith('（'):
                return
            spinboxes = getattr(self, '_settings_spinboxes', {})
            if region not in self.regions or not isinstance(self.regions[region], dict):
                self.regions[region] = {}
            for prod, spin in spinboxes.items():
                try:
                    # v1.4.6（bug hunt L8/L14 clamp）：readonly 框可输入任意文本/小数/越界值，
                    # 收紧为 1..30 整数（int(float(s)) 容忍 "3.0"、去空白；越界/clamp 兜底 3）
                    _raw = str(spin.get()).strip()
                    _val = int(float(_raw)) if _raw.replace('.', '', 1).isdigit() else -1
                    self.regions[region][prod] = 3 if not (1 <= _val <= 30) else _val
                except ValueError:
                    self.regions[region][prod] = 3
            self._save_regions()
            self.status_text.set(f"「{region}」商品运输时效已保存 — {len(spinboxes)} 个商品")
            if region == self.region_var.get() and region in self.cache:
                self._calc_from_items(self.cache[region]['items'])

        btn_frame = tk.Frame(parent, bg=self.C_BG)
        btn_frame.pack(pady=10)
        self._mk_btn(btn_frame, "保存时效设置", save_all, kind='primary',
                  font=self.FONT_BOLD).pack(side="left", padx=5)

        # 一键全设（批量调运输天数，避免逐商品手调）
        def set_all_days(days):
            region = self._settings_region_var.get()
            if not region or region.startswith('（'):
                return
            spinboxes = getattr(self, '_settings_spinboxes', {})
            if not spinboxes:
                messagebox.showwarning("无商品", "该地区暂无商品可设置", parent=self.win)
                return
            for prod, spin in spinboxes.items():
                # v1.4.6（bug hunt L8 clamp）：批量设置同样收紧到 1..30
                _days = 3 if not (1 <= int(days) <= 30) else int(days)
                spin.delete(0, "end")
                spin.insert(0, str(_days))
            self.status_text.set(f"已将所有商品运输时效设为 {days} 天（记得点保存）")

        self._mk_btn(btn_frame, "全部设为 3 天", lambda: set_all_days(3), kind='ghost',
                  font=(self.FONT[0], 8)).pack(side="left", padx=5)
        self._mk_btn(btn_frame, "全部设为 5 天", lambda: set_all_days(5), kind='ghost',
                  font=(self.FONT[0], 8)).pack(side="left", padx=5)

    def _build_skin_tab(self, parent):
        """主题选择：四套主题 2×2 网格，点击预览卡即切换"""
        self._lbl(parent, text="选择界面主题", font=self.FONT_HEADING).pack(padx=16, pady=(14, 2))  # 页标题边距统一 16/(14,2)
        self._lbl(parent, text="点击卡片即时切换，自动保存偏好", font=(self.FONT[0], 8), fg=self.C_MUTED).pack()

        cards_frame = tk.Frame(parent, bg=self.C_BG)
        cards_frame.pack(fill="x", padx=15, pady=10)
        # 实施 A3：4 卡 2×2 网格（列等权）
        cards_frame.columnconfigure(0, weight=1)
        cards_frame.columnconfigure(1, weight=1)

        def select_theme(name):
            self._apply_theme(name)
            save_theme_pref(name)
            self.status_text.set(f"皮肤已切换为「{name}」")
            for child in cards_frame.winfo_children():
                is_sel = getattr(child, '_skin_name', '') == name
                ac = THEMES.get(name, {}).get('C_ACCENT', '#3B82F6')
                child.configure(highlightbackground=ac if is_sel else self.C_BORDER,
                               highlightthickness=2 if is_sel else 1)
                for gc in child.winfo_children():
                    if isinstance(gc, tk.Frame):
                        for gcc in gc.winfo_children():
                            if isinstance(gcc, tk.Label) and gcc.cget('text') == '✓ 当前':
                                if is_sel:
                                    gcc.configure(text='✓ 当前', fg=ac)
                                else:
                                    gcc.configure(text='')

        for i, (name, theme_data) in enumerate(THEMES.items()):
            ac = theme_data['C_ACCENT']

            is_sel = name == self._theme_name
            # 实施 A4：#E2E8F0 → self.C_BORDER（零新 token）
            card = tk.Frame(cards_frame, bg="#FFFFFF",
                           highlightbackground=ac if is_sel else self.C_BORDER,
                           highlightthickness=2 if is_sel else 1)
            # 实施 A3：2×2 网格（行 = i//2, 列 = i%2）
            card.grid(row=i // 2, column=i % 2, padx=4, pady=6, sticky="nsew")
            card._skin_name = name
            card._skip_theme = True

            info = tk.Frame(card, bg="#FFFFFF")
            info.pack(fill="x", padx=12, pady=(16, 12))
            self._lbl(info, text=theme_data['label'], font=self.FONT_TITLE,
                    bg="#FFFFFF", fg="#1E293B").pack(anchor="w")
            self._lbl(info, text=theme_data['desc'], font=(self.FONT[0], 8),
                    bg="#FFFFFF", fg="#94A3B8").pack(anchor="w", pady=(3, 8))
            if is_sel:
                self._lbl(info, text="✓ 当前", font=(self.FONT[0], 9, 'bold'),
                        bg="#FFFFFF", fg=ac).pack(anchor="w")

            for w in [card, info] + list(card.winfo_children()):
                try:
                    w.bind("<Button-1>", lambda e, n=name: select_theme(n))
                except:
                    pass

    def _build_calibrate_inline(self, parent):
        """ (⑤)：原 _build_calibrate_tab 改名为 _build_calibrate_inline。
        原因：grep 显示零外部引用（仅 _build_general_page L76 调用），
        命名 "tab" 误导（实际是 _build_general_page 内的 section，不是独立 nav page）。
        保留为私有 method 而非 inline 函数体，因为函数体 200+ 行，inline 改动太大。
        """
        import json, time as _time
        from datetime import datetime

        self._lbl(parent, text="定位校准", font=self.FONT_HEADING).pack(padx=16, pady=(14, 2))  # 页标题边距统一 16/(14,2)

        from utils import Config as _Cfg3
        s = _Cfg3.load()  # 安全回退
        cal = s.get('calibrate')
        if not isinstance(cal, dict):
            cal = {'mode': 'ai', 'ai': {}}

        # ── AI 模式卡片（v1.4 起唯一模式，无模式选择器）──
        ai_card = tk.Frame(parent, bg=self.C_BG, highlightthickness=1, highlightbackground=self.C_BORDER)

        ai_status_lbl = self._lbl(ai_card, text="", font=(self.FONT[0], 8), fg=self.C_TEXT, bg=self.C_BG)
        ai_status_lbl.pack(pady=5)
        ai_coords_lbl = self._lbl(ai_card, text="", font=self.FONT, fg=self.C_PRIMARY, bg=self.C_BG)
        ai_coords_lbl.pack(pady=2)
        ai_conf_lbl = self._lbl(ai_card, text="", font=(self.FONT[0], 8), fg=self.C_MUTED, bg=self.C_BG)
        ai_conf_lbl.pack(pady=2)
        ai_res_lbl = self._lbl(ai_card, text="", font=(self.FONT[0], 8), fg=self.C_MUTED, bg=self.C_BG)
        ai_res_lbl.pack(pady=2)

        ai_btn_frame = tk.Frame(ai_card, bg=self.C_BG)
        ai_btn_frame.pack(pady=8)

        def _minimize_away():
            """操作前让位：隐藏设置窗口 + 最小化主窗口，露出浏览器。
            否则 PrintWindow 失败回退前台截图会截到 PDD EZ 窗口内容（定位错乱），
            且定位后窗口一直盖住浏览器，用户无法手动核对定位精准度。"""
            hidden = []
            try:
                _t = parent.winfo_toplevel()
                if _t.winfo_exists() and _t.state() != 'withdrawn':
                    _t.withdraw()
                    hidden.append(_t)
            except Exception:
                pass
            try:
                if self.win.winfo_exists() and self.win.state() != 'iconic':
                    self.win.iconify()
                    hidden.append(self.win)
            except Exception:
                pass
            return hidden

        def _restore_windows(hidden):
            for w in hidden:
                try:
                    w.deiconify()
                    w.lift()
                except Exception:
                    pass

        def _bring_browser_front():
            try:
                import pygetwindow as gw
                for title in ['拼多多', 'pinduoduo', 'Microsoft Edge', 'Edge', 'Chrome', 'Firefox']:
                    wins = gw.getWindowsWithTitle(title)
                    if wins:
                        wins[0].activate()
                        return True
            except Exception:
                pass
            return False

        def do_ai_locate():
            """手动定位：锁定商家后台窗口截图（自动前置该窗口），AI 识别坐标后转全屏坐标保存。
            失败一律弹窗提示，绝不静默。
            v1.4.5（bug hunt F28）：AI 多采样（每轮最长 180s）移入 worker 线程，弱网不冻结
            主线程；_minimize/恢复/status 等 Tk 交互仍在主线程（after 回填）。"""
            try:
                # v1.4.6（fix-review C11 尾项）：定位运行中重入守卫，连点只跑一次
                if getattr(self, '_ai_locating', False):
                    ai_status_lbl.configure(text="定位进行中，请等待完成...")
                    return
                self._ai_locating = True
                import os  # v1.4.5（bug hunt F3）：此前无模块级 import os，此处裸 os 必 NameError（被下方 except 吞成"定位失败"）
                import tempfile, threading
                import pyautogui as pg
                from vision import ai_locate_elements
                from utils import capture_pdd_screenshot, Config as _CfgA
                ai_status_lbl.configure(text="正在定位商家后台窗口...")
                self.win.update()
                # 主线程：让位 + 截图（<1s），随后 AI 采样放 worker
                _hidden = _minimize_away()
                try:
                    _time.sleep(0.6)
                    _shot = os.path.join(tempfile.gettempdir(), 'pdd_calib_manual.png')
                    _pos = {}
                    capture_pdd_screenshot(_shot, _pos)
                except Exception:
                    _restore_windows(_hidden)
                    raise

                def _finish(result, pos, err=''):
                    try:
                        self._ai_locating = False  # v1.4.6 C11：定位结束清除标志（成功/失败统一）
                        _restore_windows(_hidden)
                        _bring_browser_front()
                        if not result:
                            # fix-review C11：err 非空时展示具体错误原因（API/网络/超时）
                            _detail = f"（{err[:200]}）" if err else ""
                            messagebox.showwarning(
                                "定位失败",
                                "未识别到商家后台的元素。" + _detail +
                                "\n请确认：\n1. 拼多多商家后台页面已打开并加载完成\n2. 页面显示的是商品列表（有省份下拉框和查询按钮）",
                            )
                            ai_status_lbl.configure(text=f"定位失败：未识别到后台元素{_detail}")
                            return
                        ox, oy = pos.get('left', 0), pos.get('top', 0)
                        sx = pos.get('scale_x', 1.0) or 1.0
                        sy = pos.get('scale_y', 1.0) or 1.0
                        dd = {'x': int(result['dropdown']['x'] * sx) + ox, 'y': int(result['dropdown']['y'] * sy) + oy}
                        qq = {'x': int(result['query']['x'] * sx) + ox, 'y': int(result['query']['y'] * sy) + oy}
                        screen_w, screen_h = pg.size()
                        cal['ai'] = {
                            'last_time': _time.time(),
                            'dropdown': dd, 'query': qq,
                            'confidence': result['confidence'],
                            'screen_width': screen_w, 'screen_height': screen_h,
                        }
                        cal['mode'] = 'ai'
                        s['calibrate'] = cal
                        _CfgA.save(s)
                        ai_status_lbl.configure(text="✅ 定位完成")
                        self.status_text.set("AI 智能定位完成")
                    except Exception as e:
                        messagebox.showwarning("定位失败", str(e)[:300])
                        ai_status_lbl.configure(text=f"定位失败: {str(e)[:60]}")
                    _refresh_cards()

                def _work():
                    try:
                        _result = ai_locate_elements(_shot)
                        self.win.after(0, lambda: _finish(_result, _pos))
                    except Exception as _e:
                        # v1.4.5（bug hunt F28 + fix-review C11）：异常透传具体原因，
                        # 不要无捕获变量丢弃细节（否则 API/网络错误全误报"未识别到后台元素"）
                        self.win.after(0, lambda e=_e: _finish(None, _pos, str(e)))
                threading.Thread(target=_work, daemon=True).start()
            except Exception as e:
                try:
                    self._ai_locating = False  # v1.4.6 C11：截图/前置阶段异常，worker 未启动，也须清除标志
                    messagebox.showwarning("定位失败", str(e)[:300])
                except Exception:
                    pass
                ai_status_lbl.configure(text=f"定位失败: {str(e)[:60]}")
                _refresh_cards()

        self._mk_btn(ai_btn_frame, "立即定位", do_ai_locate, kind='dark',
                  font=self.FONT_BOLD, width=12).pack(side='left', padx=5)
        self._mk_btn(ai_btn_frame, "测试点击", lambda: _test_click(cal), kind='ghost',
                  font=(self.FONT[0], 8)).pack(side='left', padx=5)

        # ── 刷新显示 ──
        def _refresh_cards():
            # v1.4 起只保留 AI 智能定位（绝对坐标模式已移除）
            ai_card.pack(fill='x', padx=20, pady=10)
            _ai_raw = cal.get('ai')
            ai_data = _ai_raw if isinstance(_ai_raw, dict) else {}
            if ai_data.get('last_time'):
                t = datetime.fromtimestamp(ai_data['last_time']).strftime('%Y-%m-%d %H:%M:%S')
                dd = ai_data.get('dropdown', {})
                qq = ai_data.get('query', {})
                ai_status_lbl.configure(text=f"上次定位: {t}")
                ai_coords_lbl.configure(text=f"下拉框 ({dd.get('x','?')}, {dd.get('y','?')})  查询 ({qq.get('x','?')}, {qq.get('y','?')})")
                ai_conf_lbl.configure(text=f"置信度: {ai_data.get('confidence', 0):.0%}")
                ai_res_lbl.configure(text=f"定位分辨率: {ai_data.get('screen_width',0)}×{ai_data.get('screen_height',0)}")
            else:
                ai_status_lbl.configure(text="尚未进行 AI 定位")
                ai_coords_lbl.configure(text="")
                ai_conf_lbl.configure(text="")
                ai_res_lbl.configure(text="")

        def _test_click(cal_data):
            _ai_raw = cal_data.get('ai')
            ai_data = _ai_raw if isinstance(_ai_raw, dict) else {}
            dd = ai_data.get('dropdown', {})
            if dd and 'x' in dd and 'y' in dd and dd['x'] is not None and dd['y'] is not None:
                import pyautogui as pg
                # v1.4 bugfix：点击前让位（隐藏设置窗口+最小化主窗口），
                # 点击后恢复并把浏览器置前——用户需要看到下拉框是否真的展开
                _hidden = _minimize_away()
                try:
                    _time.sleep(0.5)
                    pg.click(dd['x'], dd['y'])
                finally:
                    _restore_windows(_hidden)
                    _bring_browser_front()
                self.status_text.set(f"已点击下拉框 ({dd['x']}, {dd['y']})，请确认浏览器中是否展开")

        _refresh_cards()

    def _build_backend_tab(self, parent, dlg=None):
        """配置拼多多商家后台链接和登录凭据"""
        self._lbl(parent, text="商家后台快捷入口", font=self.FONT_HEADING).pack(padx=16, pady=(14, 2))  # 页标题边距统一 16/(14,2)
        self._lbl(parent, text="设置后可通过主页「🏪 商家后台」按钮一键打开", font=(self.FONT[0], 8), fg=self.C_MUTED).pack()

        config = self._get_backend_config()

        url_frame = tk.Frame(parent, bg=self.C_BG)
        url_frame.pack(fill="x", padx=20, pady=(14, 4))  # (b-2): (15,5)→(14,4) 与其它页标题距对齐
        self._lbl(url_frame, text="后台地址:", font=self.FONT, width=9, anchor="e").pack(side="left")  # (b-2): 10→9
        url_var = tk.StringVar(self.win, value=config.get('url', 'https://mms.pinduoduo.com/'))
        tk.Entry(url_frame, textvariable=url_var, font=self.FONT, width=40, bg=self.C_BG,
                 fg=self.C_TEXT, relief='flat', bd=0, highlightthickness=1,
                 highlightbackground="#EAEAEA").pack(side="left", padx=5)

        # 实施 A5：账号+密码合凭据卡（公开信息 URL 在卡外，隐私凭据在卡内）
        _cred_card = tk.Frame(parent, bg=self.C_BG, highlightthickness=1, highlightbackground=self.C_BORDER)
        _cred_card.pack(fill="x", padx=20, pady=(6, 6))
        acc_frame = tk.Frame(_cred_card, bg=self.C_BG)
        acc_frame.pack(fill="x", padx=10, pady=(10, 4))
        self._lbl(acc_frame, text="登录账号:", font=self.FONT, width=9, anchor="e").pack(side="left")
        acc_var = tk.StringVar(self.win, value=config.get('account', ''))
        acc_entry = tk.Entry(acc_frame, textvariable=acc_var, font=self.FONT, width=40, fg=self.C_MUTED, bg=self.C_BG)
        acc_entry.pack(side="left", padx=5)
        def _ph_entry(entry, placeholder, var):
            def on_focus_in(e):
                if var.get() == placeholder:
                    var.set('')
                    entry.configure(fg=self.C_TEXT)
            def on_focus_out(e):
                # 值为空或仍是 placeholder 文案时恢复占位符（防用户恰好输入了占位符文字被误判）
                if var.get() in (placeholder, ''):
                    var.set(placeholder)
                    entry.configure(fg=self.C_MUTED)
            entry.bind('<FocusIn>', on_focus_in)
            entry.bind('<FocusOut>', on_focus_out)
            if not var.get():
                var.set(placeholder)
            else:
                # 已有真实值：显示正常文字色，避免看起来像 placeholder
                entry.configure(fg=self.C_TEXT)
        _ph_entry(acc_entry, '输入手机号', acc_var)

        pwd_frame = tk.Frame(_cred_card, bg=self.C_BG)
        pwd_frame.pack(fill="x", padx=10, pady=(4, 10))
        self._lbl(pwd_frame, text="登录密码:", font=self.FONT, width=9, anchor="e").pack(side="left")
        # v1.4.8 P1-A：默认不保存密码——初始值永远不读已存密码（即便 config 里还有 legacy 值）
        # 仅当用户主动勾选「记住密码」且点保存时才落盘。已存密码提示用户可手动清除。
        pwd_var = tk.StringVar(self.win, value='')  # 永远从空开始，不预填
        pwd_entry = tk.Entry(pwd_frame, textvariable=pwd_var, font=self.FONT, width=40, show="", bg=self.C_BG)
        pwd_entry.pack(side="left", padx=5)
        pwd_var.set('输入密码')
        pwd_entry.configure(fg=self.C_MUTED)
        def _pwd_on_focus(e):
            if pwd_var.get() == '输入密码': pwd_var.set(''); pwd_entry.configure(fg=self.C_TEXT, show='*')
        def _pwd_on_blur(e):
            if not pwd_var.get(): pwd_var.set('输入密码'); pwd_entry.configure(fg=self.C_MUTED, show='')
        pwd_entry.bind('<FocusIn>', _pwd_on_focus)
        pwd_entry.bind('<FocusOut>', _pwd_on_blur)

        show_var = tk.BooleanVar(dlg, value=False)
        def toggle_pwd():
            pwd_entry.configure(show="" if show_var.get() else "*")
        tk.Checkbutton(pwd_frame, text="显示", variable=show_var, command=toggle_pwd,
                       font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_TEXT,
                       selectcolor=self.C_BG, activebackground=self.C_BG).pack(side="left")

        # v1.4.8 P1-A：「记住密码」默认关——勾选才落盘；取消勾选则清空已存密码
        remember_var = tk.BooleanVar(dlg, value=False)
        tk.Checkbutton(pwd_frame, text="记住密码", variable=remember_var,
                       font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_TEXT,
                       selectcolor=self.C_BG, activebackground=self.C_BG).pack(side="left", padx=(8,0))

        self._lbl(parent, text="⚠ 默认不保存密码（更安全）；勾选「记住密码」才会写入本机配置",
                 font=(self.FONT[0], 7), fg=self.C_MUTED).pack(pady=(10,0))

        def save_backend():
            from utils import Config as _CfgB
            s = _CfgB.load()  # 安全回退
            if not isinstance(s, dict):
                s = {}  # 顶层合法 JSON 但不是 dict（如 []）时防 TypeError
            # 关键判定：只有勾选「记住密码」才把密码写入；否则强制为空
            typed_pwd = '' if pwd_var.get() == '输入密码' else pwd_var.get()
            # v1.4.8 P1-C：勾选记住 → DPAPI 加密落盘（防明文 settings.json 被拷走即丢账号）
            if remember_var.get() and typed_pwd:
                try:
                    from dpapi_utils import enc as _dpapi_enc, is_available as _dpi_avail
                    if _dpi_avail():
                        _enc = _dpapi_enc(typed_pwd)
                        if _enc:
                            typed_pwd = _enc
                except Exception:
                    pass  # DPAPI 不可用 → 静默保留明文（与设计一致：降级可用）
            final_pwd = typed_pwd if remember_var.get() else ''
            s['backend'] = {
                'url': url_var.get().strip(),
                'account': '' if acc_var.get() in ('输入手机号', '') else acc_var.get().strip(),
                'password': final_pwd
            }
            try:
                _CfgB.save(s)  # 原子写入
            except Exception as e:
                messagebox.showerror("保存失败", str(e), parent=dlg)
                return
            if final_pwd:
                messagebox.showinfo("已保存", "商家后台配置已保存（密码已记忆）", parent=dlg)
            else:
                messagebox.showinfo("已保存", "商家后台配置已保存（密码未保存）", parent=dlg)

        self._mk_btn(parent, "保存配置", save_backend, kind='primary',
                  font=self.FONT_BOLD, width=15).pack(pady=15)

    def _build_api_page(self, parent, dlg=None):
        """API 管理：三个提供商独立配置，用户自填 Key 和模型名，消除隐私隐患"""
        import json
        api_cfg = get_api_config()
        providers = api_cfg.get('providers', {})
        active = api_cfg.get('active_provider', 'doubao')

        # 提供商预设
        PRESET_PROVIDERS = {
            'doubao':  {'name': '火山引擎（豆包）', 'endpoint': 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'},
            'qwen':    {'name': '阿里云百炼（千问）', 'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'},
            'glm':     {'name': '智谱清言（GLM）',   'endpoint': 'https://open.bigmodel.cn/api/paas/v4/chat/completions'},
        }

        # page 标题统一 FONT_HEADING + 无 emoji（emoji 保留在导航项）
        self._lbl(parent, text="API 提供商管理", font=self.FONT_HEADING, bg=self.C_BG, fg=self.C_TEXT).pack(
            padx=16, pady=(14, 2))  # 页标题边距统一 16/(14,2)（原 b-3 的 20 归一）
        self._lbl(parent, text="每个提供商独立配置 Key 和模型名，数据仅保存在本机",
                 font=(self.FONT[0], 8), fg=self.C_MUTED, bg=self.C_BG).pack(
            padx=16, pady=(0, 4))

        # 活跃提供商选择（机能单选：选中圆点填充亮黄）
        active_frame = tk.Frame(parent, bg=self.C_BG)
        active_frame.pack(fill="x", padx=20, pady=(8, 4))  # (b-3): padx=24→20, pady=(14,6)→(8,4)
        self._lbl(active_frame, text="当前使用:", font=self.FONT_BOLD, bg=self.C_BG, fg=self.C_TEXT).pack(side="left", padx=(0,8))
        active_var = tk.StringVar(self.win, value=active)
        for key, info in PRESET_PROVIDERS.items():
            tk.Radiobutton(active_frame, text=info['name'], variable=active_var,
                          value=key, font=self.FONT, bg=self.C_BG, fg=self.C_TEXT,
                          selectcolor=self.C_ACCENT, activebackground=self.C_BG,
                          bd=0, relief='flat', highlightthickness=0,
                          command=lambda: self._refresh_model_badge()).pack(side="left", padx=(12, 0))  # (b-3): padx=12→(12,0) 与左缘对齐

        # 三张提供商卡片（浅灰机能卡片 + 细黑切角边框，带滚轮）
        canvas = tk.Canvas(parent, highlightthickness=0, bg=self.C_BG)
        scroll = ttk.Scrollbar(parent, orient='vertical', command=canvas.yview)
        cards_frame = tk.Frame(canvas, bg=self.C_BG)
        cards_frame.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        wid = canvas.create_window((0, 0), window=cards_frame, anchor='nw')
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(wid, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        def _api_mw(e): canvas.yview_scroll(int(-1*(e.delta/120)), 'units')
        canvas.bind('<Enter>', lambda e: canvas.bind_all('<MouseWheel>', _api_mw))
        canvas.bind('<Leave>', lambda e: canvas.unbind_all('<MouseWheel>'))

        key_vars = {}
        model_vars = {}
        show_vars = {}

        def _api_entry(parent, var, width=50, show=None):
            """白底细黑切角边框输入框（终末地统一控件）"""
            e = tk.Entry(parent, textvariable=var, font=(self.FONT[0], 8), width=width,
                         show=show, relief='flat', bd=0,
                         highlightthickness=1, highlightbackground="#EAEAEA",
                         highlightcolor="#EAEAEA",
                         bg=self.C_BG, fg=self.C_TEXT, insertbackground=self.C_TEXT)
            return e

        for key, info in PRESET_PROVIDERS.items():
            cfg = providers.get(key, {}) if isinstance(providers, dict) else {}
            # 浅灰机能卡片（细黑边框 + 内边距）
            card = tk.Frame(cards_frame, bg=self.C_BG, highlightthickness=1,
                            highlightbackground="#EAEAEA", bd=0)
            card.pack(fill="x", padx=8, pady=8)  # (b-3): padx=4→8 卡片间 8px
            self._lbl(card, text=info['name'], font=(self.FONT[0], 10, 'bold'),
                     bg=self.C_BG, fg=self.C_TEXT, anchor="w").pack(
                fill="x", padx=16, pady=(8, 2))  # (b-3): padx=12→16

            # API Key 行
            kf = tk.Frame(card, bg=self.C_BG)
            kf.pack(fill="x", padx=16, pady=6)  # (b-3): padx=12→16, pady=4→6
            self._lbl(kf, text="API Key:", font=self.FONT, width=9, anchor="e",
                     bg=self.C_BG, fg=self.C_TEXT).pack(side="left")
            # v1.4.8 P1-C：若存的是 dpapi:v1: 密文，解密后填入；解密失败（跨机/损坏）
            # → Config.decrypt_value 返回空串 → 提示用户重填，绝不让界面卡住
            from utils import Config as _CfgKey
            _stored_key = cfg.get('api_key', '')
            _dec_key = _CfgKey.decrypt_value(_stored_key) if _stored_key else ''
            if _stored_key and not _dec_key and _stored_key.startswith("dpapi:v1:"):
                # 凭据失效：仅一次提示，UI 不阻塞
                try:
                    self.win.after(200, lambda k=key: messagebox.showwarning(
                        "凭据失效", f"提供商「{info['name']}」的 API Key 加密存储已失效\n"
                        f"（可能跨机/换账户），请重新填写。", parent=self.win))
                except Exception:
                    pass
            kv = tk.StringVar(self.win, value=_dec_key)
            ke = _api_entry(kf, kv, show='*')
            ke.pack(side="left", padx=6)
            sv = tk.BooleanVar(self.win, value=False)
            tk.Checkbutton(kf, text='显示', variable=sv, bg=self.C_BG, fg=self.C_TEXT,
                          selectcolor=self.C_ACCENT, activebackground=self.C_BG,
                          bd=0, relief='flat', highlightthickness=0,
                          command=lambda e=ke, v=sv: e.configure(show='' if v.get() else '*')).pack(side="left")
            key_vars[key] = kv
            show_vars[key] = sv

            # 默认历史模型名
            DEFAULT_MODELS = {
                'doubao': ['Doubao-Seed-2.1-pro', 'Doubao-1.5-vision-pro-32k'],
                'qwen':   ['qwen3.5-omni-flash'],
                'glm':    ['glm-4v-flash'],
            }
            history = cfg.get('model_history', []) if isinstance(cfg, dict) else []
            # 预置默认模型到历史
            for dm in DEFAULT_MODELS.get(key, []):
                if dm not in history:
                    history.append(dm)
            if cfg.get('model', '') and cfg['model'] not in history:
                history.insert(0, cfg['model'])

            mf = tk.Frame(card, bg=self.C_BG)
            mf.pack(fill="x", padx=16, pady=6)  # (b-3)
            self._lbl(mf, text="模型名称:", font=self.FONT, width=9, anchor="e",
                     bg=self.C_BG, fg=self.C_TEXT).pack(side="left")
            mv = tk.StringVar(self.win, value=cfg.get('model', ''))
            combo = ttk.Combobox(mf, textvariable=mv, values=history, font=(self.FONT[0], 8), width=47)
            combo.pack(side="left", padx=6)
            model_vars[key] = mv
            setattr(self, f'_api_combo_{key}', combo)
            setattr(self, f'_api_history_{key}', history)

            # Endpoint 行（预填，可改，完全由用户控制）
            ef = tk.Frame(card, bg=self.C_BG)
            ef.pack(fill="x", padx=16, pady=6)  # (b-3)
            self._lbl(ef, text="Endpoint:", font=self.FONT, width=9, anchor="e",
                     bg=self.C_BG, fg=self.C_TEXT).pack(side="left")
            ev = tk.StringVar(self.win, value=cfg.get('endpoint', info['endpoint']))
            _api_entry(ef, ev).pack(side="left", padx=6)
            setattr(self, f'_api_ep_{key}', ev)

            # 豆包专属：自定义推理接入点（可选）
            if key == 'doubao':
                cef = tk.Frame(card, bg=self.C_BG)
                cef.pack(fill="x", padx=16, pady=6)  # (b-3)
                self._lbl(cef, text="推理接入点:", font=self.FONT, width=9, anchor="e",
                         bg=self.C_BG, fg=self.C_TEXT).pack(side="left")
                cev = tk.StringVar(self.win, value=cfg.get('custom_endpoint', ''))
                _api_entry(cef, cev).pack(side="left", padx=6)
                setattr(self, f'_api_ce_{key}', cev)
                self._lbl(cef, text="如 ep-xxx，留空则用默认", font=(self.FONT[0], 7),
                         fg=self.C_MUTED, bg=self.C_BG).pack(side="left")

        def save_all():
            import json
            from utils import Config
            # v1.4.8 P1-C：用户键入的明文 API Key 在落盘前用 DPAPI 加密；
            # 已加密的（用户复制粘贴回原值）直接跳过；空串明文不加密。
            try:
                from dpapi_utils import enc as _dpapi_enc, is_available as _dpi_avail
            except Exception:
                _dpapi_enc = None
                _dpi_avail = lambda: False
            new_providers = {}
            for key in PRESET_PROVIDERS:
                model = model_vars[key].get().strip()
                history = getattr(self, f'_api_history_{key}', [])
                if model and model not in history:
                    history.insert(0, model)
                history = history[:10]  # 保留最近10个
                _typed_key = key_vars[key].get().strip()
                # 若用户粘贴回已是 dpapi:v1: 密文 → 跳过再加密（防双重包裹）
                if _typed_key and not _typed_key.startswith("dpapi:v1:") and _dpi_avail() and _dpapi_enc:
                    _enc_key = _dpapi_enc(_typed_key)
                    _typed_key = _enc_key if _enc_key else _typed_key
                new_providers[key] = {
                    'api_key': _typed_key,
                    'model': model,
                    'model_history': history,
                    'endpoint': getattr(self, f'_api_ep_{key}').get().strip(),
                }
                # 豆包自定义推理接入点
                if key == 'doubao':
                    new_providers[key]['custom_endpoint'] = getattr(self, '_api_ce_doubao').get().strip()
                # 刷新下拉列表
                combo = getattr(self, f'_api_combo_{key}', None)
                if combo:
                    combo['values'] = history
            # 原子写入（Config.save 先写 .tmp 再 os.replace，防止崩溃截断配置）
            s = Config.load()
            if not isinstance(s, dict):
                s = {}
            s['api'] = {
                'active_provider': active_var.get(),
                'providers': new_providers,
            }
            Config.save(s)
            self._refresh_model_badge()
            self.status_text.set(f"API 配置已保存 — 当前: {PRESET_PROVIDERS[active_var.get()]['name']}")

        self._mk_btn(parent, "保存", save_all, kind='primary',
                  font=self.FONT_BOLD, width=18).pack_configure(pady=(16, 18))
        # v1.4.x 导航重构：「💰 用量明细」入口已迁至导航独立页（stats_ui.StatsPagesMixin
        # ._build_usage_page，唯一实现）；API 设置页不再内置用量入口（用户明确要求）
