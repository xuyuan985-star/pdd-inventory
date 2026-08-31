# Business Domain Map — ImportService 域（TC-A1.1 前置工件）

> 生成时间：TC-A1.1 抽取前（attempt 2）
> 基线：gui.py 7282 行（t20/t25/t29/t32 后）

## 1. 方法清单（当前行号实测）

| # | 方法 | 行号 | 行数 | 来源 |
|---|------|------|------|------|
| 1 | `_open_import_menu` | 5731–5774 | 44 | gui.py |
| 2 | `_dispatch_import` | 5776–5794 | 19 | gui.py |
| 3 | `_import_table` | 6818–6875 | 58 | gui.py |
| 4 | `_import_preview_dialog` | 6877–6983 | 107 | gui.py |
| 5 | `_import_done` | 6985–7005 | 21 | gui.py |
| 6 | `_import_report_dialog` | 7007–7036 | 30 | gui.py |
| — | `resolve_last_mapping`（模块级函数） | 670–698 | 29 | gui.py |

合计：6 方法 279 行 + 1 函数 29 行 = 308 行迁移。

## 2. 各方法依赖 self 字段清单

### 2.1 `_open_import_menu(self)`
- **self 字段**：`status_text`, `win`, `_import_table()`, `_dispatch_import()`
- **模块级引用**：无（全 late import）
- **Late import**：`home_actions.IMPORT_MENU_ITEMS`, `home_actions.menu_labels`, `tkinter.messagebox`
- **Tk 控件**：`tk.Menu`

### 2.2 `_dispatch_import(self, key)`
- **self 字段**：`_batch_images()`, `_import_table()`
- **模块级引用**：无
- **Late import**：`utils._sanitize_for_log`
- **行为**：key='pick_images' → `self._batch_images()`；key='import_table' → `self._import_table()`；未知 key → 记日志不静默跳转（§4）

### 2.3 `_import_table(self)`
- **self 字段**：`_batch_running`, `_img_batch_running`, `win`, `status_text`, `_friendly_error()`, `_import_preview_dialog()`, `_run_begin()`, `_task_queue`, `_import_done()`
- **模块级引用**：无
- **Late import**：`table_import`, `tkinter.filedialog`, `ocr_review.categorize_error`
- **RunContext 接线**：`self._run_begin('import')`（t32 相位 2 加，行 6858）
- **Worker 线程纪律**：task 函数内不碰 Tk，结果经 `win.after(0, ...)` 回主线程
- **任务签名**：`def task(_progress=None):` — 兼容 TaskQueue progress 回调（v1.5.9.4-hotfix）

### 2.4 `_import_preview_dialog(self, headers, path)`
- **self 字段**：`win`, `_geo()`, `C_BG`, `C_TEXT`, `C_MUTED`, `FONT`, `_mk_btn()`, `region_var`
- **模块级引用**：`import_memory`（guarded, 可为 None）, `resolve_last_mapping`（模块级函数）, `log`, `get_base_dir`
- **Late import**：`table_import`, `os`（模块级已 import）
- **返回**：`(mapping|None, has_region)`

### 2.5 `_import_done(self, items, issues, has_region)`
- **self 字段**：`_import_report_dialog()`, `status_text`, `_fill_from_ocr()`, `region_var`, `_show_error()`
- **模块级引用**：无
- **Late import**：`export_xlsx._sanitize_cell`
- **行为**：清洗 name/region/warehouse → `_fill_from_ocr(items, source='import')` 收口

### 2.6 `_import_report_dialog(self, issues)`
- **self 字段**：`win`, `_geo()`, `C_BG`, `C_TEXT`, `FONT`, `_mk_btn()`
- **模块级引用**：无
- **Tk 控件**：`tk.Toplevel`, `ttk.Treeview`, `ttk.Scrollbar`

### 2.7 `resolve_last_mapping(headers, mapping)`（模块级函数）
- **模块级引用**：无（`from ocr import normalize_col_name` late import）
- **调用方**：`_import_preview_dialog`（行 6898）, `test_workflow_memory.py`（12 处，`gui.resolve_last_mapping`）
- **迁移目标**：移入 `import_service.py`；gui.py 加 `from import_service import resolve_last_mapping` 保持 `gui.resolve_last_mapping` 可用

## 3. 调用方清单（grep 引用点）

### gui.py 内部引用
| 引用行 | 代码 | 上下文 |
|--------|------|--------|
| 1679 | `# ...见 _open_import_menu` | 注释 |
| 1680 | `self._open_import_menu` | 导入按钮 command |
| 5743 | `self._import_table()` | _open_import_menu 菜单初始化失败回退 |
| 5752 | `self._dispatch_import(k)` | _open_import_menu 菜单项分派 |
| 5765 | `self._import_table()` | _open_import_menu TclError 回退 |
| 5787 | `self._import_table()` | _dispatch_import key='import_table' |
| 6854 | `self._import_preview_dialog(headers, path)` | _import_table 调用 |
| 6867 | `self._import_done(i, s, has_region)` | _import_table worker 完成回调 |
| 6989 | `self._import_report_dialog(issues)` | _import_done 调用 |

### 测试引用
| 文件 | 引用方法 | 断言类型 |
|------|----------|----------|
| test_home_actions.py | `_open_import_menu`, `_dispatch_import`, `_import_table` | `hasattr(gui.App, ...)` |
| test_queue_signature.py | `_import_table` | `inspect.getsource(gui.App._import_table)` — task 签名 |
| test_import_menu_fix.py | `_import_table`, `_dispatch_import`, `_open_import_menu` | `inspect.getsource(gui.App._method)` — 源码级断言 |
| test_smoke.py:2666 | class App 定义 | `assertIn('class App(SettingsUIMixin, StatsPagesMixin)')` — **需更新** |
| test_smoke.py:3665 | `_open_import_menu` | `assertIn("self._open_import_menu", src)` — _build_ui 内引用 |
| test_workflow_memory.py | `gui.resolve_last_mapping` | 12 处调用 — 需保持 `gui.resolve_last_mapping` 可用 |

## 4. 共享 self._import_* 字段

grep `self._import_` 结果：**6 处全是方法调用，无字段赋值**。
即：无 `self._import_foo = ...` 形式的实例字段需迁移。方法间调用全经 `self.` MRO 解析。

## 5. 迁移策略

1. `resolve_last_mapping` 移入 `import_service.py`（纯函数，依赖 `ocr.normalize_col_name`）
2. `import_memory` 在 `import_service.py` 加 guarded import（同 gui.py 模式）
3. `log` / `get_base_dir` 在 `import_service.py` 模块级 import
4. 6 方法原样搬入 `ImportServiceMixin`（零行为变更）
5. gui.py 加 `from import_service import ImportServiceMixin, resolve_last_mapping`
6. gui.py `class App(SettingsUIMixin, StatsPagesMixin)` → `class App(SettingsUIMixin, StatsPagesMixin, ImportServiceMixin)`
7. gui.py 删除 6 方法 + `resolve_last_mapping` 函数
8. test_smoke.py:2666 更新断言

## 6. 不迁移项（明确边界）

- `_batch_images()` — 不属于 ImportService 域（图片批量识别，留在 gui.py）
- `_friendly_error()` — 通用错误归类（OCR/导入共用，留在 gui.py）
- `_fill_from_ocr()` — 通用结果收口（OCR/导入/手动共用，留在 gui.py）
- `_run_begin()` — RunContext 入口（t32 全局，留在 gui.py）
- `_show_error()` — 通用错误弹窗（留在 gui.py）
- `_geo()` / `_mk_btn()` — 通用 UI 工具（留在 gui.py）
