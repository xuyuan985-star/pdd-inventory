"""PDD EZ — OCR 复核流程单元测试（v1.4.8 P2-OCR t9）

覆盖 ocr_review.py 纯函数：
  - categorize_error：12 类异常归类 + 未知 fallback + 空字符串/None/非 str 安全
  - summarize_review：空 / 全 high / 混合 / 旧标记（无 confidence 字段）兼容
  - build_review_list：low 在前 + medium 兜底 + 排序稳定 + 无 items 返回 []
  - apply_user_edits：白名单字段 + 越界 index 跳过 + 解析失败跳过 + in-place
  - has_low_confidence / has_review_items：快路径判定
不依赖 Tk；可与 test_ocr_confidence / test_smoke 一同跑。
"""
import os
import sys
import unittest
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


class TestCategorizeError(unittest.TestCase):
    """categorize_error：异常 → (category, user_msg, title)。"""

    def test_csv_encoding(self):
        from ocr_review import categorize_error
        cat, msg, title = categorize_error('无法识别 CSV 编码')
        self.assertEqual(cat, 'csv_encoding')
        self.assertIn('UTF-8', msg)
        self.assertEqual(title, '导入失败')

    def test_xlsx_corrupt(self):
        from ocr_review import categorize_error
        cat, msg, title = categorize_error('XLSX 文件已损坏或格式不识别')
        self.assertEqual(cat, 'xlsx_corrupt')
        self.assertEqual(title, '导入失败')

    def test_import_too_large(self):
        from ocr_review import categorize_error
        cat, msg, _ = categorize_error('数据行 15000 超过上限 10000')
        self.assertEqual(cat, 'import_too_large')
        self.assertIn('10000', msg)

    def test_legacy_xls(self):
        from ocr_review import categorize_error
        cat, msg, _ = categorize_error('暂不支持 .xls 老格式')
        self.assertEqual(cat, 'legacy_xls')

    def test_mapping_missing(self):
        from ocr_review import categorize_error
        cat, _, _ = categorize_error('导入文件列映射不完整，缺关键字段')
        self.assertEqual(cat, 'mapping_missing')

    def test_api_key_missing(self):
        from ocr_review import categorize_error
        cat, msg, title = categorize_error('API Key 未设置 — 请在「API 管理」页面配置')
        self.assertEqual(cat, 'api_key_missing')
        self.assertEqual(title, '识别失败')
        self.assertIn('API', msg)

    def test_fatal_quota(self):
        from ocr_review import categorize_error
        cat, _, _ = categorize_error('Free Quota Exceeded')
        self.assertEqual(cat, 'fatal_quota')
        cat2, _, _ = categorize_error('余额不足')
        self.assertEqual(cat2, 'fatal_quota')
        cat3, _, _ = categorize_error('403 forbidden')
        self.assertEqual(cat3, 'fatal_quota')

    def test_api_timeout(self):
        from ocr_review import categorize_error
        cat, _, _ = categorize_error('Read timed out')
        self.assertEqual(cat, 'api_timeout')
        cat2, _, _ = categorize_error('Request timeout after 30s')
        self.assertEqual(cat2, 'api_timeout')

    def test_json_parse(self):
        from ocr_review import categorize_error
        cat, _, _ = categorize_error('模型返回无法解析的 JSON')
        self.assertEqual(cat, 'json_parse')
        cat2, _, _ = categorize_error('JSON 截断')
        self.assertEqual(cat2, 'json_parse')

    def test_no_model(self):
        from ocr_review import categorize_error
        cat, _, _ = categorize_error('没有可用的识别模型')
        self.assertEqual(cat, 'no_model')

    def test_blur_ocr(self):
        from ocr_review import categorize_error
        cat, _, _ = categorize_error('图片模糊，建议重新截图')
        self.assertEqual(cat, 'blur_ocr')

    def test_low_confidence_signal(self):
        from ocr_review import categorize_error
        cat, _, title = categorize_error('low_confidence 标记')
        self.assertEqual(cat, 'low_confidence')
        self.assertEqual(title, '识别结果需复核')

    def test_unknown_fallback(self):
        from ocr_review import categorize_error
        cat, msg, title = categorize_error('Something completely unrelated XYZ123')
        self.assertEqual(cat, 'unknown')
        self.assertEqual(title, '出错')
        self.assertIsInstance(msg, str)
        self.assertGreater(len(msg), 0)

    def test_empty_string_safe(self):
        from ocr_review import categorize_error
        cat, msg, title = categorize_error('')
        self.assertEqual(cat, 'unknown')
        self.assertEqual(title, '出错')
        self.assertIsInstance(msg, str)

    def test_none_safe(self):
        from ocr_review import categorize_error
        cat, msg, title = categorize_error(None)  # type: ignore
        self.assertEqual(cat, 'unknown')
        self.assertIsInstance(msg, str)

    def test_exception_object_safe(self):
        from ocr_review import categorize_error
        try:
            raise ValueError('API Key 未设置')
        except ValueError as e:
            cat, _, _ = categorize_error(e)
        self.assertEqual(cat, 'api_key_missing')

    def test_case_insensitive(self):
        from ocr_review import categorize_error
        cat, _, _ = categorize_error('TIMEOUT after 30s')
        self.assertEqual(cat, 'api_timeout')


