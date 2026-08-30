"""
PDD EZ — 公共工具函数
提供数据目录路径和设置读取，消除 main/ocr/gui 中的重复定义。
"""
import os, re, sys, json, threading

VERSION = "v1.5.7"


# ── v1.4.8 ：日志/调试脱敏（logger.py / ocr.py 共用）─────────
# 替换值为 ***，保留键名/前缀以便排查调用栈。
# 不引入第三方依赖；纯 stdlib re。
_SENSITIVE_KEYS = (
    'api_key', 'apikey', 'api-key',
    'password', 'passwd', 'pwd',
    'authorization', 'x-api-key', 'x-auth-token',
    'access_token', 'refresh_token', 'secret',
)
_SENSITIVE_KV_RE = re.compile(
    r'(?i)("??\'??)(' + '|'.join(re.escape(k) for k in _SENSITIVE_KEYS) + r')("??\'??)'
    r'\s*[:=]\s*("??)([^\s,"\'}\]\)]+)\4'
)
_BEARER_RE = re.compile(r'(?i)(Bearer\s+)[A-Za-z0-9._\-\=]+')


def _sanitize_for_log(text):
    """对日志/调试文本中的敏感字段做脱敏（替换值为 ***）。
    失败返回原文（不阻塞调用方）。支持 key=value / key: value / "key":"value" /
    Bearer xxx / Authorization: xxx 多种形式。"""
    if not isinstance(text, str) or not text:
        return text
    try:
        text = _BEARER_RE.sub(r'\1***', text)

        def _kv_sub(m):
            q4 = m.group(4) or ''
            tail = q4 if q4 else ''
            return f'{m.group(1)}{m.group(2)}{m.group(3)}: {q4}***{tail}'
        text = _SENSITIVE_KV_RE.sub(_kv_sub, text)
        return text
    except Exception:
        return text


def version_newer(remote: str, local: str) -> bool:
    """比较两个 vX.Y[.Z] 格式的版本号，返回 remote > local"""
    def _parse(v):
        # 去掉前缀 v/V，按 . 拆分转整数列表
        v = v.lstrip('vV')
        return [int(x) for x in v.split('.') if x.isdigit()]
    try:
        r = _parse(remote)
        l = _parse(local)
        # 补齐到相同长度：v1.1 vs v1.1.0 → [1,1,0] vs [1,1,0]，避免元组长度比较误判
        n = max(len(r), len(l))
        r += [0] * (n - len(r))
        l += [0] * (n - len(l))
        return tuple(r) > tuple(l)
    except Exception:
        # 解析失败（如 v1.4.0-beta 非纯数字段）不视为更新——静默判"有更新"
        # 会导致每次启动都弹提示（v1.4 审查修复）
        return False


# 核心列映射默认值：真实 PDD 后台表头（glm-4.6v 实测确认）
# 商品信息列含商品名 + 商品ID（小字 ID:xxx），识别时拆分为 name + sku_id
DEFAULT_COL_MAPPING = {
    'name': '商品信息',
    'stock': '仓库总库存',
    'sales': '仓库预估总销售数',
    'region': '销售区域',
    'warehouse': '仓库信息',
}
"""核心列映射默认值：通用列名 → 业务字段。可在设置页修改（后台列名变化时）。"""


def get_ocr_columns() -> dict:
    """
    读取识别列配置：{all: [探测到的全部列], selected: [客户勾选列], mapping: {字段: 列名}}。
    缺省时用默认映射，selected 为空则默认全部列。
    """
    s = Config.load()
    cfg = s.get('ocr_columns') or {}
    if not isinstance(cfg, dict):
        cfg = {}
    mapping = dict(DEFAULT_COL_MAPPING)
    saved_map = cfg.get('mapping') or {}
    if isinstance(saved_map, dict):
        # 旧默认检测：用户保存过但从未自定义（值恰为 v1.3 旧默认全集）时，
        # 视为"默认映射未动过"，允许新默认覆盖——否则升级后旧列名（商品名称/省份/仓库）
        # 匹配不上真实表头（商品信息/销售区域/仓库信息），导致识别为空。
        _OLD_DEFAULTS = {
            'name': '商品名称', 'stock': '仓库总库存', 'sales': '仓库预估总销售数',
            'region': '省份', 'warehouse': '仓库',
        }
        _is_old_default = bool(saved_map) and all(
            saved_map.get(k) == v for k, v in _OLD_DEFAULTS.items())
        if not _is_old_default:
            for k, v in saved_map.items():
                if v:  # 空值不覆盖默认
                    mapping[k] = v
    return {
        'all': list(cfg.get('all') or []),
        'selected': list(cfg.get('selected') or []),
        'mapping': mapping,
    }


def save_ocr_columns(all_cols: list = None, selected: list = None, mapping: dict = None):
    """持久化识别列配置到 settings.json（原子写入）。"""
    s = Config.load()
    cur = s.get('ocr_columns') or {}
    if not isinstance(cur, dict):
        cur = {}
    if all_cols is not None:
        cur['all'] = list(all_cols)
    if selected is not None:
        cur['selected'] = list(selected)
    if mapping is not None:
        cur['mapping'] = dict(mapping)
    s['ocr_columns'] = cur
    Config.save(s)


def get_secondary_model() -> str:
    """读取双模型验证的副模型，默认 glm-4v-flash。"""
    s = Config.load()
    cfg = s.get('ocr_columns') or {}
    if not isinstance(cfg, dict):
        return 'glm-4v-flash'
    return cfg.get('secondary_model') or 'glm-4v-flash'


def save_secondary_model(name: str):
    """保存双模型验证的副模型（原子写入）。"""
    s = Config.load()
    cur = s.get('ocr_columns') or {}
    if not isinstance(cur, dict):
        cur = {}
    cur['secondary_model'] = name
    s['ocr_columns'] = cur
    Config.save(s)


def get_usage_cfg() -> dict:
    """读取用量采集配置（usage 节点，走 Config 模板自愈合并；v1.4.7 WS-C）。

    结构（settings_template.json 自愈补全）：
      {enabled: true, batch_budget_cny: 0, monthly_budget_cny: 0,
       pricing: {provider: {model: {input_per_million, output_per_million, image_per_call}}},
       debug_archive_enabled: false}
    预算两键本次只落配置不接逻辑（P2 启用）。
    """
    s = Config.load()
    u = s.get('usage')
    return dict(u) if isinstance(u, dict) else {}


def get_history_cfg() -> dict:
    """读取历史库配置（history 节点，走 Config 模板自愈合并；v1.4.7 WS-A）。

    结构：{retention_days: 180, max_rows: 200000}
    """
    s = Config.load()
    h = s.get('history')
    return dict(h) if isinstance(h, dict) else {}


