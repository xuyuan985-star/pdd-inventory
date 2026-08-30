"""PDD EZ 高级算法-UI 集成测试（test_algorithm_ui.py · t8）

- 不依赖 Tk：所有可纯函数化的逻辑都在 algorithm_ui.py，可单测
- 覆盖：
  * 模型分发（classic / weighted / advanced / 异常回退）
  * cfg 缺省/None/缺 advanced 节点兜底
  * 预警标签生成（4 类单选/组合/空）
  * 大促日期解析（合法/坏日期/混合分隔/None）
  * UI 表单 → cfg 存储形态
  * plan 字段补齐（高级字段缺省填充）
  * 与 t2 契约：calc_replenishment_advanced 字段同构 + 附加字段全补齐
- 验收：python -m unittest test_algorithm_ui + test_algorithm + test_smoke 全绿
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _build_history(daily_sales, days, today=None, start_offset=0):
    """构造近 N 日 daily_sales 销量的 history_rows。"""
    if today is None:
        today = datetime.now()
    rows = []
    for i in range(start_offset, start_offset + days):
        d = (today - timedelta(days=i)).strftime('%Y-%m-%d')
        rows.append({'captured_at': d, 'sales': daily_sales, 'name': 'X'})
    return rows


class TestDispatchPlanBasic(unittest.TestCase):
    """dispatch_plan：模型分发正确性 + cfg 兜底 + 异常回退"""

    def setUp(self):
        from algorithm_ui import dispatch_plan
        self.dispatch = dispatch_plan

    def test_classic_dispatch(self):
        """model='classic' → 走经典公式 + 高级字段填充占位"""
        cfg = {'model': 'classic', 'safety_days': 2, 'in_transit_qty': 0}
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.dispatch(item, '广东', 2, cfg, None)
        self.assertEqual(plan['model'], 'classic')
        # 高级字段必须有（占位）
        for k in ('season_factor', 'promo_multiplier', 'effective_daily',
                  'slow_moving', 'oversell_risk', 'oversell_level', 'warning'):
            self.assertIn(k, plan)
        self.assertEqual(plan['season_factor'], 1.0)
        self.assertEqual(plan['promo_multiplier'], 1.0)
        self.assertEqual(plan['slow_moving'], False)
        self.assertEqual(plan['oversell_risk'], False)
        self.assertIsNone(plan['oversell_level'])
        # 经典模式无低置信 → warning=''
        self.assertEqual(plan['warning'], '')

    def test_weighted_dispatch(self):
        """model='weighted' + 有历史 → 走加权；无历史 → 回退经典(no_history)"""
        rows = _build_history(10, 30)
        def hlookup(sku, reg, days, name=None): return rows
        cfg = {'model': 'weighted', 'safety_days': 2, 'in_transit_qty': 0}
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.dispatch(item, '广东', 2, cfg, hlookup)
        self.assertEqual(plan['model'], 'weighted')

    def test_weighted_dispatch_no_history_fallback(self):
        """weighted 无历史 → 经典(no_history) 兜底 + warning 字段补齐"""
        def hlookup(*a, **kw): return []
        cfg = {'model': 'weighted', 'safety_days': 2, 'in_transit_qty': 0}
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.dispatch(item, '广东', 2, cfg, hlookup)
        self.assertEqual(plan['model'], 'classic(no_history)')
        self.assertIn('warning', plan)

    def test_advanced_dispatch(self):
        """model='advanced' → 走 calc_replenishment_advanced + 高级字段填充"""
        rows = _build_history(10, 30)
        def hlookup(sku, reg, days, name=None): return rows
        cfg = {'model': 'advanced', 'safety_days': 2, 'in_transit_qty': 0,
               'advanced': {
                   'promo': {'dates': [], 'boost': 1.5, 'lead_days': 3, 'enabled': False},
                   'slow': {'threshold_per_day': 1.0, 'stock_ratio': 5.0, 'enabled': False},
                   'season': {'enabled': False},
                   'oversell': {'high_ratio': 0.5, 'enabled': False},
               }}
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.dispatch(item, '广东', 2, cfg, hlookup)
        self.assertEqual(plan['model'], 'advanced')
        # 全部默认关闭 → 高级字段全 1.0/False
        self.assertEqual(plan['season_factor'], 1.0)
        self.assertEqual(plan['promo_multiplier'], 1.0)
        self.assertEqual(plan['slow_moving'], False)
        self.assertEqual(plan['oversell_risk'], False)
        self.assertIsNone(plan['oversell_level'])
        self.assertEqual(plan['effective_daily'], 10.0)

    def test_advanced_dispatch_history_exception_fallback(self):
        """advanced + history 抛异常 → classic(error) 兜底"""
        def hlookup(*a, **kw):
            raise RuntimeError('db broken')
        cfg = {'model': 'advanced', 'safety_days': 2, 'in_transit_qty': 0,
               'advanced': {
                   'promo': {'dates': [], 'boost': 1.5, 'lead_days': 3, 'enabled': False},
                   'slow': {'threshold_per_day': 1.0, 'stock_ratio': 5.0, 'enabled': False},
                   'season': {'enabled': False},
                   'oversell': {'high_ratio': 0.5, 'enabled': False},
               }}
        item = {'name': 'X', 'stock': 0, 'sales': 10, 'sku_id': 'S1'}
        plan = self.dispatch(item, '广东', 2, cfg, hlookup)
        self.assertEqual(plan['model'], 'classic(error)')

    def test_cfg_none_uses_default(self):
        """cfg=None → 用 build_default_cfg()（advanced 默认）"""
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.dispatch(item, '广东', 2, None, None)
        # 缺参兜底走 build_default_cfg（model='advanced'）
        self.assertEqual(plan['model'], 'advanced')

    def test_cfg_not_dict_uses_default(self):
        """cfg='something' (非 dict) → 用 build_default_cfg()"""
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.dispatch(item, '广东', 2, 'oops', None)
        self.assertEqual(plan['model'], 'advanced')

    def test_cfg_missing_advanced_node_uses_default(self):
        """cfg 缺 'advanced' 节点 → cfg 字典自取（计算函数兜底）"""
        cfg = {'model': 'advanced', 'safety_days': 2, 'in_transit_qty': 0}
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        # calc_replenishment_advanced 内部会 _merge_advanced_cfg 兜底
        plan = self.dispatch(item, '广东', 2, cfg, None)
        self.assertEqual(plan['model'], 'advanced')

    def test_unknown_model_falls_back_to_classic(self):
        """cfg.model='xxx' 非法值 → 兜底经典"""
        cfg = {'model': 'xxx', 'safety_days': 2, 'in_transit_qty': 0}
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.dispatch(item, '广东', 2, cfg, None)
        self.assertEqual(plan['model'], 'classic')

    def test_dispatch_no_history_lookup(self):
        """history_lookup=None 时所有模式都不崩"""
        cfg = {'model': 'weighted', 'safety_days': 2, 'in_transit_qty': 0}
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.dispatch(item, '广东', 2, cfg, None)
        # weighted 没数据 → classic(no_history)
        self.assertEqual(plan['model'], 'classic(no_history)')


class TestWarningTags(unittest.TestCase):
    """预警标签：4 类单选/组合/空"""

    def setUp(self):
        from algorithm_ui import (
            warning_tags_for_plan, warning_display,
            TAG_SLOW, TAG_OVERSELL_HIGH, TAG_OVERSELL_MED, TAG_LOWCONF,
        )
        self.tags_for = warning_tags_for_plan
        self.display = warning_display
        self.TAG_SLOW = TAG_SLOW
        self.TAG_OVERSELL_HIGH = TAG_OVERSELL_HIGH
        self.TAG_OVERSELL_MED = TAG_OVERSELL_MED
        self.TAG_LOWCONF = TAG_LOWCONF

    def test_empty_plan(self):
        self.assertEqual(self.tags_for({}), [])
        self.assertEqual(self.tags_for(None), [])
        self.assertEqual(self.display({}), '')

    def test_slow_only(self):
        plan = {'slow_moving': True}
        self.assertEqual(self.tags_for(plan), [self.TAG_SLOW])
        self.assertEqual(self.display(plan), '滞销⚠')

    def test_oversell_high(self):
        plan = {'oversell_risk': True, 'oversell_level': 'high'}
        self.assertEqual(self.tags_for(plan), [self.TAG_OVERSELL_HIGH])
        self.assertEqual(self.display(plan), '超卖🔥')

    def test_oversell_medium(self):
        plan = {'oversell_risk': True, 'oversell_level': 'medium'}
        self.assertEqual(self.tags_for(plan), [self.TAG_OVERSELL_MED])
        self.assertEqual(self.display(plan), '超卖⚠')

    def test_oversell_no_level_falls_back_medium(self):
        """oversell_risk=True 但 level 缺失 → 兜底 medium"""
        plan = {'oversell_risk': True}
        self.assertEqual(self.tags_for(plan), [self.TAG_OVERSELL_MED])

    def test_low_confidence_from_item(self):
        """低置信从 item-level flag 透传"""
        plan = {}
        item = {'name': 'X', '_low_confidence': True}
        self.assertEqual(self.tags_for(plan, item), [self.TAG_LOWCONF])
        self.assertEqual(self.display(plan, item), '低置信⚠')

    def test_combined_slow_oversell(self):
        plan = {'slow_moving': True, 'oversell_risk': True, 'oversell_level': 'high'}
        self.assertEqual(self.tags_for(plan),
                         [self.TAG_OVERSELL_HIGH, self.TAG_SLOW])
        # 顺序：先超卖 high → 滞销
        self.assertEqual(self.display(plan), '超卖🔥 / 滞销⚠')

    def test_combined_slow_lowconf(self):
        plan = {'slow_moving': True}
        item = {'name': 'X', '_low_confidence': True}
        self.assertEqual(self.tags_for(plan, item),
                         [self.TAG_SLOW, self.TAG_LOWCONF])
        self.assertEqual(self.display(plan, item), '滞销⚠ / 低置信⚠')

    def test_combined_all_three(self):
        plan = {'slow_moving': True, 'oversell_risk': True, 'oversell_level': 'high'}
        item = {'name': 'X', '_low_confidence': True}
        tags = self.tags_for(plan, item)
        self.assertEqual(len(tags), 3)
        self.assertIn(self.TAG_OVERSELL_HIGH, tags)
        self.assertIn(self.TAG_SLOW, tags)
        self.assertIn(self.TAG_LOWCONF, tags)

    def test_low_confidence_only_works_in_classic_too(self):
        """低置信透传与模型无关：经典模式 item._low_confidence=True 也会显示"""
        # 即 plan 是经典模式无 slow/oversell 字段
        plan = {'status': '立刻补货', 'color': 'red', 'qty': 100, 'model': 'classic'}
        item = {'name': 'X', '_low_confidence': True}
        self.assertEqual(self.display(plan, item), '低置信⚠')

    def test_oversell_risk_false_ignored(self):
        plan = {'oversell_risk': False, 'oversell_level': 'high'}
        self.assertEqual(self.tags_for(plan), [])

    def test_slow_moving_false_ignored(self):
        plan = {'slow_moving': False}
        self.assertEqual(self.tags_for(plan), [])


class TestPromoDateParsing(unittest.TestCase):
    """大促日期解析"""

    def setUp(self):
        from algorithm_ui import normalize_promo_dates, parse_promo_date_input
        self.norm = normalize_promo_dates
        self.parse = parse_promo_date_input

    def test_valid_dates_kept(self):
        self.assertEqual(self.norm(['2025-11-11', '2025-12-12']),
                         ['2025-11-11', '2025-12-12'])

    def test_comma_separated_string(self):
        self.assertEqual(self.norm('2025-11-11, 2025-12-12'),
                         ['2025-11-11', '2025-12-12'])

    def test_space_separated_string(self):
        self.assertEqual(self.norm('2025-11-11 2025-12-12'),
                         ['2025-11-11', '2025-12-12'])

    def test_newline_separated_string(self):
        self.assertEqual(self.norm('2025-11-11\n2025-12-12'),
                         ['2025-11-11', '2025-12-12'])

    def test_bad_dates_ignored(self):
        self.assertEqual(self.norm(['2025-99-99', 'not-a-date', '2024-13-45']),
                         [])
        self.assertEqual(self.norm(['2025-11-11', 'bad', '2025-12-12']),
                         ['2025-11-11', '2025-12-12'])

    def test_duplicates_dedup_preserve_order(self):
        self.assertEqual(self.norm(['2025-11-11', '2025-12-12', '2025-11-11']),
                         ['2025-11-11', '2025-12-12'])

    def test_none_returns_empty(self):
        self.assertEqual(self.norm(None), [])
        self.assertEqual(self.norm(''), [])
        self.assertEqual(self.norm(123), [])

    def test_empty_list(self):
        self.assertEqual(self.norm([]), [])

    def test_whitespace_stripped(self):
        self.assertEqual(self.norm(['  2025-11-11  ']),
                         ['2025-11-11'])

    def test_non_string_items_skipped(self):
        self.assertEqual(self.norm([None, 123, '2025-11-11']),
                         ['2025-11-11'])

    def test_parse_empty_string(self):
        self.assertEqual(self.parse(''), [])
        self.assertEqual(self.parse(None), [])


class TestCollectAdvancedCfgFromForm(unittest.TestCase):
    """UI 表单 → cfg 存储形态"""

    def setUp(self):
        from algorithm_ui import collect_advanced_cfg_from_form
        self.collect = collect_advanced_cfg_from_form

    def test_empty_form_returns_defaults(self):
        cfg = self.collect({})
        self.assertFalse(cfg['promo']['enabled'])
        self.assertEqual(cfg['promo']['dates'], [])
        self.assertEqual(cfg['promo']['boost'], 1.5)
        self.assertEqual(cfg['promo']['lead_days'], 3)
        self.assertFalse(cfg['slow']['enabled'])
        self.assertEqual(cfg['slow']['threshold_per_day'], 1.0)
        self.assertEqual(cfg['slow']['stock_ratio'], 5.0)
        self.assertFalse(cfg['season']['enabled'])
        self.assertFalse(cfg['oversell']['enabled'])
        self.assertEqual(cfg['oversell']['high_ratio'], 0.5)

    def test_none_input_returns_defaults(self):
        cfg = self.collect(None)
        self.assertFalse(cfg['promo']['enabled'])

    def test_promo_enabled_with_dates(self):
        form = {'promo': {
            'enabled': True, 'dates_text': '2025-11-11, 2025-12-12',
            'boost': 2.0, 'lead_days': 5,
        }}
        cfg = self.collect(form)
        self.assertTrue(cfg['promo']['enabled'])
        self.assertEqual(cfg['promo']['dates'], ['2025-11-11', '2025-12-12'])
        self.assertEqual(cfg['promo']['boost'], 2.0)
        self.assertEqual(cfg['promo']['lead_days'], 5)

    def test_promo_auto_enable_when_dates_present(self):
        """没显式 enabled 但有 dates → 自动 enabled=True"""
        form = {'promo': {'dates_text': '2025-11-11'}}
        cfg = self.collect(form)
        self.assertTrue(cfg['promo']['enabled'])
        self.assertEqual(cfg['promo']['dates'], ['2025-11-11'])

    def test_promo_bad_dates_filtered(self):
        form = {'promo': {'enabled': True,
                          'dates_text': '2025-11-11, bad, 2025-99-99'}}
        cfg = self.collect(form)
        self.assertEqual(cfg['promo']['dates'], ['2025-11-11'])

    def test_slow_config(self):
        form = {'slow': {'enabled': True,
                         'threshold_per_day': 2.5, 'stock_ratio': 8.0}}
        cfg = self.collect(form)
        self.assertTrue(cfg['slow']['enabled'])
        self.assertEqual(cfg['slow']['threshold_per_day'], 2.5)
        self.assertEqual(cfg['slow']['stock_ratio'], 8.0)

    def test_slow_negative_threshold_keeps_default(self):
        form = {'slow': {'enabled': True, 'threshold_per_day': -1}}
        cfg = self.collect(form)
        # 负值拒绝 → 保留默认 1.0
        self.assertEqual(cfg['slow']['threshold_per_day'], 1.0)

    def test_season_enabled(self):
        form = {'season': {'enabled': True}}
        cfg = self.collect(form)
        self.assertTrue(cfg['season']['enabled'])

    def test_oversell_high_ratio_clamped(self):
        """high_ratio 越界（>1）→ 保留默认"""
        form = {'oversell': {'enabled': True, 'high_ratio': 1.5}}
        cfg = self.collect(form)
        self.assertEqual(cfg['oversell']['high_ratio'], 0.5)  # 越界 → 默认

    def test_oversell_high_ratio_kept(self):
        form = {'oversell': {'enabled': True, 'high_ratio': 0.3}}
        cfg = self.collect(form)
        self.assertEqual(cfg['oversell']['high_ratio'], 0.3)

    def test_full_form(self):
        form = {
            'promo': {'enabled': True, 'dates_text': '2025-11-11',
                      'boost': 1.8, 'lead_days': 3},
            'slow': {'enabled': True, 'threshold_per_day': 1.0, 'stock_ratio': 5.0},
            'season': {'enabled': True},
            'oversell': {'enabled': True, 'high_ratio': 0.4},
        }
        cfg = self.collect(form)
        self.assertEqual(cfg['promo']['dates'], ['2025-11-11'])
        self.assertEqual(cfg['promo']['boost'], 1.8)
        self.assertTrue(cfg['slow']['enabled'])
        self.assertTrue(cfg['season']['enabled'])
        self.assertEqual(cfg['oversell']['high_ratio'], 0.4)


class TestEnrichPlan(unittest.TestCase):
    """enrich_plan_with_advanced_fields / enrich_plan_with_warning"""

    def setUp(self):
        from algorithm_ui import (
            enrich_plan_with_advanced_fields,
            enrich_plan_with_warning,
        )
        self.enrich_adv = enrich_plan_with_advanced_fields
        self.enrich_warn = enrich_plan_with_warning

    def test_enrich_adv_fills_defaults(self):
        plan = {'status': 'x', 'color': 'y', 'qty': 1, 'daily': 10}
        out = self.enrich_adv(plan)
        self.assertEqual(out['season_factor'], 1.0)
        self.assertEqual(out['promo_multiplier'], 1.0)
        self.assertEqual(out['effective_daily'], 10)  # daily 缺时 setdefault 不加，传入=占位
        self.assertEqual(out['slow_moving'], False)
        self.assertEqual(out['oversell_risk'], False)
        self.assertIsNone(out['oversell_level'])

    def test_enrich_adv_no_daily_key(self):
        """plan 缺 'daily' 键 → setdefault 不加 effective_daily（不抛）"""
        plan = {'status': 'x', 'color': 'y', 'qty': 1}
        out = self.enrich_adv(plan)
        # setdefault('effective_daily', plan.get('daily', 0)) → plan.get daily=None → 0 写入
        self.assertEqual(out['effective_daily'], 0)

    def test_enrich_adv_preserves_existing(self):
        plan = {'season_factor': 1.5, 'promo_multiplier': 2.0,
                'slow_moving': True, 'oversell_risk': True,
                'oversell_level': 'high', 'effective_daily': 30.0}
        out = self.enrich_adv(plan)
        self.assertEqual(out['season_factor'], 1.5)
        self.assertEqual(out['promo_multiplier'], 2.0)
        self.assertTrue(out['slow_moving'])
        self.assertTrue(out['oversell_risk'])
        self.assertEqual(out['oversell_level'], 'high')
        self.assertEqual(out['effective_daily'], 30.0)

    def test_enrich_adv_non_dict_returns_unchanged(self):
        self.assertIsNone(self.enrich_adv(None))
        # 注意：None 也会被改 = None（setdefault 抛 AttributeError？）
        # 实际 setdefault 接收 None 会因 NoneType 不支持 __setitem__ → AttributeError
        # 我们的实现里 isinstance(plan, dict) 守卫 True → 返回原 plan
        # 重新确认
        out = self.enrich_adv('not a dict')
        self.assertEqual(out, 'not a dict')

    def test_enrich_warn_slow(self):
        plan = {'slow_moving': True}
        out = self.enrich_warn(plan)
        self.assertEqual(out['warning'], '滞销⚠')

    def test_enrich_warn_oversell_high(self):
        plan = {'oversell_risk': True, 'oversell_level': 'high'}
        out = self.enrich_warn(plan)
        self.assertEqual(out['warning'], '超卖🔥')

    def test_enrich_warn_lowconf_from_item(self):
        plan = {}
        item = {'_low_confidence': True}
        out = self.enrich_warn(plan, item)
        self.assertEqual(out['warning'], '低置信⚠')

    def test_enrich_warn_empty(self):
        plan = {}
        out = self.enrich_warn(plan)
        self.assertEqual(out['warning'], '')


class TestContractWithT2(unittest.TestCase):
    """与 t2 calc_replenishment_advanced 契约核对：字段集合、type 兼容"""

    def setUp(self):
        from algorithm_ui import dispatch_plan
        self.dispatch = dispatch_plan

    def test_advanced_plan_field_set_matches_t2(self):
        """dispatch_plan 输出的 advanced plan 字段集合 = t2 契约"""
        rows = _build_history(10, 30)
        def hlookup(sku, reg, days, name=None): return rows
        cfg = {'model': 'advanced', 'safety_days': 2, 'in_transit_qty': 0,
               'advanced': {
                   'promo': {'dates': [], 'boost': 1.5, 'lead_days': 3, 'enabled': False},
                   'slow': {'threshold_per_day': 1.0, 'stock_ratio': 5.0, 'enabled': False},
                   'season': {'enabled': False},
                   'oversell': {'high_ratio': 0.5, 'enabled': False},
               }}
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.dispatch(item, '广东', 2, cfg, hlookup)
        # 基础字段（与 classic/weighted 同构）
        for k in ('status', 'color', 'qty', 'ratio', 'reorder', 'daily', 'stock', 'model'):
            self.assertIn(k, plan, f'缺基础字段 {k}')
        # 高级附加字段
        for k in ('season_factor', 'promo_multiplier', 'effective_daily',
                  'slow_moving', 'oversell_risk', 'oversell_level'):
            self.assertIn(k, plan, f'缺高级字段 {k}')
        # 预警字段
        self.assertIn('warning', plan)
        self.assertEqual(plan['model'], 'advanced')

    def test_advanced_model_tag_value(self):
        """t2 advanced 模式 → plan['model'] == 'advanced'"""
        cfg = {'model': 'advanced', 'safety_days': 2, 'in_transit_qty': 0,
               'advanced': {
                   'promo': {'dates': [], 'boost': 1.5, 'lead_days': 3, 'enabled': False},
                   'slow': {'threshold_per_day': 1.0, 'stock_ratio': 5.0, 'enabled': False},
                   'season': {'enabled': False},
                   'oversell': {'high_ratio': 0.5, 'enabled': False},
               }}
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.dispatch(item, '广东', 2, cfg, None)
        self.assertEqual(plan['model'], 'advanced')

    def test_advanced_with_promo_hit_today(self):
        """大促日期包含今天 → promo_multiplier=boost, effective_daily 放大"""
        today = datetime.now().strftime('%Y-%m-%d')
        rows = _build_history(10, 30)
        def hlookup(sku, reg, days, name=None): return rows
        cfg = {'model': 'advanced', 'safety_days': 2, 'in_transit_qty': 0,
               'advanced': {
                   'promo': {'dates': [today], 'boost': 2.0, 'lead_days': 3, 'enabled': True},
                   'slow': {'threshold_per_day': 1.0, 'stock_ratio': 5.0, 'enabled': False},
                   'season': {'enabled': False},
                   'oversell': {'high_ratio': 0.5, 'enabled': False},
               }}
        item = {'name': 'X', 'stock': 0, 'sales': 10, 'sku_id': 'S1'}
        plan = self.dispatch(item, '广东', 2, cfg, hlookup)
        self.assertEqual(plan['promo_multiplier'], 2.0)
        self.assertEqual(plan['effective_daily'], 20.0)

    def test_classic_plan_has_no_advanced_flags(self):
        """经典模式 plan 也包含高级字段（占位 None/False/1.0），方便 UI 无脑渲染"""
        cfg = {'model': 'classic', 'safety_days': 2, 'in_transit_qty': 0}
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = self.dispatch(item, '广东', 2, cfg, None)
        # 字段集合保证稳定（render 不需要 if/else 区分 model）
        for k in ('season_factor', 'promo_multiplier', 'effective_daily',
                  'slow_moving', 'oversell_risk', 'oversell_level', 'warning'):
            self.assertIn(k, plan)
        # 但值都是占位
        self.assertEqual(plan['season_factor'], 1.0)
        self.assertEqual(plan['promo_multiplier'], 1.0)
        self.assertEqual(plan['effective_daily'], 10.0)  # = plan.daily
        self.assertEqual(plan['slow_moving'], False)
        self.assertEqual(plan['oversell_risk'], False)
        self.assertIsNone(plan['oversell_level'])


class TestBuildDefaultCfg(unittest.TestCase):
    """build_default_cfg 形状与 utils DEFAULT_REPLENISHMENT_CFG 一致"""

    def test_default_cfg_shape(self):
        from algorithm_ui import build_default_cfg
        from utils import DEFAULT_REPLENISHMENT_CFG
        cfg = build_default_cfg()
        self.assertIn('model', cfg)
        self.assertIn('safety_days', cfg)
        self.assertIn('in_transit_qty', cfg)
        self.assertIn('advanced', cfg)
        # 与 utils DEFAULT 形态一致（字段集合）
        self.assertEqual(set(cfg['advanced'].keys()),
                         set(DEFAULT_REPLENISHMENT_CFG['advanced'].keys()))
        for sub in cfg['advanced']:
            self.assertEqual(set(cfg['advanced'][sub].keys()),
                             set(DEFAULT_REPLENISHMENT_CFG['advanced'][sub].keys()))

    def test_default_advanced_all_disabled(self):
        from algorithm_ui import build_default_cfg
        cfg = build_default_cfg()
        for sub in cfg['advanced']:
            self.assertFalse(cfg['advanced'][sub].get('enabled', True),
                             f'{sub} 默认应 enabled=False')


class TestGuiSourceContract(unittest.TestCase):
    """静态契约：gui.py 已包含 advanced 分发 + 预警列（防回归）"""

    def test_gui_has_advanced_dispatch(self):
        import gui
        import inspect
        src = inspect.getsource(gui.App._calc_from_items)
        self.assertIn("'advanced'", src)
        self.assertIn('dispatch_plan', src)
        self.assertIn('from algorithm_ui import', src)

    def test_gui_renders_warning_column(self):
        import gui
        import inspect
        src = inspect.getsource(gui.App._render_tree)
        # 表格列里含 '预警'
        self.assertIn("'预警'", src)
        # 渲染逻辑里含 p.get('warning', '')
        self.assertIn("p.get('warning'", src)

    def test_gui_plan_has_warning_field(self):
        import gui
        import inspect
        src = inspect.getsource(gui.App._calc_from_items)
        # plan dict 里包含 'warning' 字段
        self.assertIn("'warning':", src)
        # 高级模式附加字段
        for k in ('season_factor', 'promo_multiplier', 'slow_moving',
                  'oversell_risk', 'oversell_level'):
            self.assertIn(f"'{k}'", src)

    def test_export_xlsx_has_warning_column(self):
        import export_xlsx
        import inspect
        src = inspect.getsource(export_xlsx.export_cache_to_xlsx)
        # headers 含 '预警'
        self.assertIn("'预警'", src)
        # vals.append 写 warning 字段
        self.assertIn("p.get('warning'", src)

    def test_settings_ui_has_advanced_radio(self):
        import settings_ui
        import inspect
        src = inspect.getsource(settings_ui.SettingsUIMixin._build_replenishment_card)
        # 高级 radio
        self.assertIn("'高级（季节/大促/滞销/超卖）'", src)
        self.assertIn("'advanced'", src)
        # 大促日期编辑区
        self.assertIn('dates_text', src)
        # 滞销 spinbox
        self.assertIn('threshold_per_day', src)
        # 超卖 high_ratio
        self.assertIn('high_ratio', src)
        # 季节 enabled
        self.assertIn('season', src)


if __name__ == '__main__':
    unittest.main()
