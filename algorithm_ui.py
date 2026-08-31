"""
高级补货算法-UI 集成（t8）—— UI 无关的纯逻辑层
=================================================

把 settings_ui 的「补货策略」区块、gui.py 的「_calc_from_items 分发 + 预警列」
需要用到的非 Tk 逻辑全部抽出来，供 test_algorithm_ui.py 无 Tk 单测。

本模块不 import tkinter；不依赖 settings.json 实际存在（用注入的 cfg 字典）。

设计基线：
- 与 t2 契约对齐：MODEL_ADVANCED + cfg 形参（向后兼容）。
- 与 t6 纪律一致：纯函数 + 类型提示，test_algorithm_ui.py 端到端验证。
- 「预警」列 = 3 类（不互斥，多个用 / 分隔）：
  * 滞销⚠      — calc_replenishment_advanced 的 slow_moving=True
  * 超卖🔥     — oversell_risk=True，level=high 时前缀「重」
  * 超卖⚠      — oversell_risk=True，level=medium 时
  * 低置信⚠    — 上游 item 携带 _low_confidence=True（OCR 双模型兜底）
- 不引入新依赖（纯 stdlib）。
"""
from datetime import datetime

__all__ = [
    'DEFAULT_ADVANCED_UI', 'build_default_cfg',
    'normalize_promo_dates', 'parse_promo_date_input',
    'collect_advanced_cfg_from_form',
    'warning_tags_for_plan', 'warning_display',
    'enrich_plan_with_advanced_fields', 'enrich_plan_with_warning',
    'dispatch_plan',
    # R2 预测升级
    'recommend_safety_days', 'forecast_next_period',
    'forecast_value', 'sanitize_series',
    'parse_bulk_promo_dates',
    'save_recommendation_cache', 'load_recommendation_cache',
    'clear_recommendation_cache',
]


# ─────────── 默认值 / 形状 ───────────

DEFAULT_ADVANCED_UI = {
    'promo': {
        'enabled': False,
        'dates_text': '',  # 形如 "2025-11-11, 2025-12-12"（UI 文本框用）
        'boost': 1.5,
        'lead_days': 3,
    },
    'slow': {
        'enabled': False,
        'threshold_per_day': 1.0,
        'stock_ratio': 5.0,
    },
    'season': {
        'enabled': False,
    },
    'oversell': {
        'enabled': False,
        'high_ratio': 0.5,
    },
}
"""UI 表单→cfg 子层形态（不同于 cfg 存储形态：dates 走文本框，便于编辑）。"""


def build_default_cfg() -> dict:
    """构造一个 cfg 字典，advanced 子配置 = 默认全关闭。
    用于 t8 集成点缺参兜底。
    """
    return {
        'model': 'advanced',
        'safety_days': 2,
        'in_transit_qty': 0,
        'advanced': {
            'promo': {'dates': [], 'boost': 1.5, 'lead_days': 3, 'enabled': False},
            'slow': {'threshold_per_day': 1.0, 'stock_ratio': 5.0, 'enabled': False},
            'season': {'enabled': False},
            'oversell': {'high_ratio': 0.5, 'enabled': False},
        },
    }


# ─────────── 大促日期解析 ───────────

def normalize_promo_dates(dates) -> list:
    """把任意输入归一为 YYYY-MM-DD 字符串列表（保留原序，去重保前）。

    - list/tuple  → 逐项 str() + strip() + strptime 校验
    - str         → 按逗号 / 空格 / 换行 切分
    - 其它        → []
    失败 / 非日期字符串全部丢弃，永不抛。
    """
    out = []
    if isinstance(dates, str):
        tokens = [t for t in __import__('re').split(r'[,\s\n\r]+', dates) if t]
    elif isinstance(dates, (list, tuple)):
        tokens = list(dates)
    else:
        tokens = []
    seen = set()
    for t in tokens:
        s = str(t or '').strip()
        if not s or s in seen:
            continue
        try:
            datetime.strptime(s[:10], '%Y-%m-%d')
        except Exception:
            continue
        seen.add(s)
        out.append(s[:10])
    return out


def parse_promo_date_input(text: str) -> list:
    """解析 settings_ui 文本框输入 → 归一后的日期列表。

    与 normalize_promo_dates 的区别：明确面向「单文本框输入」——空串返 []，
    坏日期静默丢弃。
    """
    if not text:
        return []
    return normalize_promo_dates(text)


# ─────────── UI 表单 → cfg 存储形态 ───────────