# ============================================================
# 补货模型框架
# 用户裁定：经典模式（现行公式）原样保留为默认模式，一行公式逻辑都不许改；
# 加权模式作为额外可选模型，回退时标注「经典(无历史)」
# ============================================================
MODEL_CLASSIC = 'classic'
MODEL_WEIGHTED = 'weighted'
MODEL_ADVANCED = 'advanced'
DEFAULT_REPLENISHMENT_CFG = {
    'model': MODEL_CLASSIC,
    'safety_days': 2,
    'in_transit_qty': 0,
    'advanced': {
        # 缺省整体静默关闭各因子——任何字段缺失都视为未启用。
        'promo': {'dates': [], 'boost': 1.5, 'lead_days': 3, 'enabled': False},
        'slow': {'threshold_per_day': 1.0, 'stock_ratio': 5.0, 'enabled': False},
        'season': {'enabled': False},
        'oversell': {'high_ratio': 0.5, 'enabled': False},
    },
}


def _default_advanced_cfg() -> dict:
    """深拷贝默认 advanced 子配置（避免调用方对 DEFAULT 内嵌 dict 原地污染）。"""
    return {
        'promo': {'dates': [], 'boost': 1.5, 'lead_days': 3, 'enabled': False},
        'slow': {'threshold_per_day': 1.0, 'stock_ratio': 5.0, 'enabled': False},
        'season': {'enabled': False},
        'oversell': {'high_ratio': 0.5, 'enabled': False},
    }


def _merge_advanced_cfg(raw) -> dict:
    """合并用户 advanced 配置：全部 .get 兜底，缺字段回退默认；不抛异常。

    用户可只填其中一个子节点（promo/slow/season/oversell），缺字段 = 该因子关闭。
    """
    out = _default_advanced_cfg()
    if not isinstance(raw, dict):
        return out
    # promo
    try:
        p = raw.get('promo')
        if isinstance(p, dict):
            ds = p.get('dates')
            if isinstance(ds, (list, tuple)):
                # 仅保留合法 YYYY-MM-DD 字符串（fromisoformat 严格解析；坏日期/类型一律丢）
                from datetime import datetime as _pdt
                cleaned = []
                for d in ds:
                    s = str(d or '').strip()
                    if not s:
                        continue
                    try:
                        # 严格解析：必须能生成有效 date
                        _pdt.strptime(s[:10], '%Y-%m-%d')
                        cleaned.append(s[:10])
                    except Exception:
                        continue
                out['promo']['dates'] = cleaned
            try:
                out['promo']['boost'] = float(p.get('boost', out['promo']['boost']) or 0)
            except Exception:
                pass
            try:
                ld = int(p.get('lead_days', out['promo']['lead_days']) or 0)
                out['promo']['lead_days'] = max(0, ld)
            except Exception:
                pass
            if 'enabled' in p:
                out['promo']['enabled'] = bool(p.get('enabled'))
            elif out['promo']['dates']:
                # 有 dates 列表即视为启用（即使没显式 enabled=True）
                out['promo']['enabled'] = True
    except Exception:
        pass
    # slow
    try:
        s = raw.get('slow')
        if isinstance(s, dict):
            try:
                out['slow']['threshold_per_day'] = float(s.get('threshold_per_day', out['slow']['threshold_per_day']) or 0)
            except Exception:
                pass
            try:
                out['slow']['stock_ratio'] = float(s.get('stock_ratio', out['slow']['stock_ratio']) or 0)
            except Exception:
                pass
            if 'enabled' in s:
                out['slow']['enabled'] = bool(s.get('enabled'))
    except Exception:
        pass
    # season（只关心 enabled 开关）
    try:
        s2 = raw.get('season')
        if isinstance(s2, dict) and 'enabled' in s2:
            out['season']['enabled'] = bool(s2.get('enabled'))
    except Exception:
        pass
    # oversell
    try:
        o = raw.get('oversell')
        if isinstance(o, dict):
            try:
                out['oversell']['high_ratio'] = float(o.get('high_ratio', out['oversell']['high_ratio']) or 0)
            except Exception:
                pass
            if 'enabled' in o:
                out['oversell']['enabled'] = bool(o.get('enabled'))
    except Exception:
        pass
    return out


def get_replenishment_cfg() -> dict:
    """读取补货策略配置。结构：{model:'classic'|'weighted'|'advanced', safety_days:int, in_transit_qty:int, advanced: {...}}。

    缺字段时回退默认（用户裁定：default=classic，永不破坏现有行为）。
    advanced 子配置全部 .get 兜底，缺省时整体静默关闭各因子；不会写盘。
    """
    try:
        s = Config.load()
        r = s.get('replenishment')
        if not isinstance(r, dict):
            return dict(DEFAULT_REPLENISHMENT_CFG)
        out = dict(DEFAULT_REPLENISHMENT_CFG)
        m = str(r.get('model') or '').strip().lower()
        if m in (MODEL_CLASSIC, MODEL_WEIGHTED, MODEL_ADVANCED):
            out['model'] = m
        try:
            out['safety_days'] = max(0, int(r.get('safety_days', DEFAULT_REPLENISHMENT_CFG['safety_days'])))
        except Exception:
            out['safety_days'] = DEFAULT_REPLENISHMENT_CFG['safety_days']
        try:
            out['in_transit_qty'] = max(0, int(r.get('in_transit_qty', DEFAULT_REPLENISHMENT_CFG['in_transit_qty'])))
        except Exception:
            out['in_transit_qty'] = DEFAULT_REPLENISHMENT_CFG['in_transit_qty']
        out['advanced'] = _merge_advanced_cfg(r.get('advanced'))
        return out
    except Exception:
        return dict(DEFAULT_REPLENISHMENT_CFG)


