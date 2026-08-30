"""PDD EZ — import_memory 纯逻辑单测（R1 流程效率 t2 产出）

覆盖：
  - get_last_mapping：正常往返、空 / 损坏 / 字段全空 → None；
    返回值过滤非字符串键值。
  - save_last_mapping：非 dict 拒绝；空 mapping 拒绝；
    与 get_last_mapping 往返一致；落 'saved_at' 时间戳。
  - clear_last_mapping：节点不存在 → True（幂等）；有节点 → 删除。
  - last_mapping_matches：核心字段全命中 → (True, 1.0)；
    缺一 → (False, 0.666...)；全缺 → (False, 0.0)；
    非 dict mapping → (False, 0.0)；
    归一化容差（全角空格 / 列名前后空格）应等价于精确命中。
  - _import_summary_lines：mapping 空 → 占位文案；
    多行顺序：核心字段优先 + 其他按字母序。

不依赖真实 API / GUI / 数据库；临时用 tmp 目录走真 Config 通道
（utils.Config 走 get_base_dir()，临时改 settings.json 路径不可行——
本测试通过 monkey-patch Config.load/save 隔离，避免污染用户配置）。
"""
import importlib
import os
import sys
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


# ═══════════════════════ Config 隔离（不污染真实 settings.json） ═══════════
# import_memory 直接调 utils.Config.load/save，走真实磁盘。
# 测试用 monkey-patch 替换 Config 的两个静态方法，让内存 dict 充当"settings.json"。
# 这能保证：完整覆盖 save→get 往返链路 + 损坏容忍分支，又不污染用户数据。

class _MemConfig:
    """最小 Config 替身：内存 dict 持久化 + 损坏隔离语义。

    load()：未设置 → 空 dict；设置 'corrupt' → 空 dict（模拟损坏自愈）。
    save(d)：写内存 dict，并清缓存；模拟原子写成功（无异常）。
    """
    _store = {}

    @staticmethod
    def load():
        # 模拟真 Config：损坏标识 → 等价空（不再抛）
        if _MemConfig._store.get('__corrupt__'):
            return {}
        d = _MemConfig._store.get('data')
        return d if isinstance(d, dict) else {}

    @staticmethod
    def save(data):
        if not isinstance(data, dict):
            return
        _MemConfig._store['data'] = dict(data)
        # 模拟 mtime 缓存清理：直接写穿

    @staticmethod
    def _reset():
        _MemConfig._store.clear()


def _patch_config():
    """monkey-patch utils.Config → _MemConfig，并 reload import_memory 让它重绑 Config。

    返回 (patched_import_memory_module, _MemConfig)；调用方每个用例前后重置 _store。
    """
    import utils
    _original_config = utils.Config
    utils.Config = _MemConfig
    # import_memory 顶部 from utils import Config — 重新加载让它重绑到 _MemConfig
    import import_memory as _im
    importlib.reload(_im)
    return _im, _original_config


def _restore_config(_original):
    """还原 utils.Config，避免影响其他测试。"""
    import utils
    utils.Config = _original
    # 再 reload 一次让 import_memory 重新绑定回原 Config（保险）
    import import_memory as _im
    try:
        importlib.reload(_im)
    except Exception:
        pass


# ═══════════════════════ get_last_mapping / save_last_mapping ═══════════

class TestSaveGetRoundTrip(unittest.TestCase):
    """save_last_mapping → get_last_mapping 往返一致。"""

    def setUp(self):
        _MemConfig._reset()
        self._im, self._orig_config = _patch_config()

    def tearDown(self):
        _restore_config(self._orig_config)
        _MemConfig._reset()

    def test_round_trip_basic(self):
        save_last_mapping = self._im.save_last_mapping
        get_last_mapping = self._im.get_last_mapping
        mapping = {'name': '商品信息', 'stock': '仓库总库存',
                   'sales': '仓库预估总销售数', 'region': '销售区域'}
        self.assertTrue(save_last_mapping(mapping))
        self.assertEqual(get_last_mapping(), mapping)

    def test_saved_at_present(self):
        save_last_mapping = self._im.save_last_mapping
        get_last_mapping = self._im.get_last_mapping
        save_last_mapping({'name': '商品名称'})
        m = get_last_mapping()
        self.assertEqual(m, {'name': '商品名称'})
        # 单独查 settings.json 节点：必须含 saved_at
        cfg = _MemConfig.load()
        self.assertIn('import_memory', cfg)
        self.assertIn('saved_at', cfg['import_memory'])
        self.assertIsInstance(cfg['import_memory']['saved_at'], str)
        self.assertGreater(len(cfg['import_memory']['saved_at']), 0)