def collect_advanced_cfg_from_form(ui_form: dict) -> dict:
    """把 settings_ui 表单形态 → utils 存储形态（dict for cfg.advanced）。

    输入：{'promo': {'enabled': bool, 'dates_text': str, 'boost': float, 'lead_days': int},
           'slow':  {'enabled': bool, 'threshold_per_day': float, 'stock_ratio': float},
           'season': {'enabled': bool},
           'oversell': {'enabled': bool, 'high_ratio': float}}
    输出：{'promo': {'enabled', 'dates': [YYYY-MM-DD...], 'boost', 'lead_days'}, ...}
    """
    out = {
        'promo': {'enabled': False, 'dates': [], 'boost': 1.5, 'lead_days': 3},
        'slow': {'enabled': False, 'threshold_per_day': 1.0, 'stock_ratio': 5.0},
        'season': {'enabled': False},
        'oversell': {'enabled': False, 'high_ratio': 0.5},
    }
    if not isinstance(ui_form, dict):
        return out
    # promo
    try:
        p = ui_form.get('promo') or {}
        if isinstance(p, dict):
            out['promo']['enabled'] = bool(p.get('enabled'))
            out['promo']['dates'] = normalize_promo_dates(
                p.get('dates_text', p.get('dates', '')))
            try:
                b = float(p.get('boost', out['promo']['boost']) or 0)
                if b > 0:
                    out['promo']['boost'] = b
            except Exception:
                pass
            try:
                ld = int(p.get('lead_days', out['promo']['lead_days']) or 0)
                if ld >= 0:
                    out['promo']['lead_days'] = ld
            except Exception:
                pass
            if out['promo']['dates'] and 'enabled' not in p:
                out['promo']['enabled'] = True
    except Exception:
        pass
    # slow
    try:
        s = ui_form.get('slow') or {}
        if isinstance(s, dict):
            out['slow']['enabled'] = bool(s.get('enabled'))
            try:
                thr = float(s.get('threshold_per_day', out['slow']['threshold_per_day']) or 0)
                if thr > 0:
                    out['slow']['threshold_per_day'] = thr
            except Exception:
                pass
            try:
                sr = float(s.get('stock_ratio', out['slow']['stock_ratio']) or 0)
                if sr > 0:
                    out['slow']['stock_ratio'] = sr
            except Exception:
                pass
    except Exception:
        pass
    # season
    try:
        s2 = ui_form.get('season') or {}
        if isinstance(s2, dict):
            out['season']['enabled'] = bool(s2.get('enabled'))
    except Exception:
        pass
    # oversell
    try:
        o = ui_form.get('oversell') or {}
        if isinstance(o, dict):
            out['oversell']['enabled'] = bool(o.get('enabled'))
            try:
                hr = float(o.get('high_ratio', out['oversell']['high_ratio']) or 0)
                if 0 < hr < 1:
                    out['oversell']['high_ratio'] = hr
            except Exception:
                pass
    except Exception:
        pass
    return out


# ─────────── 预警标签生成 ───────────

# 标签常量（供测试断言和 UI 渲染复用）
TAG_SLOW = '滞销⚠'
TAG_OVERSELL_HIGH = '超卖🔥'  # level=high
TAG_OVERSELL_MED = '超卖⚠'  # level=medium
TAG_LOWCONF = '低置信⚠'


def warning_tags_for_plan(plan: dict, item: dict = None) -> list:
    """根据 plan + 源 item 计算预警标签列表（不互斥，多个并列）。

    顺序：先严重（超卖 high）→ 中（超卖 med / 滞销）→ 低（低置信）。
    """
    tags = []
    if not isinstance(plan, dict):
        return tags
    # 1) 超卖（高级模型字段；先 high 后 medium；严重级别最高）
    if plan.get('oversell_risk'):
        lvl = str(plan.get('oversell_level') or '').lower()
        if lvl == 'high':
            tags.append(TAG_OVERSELL_HIGH)
        elif lvl == 'medium':
            tags.append(TAG_OVERSELL_MED)
        else:
            # 兜底（理论不该出现）—— 当成 medium
            tags.append(TAG_OVERSELL_MED)
    # 2) 滞销（高级模型字段）
    if plan.get('slow_moving'):
        tags.append(TAG_SLOW)
    # 3) 低置信（OCR 阶段已标记的 item-level flag，向下透传）
    if isinstance(item, dict) and item.get('_low_confidence'):
        tags.append(TAG_LOWCONF)
    return tags


def warning_display(plan: dict, item: dict = None) -> str:
    """预警标签列表 → 表格列字符串（' / ' 分隔，无标签时返 ''）。

    空字符串 = 0 字符；导出/渲染时不再为这一行加任何视觉噪音。
    """
    tags = warning_tags_for_plan(plan, item)
    return ' / '.join(tags)


# ─────────── plan 字段补齐 ───────────

def enrich_plan_with_advanced_fields(plan: dict) -> dict:
    """给 plan 补齐高级模型附加字段的「缺省形态」。

    - 若 plan 已有这些字段（含经典/加权回退），不动。
    - 缺则补 None / False / 1.0 占位，让 UI 渲染 / 导出列可以无脑 .get。
    失败 = 返回原 plan。
    """
    if not isinstance(plan, dict):
        return plan
    plan.setdefault('season_factor', 1.0)
    plan.setdefault('promo_multiplier', 1.0)
    plan.setdefault('effective_daily', plan.get('daily', 0))
    plan.setdefault('slow_moving', False)
    plan.setdefault('oversell_risk', False)
    plan.setdefault('oversell_level', None)
    return plan


