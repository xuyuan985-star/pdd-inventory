"""
PDD EZ — 设置页 UI 构建器 (Mixin)
从 gui.py 拆分：通用/商品/皮肤/校准/分辨率/后台 六个设置页面的构建逻辑。
"""
import os, json
from tkinter import messagebox, ttk
import tkinter as tk

from config import THEMES, save_theme_pref
from utils import get_base_dir, get_api_config


class SettingsUIMixin:
    """混入 App 类，提供所有设置页面构建方法。"""

    def _build_general_page(self):
        """通用设置：导出路径 + API配置"""
        canvas = tk.Canvas(self.page_general, highlightthickness=0)
        scroll = ttk.Scrollbar(self.page_general, orient='vertical', command=canvas.yview)
        content = tk.Frame(canvas)
        content.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        wid = canvas.create_window((0, 0), window=content, anchor='nw')
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(wid, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        def _mw(e): canvas.yview_scroll(int(-1*(e.delta/120)), 'units')
        canvas.bind('<Enter>', lambda e: canvas.bind_all('<MouseWheel>', _mw))
        canvas.bind('<Leave>', lambda e: canvas.unbind_all('<MouseWheel>'))

        tk.Label(content, text='导出路径', font=self.FONT_HEADING).pack(pady=(15,5))
        pf = tk.Frame(content); pf.pack(pady=8, padx=20, fill='x')
        self.export_path_var = tk.StringVar(self.win, value=self._get_export_path())
        tk.Entry(pf, textvariable=self.export_path_var, font=self.FONT, width=50).pack(side='left')
        tk.Button(pf, text='浏览', command=lambda: self._pick_export_path(None), font=(self.FONT[0], 8)).pack(side='left', padx=5)
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
        tk.Button(pf, text='打开文件夹', command=open_export_dir, font=(self.FONT[0], 8)).pack(side='left', padx=5)
        tk.Button(content, text='保存', command=lambda: self._save_settings(None), font=(self.FONT[0], 8), bg=self.C_PRIMARY, fg='#FFFFFF').pack(pady=(5,10))
        ttk.Separator(content, orient='horizontal').pack(fill='x', padx=20, pady=5)

        # ── 定位校准（AI 智能定位，v1.3 起唯一模式）──
        self._build_calibrate_tab(content)
        ttk.Separator(content, orient='horizontal').pack(fill='x', padx=20, pady=5)

        # ── 识别列配置（v1.3 通用列：客户自主选择要识别的列）──
        tk.Label(content, text='识别列配置', font=self.FONT_HEADING).pack(pady=(5,2))
        self.col_status_var = tk.StringVar(self.win, value='')
        tk.Label(content, text="先「探测列」识别后台表格的所有列，再勾选要识别的列（库存/销量列为计算必需）",
                 font=(self.FONT[0], 8), fg=self.C_MUTED).pack()
        col_btn_row = tk.Frame(content); col_btn_row.pack(pady=8)
        tk.Button(col_btn_row, text='🔍 探测全部列', command=self._probe_columns,
                  font=(self.FONT[0], 8), bg=self.C_SECONDARY, fg='#FFFFFF').pack(side='left', padx=5)
        tk.Button(col_btn_row, text='⚙ 配置识别列', command=self._config_columns,
                  font=(self.FONT[0], 8)).pack(side='left', padx=5)
        tk.Label(content, textvariable=self.col_status_var,
                 font=(self.FONT[0], 8), fg=self.C_MUTED).pack(pady=(0,6))

        # ── 副模型（双模型验证用，🛡 勾选时生效）──
        sec_row = tk.Frame(content); sec_row.pack(pady=(4,2))
        tk.Label(sec_row, text="副模型（双模型验证）:", font=(self.FONT[0], 8),
                 fg=self.C_TEXT).pack(side="left")
        from utils import get_secondary_model, save_secondary_model
        sec_var = tk.StringVar(self.win, value=get_secondary_model())
        ttk.Combobox(sec_row, textvariable=sec_var, state='normal', width=22,
                     values=['glm-4v-flash', 'glm-4.6v', 'Doubao-Seed-2.1-pro',
                             'qwen3-omni-flash', 'qwen3.5-omni-flash'],
                     font=(self.FONT[0], 8)).pack(side="left", padx=8)
        def _save_sec():
            _v = sec_var.get().strip() or 'glm-4v-flash'
            save_secondary_model(_v)
            self.col_status_var.set(f"副模型已保存：{_v}")
            self.status_text.set(f"副模型已保存：{_v}")
        tk.Button(sec_row, text='保存', command=_save_sec,
                  font=(self.FONT[0], 7)).pack(side="left")
        tk.Label(content, text="双模型验证时主模型识别后由副模型复核（不一致标 ⚠）",
                 font=(self.FONT[0], 8), fg=self.C_MUTED).pack(pady=(0,6))

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
                result = ocr_table(shot, columns=None, table_bbox=bbox)
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
        dlg.geometry("480x560")
        dlg.configure(bg=self.C_BG)
        tk.Label(dlg, text="勾选要识别的列", font=self.FONT_HEADING,
                 bg=self.C_BG, fg=self.C_TEXT).pack(pady=(10,2))
        tk.Label(dlg, text="库存/销量列为补货计算必需，取消后计算列将为空",
                 font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_MUTED).pack()

        # 列勾选（可滚动）
        canvas = tk.Canvas(dlg, bg=self.C_SURFACE, highlightthickness=0)
        sb = tk.Scrollbar(dlg, orient="vertical", command=canvas.yview)
        list_frame = tk.Frame(canvas, bg=self.C_SURFACE)
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
                           bg=self.C_SURFACE, fg=self.C_TEXT,
                           selectcolor=self.C_SURFACE, activebackground=self.C_SURFACE,
                           anchor="w").pack(fill="x", padx=8, pady=1)

        # 核心列映射
        map_frame = tk.Frame(dlg, bg=self.C_BG)
        map_frame.pack(fill="x", padx=15, pady=(8,2))
        tk.Label(map_frame, text="核心列映射（后台列名变化时修改）",
                 font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_MUTED).pack(anchor="w")
        mapping = cfg['mapping']
        map_vars = {}
        for field, label in [('name', '商品信息列(含ID)'), ('stock', '库存列'),
                             ('sales', '销量列'), ('region', '销售区域列'), ('warehouse', '仓库信息列')]:
            row = tk.Frame(map_frame, bg=self.C_BG)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=label, font=(self.FONT[0], 8), bg=self.C_BG, fg=self.C_TEXT,
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

        tk.Button(dlg, text="保存", command=save, font=self.FONT_BOLD,
                  bg=self.C_PRIMARY, fg="#FFFFFF", width=14).pack(pady=(8,12))
        dlg.transient(self.win)
        dlg.grab_set()

    def _pick_export_path(self, parent):
        from tkinter import filedialog
        path = filedialog.askdirectory(title="选择导出文件夹")
        if path:
            self.export_path_var.set(path)

    def _get_export_path(self):
        import json
        settings_file = os.path.join(get_base_dir(), 'settings.json')
        try:
            with open(settings_file, 'r', encoding='utf-8') as f:
                s = json.load(f)
                return s.get('export_path', os.path.join(os.path.expanduser('~'), 'Desktop'))
        except:
            return os.path.join(os.path.expanduser('~'), 'Desktop')

    def _save_settings(self, dlg):
        import json, os as _os, tempfile
        settings_file = os.path.join(get_base_dir(), 'settings.json')
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
        tk.Label(parent, text="商品运输时效设置", font=self.FONT_HEADING).pack(pady=(15,2))
        tk.Label(parent, text="不同商品发往不同地区，运输时间可能不同", font=(self.FONT[0], 8), fg=self.C_MUTED).pack()

        # 地区选择
        sel_frame = tk.Frame(parent)
        sel_frame.pack(fill="x", padx=20, pady=(12,5))
        tk.Label(sel_frame, text="选择地区:", font=self.FONT).pack(side="left")
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
            if not messagebox.askyesno("确认删除", f"确定删除地区「{region}」及其所有商品时效设置？\n已识别的缓存数据也会一并清除。"):
                return
            if region in self.regions:
                del self.regions[region]
            if region in self.cache:
                del self.cache[region]
            self._save_regions()
            new_names = sorted(self.cache.keys()) if self.cache else sorted(self.regions.keys())
            if not new_names:
                new_names = ['（暂无识别数据）']
            region_combo['values'] = new_names
            self._settings_region_var.set(new_names[0])
            for w in self._settings_list_frame.winfo_children():
                w.destroy()
            tk.Label(self._settings_list_frame, text="地区已删除",
                     font=(self.FONT[0], 8), fg=self.C_MUTED).pack(pady=20)
            self._update_tabs()
            self.status_text.set(f"地区「{region}」已删除")

        tk.Button(sel_frame, text="删除地区", relief='flat', command=delete_region,
                  font=(self.FONT[0], 8), fg=self.C_RED).pack(side="left", padx=5)

        # 商品列表区（可滚动）
        canvas_frame = tk.Frame(parent)
        canvas_frame.pack(fill="both", expand=True, padx=20, pady=5)

        canvas = tk.Canvas(canvas_frame, height=220, highlightthickness=0)
        scrollbar = tk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
        self._settings_list_frame = tk.Frame(canvas)

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
                tk.Label(self._settings_list_frame, text="暂无识别数据，请先截图识别",
                         font=(self.FONT[0], 8), fg=self.C_MUTED).pack(pady=20)
                return

            products = []
            if region in self.cache:
                for item in self.cache[region].get('items', []):
                    name = item.get('name', '')
                    if name and name not in products:
                        products.append(name)

            if not products:
                tk.Label(self._settings_list_frame, text="该地区暂无商品，请先截图识别",
                         font=(self.FONT[0], 8), fg=self.C_MUTED).pack(pady=20)
                return

            hdr = tk.Frame(self._settings_list_frame)
            hdr.pack(fill="x", pady=(0,4))
            tk.Label(hdr, text="商品名称", font=self.FONT_BOLD, width=22, anchor="w").pack(side="left")
            tk.Label(hdr, text="运输天数", font=self.FONT_BOLD, width=10).pack(side="left", padx=5)

            spinboxes = {}
            current_settings = self.regions.get(region, {})
            if not isinstance(current_settings, dict):
                current_settings = {}

            for prod in products:
                row = tk.Frame(self._settings_list_frame)
                row.pack(fill="x", pady=1)
                tk.Label(row, text=prod, font=self.FONT, width=22, anchor="w").pack(side="left")
                spin = tk.Spinbox(row, from_=1, to=30, width=8, font=self.FONT)
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
                    self.regions[region][prod] = int(spin.get())
                except ValueError:
                    self.regions[region][prod] = 3
            self._save_regions()
            self.status_text.set(f"「{region}」商品运输时效已保存 — {len(spinboxes)} 个商品")
            if region == self.region_var.get() and region in self.cache:
                self._calc_from_items(self.cache[region]['items'])

        btn_frame = tk.Frame(parent)
        btn_frame.pack(pady=10)
        tk.Button(btn_frame, text="保存时效设置", command=save_all,
                  bg="#4CAF50", fg="#FFFFFF", font=self.FONT_BOLD).pack(side="left", padx=5)

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
                spin.delete(0, "end")
                spin.insert(0, str(days))
            self.status_text.set(f"已将所有商品运输时效设为 {days} 天（记得点保存）")

        tk.Button(btn_frame, text="全部设为 3 天", command=lambda: set_all_days(3),
                  font=(self.FONT[0], 8)).pack(side="left", padx=5)
        tk.Button(btn_frame, text="全部设为 5 天", command=lambda: set_all_days(5),
                  font=(self.FONT[0], 8)).pack(side="left", padx=5)

    def _build_skin_tab(self, parent):
        """主题选择：四套主题 2×2 网格，点击预览卡即切换"""
        tk.Label(parent, text="选择界面主题", font=self.FONT_HEADING).pack(pady=(15,2))
        tk.Label(parent, text="点击卡片即时切换，自动保存偏好", font=(self.FONT[0], 8), fg=self.C_MUTED).pack()

        cards_frame = tk.Frame(parent)
        cards_frame.pack(fill="both", expand=True, padx=15, pady=10)
        cards_frame.grid_columnconfigure(0, weight=1, uniform="card")
        cards_frame.grid_columnconfigure(1, weight=1, uniform="card")
        cards_frame.grid_rowconfigure(0, weight=1, uniform="card")
        cards_frame.grid_rowconfigure(1, weight=1, uniform="card")

        def select_theme(name):
            self._apply_theme(name)
            save_theme_pref(name)
            self.status_text.set(f"皮肤已切换为「{name}」")
            for child in cards_frame.winfo_children():
                is_sel = getattr(child, '_skin_name', '') == name
                child.configure(highlightbackground="#3B82F6" if is_sel else "#E2E8F0",
                               highlightthickness=2 if is_sel else 1)
                for gc in child.winfo_children():
                    if isinstance(gc, tk.Frame):
                        for gcc in gc.winfo_children():
                            if isinstance(gcc, tk.Label) and gcc.cget('text') == '✓ 当前':
                                if is_sel:
                                    gcc.configure(text='✓ 当前', fg='#3B82F6')
                                else:
                                    gcc.configure(text='')

        for i, (name, theme_data) in enumerate(THEMES.items()):
            p = theme_data['C_PRIMARY']
            s = theme_data['C_SECONDARY']
            bg = theme_data['C_BG']
            sf = theme_data['C_SURFACE']
            ac = theme_data['C_ACCENT']
            tx = theme_data['C_TEXT']

            is_sel = name == self._theme_name
            card = tk.Frame(cards_frame, bg="#FFFFFF",
                           highlightbackground="#3B82F6" if is_sel else "#E2E8F0",
                           highlightthickness=2 if is_sel else 1)
            card.grid(row=i // 2, column=i % 2, padx=6, pady=6, sticky="nsew")
            card._skin_name = name
            card._skip_theme = True

            mock = tk.Frame(card, bg="#FFFFFF", height=110)
            mock.pack(fill="x", padx=1, pady=1)
            mock.pack_propagate(False)

            bar = tk.Frame(mock, bg=p, height=22)
            bar.pack(fill="x")
            bar.pack_propagate(False)
            tk.Label(bar, text="PDD", font=("Microsoft YaHei UI", 7, "bold"),
                    bg=p, fg="#FFFFFF").place(x=8, y=2)

            body = tk.Frame(mock, bg=bg)
            body.pack(fill="both", expand=True)
            sim_card = tk.Frame(body, bg=sf, height=28, highlightbackground=theme_data['C_BORDER'],
                               highlightthickness=1)
            sim_card.pack(fill="x", padx=10, pady=8)
            sim_card.pack_propagate(False)
            tk.Label(sim_card, text="库存 500  销量 50", font=("Microsoft YaHei UI", 6),
                    bg=sf, fg=tx).place(x=6, y=4)
            tag = tk.Frame(sim_card, bg=theme_data['C_YELLOW_BG'], width=28, height=12)
            tag.place(x=130, y=6)
            tag.pack_propagate(False)
            btn = tk.Frame(body, bg=ac, width=50, height=12)
            btn.place(x=15, y=50)
            btn.pack_propagate(False)

            info = tk.Frame(card, bg="#FFFFFF")
            info.pack(fill="x", padx=8, pady=(6,4))
            tk.Label(info, text=theme_data['label'], font=self.FONT_BOLD,
                    bg="#FFFFFF", fg="#1E293B").pack(anchor="w")
            tk.Label(info, text=theme_data['desc'], font=(self.FONT[0], 7),
                    bg="#FFFFFF", fg="#94A3B8").pack(anchor="w")
            if is_sel:
                tk.Label(info, text="✓ 当前", font=(self.FONT[0], 8, 'bold'),
                        bg="#FFFFFF", fg="#3B82F6").pack(anchor="w")
            else:
                tk.Label(info, text=" ", font=(self.FONT[0], 8),
                        bg="#FFFFFF", fg="#FFFFFF").pack(anchor="w")

            swatch = tk.Frame(card, bg="#FFFFFF", height=14)
            swatch.pack(fill="x", padx=8, pady=(0,6))
            swatch.pack_propagate(False)
            for j, c in enumerate([p, s, ac, bg, sf]):
                dot = tk.Frame(swatch, bg=c, width=14, height=14, highlightbackground="#E2E8F0",
                              highlightthickness=1)
                dot.place(x=j * 18, y=0)
                dot.pack_propagate(False)

            for w in [card, mock, info, swatch] + list(card.winfo_children()):
                try:
                    w.bind("<Button-1>", lambda e, n=name: select_theme(n))
                except:
                    pass

    def _build_calibrate_tab(self, parent, dlg=None):
        """校准页：AI 智能视觉定位（v1.4 起唯一模式，绝对坐标已移除）"""
        import json, time as _time
        from datetime import datetime

        tk.Label(parent, text="定位校准", font=self.FONT_HEADING).pack(pady=(15,2))

        from utils import Config as _Cfg3
        s = _Cfg3.load()  # 安全回退
        cal = s.get('calibrate')
        if not isinstance(cal, dict):
            cal = {'mode': 'ai', 'ai': {}}

        # ── AI 模式卡片（v1.4 起唯一模式，无模式选择器）──
        ai_card = tk.Frame(parent, bg=self.C_SURFACE, highlightthickness=1, highlightbackground=self.C_BORDER)

        ai_status_lbl = tk.Label(ai_card, text="", font=(self.FONT[0], 8), fg=self.C_TEXT, bg=self.C_SURFACE)
        ai_status_lbl.pack(pady=5)
        ai_coords_lbl = tk.Label(ai_card, text="", font=self.FONT, fg=self.C_PRIMARY, bg=self.C_SURFACE)
        ai_coords_lbl.pack(pady=2)
        ai_conf_lbl = tk.Label(ai_card, text="", font=(self.FONT[0], 8), fg=self.C_MUTED, bg=self.C_SURFACE)
        ai_conf_lbl.pack(pady=2)
        ai_res_lbl = tk.Label(ai_card, text="", font=(self.FONT[0], 8), fg=self.C_MUTED, bg=self.C_SURFACE)
        ai_res_lbl.pack(pady=2)

        ai_btn_frame = tk.Frame(ai_card, bg=self.C_SURFACE)
        ai_btn_frame.pack(pady=8)

        def do_ai_locate():
            ai_status_lbl.configure(text="正在智能识别页面元素...")
            self.win.update()
            try:
                import pyautogui as pg
                from vision import ai_locate_elements
                result = ai_locate_elements()
                if not result:
                    ai_status_lbl.configure(text="定位失败：API 返回空或校验不通过")
                    _refresh_cards()
                    return

                screen_w, screen_h = pg.size()
                cal['ai'] = {
                    'last_time': _time.time(),
                    'dropdown': result['dropdown'],
                    'query': result['query'],
                    'confidence': result['confidence'],
                    'screen_width': result['screen_width'],
                    'screen_height': result['screen_height'],
                }
                cal['mode'] = 'ai'
                s['calibrate'] = cal
                from utils import Config as _CfgA
                _CfgA.save(s)  # 原子写入
                ai_status_lbl.configure(text="✅ 定位完成")
                self.status_text.set("AI 智能定位完成")
            except Exception as e:
                ai_status_lbl.configure(text=f"定位失败: {str(e)[:50]}")
            _refresh_cards()

        tk.Button(ai_btn_frame, text="立即定位", command=do_ai_locate,
                  font=self.FONT_BOLD, bg=self.C_PRIMARY, fg="#FFFFFF", width=12).pack(side='left', padx=5)
        tk.Button(ai_btn_frame, text="测试点击", command=lambda: _test_click(cal),
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
                pg.click(dd['x'], dd['y'])
                self.status_text.set(f"已点击下拉框 ({dd['x']}, {dd['y']})，请确认是否展开")

        _refresh_cards()

    def _build_backend_tab(self, parent, dlg=None):
        """配置拼多多商家后台链接和登录凭据"""
        tk.Label(parent, text="商家后台快捷入口", font=self.FONT_HEADING).pack(pady=(15,2))
        tk.Label(parent, text="设置后可通过主页「🏪 商家后台」按钮一键打开", font=(self.FONT[0], 8), fg=self.C_MUTED).pack()

        config = self._get_backend_config()

        url_frame = tk.Frame(parent)
        url_frame.pack(fill="x", padx=20, pady=(15,5))
        tk.Label(url_frame, text="后台地址:", font=self.FONT, width=10, anchor="e").pack(side="left")
        url_var = tk.StringVar(self.win, value=config.get('url', 'https://mms.pinduoduo.com/'))
        tk.Entry(url_frame, textvariable=url_var, font=self.FONT, width=40).pack(side="left", padx=5)

        acc_frame = tk.Frame(parent)
        acc_frame.pack(fill="x", padx=20, pady=5)
        tk.Label(acc_frame, text="登录账号:", font=self.FONT, width=10, anchor="e").pack(side="left")
        acc_var = tk.StringVar(self.win, value=config.get('account', ''))
        acc_entry = tk.Entry(acc_frame, textvariable=acc_var, font=self.FONT, width=40, fg=self.C_MUTED)
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

        pwd_frame = tk.Frame(parent)
        pwd_frame.pack(fill="x", padx=20, pady=5)
        tk.Label(pwd_frame, text="登录密码:", font=self.FONT, width=10, anchor="e").pack(side="left")
        pwd_var = tk.StringVar(self.win, value=config.get('password', ''))
        pwd_entry = tk.Entry(pwd_frame, textvariable=pwd_var, font=self.FONT, width=40, show="*" if config.get('password') else "")
        pwd_entry.pack(side="left", padx=5)
        if not config.get('password'):
            pwd_entry.configure(fg=self.C_MUTED)
            pwd_var.set('输入密码')
            pwd_entry.configure(show="")
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
                       font=(self.FONT[0], 8)).pack(side="left")

        tk.Label(parent, text="⚠ 密码以明文存储在本机配置文件，请确保电脑安全",
                 font=(self.FONT[0], 7), fg=self.C_MUTED).pack(pady=(10,0))

        def save_backend():
            from utils import Config as _CfgB
            s = _CfgB.load()  # 安全回退
            if not isinstance(s, dict):
                s = {}  # 顶层合法 JSON 但不是 dict（如 []）时防 TypeError
            s['backend'] = {
                'url': url_var.get().strip(),
                'account': '' if acc_var.get() in ('输入手机号', '') else acc_var.get().strip(),
                'password': '' if pwd_var.get() == '输入密码' else pwd_var.get()
            }
            try:
                _CfgB.save(s)  # 原子写入
            except Exception as e:
                messagebox.showerror("保存失败", str(e), parent=dlg)
                return
            messagebox.showinfo("已保存", "商家后台配置已保存", parent=dlg)

        tk.Button(parent, text="保存配置", command=save_backend,
                  font=self.FONT_BOLD, bg="#4CAF50", fg="#FFFFFF", width=15).pack(pady=15)

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

        tk.Label(parent, text="API 提供商管理", font=self.FONT_HEADING, bg=self.C_BG, fg=self.C_TEXT).pack(pady=(15,2))
        tk.Label(parent, text="每个提供商独立配置 Key 和模型名，数据仅保存在本机",
                 font=(self.FONT[0], 8), fg=self.C_MUTED, bg=self.C_BG).pack()

        # 活跃提供商选择
        active_frame = tk.Frame(parent, bg=self.C_BG)
        active_frame.pack(fill="x", padx=20, pady=(12,5))
        tk.Label(active_frame, text="当前使用:", font=self.FONT, bg=self.C_BG, fg=self.C_TEXT).pack(side="left")
        active_var = tk.StringVar(self.win, value=active)
        for key, info in PRESET_PROVIDERS.items():
            tk.Radiobutton(active_frame, text=info['name'], variable=active_var,
                          value=key, font=self.FONT, bg=self.C_BG, fg=self.C_TEXT,
                          selectcolor=self.C_BG, activebackground=self.C_BG,
                          command=lambda: self._refresh_model_badge()).pack(side="left", padx=10)

        # 三张提供商卡片（带滚轮）
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

        for key, info in PRESET_PROVIDERS.items():
            cfg = providers.get(key, {}) if isinstance(providers, dict) else {}
            card = tk.LabelFrame(cards_frame, text=f" {info['name']} ", font=self.FONT_BOLD,
                                fg=self.C_PRIMARY, bg=self.C_BG, padx=10, pady=8)
            card.pack(fill="x", pady=6)

            # API Key 行
            kf = tk.Frame(card, bg=self.C_BG)
            kf.pack(fill="x", pady=3)
            tk.Label(kf, text="API Key:", font=self.FONT, width=9, anchor="e", bg=self.C_BG, fg=self.C_TEXT).pack(side="left")
            kv = tk.StringVar(self.win, value=cfg.get('api_key', ''))
            ke = tk.Entry(kf, textvariable=kv, font=(self.FONT[0], 8), width=50, show='*',
                         bg=self.C_SURFACE, fg=self.C_TEXT, insertbackground=self.C_TEXT)
            ke.pack(side="left", padx=5)
            sv = tk.BooleanVar(self.win, value=False)
            tk.Checkbutton(kf, text='显示', variable=sv, bg=self.C_BG, fg=self.C_TEXT,
                          selectcolor=self.C_BG, activebackground=self.C_BG,
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
            mf.pack(fill="x", pady=3)
            tk.Label(mf, text="模型名称:", font=self.FONT, width=9, anchor="e", bg=self.C_BG, fg=self.C_TEXT).pack(side="left")
            mv = tk.StringVar(self.win, value=cfg.get('model', ''))
            combo = ttk.Combobox(mf, textvariable=mv, values=history, font=(self.FONT[0], 8), width=47)
            combo.pack(side="left", padx=5)
            model_vars[key] = mv
            setattr(self, f'_api_combo_{key}', combo)
            setattr(self, f'_api_history_{key}', history)

            # Endpoint 行（预填，可改，完全由用户控制）
            ef = tk.Frame(card, bg=self.C_BG)
            ef.pack(fill="x", pady=3)
            tk.Label(ef, text="Endpoint:", font=self.FONT, width=9, anchor="e", bg=self.C_BG, fg=self.C_TEXT).pack(side="left")
            ev = tk.StringVar(self.win, value=cfg.get('endpoint', info['endpoint']))
            tk.Entry(ef, textvariable=ev, font=(self.FONT[0], 8), width=50,
                    bg=self.C_SURFACE, fg=self.C_TEXT, insertbackground=self.C_TEXT).pack(side="left", padx=5)
            setattr(self, f'_api_ep_{key}', ev)

            # 豆包专属：自定义推理接入点（可选）
            if key == 'doubao':
                cef = tk.Frame(card, bg=self.C_BG)
                cef.pack(fill="x", pady=3)
                tk.Label(cef, text="推理接入点:", font=self.FONT, width=9, anchor="e",
                         bg=self.C_BG, fg=self.C_TEXT).pack(side="left")
                cev = tk.StringVar(self.win, value=cfg.get('custom_endpoint', ''))
                tk.Entry(cef, textvariable=cev, font=(self.FONT[0], 8), width=50,
                        bg=self.C_SURFACE, fg=self.C_TEXT, insertbackground=self.C_TEXT).pack(side="left", padx=5)
                setattr(self, f'_api_ce_{key}', cev)
                tk.Label(cef, text="如 ep-xxx，留空则用默认", font=(self.FONT[0], 7),
                         fg=self.C_MUTED, bg=self.C_BG).pack(side="left")

        def save_all():
            import json
            from utils import Config
            new_providers = {}
            for key in PRESET_PROVIDERS:
                model = model_vars[key].get().strip()
                history = getattr(self, f'_api_history_{key}', [])
                if model and model not in history:
                    history.insert(0, model)
                history = history[:10]  # 保留最近10个
                new_providers[key] = {
                    'api_key': key_vars[key].get().strip(),
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

        tk.Button(parent, text="保存", command=save_all,
                  font=self.FONT_BOLD, bg=self.C_PRIMARY, fg="#FFFFFF", width=18).pack(pady=12)
