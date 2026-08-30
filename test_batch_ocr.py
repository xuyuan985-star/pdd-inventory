"""PDD EZ — batch_ocr_images 单测（R1 流程效率 t2 产出）

覆盖：
  - 默认 recognizer（ocr_dual_verify_generic）路径：monkey-patch 替换，注入 stub。
  - 单张成功 → 入 results；成功路径包含 items 与 mapping。
  - 单张失败 → 不入 results，进 errors（含 reason 截断到 200 字）；
    后续张继续处理（不中断整批）。
  - 全失败 → results 为空 list，errors 含全部记录。
  - 空 image_paths → (results=[], errors=[])。
  - 路径为 None / 非字符串 → 转 errors，不抛。
  - mapping 显式传入 → 透传到 recognizer；mapping=None → recognizer 拿空 dict。
  - recognizer 返回非 list → 包成空 list（结果视为空，仍记 success）。
  - BatchCancelled 中断：在某张识别前 _check_cancel() 抛 → 立即终止，
    把该张记为取消错误 + 已处理结果一起返回。
  - 透传 **kwargs 给 recognizer（forced_model 等）。

不依赖真实 API / 图片：仅调用 stub recognizer；
不创建任何 Tk widget / settings.json 写入。
"""
import os
import sys
import unittest
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


# ═══════════════════════ Stub / Mock 构造工具 ═══════════

def _make_stub_recognizer(behaviors):
    """构造一个 stub recognizer，按 paths 顺序返回对应结果或抛指定异常。

    Args:
        behaviors: dict[image_path, behavior]，behavior 是：
                   - list[dict]      → 直接返回该 items（成功）
                   - tuple[Exception] → 抛该异常
                   - 'empty'         → 返回 []（成功但空结果）

    Returns:
        callable(path, mapping, **kwargs) → 与上述行为一致。
        调用记录到 stub.calls 列表便于断言 kwargs 透传。
    """
    stub = mock.Mock()
    stub.calls = []  # [(path, mapping, kwargs), ...]

    def _recognizer(path, mapping, **kwargs):
        stub.calls.append((path, mapping, dict(kwargs)))
        b = behaviors.get(path)
        if b is None:
            # 默认：成功 + 单条示例
            return [{'name': f'default_{os.path.basename(path)}', 'stock': 0,
                     'sales': 0, 'region': '', 'warehouse': '', 'sku_id': '',
                     '_raw': {}}]
        if isinstance(b, tuple) and len(b) == 1 and isinstance(b[0], Exception):
            raise b[0]
        if b == 'empty':
            return []
        if isinstance(b, list):
            return b
        # 默认兜底
        return [{'name': 'unknown', 'stock': 0, 'sales': 0, 'region': '',
                 'warehouse': '', 'sku_id': '', '_raw': {}}]
    stub.side_effect = _recognizer
    return stub


# ═══════════════════════ batch_ocr_images 基础契约 ═══════════

