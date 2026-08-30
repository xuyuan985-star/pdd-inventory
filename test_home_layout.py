# -*- coding: utf-8 -*-
"""R1 布局优化B（t3）：gui.py 主页/结果区/批量进度/复核弹窗 布局纯逻辑单测。

覆盖对象（均为 gui.py 模块级纯函数/常量，不依赖 Tk 实例）：
- batch_stage_percent   批量进度百分比映射（地区序 + 阶段占比，全程单调）
- progress_stage_label  阶段文案 → 状态栏短标签
- progress_status_text  进度 + 阶段 → 状态栏单行文案
- tree_col_width        识别结果表列宽自适应规则（预警/模型/名称类/数字）
- elide_cell            超长商品名显示省略
- model_display_label   补货模型标注 → 中文短标签
- busy_btn_text         批量忙时段按钮文案映射（_BATCH_BUSY_BTNS 状态表）
- REVIEW_COLS           复核弹窗列规格结构约束
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


class TestBatchStagePercent(unittest.TestCase):
    """批量进度百分比映射（gui.batch_stage_percent）。"""

    def test_stage_prefix_digit(self):
        """带数字阶段前缀的 dlog 文案按 地区序+阶段占比 换算。"""
        import gui
        # 单地区：阶段1 = 0%，阶段5 = 40%，阶段6（识别+滚动）= 50%
        self.assertEqual(gui.batch_stage_percent("1.✋ 页面状态正常", 0, 1), 0)
        self.assertEqual(gui.batch_stage_percent("5.页面刷新完成", 0, 1), 40)
        self.assertEqual(gui.batch_stage_percent("6.首屏OCR识别中(单模型)...", 0, 1), 50)
        # 多地区：第 2 个地区（idx=1，共 4 个）阶段 6 = (1+0.5)/4 = 37%
        self.assertEqual(gui.batch_stage_percent("6.↘ 滚动生效", 1, 4), 37)
        # 最后一个地区阶段 6 = (3+0.5)/4 = 87%（未到 100，留完成态）
        self.assertEqual(gui.batch_stage_percent("6.✓ 合计9个商品", 3, 4), 87)

    def test_no_prefix_keeps_last(self):
        """无阶段前缀的日志（如 AI 自动定位/⏹ 停止）沿用上次百分比。"""
        import gui
        self.assertEqual(gui.batch_stage_percent("AI 自动定位页面元素...", 0, 3, last=42), 42)
        self.assertEqual(gui.batch_stage_percent("⏹ 紧急停止", 2, 3, last=77), 77)
        self.assertEqual(gui.batch_stage_percent(None, 0, 1, last=13), 13)

    def test_clamped_0_99(self):
        """结果钳制 0-99（100% 留给批量完成态）。"""
        import gui
        self.assertEqual(gui.batch_stage_percent("6.无数据", 99, 1), 99)
        self.assertEqual(gui.batch_stage_percent("1.定位", 0, 1), 0)
        # region_idx 越界/total 非法也不炸
        self.assertEqual(gui.batch_stage_percent("6.无数据", 7, 0), 99)

    def test_monotonic_across_regions(self):
        """多地区批量进度全程单调递增（修复旧 stage_num*10 每地区跳回 10%）。"""
        import gui
        prev = -1
        for idx in range(4):
            for msg in ("1.定位", "3.回车确认", "5.页面刷新完成",
                        "6.首屏OCR识别中(双模型)...", "6.✓ 合计5个商品"):
                pct = gui.batch_stage_percent(msg, idx, 4)
                self.assertGreaterEqual(pct, prev, f"进度回跳: {msg}@idx={idx}")
                prev = pct


class TestProgressTexts(unittest.TestCase):
    """状态栏进度文案映射（gui.progress_stage_label / progress_status_text）。"""

    def test_stage_label_strips_prefix(self):
        """去「3.」序号前缀与尾省略号。"""
        import gui
        self.assertEqual(gui.progress_stage_label("3.回车确认"), "回车确认")
        self.assertEqual(gui.progress_stage_label("6.首屏OCR识别中(双模型)..."), "首屏OCR识别中(双模型)")
        self.assertEqual(gui.progress_stage_label("AI 自动定位页面元素..."), "AI 自动定位页面元素")

    def test_stage_label_empty_fallback(self):
        """空/None 文案兜底「处理中」。"""
        import gui
        self.assertEqual(gui.progress_stage_label(None), "处理中")
        self.assertEqual(gui.progress_stage_label(""), "处理中")
        self.assertEqual(gui.progress_stage_label("6."), "处理中")

    def test_stage_label_truncated_with_ellipsis(self):
        """超长文案截断到 limit 并加「…」。"""
        import gui
        long_msg = "6.⚠ 滚动未生效——浏览器未在前台/表格无内容可滚/页面被遮挡，请确认浏览器窗口在前台"
        out = gui.progress_stage_label(long_msg, limit=28)
        self.assertLessEqual(len(out), 29)
        self.assertTrue(out.endswith("…"))

    def test_status_text_with_percent(self):
        """有百分比 →「⏳ 批量识别 N%｜标签」。"""
        import gui
        self.assertEqual(gui.progress_status_text(37, "3.回车确认"), "⏳ 批量识别 37%｜回车确认")

    def test_status_text_without_percent(self):
        """百分比 0/非法 → 省略百分比段。"""
        import gui
        self.assertEqual(gui.progress_status_text(0, "1.定位"), "⏳ 批量识别中｜定位")
        self.assertEqual(gui.progress_status_text(None, None), "⏳ 批量识别中｜处理中")


class TestTreeColWidth(unittest.TestCase):
    """识别结果表列宽自适应规则（gui.tree_col_width）。"""

    def test_warning_column_140(self):
        """「预警」列 140：滞销⚠/超卖🔥 多标签不被截断。"""
        import gui
        self.assertEqual(gui.tree_col_width('预警'), 140)

    def test_model_column_100(self):
        """「模型」列 100：经典·无历史 等短标签可完整显示。"""
        import gui
        self.assertEqual(gui.tree_col_width('模型'), 100)

    def test_name_like_columns_260(self):
        """名称类列（商品信息/商品名称/商品/含「名称」「商品」字样）260。"""
        import gui
        for col in ('商品信息', '商品名称', '商品', '仓库销售库存名称', '商品ID'):
            self.assertEqual(gui.tree_col_width(col), 260, col)

    def test_numeric_columns_110(self):
        """数字/状态列 110：防"1109份"截断成"110份"。"""
        import gui
        for col in ('仓库总库存', '可售卖天数', '状态', '补货量', '地区'):
            self.assertEqual(gui.tree_col_width(col), 110, col)


class TestElideCell(unittest.TestCase):
    """超长商品名显示省略（gui.elide_cell）。"""

    def test_short_unchanged(self):
        import gui
        self.assertEqual(gui.elide_cell('洗衣液 2kg'), '洗衣液 2kg')
        self.assertEqual(gui.elide_cell(''), '')

    def test_long_truncated(self):
        import gui
        name = '官方旗舰店正品蓝月亮深层洁净洗衣液自然清香亮白增艳3kg装家庭囤货'
        out = gui.elide_cell(name)
        self.assertEqual(out, name[:20] + '…')
        self.assertLessEqual(len(out), 21)

    def test_none_safe(self):
        import gui
        self.assertEqual(gui.elide_cell(None), '')

    def test_custom_limit(self):
        import gui
        self.assertEqual(gui.elide_cell('ABCDEFGH', limit=3), 'ABC…')


class TestModelDisplayLabel(unittest.TestCase):
    """补货模型标注 → 中文短标签（gui.model_display_label）。"""

    def test_basic_tags(self):
        import gui
        self.assertEqual(gui.model_display_label('classic'), '经典')
        self.assertEqual(gui.model_display_label('weighted'), '加权')
        self.assertEqual(gui.model_display_label('advanced'), '高级')

    def test_fallback_tags(self):
        """加权/高级回退标注（classic(no_history)/classic(error)）。"""
        import gui
        self.assertEqual(gui.model_display_label('classic(no_history)'), '经典·无历史')
        self.assertEqual(gui.model_display_label('classic(error)'), '经典·异常')

    def test_empty_and_unknown(self):
        import gui
        self.assertEqual(gui.model_display_label(''), '')
        self.assertEqual(gui.model_display_label(None), '')
        # 未知标注原样透传（不吞自定义标注）
        self.assertEqual(gui.model_display_label('custom'), 'custom')


class TestBatchButtonPlan(unittest.TestCase):
    """批量忙时段按钮状态表（gui._BATCH_BUSY_BTNS + busy_btn_text）。"""

    def test_busy_text_mapping(self):
        """v1.5.6：批量忙时段按钮文案。「导出」对应 export_btn；「截图」对应合并后
        的 live_btn（截图主入口，菜单化后仍是 screenshot key）。"""
        import gui
        self.assertEqual(gui.busy_btn_text('导出'), '导出中…')
        self.assertEqual(gui.busy_btn_text('截图'), '截图中…')

    def test_busy_text_empty_fallback(self):
        import gui
        self.assertEqual(gui.busy_btn_text(''), '处理中…')
        self.assertEqual(gui.busy_btn_text(None), '处理中…')

    def test_state_table_covers_export_and_live(self):
        """状态表必须覆盖导出/截图两个批量期受控入口。
        v1.5.6：img_batch_btn（批量图片）已合并进 live_btn（截图菜单入口），
        批量图片的业务逻辑走 _open_shot_menu → pick_images 路径，busy 文案通过
        screenshot key（live_btn）统一处理，无需在 _BATCH_BUSY_BTNS 里单独列出。"""
        import gui
        attrs = tuple(a for a, _t, _k in gui._BATCH_BUSY_BTNS)
        self.assertIn('export_btn', attrs)
        self.assertIn('live_btn', attrs)
        # 确认无独立的 img_batch_btn（已合并入 live_btn）
        self.assertNotIn('img_batch_btn', attrs)


class TestReviewColsSpec(unittest.TestCase):
    """复核弹窗列规格结构约束（gui.REVIEW_COLS）。"""

    def test_structure(self):
        import gui
        cids = [c[0] for c in gui.REVIEW_COLS]
        # 列序：商品名/字段/异常原因/原文/解析值
        self.assertEqual(cids, ['name', 'field', 'reason', 'raw', 'parsed'])
        for cid, title, width, minw, stretch in gui.REVIEW_COLS:
            self.assertTrue(title, f'{cid} 缺标题')
            self.assertGreater(width, 0, f'{cid} 宽度非法')
            self.assertGreater(minw, 0, f'{cid} 最小宽度非法')
            self.assertGreaterEqual(width, minw, f'{cid} 初始宽 < 最小宽')
            self.assertIsInstance(stretch, bool)
        # 商品名/异常原因随窗拉伸（窄窗可横向滚动，宽窗铺满）
        stretch_map = {c[0]: c[4] for c in gui.REVIEW_COLS}
        self.assertTrue(stretch_map['name'])
        self.assertTrue(stretch_map['reason'])
        self.assertFalse(stretch_map['parsed'])


class TestWiringSourceAssert(unittest.TestCase):
    """接线源码断言：进度回调/F9 复核/复核弹窗修复 必须真实存在（防回归）。"""

    def _src(self, name):
        with open(os.path.join(HERE, name), 'r', encoding='utf-8') as f:
            return f.read()

    def test_submit_wires_on_progress(self):
        """批量 submit 必须接 on_progress=self._on_batch_progress。"""
        src = self._src('gui.py')
        self.assertIn('on_progress=self._on_batch_progress', src,
                      '批量识别未接 TaskQueue on_progress，进度可视化断线')

    def test_emergency_stop_polls_restore(self):
        """F9 必须启动终态轮询复位（_poll_cancel_restore）。"""
        src = self._src('gui.py')
        self.assertIn('_poll_cancel_restore', src)

    def test_finish_batch_resets_progress(self):
        """批量收尾必须复位进度条。"""
        src = self._src('gui.py')
        self.assertIn('self._reset_batch_progress()', src)

    def test_render_tree_has_model_column(self):
        """结果表 calc_cols 必须含「模型」列。"""
        src = self._src('gui.py')
        self.assertIn("('模型', 'rmodel')", src)

    def test_review_dialog_no_orphan_top_btns(self):
        """复核弹窗不得再有挂在未 pack 帧上的「修正选中行」（布局 bug 回归锚点）。"""
        src = self._src('gui.py')
        self.assertNotIn('_top_btns', src, '_top_btns 孤儿帧回归：修正按钮不可见')
        self.assertIn('"修正选中行"', src)


class TestBatchRegionProgress(unittest.TestCase):
    """R2 批次C：进度文案与地区联动（gui.batch_region_header / progress_status_text）。"""

    def test_region_header_parse(self):
        """地区标题行「── [广东] (2/5) ──」→ (地区名, 序号, 总数)。"""
        import gui
        self.assertEqual(gui.batch_region_header("── [广东] (2/5) ──"), ('广东', 2, 5))
        self.assertEqual(gui.batch_region_header("── [云南] (1/3) ──"), ('云南', 1, 3))

    def test_region_header_non_header(self):
        """非标题行/空值 → None。"""
        import gui
        self.assertIsNone(gui.batch_region_header("3.回车确认"))
        self.assertIsNone(gui.batch_region_header("AI 自动定位页面元素..."))
        self.assertIsNone(gui.batch_region_header(None))
        self.assertIsNone(gui.batch_region_header(""))

    def test_status_text_region_header_first(self):
        """首个地区标题 →「▶ 开始 1/N 地区：名」。"""
        import gui
        self.assertEqual(gui.progress_status_text(5, "── [广东] (1/3) ──"),
                         "▶ 开始 1/3 地区：广东")

    def test_status_text_region_header_completion(self):
        """后续地区标题 → 完成上一地区 + 开始新地区联动文案。"""
        import gui
        self.assertEqual(gui.progress_status_text(40, "── [云南] (3/5) ──"),
                         "✓ 已完成 2/5 地区 ▶ 开始 3/5：云南")

    def test_status_text_with_region_prefix(self):
        """普通阶段行 + 地区上下文 →「⏳ 批量识别 N%｜地区 · 阶段短语」。"""
        import gui
        self.assertEqual(gui.progress_status_text(24, "3.回车确认", region='广东'),
                         "⏳ 批量识别 24%｜广东 · 回车确认")

    def test_status_text_region_empty_keeps_old_shape(self):
        """无地区上下文保持旧形态（与 R1 布局B 契约一致）。"""
        import gui
        self.assertEqual(gui.progress_status_text(37, "3.回车确认"),
                         "⏳ 批量识别 37%｜回车确认")
        self.assertEqual(gui.progress_status_text(0, "1.定位", region=''),
                         "⏳ 批量识别中｜定位")


class TestReviewEditBtnState(unittest.TestCase):
    """R2 批次C：复核弹窗「修正选中行」按钮状态映射（gui.review_edit_btn_state）。"""

    def test_state_mapping(self):
        import gui
        self.assertEqual(gui.review_edit_btn_state(True), 'normal')
        self.assertEqual(gui.review_edit_btn_state(False), 'disabled')
        # Treeview.selection() 返回 tuple：空 = disabled，非空 = normal
        self.assertEqual(gui.review_edit_btn_state(()), 'disabled')
        self.assertEqual(gui.review_edit_btn_state(('1',)), 'normal')


class TestR2WiringSourceAssert(unittest.TestCase):
    """R2 修复接线源码断言（防回归）：GUI 侧关闭收队/复核触发/切店互斥/模糊采集。"""

    def _src(self, name):
        with open(os.path.join(HERE, name), 'r', encoding='utf-8') as f:
            return f.read()

    def test_on_closing_wired(self):
        """主窗关闭必须收队 TaskQueue：protocol + cancel_all + shutdown。"""
        src = self._src('gui.py')
        self.assertIn('def _on_closing', src)
        self.assertIn('self.win.protocol("WM_DELETE_WINDOW", self._on_closing)', src)
        self.assertIn('self._task_queue.cancel_all()', src)
        self.assertIn('shutdown(wait=False)', src)

    def test_review_trigger_includes_import(self):
        """复核触发必须含 import 路径。"""
        src = self._src('gui.py')
        self.assertIn("('live', 'file', 'batch', 'import')", src)

    def test_store_combo_disabled_when_busy(self):
        """批量中店铺切换器禁用态可见（批次C）。"""
        src = self._src('gui.py')
        self.assertIn("state='disabled' if busy else 'readonly'", src)

    def test_no_locals_antipattern_in_calc(self):
        """_calc_from_items 不得再有 locals() 反模式。"""
        import inspect
        import gui
        src = inspect.getsource(gui.App._calc_from_items)
        self.assertNotIn('locals()', src)

    def test_store_switch_reentrancy_guard(self):
        """切店重入守卫 + _store_id 先行。"""
        src = self._src('gui.py')
        self.assertIn('_store_switching', src)
        self.assertIn('def _apply_store_switch_locked', src)

    def test_batch_blur_seen_wired(self):
        """批量模糊证据现场采集 + _fill_from_ocr 批量分支。"""
        src = self._src('gui.py')
        self.assertIn('_batch_blur_seen', src)
        self.assertIn("if source == 'batch':", src)

    def test_stage5_wait_checks_stop(self):
        """阶段5 页面刷新等待循环检查 _batch_stop。"""
        src = self._src('gui.py')

    def test_stats_ui_history_enter_sync(self):
        """历史页进入与主页店铺联动：stats_ui 定义 + gui 调用。"""
        self.assertIn('def _history_page_enter', self._src('stats_ui.py'))
        self.assertIn('_history_page_enter', self._src('gui.py'))


if __name__ == '__main__':
    unittest.main()
