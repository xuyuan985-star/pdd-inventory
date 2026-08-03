"""
PDD EZ — Excel 导出模块
统一 GUI 和 CLI 两条路径的导出逻辑。
"""
import os
from datetime import datetime
from utils import get_base_dir


_STYLES_CACHE = None


def _create_styles():
    """返回统一的 Excel 样式对象（模块级缓存，避免高频导出重复创建）"""
    global _STYLES_CACHE
    if _STYLES_CACHE is not None:
        return _STYLES_CACHE
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    _STYLES_CACHE = {
        'fills': {
            'red': PatternFill('solid', fgColor='FFC7CE'),
            'yellow': PatternFill('solid', fgColor='FFEB9C'),
            'green': PatternFill('solid', fgColor='C6EFCE'),
        },
        'header_fill': PatternFill('solid', fgColor='4472C4'),
        'header_font': Font(name='微软雅黑', size=9, bold=True, color='FFFFFF'),
        'cell_font': Font(name='微软雅黑', size=9),
        'thin': Border(left=Side('thin'), right=Side('thin'), top=Side('thin'), bottom=Side('thin')),
        'center': Alignment(horizontal='center', vertical='center'),
    }
    return _STYLES_CACHE


def _get_default_export_dir() -> str:
    """默认导出目录：settings → 桌面"""
    import json
    try:
        sf = os.path.join(get_base_dir(), 'settings.json')
        if os.path.exists(sf):
            with open(sf, 'r', encoding='utf-8') as f:
                s = json.load(f)
                return s.get('export_path', os.path.join(os.path.expanduser('~'), 'Desktop'))
    except Exception:
        pass
    return os.path.join(os.path.expanduser('~'), 'Desktop')


def _unique_sheet_name(wb, base: str) -> str:
    """Sheet 重名保护：base 已被占用时追加 _2、_3…"""
    existing = {ws.title for ws in wb.worksheets}
    if base not in existing:
        return base
    i = 2
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"


def _sanitize_cell(v):
    """Excel/CSV 公式注入防护：以 = + - @ 开头的字符串加 ' 前缀，
    避免用户可控文本（商品名）被 Excel 当作公式执行（=WEBSERVICE 外泄等）"""
    if isinstance(v, str):
        s = v.strip()
        if s.startswith(('=', '+', '-', '@')):
            return "'" + s
    return v


def _sanitize_csv_cell(v):
    """
    CSV 路径专用消毒：在 _sanitize_cell 基础上，清理内嵌换行/回车/制表符。
    csv.writer 已处理逗号/引号的标准转义，但内嵌换行会让 Excel 打开时
    单元格被拆成多行（视觉混淆/潜在注入载体），统一替换为空格。
    """
    if isinstance(v, str):
        v = _sanitize_cell(v)
        if '\n' in v or '\r' in v or '\t' in v:
            v = v.replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
    return v


def export_cache_to_xlsx(cache: dict, export_dir: str = None) -> str:
    """
    GUI 路径：按地区分组的 cache → 追加 Sheet 到 PDD补货记录.xlsx
    返回文件路径。
    """
    import openpyxl
    from openpyxl.utils import get_column_letter

    if not export_dir:
        export_dir = _get_default_export_dir()
    os.makedirs(export_dir, exist_ok=True)  # 目录不存在时创建，避免保存崩溃
    path = os.path.join(export_dir, 'PDD补货记录.xlsx')

    ts_date = datetime.now().strftime('%m.%d_%H%M%S')
    styles = _create_styles()

    if os.path.exists(path):
        wb = openpyxl.load_workbook(path)
        ts_date = _unique_sheet_name(wb, ts_date)
        ws = wb.create_sheet(ts_date)
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = ts_date

    # 动态列（v1.3）：取第一个地区 plans 的勾选列；未配置回退默认商品字段
    sel_cols = []
    for _reg, _data in cache.items():
        _pl = (_data or {}).get('plans') or []
        if _pl and _pl[0].get('_sel_cols'):
            sel_cols = list(_pl[0]['_sel_cols'])
            break
    if not sel_cols:
        sel_cols = ['商品名称', '仓库总库存', '仓库预估总销售数']
    headers = ['地区', '仓库'] + list(sel_cols) + ['可售卖天数', '补货状态', '建议补货量']
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = styles['header_font']
        c.fill = styles['header_fill']
        c.alignment = styles['center']
        c.border = styles['thin']

    row = 2
    for region, data in sorted(cache.items()):
        plans = data.get('plans', [])
        if not plans:
            continue
        for p in plans:
            raw = p.get('_raw') or {}
            vals = [_sanitize_cell(region), _sanitize_cell(p.get('warehouse', ''))]
            for col in sel_cols:
                v = raw.get(col)
                if v is None or v == '':
                    v = p.get(col, '')
                vals.append(_sanitize_cell(v))
            vals += [p.get('ratio', p.get('days_left', '')), _sanitize_cell(p['status']), p['qty']]
            for ci, v in enumerate(vals, 1):
                c = ws.cell(row=row, column=ci, value=v)
                c.font = styles['cell_font']
                c.border = styles['thin']
                c.alignment = styles['center']
                if p.get('color') in styles['fills']:
                    c.fill = styles['fills'][p['color']]
            row += 1

    widths = [10, 12] + [20 if '名称' in c or '商品' in c else 12 for c in sel_cols] + [10, 12, 10]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    try:
        wb.save(path)
    except OSError as e:
        # 覆盖磁盘满、路径非法、文件被锁定等常见 IO 错误
        raise OSError(f"无法保存 Excel 文件（{type(e).__name__}）：{e}\n"
                      f"请检查磁盘空间或关闭已打开的 PDD补货记录.xlsx 后重试。\n文件路径: {path}")
    return path


