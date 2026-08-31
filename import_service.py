"""
ImportServiceMixin — 导入服务域（TC-A1.1 抽取）
====================================================

从 gui.py 整体迁移 6 个导入相关方法（零行为变更）：
  _open_import_menu / _dispatch_import / _import_table /
  _import_preview_dialog / _import_done / _import_report_dialog

附带迁移模块级纯函数 resolve_last_mapping（gui.py 测试仍可经
`from import_service import resolve_last_mapping` 重导出访问）。

行为契约：外部不变（抽取 ≠ 重构语义）。worker 线程纪律、失败显式保持。
gui.py class App 挂 ImportServiceMixin 后，self.* 引用经 MRO 解析不变。

迁移详情见 _BUSINESS_DOMAIN_MAP.md。
"""
import os
import tkinter as tk
from tkinter import messagebox, ttk

from logger import log
from utils import get_base_dir

# 守护式导入：增量包缺文件时映射记忆整体降级为不可用，主程序不受影响
# （同 gui.py 模式：import_memory 或为 None）
try:
    import import_memory
except Exception:
    import_memory = None

__all__ = ['ImportServiceMixin', 'resolve_last_mapping']


# ─────────── 模块级纯函数（从 gui.py:670 迁移）───────────

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


# ─────────── ImportServiceMixin ───────────

class ImportServiceMixin:
    """导入服务 mixin（TC-A1.1 从 gui.py 抽取，零行为变更）。

    挂入 class App(SettingsUIMixin, StatsPagesMixin, ImportServiceMixin) 后，
    self.* 引用经 MRO 解析——宿主类提供 _batch_images / _friendly_error /
    _fill_from_ocr / _run_begin / _show_error / _geo / _mk_btn 等方法。
    """

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
        # v1.6.0 TC-Q4 相位 2：开始一个 Run（映射确认后才算——取消无痕）
        self._run_begin('import')
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