class TestSaveReject(unittest.TestCase):
    """save_last_mapping 拒绝非法输入。"""

    def setUp(self):
        _MemConfig._reset()
        self._im, self._orig_config = _patch_config()

    def tearDown(self):
        _restore_config(self._orig_config)
        _MemConfig._reset()

    def test_non_dict_rejected(self):
        save_last_mapping = self._im.save_last_mapping
        get_last_mapping = self._im.get_last_mapping
        self.assertFalse(save_last_mapping(None))
        self.assertFalse(save_last_mapping('商品信息'))
        self.assertFalse(save_last_mapping([('name', '商品信息')]))
        self.assertFalse(save_last_mapping(42))
        self.assertIsNone(get_last_mapping())

    def test_empty_mapping_rejected(self):
        save_last_mapping = self._im.save_last_mapping
        self.assertFalse(save_last_mapping({}))
        self.assertFalse(save_last_mapping({'name': '', 'stock': None}))

    def test_partial_non_string_filtered_then_rejected(self):
        # 全是非字符串/空字符串 → 清空后空 → 拒绝
        save_last_mapping = self._im.save_last_mapping
        get_last_mapping = self._im.get_last_mapping
        self.assertFalse(save_last_mapping({'name': None, 'stock': 42, 'sales': ''}))
        # 部分有效仍接受（只持久化有效键值对）
        self.assertTrue(save_last_mapping({'name': '商品信息', 'stock': 42, 'sales': '销量'}))
        m = get_last_mapping()
        # stock=42 被过滤；name/sales=字符串非空 → 保留
        self.assertEqual(m, {'name': '商品信息', 'sales': '销量'})


class TestCorruptionTolerance(unittest.TestCase):
    """settings 损坏 / 类型异常 → 返回 None，不抛。"""

    def setUp(self):
        _MemConfig._reset()
        self._im, self._orig_config = _patch_config()

    def tearDown(self):
        _restore_config(self._orig_config)
        _MemConfig._reset()

    def test_empty_settings_returns_none(self):
        get_last_mapping = self._im.get_last_mapping
        self.assertIsNone(get_last_mapping())

    def test_import_memory_node_missing(self):
        save_last_mapping = self._im.save_last_mapping
        get_last_mapping = self._im.get_last_mapping
        save_last_mapping({'name': '商品信息'})
        # 模拟节点被外部误删
        _MemConfig._store['data'].pop('import_memory', None)
        self.assertIsNone(get_last_mapping())

    def test_import_memory_node_not_dict(self):
        get_last_mapping = self._im.get_last_mapping
        _MemConfig._store['data'] = {'import_memory': 'oops'}
        self.assertIsNone(get_last_mapping())
        _MemConfig._store['data'] = {'import_memory': ['a', 'b']}
        self.assertIsNone(get_last_mapping())
        _MemConfig._store['data'] = {'import_memory': None}
        self.assertIsNone(get_last_mapping())

    def test_mapping_field_not_dict(self):
        get_last_mapping = self._im.get_last_mapping
        _MemConfig._store['data'] = {'import_memory': {'mapping': 'oops'}}
        self.assertIsNone(get_last_mapping())

    def test_mapping_filters_non_string_values(self):
        get_last_mapping = self._im.get_last_mapping
        _MemConfig._store['data'] = {'import_memory': {
            'mapping': {'name': '商品信息', 'stock': 42, 'sales': None,
                        'region': '', 'warehouse': '  '}}}
        # 只剩 name 字段（其他要么非字符串、要么空/纯空白）
        self.assertEqual(get_last_mapping(), {'name': '商品信息'})

    def test_settings_corrupt_returns_none(self):
        """settings.json 损坏（__corrupt__ 模拟）→ 返回 None 不抛。"""
        get_last_mapping = self._im.get_last_mapping
        save_last_mapping = self._im.save_last_mapping
        _MemConfig._store['__corrupt__'] = True
        self.assertIsNone(get_last_mapping())
        # save 在损坏状态下不应抛（导入"静默"哲学：写到内存 dict 仍 OK）
        try:
            save_last_mapping({'name': 'X'})
            # load() 检测 __corrupt__ → 返回 {}，所以 get 仍 None
            self.assertIsNone(get_last_mapping())
        except Exception as e:
            self.fail(f'损坏状态下 save 不应抛：{e}')