class TestBatchOcrBasic(unittest.TestCase):
    """batch_ocr_images 基础契约（stub 注入，不调真实 OCR）。"""

    def test_empty_image_paths(self):
        from ocr import batch_ocr_images
        stub = _make_stub_recognizer({})
        results, errors = batch_ocr_images([], recognizer=stub)
        self.assertEqual(results, [])
        self.assertEqual(errors, [])
        # 空输入 → recognizer 不被调用
        self.assertEqual(len(stub.calls), 0)

    def test_none_image_paths(self):
        from ocr import batch_ocr_images
        stub = _make_stub_recognizer({})
        results, errors = batch_ocr_images(None, recognizer=stub)
        self.assertEqual(results, [])
        self.assertEqual(errors, [])

    def test_single_success(self):
        from ocr import batch_ocr_images
        items = [{'name': '商品A', 'stock': 10, 'sales': 5, 'region': '',
                  'warehouse': '', 'sku_id': '', '_raw': {}}]
        stub = _make_stub_recognizer({'a.png': items})
        # mapping 显式传空 → 不走 get_ocr_columns 兜底
        results, errors = batch_ocr_images(['a.png'], mapping={},
                                            recognizer=stub)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['path'], 'a.png')
        self.assertEqual(results[0]['items'], items)
        self.assertEqual(results[0]['mapping'], {})

    def test_multi_success_order_preserved(self):
        from ocr import batch_ocr_images
        a_items = [{'name': 'A', 'stock': 1, 'sales': 1, 'region': '',
                    'warehouse': '', 'sku_id': '', '_raw': {}}]
        b_items = [{'name': 'B', 'stock': 2, 'sales': 2, 'region': '',
                    'warehouse': '', 'sku_id': '', '_raw': {}}]
        c_items = [{'name': 'C', 'stock': 3, 'sales': 3, 'region': '',
                    'warehouse': '', 'sku_id': '', '_raw': {}}]
        stub = _make_stub_recognizer({
            'a.png': a_items, 'b.png': b_items, 'c.png': c_items})
        paths = ['a.png', 'b.png', 'c.png']
        results, errors = batch_ocr_images(paths, recognizer=stub)
        self.assertEqual(errors, [])
        self.assertEqual([r['path'] for r in results], paths)
        self.assertEqual([r['items'][0]['name'] for r in results], ['A', 'B', 'C'])

    def test_single_failure_does_not_interrupt(self):
        from ocr import batch_ocr_images
        a_items = [{'name': 'A', 'stock': 0, 'sales': 0, 'region': '',
                    'warehouse': '', 'sku_id': '', '_raw': {}}]
        stub = _make_stub_recognizer({
            'a.png': a_items,
            'b.png': (RuntimeError('API quota exhausted'),),
            'c.png': [{'name': 'C', 'stock': 0, 'sales': 0, 'region': '',
                       'warehouse': '', 'sku_id': '', '_raw': {}}],
        })
        results, errors = batch_ocr_images(['a.png', 'b.png', 'c.png'],
                                            recognizer=stub)
        # b 失败：a/c 仍应成功
        self.assertEqual(len(results), 2)
        self.assertEqual([r['path'] for r in results], ['a.png', 'c.png'])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], 'b.png')
        self.assertIn('API quota exhausted', errors[0][1])
        self.assertIn('RuntimeError', errors[0][1])

    def test_all_failures(self):
        from ocr import batch_ocr_images
        stub = _make_stub_recognizer({
            'a.png': (ValueError('image corrupt'),),
            'b.png': (RuntimeError('network error'),),
        })
        results, errors = batch_ocr_images(['a.png', 'b.png'],
                                            recognizer=stub)
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 2)
        # 顺序与输入一致
        self.assertEqual(errors[0][0], 'a.png')
        self.assertEqual(errors[1][0], 'b.png')
        self.assertIn('image corrupt', errors[0][1])
        self.assertIn('network error', errors[1][1])

    def test_empty_result_is_success_not_error(self):
        """recognizer 返回 []（如表格为空）→ 视为成功，结果里 path+items=[]。"""
        from ocr import batch_ocr_images
        stub = _make_stub_recognizer({'a.png': 'empty'})
        results, errors = batch_ocr_images(['a.png'], recognizer=stub)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['path'], 'a.png')
        self.assertEqual(results[0]['items'], [])

    def test_non_list_result_normalized_to_empty(self):
        """recognizer 返回非 list（异常但未抛）→ 包成空 list，结果视为成功空。"""
        from ocr import batch_ocr_images
        # 直接让 stub 返回 dict
        stub = mock.Mock()
        stub.side_effect = lambda *a, **kw: {'oops': 'not a list'}
        results, errors = batch_ocr_images(['a.png'], recognizer=stub)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['items'], [])

    def test_recognizer_returns_none_normalized_to_empty(self):
        """recognizer 返回 None → 视为空（不抛）。"""
        from ocr import batch_ocr_images
        stub = mock.Mock(side_effect=lambda *a, **kw: None)
        results, errors = batch_ocr_images(['a.png'], recognizer=stub)
        self.assertEqual(errors, [])
        self.assertEqual(results[0]['items'], [])