def enrich_plan_with_warning(plan: dict, item: dict = None) -> dict:
    """给 plan 加 `warning` 字段（与 status/color/qty 并列；供表格+导出渲染）。"""
    if not isinstance(plan, dict):
        return plan
    plan['warning'] = warning_display(plan, item)
    return plan


# ─────────── 模型分发（GUI 调用点抽象） ───────────

def dispatch_plan(item: dict, region: str, shipping: int,
                  cfg: dict, history_lookup=None) -> dict:
    """按 cfg.model 分发到 classic / weighted / advanced。

    - cfg 必须含 'model' / 'safety_days' / 'in_transit_qty' 三个键
    - history_lookup：可调用 (sku, reg, days[, name]) → list[dict]；仅 weighted/advanced 用
    - 任何异常 → 经典公式兜底 + model='classic(error)'（与 t2 calc_replenishment 主入口同语义）

    返回的 plan 已被 enrich_plan_with_advanced_fields + enrich_plan_with_warning 补齐，
    包含 model / warning 字段；保证 t8 集成点对 plan 的字段集合稳定。

    这是 t8 给 gui.py _calc_from_items 提供的统一调用面——不传 cfg 也行（走 default）。
    """
    from utils import (
        calc_replenishment_classic,
        calc_replenishment_weighted,
        calc_replenishment_advanced,
        MODEL_CLASSIC, MODEL_WEIGHTED, MODEL_ADVANCED,
    )
    # cfg 兜底
    if not isinstance(cfg, dict):
        cfg = build_default_cfg()
    model = str(cfg.get('model') or MODEL_CLASSIC).strip().lower()
    if model not in (MODEL_CLASSIC, MODEL_WEIGHTED, MODEL_ADVANCED):
        model = MODEL_CLASSIC
    safety_days = int(cfg.get('safety_days', 0) or 0)
    in_transit = int(cfg.get('in_transit_qty', 0) or 0)
    # 兼容 history_lookup 为 None（经典模式不需要）
    if history_lookup is None:
        history_lookup = lambda *a, **kw: []
    try:
        if model == MODEL_WEIGHTED:
            plan = calc_replenishment_weighted(
                item, region, shipping, safety_days, in_transit, history_lookup)
        elif model == MODEL_ADVANCED:
            plan = calc_replenishment_advanced(
                item, region, shipping, safety_days, in_transit,
                history_lookup, cfg)
        else:
            plan = calc_replenishment_classic(item, region, shipping, 1)
    except Exception:
        # R2 问题 修复：原代码无条件把 plan.model 覆写为
        # 'classic(error)'——但兜底第一段调 calc_replenishment_classic 已
        # 成功（plan 含正确 stock/sales/daily），仅是 weighted/advanced
        # 主路径失败。原覆写让用户看到 'classic(error)' 标签但内部是有效
        # 经典结果——误导排障。改为：仅当终极兜底（dict 字面量）失败时
        # 才打 'classic(error)'；经典兜底成功则保留 'classic' 标签。
        try:
            plan = calc_replenishment_classic(item, region, shipping, 1)
            # plan.model 已是 'classic'——保留
        except Exception:
            plan = {
                'status': '计算异常', 'color': 'gray', 'qty': 0,
                'ratio': 0.0, 'reorder': 0.0, 'daily': 0, 'stock': 0,
                'model': 'classic(error)',
            }
    # 补齐高级字段 + 预警
    enrich_plan_with_advanced_fields(plan)
    enrich_plan_with_warning(plan, item)
    return plan


# ============================================================
# R2 预测升级 — 安全库存推荐 + 指数平滑预测
# ============================================================
# 设计目标（PLAN §4 R2 预测升级）
# 1) recommend_safety_days：基于近 30 天日销序列的标准差 × z × √lead_days 算安全库存；
# 数据不足（<7 点 / 全 0）返 None，不强行给值（§4 失败哲学）。
# 2) forecast_next_period：简单指数平滑 SES 预测下一期日销；
# 初始 S_0=x_0（第一个样本）。
# 3) parse_bulk_promo_dates：批量粘贴解析（每行一个 YYYY-MM-DD）→ 合法行 + 非法行分别返回。
# 4) save/load/clear_recommendation_cache：设置页"上次推荐值"展示/应用按钮的缓存通道
# （键 'replenishment.recommendation'），gui 计算时写入，设置页读出展示。
# 纯逻辑 / 失败安全 / 可单测 —— 不依赖 Tk、不连真实 DB。
# ============================================================

# 安全库存推荐 z 系数（默认 95% 服务水平，常用于工业补货；范围 1.5~2.0）
DEFAULT_SAFETY_Z = 1.65
"""工业补货推荐 z=1.65（≈95% 服务水平）。允许调用方覆盖。"""

# 推荐函数所需的最少样本数（天）；不足直接返 None
MIN_RECOMMEND_SAMPLES = 7
"""推荐所需最少日销样本数。<7 视为不足，返 None。"""

