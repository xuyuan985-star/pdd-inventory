"""PDD EZ 高级补货算法单元测试（test_algorithm.py · t2 后端）。

- 不碰真实库：所有 history 用注入的假 lookup 返回构造行。
- 覆盖：季节/大促/滞销/超卖各因子单独命中与未命中、组合、异常回退、
        与 classic 输出结构兼容、删除历史行缺键容忍。
- 验收：python -m unittest test_algorithm + test_smoke 全绿。
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _build_history(daily_sales: float, days: int, today: datetime = None,
                   start_offset: int = 0) -> list:
    """构造近 N 日每天 daily_sales 销量的 history_rows（含 captured_at / sales）。"""
    if today is None:
        today = datetime.now()
    rows = []
    for i in range(start_offset, start_offset + days):
        d = (today - timedelta(days=i)).strftime('%Y-%m-%d')
        rows.append({'captured_at': d, 'sales': daily_sales, 'name': 'X'})
    return rows


def _build_varied_history(weekly_pattern: list, today: datetime = None) -> list:
    """按周构造销量（每周销量可不同，用于季节系数测试）。"""
    if today is None:
        today = datetime.now()
    rows = []
    days_per_week = 7
    total_days = days_per_week * len(weekly_pattern)
    # 把数据按近到远填入：weekly_pattern[0] = 最近一周
    for wi, weekly in enumerate(weekly_pattern):
        # 该周内每天均匀分布
        per_day = weekly / days_per_week
        for di in range(days_per_week):
            d = (today - timedelta(days=wi * 7 + di)).strftime('%Y-%m-%d')
            rows.append({'captured_at': d, 'sales': per_day, 'name': 'X'})
    return rows


def _cfg_with_factors(**flags) -> dict:
    """构造一个 cfg 字典，覆盖 _default_advanced_cfg 中的 enabled 状态。

    flags: promo=bool, slow=bool, season=bool, oversell=bool
           可选 promo_dates (list), boost, lead_days, threshold_per_day,
                  stock_ratio, high_ratio
    """
    from utils import _default_advanced_cfg
    adv = _default_advanced_cfg()
    if flags.get('promo'):
        adv['promo']['enabled'] = True
        if 'promo_dates' in flags:
            adv['promo']['dates'] = list(flags['promo_dates'])
        if 'boost' in flags:
            adv['promo']['boost'] = float(flags['boost'])
        if 'lead_days' in flags:
            adv['promo']['lead_days'] = int(flags['lead_days'])
    if flags.get('slow'):
        adv['slow']['enabled'] = True
        if 'threshold_per_day' in flags:
            adv['slow']['threshold_per_day'] = float(flags['threshold_per_day'])
        if 'stock_ratio' in flags:
            adv['slow']['stock_ratio'] = float(flags['stock_ratio'])
    if flags.get('season'):
        adv['season']['enabled'] = True
    if flags.get('oversell'):
        adv['oversell']['enabled'] = True
        if 'high_ratio' in flags:
            adv['oversell']['high_ratio'] = float(flags['high_ratio'])
    return {
        'model': 'advanced',
        'safety_days': flags.get('safety_days', 2),
        'in_transit_qty': flags.get('in_transit_qty', 0),
        'advanced': adv,
    }


class TestCalcReplenishmentAdvancedBasics(unittest.TestCase):
    """输出字段与 classic/weighted 同构 + 附加高级字段"""

    def setUp(self):
        from utils import calc_replenishment_advanced
        self.calc = calc_replenishment_advanced

    def test_output_has_base_fields(self):
        """与 classic/weighted 同构字段：status/color/qty/ratio/reorder/daily/stock/model"""
        rows = _build_history(10, 30)
        def hlookup(*a, **kw): return rows
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, _cfg_with_factors())
        # 基础字段
        for k in ('status', 'color', 'qty', 'ratio', 'reorder', 'daily', 'stock', 'model'):
            self.assertIn(k, plan, f'缺基础字段 {k}')
        self.assertEqual(plan['model'], 'advanced')
        self.assertEqual(plan['stock'], 100)
        # 颜色：reorder <= 0 → red；<= 2 → yellow；> 2 → green
        # ratio=100/10=10, lead_time=4, reorder=6 → green
        self.assertEqual(plan['color'], 'green')
        self.assertEqual(plan['qty'], 0)

    def test_output_has_advanced_fields(self):
        """附加字段：season_factor / promo_multiplier / effective_daily /
        slow_moving / oversell_risk / oversell_level"""
        rows = _build_history(10, 30)
        def hlookup(*a, **kw): return rows
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, _cfg_with_factors())
        for k in ('season_factor', 'promo_multiplier', 'effective_daily',
                  'slow_moving', 'oversell_risk', 'oversell_level'):
            self.assertIn(k, plan, f'缺高级字段 {k}')
        # 默认全部关闭时
        self.assertEqual(plan['season_factor'], 1.0)
        self.assertEqual(plan['promo_multiplier'], 1.0)
        self.assertEqual(plan['slow_moving'], False)
        self.assertEqual(plan['oversell_risk'], False)
        self.assertIsNone(plan['oversell_level'])

    def test_no_history_uses_raw_daily(self):
        """无历史时 season_factor=1.0, promo=1.0, effective_daily=raw daily"""
        def hlookup(*a, **kw): return []
        item = {'name': 'X', 'stock': 0, 'sales': 8, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, _cfg_with_factors(season=True))
        self.assertEqual(plan['season_factor'], 1.0)
        self.assertEqual(plan['effective_daily'], 8.0)

    def test_qty_zero_sales_observation(self):
        """日销 0 → '无销量·观察' 灰 + qty=0（沿用经典兜底语义）"""
        def hlookup(*a, **kw): return []
        item = {'name': 'X', 'stock': 100, 'sales': 0, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, _cfg_with_factors())
        self.assertEqual(plan['status'], '无销量·观察')
        self.assertEqual(plan['color'], 'gray')
        self.assertEqual(plan['qty'], 0)
        self.assertEqual(plan['model'], 'advanced')

    def test_color_thresholds_match_weighted(self):
        """颜色三档阈值与 weighted 一致：reorder <= 0 红 / <= 2 黄 / > 2 绿"""
        rows = _build_history(10, 30)
        def hlookup(*a, **kw): return rows
        # red: stock=0, daily=10, lead=4 → reorder=-4
        p_red = self.calc({'name':'X','stock':0,'sales':10,'sku_id':'S1'},
                          '广东', 2, 2, 0, hlookup, _cfg_with_factors())
        self.assertEqual(p_red['color'], 'red')
        self.assertEqual(p_red['status'], '立刻补货')
        # yellow: stock=10, daily=10, lead=4 → ratio=1, reorder=-3 → 还是红
        # yellow: stock=20, daily=10, lead=4 → ratio=2, reorder=-2 → 红
        # yellow: stock=30, daily=10, lead=4 → ratio=3, reorder=-1 → 红
        # yellow: stock=50, daily=10, lead=4 → ratio=5, reorder=1 → yellow
        p_y = self.calc({'name':'X','stock':50,'sales':10,'sku_id':'S1'},
                        '广东', 2, 2, 0, hlookup, _cfg_with_factors())
        self.assertEqual(p_y['color'], 'yellow')
        # green: stock=100, daily=10, lead=4 → ratio=10, reorder=6 → green
        p_g = self.calc({'name':'X','stock':100,'sales':10,'sku_id':'S1'},
                        '广东', 2, 2, 0, hlookup, _cfg_with_factors())
        self.assertEqual(p_g['color'], 'green')

    def test_qty_rounded_to_100(self):
        """qty 100 取整（沿用 weighted 语义）"""
        rows = _build_history(5, 30)
        def hlookup(*a, **kw): return rows
        # daily=5, lead=2+2=4, required=20, in_transit=0, stock=0 → qty_raw=20 → ceil(100)=100
        plan = self.calc({'name':'X','stock':0,'sales':5,'sku_id':'S1'},
                         '广东', 2, 2, 0, hlookup, _cfg_with_factors())
        self.assertEqual(plan['qty'], 100)
        # daily=5, lead=2+2=4, required=20, in_transit=0, stock=-50 → qty_raw=70 → 100
        plan2 = self.calc({'name':'X','stock':-50,'sales':5,'sku_id':'S1'},
                          '广东', 2, 2, 0, hlookup, _cfg_with_factors())
        self.assertEqual(plan2['qty'], 100)

    def test_qty_zero_when_green(self):
        """绿档：reorder > 2 → qty=0"""
        rows = _build_history(5, 30)
        def hlookup(*a, **kw): return rows
        plan = self.calc({'name':'X','stock':1000,'sales':5,'sku_id':'S1'},
                         '广东', 2, 2, 0, hlookup, _cfg_with_factors())
        self.assertEqual(plan['color'], 'green')
        self.assertEqual(plan['qty'], 0)


class TestSeasonFactor(unittest.TestCase):
    """季节系数：近 4 周 vs 近 12 周 均值比，钳制 [0.5, 2.0]"""

    def setUp(self):
        from utils import calc_replenishment_advanced, _season_factor
        self.calc = calc_replenishment_advanced
        self._season_factor = _season_factor

    def test_no_history_returns_one(self):
        self.assertEqual(self._season_factor([]), 1.0)
        self.assertEqual(self._season_factor(None), 1.0)
        # 行数不够 12 周（按周聚合）→ 用现有周数比 mean12 仍为自身 → 1.0
        rows = _build_history(10, 3)  # 3 天 = 1 周内
        self.assertEqual(self._season_factor(rows), 1.0)

    def test_factor_one_when_flat(self):
        """销量平稳 → factor ≈ 1.0"""
        # 12 周每周 70 件 → 4 周均值 70 / 12 周均值 70 = 1.0
        rows = _build_varied_history([70] * 12)
        self.assertAlmostEqual(self._season_factor(rows), 1.0, delta=0.05)

    def test_factor_above_one_recent_spike(self):
        """近 4 周翻倍 → factor ≈ 2.0（钳制到 2.0）"""
        # 12 周内 8 周每周 50，近 4 周每周 100 → ratio = 100/((4*100+8*50)/12) = 100/66.67 = 1.5
        rows = _build_varied_history([100, 100, 100, 100] + [50] * 8)
        f = self._season_factor(rows)
        self.assertGreater(f, 1.2)
        self.assertLessEqual(f, 2.0)

    def test_factor_clamped_high(self):
        """极端 spike → 钳制到 2.0"""
        # 近 4 周 1000，远 8 周 10 → ratio = 1000/((4*1000+8*10)/12) = 1000/340 = 2.94 → 钳 2.0
        rows = _build_varied_history([1000] * 4 + [10] * 8)
        self.assertEqual(self._season_factor(rows), 2.0)

    def test_factor_clamped_low(self):
        """近 4 周极低 → 钳制到 0.5"""
        # 近 4 周 10，远 8 周 1000 → ratio = 10/((4*10+8*1000)/12) = 10/670 ≈ 0.015 → 钳 0.5
        rows = _build_varied_history([10] * 4 + [1000] * 8)
        self.assertEqual(self._season_factor(rows), 0.5)

    def test_factor_in_plan_when_enabled(self):
        """enabled=True + 有历史 → plan.season_factor 反映真实比值 + effective_daily 同步放大"""
        rows = _build_varied_history([100] * 4 + [50] * 8)  # ratio ≈ 1.5
        def hlookup(*a, **kw): return rows
        item = {'name': 'X', 'stock': 200, 'sales': 70, 'sku_id': 'S1'}
        plan_with = self.calc(item, '广东', 2, 2, 0, hlookup, _cfg_with_factors(season=True))
        plan_off = self.calc(item, '广东', 2, 2, 0, hlookup, _cfg_with_factors())
        self.assertGreater(plan_with['season_factor'], 1.2)
        self.assertLessEqual(plan_with['season_factor'], 2.0)
        # 开启季节因子 vs 关闭季节因子：effective_daily 比例 ≈ season_factor
        self.assertAlmostEqual(plan_with['effective_daily'] / plan_off['effective_daily'],
                               plan_with['season_factor'], delta=0.05)

    def test_factor_disabled_keeps_one(self):
        """enabled=False → factor=1.0 即使有历史"""
        rows = _build_varied_history([100] * 4 + [50] * 8)
        def hlookup(*a, **kw): return rows
        item = {'name': 'X', 'stock': 200, 'sales': 70, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, _cfg_with_factors())
        self.assertEqual(plan['season_factor'], 1.0)


class TestPromoFactor(unittest.TestCase):
    """大促倍数：命中 cfg.promo.dates 区间 → boost，否则 1.0"""

    def setUp(self):
        from utils import calc_replenishment_advanced, _promo_multiplier
        self.calc = calc_replenishment_advanced
        self._promo = _promo_multiplier

    def test_disabled_returns_one(self):
        cfg = _cfg_with_factors()  # promo not enabled
        self.assertEqual(self._promo(cfg['advanced']['promo']), 1.0)

    def test_no_dates_returns_one(self):
        from utils import _default_advanced_cfg
        adv = _default_advanced_cfg()
        adv['promo']['enabled'] = True
        # dates=[]
        self.assertEqual(self._promo(adv['promo']), 1.0)

    def test_hit_today_with_default_lead(self):
        """今天命中大促日期 + 默认 lead_days=3 → boost"""
        today = datetime.now().strftime('%Y-%m-%d')
        cfg = _cfg_with_factors(promo=True, promo_dates=[today], boost=1.8, lead_days=3)
        self.assertEqual(self._promo(cfg['advanced']['promo']), 1.8)

    def test_hit_within_lead_window(self):
        """今天距大促日 2 天，lead=3 → 命中"""
        target = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
        cfg = _cfg_with_factors(promo=True, promo_dates=[target], boost=2.0, lead_days=3)
        self.assertEqual(self._promo(cfg['advanced']['promo']), 2.0)

    def test_miss_outside_lead_window(self):
        """今天距大促日 10 天，lead=3 → 未命中"""
        target = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
        cfg = _cfg_with_factors(promo=True, promo_dates=[target], boost=2.0, lead_days=3)
        self.assertEqual(self._promo(cfg['advanced']['promo']), 1.0)

    def test_boost_below_one_keeps_one(self):
        """boost <= 1 → 不放大（返 1.0）"""
        today = datetime.now().strftime('%Y-%m-%d')
        cfg = _cfg_with_factors(promo=True, promo_dates=[today], boost=0.5, lead_days=3)
        self.assertEqual(self._promo(cfg['advanced']['promo']), 1.0)

    def test_malformed_dates_ignored(self):
        """畸形日期字符串 → 忽略（不抛）"""
        cfg = _cfg_with_factors(promo=True, promo_dates=['not-a-date', '2024-13-99', 'x'],
                                boost=2.0, lead_days=3)
        # 全部忽略 → 1.0
        self.assertEqual(self._promo(cfg['advanced']['promo']), 1.0)

    def test_promo_in_plan_amplifies_daily(self):
        """命中大促 → effective_daily 放大"""
        today = datetime.now().strftime('%Y-%m-%d')
        cfg = _cfg_with_factors(promo=True, promo_dates=[today], boost=2.0, lead_days=3)
        def hlookup(*a, **kw): return []
        item = {'name': 'X', 'stock': 0, 'sales': 5, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertEqual(plan['promo_multiplier'], 2.0)
        self.assertEqual(plan['effective_daily'], 10.0)


class TestSlowMovingFactor(unittest.TestCase):
    """滞销判定：近14日均销 < threshold AND stock/daily > stock_ratio"""

    def setUp(self):
        from utils import calc_replenishment_advanced
        self.calc = calc_replenishment_advanced

    def test_slow_moving_true(self):
        """近14日均销 0.5（< 1.0 阈值）+ stock=100，daily=10 → stock/daily=10 > 5.0 → True"""
        rows = _build_history(0.5, 30)
        def hlookup(*a, **kw): return rows
        cfg = _cfg_with_factors(slow=True, threshold_per_day=1.0, stock_ratio=5.0)
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertTrue(plan['slow_moving'])

    def test_slow_moving_false_avg_above(self):
        """近14日均销 5.0（>= 1.0 阈值）→ False"""
        rows = _build_history(5.0, 30)
        def hlookup(*a, **kw): return rows
        cfg = _cfg_with_factors(slow=True, threshold_per_day=1.0, stock_ratio=5.0)
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertFalse(plan['slow_moving'])

    def test_slow_moving_false_ratio_below(self):
        """均销低但 stock/daily 不够大 → False"""
        rows = _build_history(0.5, 30)
        def hlookup(*a, **kw): return rows
        cfg = _cfg_with_factors(slow=True, threshold_per_day=1.0, stock_ratio=5.0)
        # daily=10, stock=20, ratio=2 < 5
        item = {'name': 'X', 'stock': 20, 'sales': 10, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertFalse(plan['slow_moving'])

    def test_slow_moving_no_history(self):
        """无历史 → False（不抛）"""
        def hlookup(*a, **kw): return []
        cfg = _cfg_with_factors(slow=True, threshold_per_day=1.0, stock_ratio=5.0)
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertFalse(plan['slow_moving'])

    def test_slow_moving_disabled(self):
        """enabled=False → False 即使命中条件"""
        rows = _build_history(0.5, 30)
        def hlookup(*a, **kw): return rows
        cfg = _cfg_with_factors()  # slow not enabled
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertFalse(plan['slow_moving'])


class TestOversellFactor(unittest.TestCase):
    """超卖判定：stock < required → risk；level 按 high_ratio 切档"""

    def setUp(self):
        from utils import calc_replenishment_advanced
        self.calc = calc_replenishment_advanced

    def test_oversell_high(self):
        """stock 极低 → level='high'"""
        def hlookup(*a, **kw): return []
        cfg = _cfg_with_factors(oversell=True, high_ratio=0.5)
        # daily=10, lead=2+2=4, required=40, stock=10 < 40*0.5=20 → high
        item = {'name': 'X', 'stock': 10, 'sales': 10, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertTrue(plan['oversell_risk'])
        self.assertEqual(plan['oversell_level'], 'high')

    def test_oversell_medium(self):
        """stock 介于 [high_ratio*req, req) → level='medium'"""
        def hlookup(*a, **kw): return []
        cfg = _cfg_with_factors(oversell=True, high_ratio=0.5)
        # required=40, stock=25, high_ratio*req=20 → 25 >= 20 且 < 40 → medium
        item = {'name': 'X', 'stock': 25, 'sales': 10, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertTrue(plan['oversell_risk'])
        self.assertEqual(plan['oversell_level'], 'medium')

    def test_oversell_no_risk(self):
        """stock >= required → risk=False, level=None"""
        def hlookup(*a, **kw): return []
        cfg = _cfg_with_factors(oversell=True, high_ratio=0.5)
        # required=40, stock=50 → 不超卖
        item = {'name': 'X', 'stock': 50, 'sales': 10, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertFalse(plan['oversell_risk'])
        self.assertIsNone(plan['oversell_level'])

    def test_oversell_disabled(self):
        """enabled=False → 不判定"""
        def hlookup(*a, **kw): return []
        cfg = _cfg_with_factors()  # oversell not enabled
        item = {'name': 'X', 'stock': 1, 'sales': 10, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertFalse(plan['oversell_risk'])
        self.assertIsNone(plan['oversell_level'])

    def test_oversell_with_promo_amplifies(self):
        """大促放大日销 → required 变大 → 更容易超卖"""
        def hlookup(*a, **kw): return []
        today = datetime.now().strftime('%Y-%m-%d')
        cfg = _cfg_with_factors(promo=True, promo_dates=[today], boost=3.0,
                                 oversell=True, high_ratio=0.5)
        # daily_raw=10, promo=3 → eff=30, required=30*4=120
        # stock=50 < 60 (=120*0.5) → high
        item = {'name': 'X', 'stock': 50, 'sales': 10, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertTrue(plan['oversell_risk'])
        self.assertEqual(plan['oversell_level'], 'high')


class TestCombinedFactors(unittest.TestCase):
    """组合场景：多因子叠加 + 公式细节"""

    def setUp(self):
        from utils import calc_replenishment_advanced
        self.calc = calc_replenishment_advanced

    def test_all_factors_combined(self):
        """season + promo + slow + oversell 全部启用，构造能同时命中的历史"""
        # 近 4 周 100 / 远 8 周 50 → season ≈ 1.5
        rows = _build_varied_history([100] * 4 + [50] * 8)
        # 但近 14 日均销 = 100/7 ≈ 14.3 > 1.0 → slow_moving=False
        # stock=200, daily_raw=10 → 滞销条件（avg14<1 且 stock/daily>5）不满足
        today = datetime.now().strftime('%Y-%m-%d')
        cfg = _cfg_with_factors(season=True, promo=True, promo_dates=[today],
                                 boost=2.0, lead_days=3,
                                 slow=True, threshold_per_day=1.0, stock_ratio=5.0,
                                 oversell=True, high_ratio=0.5)
        def hlookup(*a, **kw): return rows
        # daily_raw=10, season=1.5, promo=2.0 → effective=30
        # lead=2+2=4, required=120
        # stock=200 >= 120 → 不超卖
        # stock/daily=200/10=20 > 5 但 avg14=14.3 > 1 → slow=False
        item = {'name': 'X', 'stock': 200, 'sales': 10, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertEqual(plan['model'], 'advanced')
        self.assertGreater(plan['season_factor'], 1.2)
        self.assertEqual(plan['promo_multiplier'], 2.0)
        self.assertEqual(plan['effective_daily'], 30.0)
        self.assertFalse(plan['slow_moving'])
        self.assertFalse(plan['oversell_risk'])

    def test_qty_uses_effective_daily(self):
        """qty 用 effective_daily 计算（不是 raw daily）"""
        def hlookup(*a, **kw): return _build_varied_history([100] * 4 + [50] * 8)
        today = datetime.now().strftime('%Y-%m-%d')
        cfg_on = _cfg_with_factors(season=True, promo=True, promo_dates=[today],
                                    boost=2.0, lead_days=3)
        # daily_raw=10, season≈1.5, promo=2.0 → eff≈30
        # stock=0, lead=4 → required≈120, qty_raw≈120 → 100 取整 200
        item = {'name': 'X', 'stock': 0, 'sales': 10, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, cfg_on)
        self.assertGreaterEqual(plan['qty'], 100)
        # 对比：关闭季节/大促时 effective_daily=10, required=40, qty=100
        cfg_off = _cfg_with_factors()
        plan2 = self.calc(item, '广东', 2, 2, 0, hlookup, cfg_off)
        self.assertGreater(plan['qty'], plan2['qty'])
        # 开启路径 effective_daily 也应该更大
        self.assertGreater(plan['effective_daily'], plan2['effective_daily'])


class TestAdvancedFallback(unittest.TestCase):
    """异常 → 回退经典 + model='classic(error)'"""

    def setUp(self):
        from utils import calc_replenishment_advanced
        self.calc = calc_replenishment_advanced

    def test_history_lookup_exception_falls_back(self):
        """history_lookup 抛异常 → 经典兜底 + 标注 'classic(error)'"""
        def hlookup(*a, **kw):
            raise RuntimeError('db corrupt')
        cfg = _cfg_with_factors(season=True, promo=True)
        item = {'name': 'X', 'stock': 0, 'sales': 10, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertEqual(plan['model'], 'classic(error)')
        self.assertEqual(plan['status'], '立刻补货')

    def test_cfg_none_uses_default(self):
        """cfg=None → 用 get_replenishment_cfg() 默认（不抛）"""
        from utils import calc_replenishment_advanced as _adv
        def hlookup(*a, **kw): return _build_history(10, 30)
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = _adv(item, '广东', 2, 2, 0, hlookup, None)
        # 不抛即可；季节/大促默认 enabled=False → factor=1.0
        self.assertEqual(plan['model'], 'advanced')

    def test_malformed_history_row_tolerated(self):
        """history 行缺键 / 坏 captured_at / 坏 sales → 全部容错（不抛、不污染）"""
        def hlookup(*a, **kw):
            return [
                {'captured_at': '2024-99-99', 'sales': 'oops'},  # 坏日期 + 坏 sales
                {'captured_at': '', 'sales': None},  # 缺键
                {'sales': 10},  # 缺 captured_at
                'not a dict',  # 非 dict
                None,  # None
                {'captured_at': datetime.now().strftime('%Y-%m-%d'), 'sales': 5},
            ]
        cfg = _cfg_with_factors(season=True, slow=True)
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        # 不抛
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertIn(plan['model'], ('advanced', 'classic(error)'))
        self.assertIsInstance(plan['status'], str)

    def test_history_lookup_returns_none(self):
        """history_lookup 返 None（不是 []）→ 视为空，不抛"""
        def hlookup(*a, **kw): return None
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, _cfg_with_factors())
        self.assertEqual(plan['model'], 'advanced')

    def test_history_lookup_returns_non_list(self):
        """history_lookup 返非 list → 视为空"""
        def hlookup(*a, **kw): return {'weird': 'shape'}
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.calc(item, '广东', 2, 2, 0, hlookup, _cfg_with_factors())
        self.assertEqual(plan['model'], 'advanced')

    def test_calc_replenishment_dispatch_exception_falls_back(self):
        """calc_replenishment 入口：model='advanced' + history 抛异常 → classic(error)"""
        from utils import calc_replenishment, MODEL_ADVANCED
        def hlookup(*a, **kw):
            raise RuntimeError('kaboom')
        def sl(item, reg): return 1
        items = [{'name': 'A', 'stock': 0, 'sales': 10, 'sku_id': 'S1'}]
        plans = calc_replenishment(items, '广东', MODEL_ADVANCED, 2, 0, sl, hlookup)
        self.assertEqual(len(plans), 1)
        # 高级函数异常 → 入口 except 兜底经典
        self.assertIn(plans[0]['model'], ('classic(error)', 'classic'))

    def test_calc_replenishment_dispatch_normal(self):
        """calc_replenishment 入口：model='advanced' + 正常历史 → advanced"""
        from utils import calc_replenishment, MODEL_ADVANCED
        rows = _build_history(10, 30)
        def hlookup(sku, reg, days, name=None): return rows
        def sl(item, reg): return 2
        items = [{'name': 'A', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}]
        plans = calc_replenishment(items, '广东', MODEL_ADVANCED, 2, 0, sl, hlookup)
        self.assertEqual(plans[0]['model'], 'advanced')

    def test_calc_replenishment_dispatch_with_cfg_param(self):
        """calc_replenishment 入口：cfg 形参显式传入 → 生效"""
        from utils import calc_replenishment, MODEL_ADVANCED
        today = datetime.now().strftime('%Y-%m-%d')
        cfg = _cfg_with_factors(promo=True, promo_dates=[today], boost=2.5)
        def hlookup(*a, **kw): return []
        def sl(item, reg): return 1
        items = [{'name': 'A', 'stock': 0, 'sales': 10, 'sku_id': 'S1'}]
        plans = calc_replenishment(items, '广东', MODEL_ADVANCED, 2, 0, sl, hlookup, cfg=cfg)
        self.assertEqual(plans[0]['model'], 'advanced')
        self.assertEqual(plans[0]['promo_multiplier'], 2.5)

    def test_calc_replenishment_backward_compat_no_cfg(self):
        """calc_replenishment 不传 cfg → 仍可用（向后兼容）"""
        from utils import calc_replenishment, MODEL_ADVANCED
        def hlookup(*a, **kw): return _build_history(10, 30)
        def sl(item, reg): return 1
        items = [{'name': 'A', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}]
        # 不传 cfg= → 旧调用方照常工作
        plans = calc_replenishment(items, '广东', MODEL_ADVANCED, 2, 0, sl, hlookup)
        self.assertEqual(plans[0]['model'], 'advanced')


class TestClassicCompat(unittest.TestCase):
    """与 classic 输出结构兼容"""

    def setUp(self):
        from utils import calc_replenishment_classic, calc_replenishment_advanced
        self.classic = calc_replenishment_classic
        self.adv = calc_replenishment_advanced

    def test_advanced_with_all_factors_off_matches_classic(self):
        """所有高级因子关闭 + 无历史 → advanced 输出结构与 classic 完全一致（同 qty/ratio/reorder/color）"""
        # classic 路径（无 advanced 因子）
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        # classic：shipping=2, offset=1
        c = self.classic(item, '广东', 2, 1)
        # advanced：shipping=2, safety=1（用 offset 等价的 safety_days）
        # daily=10, lead=2+1=3, ratio=10, reorder=7 → green
        rows = _build_history(10, 30)
        def hlookup(*a, **kw): return rows
        a = self.adv(item, '广东', 2, 1, 0, hlookup, _cfg_with_factors(safety_days=1))
        # 关键字段相等
        for k in ('status', 'color', 'qty', 'ratio', 'daily', 'stock'):
            self.assertEqual(c[k], a[k], f'{k} 不一致: classic={c[k]} vs advanced={a[k]}')
        # model 不同（一个是 classic，一个是 advanced）
        self.assertEqual(c['model'], 'classic')
        self.assertEqual(a['model'], 'advanced')
        # advanced 多了附加字段
        for k in ('season_factor', 'promo_multiplier', 'effective_daily',
                  'slow_moving', 'oversell_risk', 'oversell_level'):
            self.assertIn(k, a)

    def test_classic_formula_not_modified(self):
        """回归：calc_replenishment_classic 公式输出不变（与 t13 铁律一致）"""
        # stock=0, sales=10, shipping=1, offset=1 → 立刻补货, qty=100
        p = self.classic({'name': 'X', 'stock': 0, 'sales': 10}, '广东', 1, 1)
        self.assertEqual(p['status'], '立刻补货')
        self.assertEqual(p['color'], 'red')
        self.assertEqual(p['qty'], 100)
        self.assertEqual(p['model'], 'classic')
        # stock=200, sales=10 → green, qty=0
        p2 = self.classic({'name': 'X', 'stock': 200, 'sales': 10}, '广东', 1, 1)
        self.assertEqual(p2['color'], 'green')
        self.assertEqual(p2['qty'], 0)


class TestReplenishmentCfgAdvanced(unittest.TestCase):
    """get_replenishment_cfg 扩展：advanced 子配置 + 缺字段兜底"""

    def test_default_advanced_disabled(self):
        from utils import get_replenishment_cfg
        cfg = get_replenishment_cfg()
        self.assertIn('advanced', cfg)
        for sub in ('promo', 'slow', 'season', 'oversell'):
            self.assertIn(sub, cfg['advanced'])
            self.assertFalse(cfg['advanced'][sub].get('enabled', True),
                             f'{sub} 默认应 enabled=False')

    def test_advanced_model_in_valid_set(self):
        from utils import get_replenishment_cfg, MODEL_ADVANCED
        import json, tempfile, os, shutil
        from utils import Config
        orig = Config.load() if hasattr(Config, 'load') else {}
        orig_base = None
        try:
            import utils
            orig_base = utils.get_base_dir
            tmp = tempfile.mkdtemp()
            sf = os.path.join(tmp, 'settings.json')
            with open(sf, 'w', encoding='utf-8') as f:
                json.dump({'replenishment': {'model': 'advanced'}}, f, ensure_ascii=False)
            utils.get_base_dir = lambda: tmp
            Config._load_cache = {'mtime': -1, 'data': None}
            Config._template_cache = None
            cfg = get_replenishment_cfg()
            self.assertEqual(cfg['model'], 'advanced')
        finally:
            # 恢复 get_base_dir 并清缓存，避免污染后续 test_smoke 用例
            try:
                if orig_base is not None:
                    import utils
                    utils.get_base_dir = orig_base
                Config._load_cache = {'mtime': -1, 'data': None}
                Config._template_cache = None
            except Exception:
                pass
            try:
                if orig:
                    Config.save(orig)
            except Exception:
                pass
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass

    def test_advanced_promo_dates_cleaned(self):
        """promo.dates 自动清洗：只保留 YYYY-MM-DD"""
        from utils import _merge_advanced_cfg
        raw = {'promo': {'dates': ['2024-01-01', 'bad', '2024-13-99', 123, '2024-12-31', None, '2024-06-15']}}
        cfg = _merge_advanced_cfg(raw)
        self.assertIn('2024-01-01', cfg['promo']['dates'])
        self.assertIn('2024-12-31', cfg['promo']['dates'])
        self.assertIn('2024-06-15', cfg['promo']['dates'])
        # 坏的、None、123 等应被剔除
        self.assertEqual(len(cfg['promo']['dates']), 3)

    def test_advanced_promo_auto_enable_when_dates_present(self):
        """promo 没显式 enabled 但有 dates → 自动 enabled=True"""
        from utils import _merge_advanced_cfg
        raw = {'promo': {'dates': ['2024-01-01']}}
        cfg = _merge_advanced_cfg(raw)
        self.assertTrue(cfg['promo']['enabled'])

    def test_advanced_missing_subnode_stays_default(self):
        """只填 slow → promo/season/oversell 仍为默认"""
        from utils import _merge_advanced_cfg
        raw = {'slow': {'enabled': True, 'threshold_per_day': 2.0}}
        cfg = _merge_advanced_cfg(raw)
        self.assertTrue(cfg['slow']['enabled'])
        self.assertEqual(cfg['slow']['threshold_per_day'], 2.0)
        self.assertFalse(cfg['promo']['enabled'])
        self.assertFalse(cfg['season']['enabled'])
        self.assertFalse(cfg['oversell']['enabled'])

    def test_advanced_empty_dict_uses_defaults(self):
        from utils import _merge_advanced_cfg
        cfg = _merge_advanced_cfg({})
        self.assertFalse(cfg['promo']['enabled'])
        self.assertEqual(cfg['promo']['boost'], 1.5)
        self.assertEqual(cfg['promo']['lead_days'], 3)

    def test_advanced_none_input_uses_defaults(self):
        from utils import _merge_advanced_cfg
        cfg = _merge_advanced_cfg(None)
        self.assertFalse(cfg['promo']['enabled'])

    def test_advanced_malformed_boost_falls_back(self):
        """promo.boost 解析失败 → 保留默认"""
        from utils import _merge_advanced_cfg
        raw = {'promo': {'enabled': True, 'dates': ['2024-01-01'], 'boost': 'oops'}}
        cfg = _merge_advanced_cfg(raw)
        self.assertEqual(cfg['promo']['boost'], 1.5)  # 保留默认


if __name__ == '__main__':
    unittest.main()
