"""
ReplenishmentService 纯函数层（TC-A1.2 相位 1）
====================================================

把 gui.py ``_calc_from_items``（:3411-3735）的计算语义抽为无 ``self`` 纯函数
``build_plans``，供相位 2 薄壳接线后 gui 直接调用。本文件**只新建**，不修改任何
既有文件（相位 1 红线）。

等价契约
--------
``build_plans(items, cfg, history_lookup, shipping_fn)`` 的输出 plans 列表与
``gui.App._calc_from_items(items)`` 内部 ``plans`` 列表（排序后、存入 self.plans
之前的那份）**字段集合与值语义等价**：

- 经典公式 = ``utils.calc_replenishment_classic``（零改动红线，import 不复制）
- 加权模式 = ``utils.calc_replenishment_weighted``（异常回退经典 + 'classic(error)')
- 高级模式 = ``algorithm_ui.dispatch_plan``（异常回退经典 + 'classic(error)')
- 预警列   = ``algorithm_ui.warning_display``
- 预测     = ``algorithm_ui.forecast_next_period``（默认 as_dict=False 标量契约）
- 安全闸   = ``ocr_review.apply_safety_gate``（RED/NO_DATA/YELLOW 算后覆盖）

本函数**不做**的 GUI 副作用（相位 2 由 gui 薄壳接管）：
  - ``_render_tree(plans)``            — 渲染到 Treeview
  - ``self.plans = plans``             — 状态赋值
  - ``self.cache[region] = {...}``     — 缓存写入
  - ``save_recommendation_cache(...)``  — 安全库存推荐落盘
  - ``self.status_text.set(...)``       — 状态栏文本
  - ``self._last_trust_summary``        — 可信度汇总
  - ``self.export_btn.config(...)``     — 导出按钮状态
  - ``self._update_tabs()``             — 地区标签刷新

调用方清单（相位 2 接线后）：
  - ``gui.App._calc_from_items`` → 替换为 ``plans = build_plans(...); self._post_calc(plans)``
  - 测试 ``test_replenishment_service_extract.py``
  - 未来 ``test_smoke`` 回归（等价性间接保证）

参数
----
items : list[dict]
    OCR 结果列表，每项含 ``name/stock/sales/sku_id/warehouse/region/_raw`` 等字段。
    字段缺失时按默认值兜底（name='', stock=0, sales=0, sku_id='', warehouse=''）。

cfg : dict
    配置参数（纯 dict，不依赖 settings.json IO）：
    - ``'region'``              : str  — 默认地区（item 无 region 时的回退）
    - ``'replenishment_offset'``: int  — 经典公式偏移量（默认 1）
    - ``'model'``               : str  — 补货模型 'classic'|'weighted'|'advanced'（默认 'classic'）
    - ``'safety_days'``         : int  — 加权/高级安全库存天数（默认 2）
    - ``'in_transit_qty'``      : int  — 在途数量（默认 0）
    - ``'advanced'``            : dict — 高级模型子配置（可选）
    - ``'col_cfg'``             : dict — OCR 列配置 {'selected': [...], 'mapping': {...}}（可选）

history_lookup : callable
    签名 ``(sku_id, region, days, name=None) -> list[dict]``；
    与 ``history_db.query_sku_history`` 兼容。返回 [] 或抛异常均不中断计算。

shipping_fn : callable
    签名 ``(region, product_name) -> int``；
    与 ``gui.App._get_shipping`` 同语义。未设置时返回 3。

返回
----
list[dict] — plans 列表，按优先级排序（red < yellow < green），每项字段对齐
``_calc_from_items`` 产出（见模块级 FIELD_ALIGNMENT 表）。
"""
from __future__ import annotations

from utils import (
    calc_replenishment_classic,
    calc_replenishment_weighted,
    MODEL_CLASSIC,
    MODEL_WEIGHTED,
    MODEL_ADVANCED,
)
from algorithm_ui import (
    dispatch_plan,
    warning_display,
    forecast_next_period,
)
from ocr_review import apply_safety_gate

__all__ = ['build_plans']


# ─────────── 内部辅助 ───────────

def _to_int(v, default=0):
    """与 utils._to_int_safe / gui._calc_from_items._to_int 同语义。"""
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


# ─────────── 默认 OCR 列配置 ───────────

_DEFAULT_SEL_COLS = ['商品信息', '仓库总库存', '仓库预估总销售数']