# 指数平滑默认 α（历史推荐 0.1~0.5；高 α 偏最新观测）
DEFAULT_FORECAST_ALPHA = 0.5
"""简单指数平滑默认 α。允许调用方覆盖。"""

# 预测所需的最少样本数；不足直接返 None
MIN_FORECAST_SAMPLES = 2
"""指数平滑预测所需最少样本数。"""


def _history_to_daily_series(history_rows, days_window: int = 30):
    """把 history_rows 归约成「日期 → 销量」聚合的连续日销序列。

    与 utils._recent_avg 同源（captured_at/sales 字段），按日期求和；
    返回最新日期往前 days_window 天的 per-day sales 序列（list[float]，按日期升序）。

    缺失日期补 0（保窗口完整，标准差计算需要每个时间点）；失败 / 无数据 → []。
    """
    if not history_rows or not isinstance(history_rows, list):
        return []
    if days_window <= 0:
        return []
    try:
        per_day = {}
        latest = None
        for r in history_rows:
            if not isinstance(r, dict):
                continue
            try:
                sv = float(r.get('sales') or 0)
            except Exception:
                sv = 0.0
            ts = str(r.get('captured_at') or '')
            if not ts:
                continue
            try:
                d = datetime.fromisoformat(ts[:10]).date()
            except Exception:
                continue
            per_day[d] = per_day.get(d, 0.0) + sv
            if latest is None or d > latest:
                latest = d
        if latest is None:
            return []
        floor_ord = latest.toordinal() - days_window + 1
        from datetime import date as _date
        out = []
        for off in range(days_window):
            d_ord = floor_ord + off
            d = _date.fromordinal(d_ord)
            out.append(float(per_day.get(d, 0.0)))
        return out
    except Exception:
        return []


# ============================================================
# v1.6.0 TC-B3 预测增强 V3/V4（PLAN §1.9 WS-B2）
# ============================================================
# 纯逻辑 / 失败安全 / 可单测 —— 不依赖 Tk、不连真实 DB。
# V3 增强 = sanitize_series：IQR/winsorize 去极值 + 促销日反向缩放 +
# 连续零销剔除；30 天窗口下推荐轻量阈值；任何异常 → 原序列返回 + 标记
# `fallback=True`（绝不静默丢点）。
# V4 增强 = 尾部 N 日线性回归趋势叠加：forecast = base + slope × horizon；
# 缺数据 / 斜率不稳定 / 样本 < 14 → 显式回退 V3 + fallback_reason。
# V5 季节性明确不在本版（需 ≥8 周数据，留 §5.4 文档门槛）。
# ============================================================

# V3 清洗参数（v1.6.0 拍板）
_SANITIZE_IQR_K = 1.5  # IQR 倍数（标准 Tukey fence）
_SANITIZE_WINSOR_LOWER = 0.05  # winsorize 下分位
_SANITIZE_WINSOR_UPPER = 0.95  # winsorize 上分位
_SANITIZE_MAX_ZERO_STREAK = 7  # 连续零销 > 该值 → 剔除（保窗口完整）
_SANITIZE_PROMO_DOWNSHIFT = 0.7  # 促销日反向缩放系数（销量 1.4× → 还原 1.0）
_SANITIZE_PROMO_FACTOR = 1.4  # 判定促销日的倍率阈值（销量 > 中位数 × 该值）


