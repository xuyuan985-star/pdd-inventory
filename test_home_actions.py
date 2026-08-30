# -*- coding: utf-8 -*-
"""home_actions 单测（R2 主页按钮交互契约 · v1.5.7 布局定版）。

覆盖：
  - IMPORT_MENU_ITEMS 完整性（key/label/hint 三字段全在；import_table + pick_images）
  - find_menu_item 查表命中/未命中/异常输入（支持自定义 items）
  - menu_labels 顺序与内容
  - btn_width_for 各 kind 行为 + 非字符串 kind 兜底
  - BTN_WIDTH_HOME 常量合理性（> 0 且 <= 8）
  - busy_label_for 六 key 全覆盖 + 未知 key 兜底 + 异常输入兜底
  - busy_label_for 与原 busy_btn_text 算法等价性（兜底路径）
  - DUAL_MODEL_CHECKBUTTON_ROW 已切到 'second'
  - gui._BATCH_BUSY_BTNS 与 home_actions 契约对齐（export/live→image、无 img_batch_btn）
  - 「导入」菜单（_open_import_menu/_dispatch_import）与 gui 接线契约
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


class TestImportMenuItems(unittest.TestCase):
    """v1.5.7 导入按钮二级菜单项数据契约（导入表格文件 / 选择图片文件）。"""

    def test_items_count_two(self):
        from home_actions import IMPORT_MENU_ITEMS
        self.assertEqual(len(IMPORT_MENU_ITEMS), 2)

    def test_keys_unique_and_named(self):
        from home_actions import IMPORT_MENU_ITEMS
        keys = [it['key'] for it in IMPORT_MENU_ITEMS]
        self.assertEqual(len(set(keys)), len(keys), '菜单项 key 必须唯一')
        self.assertEqual(keys, ['import_table', 'pick_images'])

    def test_each_item_has_required_fields(self):
        from home_actions import IMPORT_MENU_ITEMS
        for it in IMPORT_MENU_ITEMS:
            self.assertIn('key', it)
            self.assertIn('label', it)
            self.assertIn('hint', it)
            self.assertIsInstance(it['key'], str)
            self.assertIsInstance(it['label'], str)
            self.assertIsInstance(it['hint'], str)
            self.assertTrue(it['label'].strip(), f'{it["key"]} label 不应为空')
            self.assertTrue(it['hint'].strip(), f'{it["key"]} hint 不应为空')

    def test_import_table_hint_mentions_table(self):
        """import_table 的 hint 必须透传「表格」语义（CSV/XLSX 列映射预览）。"""
        from home_actions import find_menu_item
        it = find_menu_item('import_table')
        self.assertIsNotNone(it)
        self.assertIn('表格', it['hint'])

    def test_pick_images_allows_multiple(self):
        """pick_images 的 hint 必须明确「多张」语义（用户：客户只管选几张图）。"""
        from home_actions import find_menu_item
        it = find_menu_item('pick_images')
        self.assertIsNotNone(it)
        self.assertIn('多张', it['hint'])


class TestFindMenuItem(unittest.TestCase):
    """按 key 查菜单项 + 异常输入兜底。"""

    def test_hit_returns_dict(self):
        from home_actions import find_menu_item, IMPORT_MENU_ITEMS
        self.assertIs(find_menu_item('import_table'), IMPORT_MENU_ITEMS[0])
        self.assertIs(find_menu_item('pick_images'), IMPORT_MENU_ITEMS[1])

    def test_custom_items_supported(self):
        """find_menu_item/menu_labels 支持传入自定义 items（契约泛化）。"""
        from home_actions import find_menu_item, menu_labels
        custom = ({'key': 'a', 'label': 'A', 'hint': 'h'},)
        self.assertIs(find_menu_item('a', custom), custom[0])
        self.assertIsNone(find_menu_item('b', custom))
        self.assertEqual(menu_labels(custom), ['A'])

    def test_miss_returns_none(self):
        from home_actions import find_menu_item
        self.assertIsNone(find_menu_item('no_such_key'))
        self.assertIsNone(find_menu_item(''))

    def test_non_string_returns_none(self):
        from home_actions import find_menu_item
        for bad in (None, 0, 1, 3.14, [], {}, (), object()):
            self.assertIsNone(find_menu_item(bad), f'非字符串输入 {bad!r} 必须返 None')


class TestMenuLabels(unittest.TestCase):
    """menu_labels 顺序与中文文案。"""

    def test_labels_in_order(self):
        from home_actions import menu_labels, IMPORT_MENU_ITEMS
        expected = [it['label'] for it in IMPORT_MENU_ITEMS]
        self.assertEqual(menu_labels(), expected)

    def test_labels_are_non_empty_chinese(self):
        from home_actions import menu_labels
        labels = menu_labels()
        self.assertEqual(len(labels), 2)
        for lab in labels:
            self.assertTrue(lab.strip())
            self.assertTrue(any('\u4e00' <= ch <= '\u9fff' for ch in lab), lab)


class TestBtnWidth(unittest.TestCase):
    """主页按钮统一宽度映射（用户意见 ③）。"""

    def test_width_constant_in_sane_range(self):
        from home_actions import BTN_WIDTH_HOME
        self.assertGreaterEqual(BTN_WIDTH_HOME, 4)
        self.assertLessEqual(BTN_WIDTH_HOME, 8)

    def test_btn_width_for_all_kinds_returns_int(self):
        from home_actions import btn_width_for, BTN_WIDTH_HOME
        for kind in ('dark', 'primary', 'text', 'ghost', 'tag', 'unknown_kind'):
            w = btn_width_for(kind)
            self.assertIsInstance(w, int)
            self.assertEqual(w, BTN_WIDTH_HOME, f'kind={kind} 应统一等于 BTN_WIDTH_HOME')

    def test_btn_width_for_non_string_fallback(self):
        from home_actions import btn_width_for, BTN_WIDTH_HOME
        for bad in (None, 0, '', b'dark', ['dark'], ('dark',), {'k': 'dark'}):
            self.assertEqual(btn_width_for(bad), BTN_WIDTH_HOME)


class TestBusyLabel(unittest.TestCase):
    """busy_label_for 全 key 映射 + 兜底。"""

    def test_six_keys_covered(self):
        from home_actions import busy_label_for, BUSY_BTN_KEYS
        # v1.5.7 定版：识图=image、导入=import、导出=export、刷新/批量同名字面、
        # 预留 capture。screenshot key 已随 v1.5.6 菜单解散移除。
        expected_keys = {'import', 'image', 'refresh', 'batch', 'export', 'capture'}
        self.assertEqual(BUSY_BTN_KEYS, expected_keys)
        for k in expected_keys:
            self.assertTrue(busy_label_for(k, '').strip(), f'{k} 文案不应为空')

    def test_image_key_returns_known_text(self):
        from home_actions import busy_label_for
        self.assertEqual(busy_label_for('image', '识图'), '识图中…')
        self.assertEqual(busy_label_for('image', ''), '识图中…')
        self.assertEqual(busy_label_for('image', None), '识图中…')

    def test_import_key(self):
        """导入按钮（表格/图片统一入口）忙时文案『导入中…』。"""
        from home_actions import busy_label_for
        self.assertEqual(busy_label_for('import', '导入'), '导入中…')

    def test_capture_key_reserved(self):
        from home_actions import busy_label_for
        self.assertEqual(busy_label_for('capture', '截图'), '截图中…')

    def test_export_key(self):
        from home_actions import busy_label_for
        self.assertEqual(busy_label_for('export', '导出'), '导出中…')

    def test_unknown_key_falls_back_to_orig_plus_zhong(self):
        from home_actions import busy_label_for
        self.assertEqual(busy_label_for('unknown', '保存'), '保存中…')
        self.assertEqual(busy_label_for('unknown', ''), '处理中…')
        self.assertEqual(busy_label_for('unknown', None), '处理中…')
        self.assertEqual(busy_label_for(None, '识图'), '识图中…')  # 非字符串 key 兜底
        self.assertEqual(busy_label_for(123, '导出'), '导出中…')

    def test_no_screenshot_key_in_busy_keys(self):
        """v1.5.7：screenshot key 已移除（并入 image 语义），防止回归旧契约。"""
        from home_actions import BUSY_BTN_KEYS
        self.assertNotIn('screenshot', BUSY_BTN_KEYS)

    def test_empty_or_none_orig_with_unknown_key(self):
        from home_actions import busy_label_for
        self.assertEqual(busy_label_for('whatever', ''), '处理中…')
        self.assertEqual(busy_label_for('whatever', None), '处理中…')


class TestDualModelContract(unittest.TestCase):
    """🛡 双模型勾选位置契约（第二排）。"""

    def test_row_moved_to_second(self):
        from home_actions import DUAL_MODEL_CHECKBUTTON_ROW
        self.assertEqual(DUAL_MODEL_CHECKBUTTON_ROW, 'second')

    def test_label_unchanged(self):
        from home_actions import DUAL_MODEL_CHECKBUTTON_LABEL
        self.assertEqual(DUAL_MODEL_CHECKBUTTON_LABEL, '🛡 双模型')


class TestPublicApi(unittest.TestCase):
    """__all__ 与公开符号一致。"""

    def test_all_symbols_importable(self):
        import home_actions
        for name in home_actions.__all__:
            self.assertTrue(hasattr(home_actions, name), f'缺少公开符号 {name}')

    def test_no_shot_menu_legacy_symbols(self):
        """v1.5.6 遗留符号 SHOT_MENU_ITEMS 必须不存在（已解散）。"""
        import home_actions
        self.assertFalse(hasattr(home_actions, 'SHOT_MENU_ITEMS'))


class TestBusyStateTableCoverage(unittest.TestCase):
    """v1.5.7 忙状态表（gui._BATCH_BUSY_BTNS）覆盖主页受控按钮。

    定版后受控入口：export_btn（导出）/ live_btn（识图=截当前窗口）。
    导入按钮（含图片路径）不纳入忙禁表——pick_images 内部自带互斥与忙状态管理。"""

    def test_gui_batch_busy_btns_has_export_and_live(self):
        import gui
        attrs = [a for a, _t, _k in gui._BATCH_BUSY_BTNS]
        self.assertIn('export_btn', attrs)
        self.assertIn('live_btn', attrs)

    def test_gui_batch_busy_btns_no_independent_image_batch_btn(self):
        import gui
        attrs = [a for a, _t, _k in gui._BATCH_BUSY_BTNS]
        self.assertNotIn('img_batch_btn', attrs)

    def test_gui_batch_busy_btns_uses_image_key_for_live_btn(self):
        """live_btn（识图）忙时语义 key 必须是 image（『识图中…』）。"""
        import gui
        key_map = dict((a, k) for a, _t, k in gui._BATCH_BUSY_BTNS)
        self.assertEqual(key_map.get('live_btn'), 'image')

    def test_gui_busy_btn_text_algorithm_preserved(self):
        import gui
        self.assertEqual(gui.busy_btn_text('识图'), '识图中…')
        self.assertEqual(gui.busy_btn_text('导出'), '导出中…')

    def test_home_actions_busy_label_image_key_coverage(self):
        from home_actions import busy_label_for, BUSY_BTN_KEYS
        self.assertIn('image', BUSY_BTN_KEYS)
        self.assertEqual(busy_label_for('image', '识图'), '识图中…')

    def test_home_actions_busy_label_all_six_keys_stable(self):
        from home_actions import busy_label_for, BUSY_BTN_KEYS
        self.assertEqual(len(BUSY_BTN_KEYS), 6)
        for key in sorted(BUSY_BTN_KEYS):
            result = busy_label_for(key, key)
            self.assertTrue(result.strip(), f'{key} 返回空文案')
            self.assertTrue(result.endswith('…'), f'{key} 文案应以省略号结尾：{result}')


class TestImportMenuVsGuiIntegration(unittest.TestCase):
    """v1.5.7 导入菜单与 gui.py 接线契约。

    home_actions.IMPORT_MENU_ITEMS 定义菜单项，gui._open_import_menu 按 key
    分派：import_table → _import_table；pick_images → _batch_images。
    识图按钮不弹菜单，直连 _live_screenshot（截当前窗口）。"""

    def test_import_menu_keys_defined(self):
        from home_actions import IMPORT_MENU_ITEMS
        keys = [it['key'] for it in IMPORT_MENU_ITEMS]
        self.assertEqual(sorted(keys), ['import_table', 'pick_images'])

    def test_gui_has_open_import_menu_method(self):
        import gui
        self.assertTrue(hasattr(gui.App, '_open_import_menu'))
        self.assertTrue(hasattr(gui.App, '_dispatch_import'))

    def test_gui_has_no_legacy_shot_menu_methods(self):
        """v1.5.6 的截图菜单方法必须移除（防回归旧布局）。"""
        import gui
        self.assertFalse(hasattr(gui.App, '_open_shot_menu'))
        self.assertFalse(hasattr(gui.App, '_dispatch_shot'))

    def test_gui_has_batch_images_method(self):
        import gui
        self.assertTrue(hasattr(gui.App, '_batch_images'))

    def test_gui_has_live_screenshot_method(self):
        import gui
        self.assertTrue(hasattr(gui.App, '_live_screenshot'))

    def test_gui_has_import_table_method(self):
        import gui
        self.assertTrue(hasattr(gui.App, '_import_table'))


if __name__ == '__main__':
    unittest.main()