# -*- coding: utf-8 -*-
"""R2 预测升级（t6/gui 侧）纯逻辑单测：预测日销列文案 / 安全库存简报 / 单品预测标注。

覆盖对象（gui.py 模块级纯函数 + 接线契约，不依赖 Tk 实例）：
- forecast_cell_text      plan['forecast'] → 结果表「预测」列文案（None → '—'）
- safety_brief_text       安全库存推荐 → 状态栏简报文案
- forecast_note_text      单品趋势弹窗预测标注（None → 数据不足提示）
- tree_col_width          「预测」列宽 90（既有列宽规则回归锚点）
- _build_safety_recommendation  推荐缓存 payload 结构（t5 契约：settings
                          ['replenishment']['recommendation'] 白名单键，σ/样本数
                          与推荐同源同窗）
- 源码接线契约            _calc_from_items 注入 forecast / export 含「预测日销」列
"""
import inspect
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


class TestForecastCellText(unittest.TestCase):
    """plan['forecast'] → 「预测」列文案（gui.forecast_cell_text）。"""

    def test_none_shows_dash(self):
        """None → '—'（无历史/样本不足，t5 返 None，不编数——§4）。"""
        import gui
        self.assertEqual(gui.forecast_cell_text(None), '—')

    def test_numeric_formatting(self):
        """数值 round 1 去尾 .0。"""
        import gui
        self.assertEqual(gui.forecast_cell_text(12.0), '12')
        self.assertEqual(gui.forecast_cell_text(12), '12')
        self.assertEqual(gui.forecast_cell_text(12.5), '12.5')
        self.assertEqual(gui.forecast_cell_text(0), '0')
        self.assertEqual(gui.forecast_cell_text(0.25), '0.2')  # round 银行家舍入 → 0.2
        self.assertEqual(gui.forecast_cell_text(1234.56), '1234.6')

    def test_invalid_shows_dash(self):
        """非数值 / NaN → '—'（不抛）。"""
        import gui
        self.assertEqual(gui.forecast_cell_text('abc'), '—')
        self.assertEqual(gui.forecast_cell_text(float('nan')), '—')
        self.assertEqual(gui.forecast_cell_text(object()), '—')


class TestSafetyBriefText(unittest.TestCase):
    """安全库存推荐 → 状态栏简报（gui.safety_brief_text）。"""

    def test_valid_days(self):
        import gui
        self.assertEqual(gui.safety_brief_text(3), '安全库存建议：3 天（基于近30天波动）')
        self.assertEqual(gui.safety_brief_text(30), '安全库存建议：30 天（基于近30天波动）')

    def test_invalid_days_empty(self):
        """None / ≤0 / 非法 → ''（状态栏不加简报）。"""
        import gui
        self.assertEqual(gui.safety_brief_text(None), '')
        self.assertEqual(gui.safety_brief_text(0), '')
        self.assertEqual(gui.safety_brief_text(-2), '')
        self.assertEqual(gui.safety_brief_text('x'), '')


class TestForecastNoteText(unittest.TestCase):
    """单品趋势弹窗预测标注（gui.forecast_note_text）。"""

    def test_insufficient_data_hint(self):
        import gui
        self.assertEqual(gui.forecast_note_text(None), '历史数据不足（暂无预测日销）')
        self.assertEqual(gui.forecast_note_text('bad'), '历史数据不足（暂无预测日销）')

    def test_forecast_hint(self):
        import gui
        self.assertEqual(gui.forecast_note_text(12.5), '预测日销 ≈ 12.5')
        self.assertEqual(gui.forecast_note_text(8.0), '预测日销 ≈ 8')


class TestTreeColWidthForecast(unittest.TestCase):
    """「预测」列宽 90（gui.tree_col_width R2 扩展 + 既有规则回归锚点）。"""

    def test_forecast_column_90(self):
        import gui
        self.assertEqual(gui.tree_col_width('预测'), 90)

    def test_existing_rules_unchanged(self):
        """既有列宽不回归：预警 140 / 模型 100 / 名称类 260 / 数字 110。"""
        import gui
        self.assertEqual(gui.tree_col_width('预警'), 140)
        self.assertEqual(gui.tree_col_width('模型'), 100)
        self.assertEqual(gui.tree_col_width('商品信息'), 260)
        self.assertEqual(gui.tree_col_width('可售卖天数'), 110)


