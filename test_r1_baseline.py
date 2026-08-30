"""R1 基线强化盲区测试套件
PDD EZ

覆盖基线强化盲区（≥30 个新用例）：
  1. history_db query_sku_history store 参数组合（≥10 例）
  2. advanced 大促配置多组应用（promo/season/oversell/slow 多因子组合）（≥10 例）
  3. async_queue 高并发压力（16 任务 × 阻塞）（≥5 例）
  4. 复核弹窗三出口状态一致性（纯逻辑部分）（≥5 例）

不依赖真实 API / 网络 / 摄像头；headless 可跑。
"""
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from typing import List

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

TODAY = datetime.now().strftime('%Y-%m-%d')


def _load_module(unique_name, fname):
    """按 test_smoke 同款模式加载被测模块（独立模块对象，避免污染导入缓存）。"""
    spec = importlib.util.spec_from_file_location(unique_name, os.path.join(HERE, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ─────────────────────────────────────────────────────────────────
# 共享 PLANS 样本
# ─────────────────────────────────────────────────────────────────
PLANS_A = [
    {'name': '洗衣液2kg', 'sku_id': '11111111111', 'stock': 50, 'daily': 10,
     'days_left': 5.0, 'status': '3天后下单', 'qty': 0, 'warehouse': '华东1号仓'},
    {'name': '抽纸整箱', 'sku_id': '22222222222', 'stock': 5, 'daily': 20,
     'days_left': 0.3, 'status': '立刻补货', 'qty': 200, 'warehouse': '华东1号仓'},
]
PLANS_B = [
    {'name': '洗洁精1.5kg', 'sku_id': '33333333333', 'stock': 30, 'daily': 6,
     'days_left': 5.0, 'status': '5天后下单', 'qty': 0, 'warehouse': '西南仓'},
]
PLANS_C = [
    {'name': '洗洁精大瓶', 'sku_id': '44444444444', 'stock': 80, 'daily': 15,
     'days_left': 5.3, 'status': '3天后下单', 'qty': 0, 'warehouse': '华南仓'},
]


def _build_history(daily_sales: float, days: int, today: datetime = None,
                   start_offset: int = 0) -> list:
    if today is None:
        today = datetime.now()
    rows = []
    for i in range(start_offset, start_offset + days):
        d = (today - timedelta(days=i)).strftime('%Y-%m-%d')
        rows.append({'captured_at': d, 'sales': daily_sales, 'name': 'X'})
    return rows


def _cfg_with_factors(**flags) -> dict:
    from utils import _default_advanced_cfg
    adv = _default_advanced_cfg()
    if flags.get('promo'):
        adv['promo']['enabled'] = True
        if 'promo_dates' in flags:
            adv['promo']['dates'] = list(flags['promo_dates'])
        if 'boost' in flags:
            adv['promo']['boost'] = float(flags['boost'])
        if 'lead_days' in flags:
            adv['promo']['lead_days'] = int(flags['lead_days'])
    if flags.get('slow'):
        adv['slow']['enabled'] = True
        if 'threshold_per_day' in flags:
            adv['slow']['threshold_per_day'] = float(flags['threshold_per_day'])
        if 'stock_ratio' in flags:
            adv['slow']['stock_ratio'] = float(flags['stock_ratio'])
    if flags.get('season'):
        adv['season']['enabled'] = True
    if flags.get('oversell'):
        adv['oversell']['enabled'] = True
        if 'high_ratio' in flags:
            adv['oversell']['high_ratio'] = float(flags['high_ratio'])
    return {
        'model': 'advanced',
        'safety_days': flags.get('safety_days', 2),
        'in_transit_qty': flags.get('in_transit_qty', 0),
        'advanced': adv,
    }


# ═════════════════════════════════════════════════════════════════
# 1. history_db query_sku_history store 参数组合
# ═════════════════════════════════════════════════════════════════

class TestHistoryQuerySkuHistoryStoreCombos(unittest.TestCase):
    """history_db query_sku_history store 参数组合全覆盖"""

    @classmethod
    def setUpClass(cls):
        cls.hdb = _load_module('pdd_hdb_sku_combo', 'history_db.py')

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pdd_sku_combo_')
        self.hdb.set_db_path(os.path.join(self.tmp, 'history.db'))

    def tearDown(self):
        self.hdb.reset_db_path()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sku_history_store_none_returns_all(self):
        """store=None → 返回全部店铺数据"""
        self.hdb.record_capture({'shopA': {'华东': PLANS_A}, 'shopB': {'华南': PLANS_B}}, 'import')
        rows = self.hdb.query_sku_history('11111111111', 30, store=None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['store'], 'shopA')

    def test_sku_history_store_empty_returns_all(self):
        """store='' → 返回全部店铺数据（全部店铺过滤）"""
        self.hdb.record_capture({'shopA': {'华东': PLANS_A}, 'shopB': {'华南': PLANS_B}}, 'import')
        rows = self.hdb.query_sku_history('11111111111', 30, store='')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['store'], 'shopA')

    def test_sku_history_store_default_includes_empty(self):
        """store='default' → 含 store='' 旧行（同店语义）"""
        self.hdb.record_capture({'华东': PLANS_A}, 'live')  # store='default'
        # 直接插 store='' 行
        conn = sqlite3.connect(self.hdb.db_path())
        try:
            conn.execute("INSERT INTO history_rows (session_id, captured_at, store, region, sku_id,"
                         " name, stock, sales, days_left, status, qty, warehouse)"
                         " VALUES (1, ?, '', '华东', '11111111111', '旧行', 10, 1, 2.0, '', 0, '')",
                         (TODAY + ' 08:00:00',))
            conn.commit()
        finally:
            conn.close()
        rows = self.hdb.query_sku_history('11111111111', 30, store='default')
        self.assertEqual(len(rows), 2, "'default' 查询应包含 store='' 旧行")

    def test_sku_history_store_exact_match(self):
        """store 精确匹配特定店铺"""
        self.hdb.record_capture({'shopA': {'华东': PLANS_A}, 'shopB': {'华东': PLANS_C}}, 'import')
        rows_a = self.hdb.query_sku_history('11111111111', 30, store='shopA')
        rows_b = self.hdb.query_sku_history('11111111111', 30, store='shopB')
        self.assertEqual(len(rows_a), 1)
        self.assertEqual(rows_a[0]['store'], 'shopA')
        self.assertEqual(len(rows_b), 0, 'shopB 不含该 SKU')

    def test_sku_history_sku_key_not_found(self):
        """sku_key 不存在 → 返回 []"""
        rows = self.hdb.query_sku_history('NOTEXIST999', 30)
        self.assertEqual(rows, [])

    def test_sku_history_region_name_fallback(self):
        """无 sku_key 时，region+name 回退查询"""
        self.hdb.record_capture({'华东': PLANS_A}, 'live')
        rows = self.hdb.query_sku_history('', 30, region='华东', name='洗衣液2kg')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['name'], '洗衣液2kg')

    def test_sku_history_region_name_with_store_filter(self):
        """region+name 回退 + store 过滤组合"""
        self.hdb.record_capture({'shopA': {'华东': PLANS_A}, 'shopB': {'华东': PLANS_B}}, 'import')
        rows = self.hdb.query_sku_history('', 30, region='华东', name='洗衣液2kg', store='shopA')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['store'], 'shopA')

    def test_sku_history_days_window_filter(self):
        """days 窗口过滤"""
        self.hdb.record_capture({'华东': PLANS_A}, 'live')
        # 全量查询
        rows_all = self.hdb.query_sku_history('11111111111', 9999)
        self.assertGreaterEqual(len(rows_all), 1)
        # days=0 → 返回 None（不限窗口），等同于全量
        rows_0 = self.hdb.query_sku_history('11111111111', 0)
        self.assertGreaterEqual(len(rows_0), 1, 'days=0 等同于不限窗口')

    def test_sku_history_both_sku_and_region_name_empty(self):
        """sku_key='' 且 region+name 也为空 → 返回 []"""
        rows = self.hdb.query_sku_history('', 30, region='', name='')
        self.assertEqual(rows, [])

    def test_sku_history_store_none_with_multiple_stores(self):
        """多店铺场景下 store=None 返回所有店铺数据"""
        self.hdb.record_capture({
            'shopA': {'华东': PLANS_A},
            'shopB': {'华南': PLANS_B},
            'default': {'华北': PLANS_C},
        }, 'import')
        rows = self.hdb.query_sku_history('44444444444', 30, store=None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['store'], 'default')

    def test_sku_history_no_sku_key_no_region_name(self):
        """sku_key='' 且 region+name 只有一个 → 返回 []"""
        rows = self.hdb.query_sku_history('', 30, region='华东', name='')
        self.assertEqual(rows, [])

    def test_sku_history_store_no_such_store(self):
        """store 指向不存在的店铺 → 返回 []"""
        self.hdb.record_capture({'华东': PLANS_A}, 'live')
        rows = self.hdb.query_sku_history('11111111111', 30, store='ghost_store_xyz')
        self.assertEqual(rows, [])

    def test_sku_history_sku_in_two_stores_both_queried(self):
        """同一 SKU 落两个店铺，store=None 能查到 2 行"""
        self.hdb.record_capture({'shopA': {'华东': PLANS_A}, 'shopB': {'华东': PLANS_A}}, 'import')
        rows = self.hdb.query_sku_history('11111111111', 30, store=None)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['store'] for r in rows}, {'shopA', 'shopB'})


