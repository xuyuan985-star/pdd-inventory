"""
PDD 商家后台数据导入 — 纯标准库，零依赖
用法: python pdd_import.py 订单导出.csv 商品列表.csv
"""

import csv
import os
from datetime import datetime


def _read_csv_auto(path: str):
    """自动检测编码读取CSV — UTF-8 → GB18030 → GBK"""
    for enc in ['utf-8', 'utf-8-sig', 'gb18030', 'gbk']:
        try:
            with open(path, 'r', encoding=enc) as f:
                rows = list(csv.reader(f))
                if rows and len(rows[0]) > 0:
                    return rows
        except (UnicodeDecodeError, UnicodeError):
            continue
        except Exception:
            continue
    raise ValueError(f"无法读取 {path}，尝试了 UTF-8/GB18030/GBK 均失败")


def _find_col(headers, *aliases):
    """在表头中查找列索引"""
    for alias in aliases:
        for i, h in enumerate(headers):
            if alias in h.replace(' ', '').lower():
                return i
    return -1


def import_orders(order_csv: str) -> dict:
    """
    解析PDD订单导出 → 每个SKU的当日销量
    返回: {sku: {'name':..., 'sales':..., 'orders':...}}
    """
    rows = _read_csv_auto(order_csv)
    headers = [h.strip() for h in rows[0]]
    
    sku_idx = _find_col(headers, '商家编码', '商品编码', 'sku')
    name_idx = _find_col(headers, '商品名称', '商品名', 'name')
    qty_idx = _find_col(headers, '数量', 'quantity', '销量')
    date_idx = _find_col(headers, '下单时间', '订单时间', '日期')
    
    # Fallback: no SKU → use 商品名称
    if sku_idx < 0 and name_idx >= 0:
        sku_idx = name_idx
    
    if sku_idx < 0 or qty_idx < 0:
        cols = ', '.join(headers[:10])
        raise ValueError(f"订单CSV缺少必要列。可用列: {cols}")
    
    today = datetime.now().strftime('%Y-%m-%d')
    sku_data = {}
    
    for row in rows[1:]:
        if len(row) <= max(sku_idx, qty_idx):
            continue
        sku = row[sku_idx].strip()
        name = row[name_idx].strip() if name_idx >= 0 and name_idx < len(row) else sku
        try:
            qty = int(row[qty_idx])
        except (ValueError, IndexError):
            continue
        
        # 只统计当天订单
        is_today = True
        if date_idx >= 0 and date_idx < len(row):
            d = row[date_idx].strip()[:10]
            if d != today and d:
                is_today = False
        
        if not is_today:
            continue
        
        if sku not in sku_data:
            sku_data[sku] = {'name': name, 'sales': 0}
        sku_data[sku]['sales'] += qty
    
    return sku_data


def import_inventory(inv_csv: str) -> list:
    """
    解析PDD库存导出 → 每个SKU的库存信息
    返回: [{'sku':..., 'name':..., 'stock':...}, ...]
    """
    rows = _read_csv_auto(inv_csv)
    headers = [h.strip() for h in rows[0]]
    
    sku_idx = _find_col(headers, '商家编码', '商品编码', 'sku')
    name_idx = _find_col(headers, '商品名称', '商品名', 'name')
    stock_idx = _find_col(headers, '库存', '当前库存', 'stock', 'quantity')
    
    if sku_idx < 0 and name_idx >= 0:
        sku_idx = name_idx
    
    if sku_idx < 0 or stock_idx < 0:
        cols = ', '.join(headers[:10])
        raise ValueError(f"库存CSV缺少必要列。可用列: {cols}")
    
    items = []
    for row in rows[1:]:
        if len(row) <= max(sku_idx, stock_idx):
            continue
        sku = row[sku_idx].strip()
        name = row[name_idx].strip() if name_idx >= 0 and name_idx < len(row) else sku
        try:
            stock = int(row[stock_idx])
        except (ValueError, IndexError):
            stock = 0
        
        items.append({'sku': sku, 'name': name, 'stock': stock})
    
    return items


# ── 命令行 ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    print("📦 PDD 商家数据导入 (pandas-free)")
    if len(sys.argv) >= 3:
        from main import run_pipeline
        run_pipeline(sys.argv[1], sys.argv[2])
    else:
        print("用法: python pdd_import.py 订单.csv 库存.csv")

