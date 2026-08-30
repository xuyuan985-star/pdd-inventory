"""R1 布局优化 — 纯逻辑助手单测（无 Tk 依赖）

settings_ui.py / stats_ui.py 抽出的小型纯函数（store_list_row_label /
store_list_active_index / store_button_disabled_state /
adv_frame_visibility_for_model / history_filter_segments /
history_summary_text / history_empty_placeholder_row）必须机器可验证；
本测试不创建任何 widget——只调静态方法、断言返回值。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import settings_ui  # noqa: E402
import stats_ui  # noqa: E402


# ═══════════════════════ 店铺管理（settings_ui.SettingsUIMixin 静态助手） ═══════════

class TestStoreListRowLabel(unittest.TestCase):
    """store_list_row_label：行文本「name  ★ 当前」/「name」。"""

    def test_active_match_appends_marker(self):
        self.assertEqual(
            settings_ui.SettingsUIMixin.store_list_row_label('主力店', 's1', 's1'),
            '主力店  ★ 当前')

    def test_active_mismatch_no_marker(self):
        self.assertEqual(
            settings_ui.SettingsUIMixin.store_list_row_label('分店', 's2', 's1'),
            '分店')

    def test_blank_name_falls_back_to_id(self):
        # 极端：name 为空时用 id 显示
        self.assertEqual(
            settings_ui.SettingsUIMixin.store_list_row_label('', 's1', 's1'),
            's1  ★ 当前')

    def test_blank_name_no_match(self):
        self.assertEqual(
            settings_ui.SettingsUIMixin.store_list_row_label('', 's2', 's1'),
            's2')

    def test_none_inputs(self):
        # 全 None / 空 不抛
        self.assertEqual(
            settings_ui.SettingsUIMixin.store_list_row_label(None, None, None),
            '')
        self.assertEqual(
            settings_ui.SettingsUIMixin.store_list_row_label(None, 's1', 's1'),
            's1  ★ 当前')


class TestStoreListActiveIndex(unittest.TestCase):
    """store_list_active_index：active 店在 stores 列表里的索引。"""

    def test_basic_hit(self):
        stores = [{'id': 'a', 'name': 'A'}, {'id': 'b', 'name': 'B'}, {'id': 'c', 'name': 'C'}]
        self.assertEqual(
            settings_ui.SettingsUIMixin.store_list_active_index(stores, 'b'), 1)

    def test_miss_returns_minus_one(self):
        stores = [{'id': 'a'}, {'id': 'b'}]
        self.assertEqual(
            settings_ui.SettingsUIMixin.store_list_active_index(stores, 'x'), -1)

    def test_empty_stores(self):
        self.assertEqual(
            settings_ui.SettingsUIMixin.store_list_active_index([], 'a'), -1)
        self.assertEqual(
            settings_ui.SettingsUIMixin.store_list_active_index(None, 'a'), -1)

    def test_dirty_entries_skipped(self):
        # 脏条目（非 dict / 缺 id）应跳过，不抛
        stores = [None, 'x', {}, {'id': 'b'}]
        self.assertEqual(
            settings_ui.SettingsUIMixin.store_list_active_index(stores, 'b'), 3)

    def test_empty_active_id(self):
        stores = [{'id': 'a'}, {'id': 'b'}]
        self.assertEqual(
            settings_ui.SettingsUIMixin.store_list_active_index(stores, ''), -1)


class TestStoreButtonDisabledState(unittest.TestCase):
    """store_button_disabled_state：4 按钮禁用态表。"""

    def test_single_store_all_locked_except_add(self):
        st = settings_ui.SettingsUIMixin.store_button_disabled_state(1, 0)
        self.assertFalse(st['add'], '新增永远可点')
        self.assertTrue(st['rename'], '单店 rename 禁')
        self.assertTrue(st['activate'], '单店 activate 禁')
        self.assertTrue(st['delete'], '单店 delete 禁')

    def test_no_selection_locks_actions(self):
        st = settings_ui.SettingsUIMixin.store_button_disabled_state(3, -1)
        self.assertFalse(st['add'])
        self.assertTrue(st['rename'])
        self.assertTrue(st['activate'])
        self.assertTrue(st['delete'])

    def test_selected_multi_store_actions_enabled(self):
        st = settings_ui.SettingsUIMixin.store_button_disabled_state(3, 1)
        self.assertFalse(st['add'])
        self.assertFalse(st['rename'])
        self.assertFalse(st['activate'])
        self.assertFalse(st['delete'])

    def test_zero_stores_only_add(self):
        st = settings_ui.SettingsUIMixin.store_button_disabled_state(0, -1)
        self.assertFalse(st['add'])
        self.assertTrue(st['rename'])
        self.assertTrue(st['activate'])
        self.assertTrue(st['delete'])

    def test_none_inputs_safe(self):
        st = settings_ui.SettingsUIMixin.store_button_disabled_state(None, None)
        # None/0 都视作 0
        self.assertFalse(st['add'])
        self.assertTrue(st['rename'])


class TestAdvFrameVisibilityForModel(unittest.TestCase):
    """adv_frame_visibility_for_model：高级编辑区显隐表。"""

    def test_advanced_model_visible(self):
        vis = settings_ui.SettingsUIMixin.adv_frame_visibility_for_model('advanced')
        self.assertTrue(vis['advanced_frame'])

    def test_classic_model_hidden(self):
        vis = settings_ui.SettingsUIMixin.adv_frame_visibility_for_model('classic')
        self.assertFalse(vis['advanced_frame'])

    def test_weighted_model_hidden(self):
        vis = settings_ui.SettingsUIMixin.adv_frame_visibility_for_model('weighted')
        self.assertFalse(vis['advanced_frame'])

    def test_empty_model_hidden(self):
        vis = settings_ui.SettingsUIMixin.adv_frame_visibility_for_model('')
        self.assertFalse(vis['advanced_frame'])

    def test_none_model_hidden(self):
        vis = settings_ui.SettingsUIMixin.adv_frame_visibility_for_model(None)
        self.assertFalse(vis['advanced_frame'])


# ═══════════════════════ 历史页（stats_ui.StatsPagesMixin 静态助手） ═══════════

class TestHistoryFilterSegments(unittest.TestCase):
    """history_filter_segments：3 段（店铺/地区/天数）元数据。"""

    def test_three_segments(self):
        segs = stats_ui.StatsPagesMixin.history_filter_segments()
        self.assertEqual(len(segs), 3)
        labels = [s[0] for s in segs]
        self.assertEqual(labels, ['店铺', '地区', '天数'])

    def test_defaults(self):
        segs = stats_ui.StatsPagesMixin.history_filter_segments()
        defaults = [s[2] for s in segs]
        self.assertEqual(defaults, ['全部店铺', '全部', '90'])

    def test_widths_positive(self):
        segs = stats_ui.StatsPagesMixin.history_filter_segments()
        for _label, w, _default in segs:
            self.assertGreater(w, 0)


class TestHistorySummaryText(unittest.TestCase):
    """history_summary_text：摘要标签文案决策（三分支）。"""

    def test_failure_path_prefixes_warning(self):
        text = stats_ui.StatsPagesMixin.history_summary_text(
            5, 90, True, 'connection refused')
        self.assertTrue(text.startswith('⚠'), '失败应带 ⚠ 前缀')
        self.assertIn('connection refused', text)

    def test_failure_long_msg_truncated(self):
        long_msg = 'x' * 200
        text = stats_ui.StatsPagesMixin.history_summary_text(0, 90, True, long_msg)
        # 截断到 60 字符（消息段）
        msg_part = text.split('：', 1)[-1] if '：' in text else text
        self.assertLessEqual(len(msg_part), 60)

    def test_rows_present_shows_count(self):
        text = stats_ui.StatsPagesMixin.history_summary_text(15, 30, False)
        self.assertIn('15', text)
        self.assertIn('30', text)
        self.assertIn('双击行看当日明细', text)

    def test_empty_rows_shows_empty_state(self):
        text = stats_ui.StatsPagesMixin.history_summary_text(0, 90, False)
        self.assertIn('暂无历史数据', text)

    def test_days_default_fallback(self):
        # days=0 / None 走 90 兜底
        t1 = stats_ui.StatsPagesMixin.history_summary_text(5, 0, False)
        t2 = stats_ui.StatsPagesMixin.history_summary_text(5, None, False)
        self.assertIn('90', t1)
        self.assertIn('90', t2)


class TestHistoryEmptyPlaceholderRow(unittest.TestCase):
    """history_empty_placeholder_row：空态占位行（5 列对应 day/region/items/alerts/stock）。"""

    def test_default_placeholder(self):
        row = stats_ui.StatsPagesMixin.history_empty_placeholder_row()
        self.assertEqual(len(row), 5)
        self.assertEqual(row[0], '--')
        self.assertEqual(row[1], '暂无数据')
        self.assertEqual(row[2], '--')
        self.assertEqual(row[3], '--')
        self.assertEqual(row[4], '--')


if __name__ == '__main__':
    unittest.main()