def _to_int_safe(v, default=0):
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def calc_replenishment_classic(item: dict, region: str, shipping: int, offset: int) -> dict:
    """经典模式补货（与原 gui.py _calc_from_items 同款公式，逐字逐行保留）。

    输入：item={name,stock,sales,...}, region, shipping(运输天数), offset(刷新偏置)
    输出：{status, color, qty, ratio, reorder, model='classic'}（字段顺序与原实现一致）

    用户裁定：此函数是原公式的精确复刻——任何修改都视为破坏 t13 铁律。
    """
    name = item.get('name', '')
    stock = _to_int_safe(item.get('stock', 0))
    daily = max(_to_int_safe(item.get('sales', 0)), 0)
    calc_daily = daily if daily > 0 else 1
    if daily <= 0:
        status = '无销量·观察'
        color = 'gray'
        qty = 0
        ratio = 0.0
        reorder = 0.0
    else:
        ratio = stock / calc_daily
        lead_time = shipping + offset
        reorder = ratio - lead_time
        if reorder <= 0:
            status = '立刻补货'
            color = 'red'
            qty = max(daily * 8, 100)
            qty = ((qty + 99) // 100) * 100
        elif reorder <= 2:
            status = f'{reorder:.0f}天后下单'
            color = 'yellow'
            qty = max(daily * 8, 100)
            qty = ((qty + 99) // 100) * 100
        else:
            status = f'{reorder:.0f}天后下单'
            color = 'green'
            qty = 0
    return {
        'status': status, 'color': color, 'qty': qty,
        'ratio': round(ratio, 1),
        'reorder': reorder,
        'daily': daily, 'stock': stock,
        'model': MODEL_CLASSIC,
    }


def _weighted_daily(history_rows: list, asof_ts: str = None) -> float:
    """加权日销：0.5×近7日 + 0.3×近14日 + 0.2×近30日。

    history_rows：query_sku_history 返回的列表，按 captured_at 升序；
                  每行至少含 'captured_at' 和 'sales' 字段。
    返回 0.0 表示无有效数据（调用方据此回退经典模式）。
    """
    from datetime import datetime
    if not history_rows or not isinstance(history_rows, list):
        return 0.0
    try:
        # 取每行 (captured_at, sales) 元组，过滤 None/无效
        pairs = []
        for r in history_rows:
            if not isinstance(r, dict):
                continue
            try:
                sales_v = float(r.get('sales') or 0)
            except Exception:
                sales_v = 0.0
            ts = str(r.get('captured_at') or '')
            if not ts:
                continue
            try:
                d = datetime.fromisoformat(ts[:10])
            except Exception:
                continue
            pairs.append((d, sales_v))
        if not pairs:
            return 0.0
        pairs.sort(key=lambda x: x[0])
        latest = pairs[-1][0]
        def avg(days):
            floor = latest.toordinal() - days + 1
            vals = [s for (d, s) in pairs if d.toordinal() >= floor and s > 0]
            if not vals:
                return 0.0
            return sum(vals) / len(vals)
        d7 = avg(7)
        d14 = avg(14)
        d30 = avg(30)
        if d7 == 0 and d14 == 0 and d30 == 0:
            return 0.0
        return 0.5 * d7 + 0.3 * d14 + 0.2 * d30
    except Exception:
        return 0.0


def calc_replenishment_weighted(item: dict, region: str, shipping: int,
                                 safety_days: int, in_transit_qty: int,
                                 history_lookup) -> dict:
    """加权模式补货。

    日销 = 0.5×近7日 + 0.3×近14日 + 0.2×近30日（无数据返 0.0 → 回退经典）。
    补货量 = max(0, (shipping + safety_days) × 日销 - in_transit - stock)，100 取整。
    状态阈值沿用经典：ratio(=stock/daily) - lead_time <= 0 立刻补货 / <=2 yellow / 否则 green。

    history_lookup：callable(item, region, days) → list[dict]（与 history_db.query_sku_history 兼容）；
                    返回 [] 或异常均回退经典 + 标注「经典(无历史)」。
    """
    name = item.get('name', '')
    stock = _to_int_safe(item.get('stock', 0))
    sku_id = item.get('sku_id', '') or ''
    _it_region = item.get('region') or region
    fallback = calc_replenishment_classic(item, _it_region, shipping, 1)
    fallback['model'] = 'classic(no_history)'  # 标注回退
    try:
        # SKU 优先；无则用 (region, name) 关联
        if sku_id:
            rows = history_lookup(sku_id, _it_region, 30) or []
        else:
            rows = history_lookup('', _it_region, 30, name=name) or []
    except Exception:
        return fallback
    if not rows:
        return fallback
    daily_w = _weighted_daily(rows)
    if daily_w <= 0:
        return fallback
    # 公式：ratio 与 reorder 与经典一致，qty 用 (shipping + safety) × daily - in_transit - stock
    ratio = stock / daily_w
    lead_time = shipping + safety_days
    reorder = ratio - lead_time
    if reorder <= 0:
        status = '立刻补货'
        color = 'red'
        qty_raw = (shipping + safety_days) * daily_w - in_transit_qty - stock
    elif reorder <= 2:
        status = f'{reorder:.0f}天后下单'
        color = 'yellow'
        qty_raw = (shipping + safety_days) * daily_w - in_transit_qty - stock
    else:
        status = f'{reorder:.0f}天后下单'
        color = 'green'
        qty_raw = 0
    qty = max(0, int(qty_raw))
    qty = ((qty + 99) // 100) * 100
    return {
        'status': status, 'color': color, 'qty': qty,
        'ratio': round(ratio, 1),
        'reorder': reorder,
        'daily': round(daily_w, 2), 'stock': stock,
        'model': MODEL_WEIGHTED,
    }


def _season_factor(history_rows: list) -> float:
    """季节系数：基于 history 近 12 周周销量 vs 近 4 周均值的比。

    实现：把 history_rows 按周聚合（按 ISO 周），取最后 12 周；ratio = mean(最近4周) / mean(最近12周)。
    - 无历史 / 解析异常 / 12 周均值 ≤ 0 → 1.0（关闭季节因子）
    - ratio 钳制到 [0.5, 2.0] 防止极端历史数据把预测拉飞
    失败永远返 1.0，不抛。
    """
    if not history_rows or not isinstance(history_rows, list):
        return 1.0
    try:
        from datetime import datetime as _dt
        # (week_start: date, sales_sum) 聚合
        weeks = {}
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
                d = _dt.fromisoformat(ts[:10]).date()
            except Exception:
                continue
            # ISO 周对齐：周一为周首
            ws = d.toordinal() - d.weekday()  # Monday ordinal
            weeks[ws] = weeks.get(ws, 0.0) + sv
        if not weeks:
            return 1.0
        # 取排序后的周销量序列
        sorted_weeks = sorted(weeks.items())  # [(ordinal, sum), ...]
        last12 = [v for _, v in sorted_weeks[-12:]]
        last4 = [v for _, v in sorted_weeks[-4:]] if len(sorted_weeks) >= 4 else last12
        if not last12:
            return 1.0
        mean12 = sum(last12) / len(last12)
        mean4 = sum(last4) / len(last4) if last4 else mean12
        if mean12 <= 0:
            return 1.0
        ratio = mean4 / mean12
        if ratio < 0.5:
            ratio = 0.5
        elif ratio > 2.0:
            ratio = 2.0
        return round(ratio, 3)
    except Exception:
        return 1.0


def _promo_multiplier(cfg_promo: dict, asof: str = None) -> float:
    """大促倍数：今天 (asof) 落在 cfg.promo.dates 任一日期的 ±cfg.promo.lead_days 窗口内 → boost；否则 1.0。

    cfg_promo：get_replenishment_cfg()['advanced']['promo']（enabled/dates/boost/lead_days）
    asof：'YYYY-MM-DD'，None 时取今天。
    - promo 未启用或 dates 为空 → 1.0
    - asof 解析失败 → 1.0（不阻塞）
    - boost <= 1 也按 1.0 处理（不放大）
    """
    try:
        if not isinstance(cfg_promo, dict) or not cfg_promo.get('enabled'):
            return 1.0
        dates = cfg_promo.get('dates') or []
        if not dates:
            return 1.0
        from datetime import datetime as _dt, timedelta as _td
        try:
            ld = max(0, int(cfg_promo.get('lead_days', 0) or 0))
        except Exception:
            ld = 0
        try:
            boost = float(cfg_promo.get('boost', 1.0) or 1.0)
        except Exception:
            boost = 1.0
        if boost <= 1.0:
            return 1.0
        try:
            today = _dt.fromisoformat((asof or '')[:10]).date() if asof else _dt.now().date()
        except Exception:
            return 1.0
        hit = False
        for ds in dates:
            try:
                pdate = _dt.fromisoformat(str(ds)[:10]).date()
            except Exception:
                continue
            if abs((today - pdate).days) <= ld:
                hit = True
                break
        return boost if hit else 1.0
    except Exception:
        return 1.0


def _recent_avg(history_rows: list, days: int) -> float:
    """近 N 日日均销量（无历史返 0.0，失败返 0.0）。

    与 _weighted_daily 同源数据契约：每行 {captured_at, sales}，按日期累计后取均值。
    """
    if not history_rows or not isinstance(history_rows, list):
        return 0.0
    try:
        from datetime import datetime as _dt
        latest = None
        per_day = {}
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
                d = _dt.fromisoformat(ts[:10]).date()
            except Exception:
                continue
            per_day[d] = per_day.get(d, 0.0) + sv
            if latest is None or d > latest:
                latest = d
        if latest is None or days <= 0:
            return 0.0
        floor_ord = latest.toordinal() - days + 1
        vals = [v for d, v in per_day.items() if d.toordinal() >= floor_ord and v > 0]
        if not vals:
            return 0.0
        return sum(vals) / len(vals)
    except Exception:
        return 0.0


def calc_replenishment_advanced(item: dict, region: str, shipping: int,
                                 safety_days: int, in_transit_qty: int,
                                 history_lookup, cfg: dict = None) -> dict:
    """高级模式补货：经典/加权之上的可选层，叠加季节/大促/滞销/超卖四个因子。

    公式语义：
      daily_raw        = item['sales'] 整数（沿用经典取数）
      season_factor    = history 近 12 周周销量 vs 近 4 周均值的比，钳制 [0.5, 2.0]；无历史=1.0
      promo_multiplier = 命中 cfg.promo.dates 区间 → cfg.promo.boost，否则 1.0
      effective_daily  = daily_raw × season_factor × promo_multiplier
      lead_time        = shipping + safety_days
      required         = lead_time × effective_daily
      qty_raw          = max(0, required - in_transit_qty - stock)（同 weighted）
      qty              = ceil(qty_raw / 100) × 100
      ratio            = stock / max(effective_daily, 1)
      reorder          = ratio - lead_time（用于颜色判定）
      color            = reorder <= 0 ? red : (reorder <= 2 ? yellow : green)
      slow_moving      = (近14日均销 < slow.threshold_per_day) AND (stock/daily_raw > slow.stock_ratio) AND 有历史
      oversell_risk    = stock < required
      oversell_level   = oversell_risk ? ('high' if stock < high_ratio × required else 'medium') : None

    输入：item={name,stock,sales,sku_id,...}, region, shipping, safety_days, in_transit_qty
          history_lookup：callable(sku_id, region, days[, name]) → list[dict]（与 query_sku_history 兼容）
          cfg：get_replenishment_cfg() 输出；缺省或 None 时调 get_replenishment_cfg() 取（不传则拿不到 advanced 节点）
    输出：与 classic/weighted 同构字段 + 附加
      {model:'advanced', season_factor, promo_multiplier, effective_daily,
       slow_moving, oversell_risk, oversell_level}

    任何历史/参数异常 → 回退 calc_replenishment_classic + model='classic(error)'
    """
    sku_id = (item.get('sku_id', '') or '')
    name = item.get('name', '')
    _it_region = item.get('region') or region
    stock = _to_int_safe(item.get('stock', 0))
    daily_raw = max(_to_int_safe(item.get('sales', 0)), 0)
    # 兜底 cfg
    try:
        if not isinstance(cfg, dict):
            cfg = get_replenishment_cfg()
    except Exception:
        cfg = {'model': MODEL_ADVANCED, 'safety_days': safety_days,
               'in_transit_qty': in_transit_qty, 'advanced': _default_advanced_cfg()}
    adv = cfg.get('advanced') if isinstance(cfg, dict) else None
    if not isinstance(adv, dict):
        adv = _default_advanced_cfg()

    # 拉历史（30 天覆盖季节计算；近 14 日均销看滞销）
    rows = []
    try:
        if sku_id:
            rows = history_lookup(sku_id, _it_region, 30) or []
        else:
            rows = history_lookup('', _it_region, 30, name=name) or []
    except Exception:
        # history_lookup 抛异常 → 经典公式兜底，标注 'classic(error)'
        try:
            fb = calc_replenishment_classic(item, _it_region, shipping, 1)
        except Exception:
            fb = {
                'status': '计算异常', 'color': 'gray', 'qty': 0,
                'ratio': 0.0, 'reorder': 0.0, 'daily': 0, 'stock': stock,
                'model': 'classic(error)',
            }
        fb['model'] = 'classic(error)'
        return fb
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        rows = []
    has_history = bool(rows)

    # 1) 季节系数
    season_cfg = adv.get('season') or {}
    if season_cfg.get('enabled') and has_history:
        season_factor = _season_factor(rows)
    else:
        season_factor = 1.0

    # 2) 大促倍数（不依赖历史）
    promo_factor = _promo_multiplier(adv.get('promo') or {})

    effective_daily = daily_raw * season_factor * promo_factor

    # 3) 滞销判定（需要 history）
    slow_cfg = adv.get('slow') or {}
    slow_moving = False
    if slow_cfg.get('enabled') and has_history and daily_raw > 0:
        try:
            avg14 = _recent_avg(rows, 14)
            thr = float(slow_cfg.get('threshold_per_day', 1.0) or 0)
            sr = float(slow_cfg.get('stock_ratio', 5.0) or 0)
            if avg14 < thr and (stock / daily_raw) > sr:
                slow_moving = True
        except Exception:
            slow_moving = False

    # 4) 超卖判定
    lead_time = shipping + max(0, int(safety_days or 0))
    required = lead_time * effective_daily
    oversell_cfg = adv.get('oversell') or {}
    oversell_risk = False
    oversell_level = None
    if oversell_cfg.get('enabled') and effective_daily > 0:
        try:
            if stock < required:
                oversell_risk = True
                hr = float(oversell_cfg.get('high_ratio', 0.5) or 0)
                oversell_level = 'high' if stock < (hr * required) else 'medium'
        except Exception:
            oversell_risk = False
            oversell_level = None

    # 5) qty / 颜色（沿用 weighted 语义）
    try:
        if effective_daily <= 0:
            # 无销量：照搬经典「无销量·观察」
            return {
                'status': '无销量·观察', 'color': 'gray', 'qty': 0,
                'ratio': 0.0, 'reorder': 0.0, 'daily': 0, 'stock': stock,
                'model': MODEL_ADVANCED,
                'season_factor': season_factor, 'promo_multiplier': promo_factor,
                'effective_daily': 0.0, 'slow_moving': False,
                'oversell_risk': False, 'oversell_level': None,
            }
        ratio = stock / effective_daily
        reorder = ratio - lead_time
        if reorder <= 0:
            status = '立刻补货'
            color = 'red'
            qty_raw = required - int(in_transit_qty or 0) - stock
        elif reorder <= 2:
            status = f'{reorder:.0f}天后下单'
            color = 'yellow'
            qty_raw = required - int(in_transit_qty or 0) - stock
        else:
            status = f'{reorder:.0f}天后下单'
            color = 'green'
            qty_raw = 0
        qty = max(0, int(qty_raw))
        qty = ((qty + 99) // 100) * 100
    except Exception:
        # 任何算术/参数异常 → 经典兜底
        try:
            fb = calc_replenishment_classic(item, _it_region, shipping, 1)
        except Exception:
            fb = {
                'status': '计算异常', 'color': 'gray', 'qty': 0,
                'ratio': 0.0, 'reorder': 0.0, 'daily': 0, 'stock': stock,
                'model': 'classic(error)',
            }
        fb['model'] = 'classic(error)'
        return fb

    return {
        'status': status, 'color': color, 'qty': qty,
        'ratio': round(ratio, 1),
        'reorder': reorder,
        'daily': round(effective_daily, 2), 'stock': stock,
        'model': MODEL_ADVANCED,
        'season_factor': season_factor,
        'promo_multiplier': promo_factor,
        'effective_daily': round(effective_daily, 2),
        'slow_moving': slow_moving,
        'oversell_risk': oversell_risk,
        'oversell_level': oversell_level,
    }


def calc_replenishment(items, region, model, safety_days, in_transit_qty,
                       shipping_lookup, history_lookup, offset=1,
                       cfg: dict = None) -> list:
    """t13 P3-A 补货模型入口：分发到 classic / weighted / advanced。

    shipping_lookup：callable(item, region) → int（运输天数；无则返 1）
    history_lookup：callable(sku_id, region, days[, name]) → list[dict]（与 query_sku_history 兼容）
    cfg：可选，advanced 模型专用配置（get_replenishment_cfg() 输出）；
         缺省或 None 时调 get_replenishment_cfg() 取；其他模型忽略。
    返回与原 _calc_from_items 同款字段的 plan dict 列表，附加 'model' 字段。
    任何异常 → 逐商品回退经典公式（绝不中断整批）。

    向后兼容：新增 cfg 为可选形参，已有调用方（test_smoke / gui）不传也照常工作。
    """
    # advanced 模式需要 cfg：缺参或非 dict 时拉一次默认配置
    _adv_cfg = None
    if str(model or '').strip().lower() == MODEL_ADVANCED:
        if not isinstance(cfg, dict):
            try:
                _adv_cfg = get_replenishment_cfg()
            except Exception:
                _adv_cfg = {'model': MODEL_ADVANCED,
                            'safety_days': int(safety_days or 0),
                            'in_transit_qty': int(in_transit_qty or 0),
                            'advanced': _default_advanced_cfg()}
        else:
            _adv_cfg = cfg

    out = []
    for item in items:
        try:
            it_region = item.get('region') or region
            try:
                shipping = int(shipping_lookup(item, it_region) or 1)
            except Exception:
                shipping = 1
            if model == MODEL_WEIGHTED:
                plan = calc_replenishment_weighted(
                    item, it_region, shipping,
                    int(safety_days or 0), int(in_transit_qty or 0),
                    history_lookup,
                )
            elif model == MODEL_ADVANCED:
                plan = calc_replenishment_advanced(
                    item, it_region, shipping,
                    int(safety_days or 0), int(in_transit_qty or 0),
                    history_lookup, _adv_cfg,
                )
            else:
                plan = calc_replenishment_classic(item, it_region, shipping, int(offset or 1))
        except Exception:
            # 终极兜底：经典公式也炸了也要返一个能用的结构
            try:
                plan = calc_replenishment_classic(item, item.get('region') or region, 1, 1)
            except Exception:
                plan = {
                    'status': '计算异常', 'color': 'gray', 'qty': 0,
                    'ratio': 0.0, 'reorder': 0.0, 'daily': 0, 'stock': 0,
                    'model': 'classic(error)',
                }
        out.append(plan)
    return out


class Config:
    """settings.json 读写（唯一通道 + 模板自愈 + 原子写）。

    v1.4.5（bug hunt F20）：保存全程持锁，防多线程并发写坏配置。
    """
    _CONFIG_SAVE_LOCK = threading.Lock()
    """配置单例：唯一读写 settings.json，原子写入。
    v1.4 升级（借鉴 March7thAssistant config.py）：加载时与 settings_template.json
    递归合并——用户配置优先，缺失字段从模板补全并写回（配置自愈，损坏/缺字段不崩）。"""

    _template_cache = None
    # v1.4.2 性能优化：settings.json 读取缓存（mtime 变化才重读）——
    # 批量识别每轮 OCR 多次调 Config.load/get_api_config/get_ocr_columns，
    # 无缓存时上百次文件读+JSON解析+模板合并；save 后清缓存强制重读。
    _load_cache = {'mtime': -1, 'data': None}

    @staticmethod
    def _load_template():
        """加载 settings_template.json 默认结构（缓存，失败返回 {}）"""
        if Config._template_cache is not None:
            return Config._template_cache
        tpl = {}
        try:
            # 打包后模板在 _MEIPASS；源码在脚本目录
            for cand in [os.path.join(get_base_dir(), 'settings_template.json'),
                         os.path.join(sys._MEIPASS, 'settings_template.json') if getattr(sys, 'frozen', False) else '']:
                if cand and os.path.exists(cand):
                    with open(cand, 'r', encoding='utf-8') as f:
                        tpl = json.load(f)
                    break
        except Exception:
            tpl = {}
        Config._template_cache = tpl
        return tpl

    @staticmethod
    def _merge(base: dict, override: dict) -> dict:
        """递归合并：override 优先，base 提供默认（March7th _update_config 同款）。
        用户 null 值按缺字段处理（补模板默认），防手动损坏配置崩程序。"""
        out = dict(base)
        for key, value in override.items():
            if value is None:
                continue  # 用户 null 视为缺字段，保留模板默认
            if key in out and isinstance(out[key], dict) and isinstance(value, dict):
                out[key] = Config._merge(out[key], value)
            else:
                out[key] = value
        return out

    @staticmethod
    def load():
        """读取 settings.json，与模板递归合并（用户配置优先，缺字段补默认）。
        v1.4.2：mtime 缓存——文件未变化时直接返回缓存，批量识别性能优化。
        v1.4.6（bug hunt F20）：返回缓存 dict 的【深拷贝】——调用方任意修改/嵌套修改
        都不再污染主缓存，避免一轮 `self.regions[region][prod]=...` 原地写被后续轮读到脏数据。"""
        import copy as _copy
        sf = os.path.join(get_base_dir(), 'settings.json')
        try:
            _mtime = os.path.getmtime(sf)
        except OSError:
            _mtime = -1
        _c = Config._load_cache
        if _c['mtime'] == _mtime and _c['data'] is not None:
            return _copy.deepcopy(_c['data'])
        data = {}
        _parse_fail = False
        try:
            if os.path.exists(sf):
                with open(sf, 'r', encoding='utf-8') as f:
                    data = json.load(f)
        except Exception:
            data = {}
            _parse_fail = True  # v1.4.5（bug hunt F21）：解析失败不再被模板覆盖写回
        if not isinstance(data, dict):
            data = {}
            _parse_fail = True
        # v1.4.6（fix-review C12）：损坏配置自动处置——改名 .corrupt 防下次仍读到损坏文件，
        # 并从 save 写入的 .bak 恢复上次好配置；备份也坏则回落到模板默认。
        if _parse_fail:
            try:
                if os.path.exists(sf):
                    _corrupt = sf + '.corrupt'
                    if os.path.exists(_corrupt):
                        os.remove(_corrupt)
                    os.replace(sf, _corrupt)
                if os.path.exists(sf + '.bak'):
                    with open(sf + '.bak', 'r', encoding='utf-8') as _f:
                        _bak_data = json.load(_f)
                    if isinstance(_bak_data, dict) and _bak_data:
                        data = _bak_data
                        _parse_fail = False
            except Exception:
                pass
        # 模板合并：补全缺失字段（损坏/解析失败时不写回——保留原文件供人工抢救，
        # 不再静默覆盖成模板默认，避免用户数据从主文件消失）
        tpl = Config._load_template()
        if tpl and not _parse_fail:
            merged = Config._merge(tpl, data)
            # v1.4.8 ：DPAPI 凭据加密迁移（首启静默）。
            # 仅迁移【原始用户数据】里的明文敏感字段，不动模板默认（模板里都是空串）。
            # 已加密的（dpapi:v1: 前缀）跳过；meta.dpi_v=1 标记防重复迁移。
            try:
                _migrated = Config._migrate_secrets(merged)
            except Exception:
                _migrated = False
            # 有补全（模板字段缺失）或迁移改动 → 写回自愈
            if merged != data or _migrated:
                try:
                    Config.save(merged)
                except Exception:
                    pass
            Config._load_cache['mtime'] = _mtime
            Config._load_cache['data'] = merged
            return _copy.deepcopy(merged)
        Config._load_cache['mtime'] = _mtime
        Config._load_cache['data'] = data
        return _copy.deepcopy(data)

    # ── v1.4.8 ：DPAPI 凭据加密迁移（docs/SOLUTION_tech_ .md § ）──
    @staticmethod
    def _migrate_secrets(data: dict) -> bool:
        """首启检到 api.providers.*.api_key 或 backend.password 为非空明文→静默加密覆写。
        写入 meta.dpi_v=1 防重复迁移；已加密的（dpapi:v1: 前缀）跳过。
        DPAPI 不可用时静默跳过（保留明文——确保 DPAPI 沙盒环境下程序仍可用）。

        返回 True 表示做了迁移，False 表示无需/无法迁移。
        整个函数 try/except 包裹，绝不抛——Config.load 主链必须能完成。
        """
        try:
            from dpapi_utils import enc as _dpapi_enc, is_encrypted as _is_enc, is_available as _is_avail
        except Exception:
            return False
        if not _is_avail():
            return False
        meta = data.get('meta')
        if not isinstance(meta, dict):
            meta = {}
        if meta.get('dpi_v') == 1:
            return False  # 已迁移过

        changed = False
        # 1) api.providers.*.api_key
        try:
            api = data.get('api')
            if isinstance(api, dict):
                provs = api.get('providers')
                if isinstance(provs, dict):
                    for pname, pcfg in provs.items():
                        if not isinstance(pcfg, dict):
                            continue
                        k = pcfg.get('api_key')
                        if isinstance(k, str) and k and not _is_enc(k):
                            enc_v = _dpapi_enc(k)
                            if enc_v:
                                pcfg['api_key'] = enc_v
                                changed = True
        except Exception:
            pass
        # 2) backend.password
        try:
            backend = data.get('backend')
            if isinstance(backend, dict):
                p = backend.get('password')
                if isinstance(p, str) and p and not _is_enc(p):
                    enc_v = _dpapi_enc(p)
                    if enc_v:
                        backend['password'] = enc_v
                        changed = True
        except Exception:
            pass

        if changed:
            meta['dpi_v'] = 1
            data['meta'] = meta
        return changed

    @staticmethod
    def decrypt_value(value):
        """UI 读取已加密字段的便捷包装：dpapi 前缀 → 明文；其他 → 原样；
        解密失败（跨机/损坏）→ 返回 ""（让 UI 提示用户重填，绝不抛阻塞启动）。
        """
        if not value:
            return ""
        if not isinstance(value, str):
            return value
        # 快速前缀判定：避免每个字段都 import dpapi_utils
        if not value.startswith("dpapi:v1:"):
            return value
        try:
            from dpapi_utils import dec as _dpapi_dec, DPAPIError
            return _dpapi_dec(value)
        except DPAPIError:
            # 凭据失效：返回空串，由 UI 层 messagebox 提示后置空
            return ""
        except Exception:
            return ""

    @staticmethod
    def save(data: dict):
        """原子写 settings.json + 写前 .bak 备份 + 重试（防 Windows 文件锁丢配置）"""
        import time as _time
        # v1.4.2：写后清读取缓存，下次 load 强制重读
        # v1.4.5（bug hunt F20）：保存加模块级锁——多线程并发 save（探测/批量/设置页）
        # 共用固定 .tmp 会互相覆盖/写坏；tmp 名按 pid 唯一化
        with Config._CONFIG_SAVE_LOCK:
            Config._load_cache['mtime'] = -1
            Config._load_cache['data'] = None
            sf = os.path.join(get_base_dir(), 'settings.json')
            tmp = f"{sf}.tmp{os.getpid()}"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            # 写前备份现有配置（杀毒/云同步短暂锁定时可恢复）
            try:
                if os.path.exists(sf):
                    with open(sf, 'r', encoding='utf-8') as _f:
                        _bak = _f.read()
                    with open(sf + '.bak', 'w', encoding='utf-8') as _f:
                        _f.write(_bak)
            except Exception:
                pass
            # os.replace 原子替换；Windows 上目标被短暂锁定会抛 PermissionError → 重试 3 次
            for _attempt in range(3):
                try:
                    os.replace(tmp, sf)
                    return
                except OSError:
                    if _attempt >= 2:
                        raise
                    _time.sleep(0.2)

    @staticmethod
    def get(key, default=None):
        return Config.load().get(key, default)

    @staticmethod
    def set(key, value):
        data = Config.load()
        data[key] = value
        Config.save(data)


# ── v1.4.8 -fix：运行时凭据解密───────────────────────
# ocr.py / vision.py 在每次 API 调用前从 provider dict 拿 api_key；
# 之后 settings.json 里存的是 dpapi:v1: 密文，裸拿 = 401 全军覆没。
# 这里提供一个运行时入口：明文直通 + 密文解密 + 失败降级 + 进程内 memo
# （同一进程反复调用的热点路径，避免每次都走 CryptUnprotectData）。
_decrypt_memo = {}
_DECRYPT_MEMO_MAX = 256


def decrypt_secret(value):
    """运行时凭据解密入口（ocr.py / vision.py 调用）：
    - 空 / None / 非字符串：原样返回（不抛）
    - 非 dpapi:v1: 前缀：原样返回（明文直通 / 未来 v2/v3 前缀走对应分支）
    - 解密失败（跨机/损坏）：返回 ""（调用方应按"key 为空"路径处理 — 通常已 raise RuntimeError）
    - 成功：缓存进 _decrypt_memo（最多 256 项，LRU 不严格 — dict 大小硬上限防泄漏）
    """
    if not value:
        return value if value is None else ""
    if not isinstance(value, str):
        return value
    if not value.startswith("dpapi:v1:"):
        return value
    # 命中缓存
    if value in _decrypt_memo:
        return _decrypt_memo[value]
    # 缓存上限保护：满了就清空（最坏情况每次都重解一次，不致命）
    if len(_decrypt_memo) >= _DECRYPT_MEMO_MAX:
        try:
            _decrypt_memo.clear()
        except Exception:
            pass
    try:
        from dpapi_utils import dec as _dpapi_dec, DPAPIError
        try:
            plain = _dpapi_dec(value)
        except DPAPIError:
            return ""
        except Exception:
            return ""
    except Exception:
        # 极早期 import 失败（dpapi_utils 损坏/路径错）：返回空串
        return ""
    if plain:
        try:
            _decrypt_memo[value] = plain
        except Exception:
            pass
    return plain


def get_base_dir() -> str:
    """可写数据目录：打包后 → %APPDATA%/PDD补货助手，源码 → 脚本目录"""
    if getattr(sys, 'frozen', False):
        data_dir = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'PDD补货助手')
        os.makedirs(data_dir, exist_ok=True)
        return data_dir
    return os.path.dirname(os.path.abspath(__file__))


def get_api_config() -> dict:
    """读取 settings.json 中的 API 配置，自动迁移旧格式。
    v1.4.5（bug hunt F7）：迁移判定必须基于【原始文件】——旧实现先 Config.load()
    会把模板的 api.providers 合并进旧格式 {mode,key}，导致 `'providers' in api` 恒真，
    旧 key/mode 永远不迁移（旧用户升级后 key 静默丢失、active_provider 被模板钉成 doubao）。"""
    try:
        sf = os.path.join(get_base_dir(), 'settings.json')
        _raw_api = {}
        if os.path.exists(sf):
            with open(sf, 'r', encoding='utf-8') as _f:
                _raw = json.load(_f)
                if isinstance(_raw, dict):
                    _raw_api = _raw.get('api', {}) or {}
        # 旧格式（api 直接挂 mode/key、无 providers）→ 迁移
        if _raw_api and 'providers' not in _raw_api and (_raw_api.get('key') or _raw_api.get('mode')):
            old_model = (_raw_api.get('builtin_model', '') or _raw_api.get('custom_model', '')
                         or _raw_api.get('mode', ''))
            old_key = _raw_api.get('key', '')
            if old_model.lower().startswith('doubao') or 'doubao' in old_model.lower():
                active = 'doubao'; ep = 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'
            elif old_model.startswith('qwen'):
                active = 'qwen'; ep = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
            elif old_model.startswith('glm'):
                active = 'glm'; ep = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
            else:
                active = 'doubao'; ep = 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'
            new_api = {
                'active_provider': active,
                'providers': {
                    active: {'api_key': old_key, 'model': old_model, 'endpoint': ep,
                             'model_history': [old_model] if old_model else []}
                }
            }
            _s = Config.load()
            _s['api'] = new_api
            Config.save(_s)
            return new_api
        return Config.load().get('api', {}) or {}
    except (json.JSONDecodeError, IOError, OSError):
        pass
    return {}


def capture_pdd_screenshot(output_path: str, out_window_pos: dict = None) -> bool:
    """
    锁定浏览器窗口截图 → 按设置裁剪 → 保存。
    返回 True 表示截到窗口，False 表示未找到窗口（已 fallback 全屏）。
    out_window_pos: 可选 dict，调用方传入后填充 {'left': int, 'top': int}（窗口左上角
    在全屏坐标系中的位置）。滚动/点击坐标换算时用窗口位置还原全屏偏移，
    避免窗口未最大化时坐标错位（如 1920 窗口在 4K 屏上）。
    实测耗时 ~0.7s，无需线程超时包装；窗口恢复由调用方主线程 after 负责。
    v1.4 修复：优先 PrintWindow 后台截图（借鉴 March7thAssistant screenshot.py）——
    不抢焦点、窗口被遮挡也能截到内容；失败才回退前台截图。
    """
    import os as _os, time as _time
    _os.makedirs(_os.path.dirname(output_path) or '.', exist_ok=True)

    # AI 自动定位表格后不再需要手动裁剪比例；截图全图交给 AI bbox 定位
    import pyautogui as pg
    from PIL import Image as PILImage

    found_window = False
    img = None
    win_left = win_top = 0
    try:
        import pygetwindow as gw
        # 窗口选择（v1.4 全量审查修复）
        # 1) 优先标题含「拼多多/pinduoduo」的窗口（商家后台标签激活时窗口标题带站点名）
        # 2) 没有 → 所有浏览器窗口中选「当前激活」的那个（用户刚在看的就是 PDD 页面）
        # 3) 再没有 → 第一个浏览器窗口（多窗口时可能有偏差，但比截错窗口好）
        # 旧逻辑直接 wins[0]：用户开多个 Edge/Chrome 窗口时可能截到别的网站窗口。
        def _pick_window(titles):
            for title in titles:
                wins = gw.getWindowsWithTitle(title)
                if not wins:
                    continue
                if '拼多多' in title or 'pinduoduo' in title.lower():
                    return wins[0]  # 精确站点名优先
                for w in wins:  # 浏览器窗口：优先当前激活的
                    try:
                        if w.isActive:
                            return w
                    except Exception:
                        pass
                return wins[0]
            return None
        win = _pick_window(['拼多多', 'pinduoduo', 'Microsoft Edge', 'Edge', 'Chrome', 'Firefox'])
        if win is not None:
            found_window = True
            win_left, win_top = win.left, win.top
            # 1) 优先后台截图（PrintWindow）：不抢焦点、不遮挡、窗口被盖住也能截
            try:
                img = _capture_window_background(win)
            except Exception:
                img = None
            if img is not None:
                # v1.4 修复：PrintWindow 截的是**客户区**（不含标题栏/边框），
                # 偏移必须用客户区左上角的全屏坐标（ClientToScreen）——外框坐标
                # win.left/top 含边框/标题栏，非最大化窗口会系统性偏左上，
                # 客户反馈"AI 定位后点击查询按钮偏左"即此因（本机测试窗口
                # 最大化时外框≈客户区，偏差被掩盖）。DPI 感知进程返回物理像素。
                try:
                    _co = _client_origin(win)
                    if _co:
                        win_left, win_top = _co[0], _co[1]
                except Exception:
                    pass  # 客户区坐标失败则保留外框坐标（近似，最大化时无差）
            if img is None:
                # 2) 后台失败 → 前台截图（激活窗口，pyautogui region）
                if win.isMinimized:
                    win.restore()
                win.activate()
                _time.sleep(0.2)
                img = pg.screenshot(region=(win.left, win.top, win.width, win.height))
    except Exception:
        pass

    if img is None:
        # 未找到窗口，或找到窗口但截图失败（句柄无效等）→ fallback 全屏
        img = pg.screenshot()
        win_left = win_top = 0

    # 调用方需要窗口位置（滚动坐标换算）时回传
    if isinstance(out_window_pos, dict):
        out_window_pos['left'] = int(win_left)
        out_window_pos['top'] = int(win_top)
        # 窗口原始宽高（截图缩放前），供滚动坐标按真实比例还原
        if img is not None:
            out_window_pos['width'] = int(img.size[0])
            out_window_pos['height'] = int(img.size[1])

    w, h = img.size
    cw, ch = w, h  # AI 定位表格自行 bbox，不再按比例预裁剪
    if cw > 2560:
        img = img.resize((2560, int(ch * 2560 / cw)), PILImage.LANCZOS)
    img.save(output_path)
    # 截图缩放系数：AI 返回的是保存后图上的坐标（宽 ≤2560），
    # 调用方要把坐标还原到原始窗口/全屏像素（4K/带鱼屏必须，v1.4 审查修复）
    if isinstance(out_window_pos, dict):
        _saved_w = img.size[0]
        out_window_pos['scale_x'] = (cw / _saved_w) if _saved_w else 1.0
        out_window_pos['scale_y'] = (ch / img.size[1]) if img.size[1] else 1.0
    return found_window


def _client_origin(win) -> tuple:
    """窗口客户区左上角的全屏坐标（物理像素）。

    PrintWindow 截的是客户区（不含标题栏/边框），坐标换算偏移必须用客户区
    起点而非窗口外框 win.left/top——非最大化窗口两者差一个边框/标题栏，
    用外框会导致点击系统性偏左上（v1.4 修复：客户反馈查询按钮点击偏左）。
    DPI 感知进程下返回物理像素，与 pyautogui/pygetwindow 一致。
    """
    import ctypes
    from ctypes import wintypes
    try:
        hwnd = win._hWnd if hasattr(win, '_hWnd') else None
        if not hwnd:
            return None
        user32 = ctypes.windll.user32
        pt = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            return None
        return int(pt.x), int(pt.y)
    except Exception:
        return None


def _capture_window_background(win) -> object:
    """PrintWindow 后台截图（借鉴 March7thAssistant capture_window_background）。
    纯 ctypes 实现，零 pywin32 依赖——保证增量包（仅 exe+updater）对旧 v1.4 客户可用
    （旧版 _internal 无 win32，引入 pywin32 会导致旧客户升级后 ImportError 崩溃）。
    不激活窗口、不抢焦点、窗口被其他窗口遮挡时仍能截到内容。
    返回 PIL Image；失败返回 None（调用方回退前台截图）。
    flag=3：强制完整渲染 + 只抓客户区（不含标题栏/边框）。
    """
    import ctypes
    from ctypes import wintypes
    from PIL import Image as PILImage
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    kernel32 = ctypes.windll.kernel32

    hwnd = win._hWnd if hasattr(win, '_hWnd') else None
    if not hwnd:
        return None

    # 最小化窗口无法后台截图，回退（调用方会 restore + 前台）
    if win.isMinimized:
        return None

    # 客户区尺寸（不含标题栏边框）
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None

    # 设备上下文链：窗口 DC → 兼容 DC → 位图（每资源独立释放，防异常路径泄漏 GDI）
    hwndDC = memDC = None
    hBitmap = None
    _prev_obj = None  # v1.4.5（bug hunt F12）：memDC 原选中对象，释放前还原
    try:
        hwndDC = user32.GetWindowDC(hwnd)
        if not hwndDC:
            return None
        memDC = gdi32.CreateCompatibleDC(hwndDC)
        if not memDC:
            return None
        hBitmap = gdi32.CreateCompatibleBitmap(hwndDC, width, height)
        if not hBitmap:
            return None
        # v1.4.5（bug hunt F12）：保存 SelectObject 的旧对象返回值——否则位图仍选中于
        # memDC 时先 DeleteObject 违反 MSDN（应失败，但部分环境返回"成功"造成差异，
        # 规范做法是还原后再删，杜绝 PrintWindow 每帧潜在 GDI 泄漏）
        _prev_obj = gdi32.SelectObject(memDC, hBitmap)

        # PrintWindow flag=3：强制完整渲染 + 客户区
        result = user32.PrintWindow(hwnd, memDC, 3)
        if result != 1:
            return None

        # 位图 → PIL（BGRX raw：CreateCompatibleBitmap 是 32bpp BGRA，去掉 alpha）
        # ctypes.wintypes 无 BITMAPINFO，手动定义（GDI 标准结构）
        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ('biSize', wintypes.DWORD),
                ('biWidth', wintypes.LONG),
                ('biHeight', wintypes.LONG),
                ('biPlanes', wintypes.WORD),
                ('biBitCount', wintypes.WORD),
                ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD),
                ('biXPelsPerMeter', wintypes.LONG),
                ('biYPelsPerMeter', wintypes.LONG),
                ('biClrUsed', wintypes.DWORD),
                ('biClrImportant', wintypes.DWORD),
            ]
        class BITMAPINFO(ctypes.Structure):
            _fields_ = [('bmiHeader', BITMAPINFOHEADER)]

        bmpinfo = BITMAPINFO()
        bmpinfo.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmpinfo.bmiHeader.biWidth = width
        bmpinfo.bmiHeader.biHeight = -height  # 负值 = 自上而下（顶行在前）
        bmpinfo.bmiHeader.biPlanes = 1
        bmpinfo.bmiHeader.biBitCount = 32
        bmpinfo.bmiHeader.biCompression = 0  # BI_RGB
        buf = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetDIBits(hwndDC, hBitmap, 0, height, buf,
                        ctypes.byref(bmpinfo), 0)
        img = PILImage.frombuffer('RGB', (width, height),
                                  buf.raw, 'raw', 'BGRX', 0, 1)
        return img
    except Exception:
        return None
    finally:
        # 逆序释放（v1.4.5 bug hunt F12）：先还原 memDC 旧选中对象，再删位图；最后删 DC
        if memDC and _prev_obj:
            try:
                gdi32.SelectObject(memDC, _prev_obj)
            except Exception:
                pass
        if hBitmap:
            try:
                gdi32.DeleteObject(hBitmap)
            except Exception:
                pass
        if memDC:
            try:
                gdi32.DeleteDC(memDC)
            except Exception:
                pass
        if hwndDC:
            try:
                user32.ReleaseDC(hwnd, hwndDC)
            except Exception:
                pass