class TestSummarizeReview(unittest.TestCase):
    """summarize_review：confidence 分布统计。"""

    def test_empty(self):
        from ocr_review import summarize_review
        s = summarize_review([])
        self.assertEqual(s, {'total': 0, 'high': 0, 'medium': 0, 'low': 0, 'need_review': 0})

    def test_none_safe(self):
        from ocr_review import summarize_review
        s = summarize_review(None)  # type: ignore
        self.assertEqual(s['total'], 0)

    def test_all_high(self):
        from ocr_review import summarize_review
        items = [{'name': 'A', 'confidence': {'level': 'high', 'reasons': []}}] * 3
        s = summarize_review(items)
        self.assertEqual(s, {'total': 3, 'high': 3, 'medium': 0, 'low': 0, 'need_review': 0})

    def test_mixed(self):
        from ocr_review import summarize_review
        items = [
            {'confidence': {'level': 'high', 'reasons': []}},
            {'confidence': {'level': 'medium', 'reasons': []}},
            {'confidence': {'level': 'low', 'reasons': []}},
            {'confidence': {'level': 'low', 'reasons': []}},
        ]
        s = summarize_review(items)
        self.assertEqual(s['total'], 4)
        self.assertEqual(s['high'], 1)
        self.assertEqual(s['medium'], 1)
        self.assertEqual(s['low'], 2)
        self.assertEqual(s['need_review'], 3)

    def test_legacy_fallback_marks_low(self):
        """旧路径无 confidence 字段，但有 _low_confidence=True → 归 low。"""
        from ocr_review import summarize_review
        items = [
            {'name': 'A', '_low_confidence': True},
            {'name': 'B'},
        ]
        s = summarize_review(items)
        self.assertEqual(s['low'], 1)
        self.assertEqual(s['high'], 1)
        self.assertEqual(s['need_review'], 1)

    def test_name_unmatched_counts_as_low(self):
        from ocr_review import summarize_review
        items = [{'name': 'A', '_name_unmatched': True}]
        s = summarize_review(items)
        self.assertEqual(s['low'], 1)

    def test_dual_degraded_counts_as_low(self):
        from ocr_review import summarize_review
        items = [{'name': 'A', '_dual_degraded': True}]
        s = summarize_review(items)
        self.assertEqual(s['low'], 1)

    def test_non_dict_items_skipped(self):
        from ocr_review import summarize_review
        items = ['str', None, {'confidence': {'level': 'high', 'reasons': []}}]
        s = summarize_review(items)
        # 不抛；只统计 dict → 3 个 item 计入 total，但只有 1 个 dict 进 high
        self.assertEqual(s['total'], 3)
        self.assertEqual(s['high'], 1)
        self.assertEqual(s['need_review'], 0)


