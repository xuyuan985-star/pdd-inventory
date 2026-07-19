"""
PDD EZ — Excel 导出模块
统一 GUI 和 CLI 两条路径的导出逻辑。
"""
import os
from datetime import datetime
from utils import get_base_dir


def _create_styles():
    """返回统一的 Excel 样式对象"""
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    return {
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


def export_cache_to_xlsx(cache: dict, export_dir: str = None) -> str:
    """
    GUI 路径：按地区分组的 cache → 追加 Sheet 到 PDD补货记录.xlsx
    返回文件路径。
    """
    import openpyxl
    from openpyxl.utils import get_column_letter

    if not export_dir:
        export_dir = _get_default_export_dir()
    path = os.path.join(export_dir, 'PDD补货记录.xlsx')

    ts_date = datetime.now().strftime('%m.%d')
    styles = _create_styles()

    if os.path.exists(path):
        wb = openpyxl.load_workbook(path)
        ws = wb.create_sheet(ts_date)
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = ts_date

    headers = ['地区', '商品名称', f'库存({ts_date})', '当日销量', '可售卖天数', '补货状态', '建议补货量']
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
            vals = [region, p['name'], p['stock'], p['daily'],
                    p.get('ratio', p.get('days_left', '')), p['status'], p['qty']]
            for ci, v in enumerate(vals, 1):
                c = ws.cell(row=row, column=ci, value=v)
                c.font = styles['cell_font']
                c.border = styles['thin']
                c.alignment = styles['center']
                if p.get('color') in styles['fills']:
                    c.fill = styles['fills'][p['color']]
            row += 1

    widths = [10, 20, 10, 10, 10, 12, 10]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    wb.save(path)
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

    path = os.path.join(export_dir, 'PDD补货记录.xlsx')
    ts = datetime.now().strftime('%m-%d %H.%M')

    styles = _create_styles()

    if os.path.exists(path):
        wb = openpyxl.load_workbook(path)
        ws = wb.create_sheet(ts)
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = ts

    ts_date = ts.split()[0].replace('-', '.')
    headers = ['商品名称', f'库存({ts_date})', '当日销量', '可售卖天数', '补货状态', '建议补货量']
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = styles['header_font']
        c.fill = styles['header_fill']
        c.alignment = styles['center']
        c.border = styles['thin']

    for ri, p in enumerate(plans, 2):
        vals = [p['name'], p['stock'], p['daily'],
                p.get('ratio', p.get('days_left', '')), p['status'], p['qty']]
        for ci, v in enumerate(vals, 1):
            c = ws.cell(row=ri, column=ci, value=v)
            c.font = styles['cell_font']
            c.border = styles['thin']
            c.alignment = styles['center']
            if p.get('color') in styles['fills']:
                c.fill = styles['fills'][p['color']]

    widths = [22, 12, 10, 10, 12, 12]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    wb.save(path)
    return path


def export_plans_to_csv(plans: list, schedule: list, export_dir: str = None) -> str:
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
            w.writerow([p['name'], p.get('sku', p['name']), p['stock'], p['daily'],
                        p.get('ratio', p.get('days_left', '')), p['status'], p['qty'],
                        p['order_date'], p['arrive_date']])
    return path