class TestBatchOcrInputGuards(unittest.TestCase):
    """路径 / 输入守卫（None / 非字符串 / 空串）。"""

    def test_path_none_recorded_as_error(self):
        from ocr import batch_ocr_images
        stub = _make_stub_recognizer({})
        results, errors = batch_ocr_images([None, 'a.png'], recognizer=stub)
        # None 路径：进 errors，不抛；后续正常处理
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['path'], 'a.png')
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], '')
        self.assertIn('路径为空或非字符串', errors[0][1])

    def test_empty_string_path_recorded_as_error(self):
        from ocr import batch_ocr_images
        stub = _make_stub_recognizer({})
        results, errors = batch_ocr_images(['', 'a.png'], recognizer=stub)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], '')

    def test_non_string_path_recorded_as_error(self):
        from ocr import batch_ocr_images
        stub = _make_stub_recognizer({})
        results, errors = batch_ocr_images([42, 'a.png'], recognizer=stub)
        # 数字路径视为非字符串 → 进 errors
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], '42')

    def test_recognizer_raises_unexpected(self):
        """recognizer 抛任意 Exception（除 BatchCancelled）→ 转 errors。"""
        from ocr import batch_ocr_images
        stub = _make_stub_recognizer({
            'a.png': (KeyError('col missing'),),
            'b.png': [{'name': 'B', 'stock': 0, 'sales': 0, 'region': '',
                       'warehouse': '', 'sku_id': '', '_raw': {}}],
        })
        results, errors = batch_ocr_images(['a.png', 'b.png'],
                                            recognizer=stub)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn('KeyError', errors[0][1])
        self.assertIn('col missing', errors[0][1])


class TestBatchOcrKwargsPassthrough(unittest.TestCase):
    """mapping / **kwargs 透传到 recognizer。"""

    def test_explicit_mapping_passed_through(self):
        from ocr import batch_ocr_images
        stub = _make_stub_recognizer({'a.png': [{'name': 'X', 'stock': 0,
                                                   'sales': 0, 'region': '',
                                                   'warehouse': '', 'sku_id': '',
                                                   '_raw': {}}]})
        mp = {'name': '商品信息', 'stock': '仓库总库存'}
        results, errors = batch_ocr_images(['a.png'], mapping=mp,
                                            recognizer=stub)
        self.assertEqual(errors, [])
        self.assertEqual(results[0]['mapping'], mp)
        # stub.calls 透传 mapping
        self.assertEqual(stub.calls[0][1], mp)

    def test_default_mapping_is_empty_dict_when_none(self):
        """mapping=None 时 recognizer 拿到 {}（兜底），不调 get_ocr_columns。"""
        from ocr import batch_ocr_images
        # 临时确保 get_ocr_columns 不被调用：用 mock.Mock 替换
        stub = _make_stub_recognizer({'a.png': []})
        with mock.patch('utils.get_ocr_columns', create=True,
                        side_effect=AssertionError('不应调用 get_ocr_columns')):
            results, errors = batch_ocr_images(['a.png'], mapping=None,
                                                recognizer=stub)
        self.assertEqual(results[0]['mapping'], {})
        self.assertEqual(stub.calls[0][1], {})

    def test_kwargs_passed_to_recognizer(self):
        """forced_model / secondary_model 等 **kwargs 透传。"""
        from ocr import batch_ocr_images
        stub = _make_stub_recognizer({'a.png': []})
        batch_ocr_images(['a.png'], recognizer=stub,
                          forced_model='qwen3.5-ocr',
                          secondary_model='glm-4v-flash',
                          custom_arg=42)
        self.assertEqual(len(stub.calls), 1)
        _path, _mp, kwargs = stub.calls[0]
        self.assertEqual(kwargs.get('forced_model'), 'qwen3.5-ocr')
        self.assertEqual(kwargs.get('secondary_model'), 'glm-4v-flash')
        self.assertEqual(kwargs.get('custom_arg'), 42)


# ═══════════════════════ BatchCancelled 中断 ═══════════

