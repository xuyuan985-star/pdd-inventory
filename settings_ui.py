"""
PDD EZ — 设置页 UI 构建器 (Mixin)
从 gui.py 拆分：通用/商品/皮肤/校准/分辨率/后台 六个设置页面的构建逻辑。
"""
from tkinter import messagebox, ttk
import tkinter as tk

from config import THEMES, save_theme_pref
from utils import get_base_dir, get_api_config, Config


class SettingsUIMixin:
    """混入 App 类，提供所有设置页面构建方法。"""

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

        # ── 导出路径模块（浅灰白卡片容器）──
        _m1 = tk.Frame(content, bg=self.C_BG, highlightthickness=1, highlightbackground=self.C_BORDER)
        _m1.pack(fill="x", padx=20, pady=8)
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
        ttk.Separator(content, orient='horizontal').pack(fill='x', padx=20, pady=5)

        # ── 定位校准（AI 智能定位，v1.3 起唯一模式）──
        self._build_calibrate_tab(content)
        ttk.Separator(content, orient='horizontal').pack(fill='x', padx=20, pady=5)

        # ── 识别列配置模块（浅灰白卡片容器）──
        _m3 = tk.Frame(content, bg=self.C_BG, highlightthickness=1, highlightbackground=self.C_BORDER)
        _m3.pack(fill="x", padx=20, pady=8)
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
        sec_row = tk.Frame(content, bg=self.C_BG); sec_row.pack(pady=(4,2))
        self._lbl(sec_row, text="副模型（双模型验证）:", font=(self.FONT[0], 8),
                 fg=self.C_TEXT).pack(side="left")
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
        ttk.Combobox(sec_row, textvariable=sec_var, state='normal', width=22,
                     values=_sec_models,
                     font=(self.FONT[0], 8)).pack(side="left", padx=8)
        def _save_sec():
            _v = sec_var.get().strip() or 'glm-4v-flash'
            save_secondary_model(_v)
            self.col_status_var.set(f"副模型已保存：{_v}")
            self.status_text.set(f"副模型已保存：{_v}")
        self._mk_btn(sec_row, '保存', _save_sec, kind='primary',
                  font=(self.FONT[0], 7)).pack(side="left")
        self._lbl(content, text="双模型验证时主模型识别后由副模型复核（不一致标 ⚠）",
                 font=(self.FONT[0], 8), fg=self.C_MUTED).pack(pady=(0,6))

        # ── 授权管理（t12 P2-C）──
        self._build_license_card(content)

        # ── 补货策略（t13 P3-A）──
        self._build_replenishment_card(content)

    def _build_replenishment_card(self, parent):
        """t13 P3-A 补货策略卡片：模型单选（经典/加权）+ safety_days + in_transit_qty。

        用户裁定：默认 'classic'（一行公式逻辑都不许改）；切到 'weighted' 时
        会按 sku_id 关联历史库做加权日销，无历史自动回退经典并标注「经典(无历史)」。
        """
        try:
            from utils import get_replenishment_cfg, MODEL_CLASSIC, MODEL_WEIGHTED
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
            cur = {'model': 'classic', 'safety_days': 2, 'in_transit_qty': 0}

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

        # safety_days spinbox
        sd_row = tk.Frame(card, bg=self.C_BG); sd_row.pack(pady=(6, 2), padx=20, fill='x')
        self._lbl(sd_row, text="安全库存天数：", font=(self.FONT[0], 9),
                  bg=self.C_BG, fg=self.C_TEXT).pack(side='left', padx=(0, 6))
        _sd_var = tk.IntVar(self.win, value=int(cur.get('safety_days', 2) or 0))
        tk.Spinbox(sd_row, from_=0, to=30, textvariable=_sd_var, width=6,
                   font=(self.FONT[0], 9), relief='flat', bd=0, highlightthickness=1,
                   highlightbackground="#EAEAEA", highlightcolor="#EAEAEA",
                   bg="#FFFFFF", fg=self.C_TEXT, buttonbackground=self.C_BG).pack(side='left')
        self._lbl(sd_row, text="（加权模式：运输+此值=到货覆盖天数）",
                  font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_MUTED).pack(side='left', padx=(8, 0))

        # in_transit_qty spinbox
        it_row = tk.Frame(card, bg=self.C_BG); it_row.pack(pady=(6, 2), padx=20, fill='x')
        self._lbl(it_row, text="在途库存：", font=(self.FONT[0], 9),
                  bg=self.C_BG, fg=self.C_TEXT).pack(side='left', padx=(0, 6))
        _it_var = tk.IntVar(self.win, value=int(cur.get('in_transit_qty', 0) or 0))
        tk.Spinbox(it_row, from_=0, to=100000, textvariable=_it_var, width=8,
                   font=(self.FONT[0], 9), relief='flat', bd=0, highlightthickness=1,
                   highlightbackground="#EAEAEA", highlightcolor="#EAEAEA",
                   bg="#FFFFFF", fg=self.C_TEXT, buttonbackground=self.C_BG).pack(side='left')
        self._lbl(it_row, text="（加权模式：补货量 = (运输+安全)×日销 − 在途 − 库存，100 取整）",
                  font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_MUTED).pack(side='left', padx=(8, 0))

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
                cfg["replenishment"] = rep
                Config.save(cfg)
                self.status_text.set(f"补货策略已保存：model={rep['model']}  safety_days={rep['safety_days']}  in_transit={rep['in_transit_qty']}")
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
                        "无历史数据时自动回退经典并标注「经典(无历史)」。"),
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
        enf_row = tk.Frame(card, bg=self.C_BG); enf_row.pack(pady=(8, 2), padx=20, fill='x')
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
                # t24 修复包 A (BUG-13)：写盘失败时回滚 UI 到旧值，
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
        _key_entry = tk.Entry(in_row, textvariable=_key_var, font=self.FONT, width=60,
                              relief='flat', bd=0, highlightthickness=1,
                              highlightbackground="#EAEAEA", highlightcolor="#EAEAEA",
                              bg="#FFFFFF", fg=self.C_TEXT, insertbackground=self.C_TEXT)
        _key_entry.pack(side='left', padx=(0, 6), fill='x', expand=True)

        def _on_import():
            try:
                # t24 修复包 A (BUG-14)：写盘失败时回滚 _key_var 为旧值；
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
                # t24 修复包 A (BUG-14)：写盘失败时回滚 _key_var 为旧值
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
        self._lbl(parent, text="商品运输时效设置", font=self.FONT_HEADING).pack(pady=(15,2))
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

        canvas = tk.Canvas(canvas_frame, height=220, highlightthickness=0, bg=self.C_BG)
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
        self._lbl(parent, text="选择界面主题", font=self.FONT_HEADING).pack(pady=(15,2))
        self._lbl(parent, text="点击卡片即时切换，自动保存偏好", font=(self.FONT[0], 8), fg=self.C_MUTED).pack()

        cards_frame = tk.Frame(parent, bg=self.C_BG)
        cards_frame.pack(fill="x", padx=15, pady=10)

        def select_theme(name):
            self._apply_theme(name)
            save_theme_pref(name)
            self.status_text.set(f"皮肤已切换为「{name}」")
            for child in cards_frame.winfo_children():
                is_sel = getattr(child, '_skin_name', '') == name
                ac = THEMES.get(name, {}).get('C_ACCENT', '#3B82F6')
                child.configure(highlightbackground=ac if is_sel else "#E2E8F0",
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
            card = tk.Frame(cards_frame, bg="#FFFFFF",
                           highlightbackground=ac if is_sel else "#E2E8F0",
                           highlightthickness=2 if is_sel else 1)
            card.pack(fill="x", padx=4, pady=6)
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

    def _build_calibrate_tab(self, parent, dlg=None):
        """校准页：AI 智能视觉定位（v1.4 起唯一模式，绝对坐标已移除）"""
        import json, time as _time
        from datetime import datetime

        self._lbl(parent, text="定位校准", font=self.FONT_HEADING).pack(pady=(15,2))

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
        self._lbl(parent, text="商家后台快捷入口", font=self.FONT_HEADING).pack(pady=(15,2))
        self._lbl(parent, text="设置后可通过主页「🏪 商家后台」按钮一键打开", font=(self.FONT[0], 8), fg=self.C_MUTED).pack()

        config = self._get_backend_config()

        url_frame = tk.Frame(parent, bg=self.C_BG)
        url_frame.pack(fill="x", padx=20, pady=(15,5))
        self._lbl(url_frame, text="后台地址:", font=self.FONT, width=10, anchor="e").pack(side="left")
        url_var = tk.StringVar(self.win, value=config.get('url', 'https://mms.pinduoduo.com/'))
        tk.Entry(url_frame, textvariable=url_var, font=self.FONT, width=40, bg=self.C_BG,
                 fg=self.C_TEXT, relief='flat', bd=0, highlightthickness=1,
                 highlightbackground="#EAEAEA").pack(side="left", padx=5)

        acc_frame = tk.Frame(parent, bg=self.C_BG)
        acc_frame.pack(fill="x", padx=20, pady=5)
        self._lbl(acc_frame, text="登录账号:", font=self.FONT, width=10, anchor="e").pack(side="left")
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

        pwd_frame = tk.Frame(parent, bg=self.C_BG)
        pwd_frame.pack(fill="x", padx=20, pady=5)
        self._lbl(pwd_frame, text="登录密码:", font=self.FONT, width=10, anchor="e").pack(side="left")
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

        self._lbl(parent, text="API 提供商管理", font=self.FONT_TITLE, bg=self.C_BG, fg=self.C_TEXT).pack(pady=(18,2))
        self._lbl(parent, text="每个提供商独立配置 Key 和模型名，数据仅保存在本机",
                 font=(self.FONT[0], 8), fg=self.C_MUTED, bg=self.C_BG).pack()

        # 活跃提供商选择（机能单选：选中圆点填充亮黄）
        active_frame = tk.Frame(parent, bg=self.C_BG)
        active_frame.pack(fill="x", padx=24, pady=(14,6))
        self._lbl(active_frame, text="当前使用:", font=self.FONT_BOLD, bg=self.C_BG, fg=self.C_TEXT).pack(side="left", padx=(0,8))
        active_var = tk.StringVar(self.win, value=active)
        for key, info in PRESET_PROVIDERS.items():
            tk.Radiobutton(active_frame, text=info['name'], variable=active_var,
                          value=key, font=self.FONT, bg=self.C_BG, fg=self.C_TEXT,
                          selectcolor=self.C_ACCENT, activebackground=self.C_BG,
                          bd=0, relief='flat', highlightthickness=0,
                          command=lambda: self._refresh_model_badge()).pack(side="left", padx=12)

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
            card.pack(fill="x", padx=4, pady=8)
            self._lbl(card, text=info['name'], font=(self.FONT[0], 10, 'bold'),
                     bg=self.C_BG, fg=self.C_TEXT, anchor="w").pack(fill="x", padx=12, pady=(8, 2))

            # API Key 行
            kf = tk.Frame(card, bg=self.C_BG)
            kf.pack(fill="x", padx=12, pady=4)
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
            mf.pack(fill="x", padx=12, pady=4)
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
            ef.pack(fill="x", padx=12, pady=4)
            self._lbl(ef, text="Endpoint:", font=self.FONT, width=9, anchor="e",
                     bg=self.C_BG, fg=self.C_TEXT).pack(side="left")
            ev = tk.StringVar(self.win, value=cfg.get('endpoint', info['endpoint']))
            _api_entry(ef, ev).pack(side="left", padx=6)
            setattr(self, f'_api_ep_{key}', ev)

            # 豆包专属：自定义推理接入点（可选）
            if key == 'doubao':
                cef = tk.Frame(card, bg=self.C_BG)
                cef.pack(fill="x", padx=12, pady=4)
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