class TestBuildReviewList(unittest.TestCase):
    """build_review_list：low 优先 + 排序 + reason 合并。"""

    def test_empty(self):
        from ocr_review import build_review_list
        self.assertEqual(build_review_list([]), [])
        self.assertEqual(build_review_list(None), [])  # type: ignore

    def test_all_high_returns_empty(self):
        from ocr_review import build_review_list
        items = [{'name': 'A', 'confidence': {'level': 'high', 'reasons': []}}] * 3
        self.assertEqual(build_review_list(items), [])

    def test_low_returns_review_row(self):
        from ocr_review import build_review_list
        items = [
            {'name': '商品A', 'stock': 100, 'sales': 50,
             '_raw': {'stock': '100', 'sales': '50'},
             'confidence': {'level': 'low',
                            'reasons': ['双模型差异>30%或name配对异常']}},
        ]
        out = build_review_list(items)
        self.assertEqual(len(out), 1)
        r = out[0]
        self.assertEqual(r['index'], 0)
        self.assertEqual(r['name'], '商品A')
        self.assertEqual(r['level'], 'low')
        self.assertIn('双模型', r['reason'])

    def test_medium_returns_review_row(self):
        from ocr_review import build_review_list
        items = [
            {'name': 'A', 'confidence': {'level': 'medium',
                                         'reasons': ['缺少商品ID（依赖 name 模糊匹配）']}},
        ]
        out = build_review_list(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['level'], 'medium')

    def test_low_first_then_medium(self):
        from ocr_review import build_review_list
        items = [
            {'name': 'A', 'confidence': {'level': 'medium', 'reasons': []}},
            {'name': 'B', 'confidence': {'level': 'low', 'reasons': []}},
            {'name': 'C', 'confidence': {'level': 'medium', 'reasons': []}},
            {'name': 'D', 'confidence': {'level': 'low', 'reasons': []}},
        ]
        out = build_review_list(items)
        # 排序：low 在前，medium 在后；同 level 内按 index 升序
        self.assertEqual([r['index'] for r in out], [1, 3, 0, 2])
        self.assertEqual([r['level'] for r in out], ['low', 'low', 'medium', 'medium'])

    def test_numeric_anomaly_field_parsing(self):
        """数字异常 reason 形如 '数字异常(stock):销量>0 但库存=0' → 拆出 field。"""
        from ocr_review import build_review_list
        items = [
            {'name': 'A', 'stock': 0, 'sales': 200,
             '_raw': {'stock': '', 'sales': '200'},
             'confidence': {'level': 'low',
                            'reasons': ['数字异常(stock):销量>0 但库存=0（疑似漏识）']}},
        ]
        out = build_review_list(items)
        self.assertEqual(out[0]['field'], 'stock')
        self.assertIn('漏识', out[0]['reason'])
        self.assertEqual(out[0]['parsed'], 0)
        self.assertEqual(out[0]['raw'], '')

    def test_blur_reason_field_overall(self):
        from ocr_review import build_review_list
        items = [
            {'name': 'A', 'confidence': {'level': 'low',
                                         'reasons': ['图片模糊(Laplacian方差=35.0<100)']}},
        ]
        out = build_review_list(items)
        self.assertEqual(out[0]['field'], 'overall')
        self.assertIn('模糊', out[0]['reason'])

    def test_legacy_low_confidence_flag(self):
        """旧路径：_low_confidence=True 但无 confidence 字段 → 也进复核。"""
        from ocr_review import build_review_list
        items = [{'name': 'A', '_low_confidence': True, '_raw': {}}]
        out = build_review_list(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['level'], 'low')
        self.assertIn('双模型', out[0]['reason'])

    def test_legacy_name_unmatched(self):
        from ocr_review import build_review_list
        items = [{'name': 'A', '_name_unmatched': True, '_raw': {}}]
        out = build_review_list(items)
        self.assertEqual(out[0]['field'], 'name')

    def test_legacy_dual_degraded(self):
        from ocr_review import build_review_list
        items = [{'name': 'A', '_dual_degraded': True, '_raw': {}}]
        out = build_review_list(items)
        self.assertEqual(out[0]['level'], 'low')

    def test_legacy_missing_id_only(self):
        from ocr_review import build_review_list
        items = [{'name': 'A', '_missing_id': True, '_raw': {}}]
        out = build_review_list(items)
        # _missing_id 单独走 medium
        self.assertEqual(out[0]['level'], 'medium')
        self.assertIn('商品 ID', out[0]['reason'])

    def test_name_truncated(self):
        from ocr_review import build_review_list
        long_name = '商品' * 30  # 60 字
        items = [{'name': long_name, 'confidence': {'level': 'low', 'reasons': ['x']}}]
        out = build_review_list(items)
        self.assertLessEqual(len(out[0]['name']), 40)

    def test_raw_truncated_to_60(self):
        from ocr_review import build_review_list
        long_raw = 'X' * 200
        items = [{
            'name': 'A', 'stock': 0,
            '_raw': {'stock': long_raw},
            'confidence': {'level': 'low', 'reasons': ['数字异常(stock):abc']},
        }]
        out = build_review_list(items)
        self.assertLessEqual(len(out[0]['raw']), 60)

    def test_non_dict_items_skipped(self):
        from ocr_review import build_review_list
        items = ['str', None, {'name': 'A', 'confidence': {'level': 'low', 'reasons': ['x']}}]
        out = build_review_list(items)
        # 不抛；只 dict 进 review
        self.assertEqual(len(out), 1)

    def test_multiple_reasons_joined(self):
        from ocr_review import build_review_list
        items = [{
            'name': 'A', 'stock': 0, 'sales': 200,
            '_raw': {'stock': '', 'sales': '200'},
            'confidence': {'level': 'low', 'reasons': [
                '数字异常(stock):销量>0 但库存=0（疑似漏识）',
                '双模型差异>30%或name配对异常',
            ]},
        }]
        out = build_review_list(items)
        # 多 reason 用 ； 合并
        self.assertIn('；', out[0]['reason'])
        self.assertIn('双模型', out[0]['reason'])