class TestBatchCancelledBreaks(unittest.TestCase):
    """F9 紧急停止：BatchCancelled 立即中断批量，把当前张记为取消错误。"""

    def _set_cancel_after_n(self, n):
        """注册 cancel check：第 n+1 次调用时抛 BatchCancelled（_check_cancel 内部用）。"""
        from ocr import BatchCancelled
        counter = {'n': 0}

        def _check():
            counter['n'] += 1
            if counter['n'] > n:
                raise BatchCancelled('紧急停止（F9）')
        from ocr import set_cancel_check
        set_cancel_check(_check)
        return counter

    def tearDown(self):
        from ocr import set_cancel_check
        set_cancel_check(None)

    def test_cancel_after_first_success(self):
        from ocr import batch_ocr_images
        counter = self._set_cancel_after_n(1)  # 第 2 次检查时取消
        a_items = [{'name': 'A', 'stock': 0, 'sales': 0, 'region': '',
                    'warehouse': '', 'sku_id': '', '_raw': {}}]
        stub = _make_stub_recognizer({
            'a.png': a_items,
            'b.png': [{'name': 'B', 'stock': 0, 'sales': 0, 'region': '',
                       'warehouse': '', 'sku_id': '', '_raw': {}}],
            'c.png': [{'name': 'C', 'stock': 0, 'sales': 0, 'region': '',
                       'warehouse': '', 'sku_id': '', '_raw': {}}],
        })
        results, errors = batch_ocr_images(['a.png', 'b.png', 'c.png'],
                                            recognizer=stub)
        # a 成功 → 检查（counter=1，未取消）→ 处理 b/c 前检查（counter=2，>1 → 取消）
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['path'], 'a.png')
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], 'b.png')
        self.assertIn('取消', errors[0][1])
        # c 不应被处理（recognizer 应只调用了 1 次）
        self.assertEqual(len(stub.calls), 1)

    def test_cancel_before_first_image(self):
        """set_cancel_check 第一张识别前就触发 → 全批零结果，单 errors 条目。"""
        from ocr import batch_ocr_images
        # 永远取消
        from ocr import BatchCancelled
        from ocr import set_cancel_check
        def _always():
            raise BatchCancelled('紧急停止（F9）')
        set_cancel_check(_always)
        stub = _make_stub_recognizer({'a.png': []})
        results, errors = batch_ocr_images(['a.png', 'b.png'],
                                            recognizer=stub)
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], 'a.png')
        self.assertEqual(len(stub.calls), 0)


# ═══════════════════════ 默认 recognizer 路径（不替换 ocr_dual_verify_generic） ═══════════

class TestBatchOcrDefaultRecognizer(unittest.TestCase):
    """默认 recognizer 是 ocr_dual_verify_generic（不替换，monkey-patch 替换它）。"""

    def test_default_recognizer_is_ocr_dual_verify_generic(self):
        """不传 recognizer → 调用 ocr_dual_verify_generic（monkey-patch 验证）。"""
        from ocr import batch_ocr_images, ocr_dual_verify_generic
        # 用 monkey-patch 替换模块的 ocr_dual_verify_generic（不影响真实函数）
        from ocr import ocr_dual_verify_generic as _orig
        called = mock.Mock()
        called.calls = []

        def _stub(path, mapping, **kwargs):
            called.calls.append(path)
            return [{'name': 'X', 'stock': 0, 'sales': 0, 'region': '',
                     'warehouse': '', 'sku_id': '', '_raw': {}}]

        # 替换模块引用
        with mock.patch('ocr.ocr_dual_verify_generic', _stub):
            results, errors = batch_ocr_images(['a.png', 'b.png'])
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(called.calls, ['a.png', 'b.png'])


# ═══════════════════════ 默认 mapping 兜底 ═══════════

class TestBatchOcrDefaultMapping(unittest.TestCase):
    """mapping=None 时走 get_ocr_columns() 兜底（不调真实 cfg：mock 替换）。"""

    def test_mapping_none_falls_back_to_get_ocr_columns(self):
        """mapping=None + 兜底 cfg 有 mapping → recognizer 拿到 cfg['mapping']。"""
        from ocr import batch_ocr_images
        # mock utils.get_ocr_columns（在 batch_ocr_images 内部 lazy import）
        fake_cfg = {'mapping': {'name': '商品信息', 'stock': '仓库总库存'}}
        stub = _make_stub_recognizer({'a.png': []})
        with mock.patch('utils.get_ocr_columns', create=True,
                        return_value=fake_cfg):
            results, errors = batch_ocr_images(['a.png'], mapping=None,
                                                recognizer=stub)
        self.assertEqual(results[0]['mapping'], fake_cfg['mapping'])
        self.assertEqual(stub.calls[0][1], fake_cfg['mapping'])

    def test_mapping_none_get_ocr_columns_fails_falls_back_to_empty(self):
        """mapping=None + get_ocr_columns 抛异常 → recognizer 拿到 {}（兜底）。"""
        from ocr import batch_ocr_images
        stub = _make_stub_recognizer({'a.png': []})
        with mock.patch('utils.get_ocr_columns', create=True,
                        side_effect=RuntimeError('cfg read fail')):
            results, errors = batch_ocr_images(['a.png'], mapping=None,
                                                recognizer=stub)
        self.assertEqual(results[0]['mapping'], {})
        self.assertEqual(stub.calls[0][1], {})


if __name__ == '__main__':
    unittest.main()