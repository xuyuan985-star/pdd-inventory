# -*- coding: utf-8 -*-
"""R1 流程效率（t1/gui 侧）纯逻辑单测：窗口状态记忆 / 导入映射记忆预填 / 批量图片进度文案。

覆盖对象（均为 gui.py 模块级纯函数，不依赖 Tk 实例）：
- clamp_geometry            窗口 geometry 越界保护（负坐标/超出屏幕 → None 回默认）
- resolve_last_mapping      上次导入映射 → 当前文件表头的预填对位
- batch_images_progress_text 批量图片识别进度状态栏文案
- gui↔import_memory 契约     守护式导入接线 + settings['import_memory']['mapping']
                            落盘结构（t2 settings_ui 清除入口同键）+ 预填联动
"""
import importlib
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


class TestClampGeometry(unittest.TestCase):
    """窗口 geometry 越界保护（gui.clamp_geometry）。"""

    def test_valid_full_geometry_passthrough(self):
        """合法 'WxH+X+Y' 原样返回（用户日常布局不动）。"""
        import gui
        self.assertEqual(gui.clamp_geometry('900x620+100+50', 1920, 1080), '900x620+100+50')
        self.assertEqual(gui.clamp_geometry('1280x800+0+0', 1920, 1080), '1280x800+0+0')
        # 贴右下边缘（x+w > 屏宽但窗口部分可见）是真实布局，保留
        self.assertEqual(gui.clamp_geometry('900x620+1100+500', 1920, 1080), '900x620+1100+500')

    def test_valid_size_only_passthrough(self):
        """无位置的 'WxH'（只校验尺寸）合法返回。"""
        import gui
        self.assertEqual(gui.clamp_geometry('900x620', 1920, 1080), '900x620')

    def test_oversize_rejected(self):
        """宽或高大于屏幕 → None（换显示器/DPI 变更后旧尺寸溢出）。"""
        import gui
        self.assertIsNone(gui.clamp_geometry('2000x620+0+0', 1920, 1080))
        self.assertIsNone(gui.clamp_geometry('900x1200+0+0', 1920, 1080))
        # 恰好等于屏幕尺寸是合法边界
        self.assertEqual(gui.clamp_geometry('1920x1080+0+0', 1920, 1080), '1920x1080+0+0')

    def test_negative_position_rejected(self):
        """负坐标（拖出左/上边缘，或最大化读数 -8 偏移）→ None。"""
        import gui
        self.assertIsNone(gui.clamp_geometry('900x620+-8+-8', 1920, 1080))
        self.assertIsNone(gui.clamp_geometry('900x620+-100+50', 1920, 1080))
        self.assertIsNone(gui.clamp_geometry('900x620+100+-1', 1920, 1080))

    def test_fully_offscreen_rejected(self):
        """X ≥ 屏宽 / Y ≥ 屏高（整体在屏幕外）→ None。"""
        import gui
        self.assertIsNone(gui.clamp_geometry('900x620+1920+50', 1920, 1080))
        self.assertIsNone(gui.clamp_geometry('900x620+100+1080', 1920, 1080))

    def test_zero_or_malformed_rejected(self):
        """宽/高为 0、非法串、空值、非字符串 → None。"""
        import gui
        self.assertIsNone(gui.clamp_geometry('0x0+1+1', 1920, 1080))
        self.assertIsNone(gui.clamp_geometry('abc', 1920, 1080))
        self.assertIsNone(gui.clamp_geometry('900', 1920, 1080))
        self.assertIsNone(gui.clamp_geometry('900x620+100', 1920, 1080))  # 缺 Y
        self.assertIsNone(gui.clamp_geometry('', 1920, 1080))
        self.assertIsNone(gui.clamp_geometry(None, 1920, 1080))
        self.assertIsNone(gui.clamp_geometry('900x620+100+50', 0, 1080))  # 屏幕尺寸非法
        self.assertIsNone(gui.clamp_geometry('900x620+100+50', 1920, -5))

    def test_whitespace_tolerated(self):
        """首尾空白容忍（Tk geometry 读数个别场景带空白）。"""
        import gui
        self.assertEqual(gui.clamp_geometry('  900x620+100+50 ', 1920, 1080),
                         '900x620+100+50')


