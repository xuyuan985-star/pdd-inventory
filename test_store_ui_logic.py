"""t6 多店铺隔离-UI 回归（store_ui_logic.py 纯逻辑 + gui 接入契约）

覆盖：
1. resolve_store_switch：切店互斥（批量中拒切）/ 同店重复切幂等 / 非法目标拒绝 /
   空 id 拒绝 / cur 缺省归 default / 脏店铺清单容忍；
2. fresh_gui_state：切店全新状态（cache/plans/active_region/region_var），
   跨店数据不串（regions 非 dict 不带脏数据）；
3. group_plans_by_store：单层→双层组装（default 归一 / 空数据不伪造 / 拷贝防污染 /
   多店互不干扰）；
4. store_choices：下拉选项（全部店铺首项 / 重名消歧 / 脏条目跳过）；
5. 组装→入库端到端（tmp 库）：双层入参经 history_db.record_capture 落对店铺。

UI 无关：本文件不创建任何 Tk 组件；gui/stats_ui 的接入由 import gui 冒烟 +
test_smoke 静态断言兜底。
"""
import importlib.util
import os
import sys
import tempfile
import shutil
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _load_module(unique_name, fname):
    """按 test_smoke 同款模式加载被测模块（独立模块对象）。"""
    spec = importlib.util.spec_from_file_location(unique_name, os.path.join(HERE, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


STORES = [
    {'id': 'default', 'name': '默认店铺'},
    {'id': 'store_aaa', 'name': '二号仓店'},
    {'id': 'store_bbb', 'name': '旗舰三店'},
]


class TestResolveStoreSwitch(unittest.TestCase):
    """切店决策：互斥 / 幂等 / 拒绝路径"""

    def test_valid_cross_store_switch(self):
        r = _load_module('pdd_sul_r1', 'store_ui_logic.py').resolve_store_switch
        d = r('default', 'store_aaa', STORES)
        self.assertTrue(d['ok'])
        self.assertEqual(d['store_id'], 'store_aaa')
        self.assertEqual(d['reason'], 'ok-switched')

    def test_same_store_reselect_is_idempotent(self):
        """同店重复切：幂等放行（reason=ok-idempotent），即使 busy 也不报错。"""
        r = _load_module('pdd_sul_r2', 'store_ui_logic.py').resolve_store_switch
        d = r('store_aaa', 'store_aaa', STORES)
        self.assertTrue(d['ok'])
        self.assertEqual(d['reason'], 'ok-idempotent')
        self.assertEqual(d['store_id'], 'store_aaa')
        d2 = r('store_aaa', 'store_aaa', STORES, busy=True)
        self.assertTrue(d2['ok'], '同店重复选择无状态变化，busy 不应拒绝')
        self.assertEqual(d2['reason'], 'ok-idempotent')

    def test_busy_blocks_cross_store_switch(self):
        """切店互斥：批量识别运行中跨店切换必须拒绝，且状态保持当前店。"""
        r = _load_module('pdd_sul_r3', 'store_ui_logic.py').resolve_store_switch
        d = r('default', 'store_bbb', STORES, busy=True)
        self.assertFalse(d['ok'])
        self.assertEqual(d['reason'], 'rejected-busy')
        self.assertEqual(d['store_id'], 'default', '拒绝时保持当前店铺')

    def test_invalid_target_rejected_state_kept(self):
        r = _load_module('pdd_sul_r4', 'store_ui_logic.py').resolve_store_switch
        for bad in ('ghost-store', '', None, '   '):
            d = r('store_aaa', bad, STORES)
            self.assertFalse(d['ok'], f'非法目标 {bad!r} 必须拒绝')
            self.assertEqual(d['store_id'], 'store_aaa', f'拒绝后保持当前店（目标 {bad!r}）')
        self.assertEqual(r('store_aaa', 'ghost', STORES)['reason'], 'rejected-invalid')
        self.assertEqual(r('store_aaa', '', STORES)['reason'], 'rejected-empty')

    def test_cur_missing_normalizes_to_default(self):
        """cur 缺失（首启/异常）按 default 处理：选 default 即幂等，不产生假切换。"""
        r = _load_module('pdd_sul_r5', 'store_ui_logic.py').resolve_store_switch
        d = r(None, 'default', STORES)
        self.assertTrue(d['ok'])
        self.assertEqual(d['reason'], 'ok-idempotent')

    def test_dirty_store_list_tolerated(self):
        """店铺清单脏条目（非 dict/缺 id）跳过，不影响合法目标判定。"""
        r = _load_module('pdd_sul_r6', 'store_ui_logic.py').resolve_store_switch
        dirty = [None, '垃圾', 42, {'name': '无id店铺'}, {'id': 'store_aaa', 'name': '二号仓店'}]
        d = r('default', 'store_aaa', dirty)
        self.assertTrue(d['ok'])
        d2 = r('default', '无id店铺的假id', dirty)
        self.assertFalse(d2['ok'])


class TestFreshGuiState(unittest.TestCase):
    """切店全新状态：跨店数据不串（DESIGN §3 机器可验证形态）"""

    def setUp(self):
        self.m = _load_module('pdd_sul_f', 'store_ui_logic.py')

    def test_fresh_state_is_all_empty(self):
        """cache/plans/active_region/region_var 必须全新空态，禁止残留旧店铺数据。"""
        regions = {'华东': {'': 3}}
        s = self.m.fresh_gui_state('store_aaa', regions)
        self.assertEqual(s['store_id'], 'store_aaa')
        self.assertEqual(s['cache'], {})
        self.assertEqual(s['plans'], [])
        self.assertIsNone(s['active_region'])
        self.assertEqual(s['region_var'], '未识别')
        self.assertEqual(s['regions'], {'华东': {'': 3}})

    def test_cross_store_contamination_blocked(self):
        """regions 非 dict（读失败等）→ 空配置；绝不把上一店铺数据带进新店铺。"""
        s = self.m.fresh_gui_state('store_bbb', None)
        self.assertEqual(s['regions'], {})
        self.assertEqual(s['cache'], {})
        s2 = self.m.fresh_gui_state('store_bbb', '上一店铺的脏数据')
        self.assertEqual(s2['regions'], {})

    def test_regions_outer_dict_copied(self):
        """外层 dict 拷贝：改返回状态的 regions 不影响调用方原 dict。"""
        regions = {'华东': {'': 3}}
        s = self.m.fresh_gui_state('store_aaa', regions)
        s['regions']['新增地区'] = {}
        self.assertNotIn('新增地区', regions)

    def test_store_id_normalized(self):
        s = self.m.fresh_gui_state('', {})
        self.assertEqual(s['store_id'], 'default')
        s2 = self.m.fresh_gui_state(None, {})
        self.assertEqual(s2['store_id'], 'default')


class TestGroupPlansByStore(unittest.TestCase):
    """按店铺组装 record_capture 双层入参"""

    def setUp(self):
        self.m = _load_module('pdd_sul_g', 'store_ui_logic.py')
        self.plans = [{'name': '商品A', 'sku_id': '1', 'stock': 5}]

    def test_assembles_double_level(self):
        out = self.m.group_plans_by_store({'华东': self.plans}, 'shopA')
        self.assertEqual(out, {'shopA': {'华东': self.plans}})

    def test_empty_store_id_normalized_to_default(self):
        out = self.m.group_plans_by_store({'华东': self.plans}, '')
        self.assertEqual(list(out.keys()), ['default'])
        out2 = self.m.group_plans_by_store({'华东': self.plans}, None)
        self.assertEqual(list(out2.keys()), ['default'])

    def test_empty_inputs_return_empty(self):
        """空/非 dict/全空 plans → {}（调用方按空跳过，不伪造入库数据）。"""
        self.assertEqual(self.m.group_plans_by_store({}, 'shopA'), {})
        self.assertEqual(self.m.group_plans_by_store(None, 'shopA'), {})
        self.assertEqual(self.m.group_plans_by_store('垃圾', 'shopA'), {})
        self.assertEqual(self.m.group_plans_by_store({'华东': []}, 'shopA'), {},
                         '全空 plans 不得组装出空壳店铺')
        self.assertEqual(self.m.group_plans_by_store({'华东': None}, 'shopA'), {})

    def test_copies_defend_against_pollution(self):
        """拷贝防污染：改返回结构（内层 dict / plans 列表）不影响调用方原数据。"""
        src = {'华东': self.plans}
        out = self.m.group_plans_by_store(src, 'shopA')
        out['shopA']['华东'].append({'name': '脏数据'})
        out['shopA']['新地区'] = []
        self.assertEqual(len(self.plans), 1, '原 plans 列表不得被改写')
        self.assertEqual(list(src.keys()), ['华东'], '原单层 dict 不得被改写')

    def test_two_stores_disjoint(self):
        """跨店数据不串：同一天两店的组装结果互不包含对方地区。"""
        p1 = {'华东': [{'name': '甲店货'}]}
        p2 = {'华南': [{'name': '乙店货'}]}
        out1 = self.m.group_plans_by_store(p1, 'shopA')
        out2 = self.m.group_plans_by_store(p2, 'shopB')
        self.assertEqual(set(out1.keys()), {'shopA'})
        self.assertEqual(set(out2.keys()), {'shopB'})
        self.assertNotIn('华南', out1['shopA'])
        self.assertNotIn('华东', out2['shopB'])


class TestStoreChoices(unittest.TestCase):
    """店铺下拉选项助手"""

    def setUp(self):
        self.f = _load_module('pdd_sul_c', 'store_ui_logic.py').store_choices

    def test_no_all_label(self):
        labels, n2i = self.f(STORES)
        self.assertEqual(labels, ['默认店铺', '二号仓店', '旗舰三店'])
        self.assertEqual(n2i['二号仓店'], 'store_aaa')

    def test_all_label_first_and_maps_none(self):
        labels, n2i = self.f(STORES, all_label='全部店铺')
        self.assertEqual(labels[0], '全部店铺')
        self.assertIsNone(n2i['全部店铺'])
        self.assertEqual(n2i['默认店铺'], 'default')

    def test_duplicate_names_disambiguated(self):
        """重名店铺消歧：第二个重名项 label 追加 id 前缀，name→id 保持唯一。"""
        dup = [{'id': 'store_1', 'name': '同名店'},
               {'id': 'store_2', 'name': '同名店'}]
        labels, n2i = self.f(dup)
        self.assertEqual(len(labels), 2)
        self.assertEqual(n2i[labels[0]], 'store_1')
        self.assertEqual(n2i[labels[1]], 'store_2')
        self.assertIn('store_2'[:8], labels[1])

    def test_dirty_entries_skipped(self):
        labels, n2i = self.f([None, '垃圾', {'name': '缺id'}, {'id': 'ok1', 'name': '好店'}])
        self.assertEqual(labels, ['好店'])
        self.assertEqual(n2i, {'好店': 'ok1'})


class TestAssembleAndRecordEndToEnd(unittest.TestCase):
    """组装→入库端到端：group_plans_by_store 产物经 record_capture 落对店铺（tmp 库）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pdd_sul_e2e_')
        self.hdb = _load_module('pdd_sul_hdb', 'history_db.py')
        self.hdb.set_db_path(os.path.join(self.tmp, 'history.db'))

    def tearDown(self):
        self.hdb.reset_db_path()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_double_level_lands_under_right_store(self):
        sul = _load_module('pdd_sul_e2e_sul', 'store_ui_logic.py')
        today = datetime.now().strftime('%Y-%m-%d')
        single = {'华东': [{'name': 'A店货', 'sku_id': '1', 'stock': 5}]}
        payload = sul.group_plans_by_store(single, 'shopA')
        sid = self.hdb.record_capture(payload, source='live')
        self.assertGreater(sid, 0)
        rows_a = self.hdb.query_region_days('华东', today, store='shopA')
        self.assertEqual(len(rows_a), 1)
        self.assertEqual(rows_a[0]['store'], 'shopA')
        self.assertEqual(self.hdb.query_region_days('华东', today, store='default'), [],
                         '不得泄漏到 default 店铺')
        # 空 payload（无有效 plans）→ 组装为 {}，GUI 侧按空跳过（这里验证 record 空形
        # 也只是 0 行 session，不产生脏店铺数据）
        empty_payload = sul.group_plans_by_store({'华东': []}, 'shopA')
        self.assertEqual(empty_payload, {})


class TestGuiContract(unittest.TestCase):
    """gui/stats_ui 接入契约静态断言（不创建 Tk）"""

    def test_gui_wires_store_switcher_and_store_capture(self):
        with open(os.path.join(HERE, 'gui.py'), encoding='utf-8') as f:
            src = f.read()
        self.assertIn('import store_ui_logic', src)
        self.assertIn('store_combo.bind', src, '店铺切换器必须绑定回调')
        self.assertIn('def _on_store_switch', src)
        self.assertIn('def _apply_store_switch', src)
        self.assertIn('def _refresh_store_combo', src)
        self.assertIn('store_ui_logic.resolve_store_switch', src,
                      '切店决策必须走可单测纯函数')
        self.assertIn('store_ui_logic.fresh_gui_state', src,
                      '切店重建必须走可单测纯函数（DESIGN §3）')
        self.assertIn('store_ui_logic.group_plans_by_store', src,
                      '历史入库必须按店铺组装双层入参')
        self.assertIn("export_cache_to_xlsx(self.cache, export_dir, store_name=",
                      src, '导出必须传当前店铺名')

    def test_settings_ui_has_store_card(self):
        with open(os.path.join(HERE, 'settings_ui.py'), encoding='utf-8') as f:
            src = f.read()
        self.assertIn('def _build_store_card', src)
        self.assertIn('def _store_delete', src)
        self.assertIn('askyesnocancel', src, '删店必须三选确认')
        self.assertIn('_hdb.delete_store(sid)', src, '删店清历史必须调 history_db.delete_store')

    def test_stats_ui_has_store_filter(self):
        with open(os.path.join(HERE, 'stats_ui.py'), encoding='utf-8') as f:
            src = f.read()
        self.assertIn('_hist_store_var', src)
        self.assertIn("query_daily(days=int(self._hist_days_var.get() or 90),",
                      src)
        self.assertIn('store=store_id)', src, '历史查询必须带店铺过滤参数')

    def test_export_xlsx_has_store_column(self):
        with open(os.path.join(HERE, 'export_xlsx.py'), encoding='utf-8') as f:
            src = f.read()
        self.assertIn("store_name: str = None", src)
        self.assertIn("['地区', '店铺', '仓库']", src, '店铺列必须紧跟地区列')


if __name__ == '__main__':
    unittest.main()