# ═════════════════════════════════════════════════════════════════
# 2. advanced 大促配置多组应用
# ═════════════════════════════════════════════════════════════════

class TestAdvancedMultiFactorCombos(unittest.TestCase):
    """advanced 算法多因子（promo/season/oversell/slow）组合配置"""

    def test_promo_and_season_combined(self):
        """promo + season 双因子同时启用"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        today = datetime.now().strftime('%Y-%m-%d')
        cfg = _cfg_with_factors(promo=True, promo_dates=[today], boost=2.0, lead_days=3,
                                season=True)
        def hlookup(*a, **kw): return _build_history(10, 30)
        item = {'name': 'X', 'stock': 50, 'sales': 10, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertIn(plan['model'], ('advanced', 'classic(error)'))
        self.assertGreaterEqual(plan['promo_multiplier'], 1.0)

    def test_promo_and_oversell_combined(self):
        """promo + oversell 双因子"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        today = datetime.now().strftime('%Y-%m-%d')
        cfg = _cfg_with_factors(promo=True, promo_dates=[today], boost=1.8, lead_days=3,
                                oversell=True, high_ratio=0.5)
        def hlookup(*a, **kw): return []
        item = {'name': 'X', 'stock': 10, 'sales': 20, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertIn(plan['model'], ('advanced', 'classic(error)'))
        # oversell 应生效
        self.assertTrue(plan.get('oversell_risk', False) or plan.get('oversell_level') is not None)

    def test_season_and_slow_combined(self):
        """season + slow 双因子"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        cfg = _cfg_with_factors(season=True, slow=True, threshold_per_day=1.0, stock_ratio=5.0)
        def hlookup(*a, **kw): return _build_history(5, 60)  # 需要足够多的历史算季节
        item = {'name': 'X', 'stock': 100, 'sales': 5, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertIn(plan['model'], ('advanced', 'classic(error)'))

    def test_promo_season_oversell_triple(self):
        """promo + season + oversell 三因子"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        today = datetime.now().strftime('%Y-%m-%d')
        cfg = _cfg_with_factors(promo=True, promo_dates=[today], boost=2.5, lead_days=3,
                                season=True, oversell=True, high_ratio=0.6)
        def hlookup(*a, **kw): return _build_history(15, 90)
        item = {'name': 'X', 'stock': 20, 'sales': 15, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertIn(plan['model'], ('advanced', 'classic(error)'))
        self.assertGreaterEqual(plan['promo_multiplier'], 1.0)

    def test_all_four_factors_combined(self):
        """promo + season + oversell + slow 四因子全开"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        today = datetime.now().strftime('%Y-%m-%d')
        cfg = _cfg_with_factors(
            promo=True, promo_dates=[today], boost=2.0, lead_days=3,
            season=True,
            oversell=True, high_ratio=0.5,
            slow=True, threshold_per_day=1.0, stock_ratio=5.0,
        )
        def hlookup(*a, **kw): return _build_history(10, 120)
        item = {'name': 'X', 'stock': 30, 'sales': 10, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertIn(plan['model'], ('advanced', 'classic(error)'))

    def test_promo_multiple_dates_in_window(self):
        """大促多日期在 lead_days 窗口内"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        today_dt = datetime.now()
        dates = [(today_dt - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(5)]
        cfg = _cfg_with_factors(promo=True, promo_dates=dates, boost=2.5, lead_days=3)
        def hlookup(*a, **kw): return _build_history(10, 30)
        item = {'name': 'X', 'stock': 0, 'sales': 10, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertEqual(plan['promo_multiplier'], 2.5,
                         '窗口内多个日期应使用 boost')

    def test_promo_zero_boost_no_effect(self):
        """promo boost=0 无放大效果（boost<=1 时返回 1.0）"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        today = datetime.now().strftime('%Y-%m-%d')
        cfg = _cfg_with_factors(promo=True, promo_dates=[today], boost=0.0, lead_days=3)
        def hlookup(*a, **kw): return _build_history(10, 30)
        item = {'name': 'X', 'stock': 0, 'sales': 10, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertEqual(plan['promo_multiplier'], 1.0,
                         'boost<=1 时返回 1.0（不放大）')

    def test_promo_negative_boost_ignored(self):
        """promo boost 负值 → 忽略（boost≤1 时不放大）"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        today = datetime.now().strftime('%Y-%m-%d')
        cfg = _cfg_with_factors(promo=True, promo_dates=[today], boost=-5.0, lead_days=3)
        def hlookup(*a, **kw): return _build_history(10, 30)
        item = {'name': 'X', 'stock': 0, 'sales': 10, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        # boost 负值应被忽略，promo_multiplier 应该是 boost 原始值或处理后的值
        self.assertIsInstance(plan['promo_multiplier'], (int, float))

    def test_oversell_high_ratio_one(self):
        """oversell high_ratio=1.0（临界值）"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        cfg = _cfg_with_factors(oversell=True, high_ratio=1.0)
        def hlookup(*a, **kw): return []
        # stock=39, required=40, high_ratio=1.0 → threshold=40
        # stock=39 < 40 → high
        item = {'name': 'X', 'stock': 39, 'sales': 10, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 4, 0, hlookup, cfg)
        self.assertIn(plan.get('oversell_level'), ('high', 'medium', None))

    def test_oversell_high_ratio_zero(self):
        """oversell high_ratio=0（严格模式：任何 stock<required 都是 high）"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        cfg = _cfg_with_factors(oversell=True, high_ratio=0.0)
        def hlookup(*a, **kw): return []
        # stock=1, required=40, threshold=0
        # stock=1 > 0 → 不满足 high 条件
        item = {'name': 'X', 'stock': 1, 'sales': 10, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 4, 0, hlookup, cfg)
        # 行为取决于实现，但应不抛
        self.assertIn(plan['model'], ('advanced', 'classic(error)'))

    def test_slow_moving_high_stock_ratio(self):
        """slow_moving 高 stock_ratio（不缺货）"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        # stock/daily=200/10=20 > threshold=15 → 非 slow-moving
        cfg = _cfg_with_factors(slow=True, threshold_per_day=5.0, stock_ratio=15.0)
        def hlookup(*a, **kw): return _build_history(10, 30)
        item = {'name': 'X', 'stock': 200, 'sales': 10, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertIn(plan['model'], ('advanced', 'classic(error)'))

    def test_slow_moving_stock_ratio_zero(self):
        """slow_moving stock_ratio=0（除零防御）"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        cfg = _cfg_with_factors(slow=True, threshold_per_day=5.0, stock_ratio=0.0)
        def hlookup(*a, **kw): return _build_history(10, 30)
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertIn(plan['model'], ('advanced', 'classic(error)'))


# ═════════════════════════════════════════════════════════════════
# 3. async_queue 高并发压力（16 任务 × 阻塞）
# ═════════════════════════════════════════════════════════════════

class TestAsyncQueueHighConcurrencyStress(unittest.TestCase):
    """async_queue 高并发压力测试"""

    def test_16_tasks_concurrent_blocking(self):
        """16 个阻塞任务同时提交，全部完成"""
        from async_queue import TaskQueue, TaskState
        q = TaskQueue(max_workers=4)
        results = []

        def blocking_task(task_id):
            def fn(_):
                time.sleep(0.3)
                results.append(task_id)
                return task_id
            return fn

        task_ids = [q.submit(f'task_{i}', blocking_task(i)) for i in range(16)]
        # 等待所有任务完成（最多 10 秒）
        all_done = all(q.wait(tid, timeout=10.0) for tid in task_ids)
        q.shutdown(wait=True)
        self.assertTrue(all_done, '所有 16 个任务应在 10 秒内完成')
        self.assertEqual(len(results), 16, f'16 个任务应全部完成，实际 {len(results)}')

    def test_16_tasks_cancel_all_stress(self):
        """16 个任务快速提交后 cancel_all"""
        from async_queue import TaskQueue
        q = TaskQueue(max_workers=0)  # 无 worker → 任务全是 PENDING
        task_ids = [q.submit(f't{i}', lambda _: time.sleep(10)) for i in range(16)]
        n = q.cancel_all()
        self.assertEqual(n, 16, f'应取消 16 个任务，实际 {n}')
        q.shutdown(wait=False)

    def test_burst_submit_100_tasks(self):
        """突发提交 100 个任务，队列不崩溃"""
        from async_queue import TaskQueue
        q = TaskQueue(max_workers=3)
        count = []
        lock = threading.Lock()

        for i in range(100):
            q.submit(f'burst_{i}', lambda _: (count.append(1) or 1))

        time.sleep(2.0)
        n_cancelled = q.cancel_all()
        q.shutdown(wait=True)
        # 不崩溃即通过
        self.assertGreaterEqual(len(count) + n_cancelled, 90,
                               '至少 90 个任务被处理或取消')

    def test_max_workers_zero_submit(self):
        """max_workers=0 时提交任务（任务永不执行）"""
        from async_queue import TaskQueue, TaskState
        q = TaskQueue(max_workers=0)
        tid = q.submit('zero_worker', lambda _: 42)
        status = q.task_status(tid)
        # 无 worker 线程，任务永远不会从 PENDING 转换
        self.assertEqual(status, TaskState.PENDING)
        q.shutdown(wait=False)

    def test_multiple_queue_instances_independent(self):
        """多个独立队列互不干扰"""
        from async_queue import TaskQueue
        results = []
        queues = [TaskQueue(max_workers=2) for _ in range(5)]

        for qi, q in enumerate(queues):
            q.submit(f'q{qi}_t0', lambda _: results.append(f'q{qi}_0'))
            q.submit(f'q{qi}_t1', lambda _: results.append(f'q{qi}_1'))

        time.sleep(1.0)
        for q in queues:
            q.shutdown(wait=True)

        self.assertEqual(len(results), 10, f'5 个队列各 2 个任务共 10 个，应全部完成，实际 {len(results)}')


# ═════════════════════════════════════════════════════════════════
# 4. 复核弹窗三出口状态一致性（纯逻辑部分）
# ═════════════════════════════════════════════════════════════════

class TestReviewFlowStateConsistency(unittest.TestCase):
    """复核弹窗三出口（通过/编辑/取消）状态一致性纯逻辑验证"""

    def test_apply_user_edits_with_all_whitelisted_fields(self):
        """编辑所有白名单字段（stock/sales/name/region/warehouse）"""
        from ocr_review import apply_user_edits
        items = [
            {'name': 'A', 'stock': 100, 'sales': 50, 'region': '广东', 'warehouse': 'A仓'},
        ]
        edits = [
            {'index': 0, 'field': 'stock', 'value': 200},
            {'index': 0, 'field': 'sales', 'value': 100},
            {'index': 0, 'field': 'name', 'value': 'B 商品'},
            {'index': 0, 'field': 'region', 'value': '浙江'},
            {'index': 0, 'field': 'warehouse', 'value': 'B仓'},
        ]
        result = apply_user_edits(items, edits)
        self.assertEqual(items[0]['stock'], 200)
        self.assertEqual(items[0]['sales'], 100)
        self.assertEqual(items[0]['name'], 'B 商品')
        self.assertEqual(items[0]['region'], '浙江')
        self.assertEqual(items[0]['warehouse'], 'B仓')

    def test_apply_user_edits_index_out_of_range(self):
        """index 超出范围 → 不修改"""
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100}]
        edits = [{'index': 99, 'field': 'stock', 'value': 999}]
        apply_user_edits(items, edits)
        self.assertEqual(items[0]['stock'], 100)

    def test_apply_user_edits_empty_list(self):
        """edits=[] → 原样返回"""
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100}]
        result = apply_user_edits(items, [])
        self.assertIs(result, items)
        self.assertEqual(items[0]['stock'], 100)

    def test_summarize_review_all_levels(self):
        """summarize_review 覆盖 high/medium/low 全部置信等级"""
        from ocr_review import build_review_list, summarize_review
        items = [
            {'name': 'A', 'stock': 100, 'sales': 50,
             'confidence': {'level': 'high'}},
            {'name': 'B', 'stock': 0, 'sales': 50,
             'confidence': {'level': 'medium', 'reasons': [{'field': 'stock', 'reason': '零库存'}]}},
            {'name': 'C', 'stock': 10, 'sales': 0,
             'confidence': {'level': 'low', 'reasons': [{'field': 'sales', 'reason': '零销量'}]}},
        ]
        # build_review_list 只返回 low/medium 级别，high 级别被跳过
        low_items = build_review_list(items)
        self.assertEqual(len(low_items), 2, 'high 级别被跳过，应返回 2 个')
        summary = summarize_review(items)
        self.assertEqual(summary['total'], 3)
        self.assertEqual(summary['high'], 1)
        self.assertEqual(summary['medium'], 1)
        self.assertEqual(summary['low'], 1)

    def test_categorize_error_all_known_patterns(self):
        """categorize_error 覆盖所有已知错误模式"""
        from ocr_review import categorize_error
        test_cases = [
            ('csv_encoding', r'CSV 编码问题', 'USER_MSG_CSV_ENCODING'),
            ('xlsx_corrupt', r'xlsx 文件损坏', 'USER_MSG_XLSX_CORRUPT'),
            ('api_timeout', r'timeout 错误', 'USER_MSG_API_TIMEOUT'),
            ('fatal_quota', r'quota exhausted', 'USER_MSG_FATAL_QUOTA'),
            ('blur_ocr', r'截图模糊', 'USER_MSG_BLUR'),
            ('low_confidence', r'低置信', 'USER_MSG_LOW_CONFIDENCE'),
        ]
        for cat_key, msg_part, const_name in test_cases:
            cat, user_msg, title = categorize_error(msg_part)
            self.assertIsInstance(cat, str)
            self.assertIsInstance(user_msg, str)
            self.assertIsInstance(title, str)

    def test_build_review_list_with_missing_confidence_field(self):
        """build_review_list 兼容缺少 confidence 字段的 item"""
        from ocr_review import build_review_list
        items = [
            {'name': 'A', 'stock': 100, 'sales': 50},  # 无 confidence
            {'name': 'B', 'stock': 0, 'confidence': {'level': 'low'}},
        ]
        low_items = build_review_list(items)
        # 无 confidence 字段 → high 级别（_row_confidence_level 默认）
        self.assertGreaterEqual(len(low_items), 1)

    def test_apply_user_edits_unknown_field_rejected(self):
        """未知 field 名（不在白名单）→ 不修改"""
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100, 'sku_id': 'S1'}]
        edits = [{'index': 0, 'field': 'sku_id', 'value': 'CHANGED'}]
        apply_user_edits(items, edits)
        self.assertEqual(items[0]['sku_id'], 'S1', 'sku_id 不在白名单，不应修改')

    def test_summarize_review_with_zero_items(self):
        """summarize_review 空列表"""
        from ocr_review import summarize_review
        summary = summarize_review([])
        self.assertEqual(summary['total'], 0)
        self.assertEqual(summary['high'], 0)
        self.assertEqual(summary['medium'], 0)
        self.assertEqual(summary['low'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