class TestClearLastMapping(unittest.TestCase):
    """clear_last_mapping：删除节点 / 幂等。"""

    def setUp(self):
        _MemConfig._reset()
        self._im, self._orig_config = _patch_config()

    def tearDown(self):
        _restore_config(self._orig_config)
        _MemConfig._reset()

    def test_clear_existing(self):
        save_last_mapping = self._im.save_last_mapping
        clear_last_mapping = self._im.clear_last_mapping
        get_last_mapping = self._im.get_last_mapping
        save_last_mapping({'name': '商品信息'})
        self.assertIsNotNone(get_last_mapping())
        self.assertTrue(clear_last_mapping())
        self.assertIsNone(get_last_mapping())

    def test_clear_nonexistent_idempotent(self):
        clear_last_mapping = self._im.clear_last_mapping
        self.assertTrue(clear_last_mapping())

    def test_clear_does_not_touch_other_keys(self):
        """clear 只删 import_memory 节点；其它键保留。"""
        save_last_mapping = self._im.save_last_mapping
        clear_last_mapping = self._im.clear_last_mapping
        _MemConfig._store['data'] = {
            'theme': '极简白',
            'export_path': 'C:/export',
        }
        save_last_mapping({'name': 'X'})
        clear_last_mapping()
        cfg = _MemConfig.load()
        self.assertNotIn('import_memory', cfg)
        self.assertEqual(cfg.get('theme'), '极简白')
        self.assertEqual(cfg.get('export_path'), 'C:/export')


# ═══════════════════════ last_mapping_matches ═══════════