def sanitize_series(series, cfg=None):
    """V3 日销序列清洗（IQR/winsorize 去极值 + 促销日反向缩放 + 连续零销剔除）。

    纯函数；输入 list[float] → 输出 (cleaned_series, meta)：
    - cleaned_series: list[float]，按原序，长度==输入（**保窗口完整**——剔除位补
      中位数；这是与"截断丢弃"的关键区别，_history_to_daily_series 后调用
      不可改索引语义）。
    - meta: dict，含 keys：
        - removed_outliers: int，IQR 标记并 winsorize 替换的点数
        - downshifted_promo: int，反向缩放的促销日数
        - zero_streaks_trimmed: int，连续零销剔除的点数
        - fallback: bool，是否回退原序列（异常路径）
        - fallback_reason: str|None，回退原因（异常时填写）
        - cfg: dict，本函数看到的实际参数（供测试断言）

    cfg 字段（缺省走常量 _SANITIZE_*）：
        - iqr_k: float,  IQR 倍数
        - winsor_lower: float, 下分位（0~1）
        - winsor_upper: float, 上分位（0~1，且 > winsor_lower）
        - max_zero_streak: int, 连续零销剔除阈值
        - promo_downshift: float, 促销日反向缩放系数
        - promo_factor: float, 判定促销日倍率阈值
        - disabled: bool, True → 原样返回（便于 A/B 关闭）
    """
    import statistics as _st
    empty_meta = {
        'removed_outliers': 0, 'downshifted_promo': 0,
        'zero_streaks_trimmed': 0, 'fallback': False,
        'fallback_reason': None, 'cfg': {},
    }
    if not isinstance(series, list) or len(series) == 0:
        return list(series) if isinstance(series, list) else [], {**empty_meta, 'fallback': True,
                                                                  'fallback_reason': 'empty_input'}

    # cfg 解析（缺键默认 / 非法值兜底）
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        iqr_k = float(cfg.get('iqr_k', _SANITIZE_IQR_K))
    except Exception:
        iqr_k = _SANITIZE_IQR_K
    try:
        wl = float(cfg.get('winsor_lower', _SANITIZE_WINSOR_LOWER))
    except Exception:
        wl = _SANITIZE_WINSOR_LOWER
    try:
        wu = float(cfg.get('winsor_upper', _SANITIZE_WINSOR_UPPER))
    except Exception:
        wu = _SANITIZE_WINSOR_UPPER
    try:
        mzs = int(cfg.get('max_zero_streak', _SANITIZE_MAX_ZERO_STREAK))
    except Exception:
        mzs = _SANITIZE_MAX_ZERO_STREAK
    try:
        pds = float(cfg.get('promo_downshift', _SANITIZE_PROMO_DOWNSHIFT))
    except Exception:
        pds = _SANITIZE_PROMO_DOWNSHIFT
    try:
        pf = float(cfg.get('promo_factor', _SANITIZE_PROMO_FACTOR))
    except Exception:
        pf = _SANITIZE_PROMO_FACTOR
    if wl < 0 or wl > 1 or wu <= wl or wu > 1:
        wl, wu = _SANITIZE_WINSOR_LOWER, _SANITIZE_WINSOR_UPPER
    if mzs < 0:
        mzs = _SANITIZE_MAX_ZERO_STREAK
    if pf <= 1.0:
        pf = _SANITIZE_PROMO_FACTOR
    if pds <= 0 or pds >= 1:
        pds = _SANITIZE_PROMO_DOWNSHIFT

    meta = {
        'removed_outliers': 0, 'downshifted_promo': 0,
        'zero_streaks_trimmed': 0, 'fallback': False,
        'fallback_reason': None,
        'cfg': {'iqr_k': iqr_k, 'winsor_lower': wl, 'winsor_upper': wu,
                'max_zero_streak': mzs, 'promo_downshift': pds, 'promo_factor': pf},
    }
    if cfg.get('disabled'):
        return list(series), {**meta, 'fallback': True, 'fallback_reason': 'disabled_by_cfg'}

    try:
        # 1) 数值规整（None / 非数值 → 0；负数钳 0）
        norm = []
        for x in series:
            try:
                xv = float(x)
            except Exception:
                xv = 0.0
            if xv < 0:
                xv = 0.0
            norm.append(xv)

        n = len(norm)
        if n == 0:
            return [], {**meta, 'fallback': True, 'fallback_reason': 'all_invalid'}

        # 2) IQR / winsorize 去极值（基于非零分位；全部为零直接跳过）
        nonzero = [x for x in norm if x > 0]
        if nonzero:
            try:
                if len(nonzero) >= 4:
                    qs = _st.quantiles(nonzero, n=4, method='inclusive')
                    q1, q3 = qs[0], qs[2]
                else:
                    q1, q3 = min(nonzero), max(nonzero)
            except Exception:
                q1, q3 = min(nonzero), max(nonzero)
            iqr = max(0.0, q3 - q1)
            upper_cap = q3 + iqr_k * iqr
            sorted_nz = sorted(nonzero)
            def _percentile(seq, p):
                if not seq:
                    return 0.0
                k = max(0, min(len(seq) - 1, int(round(p * (len(seq) - 1)))))
                return seq[k]
            wl_v = _percentile(sorted_nz, wl)
            wu_v = _percentile(sorted_nz, wu)
            winsorized = []
            for x in norm:
                if x == 0:
                    winsorized.append(0.0)
                    continue
                if x > upper_cap or x > wu_v:
                    winsorized.append(wu_v)
                    meta['removed_outliers'] += 1
                elif x < wl_v:
                    winsorized.append(wl_v)
                    meta['removed_outliers'] += 1
                else:
                    winsorized.append(x)
        else:
            winsorized = list(norm)

        # 3) 促销日反向缩放（销量 > 中位数 × promo_factor → 视为促销日）
        sorted_for_med = sorted([x for x in winsorized if x > 0])
        if sorted_for_med:
            median_v = sorted_for_med[len(sorted_for_med) // 2]
            threshold = median_v * pf
            if threshold > 0:
                for i, x in enumerate(winsorized):
                    if x >= threshold and pds != 1.0:
                        winsorized[i] = x * pds
                        meta['downshifted_promo'] += 1

        # 4) 连续零销剔除（保窗口完整——剔除位补中位数）
        if mzs > 0:
            sorted_pos = sorted([x for x in winsorized if x > 0])
            fill_v = sorted_pos[len(sorted_pos) // 2] if sorted_pos else 0.0
            i = 0
            cleaned = list(winsorized)
            while i < n:
                if cleaned[i] == 0:
                    j = i
                    while j < n and cleaned[j] == 0:
                        j += 1
                    streak_len = j - i
                    if streak_len > mzs:
                        for k in range(i + mzs, j):
                            cleaned[k] = fill_v
                            meta['zero_streaks_trimmed'] += 1
                    i = j
                else:
                    i += 1
            return cleaned, meta
        return winsorized, meta
    except Exception as e:
        return list(series), {**meta, 'fallback': True, 'fallback_reason': f'sanitize_error:{type(e).__name__}'}


def forecast_value(result):
    """向后兼容辅助函数（v1.6.0 TC-B3 配套）—— 把 dict 契约折叠成 float。

    用途：旧调用方（export_xlsx.py / 既有单元测试）只需一个标量预测值，
    调 forecast_value(v) 即可拿到 v['base']。**不影响新增 dict 字段**。
    - dict → 返回 result.get('base')（None 时 0.0）
    - 非 dict（理论上不会出现；防御性兜底）→ 视作 float 返回
    """
    if isinstance(result, dict):
        b = result.get('base')
        try:
            return float(b) if b is not None else 0.0
        except Exception:
            return 0.0
    try:
        return float(result)
    except Exception:
        return 0.0


# V4 趋势参数（v1.6.0 拍板）
_TREND_TAIL_N = 14  # 取尾部 N 日做线性回归
_TREND_MIN_SAMPLES = 14  # V4 启用最小样本数
_TREND_SLOPE_NORM_CAP = 0.5  # 斜率 / base 比例上限（超过视为回归不稳定，回退 V3）


def _linear_trend(tail):
    """尾部 N 日线性回归 → (base_at_t0, slope_per_step)。

    纯函数；输入 list[float] 长度 N → (float, float)：
    - base = t=0 处的回归值（首日基线）
    - slope = 每步增量
    缺数据 / N<2 → 返 (0.0, 0.0)；斜率异常大 → 仍计算（上层做 norm 兜底回退）。
    """
    try:
        if not isinstance(tail, list) or len(tail) < 2:
            return (0.0, 0.0)
        ys = [float(y) for y in tail]
        n = len(ys)
        x_mean = (n - 1) / 2.0
        y_mean = sum(ys) / n
        num = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(ys))
        den = sum((i - x_mean) ** 2 for i in range(n))
        if den <= 0:
            return (y_mean, 0.0)
        slope = num / den
        base = y_mean - slope * x_mean
        return (float(base), float(slope))
    except Exception:
        return (0.0, 0.0)


def forecast_next_period(history_rows, alpha: float = DEFAULT_FORECAST_ALPHA,
                         version: str = '', sanitize_cfg=None, as_dict: bool = False):
    """v1.6.0 增强预测：可选 dict 契约（as_dict=True）。

    契约（v1.5.13 → v1.6.0）：
    - 默认（as_dict=False）→ 返 float | None（与 v1.5.13 完全一致；gui.py / 旧调用零改动）
    - as_dict=True → 返 dict：
        {
          'base': float,  # 下一期预测日销（>=0；与旧契约同语义）
          'trend': float,  # 尾部线性回归斜率（每日增量；V4 用）
          'version': 'v3'|'v4',  # 实际启用的版本
          'fallback': bool,  # 是否回退
          'fallback_reason': str|None,  # 回退原因
        }
    - 旧调用方拿标量可用 `forecast_value(v)` 折叠。
    失败哲学：缺数据 / 异常 / V4 不稳定 → 显式回退 V3 + 填 fallback_reason
    （绝不静默，符合 §4 失败哲学 + 宪法 R8）。

    version 行为（缺省从 settings 读：replenishment.forecast_version）：
    - 'v3'：sanitize_series + SES（默认保守）
    - 'v4'：sanitize_series + SES + 尾部 N 日线性回归（base + slope×1）
    - 其他 / 缺省 → 'v3'（拍板：保守先）
    V5 季节性明确不在本版（需 ≥8 周数据，留 §5.4 文档门槛）。
    """
    # 0) 解析 version（缺省走 v3 保守）
    if not version:
        try:
            from utils import Config
            cfg = Config.load()
            version = str(((cfg.get('replenishment') or {}).get('forecast_version') or 'v3')).strip().lower() or 'v3'
        except Exception:
            version = 'v3'
    if version not in ('v3', 'v4'):
        version = 'v3'

    # 1) α 钳制（与 v1.5.13 同款）
    try:
        a = float(alpha)
    except Exception:
        a = DEFAULT_FORECAST_ALPHA
    if a < 0:
        a = 0.0
    elif a > 1:
        a = 1.0

    # 2) 取日销序列 + 清洗
    series = _history_to_daily_series(history_rows, days_window=30)
    if len(series) < MIN_FORECAST_SAMPLES:
        # 缺数据：实际启用版本回退到 v3（与 V4 内部的 insufficient_samples_for_v4 对齐）
        result = {
            'base': None, 'trend': 0.0, 'version': 'v3',
            'fallback': True, 'fallback_reason': 'insufficient_samples',
        }
        return result if as_dict else None
    cleaned, s_meta = sanitize_series(series, cfg=sanitize_cfg)

    # 3) SES 递推
    s = cleaned[0]
    for x in cleaned[1:]:
        s = a * x + (1.0 - a) * s
    if s < 0:
        s = 0.0
    if s > 1e9:
        s = 1e9
    base = round(s, 4)

    # 4) V4 趋势叠加
    trend = 0.0
    final = base
    final_version = version
    fallback = False
    fallback_reason = None
    if version == 'v4':
        if len(cleaned) < _TREND_MIN_SAMPLES:
            final_version = 'v3'
            fallback = True
            fallback_reason = 'insufficient_samples_for_v4'
        else:
            tail = cleaned[-_TREND_TAIL_N:] if len(cleaned) >= _TREND_TAIL_N else cleaned
            t_base, t_slope = _linear_trend(tail)
            if base > 0 and abs(t_slope) / base > _TREND_SLOPE_NORM_CAP:
                final_version = 'v3'
                trend = 0.0
                fallback = True
                fallback_reason = 'v4_unstable'
            else:
                trend = round(t_slope, 4)
                final = round(base + t_slope, 4)
                if final < 0:
                    final = 0.0

    if not as_dict:
        # 默认契约：浮点标量（与 v1.5.13 一致）—— None 仍表示数据不足
        return final if not (fallback or s_meta.get('fallback', False)) else None
    return {
        'base': final,
        'trend': trend,
        'version': final_version,
        'fallback': fallback or s_meta.get('fallback', False),
        'fallback_reason': fallback_reason if fallback else s_meta.get('fallback_reason'),
    }


def recommend_safety_days(history_rows, lead_days: int, z: float = DEFAULT_SAFETY_Z):
    """推荐安全库存天数（基于近期日销波动）。

    公式：safety_days = ceil(z × σ_daily × sqrt(lead_days))
      - σ_daily = std(近 30 天日销序列)，样本标准差（ddof=1）
      - lead_days = 运输天数；sqrt(lead_days) 把波动累积按 √t 缩放
      - z = 1.65 对应 ~95% 服务水平（工业补货常用）

    Returns:
        int: 推荐的整数天数（>=1），上限 30（与 settings_ui Spinbox 上限一致）。
        None: 数据不足 / 全 0 → σ=0 / 输入异常。
    Raises:
        无。所有异常路径返 None。

    契约（供 fix-glm/t5 接入）：
        - history_rows：list[dict]，每行 {captured_at, sales}，
          与 history_db.query_sku_history 返回同款形态。
        - lead_days：int，推荐窗口长度（运输天数）。<=0 → 返 None。
        - z：默认 1.65（可被覆盖；不暴露 UI 旋钮）。
    """
    # 1) 输入守卫
    try:
        ld = int(lead_days)
    except Exception:
        return None
    if ld <= 0:
        return None
    try:
        zv = float(z)
    except Exception:
        zv = DEFAULT_SAFETY_Z
    if zv <= 0:
        zv = DEFAULT_SAFETY_Z

    # 2) 归约：取近 30 天日销序列
    series = _history_to_daily_series(history_rows, days_window=30)
    if len(series) < MIN_RECOMMEND_SAMPLES:
        return None

    # 3) 样本标准差（ddof=1）；全 0 序列 → σ=0 → None
    import math
    n = len(series)
    mean = sum(series) / n
    sq_diff_sum = sum((x - mean) ** 2 for x in series)
    variance = sq_diff_sum / max(1, n - 1)
    if variance <= 0:
        return None
    sigma = math.sqrt(variance)
    if sigma <= 0:
        return None

    # 4) 推荐值：ceil(z × σ × √lead_days)，钳到 [1, 30]
    raw = zv * sigma * math.sqrt(ld)
    if raw <= 0:
        return None
    recommended = max(1, min(30, int(math.ceil(raw))))
    return recommended


def parse_bulk_promo_dates(text: str):
    """批量粘贴解析（每行一个 YYYY-MM-DD）→ (valid, invalid, total_lines)。

    与 normalize_promo_dates 区别：
        - 不静默丢弃非法行；返回给调用方由其决定 UX（状态栏/弹窗提示哪行错）。
        - 接受换行/逗号/空格/分号四种分隔；空行跳过不计。

    Args:
        text: 多行字符串（settings_ui 文本域粘贴）。

    Returns:
        tuple:
            - valid (list[str]): 合法 YYYY-MM-DD 字符串（按出现顺序，去重保前）。
            - invalid (list[tuple[int, str]]): (原始行号 1-based, 原始文本)。
              "原始行号"指输入文本按行拆分后的索引（1 起），便于 UI 高亮错误行。
            - total_lines (int): 总输入行数（含空行）；调用方做覆盖率展示。

    Raises:
        无（永远不抛——批量粘贴任意输入都不应让程序崩，§4 失败哲学）。

    契约：
        - 合法日期 = strptime 严格 '%Y-%m-%d' 解析。
        - 短前缀 / 长前缀：取前 10 字符尝试；解析失败即非法。
        - 重复合法日期只在 valid 留一份（保前），但 invalid 不计重复。
        - 一行既有合法又有非法：合法 token 进 valid；整行也记 invalid（合并展示）。
    """
    valid = []
    invalid = []
    seen = set()
    if not text or not isinstance(text, str):
        return (valid, invalid, 0)
    import re as _re
    physical_lines = text.splitlines()
    total_lines = 0
    for ln_idx, raw in enumerate(physical_lines, start=1):
        if not raw or not raw.strip():
            continue
        total_lines += 1
        tokens = _re.split(r'[,\s;]+', raw.strip())
        line_invalid_text = []
        for tok in tokens:
            t = (tok or '').strip()
            if not t:
                continue
            head = t[:10]
            try:
                datetime.strptime(head, '%Y-%m-%d')
            except Exception:
                line_invalid_text.append(t)
                continue
            if head in seen:
                continue
            seen.add(head)
            valid.append(head)
        # 整行若有非法 token → 进 invalid（合并展示）
        if line_invalid_text:
            invalid.append((ln_idx, raw.strip()))
    return (valid, invalid, total_lines)


# ─────────── 推荐缓存（设置页展示 / 应用按钮用） ───────────
# 键：settings.json['replenishment']['recommendation']
# 形态
# {
# 'safety_days': int,
# 'safety_days_lead': int, # 推荐时的 lead_days（运输天数）
# 'sigma': float, # 实际算出的样本标准差（只读展示）
# 'forecast': float, # forecast_next_period 结果
# 'n_samples': int, # 实际样本数（窗口内）
# 'z': float, # 使用的 z 系数
# 'computed_at': 'YYYY-MM-DDTHH:MM:SS', # ISO 本地时间
# 'sku_key': str, # 推荐是基于哪个商品（sku 或 region+name）
# }
# 写入方：fix-glm 在 _calc_from_items / dispatch_plan 后把当前商品的推荐值写入。
# 读取方：settings_ui 在「补货策略」卡 advanced 区展示 + 一键应用按钮。
# 失败语义：键不存在 / 损坏 / 过期 → load 返回 None（视为"无缓存推荐"）。

_RECOMMEND_KEY = 'recommendation'


def save_recommendation_cache(payload: dict) -> bool:
    """写入推荐缓存（settings.json['replenishment']['recommendation'] = payload）。

    Args:
        payload: 见模块头节点形态。非 dict → False（拒绝）。
    Returns:
        bool: True=写盘成功；False=拒绝/写盘失败。
    Raises:
        无（失败静默——UI 提示由调用方按状态栏处理）。
    """
    if not isinstance(payload, dict):
        return False
    try:
        from utils import Config
        cfg = Config.load()
    except Exception:
        return False
    if not isinstance(cfg, dict):
        return False
    rep = cfg.get('replenishment')
    if not isinstance(rep, dict):
        rep = {}
    # 只取白名单字段，避免外部误塞垃圾键
    safe = {
        'safety_days': int(payload.get('safety_days') or 0),
        'safety_days_lead': int(payload.get('safety_days_lead') or 0),
        'sigma': float(payload.get('sigma') or 0.0),
        'forecast': float(payload.get('forecast') or 0.0),
        'n_samples': int(payload.get('n_samples') or 0),
        'z': float(payload.get('z') or DEFAULT_SAFETY_Z),
        'computed_at': str(payload.get('computed_at') or ''),
        'sku_key': str(payload.get('sku_key') or ''),
    }
    if safe['safety_days'] <= 0:
        return False
    rep[_RECOMMEND_KEY] = safe
    cfg['replenishment'] = rep
    try:
        Config.save(cfg)
        return True
    except Exception:
        return False


def load_recommendation_cache():
    """读取推荐缓存。损坏 / 不存在 / 字段缺失 → None。"""
    try:
        from utils import Config
        cfg = Config.load()
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    rep = cfg.get('replenishment')
    if not isinstance(rep, dict):
        return None
    node = rep.get(_RECOMMEND_KEY)
    if not isinstance(node, dict):
        return None
    sd = node.get('safety_days')
    try:
        sd = int(sd)
    except Exception:
        return None
    if sd <= 0:
        return None
    return node


def clear_recommendation_cache() -> bool:
    """清除推荐缓存（仅删 _RECOMMEND_KEY 子节点；不破坏同层 cfg）。

    Returns:
        bool: True=已清/不存在（幂等）；False=写盘失败。
    """
    try:
        from utils import Config
        cfg = Config.load()
    except Exception:
        return False
    if not isinstance(cfg, dict):
        return True
    rep = cfg.get('replenishment')
    if not isinstance(rep, dict) or _RECOMMEND_KEY not in rep:
        return True
    try:
        del rep[_RECOMMEND_KEY]
    except Exception:
        return False
    cfg['replenishment'] = rep
    try:
        Config.save(cfg)
        return True
    except Exception:
        return False