def export_plans_to_xlsx(plans: list, export_dir: str = None) -> str:
    """
    CLI 路径：plans 列表 → 追加 Sheet 到 PDD补货记录.xlsx
    返回文件路径。
    """
    import openpyxl
    from openpyxl.utils import get_column_letter

    if not export_dir:
        export_dir = os.path.join(get_base_dir(), 'output')
    os.makedirs(export_dir, exist_ok=True)  # 目录不存在时创建，避免保存崩溃

    path = os.path.join(export_dir, 'PDD补货记录.xlsx')
    ts = datetime.now().strftime('%m-%d %H.%M.%S')

    styles = _create_styles()

    if os.path.exists(path):
        wb = openpyxl.load_workbook(path)
        # 同秒重复导出时追加序号，避免 Sheet 重名崩溃
        ts = _unique_sheet_name(wb, ts)
        ws = wb.create_sheet(ts)
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = ts

    ts_date = ts.split()[0].replace('-', '.')
    # 动态列兼容：CLI plans 若带 _sel_cols（v1.3 GUI 缓存导出复用）则走动态列，否则固定列
    sel_cols = []
    if plans and plans[0].get('_sel_cols'):
        sel_cols = list(plans[0]['_sel_cols'])
    if sel_cols:
        headers = ['仓库'] + list(sel_cols) + ['可售卖天数', '补货状态', '建议补货量']
    else:
        headers = ['仓库', '商品名称', f'库存({ts_date})', '当日销量', '可售卖天数', '补货状态', '建议补货量']
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = styles['header_font']
        c.fill = styles['header_fill']
        c.alignment = styles['center']
        c.border = styles['thin']

    for ri, p in enumerate(plans, 2):
        if sel_cols:
            raw = p.get('_raw') or {}
            vals = [_sanitize_cell(p.get('warehouse', ''))]
            for col in sel_cols:
                v = raw.get(col)
                if v is None or v == '':
                    v = p.get(col, '')
                vals.append(_sanitize_cell(v))
            vals += [p.get('ratio', p.get('days_left', '')), _sanitize_cell(p['status']), p['qty']]
            widths = [12] + [20 if '名称' in c or '商品' in c else 12 for c in sel_cols] + [10, 12, 12]
        else:
            vals = [_sanitize_cell(p.get('warehouse', '')), _sanitize_cell(p['name']), p['stock'], p['daily'],
                    p.get('ratio', p.get('days_left', '')), _sanitize_cell(p['status']), p['qty']]
            widths = [12, 22, 12, 10, 10, 12, 12]
        for ci, v in enumerate(vals, 1):
            c = ws.cell(row=ri, column=ci, value=v)
            c.font = styles['cell_font']
            c.border = styles['thin']
            c.alignment = styles['center']
            if p.get('color') in styles['fills']:
                c.fill = styles['fills'][p['color']]

    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    try:
        wb.save(path)
    except OSError as e:
        # 覆盖磁盘满、路径非法、文件被锁定等常见 IO 错误
        raise OSError(f"无法保存 Excel 文件（{type(e).__name__}）：{e}\n"
                      f"请检查磁盘空间或关闭已打开的 PDD补货记录.xlsx 后重试。\n文件路径: {path}")
    return path


def export_plans_to_csv(plans: list, export_dir: str = None) -> str:
    """CSV 降级导出（无 openpyxl 时使用）"""
    import csv
    if not export_dir:
        export_dir = os.path.join(get_base_dir(), 'output')
    ts = datetime.now().strftime('%Y%m%d_%H%M')
    path = os.path.join(export_dir, f'补货计划_{ts}.csv')
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['商品', '规格', '库存', '销量', '库存÷销量', '状态', '补货量', '下单日', '到货日'])
        for p in plans:
            # GUI 路径 plans 无 order_date/arrive_date，用 .get 防御；CSV 专用消毒防公式注入+换行拆行
            w.writerow([_sanitize_csv_cell(p['name']), _sanitize_csv_cell(p.get('sku', p['name'])), p['stock'], p['daily'],
                        p.get('ratio', p.get('days_left', '')), _sanitize_csv_cell(p['status']), p['qty'],
                        p.get('order_date', '-'), p.get('arrive_date', '-')])
    return path