class TestLastMappingMatches(unittest.TestCase):
    """headers × mapping 比对：精确命中 / 归一化容差 / 缺字段语义。"""

    def test_full_match(self):
        from import_memory import last_mapping_matches
        mapping = {'name': '商品信息', 'stock': '仓库总库存', 'sales': '仓库预估总销售数'}
        headers = ['商品信息', '仓库总库存', '仓库预估总销售数', '销售区域']
        match, rate = last_mapping_matches(headers, mapping)
        self.assertTrue(match)
        self.assertEqual(rate, 1.0)

    def test_one_core_field_missing(self):
        from import_memory import last_mapping_matches
        # 缺 sales → match=False，rate=2/3
        mapping = {'name': '商品信息', 'stock': '仓库总库存', 'sales': '仓库预估总销售数'}
        headers = ['商品信息', '仓库总库存']
        match, rate = last_mapping_matches(headers, mapping)
        self.assertFalse(match)
        self.assertAlmostEqual(rate, 2 / 3, places=4)

    def test_all_core_missing(self):
        from import_memory import last_mapping_matches
        mapping = {'name': '商品信息', 'stock': '仓库总库存', 'sales': '销量'}
        headers = ['其他', '字段']
        match, rate = last_mapping_matches(headers, mapping)
        self.assertFalse(match)
        self.assertEqual(rate, 0.0)

    def test_non_dict_mapping(self):
        from import_memory import last_mapping_matches
        match, rate = last_mapping_matches(['a', 'b'], None)
        self.assertFalse(match)
        self.assertEqual(rate, 0.0)
        match, rate = last_mapping_matches(['a', 'b'], 'string')
        self.assertFalse(match)
        self.assertEqual(rate, 0.0)
        match, rate = last_mapping_matches(['a', 'b'], {})
        self.assertFalse(match)
        self.assertEqual(rate, 0.0)

    def test_normalize_full_width_space(self):
        from import_memory import last_mapping_matches
        # 表头含全角空格 → 归一化后仍命中
        mapping = {'name': '商品信息', 'stock': '仓库总库存', 'sales': '仓库预估总销售数'}
        headers = ['商品\u3000信息', '仓库 总库存', '仓库预估总销售数']
        match, rate = last_mapping_matches(headers, mapping)
        self.assertTrue(match, f'归一化后应命中，实际 rate={rate}')
        self.assertEqual(rate, 1.0)

    def test_optional_fields_not_in_hit_rate(self):
        """region/warehouse 缺失不影响 hit_rate（核心字段=3）。"""
        from import_memory import last_mapping_matches
        mapping = {'name': '商品信息', 'stock': '仓库总库存',
                   'sales': '仓库预估总销售数',
                   'region': '销售区域', 'warehouse': '仓库信息'}
        # headers 缺 region/warehouse：核心字段全在 → match=True
        headers = ['商品信息', '仓库总库存', '仓库预估总销售数']
        match, rate = last_mapping_matches(headers, mapping)
        self.assertTrue(match)
        self.assertEqual(rate, 1.0)

    def test_empty_headers(self):
        from import_memory import last_mapping_matches
        mapping = {'name': '商品信息'}
        match, rate = last_mapping_matches([], mapping)
        self.assertFalse(match)
        self.assertEqual(rate, 0.0)
        match, rate = last_mapping_matches(None, mapping)
        self.assertFalse(match)
        self.assertEqual(rate, 0.0)

    def test_non_iterable_headers(self):
        from import_memory import last_mapping_matches
        match, rate = last_mapping_matches(42, {'name': '商品信息'})
        self.assertFalse(match)
        self.assertEqual(rate, 0.0)

    def test_headers_with_none_entries(self):
        from import_memory import last_mapping_matches
        # 表头含 None / 空字符串 → 跳过，不影响命中
        mapping = {'name': '商品信息', 'stock': '仓库总库存', 'sales': '销量'}
        headers = [None, '', '商品信息', '仓库总库存', '销量']
        match, rate = last_mapping_matches(headers, mapping)
        self.assertTrue(match)
        self.assertEqual(rate, 1.0)


# ═══════════════════════ _import_summary_lines（settings_ui 静态助手） ═══════════