def _resolve_col_cfg(cfg: dict) -> tuple:
    """从 cfg 解析 OCR 列配置（selected + reverse mapping）。

    与 _calc_from_items :3416-3430 同语义：col_cfg 缺失/selected 空 → 默认三列。
    """
    col_cfg = cfg.get('col_cfg') if isinstance(cfg, dict) else None
    if not isinstance(col_cfg, dict):
        col_cfg = {}
    sel_cols = [c for c in (col_cfg.get('selected') or []) if c]
    if not sel_cols:
        sel_cols = list(_DEFAULT_SEL_COLS)
    try:
        sel_cols_map = {v: k for k, v in
                        ((col_cfg.get('mapping') or {}).items()) if v}
    except Exception:
        sel_cols_map = {}
    return sel_cols, sel_cols_map


# ─────────── 字段对齐表 ───────────

FIELD_ALIGNMENT = {
    # 字段名: 来源说明
    'name':            'item["name"]',
    'stock':           '_to_int(item["stock"])',
    'daily':           'calc 函数返回的 daily（classic=原始sales; weighted=加权日销）',
    'ratio':           'round(ratio, 1) — 可售卖天数',
    'days_left':       'round(ratio, 1) — 同 ratio（gui 下游用 days_left 或 ratio 均可）',
    'status':          'calc 函数返回的 status（安全闸可能加前缀）',
    'color':           "'red'|'yellow'|'green'|'gray'",
    'qty':             'calc 函数返回的 qty（安全闸 RED→0）',
    '_row_idx':        '原始 items 索引（排序后仍可回写正确行）',
    'warehouse':       "item.get('warehouse', '')",
    'sku_id':          "item.get('sku_id', '') or ''",
    '_raw':            "item.get('_raw') or {}",
    '_sel_cols':       'cfg col_cfg selected 列表',
    '_sel_cols_map':   'cfg col_cfg mapping 的反查表',
    'model':           "模型标注 'classic'|'weighted'|'classic(no_history)'|'classic(error)'|'advanced'",
    'warning':         "algorithm_ui.warning_display(plan, item)",
    'season_factor':   '1.0（非 advanced）/ dispatch_plan 返回值',
    'promo_multiplier': '1.0（非 advanced）/ dispatch_plan 返回值',
    'effective_daily': 'daily（非 advanced）/ dispatch_plan 返回值',
    'slow_moving':     'False（非 advanced）/ dispatch_plan 返回值',
    'oversell_risk':   'False（非 advanced）/ dispatch_plan 返回值',
    'oversell_level':  'None（非 advanced）/ dispatch_plan 返回值',
    'forecast':        'algorithm_ui.forecast_next_period(history_rows) | None',
    'trust':           "ocr_review.apply_safety_gate 设置 'GREEN'|'YELLOW'|'RED'",
    'signal':          "ocr_review.apply_safety_gate 设置 'ok'|'no_history'|'fallback_error'",
}


