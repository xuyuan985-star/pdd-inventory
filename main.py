"""
PDD 库存补货排期系统 — 纯标准库
公式：补货时间 = 库存 ÷ 当天销量 - 运输时间
"""

import os, sys
from datetime import datetime, timedelta

from utils import get_base_dir


def calculate_replenishment(inventory: list, sales: dict,
                            shipping_days: int = 3,
                            min_order: int = 100) -> list:
    today = datetime.now()
    lead_time = shipping_days + 1  # 补货时间 = 运输天数 + 1
    plans = []
    for item in inventory:
        sku = item['sku']
        name = item['name']
        stock = item['stock']
        sd = sales.get(sku, {})
        daily = sd.get('sales', 0) if sd else 0
        daily = max(daily, 1)
        ratio = stock / daily
        reorder_days = ratio - lead_time
        if reorder_days <= 0:
            status = '现在下单'; color = 'red'
            qty = max(daily * 8, min_order)
            qty = ((qty + min_order - 1) // min_order) * min_order
        elif reorder_days <= 2:
            status = f'{reorder_days:.0f}天后下单'; color = 'yellow'
            qty = max(daily * 8, min_order)
            qty = ((qty + min_order - 1) // min_order) * min_order
        else:
            status = f'{reorder_days:.0f}天后下单'; color = 'green'
            qty = 0
        order_date = today if qty > 0 else None
        arrive_date = order_date + timedelta(days=shipping_days) if order_date else None
        plans.append(dict(sku=sku, name=name, stock=stock, daily=daily,
            ratio=round(ratio,1), days_left=round(ratio,1), status=status,
            color=color, qty=qty,
            order_date=order_date.strftime('%Y-%m-%d') if order_date else '-',
            arrive_date=arrive_date.strftime('%Y-%m-%d') if arrive_date else '-'))
    plans.sort(key=lambda p: {'red':0,'yellow':1,'green':2}.get(p['color'],99))
    return plans


def generate_schedule(plans: list) -> list:
    schedule = []
    for p in plans:
        if p['qty'] > 0:
            schedule.append(dict(date=p['order_date'], type='下单', sku=p['sku'], name=p['name'], qty=p['qty']))
            schedule.append(dict(date=p['arrive_date'], type='到货', sku=p['sku'], name=p['name'], qty=p['qty']))
    schedule.sort(key=lambda x: x['date'])
    return schedule


def export_results(plans: list, output_dir: str = None) -> str:
    """导出到 PDD补货记录.xlsx，每次追加新sheet（时间戳命名）"""
    from export_xlsx import export_plans_to_xlsx
    return export_plans_to_xlsx(plans, output_dir)


def run_pipeline(order_csv: str, inv_csv: str, output_dir: str = None):
    from pdd_import import import_orders, import_inventory
    sales = import_orders(order_csv)
    inventory = import_inventory(inv_csv)
    plans = calculate_replenishment(inventory, sales)
    schedule = generate_schedule(plans)
    if not output_dir:
        output_dir = os.path.join(get_base_dir(), 'output')
    path = export_results(plans, output_dir)
    print(f"\n[DONE] {path}")
    return path


if __name__ == '__main__':
    import sys
    if len(sys.argv) >= 3:
        run_pipeline(sys.argv[1], sys.argv[2])
    else:
        print("PDD库存补货排期系统")
        print("用法: python main.py 订单.csv 库存.csv")
