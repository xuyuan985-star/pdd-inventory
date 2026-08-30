"""PDD EZ — OCR 置信度引擎单元测试（v1.4.8 P2-OCR t3）

覆盖：
  - detect_blur：清晰图（黑字白底）→ not blur；高斯模糊图 → blur；
    阈值常量调参；异常路径（路径不存在 / 非法类型 / 空字符串）→ (False, 0) 不抛。
  - audit_numeric_fields：原文字符残留 / 销售>0 库存=0 / 量级怪异 / 解析为 0
    但原文字段含数字；多字段异常聚合；无异常输入 → []。
  - build_confidence_meta：优先级 low>medium>high；多原因合并；blur 共享；
    已有 _low_confidence/_name_unmatched/_dual_degraded 标记纳入；
    _missing_id 单独走 medium。
  - 全部失败路径不抛（§4 失败哲学）。

不依赖真实 API / GUI / 数据库；只动 stdlib + opencv-python + numpy + PIL
（项目已有依赖，见 requirements.txt）。
"""
import os
import sys
import tempfile
import shutil
import unittest
from typing import List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _make_sharp_image(path: str, size=(400, 200)) -> None:
    """合成清晰图：大号黑字白底（强对比，文字边缘锐利，OpenCV Laplacian 方差应 >> 100）。"""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new('RGB', size, (255, 255, 255))
    draw = ImageDraw.Draw(img)
    text = '示例商品A500g'
    # 用默认字体（无 ttf 依赖，跨平台稳定）
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None
    # 画横线（边缘锐利）增加方差
    for y in range(20, size[1] - 20, 4):
        draw.line([(10, y), (size[0] - 10, y)], fill=(0, 0, 0), width=2)
    # 大字
    draw.text((20, 80), text, fill=(0, 0, 0), font=font)
    img.save(path, 'PNG')


