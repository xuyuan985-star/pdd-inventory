"""
PDD EZ — 设置页 UI 构建器 (Mixin)
从 gui.py 拆分：通用/商品/皮肤/校准/分辨率/后台 六个设置页面的构建逻辑。
"""
import os, json
from tkinter import messagebox, ttk
import tkinter as tk

from config import THEMES, RESOLUTION_PRESETS, save_theme_pref, load_resolution_pref, save_resolution_pref
from utils import get_base_dir, get_api_config


class SettingsUIMixin:
    """混入 App 类，提供所有设置页面构建方法。"""

    def _build_general_page(self):
        """通用设置：导出路径 + API配置 + 截图裁剪"""
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
        canvas.bind('<Leave>', lambda e: canvas.unbind('<MouseWheel>'))

        tk.Label(content, text='导出路径', font=self.FONT_HEADING).pack(pady=(15,5))
        pf = tk.Frame(content); pf.pack(pady=8, padx=20, fill='x')
        self.export_path_var = tk.StringVar(self.win, value=self._get_export_path())
        tk.Entry(pf, textvariable=self.export_path_var, font=self.FONT, width=50).pack(side='left')
        tk.Button(pf, text='浏览', command=lambda: self._pick_export_path(None), font=(self.FONT[0], 8)).pack(side='left', padx=5)
        tk.Button(content, text='保存', command=lambda: self._save_settings(None), font=(self.FONT[0], 8), bg=self.C_PRIMARY, fg='#FFFFFF').pack(pady=(5,10))
        ttk.Separator(content, orient='horizontal').pack(fill='x', padx=20, pady=5)

        ttk.Separator(content, orient='horizontal').pack(fill='x', padx=20, pady=5)
        tk.Label(content, text='截图裁剪', font=self.FONT_HEADING).pack(pady=(5,2))
        cf = tk.Frame(content); cf.pack(pady=5)
        tk.Label(cf, text='左:', font=(self.FONT[0], 8), fg=self.C_TEXT).pack(side='left')
        left_var = tk.StringVar(self.win, value='0.11')
        tk.Entry(cf, textvariable=left_var, font=(self.FONT[0], 8), width=5).pack(side='left', padx=3)
        tk.Label(cf, text='上:', font=(self.FONT[0], 8), fg=self.C_TEXT).pack(side='left', padx=(10,0))
        top_var = tk.StringVar(self.win, value='0.40')
        tk.Entry(cf, textvariable=top_var, font=(self.FONT[0], 8), width=5).pack(side='left', padx=3)
        def save_crop():
            import json
            sf = os.path.join(get_base_dir(), 'settings.json')
            try: s = json.load(open(sf, 'r', encoding='utf-8'))
            except: s = {}
            try: s['crop'] = {'left': float(left_var.get()), 'top': float(top_var.get())}
            except: s['crop'] = {'left': 0.11, 'top': 0.40}
            json.dump(s, open(sf, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        tk.Button(cf, text='保存', command=save_crop, font=(self.FONT[0], 7)).pack(side='left', padx=10)

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
        import json
        settings_file = os.path.join(get_base_dir(), 'settings.json')
        path = self.export_path_var.get().strip()
        if not path:
            messagebox.showwarning("路径为空", "请先选择或输入导出路径", parent=dlg)
            return
        try:
            with open(settings_file, 'r', encoding='utf-8') as f:
                s = json.load(f)
        except:
            s = {}
        s['export_path'] = path
        try:
            with open(settings_file, 'w', encoding='utf-8') as f:
                json.dump(s, f, ensure_ascii=False, indent=2)
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
        self._settings_region_var = tk.StringVar(dlg, value=region_names[0] if region_names else '')
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

    def _build_skin_tab(self, parent, dlg=None):
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
        """点击校准：记录用户手动点击的销售区域和查询按钮位置"""
        import json
        tk.Label(parent, text="点击位置校准", font=self.FONT_HEADING).pack(pady=(15,2))
        tk.Label(parent, text="打开PDD后台，依次点击两个位置，软件自动记录坐标",
                 font=(self.FONT[0], 8), fg=self.C_MUTED).pack()

        settings_file = os.path.join(get_base_dir(), 'settings.json')
        try:
            with open(settings_file, 'r', encoding='utf-8') as f: s = json.load(f)
        except: s = {}
        cal = s.get('calibrate', {})

        def show_val(key, label):
            v = cal.get(key, {})
            txt = f"{label}: X={v.get('x','?')} Y={v.get('y','?')}" if v else f"{label}: 未校准"
            return tk.Label(parent, text=txt, font=self.FONT, fg=self.C_TEXT)

        lbl_dd = show_val('dropdown', '销售区域文本框')
        lbl_dd.pack(pady=(15,3))
        lbl_qq = show_val('query', '查询按钮')
        lbl_qq.pack(pady=3)

        offset_x = cal.get('query',{}).get('x',0) - cal.get('dropdown',{}).get('x',0)
        offset_y = cal.get('query',{}).get('y',0) - cal.get('dropdown',{}).get('y',0)
        tk.Label(parent, text=f"查询相对偏移: ΔX={offset_x} ΔY={offset_y}" if cal else "查询相对偏移: 未校准",
                 font=(self.FONT[0], 8), fg=self.C_MUTED).pack(pady=3)

        mode_var = tk.StringVar(dlg, value=cal.get('mode', 'absolute'))
        mode_frame = tk.Frame(parent)
        mode_frame.pack(pady=10)
        tk.Label(mode_frame, text="定位模式:", font=self.FONT, fg=self.C_TEXT).pack(side='left')
        tk.Radiobutton(mode_frame, text="绝对坐标（直接使用校准位置）", variable=mode_var,
                       value='absolute', font=(self.FONT[0], 8), fg=self.C_TEXT,
                       selectcolor=self.C_BG, activebackground=self.C_BG).pack(anchor='w')
        tk.Radiobutton(mode_frame, text="相对偏移（文本框模板匹配 + 校准偏移推算查询按钮）", variable=mode_var,
                       value='offset', font=(self.FONT[0], 8), fg=self.C_TEXT,
                       selectcolor=self.C_BG, activebackground=self.C_BG).pack(anchor='w')

        def save_mode():
            cal['mode'] = mode_var.get()
            s['calibrate'] = cal
            with open(settings_file, 'w', encoding='utf-8') as f:
                json.dump(s, f, ensure_ascii=False, indent=2)
            self.status_text.set(f"定位模式已设为: {'绝对坐标' if mode_var.get()=='absolute' else '相对偏移'}")

        tk.Button(mode_frame, text="保存模式", command=save_mode,
                  font=(self.FONT[0], 8)).pack(pady=5)

        status_lbl = tk.Label(parent, text="", font=(self.FONT[0], 8), fg=self.C_ACCENT)
        status_lbl.pack(pady=5)

        def start_calibrate():
            import pyautogui, json
            sf = os.path.join(get_base_dir(), 'settings.json')
            try: s2 = json.load(open(sf, 'r', encoding='utf-8'))
            except: s2 = {}
            cal2 = s2.get('calibrate', {})
            pos = {}
            for step, hint in enumerate(['销售区域文本框', '查询按钮']):
                pw = tk.Toplevel()
                pw.title(f"校准 {step+1}/2")
                pw.geometry("400x200"); pw.attributes('-topmost', True)
                pw.configure(bg=self.C_BG)
                tk.Label(pw, text=f"第{step+1}步：鼠标移到「{hint}」上", font=self.FONT_HEADING, fg=self.C_TEXT).pack(pady=(15,5))
                cdlbl = tk.Label(pw, text="", font=('Consolas', 36, 'bold'), fg=self.C_PRIMARY)
                cdlbl.pack(pady=10)
                tk.Label(pw, text="倒计时结束后自动记录鼠标位置", font=(self.FONT[0], 8), fg=self.C_MUTED).pack()

                recorded = [False]
                def countdown(n=3):
                    if recorded[0]: return
                    if n > 0:
                        cdlbl.configure(text=str(n))
                        pw.after(1000, lambda: countdown(n-1))
                    else:
                        pos[step] = pyautogui.position()
                        recorded[0] = True
                        cdlbl.configure(text="✓ 已记录")
                        pw.after(500, pw.destroy)
                pw.after(500, countdown)
                pw.grab_set(); pw.wait_window()
                if step not in pos: status_lbl.configure(text="已取消"); return
            cal2['dropdown'] = {'x': pos[0][0], 'y': pos[0][1]}
            cal2['query'] = {'x': pos[1][0], 'y': pos[1][1]}
            if 'mode' not in cal2: cal2['mode'] = 'absolute'
            s2['calibrate'] = cal2
            with open(sf, 'w', encoding='utf-8') as f:
                json.dump(s2, f, ensure_ascii=False, indent=2)
            lbl_dd.configure(text=f"销售区域文本框: X={pos[0][0]} Y={pos[0][1]}")
            lbl_qq.configure(text=f"查询按钮: X={pos[1][0]} Y={pos[1][1]}")
            status_lbl.configure(text="✅ 校准完成！")

        tk.Button(parent, text="开始校准", command=start_calibrate,
                  font=self.FONT_BOLD, bg=self.C_PRIMARY, fg="#FFFFFF", width=15, height=2).pack(pady=15)
        tk.Label(parent, text="移好鼠标→点记录按钮→重复两次", font=(self.FONT[0], 7), fg=self.C_MUTED).pack()

    def _build_resolution_tab(self, parent, dlg):
        """分辨率预设：选择屏幕分辨率，批量识别自动适配点击坐标"""
        tk.Label(parent, text="屏幕分辨率设置", font=self.FONT_HEADING).pack(pady=(15,2))
        tk.Label(parent, text="选择与您电脑匹配的分辨率，批量识别将使用预设坐标",
                 font=(self.FONT[0], 8), fg=self.C_MUTED).pack()

        current = load_resolution_pref()
        res_var = tk.StringVar(dlg, value=current)

        list_frame = tk.Frame(parent)
        list_frame.pack(fill="both", expand=True, padx=30, pady=15)

        for name, preset in RESOLUTION_PRESETS.items():
            tk.Radiobutton(list_frame, text=f"{name}  —  下拉({preset['dropdown_x']:.0%},{preset['dropdown_y']:.0%})  查询({preset['query_x']:.0%},{preset['query_y']:.0%})",
                           variable=res_var, value=name, font=self.FONT,
                           bg=self.C_SURFACE, fg=self.C_TEXT,
                           selectcolor=self.C_SURFACE, activebackground=self.C_SURFACE,
                           anchor="w").pack(fill="x", pady=3)

        def save_res():
            save_resolution_pref(res_var.get())
            self.status_text.set(f"分辨率已设为 {res_var.get()}")
            messagebox.showinfo("已保存", f"分辨率预设已保存\n批量识别将使用对应坐标", parent=dlg)

        tk.Button(parent, text="保存", command=save_res,
                  font=self.FONT_BOLD, bg=self.C_PRIMARY, fg="#FFFFFF", width=15).pack(pady=15)

    def _build_backend_tab(self, parent, dlg=None):
        """配置拼多多商家后台链接和登录凭据"""
        tk.Label(parent, text="商家后台快捷入口", font=self.FONT_HEADING).pack(pady=(15,2))
        tk.Label(parent, text="设置后可通过主页「🏪 商家后台」按钮一键打开", font=(self.FONT[0], 8), fg=self.C_MUTED).pack()

        config = self._get_backend_config()

        url_frame = tk.Frame(parent)
        url_frame.pack(fill="x", padx=20, pady=(15,5))
        tk.Label(url_frame, text="后台地址:", font=self.FONT, width=10, anchor="e").pack(side="left")
        url_var = tk.StringVar(dlg, value=config.get('url', 'https://mms.pinduoduo.com/'))
        tk.Entry(url_frame, textvariable=url_var, font=self.FONT, width=40).pack(side="left", padx=5)

        acc_frame = tk.Frame(parent)
        acc_frame.pack(fill="x", padx=20, pady=5)
        tk.Label(acc_frame, text="登录账号:", font=self.FONT, width=10, anchor="e").pack(side="left")
        acc_var = tk.StringVar(dlg, value=config.get('account', ''))
        acc_entry = tk.Entry(acc_frame, textvariable=acc_var, font=self.FONT, width=40, fg=self.C_MUTED)
        acc_entry.pack(side="left", padx=5)
        def _ph_entry(entry, placeholder, var):
            def on_focus_in(e):
                if var.get() == placeholder:
                    var.set('')
                    entry.configure(fg=self.C_TEXT)
            def on_focus_out(e):
                if not var.get():
                    var.set(placeholder)
                    entry.configure(fg=self.C_MUTED)
            entry.bind('<FocusIn>', on_focus_in)
            entry.bind('<FocusOut>', on_focus_out)
            if not var.get():
                var.set(placeholder)
        _ph_entry(acc_entry, '输入手机号', acc_var)

        pwd_frame = tk.Frame(parent)
        pwd_frame.pack(fill="x", padx=20, pady=5)
        tk.Label(pwd_frame, text="登录密码:", font=self.FONT, width=10, anchor="e").pack(side="left")
        pwd_var = tk.StringVar(dlg, value=config.get('password', ''))
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
            import json
            settings_file = os.path.join(get_base_dir(), 'settings.json')
            try:
                with open(settings_file, 'r', encoding='utf-8') as f:
                    s = json.load(f)
            except:
                s = {}
            s['backend'] = {
                'url': url_var.get().strip(),
                'account': '' if acc_var.get() in ('输入手机号', '') else acc_var.get().strip(),
                'password': '' if pwd_var.get() == '输入密码' else pwd_var.get()
            }
            try:
                with open(settings_file, 'w', encoding='utf-8') as f:
                    json.dump(s, f, ensure_ascii=False, indent=2)
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

        # 三张提供商卡片
        cards_frame = tk.Frame(parent, bg=self.C_BG)
        cards_frame.pack(fill="both", expand=True, padx=15, pady=10)

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

        def save_all():
            import json
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
                # 刷新下拉列表
                combo = getattr(self, f'_api_combo_{key}', None)
                if combo:
                    combo['values'] = history
            sf = os.path.join(get_base_dir(), 'settings.json')
            try: s = json.load(open(sf, 'r', encoding='utf-8'))
            except: s = {}
            s['api'] = {
                'active_provider': active_var.get(),
                'providers': new_providers,
            }
            json.dump(s, open(sf, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
            self._refresh_model_badge()
            self.status_text.set(f"API 配置已保存 — 当前: {PRESET_PROVIDERS[active_var.get()]['name']}")

        tk.Button(parent, text="保存全部 API 配置", command=save_all,
                  font=self.FONT_BOLD, bg=self.C_PRIMARY, fg="#FFFFFF", width=18).pack(pady=12)