class TestApplyUserEdits(unittest.TestCase):
    """apply_user_edits：白名单 + 越界 + 解析失败 in-place 写回。"""

    def test_empty_items(self):
        from ocr_review import apply_user_edits
        self.assertEqual(apply_user_edits([], [{'index': 0, 'field': 'stock', 'value': 100}]), [])

    def test_empty_edits(self):
        from ocr_review import apply_user_edits
        items = [{'stock': 100}]
        out = apply_user_edits(items, [])
        self.assertIs(out, items)
        self.assertEqual(items[0]['stock'], 100)

    def test_modify_stock(self):
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100, 'sales': 50}]
        apply_user_edits(items, [{'index': 0, 'field': 'stock', 'value': 200}])
        self.assertEqual(items[0]['stock'], 200)

    def test_modify_sales(self):
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100, 'sales': 50}]
        apply_user_edits(items, [{'index': 0, 'field': 'sales', 'value': 999}])
        self.assertEqual(items[0]['sales'], 999)

    def test_modify_name(self):
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100, 'sales': 50}]
        apply_user_edits(items, [{'index': 0, 'field': 'name', 'value': 'B'}])
        self.assertEqual(items[0]['name'], 'B')

    def test_stock_string_parsed(self):
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100, 'sales': 50}]
        apply_user_edits(items, [{'index': 0, 'field': 'stock', 'value': '200'}])
        self.assertEqual(items[0]['stock'], 200)

    def test_stock_unparseable_skipped(self):
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100, 'sales': 50}]
        apply_user_edits(items, [{'index': 0, 'field': 'stock', 'value': 'abc'}])
        # 解析失败 → 不改
        self.assertEqual(items[0]['stock'], 100)

    def test_stock_empty_sets_zero(self):
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100, 'sales': 50}]
        apply_user_edits(items, [{'index': 0, 'field': 'stock', 'value': ''}])
        self.assertEqual(items[0]['stock'], 0)

    def test_field_not_whitelisted_skipped(self):
        from ocr_review import apply_user_edits
        # _internal_field 不在白名单 (白名单: stock/sales/name/region/warehouse)
        items = [{'name': 'A', 'stock': 100, 'sales': 50, '_internal_field': 'original'}]
        apply_user_edits(items, [{'index': 0, 'field': '_internal_field', 'value': 'hacked'}])
        # _internal_field 不在白名单 → 不改
        self.assertEqual(items[0]['_internal_field'], 'original')

    def test_internal_flag_not_modifiable(self):
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100, '_low_confidence': True}]
        apply_user_edits(items, [{'index': 0, 'field': '_low_confidence', 'value': False}])
        self.assertTrue(items[0]['_low_confidence'])

    def test_index_out_of_range_skipped(self):
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100}]
        apply_user_edits(items, [
            {'index': 5, 'field': 'stock', 'value': 200},  # 越界
            {'index': -1, 'field': 'stock', 'value': 300},  # 负
        ])
        self.assertEqual(items[0]['stock'], 100)

    def test_non_dict_items_skipped(self):
        from ocr_review import apply_user_edits
        items = ['str', None]
        # 不抛
        apply_user_edits(items, [{'index': 0, 'field': 'stock', 'value': 200}])
        apply_user_edits(items, [{'index': 1, 'field': 'stock', 'value': 200}])

    def test_returns_same_list(self):
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100}]
        out = apply_user_edits(items, [{'index': 0, 'field': 'stock', 'value': 200}])
        self.assertIs(out, items)

    def test_multiple_edits(self):
        from ocr_review import apply_user_edits
        items = [
            {'name': 'A', 'stock': 100, 'sales': 50},
            {'name': 'B', 'stock': 200, 'sales': 80},
        ]
        apply_user_edits(items, [
            {'index': 0, 'field': 'stock', 'value': 150},
            {'index': 1, 'field': 'sales', 'value': 999},
        ])
        self.assertEqual(items[0]['stock'], 150)
        self.assertEqual(items[1]['sales'], 999)