class TestResolveLastMapping(unittest.TestCase):
    """上次导入映射 → 预填对位（gui.resolve_last_mapping）。"""

    HEADERS = ['商品信息', '仓库总库存', '仓库预估总销售数', '销售区域', '仓库信息']

    def test_exact_match_resolves_to_header_text(self):
        """映射列名与表头一致 → 返回 {field: 表头原文}（下拉预填可直接命中）。"""
        import gui
        mapping = {'name': '商品信息', 'stock': '仓库总库存', 'sales': '仓库预估总销售数',
                   'region': '销售区域', 'warehouse': '仓库信息'}
        out = gui.resolve_last_mapping(self.HEADERS, mapping)
        self.assertEqual(out, {'name': '商品信息', 'stock': '仓库总库存',
                               'sales': '仓库预估总销售数', 'region': '销售区域',
                               'warehouse': '仓库信息'})

    def test_whitespace_normalized_match(self):
        """映射列名带全角/前后空格，normalize 后与表头对上 → 返回表头原文。"""
        import gui
        mapping = {'name': ' 商品信息 ', 'stock': '　仓库总库存', 'sales': '仓库预估总销售数'}
        out = gui.resolve_last_mapping(self.HEADERS, mapping)
        self.assertEqual(out, {'name': '商品信息', 'stock': '仓库总库存',
                               'sales': '仓库预估总销售数'})

    def test_unmatched_fields_dropped(self):
        """对不上的字段不进结果（预填保持 guess 默认），命中的照常返回。"""
        import gui
        mapping = {'name': '商品信息', 'stock': '仓库总库存',
                   'sales': '仓库预估总销售数', 'region': '不存在的列'}
        out = gui.resolve_last_mapping(self.HEADERS, mapping)
        self.assertEqual(out, {'name': '商品信息', 'stock': '仓库总库存',
                               'sales': '仓库预估总销售数'})

    def test_empty_inputs(self):
        """空 mapping / 空 headers / None → {}（调用方按无预填处理）。"""
        import gui
        self.assertEqual(gui.resolve_last_mapping(self.HEADERS, {}), {})
        self.assertEqual(gui.resolve_last_mapping(self.HEADERS, None), {})
        self.assertEqual(gui.resolve_last_mapping([], {'name': '商品信息'}), {})
        self.assertEqual(gui.resolve_last_mapping(None, {'name': '商品信息'}), {})

    def test_junk_types_tolerated(self):
        """非字符串键值 / 非串表头项容忍（settings 手改损坏不崩）。"""
        import gui
        mapping = {'name': '商品信息', 3: 'x', 'stock': None, 'sales': 123}
        headers = ['商品信息', 45, None, '仓库总库存']
        out = gui.resolve_last_mapping(headers, mapping)
        self.assertEqual(out, {'name': '商品信息'})


class TestBatchImagesProgressText(unittest.TestCase):
    """批量图片识别进度文案（gui.batch_images_progress_text）。
    
    v1.5.6：此函数仍存在且语义不变。「批量图片」按钮已合并入截图主入口菜单
    （_open_shot_menu → pick_images），但批量图片的业务进度仍走同一逻辑路径，
    进度文案前缀"批量图片识别"保持不变。"""

    def test_basic_format(self):
        import gui
        self.assertEqual(gui.batch_images_progress_text(1, 5), '批量图片识别 第 1/5 张…')
        self.assertEqual(gui.batch_images_progress_text(3, 5), '批量图片识别 第 3/5 张…')

    def test_index_clamped(self):
        """i 越界钳制到 [1, n]。"""
        import gui
        self.assertEqual(gui.batch_images_progress_text(0, 5), '批量图片识别 第 1/5 张…')
        self.assertEqual(gui.batch_images_progress_text(-3, 5), '批量图片识别 第 1/5 张…')
        self.assertEqual(gui.batch_images_progress_text(9, 5), '批量图片识别 第 5/5 张…')

    def test_invalid_total_fallback(self):
        """n 非法（≤0 / 非数字）→ 兜底文案。"""
        import gui
        self.assertEqual(gui.batch_images_progress_text(1, 0), '批量图片识别 准备中…')
        self.assertEqual(gui.batch_images_progress_text(1, -2), '批量图片识别 准备中…')
        self.assertEqual(gui.batch_images_progress_text(1, None), '批量图片识别 准备中…')
        self.assertEqual(gui.batch_images_progress_text(1, 'abc'), '批量图片识别 准备中…')