class TestSafetyRecommendationPayload(unittest.TestCase):
    """推荐缓存 payload 结构（gui.App._build_safety_recommendation，t5 契约）。

    该方法不引用任何 self 状态（纯逻辑），测试以 None 作 self 直接调用。
    """

    @staticmethod
    def _rows(sales_by_day):
        """构造 history_db.query_sku_history 同款形态：[{captured_at, sales}, ...]。"""
        return [{'captured_at': f'2026-08-{d:02d}T10:00:00', 'sales': v}
                for d, v in sales_by_day]

    def test_payload_structure_and_keys(self):
        """有效波动数据 → payload 含 t5 白名单全键；σ>0、样本数=30 天窗、forecast 透传。"""
        import gui
        rows = self._rows([(d, 10 + (d % 4) * 5) for d in range(1, 11)])  # 10 天波动
        p = gui.App._build_safety_recommendation(
            None, rows, lead_days=3, name='商品A', sku_id='SKU123',
            region='广东', forecast=9.5)
        self.assertIsInstance(p, dict)
        for k in ('safety_days', 'safety_days_lead', 'sigma', 'forecast',
                  'n_samples', 'z', 'computed_at', 'sku_key'):
            self.assertIn(k, p, k)
        self.assertIsInstance(p['safety_days'], int)
        self.assertGreaterEqual(p['safety_days'], 1)
        self.assertLessEqual(p['safety_days'], 30)
        self.assertEqual(p['safety_days_lead'], 3)
        self.assertEqual(p['forecast'], 9.5)
        self.assertEqual(p['n_samples'], 30)  # 30 天窗零填充
        self.assertGreater(p['sigma'], 0.0)  # 有波动 → σ>0
        self.assertAlmostEqual(p['z'], 1.65)
        self.assertEqual(p['sku_key'], 'SKU123')

    def test_sku_key_fallback_region_name(self):
        """无 sku_id → sku_key 回退 region+name（与历史库关联键同语义）。"""
        import gui
        rows = self._rows([(d, 10 + d) for d in range(1, 11)])
        p = gui.App._build_safety_recommendation(
            None, rows, lead_days=3, name='商品B', sku_id='', region='云南', forecast=None)
        self.assertEqual(p['sku_key'], '云南+商品B')
        self.assertEqual(p['forecast'], 0.0)  # 无预测 → 0.0 占位（缓存形态要求 float）

    def test_flat_series_no_recommendation(self):
        """全 0 序列（σ=0）→ t5 返 None → 不产 payload（§4 不强给）。"""
        import gui
        rows = self._rows([(d, 0) for d in range(1, 11)])
        p = gui.App._build_safety_recommendation(None, rows, lead_days=3)
        self.assertIsNone(p)

    def test_bad_rows_no_recommendation(self):
        """空/坏输入 → None，不抛。"""
        import gui
        self.assertIsNone(gui.App._build_safety_recommendation(None, [], lead_days=3))
        self.assertIsNone(gui.App._build_safety_recommendation(None, None, lead_days=3))
        self.assertIsNone(gui.App._build_safety_recommendation(None, 'bad', lead_days=0))


class TestForecastWiringContract(unittest.TestCase):
    """源码接线契约（inspect 断言，防后续重构静默断链）。"""

    def test_calc_from_items_injects_forecast(self):
        """_calc_from_items 必须调 forecast_next_period 并写 plan['forecast']。"""
        import gui
        src = inspect.getsource(gui.App._calc_from_items)
        self.assertIn('forecast_next_period', src)
        self.assertIn("plans[-1]['forecast']", src)
        self.assertIn('_build_safety_recommendation', src)
        # 经典公式关键词不回归（铁律锚点，与 test_smoke 同款）
        self.assertIn("daily * 8", src)

    def test_export_xlsx_has_forecast_column(self):
        """export_xlsx headers 含「预测日销」且写在 forecast 字段。"""
        import export_xlsx
        src = inspect.getsource(export_xlsx.export_cache_to_xlsx)
        self.assertIn("'预测日销'", src)
        self.assertIn("p.get('forecast')", src)

    def test_history_chart_has_forecast_note(self):
        """单品趋势弹窗必须带预测标注（数据不足也有提示文字）。"""
        import gui
        src = inspect.getsource(gui.App._history_sku_chart)
        self.assertIn('forecast_next_period', src)
        self.assertIn('forecast_note_text', src)

    def test_render_tree_has_forecast_column(self):
        """结果表 calc_cols 必须含「预测」列且用 forecast_cell_text 渲染。"""
        import gui
        src = inspect.getsource(gui.App._render_tree)
        self.assertIn("('预测', 'forecast')", src)
        self.assertIn('forecast_cell_text(p.get', src)


if __name__ == '__main__':
    unittest.main()