class TestHasLowConfidence(unittest.TestCase):
    """has_low_confidence / has_review_items 快路径。"""

    def test_empty(self):
        from ocr_review import has_low_confidence, has_review_items
        self.assertFalse(has_low_confidence([]))
        self.assertFalse(has_review_items([]))
        self.assertFalse(has_low_confidence(None))  # type: ignore
        self.assertFalse(has_review_items(None))  # type: ignore

    def test_all_high(self):
        from ocr_review import has_low_confidence, has_review_items
        items = [{'confidence': {'level': 'high', 'reasons': []}}] * 3
        self.assertFalse(has_low_confidence(items))
        self.assertFalse(has_review_items(items))

    def test_has_low(self):
        from ocr_review import has_low_confidence, has_review_items
        items = [
            {'confidence': {'level': 'high', 'reasons': []}},
            {'confidence': {'level': 'low', 'reasons': []}},
        ]
        self.assertTrue(has_low_confidence(items))
        self.assertTrue(has_review_items(items))

    def test_only_medium_triggers_review_not_low(self):
        from ocr_review import has_low_confidence, has_review_items
        items = [{'confidence': {'level': 'medium', 'reasons': []}}]
        self.assertFalse(has_low_confidence(items))
        self.assertTrue(has_review_items(items))

    def test_legacy_flag(self):
        from ocr_review import has_low_confidence
        items = [{'_low_confidence': True}]
        self.assertTrue(has_low_confidence(items))


class TestReviewFlowIntegration(unittest.TestCase):
    """build_review_list + apply_user_edits 集成：模拟"用户接受若干修正后"的场景。"""

    def test_user_edits_remove_low_confidence(self):
        """用户修正后：把 _low_confidence 清掉（虽然不通过 apply_user_edits，模拟外部重置）。"""
        from ocr_review import build_review_list
        items = [
            {'name': 'A', 'stock': 0, 'sales': 200,
             '_raw': {'stock': '', 'sales': '200'},
             'confidence': {'level': 'low',
                            'reasons': ['数字异常(stock):销量>0 但库存=0']}},
        ]
        out = build_review_list(items)
        self.assertEqual(len(out), 1)
        # 模拟用户改 stock 后，重置 confidence.level 为 high
        items[0]['stock'] = 50
        items[0]['confidence'] = {'level': 'high', 'reasons': []}
        out2 = build_review_list(items)
        self.assertEqual(out2, [])

    def test_pure_chain_no_tk(self):
        """端到端纯函数链：classify → review → apply → reclassify 全程不依赖 Tk。"""
        from ocr_review import categorize_error, build_review_list, apply_user_edits, has_review_items
        # 1) 异常归类
        cat, msg, _ = categorize_error('CSV 编码失败')
        self.assertEqual(cat, 'csv_encoding')
        # 2) 构建复核清单
        items = [
            {'name': 'A', 'stock': 100, 'sales': 50,
             'confidence': {'level': 'low',
                            'reasons': ['数字异常(stock):abc']},
             '_raw': {'stock': 'XXX', 'sales': '50'}},
        ]
        rows = build_review_list(items)
        self.assertEqual(len(rows), 1)
        # 3) 应用修正
        apply_user_edits(items, [{'index': 0, 'field': 'stock', 'value': 150}])
        # 4) 重新评估
        items[0]['confidence'] = {'level': 'high', 'reasons': []}
        self.assertFalse(has_review_items(items))


if __name__ == '__main__':
    unittest.main(verbosity=2)