class TestImportSummaryLines(unittest.TestCase):
    """_import_summary_lines：UI 渲染层纯函数。"""

    def setUp(self):
        _MemConfig._reset()

    def test_empty_mapping_placeholder(self):
        from settings_ui import SettingsUIMixin
        lines = SettingsUIMixin._import_summary_lines(None, None)
        self.assertEqual(lines, ['（暂无上次导入记录）'])

    def test_empty_dict_placeholder(self):
        from settings_ui import SettingsUIMixin
        self.assertEqual(
            SettingsUIMixin._import_summary_lines({}, ''),
            ['（暂无上次导入记录）'])

    def test_basic_mapping(self):
        from settings_ui import SettingsUIMixin
        mapping = {'name': '商品信息', 'stock': '仓库总库存',
                   'sales': '仓库预估总销售数'}
        lines = SettingsUIMixin._import_summary_lines(mapping, '')
        # 核心字段优先 + 无 saved_at → 三行
        self.assertEqual(lines, [
            '名称:商品信息',
            '库存:仓库总库存',
            '销量:仓库预估总销售数',
        ])

    def test_optional_fields_after_core(self):
        from settings_ui import SettingsUIMixin
        mapping = {'region': '销售区域', 'warehouse': '仓库信息',
                   'name': '商品信息', 'stock': '仓库总库存', 'sales': '销量'}
        lines = SettingsUIMixin._import_summary_lines(mapping, '')
        # 核心在前（name/stock/sales），可选字段按字母序
        self.assertEqual(lines, [
            '名称:商品信息',
            '库存:仓库总库存',
            '销量:销量',
            '销售区域:销售区域',
            '仓库:仓库信息',
        ])

    def test_saved_at_appended(self):
        from settings_ui import SettingsUIMixin
        mapping = {'name': '商品信息'}
        lines = SettingsUIMixin._import_summary_lines(mapping, '2026-08-30T12:34:56')
        self.assertEqual(lines, [
            '名称:商品信息',
            '（保存于 2026-08-30T12:34:56）',
        ])

    def test_skips_non_string_or_empty_values(self):
        from settings_ui import SettingsUIMixin
        mapping = {'name': '商品信息', 'stock': '', 'sales': None,
                   'region': '  '}
        lines = SettingsUIMixin._import_summary_lines(mapping, '')
        # 只剩有效键值（纯空白 / 空 / None 均丢弃）
        self.assertEqual(lines, ['名称:商品信息'])

    def test_no_valid_keys_returns_placeholder(self):
        from settings_ui import SettingsUIMixin
        # 所有键都无效（None / 空字符串）→ placeholder
        mapping = {'name': '', 'stock': None, 'sales': '   '}
        lines = SettingsUIMixin._import_summary_lines(mapping, '')
        self.assertEqual(lines, ['（暂无上次导入记录）'])

    def test_unknown_field_uses_raw_key(self):
        from settings_ui import SettingsUIMixin
        # 未知字段名 → 直接用原 key 显示（不抛，不强制翻译）
        mapping = {'unknown_field': 'X列'}
        lines = SettingsUIMixin._import_summary_lines(mapping, '')
        self.assertEqual(lines, ['unknown_field:X列'])


# ═══════════════════════ 全栈：save → get → summary 链路 ═══════════

class TestEndToEndSaveGetSummary(unittest.TestCase):
    """设置页整链路：save 持久化 → get 读出 → summary 渲染。"""

    def setUp(self):
        _MemConfig._reset()
        self._im, self._orig_config = _patch_config()

    def tearDown(self):
        _restore_config(self._orig_config)
        _MemConfig._reset()

    def test_full_chain(self):
        save_last_mapping = self._im.save_last_mapping
        get_last_mapping = self._im.get_last_mapping
        from settings_ui import SettingsUIMixin

        # 1) 用户导入 CSV，自动记忆
        mapping = {'name': '商品信息', 'stock': '仓库总库存',
                   'sales': '仓库预估总销售数', 'region': '销售区域'}
        self.assertTrue(save_last_mapping(mapping))

        # 2) 下次进入设置页，读 mapping
        loaded = get_last_mapping()
        self.assertEqual(loaded, mapping)

        # 3) UI 渲染摘要
        cfg = _MemConfig.load()
        saved_at = cfg.get('import_memory', {}).get('saved_at', '')
        lines = SettingsUIMixin._import_summary_lines(loaded, saved_at)
        self.assertEqual(lines, [
            '名称:商品信息',
            '库存:仓库总库存',
            '销量:仓库预估总销售数',
            '销售区域:销售区域',
            f'（保存于 {saved_at}）',
        ])

    def test_clear_then_summary_shows_placeholder(self):
        save_last_mapping = self._im.save_last_mapping
        clear_last_mapping = self._im.clear_last_mapping
        get_last_mapping = self._im.get_last_mapping
        from settings_ui import SettingsUIMixin

        save_last_mapping({'name': '商品信息'})
        self.assertTrue(clear_last_mapping())
        self.assertIsNone(get_last_mapping())

        lines = SettingsUIMixin._import_summary_lines(get_last_mapping(), '')
        self.assertEqual(lines, ['（暂无上次导入记录）'])


if __name__ == '__main__':
    unittest.main()