class TestGuiImportMemoryContract(unittest.TestCase):
    """gui ↔ import_memory（t2）接线契约：守护式导入 + 同键落盘 + 预填联动。

    用 tmp 目录重定向 utils.get_base_dir（finally 还原），不污染真实 settings.json。
    """

    def setUp(self):
        import utils as _utils
        self._utils = _utils
        self._real_gbd = _utils.get_base_dir
        self._tmp = tempfile.mkdtemp()
        _utils.get_base_dir = lambda: self._tmp
        _utils.Config._load_cache = {'mtime': -1, 'data': None}
        _utils.Config._template_cache = None

    def tearDown(self):
        self._utils.get_base_dir = self._real_gbd
        self._utils.Config._load_cache = {'mtime': -1, 'data': None}
        self._utils.Config._template_cache = None
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_gui_binds_import_memory_module(self):
        """gui 守护式导入接线：import_memory 落盘时 gui.import_memory 必须绑定成功。"""
        import gui
        import import_memory
        self.assertIsNotNone(gui.import_memory,
                             'gui.import_memory 为 None：守护式导入失败，映射记忆功能失效')
        self.assertIs(gui.import_memory, import_memory)

    def test_save_then_settings_key_structure(self):
        """save_last_mapping → settings.json 出现 import_memory.mapping 节点
        （gui 写回与 t2 设置页清除入口共用的键结构契约）。"""
        import import_memory
        self.assertTrue(import_memory.save_last_mapping(
            {'name': '商品信息', 'stock': '仓库总库存', 'sales': '销量'}))
        sf = os.path.join(self._tmp, 'settings.json')
        with open(sf, 'r', encoding='utf-8') as f:
            data = json.load(f)
        node = data.get('import_memory')
        self.assertIsInstance(node, dict)
        self.assertEqual(node.get('mapping'),
                         {'name': '商品信息', 'stock': '仓库总库存', 'sales': '销量'})
        self.assertIn('saved_at', node)  # 契约：同步落 ISO 时间戳

    def test_roundtrip_prefill_flow(self):
        """存 → 读 → matches → resolve 全链：同结构文件预填可用。"""
        import gui
        import import_memory
        import_memory.save_last_mapping(
            {'name': '商品信息', 'stock': '仓库总库存', 'sales': '仓库预估总销售数',
             'region': '销售区域'})
        last = import_memory.get_last_mapping()
        self.assertIsNotNone(last)
        headers = ['商品信息', '仓库总库存', '仓库预估总销售数', '销售区域', '仓库信息']
        matched, hit_rate = import_memory.last_mapping_matches(headers, last)
        self.assertTrue(matched)
        self.assertEqual(hit_rate, 1.0)
        resolved = gui.resolve_last_mapping(headers, last)
        self.assertEqual(resolved.get('name'), '商品信息')
        self.assertEqual(resolved.get('region'), '销售区域')

    def test_stale_headers_no_prefill(self):
        """表头结构变了（核心列对不上）→ matches=False，gui 侧不预填。"""
        import gui
        import import_memory
        import_memory.save_last_mapping({'name': '商品信息', 'stock': '仓库总库存',
                                         'sales': '仓库预估总销售数'})
        last = import_memory.get_last_mapping()
        new_headers = ['商品名称', '库存数量', '日销量']  # 换了一套列名
        matched, _ = import_memory.last_mapping_matches(new_headers, last)
        self.assertFalse(matched)
        self.assertEqual(gui.resolve_last_mapping(new_headers, last), {})


if __name__ == '__main__':
    unittest.main()