def _make_blurry_image(path: str, size=(400, 200)) -> None:
    """合成高斯模糊图：先把清晰图重采样到很小再放大（快速近似高斯模糊），方差应 << 100。"""
    from PIL import Image, ImageFilter
    img = Image.new('RGB', size, (255, 255, 255))
    # 先画些清晰边缘再模糊
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    for y in range(20, size[1] - 20, 4):
        draw.line([(10, y), (size[0] - 10, y)], fill=(0, 0, 0), width=2)
    draw.text((20, 80), '示例商品A500g', fill=(0, 0, 0))
    # 重采样到 1/16 再放大 → 边缘完全糊掉
    small = img.resize((max(1, size[0] // 16), max(1, size[1] // 16)), Image.BILINEAR)
    big = small.resize(size, Image.BILINEAR)
    # 进一步高斯模糊确保
    big = big.filter(ImageFilter.GaussianBlur(radius=4))
    big.save(path, 'PNG')


class TestDetectBlur(unittest.TestCase):
    """detect_blur：清晰图不模糊，模糊图模糊，异常输入不抛。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sharp_image_not_blur(self):
        from ocr import detect_blur
        p = os.path.join(self.tmp, 'sharp.png')
        _make_sharp_image(p)
        is_blur, var = detect_blur(p)
        self.assertFalse(is_blur, f'清晰图不应判定模糊，但 var={var}')
        self.assertGreater(var, 0.0)

    def test_blurry_image_is_blur(self):
        from ocr import detect_blur
        p = os.path.join(self.tmp, 'blurry.png')
        _make_blurry_image(p)
        is_blur, var = detect_blur(p)
        self.assertTrue(is_blur, f'高斯模糊图应判定模糊，但 var={var}')
        self.assertLess(var, 100.0)

    def test_threshold_constant_tuning(self):
        """BLUR_VAR_THRESHOLD 常量：调高到 999999 → 清晰图也变 blur。"""
        from ocr import detect_blur, BLUR_VAR_THRESHOLD
        orig = BLUR_VAR_THRESHOLD
        try:
            # 临时把模块常量改大（用 monkey-patch 等价：直接覆盖 import 后的引用）
            import ocr
            ocr.BLUR_VAR_THRESHOLD = 999999
            p = os.path.join(self.tmp, 'sharp.png')
            _make_sharp_image(p)
            is_blur, var = detect_blur(p)
            self.assertTrue(is_blur, '调高阈值后清晰图也应被判定模糊')
        finally:
            ocr.BLUR_VAR_THRESHOLD = orig

    def test_nonexistent_path_returns_safe(self):
        from ocr import detect_blur
        is_blur, var = detect_blur(os.path.join(self.tmp, 'no_such_file.png'))
        self.assertFalse(is_blur)
        self.assertEqual(var, 0.0)

    def test_empty_path_returns_safe(self):
        from ocr import detect_blur
        is_blur, var = detect_blur('')
        self.assertFalse(is_blur)
        self.assertEqual(var, 0.0)

    def test_none_path_returns_safe(self):
        from ocr import detect_blur
        is_blur, var = detect_blur(None)
        self.assertFalse(is_blur)
        self.assertEqual(var, 0.0)

    def test_non_image_file_returns_safe(self):
        """非图像文件（纯文本）→ cv2.imread 返 None → (False, 0)，不抛。"""
        from ocr import detect_blur
        p = os.path.join(self.tmp, 'not_image.png')
        with open(p, 'w', encoding='utf-8') as f:
            f.write('this is not an image')
        is_blur, var = detect_blur(p)
        self.assertFalse(is_blur)
        self.assertEqual(var, 0.0)


class TestAuditNumericFields(unittest.TestCase):
    """audit_numeric_fields：各异常用例 + 无异常 → []。"""

    def test_clean_items_no_issues(self):
        from ocr import audit_numeric_fields
        items = [
            {'name': 'A', 'stock': 100, 'sales': 50, '_raw': {'stock': '100', 'sales': '50'}},
            {'name': 'B', 'stock': 0, 'sales': 0, '_raw': {'stock': '0', 'sales': '0'}},
        ]
        self.assertEqual(audit_numeric_fields(items), [])

    def test_sales_positive_stock_zero_suspected_miss(self):
        from ocr import audit_numeric_fields
        items = [
            {'name': 'A', 'stock': 0, 'sales': 200, '_raw': {'stock': '', 'sales': '200'}},
        ]
        issues = audit_numeric_fields(items)
        # 至少一条 stock 的"销量>0 但库存=0"
        _stock_issues = [i for i in issues if i[1] == 'stock' and '漏识' in i[2]]
        self.assertGreater(len(_stock_issues), 0)
        idx, field, reason, raw, parsed = _stock_issues[0]
        self.assertEqual(idx, 0)
        self.assertEqual(field, 'stock')
        self.assertEqual(parsed, 0)

    def test_absurd_value_too_large(self):
        from ocr import audit_numeric_fields
        items = [
            {'name': 'A', 'stock': 200, 'sales': 9999999, '_raw': {'stock': '200', 'sales': '9999999'}},
        ]
        issues = audit_numeric_fields(items)
        _absurd = [i for i in issues if '999999' in i[2] or '量级' in i[2] or '串列' in i[2]]
        self.assertGreater(len(_absurd), 0)
        self.assertEqual(_absurd[0][1], 'sales')

    def test_negative_value(self):
        from ocr import audit_numeric_fields
        items = [
            {'name': 'A', 'stock': -5, 'sales': 0, '_raw': {'stock': '-5', 'sales': '0'}},
        ]
        issues = audit_numeric_fields(items)
        _neg = [i for i in issues if i[1] == 'stock' and '负' in i[2]]
        self.assertGreater(len(_neg), 0)

    def test_parsed_zero_but_raw_has_number(self):
        from ocr import audit_numeric_fields
        items = [
            {'name': 'A', 'stock': 0, 'sales': 0,
             '_raw': {'stock': '???', 'sales': '300'}},
        ]
        issues = audit_numeric_fields(items)
        # 原文 '???' 没有数字 → 不应报 sales 矛盾（sales 解析正确 0 原文没数字 合理）
        # 关键：sales raw='300', parsed=0 矛盾 → 至少 1 条
        _parsed_zero = [i for i in issues if '解析为 0' in i[2]]
        self.assertGreater(len(_parsed_zero), 0)

    def test_residual_garbage_chars_in_raw(self):
        from ocr import audit_numeric_fields
        items = [
            {'name': 'A', 'stock': 128, 'sales': 50,
             '_raw': {'stock': '128xyz', 'sales': '50份'}},  # 128xyz 有怪字符，50份是常见单位
        ]
        issues = audit_numeric_fields(items)
        _resid = [i for i in issues if '残留' in i[2] and i[1] == 'stock']
        self.assertGreater(len(_resid), 0, f'应报 stock 残留，issues={issues}')

    def test_common_unit_not_flagged(self):
        from ocr import audit_numeric_fields
        items = [
            {'name': 'A', 'stock': 128, 'sales': 50,
             '_raw': {'stock': '128份', 'sales': '50件'}},
        ]
        issues = audit_numeric_fields(items)
        _resid = [i for i in issues if '残留' in i[2]]
        self.assertEqual(_resid, [], f'常见单位不应被报，issues={issues}')

    def test_index_returned_correctly(self):
        from ocr import audit_numeric_fields
        items = [
            {'name': 'A', 'stock': 0, 'sales': 0, '_raw': {'stock': '', 'sales': ''}},  # index 0 干净
            {'name': 'B', 'stock': 0, 'sales': 200, '_raw': {'stock': '', 'sales': '200'}},  # index 1 异常
        ]
        issues = audit_numeric_fields(items)
        _stock_issues = [i for i in issues if i[1] == 'stock']
        self.assertTrue(all(i[0] == 1 for i in _stock_issues),
                        f'异常 index 应=1，实际: {[(i[0], i[2]) for i in _stock_issues]}')

    def test_empty_items(self):
        from ocr import audit_numeric_fields
        self.assertEqual(audit_numeric_fields([]), [])

    def test_non_dict_items_skipped(self):
        from ocr import audit_numeric_fields
        items = ['not a dict', None, {'name': 'A', 'stock': 100, 'sales': 50, '_raw': {}}]
        issues = audit_numeric_fields(items)
        # 不应抛，且只对正常 dict 审计
        self.assertIsInstance(issues, list)

    def test_raw_missing_does_not_crash(self):
        from ocr import audit_numeric_fields
        items = [
            {'name': 'A', 'stock': 100, 'sales': 50},  # 无 _raw
        ]
        # 不应抛；干净数据 → 无 issues
        self.assertEqual(audit_numeric_fields(items), [])

    def test_non_int_parsed_coerced(self):
        from ocr import audit_numeric_fields
        items = [
            {'name': 'A', 'stock': '100', 'sales': 50.5, '_raw': {'stock': '100', 'sales': '50.5'}},
        ]
        # 不应抛
        issues = audit_numeric_fields(items)
        self.assertIsInstance(issues, list)


class TestBuildConfidenceMeta(unittest.TestCase):
    """build_confidence_meta：优先级 + 多原因合并 + blur 共享 + 已有标记纳入。"""

    def test_empty_items_returns_empty(self):
        from ocr import build_confidence_meta
        self.assertEqual(build_confidence_meta([]), [])
        # None / 空 → 都返回空 list（不抛）
        self.assertEqual(build_confidence_meta(None), [])  # type: ignore

    def test_clean_items_high(self):
        from ocr import build_confidence_meta
        items = [
            {'name': 'A', 'stock': 100, 'sales': 50, '_raw': {'stock': '100', 'sales': '50'}},
        ]
        out = build_confidence_meta(items)
        self.assertEqual(len(out), 1)
        c = out[0]['confidence']
        self.assertEqual(c['level'], 'high')
        self.assertEqual(c['reasons'], [])

    def test_low_confidence_flag_becomes_low(self):
        from ocr import build_confidence_meta
        items = [{'name': 'A', 'stock': 100, 'sales': 50, '_low_confidence': True,
                  '_raw': {'stock': '100', 'sales': '50'}}]
        out = build_confidence_meta(items)
        self.assertEqual(out[0]['confidence']['level'], 'low')
        self.assertTrue(any('双模型' in r for r in out[0]['confidence']['reasons']))

    def test_name_unmapped_flag_becomes_low(self):
        from ocr import build_confidence_meta
        items = [{'name': 'A', 'stock': 100, 'sales': 50, '_name_unmatched': True,
                  '_raw': {'stock': '100', 'sales': '50'}}]
        out = build_confidence_meta(items)
        self.assertEqual(out[0]['confidence']['level'], 'low')

    def test_dual_degraded_becomes_low(self):
        from ocr import build_confidence_meta
        items = [{'name': 'A', 'stock': 100, 'sales': 50, '_dual_degraded': True,
                  '_raw': {'stock': '100', 'sales': '50'}}]
        out = build_confidence_meta(items)
        self.assertEqual(out[0]['confidence']['level'], 'low')
        self.assertTrue(any('双模型校验' in r for r in out[0]['confidence']['reasons']))

    def test_numeric_anomaly_becomes_low(self):
        from ocr import build_confidence_meta
        items = [{'name': 'A', 'stock': 0, 'sales': 200,
                  '_raw': {'stock': '', 'sales': '200'}}]
        out = build_confidence_meta(items)
        self.assertEqual(out[0]['confidence']['level'], 'low')
        self.assertTrue(any('数字异常' in r for r in out[0]['confidence']['reasons']))

    def test_blur_shared_across_all_items(self):
        from ocr import build_confidence_meta
        items = [
            {'name': 'A', 'stock': 100, 'sales': 50, '_raw': {'stock': '100', 'sales': '50'}},
            {'name': 'B', 'stock': 200, 'sales': 80, '_raw': {'stock': '200', 'sales': '80'}},
        ]
        out = build_confidence_meta(items, blur_info=(True, 35.0))
        for it in out:
            self.assertEqual(it['confidence']['level'], 'low')
            self.assertTrue(any('模糊' in r for r in it['confidence']['reasons']))

    def test_blur_info_none_skips_blur(self):
        from ocr import build_confidence_meta
        items = [{'name': 'A', 'stock': 100, 'sales': 50, '_raw': {}}]
        out = build_confidence_meta(items, blur_info=None)
        self.assertEqual(out[0]['confidence']['level'], 'high')

    def test_blur_info_not_blur_skips_reason(self):
        from ocr import build_confidence_meta
        items = [{'name': 'A', 'stock': 100, 'sales': 50, '_raw': {}}]
        out = build_confidence_meta(items, blur_info=(False, 500.0))
        self.assertEqual(out[0]['confidence']['level'], 'high')
        self.assertNotIn('模糊', '|'.join(out[0]['confidence']['reasons']))

    def test_missing_id_alone_becomes_medium(self):
        from ocr import build_confidence_meta
        items = [{'name': 'A', 'stock': 100, 'sales': 50, '_missing_id': True,
                  '_raw': {}}]
        out = build_confidence_meta(items)
        self.assertEqual(out[0]['confidence']['level'], 'medium')
        self.assertTrue(any('商品ID' in r for r in out[0]['confidence']['reasons']))

    def test_multiple_reasons_concatenated(self):
        from ocr import build_confidence_meta
        items = [{'name': 'A', 'stock': 0, 'sales': 200,
                  '_low_confidence': True, '_name_unmatched': True,
                  '_raw': {'stock': '', 'sales': '200'}}]
        out = build_confidence_meta(items)
        c = out[0]['confidence']
        self.assertEqual(c['level'], 'low')
        # 至少 3 条原因：双模型差异 + 名字配对 + 数字异常
        self.assertGreaterEqual(len(c['reasons']), 3)

    def test_priority_low_over_medium(self):
        from ocr import build_confidence_meta
        # _missing_id（medium）+ _dual_degraded（low）→ 最终 low
        items = [{'name': 'A', 'stock': 100, 'sales': 50,
                  '_missing_id': True, '_dual_degraded': True, '_raw': {}}]
        out = build_confidence_meta(items)
        self.assertEqual(out[0]['confidence']['level'], 'low')

    def test_returns_same_list_reference(self):
        """in-place 改 items 并返回同一引用（链式友好）。"""
        from ocr import build_confidence_meta
        items = [{'name': 'A', 'stock': 100, 'sales': 50, '_raw': {}}]
        out = build_confidence_meta(items)
        self.assertIs(out, items)
        self.assertIn('confidence', items[0])

    def test_non_dict_items_skipped(self):
        from ocr import build_confidence_meta
        items = ['str', None, {'name': 'A', 'stock': 100, 'sales': 50, '_raw': {}}]
        out = build_confidence_meta(items)
        # 不抛
        self.assertEqual(len(out), 3)
        # 字典那一个应有 confidence
        self.assertIn('confidence', out[2])

    def test_reasons_are_strings(self):
        from ocr import build_confidence_meta
        items = [{'name': 'A', 'stock': 0, 'sales': 200,
                  '_raw': {'stock': '', 'sales': '200'}}]
        out = build_confidence_meta(items)
        for r in out[0]['confidence']['reasons']:
            self.assertIsInstance(r, str)


class TestUserMsgConstants(unittest.TestCase):
    """中文容错文案常量存在且非空（供 t8 弹窗/日志统一消费）。"""

    def test_user_msg_constants_defined(self):
        import ocr
        required = [
            'USER_MSG_BLUR', 'USER_MSG_NUMERIC_ANOMALY', 'USER_MSG_DUAL_DEGRADED',
            'USER_MSG_NAME_UNMATCHED', 'USER_MSG_LOW_CONFIDENCE',
            'USER_MSG_API_KEY_MISSING', 'USER_MSG_API_TIMEOUT',
            'USER_MSG_JSON_PARSE_FAIL', 'USER_MSG_NO_MODEL_AVAILABLE',
            'USER_MSG_FATAL_QUOTA', 'USER_MSG_CSV_ENCODING', 'USER_MSG_XLSX_CORRUPT',
            'USER_MSG_IMPORT_TOO_LARGE', 'USER_MSG_LEGACY_XLS', 'USER_MSG_MAPPING_MISSING',
        ]
        for name in required:
            self.assertTrue(hasattr(ocr, name), f'缺少常量: {name}')
            v = getattr(ocr, name)
            self.assertIsInstance(v, str)
            self.assertGreater(len(v), 0, f'常量 {name} 为空')
            # 至少 4 个字符，且不是 raw stacktrace 类（不应含 '{' 或 'Traceback'）
            self.assertNotIn('Traceback', v)
            self.assertNotIn('{', v)

    def test_threshold_constants(self):
        import ocr
        self.assertIsInstance(ocr.BLUR_VAR_THRESHOLD, int)
        self.assertEqual(ocr.BLUR_VAR_THRESHOLD, 100)
        self.assertIsInstance(ocr.NUMERIC_ABSURD_MAX, int)
        self.assertGreater(ocr.NUMERIC_ABSURD_MAX, 0)


class TestAPIPublicContract(unittest.TestCase):
    """公开 API 完整性 + 签名契约。"""

    def test_public_api_callable(self):
        import ocr
        for fn in ('detect_blur', 'audit_numeric_fields', 'build_confidence_meta'):
            self.assertTrue(callable(getattr(ocr, fn, None)), f'{fn} 不可调用')

    def test_detect_blur_signature(self):
        """detect_blur 接受 1 位置参数，返回 (bool, float) 二元组。"""
        import ocr
        r = ocr.detect_blur('nonexistent.png')
        self.assertIsInstance(r, tuple)
        self.assertEqual(len(r), 2)
        self.assertIsInstance(r[0], bool)
        self.assertIsInstance(r[1], float)

    def test_audit_returns_list(self):
        import ocr
        r = ocr.audit_numeric_fields([{'name': 'A', 'stock': 100, 'sales': 50, '_raw': {}}])
        self.assertIsInstance(r, list)


if __name__ == '__main__':
    unittest.main(verbosity=2)