def build_plans(items, cfg, history_lookup, shipping_fn):
    """纯函数：从 OCR items 构建补货计划列表（与 gui._calc_from_items 计算语义等价）。

    详见模块级 docstring 的等价契约与参数说明。

    Returns:
        list[dict] — plans 列表，按 red < yellow < green 优先级排序。
    """
    # ── 参数解析 ──
    if not isinstance(cfg, dict):
        cfg = {}
    region = cfg.get('region', '')
    offset = int(cfg.get('replenishment_offset', 1) or 1)
    model = str(cfg.get('model') or MODEL_CLASSIC).strip().lower()
    if model not in (MODEL_CLASSIC, MODEL_WEIGHTED, MODEL_ADVANCED):
        model = MODEL_CLASSIC
    safety_days = int(cfg.get('safety_days', 2) or 0)
    in_transit_qty = int(cfg.get('in_transit_qty', 0) or 0)

    # OCR 列配置
    sel_cols, sel_cols_map = _resolve_col_cfg(cfg)

    # 高级模型子配置
    rep_cfg = {
        'model': model,
        'safety_days': safety_days,
        'in_transit_qty': in_transit_qty,
    }
    if 'advanced' in cfg and isinstance(cfg['advanced'], dict):
        rep_cfg['advanced'] = cfg['advanced']

    # history_lookup 兜底
    if history_lookup is None:
        def history_lookup(*a, **kw):
            return []

    # shipping_fn 兜底
    if shipping_fn is None:
        def shipping_fn(*a, **kw):
            return 3

    if not isinstance(items, (list, tuple)):
        return []

    plans = []

    for item in items:
        if not isinstance(item, dict):
            item = {}
        name = item.get('name', '')
        stock = _to_int(item.get('stock', 0))
        daily = max(_to_int(item.get('sales', 0)), 0)

        # 每商品用自己的地区查时效
        _it_region = item.get('region') or region

        # 运输时效
        try:
            shipping = int(shipping_fn(_it_region, name))
        except Exception:
            shipping = 3

        _adv_plan = None

        # ── 模型分发 ──
        if model == MODEL_WEIGHTED:
            # P3-A：加权模式——走 utils.calc_replenishment_weighted
            try:
                _w = calc_replenishment_weighted(
                    item, _it_region, shipping, safety_days, in_transit_qty,
                    history_lookup,
                )
                status = _w['status']
                color = _w['color']
                qty = _w['qty']
                ratio = _w['ratio']
                daily = _w.get('daily', daily)
                _model_tag = _w.get('model', 'weighted')
            except Exception:
                # 加权异常 → 经典公式兜底 + 'classic(error)'
                _model_tag = 'classic(error)'
                _fb = calc_replenishment_classic(item, _it_region, shipping, offset)
                status = _fb['status']
                color = _fb['color']
                qty = _fb['qty']
                ratio = _fb['ratio']
                daily = _fb.get('daily', daily)
        elif model == MODEL_ADVANCED:
            # 高级模式：走 algorithm_ui.dispatch_plan
            try:
                _adv_plan = dispatch_plan(
                    item, _it_region, shipping,
                    rep_cfg, history_lookup,
                )
                status = _adv_plan['status']
                color = _adv_plan['color']
                qty = _adv_plan['qty']
                ratio = _adv_plan['ratio']
                daily = _adv_plan.get('daily', daily)
                _model_tag = _adv_plan.get('model', 'advanced')
            except Exception:
                # 双保险：dispatch_plan 已自带兜底，这里是终极兜底
                _model_tag = 'classic(error)'
                _fb = calc_replenishment_classic(item, _it_region, shipping, offset)
                status = _fb['status']
                color = _fb['color']
                qty = _fb['qty']
                ratio = _fb['ratio']
                daily = _fb.get('daily', daily)
        else:
            # 经典模式（用户裁定：一行公式都不许改）
            _model_tag = MODEL_CLASSIC
            _fb = calc_replenishment_classic(item, _it_region, shipping, offset)
            status = _fb['status']
            color = _fb['color']
            qty = _fb['qty']
            ratio = _fb['ratio']
            daily = _fb.get('daily', daily)

        # ── 构建 plan dict（字段对齐 _calc_from_items :3605-3628）──
        plans.append({
            'name': name, 'stock': stock,
            'daily': daily, 'ratio': round(ratio, 1),
            'days_left': round(ratio, 1),
            'status': status, 'color': color, 'qty': qty,
            '_row_idx': len(plans),  # 原始 rows 索引
            'warehouse': item.get('warehouse', ''),
            'sku_id': item.get('sku_id', '') or '',
            '_raw': item.get('_raw') or {},
            '_sel_cols': sel_cols,
            '_sel_cols_map': sel_cols_map,
            'model': _model_tag,
            'warning': '',
        })

        # ── 高级模式附加字段（:3629-3644）──
        _adv = _adv_plan if isinstance(_adv_plan, dict) else {}
        plans[-1]['season_factor'] = _adv.get('season_factor', 1.0) \
            if model == MODEL_ADVANCED else 1.0
        plans[-1]['promo_multiplier'] = _adv.get('promo_multiplier', 1.0) \
            if model == MODEL_ADVANCED else 1.0
        plans[-1]['effective_daily'] = _adv.get('effective_daily', daily) \
            if model == MODEL_ADVANCED else daily
        plans[-1]['slow_moving'] = _adv.get('slow_moving', False) \
            if model == MODEL_ADVANCED else False
        plans[-1]['oversell_risk'] = _adv.get('oversell_risk', False) \
            if model == MODEL_ADVANCED else False
        plans[-1]['oversell_level'] = _adv.get('oversell_level', None) \
            if model == MODEL_ADVANCED else None

        # ── 预警列（:3645-3650）──
        try:
            plans[-1]['warning'] = warning_display(plans[-1], item)
        except Exception:
            plans[-1]['warning'] = ''

        # ── R2 预测（:3651-3668）──
        _fc = None
        try:
            _hrows = history_lookup(item.get('sku_id', '') or '',
                                    _it_region, 30, name=name)
            if _hrows:
                _fc = forecast_next_period(_hrows)
        except Exception:
            _fc = None  # 预测失败不阻塞计算主链
        plans[-1]['forecast'] = _fc

        # ── TC-Q2 安全闸（:3670-3680）──
        try:
            apply_safety_gate(plans[-1], item)
        except Exception:
            plans[-1].setdefault('trust', 'GREEN')
            plans[-1].setdefault('signal', 'ok')

    # ── 排序（:3682-3684）──
    priority = {'red': 0, 'yellow': 1, 'green': 2}
    plans.sort(key=lambda p: priority.get(p['color'], 99))

    return plans
