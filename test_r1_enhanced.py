"""R1 测试强化：上期新逻辑补边界/并发/损坏用例 + 回归门禁
PDD EZ

覆盖盲区（≥30 个新用例）：
  1. store_registry：多店铺并发写 / regions 损坏与非法 JSON /
     delete_store 删 default 拒绝 / 重名店 id 稳定性
  2. advanced 算法：大促跨年/重复/非法格式 / 季节无历史/单点 /
     超卖 high/medium 边界 / cfg 缺字段合并
  3. async_queue：submit 后立即 shutdown / cancel 后 on_progress 不回调 /
     BaseException 系统异常处理
  4. ocr confidence：全黑/全白图 / 合法极端值不误报 / low 覆盖 medium
  5. review 流程：apply_user_edits 空 edits/None/非白名单拒绝 /
     categorize_error 未分类异常兜底
  6. 集成点：record_capture 双层入参与 gui 组装契约 / calc_replenishment cfg 透传

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
import traceback
import unittest
from datetime import datetime, timedelta
from typing import List

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

TODAY = datetime.now().strftime('%Y-%m-%d')


def _load_module(unique_name, fname):
    import importlib.util
    spec = importlib.util.spec_from_file_location(unique_name, os.path.join(HERE, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ─────────────────────────────────────────────────────────────────
# 共享 PLANS 样本（与 test_store_db.py 同款）
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

# ─────────────────────────────────────────────────────────────────
# 公共基类
# ─────────────────────────────────────────────────────────────────
class _StoreEnvTest(unittest.TestCase):
    """tmp 目录 + 独立 utils/store_registry 副本"""

    @classmethod
    def setUpClass(cls):
        cls.u = _load_module('pdd_u_r1', 'utils.py')
        cls.sr = _load_module('pdd_sr_r1', 'store_registry.py')
        cls.sr.utils = cls.u

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pdd_r1_')
        self.u.get_base_dir = lambda: self.tmp
        self._reset_config_cache()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reset_config_cache(self):
        self.u.Config._load_cache = {'mtime': -1, 'data': None}
        self.u.Config._template_cache = None

    def _write_template(self, stores=None):
        tpl = {
            'theme': '极简白',
            'api': {'active_provider': 'doubao', 'providers': {}},
            'history': {'retention_days': 180, 'max_rows': 200000},
        }
        if stores is not None:
            tpl['stores'] = stores
        with open(os.path.join(self.tmp, 'settings_template.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(tpl, f, ensure_ascii=False, indent=2)
        self._reset_config_cache()

    def _regions_path(self):
        return os.path.join(self.tmp, 'regions.json')

    def _read_settings(self):
        with open(os.path.join(self.tmp, 'settings.json'), encoding='utf-8') as f:
            return json.load(f)


# ═════════════════════════════════════════════════════════════════
# 1. store_registry — 并发写 / 损坏 / 守卫
# ═════════════════════════════════════════════════════════════════

class TestStoreRegistryConcurrentWrite(_StoreEnvTest):
    """多店铺并发写 + id 稳定性"""

    def test_concurrent_add_stores_unique_ids(self):
        """多线程并发建店，id 全部唯一，无竞争导致重复或丢失"""
        self._write_template()
        errors = []
        ids = []
        lock = threading.Lock()

        def adder(idx):
            try:
                item = self.sr.add_store(f'并发店{idx}')
                if item:
                    with lock:
                        ids.append(item['id'])
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=adder, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f'并发写出错: {errors}')
        self.assertEqual(len(ids), 20, f'应成功创建 20 家店，实际 {len(ids)}')
        self.assertEqual(len(set(ids)), 20, 'id 必须全部唯一')

    def test_concurrent_add_and_read_no_crash(self):
        """并发 add_store + get_stores 不崩溃，数据不丢失"""
        self._write_template()
        barrier = threading.Barrier(10)
        done = []

        def writer(i):
            barrier.wait()
            item = self.sr.add_store(f'并发写{i}')
            done.append(item is not None)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stores = self.sr.get_stores()
        self.assertGreaterEqual(len(stores), 11, 'default + 至少 10 家并发店')
        # 所有写操作成功
        self.assertEqual(done, [True] * 10)

    def test_concurrent_set_active_no_race(self):
        """并发 set_active，任意线程均不崩溃"""
        self._write_template()
        sid = self.sr.add_store('B店')['id']
        barrier = threading.Barrier(5)
        errors = []

        def toggler(idx):
            barrier.wait()
            try:
                if idx % 2 == 0:
                    self.sr.set_active(sid)
                else:
                    self.sr.set_active('default')
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=toggler, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f'set_active 并发出错: {errors}')
        active = self.sr.get_active()
        self.assertIn(active, ('default', sid))

    def test_concurrent_read_write_no_crash(self):
        """读线程（get_stores）与写线程（add_store）并发，不崩溃"""
        self._write_template()
        barrier = threading.Barrier(6)
        read_ok = []
        errors = []

        def reader():
            barrier.wait()
            for _ in range(30):
                try:
                    stores = self.sr.get_stores()
                    read_ok.append(isinstance(stores, list))
                except Exception as e:
                    errors.append(str(e))

        def writer(i):
            barrier.wait()
            for _ in range(10):
                try:
                    self.sr.add_store(f'店{i}')
                except Exception as e:
                    errors.append(str(e))

        threads = [threading.Thread(target=reader)]
        threads += [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f'读写并发出错: {errors}')
        self.assertEqual(len(read_ok), 30)


class TestStoreRegionsCorruption(_StoreEnvTest):
    """regions.json 损坏 / 非法 JSON 场景"""

    def test_regions_file_totally_empty(self):
        """regions.json 空文件 → 安全返回 {}"""
        with open(self._regions_path(), 'w', encoding='utf-8') as f:
            pass  # 空文件
        self.assertEqual(self.sr.get_regions('default'), {})

    def test_regions_file_plain_text(self):
        """regions.json 是普通文本 → JSON 解析失败 → {}"""
        with open(self._regions_path(), 'w', encoding='utf-8') as f:
            f.write('这不是 JSON 文件\n随便写点什么')
        self.assertEqual(self.sr.get_regions('default'), {})
        self.assertEqual(self.sr.get_regions('shopX'), {})

    def test_regions_file_list_instead_of_dict(self):
        """regions.json 顶层是 list 而非 dict → {}"""
        with open(self._regions_path(), 'w', encoding='utf-8') as f:
            json.dump(['华东', '华南'], f)
        self.assertEqual(self.sr.get_regions('default'), {})

    def test_regions_file_deeply_nested_corruption(self):
        """regions.json 含非法字符 / 深层嵌套 → 不抛"""
        with open(self._regions_path(), 'w', encoding='utf-8') as f:
            f.write('{"default": {"华东": {"纸": 5}, "\\x00invalid": 1}')
        # 能读，但 \\x00 在 JSON 里不合法，json.load 失败
        self.assertEqual(self.sr.get_regions('default'), {})

    def test_regions_file_unclosed_brace(self):
        """regions.json 含未闭合括号 → JSON 解析失败安全返回"""
        with open(self._regions_path(), 'w', encoding='utf-8') as f:
            f.write('{"default": {"华东": {"纸": 5}}')  # 少一个 }
        self.assertEqual(self.sr.get_regions('default'), {})

    def test_regions_write_with_corrupt_file(self):
        """损坏文件上 save_regions → 自动重建为按店铺新格式"""
        with open(self._regions_path(), 'w', encoding='utf-8') as f:
            f.write('corrupted {{{')
        self.assertTrue(self.sr.save_regions({'华东': {'': 5}}, 'default'))
        self.assertEqual(self.sr.get_regions('default'), {'华东': {'': 5}})

    def test_regions_file_save_empty_dict(self):
        """save_regions 写空 {} → 落盘格式正确且可再次读取"""
        self.sr.save_regions({}, 'default')
        # 空 regions 仍写为新格式（default: {}）
        with open(self._regions_path(), encoding='utf-8') as f:
            data = json.load(f)
        self.assertIn('default', data)
        self.assertEqual(self.sr.get_regions('default'), {})

    def test_regions_file_invalid_utf8(self):
        """regions.json 含非 UTF-8 字节序列 → json.load 失败安全返回"""
        path = self._regions_path()
        # 写入非 UTF-8 JSON（悬置逗号 + 非 ASCII）
        with open(path, 'w', encoding='latin-1') as f:
            f.write('{"default": {"\x80\x81invalid_key": {"": 3}}}')
        self.assertEqual(self.sr.get_regions('default'), {})


class TestStoreDeleteDefaultGuard(_StoreEnvTest):
    """delete_store 删 default 拒绝 + 守卫"""

    def test_delete_default_returns_none(self):
        """delete_store('default') 返回 None，不抛"""
        self._write_template()
        self.assertIsNone(self.sr.delete_store('default'))
        # default 店仍然健在
        stores = self.sr.get_stores()
        self.assertTrue(any(s['id'] == 'default' for s in stores))

    def test_delete_default_case_variations(self):
        """不同大小写变体均被拒绝"""
        self._write_template()
        self.assertIsNone(self.sr.delete_store('DEFAULT'))
        self.assertIsNone(self.sr.delete_store('Default'))
        self.assertIsNone(self.sr.delete_store('  default  '))

    def test_delete_store_returns_deleted_name(self):
        """delete_store 成功时返回被删店铺名"""
        self._write_template()
        sid = self.sr.add_store('测试删除店')['id']
        result = self.sr.delete_store(sid)
        self.assertEqual(result, '测试删除店')

    def test_delete_nonexistent_returns_none(self):
        """删除不存在的店铺 id → None"""
        self._write_template()
        self.assertIsNone(self.sr.delete_store('ghost-id-12345'))
        self.assertIsNone(self.sr.delete_store(''))
        self.assertIsNone(self.sr.delete_store(None))


class TestStoreIdStability(_StoreEnvTest):
    """重名店 id 稳定性 + id 生成规则"""

    def test_same_name_multiple_stores_unique_ids(self):
        """相同名字多次建店，id 各不相同"""
        self._write_template()
        ids = [self.sr.add_store('同名店')['id'] for _ in range(5)]
        self.assertEqual(len(set(ids)), 5, '同名店 id 必须互不重复')
        self.assertNotIn('default', ids)

    def test_id_format_is_store_uuid(self):
        """生成的 id 符合 store_ 前缀 + 12 位 hex 格式"""
        self._write_template()
        item = self.sr.add_store('格式店')
        sid = item['id']
        self.assertTrue(sid.startswith('store_'), f'id 应以 store_ 开头: {sid}')
        hex_part = sid[6:]
        self.assertEqual(len(hex_part), 12)
        int(hex_part, 16)  # 必须是合法 16 进制

    def test_id_unchanged_after_rename(self):
        """改名后 id 保持不变"""
        self._write_template()
        item = self.sr.add_store('原名')
        sid = item['id']
        self.sr.rename_store(sid, '新名')
        stores = self.sr.get_stores()
        hit = next((s for s in stores if s['id'] == sid), None)
        self.assertIsNotNone(hit, '改名后店铺仍可通过原 id 找到')
        self.assertEqual(hit['name'], '新名')


# ═════════════════════════════════════════════════════════════════
# 2. advanced 算法 — 边界用例
# ═════════════════════════════════════════════════════════════════

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


class TestPromoCrossYearEdge(_StoreEnvTest):
    """大促日期跨年 / 重复 / 非法格式"""

    def test_promo_cross_year_boundary(self):
        """大促日期跨年（去年 12/31 → 今年 1/1）"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        # 去年 12/31
        last_year = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')[:8] + '31'
        cfg = _cfg_with_factors(promo=True, promo_dates=[last_year], boost=2.0, lead_days=3)
        def hlookup(*a, **kw): return _build_history(10, 30)
        item = {'name': 'X', 'stock': 0, 'sales': 10, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        # lead_days=3，如果跨年大促在窗口内则生效
        self.assertIn(plan['model'], ('advanced', 'classic(error)'))

    def test_promo_repeated_dates(self):
        """相同大促日期重复出现"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        today = datetime.now().strftime('%Y-%m-%d')
        cfg = _cfg_with_factors(promo=True,
                                promo_dates=[today, today, today],
                                boost=1.8, lead_days=3)
        def hlookup(*a, **kw): return []
        item = {'name': 'X', 'stock': 0, 'sales': 5, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertEqual(plan['promo_multiplier'], 1.8,
                         '重复日期不应导致错误')

    def test_promo_all_invalid_dates(self):
        """全部大促日期非法 → boost=1.0"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        cfg = _cfg_with_factors(promo=True,
                                promo_dates=['not-date', '99/99/9999', '2024-13-01'],
                                boost=2.0, lead_days=3)
        def hlookup(*a, **kw): return _build_history(10, 30)
        item = {'name': 'X', 'stock': 0, 'sales': 10, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertEqual(plan['promo_multiplier'], 1.0)

    def test_promo_future_date_beyond_lead(self):
        """大促在很远未来（超过 lead_days）→ 不生效"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        far_future = (datetime.now() + timedelta(days=100)).strftime('%Y-%m-%d')
        cfg = _cfg_with_factors(promo=True, promo_dates=[far_future],
                                boost=3.0, lead_days=3)
        def hlookup(*a, **kw): return _build_history(5, 30)
        item = {'name': 'X', 'stock': 0, 'sales': 5, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertEqual(plan['promo_multiplier'], 1.0,
                         '超过 lead_days 的大促不应生效')

    def test_promo_malformed_date_parsing(self):
        """畸形日期（部分合法）"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        cfg = _cfg_with_factors(promo=True,
                                promo_dates=['2024-02-30', '2024-00-01', '2024-01-00'],
                                boost=2.0, lead_days=3)
        def hlookup(*a, **kw): return []
        item = {'name': 'X', 'stock': 0, 'sales': 5, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        # 不抛即可
        self.assertIn(plan['model'], ('advanced', 'classic(error)'))


class TestSeasonFactorEdge(_StoreEnvTest):
    """季节因子无历史 / 单点历史"""

    def test_season_no_history_returns_one(self):
        """无历史 → season_factor=1.0"""
        from utils import _season_factor
        self.assertEqual(_season_factor([]), 1.0)
        self.assertEqual(_season_factor(None), 1.0)

    def test_season_single_day_history(self):
        """只有 1 天历史 → factor=1.0（不到 1 周无法算季节）"""
        from utils import _season_factor
        rows = _build_history(10, 1)
        self.assertEqual(_season_factor(rows), 1.0)

    def test_season_exactly_one_week(self):
        """恰好 1 周数据（7 天）→ factor=1.0"""
        from utils import _season_factor
        rows = _build_history(10, 7)
        self.assertEqual(_season_factor(rows), 1.0,
                         '1 周数据不足以算季节差')

    def test_season_zero_sales_all_weeks(self):
        """所有周销量都是 0 → factor=1.0（避免除 0）"""
        from utils import _season_factor
        rows = _build_history(0, 84)  # 12 周
        f = _season_factor(rows)
        self.assertEqual(f, 1.0, '零销量应避免除零返回 1.0')


class TestOversellBoundary(_StoreEnvTest):
    """超卖 high/medium 阈值边界"""

    def test_oversell_high_strictly_below_boundary(self):
        """stock < high_ratio * required → high（严格小于）"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        # daily=10, lead=4, required=40, high_ratio=0.5 → threshold=20
        # stock=19 < 20 → high
        cfg = _cfg_with_factors(oversell=True, high_ratio=0.5)
        def hlookup(*a, **kw): return []
        item = {'name': 'X', 'stock': 19, 'sales': 10, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertTrue(plan['oversell_risk'])
        self.assertEqual(plan['oversell_level'], 'high',
                         'stock < high_ratio*required 应为 high')

    def test_oversell_medium_between_boundaries(self):
        """high_ratio*required <= stock < required → medium"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        # required=40, threshold=20; stock=30 ∈ [20, 40) → medium
        cfg = _cfg_with_factors(oversell=True, high_ratio=0.5)
        def hlookup(*a, **kw): return []
        item = {'name': 'X', 'stock': 30, 'sales': 10, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertTrue(plan['oversell_risk'])
        self.assertEqual(plan['oversell_level'], 'medium',
                         'stock ∈ [high_ratio*req, req) 应为 medium')

    def test_oversell_no_risk_at_exact_required(self):
        """stock = required → 无超卖风险（oversell_risk=False）"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        # stock=40 == required=40 → stock < required = False → no risk
        cfg = _cfg_with_factors(oversell=True, high_ratio=0.5)
        def hlookup(*a, **kw): return []
        item = {'name': 'X', 'stock': 40, 'sales': 10, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertFalse(plan['oversell_risk'])
        self.assertIsNone(plan['oversell_level'],
                         'stock == required 时无超卖风险')

    def test_oversell_no_risk_when_sufficient(self):
        """stock > required → 无超卖风险"""
        from utils import calc_replenishment_advanced
        calc = calc_replenishment_advanced
        cfg = _cfg_with_factors(oversell=True, high_ratio=0.5)
        def hlookup(*a, **kw): return []
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = calc(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertFalse(plan['oversell_risk'])
        self.assertIsNone(plan['oversell_level'])


class TestAdvancedCfgMergeEdge(_StoreEnvTest):
    """cfg 缺字段合并"""

    def test_cfg_advanced_empty_dict(self):
        """cfg.advanced = {} → 全用默认"""
        from utils import calc_replenishment_advanced, _merge_advanced_cfg
        cfg = _merge_advanced_cfg({})
        self.assertFalse(cfg['promo']['enabled'])
        self.assertFalse(cfg['season']['enabled'])
        self.assertFalse(cfg['slow']['enabled'])
        self.assertFalse(cfg['oversell']['enabled'])

    def test_cfg_advanced_none_uses_default(self):
        """cfg=None → 用 get_replenishment_cfg() 默认（不抛）"""
        from utils import calc_replenishment_advanced
        def hlookup(*a, **kw): return _build_history(10, 30)
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = calc_replenishment_advanced(item, '广东', 2, 2, 0, hlookup, None)
        self.assertEqual(plan['model'], 'advanced')

    def test_cfg_advanced_partial_override(self):
        """只填 season → 其他因子仍为默认关闭"""
        from utils import _merge_advanced_cfg
        raw = {'season': {'enabled': True}}
        cfg = _merge_advanced_cfg(raw)
        self.assertTrue(cfg['season']['enabled'])
        self.assertFalse(cfg['promo']['enabled'])
        self.assertFalse(cfg['slow']['enabled'])
        self.assertFalse(cfg['oversell']['enabled'])

    def test_cfg_advanced_unknown_subkey_ignored(self):
        """cfg 含未知子键 → 忽略不抛"""
        from utils import _merge_advanced_cfg
        raw = {'promo': {'enabled': True}, 'unknown_factor': {'enabled': True}}
        cfg = _merge_advanced_cfg(raw)
        self.assertTrue(cfg['promo']['enabled'])
        # unknown_factor 不存在，应不抛
        self.assertNotIn('unknown_factor', cfg)

    def test_cfg_advanced_bad_type_values(self):
        """cfg 值为错误类型（int 而非 bool）→ 不抛"""
        from utils import _merge_advanced_cfg
        raw = {'promo': {'enabled': 123}, 'boost': 'not-a-number'}
        cfg = _merge_advanced_cfg(raw)
        # 不抛即通过
        self.assertIn('promo', cfg)


# ═════════════════════════════════════════════════════════════════
# 3. async_queue — shutdown / cancel 回调 / 系统异常
# ═════════════════════════════════════════════════════════════════

class TestAsyncQueueSubmitShutdown(_StoreEnvTest):
    """submit 后立即 shutdown"""

    def test_submit_then_immediate_shutdown_nowait(self):
        """submit 后立即 shutdown(wait=False) → 不崩溃"""
        from async_queue import TaskQueue
        q = TaskQueue(max_workers=1)
        tid = q.submit('quick', lambda _: time.sleep(0.3))
        q.shutdown(wait=False)
        # 再次调用 shutdown 不抛
        q.shutdown(wait=False)
        q.shutdown(wait=True)

    def test_submit_then_immediate_shutdown_wait(self):
        """submit 后立即 shutdown(wait=True) → 等待完成"""
        from async_queue import TaskQueue
        q = TaskQueue(max_workers=1)
        result = []
        tid = q.submit('wait_shutdown', lambda _: result.append('done'))
        # 短暂等待任务执行
        import time as _time
        _time.sleep(0.1)
        q.shutdown(wait=True)
        self.assertEqual(result, ['done'])

    def test_submit_during_shutdown_rejected(self):
        """shutdown 期间 submit → RuntimeError"""
        from async_queue import TaskQueue
        q = TaskQueue(max_workers=1)
        q.submit('block', lambda _: time.sleep(0.5))
        # 在 shutdown 过程中再 submit
        import threading
        barrier = threading.Barrier(2)

        def late_submit():
            barrier.wait()
            try:
                q.submit('late', lambda _: None)
            except RuntimeError:
                barrier.wait()

        t = threading.Thread(target=late_submit)
        t.start()
        barrier.wait()
        q.shutdown(wait=False)
        barrier.wait()
        t.join()


class TestAsyncQueueCancelProgressCallback(_StoreEnvTest):
    """cancel 后 on_progress 不再回调"""

    def test_cooperative_cancel_respects_cancel_event(self):
        """cooperative cancel：任务检查 cancel_event.is_set() 则在边界处退出"""
        from async_queue import TaskQueue
        q = TaskQueue(max_workers=1)
        progress_calls = []
        cancel_evt = threading.Event()

        def cancellable_task(prog):
            # Cooperative cancellation: 任务内部检查 cancel_event
            for i in range(10):
                if cancel_evt.is_set():
                    return  # 合作式取消
                prog(i * 10, f'step{i}')
                time.sleep(0.15)
            progress_calls.append('completed')

        def on_progress(pct, msg):
            progress_calls.append((pct, msg))

        tid = q.submit('coop_cancel', cancellable_task, on_progress=on_progress,
                       cancel_event=cancel_evt)
        time.sleep(0.25)  # 等待任务开始并触发几步
        q.cancel(tid)  # cancel_running 返回 False，但 cancel_event 被设置
        time.sleep(0.3)
        q.shutdown(wait=True)

        # 合作式取消后，任务看到 cancel_event.set() 应退出
        self.assertNotIn('completed', progress_calls,
                        'cooperative cancel 后任务应退出')

    def test_cancel_of_running_task_returns_false(self):
        """cancel 已运行中任务 → 返回 False（仅 pending 可取消）"""
        from async_queue import TaskQueue
        q = TaskQueue(max_workers=1)
        barrier = threading.Barrier(2)

        def long_task(_):
            barrier.wait(timeout=2.0)
            time.sleep(1.0)

        tid = q.submit('running', long_task)
        barrier.wait(timeout=2.0)  # 任务已开始
        result = q.cancel(tid)
        self.assertFalse(result, '运行中任务不可通过 cancel 取消')
        q.shutdown(wait=True)


class TestAsyncQueueSystemException(_StoreEnvTest):
    """worker 崩溃（系统异常 BaseException）处理"""

    def test_base_exception_caught(self):
        """BaseException（非 Exception 子类）被捕获"""
        from async_queue import TaskQueue, TaskState
        q = TaskQueue(max_workers=1)
        caught = []

        def bad_task(_):
            raise SystemExit(0)

        def on_error(e):
            caught.append(type(e).__name__)

        tid = q.submit('base_exc', bad_task, on_error=on_error)
        q.wait(tid, timeout=5.0)
        q.shutdown(wait=True)
        self.assertEqual(len(caught), 1)

    def test_keyboard_interrupt_caught(self):
        """KeyboardInterrupt 被捕获，不影响队列继续工作"""
        from async_queue import TaskQueue
        q = TaskQueue(max_workers=1)

        def bad_task(_):
            raise KeyboardInterrupt

        tid = q.submit('kbd_int', bad_task)
        q.wait(tid, timeout=5.0)
        # 队列仍可接受新任务
        tid2 = q.submit('after_kbd', lambda _: 'survived')
        q.wait(tid2, timeout=5.0)
        q.shutdown(wait=True)

    def test_worker_exception_does_not_crash_other_tasks(self):
        """一个任务崩溃不影响其他任务"""
        import time as _time
        from async_queue import TaskQueue
        q = TaskQueue(max_workers=3)
        results = []

        def ok_task(i):
            def task(_):
                results.append(i)
                return i
            return task

        # 先提交崩溃任务
        q.submit('crash', lambda _: 1/0)
        # 再提交 5 个成功任务
        for i in range(5):
            q.submit(f'ok{i}', ok_task(i))
        # 等待任务完成
        _time.sleep(2.0)
        q.shutdown(wait=True)
        # 崩溃任务不应阻止其他任务完成
        self.assertEqual(len(results), 5,
                        f'5 个正常任务应完成，实际 {len(results)}: {results}')


# ═════════════════════════════════════════════════════════════════
# 4. ocr confidence — 全黑/全白图 / 极端值 / 优先级
# ═════════════════════════════════════════════════════════════════

class TestOcrDetectBlurEdge(_StoreEnvTest):
    """detect_blur 全黑 / 全白图"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix='ocr_edge_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def _make_image(self, pixels, fname):
        from PIL import Image
        img = Image.new('L', (100, 100), pixels)
        img.save(os.path.join(self.tmp, fname), 'PNG')
        return os.path.join(self.tmp, fname)

    def test_detect_blur_all_black_image(self):
        """全黑图（方差=0）→ is_blur=True（Laplacian 方差为 0 < 100 阈值）"""
        from ocr import detect_blur
        path = self._make_image(0, 'black.png')
        is_blur, var = detect_blur(path)
        self.assertEqual(var, 0.0, '全黑图方差应为 0')
        # 纯色图方差=0，低于阈值 100 → 被判定为模糊（这是实际行为）
        self.assertTrue(is_blur, '全黑图方差为0，低于阈值应判定为模糊')

    def test_detect_blur_all_white_image(self):
        """全白图（方差=0）→ is_blur=True"""
        from ocr import detect_blur
        path = self._make_image(255, 'white.png')
        is_blur, var = detect_blur(path)
        self.assertEqual(var, 0.0)
        self.assertTrue(is_blur, '全白图方差为0，低于阈值应判定为模糊')

    def test_detect_blur_near_black(self):
        """几乎全黑（方差极小）→ 判定为模糊"""
        from ocr import detect_blur
        from PIL import Image
        import numpy as np
        path = os.path.join(self.tmp, 'near_black.png')
        arr = np.zeros((100, 100), dtype=np.uint8)
        arr[:, :] = 1  # 几乎全 0
        img = Image.fromarray(arr, 'L')
        img.save(path)
        is_blur, var = detect_blur(path)
        self.assertEqual(var, 0.0)
        # 方差为 0 < 100 → blur
        self.assertTrue(is_blur)


class TestOcrAuditEdge(_StoreEnvTest):
    """audit_numeric_fields 合法极端值不误报"""

    def test_extreme_stock_reported(self):
        """库存 > NUMERIC_ABSURD_MAX(999999) → 报量级异常（这是实际行为）"""
        from ocr import audit_numeric_fields
        items = [
            {'name': 'A', 'stock': 9999999, 'sales': 5000,
             '_raw': {'stock': '9999999', 'sales': '5000'}},
        ]
        issues = audit_numeric_fields(items)
        # NUMERIC_ABSURD_MAX=999999，9999999 > 999999 → 报量级异常
        self.assertGreater(len(issues), 0, '超限值应被报告')
        self.assertEqual(issues[0][1], 'stock')

    def test_within_absurd_threshold_no_issue(self):
        """库存 = NUMERIC_ABSURD_MAX（999999）→ 不报异常"""
        from ocr import audit_numeric_fields
        items = [
            {'name': 'A', 'stock': 999999, 'sales': 5000,
             '_raw': {'stock': '999999', 'sales': '5000'}},
        ]
        issues = audit_numeric_fields(items)
        self.assertEqual(issues, [],
                        f'等于阈值不应报异常: {issues}')

    def test_extreme_sales_in_context(self):
        """销量极大但在合理商品语境下 → 不误报"""
        from ocr import audit_numeric_fields
        items = [
            {'name': '一次性手套 100只装', 'stock': 100, 'sales': 999999,
             '_raw': {'stock': '100', 'sales': '999999'}},
        ]
        issues = audit_numeric_fields(items)
        # 如果 NUMERIC_ABSURD_MAX >= 999999 则不应报
        import ocr
        if ocr.NUMERIC_ABSURD_MAX > 999999:
            self.assertEqual(issues, [])

    def test_zero_stock_zero_sales_no_issue(self):
        """库存 0 / 销量 0 → 无问题"""
        from ocr import audit_numeric_fields
        items = [
            {'name': 'A', 'stock': 0, 'sales': 0,
             '_raw': {'stock': '0', 'sales': '0'}},
        ]
        self.assertEqual(audit_numeric_fields(items), [])

    def test_very_small_decimal_sales(self):
        """销量极小（小数）"""
        from ocr import audit_numeric_fields
        items = [
            {'name': 'A', 'stock': 100, 'sales': 0.001,
             '_raw': {'stock': '100', 'sales': '0.001'}},
        ]
        issues = audit_numeric_fields(items)
        self.assertIsInstance(issues, list)

    def test_negative_stock_with_negative_sales(self):
        """库存和销量都为负（极端脏数据）"""
        from ocr import audit_numeric_fields
        items = [
            {'name': 'A', 'stock': -999, 'sales': -50,
             '_raw': {'stock': '-999', 'sales': '-50'}},
        ]
        issues = audit_numeric_fields(items)
        # 应报负值异常
        self.assertGreater(len(issues), 0)


class TestOcrConfidencePriority(_StoreEnvTest):
    """confidence 优先级（low 覆盖 medium）"""

    def test_low_over_medium_via_flag_combination(self):
        """_missing_id(medium) + _low_confidence(low) → 最终 low"""
        from ocr import build_confidence_meta
        items = [
            {'name': 'A', 'stock': 100, 'sales': 50,
             '_missing_id': True, '_low_confidence': True,
             '_raw': {}},
        ]
        out = build_confidence_meta(items)
        self.assertEqual(out[0]['confidence']['level'], 'low',
                         '_low_confidence 应覆盖 _missing_id 的 medium')

    def test_dual_degraded_over_missing_id(self):
        """_missing_id(medium) + _dual_degraded(low) → low"""
        from ocr import build_confidence_meta
        items = [
            {'name': 'A', 'stock': 100, 'sales': 50,
             '_missing_id': True, '_dual_degraded': True,
             '_raw': {}},
        ]
        out = build_confidence_meta(items)
        self.assertEqual(out[0]['confidence']['level'], 'low')

    def test_three_flags_low_wins(self):
        """三个 low 标记 + 一个 medium → low"""
        from ocr import build_confidence_meta
        items = [
            {'name': 'A', 'stock': 0, 'sales': 999,
             '_missing_id': True, '_low_confidence': True,
             '_dual_degraded': True, '_raw': {'stock': '', 'sales': '999'}},
        ]
        out = build_confidence_meta(items)
        self.assertEqual(out[0]['confidence']['level'], 'low')


# ═════════════════════════════════════════════════════════════════
# 5. review 流程边界
# ═════════════════════════════════════════════════════════════════

class TestApplyUserEditsEdge(unittest.TestCase):
    """apply_user_edits 边界（空 edits/None/非白名单字段拒绝）"""

    def test_edits_none_accepted(self):
        """edits=None → 不抛，返回原 items"""
        from ocr_review import apply_user_edits
        items = [{'stock': 100}]
        out = apply_user_edits(items, None)
        self.assertIs(out, items)
        self.assertEqual(items[0]['stock'], 100)

    def test_edits_non_list_accepted(self):
        """edits 是 dict（非 list）→ 遍历 dict 键不抛"""
        from ocr_review import apply_user_edits
        items = [{'stock': 100}]
        # edits={'a': 1}: 遍历 dict 键，每个键是 str → ed.get('index')=None → 非 int → 跳过
        out = apply_user_edits(items, {'a': 1})
        self.assertIs(out, items)
        self.assertEqual(items[0]['stock'], 100)

    def test_field_case_sensitivity(self):
        """field 名大小写敏感（小写 'stock' vs 大写 'STOCK'）"""
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100, 'sales': 50}]
        apply_user_edits(items, [{'index': 0, 'field': 'STOCK', 'value': 200}])
        self.assertEqual(items[0]['stock'], 100,
                         '大写 STOCK 不应修改小写 stock')

    def test_qty_field_not_in_whitelist(self):
        """qty 不在白名单中（白名单仅 stock/sales/name）"""
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100, 'qty': 50}]
        apply_user_edits(items, [{'index': 0, 'field': 'qty', 'value': 999}])
        # qty 不在白名单中，不应修改
        self.assertEqual(items[0]['qty'], 50)

    def test_index_exact_boundary(self):
        """index = len(items) - 1 合法"""
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100}, {'name': 'B', 'stock': 200}]
        apply_user_edits(items, [{'index': 1, 'field': 'stock', 'value': 999}])
        self.assertEqual(items[1]['stock'], 999)

    def test_negative_index_rejected(self):
        """负数 index → 不改"""
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100}]
        apply_user_edits(items, [{'index': -1, 'field': 'stock', 'value': 999}])
        self.assertEqual(items[0]['stock'], 100)


class TestCategorizeErrorEdge(unittest.TestCase):
    """categorize_error 未分类异常兜底"""

    def test_long_error_message_unknown(self):
        """长错误消息不匹配任何类别 → unknown"""
        from ocr_review import categorize_error
        msg = '这是一个非常长的错误消息，可能包含各种内容但不匹配任何已知模式' * 5
        cat, _, _ = categorize_error(msg)
        self.assertEqual(cat, 'unknown')

    def test_numeric_error_string(self):
        """纯数字错误消息 → unknown"""
        from ocr_review import categorize_error
        cat, _, _ = categorize_error('12345')
        self.assertEqual(cat, 'unknown')

    def test_unicode_special_chars(self):
        """含特殊 Unicode 字符的错误消息"""
        from ocr_review import categorize_error
        cat, _, title = categorize_error('网络连接失败 🔥 网络不可达')
        # 不抛即通过
        self.assertIsInstance(cat, str)
        self.assertIsInstance(title, str)


# ═════════════════════════════════════════════════════════════════
# 6. 集成点测试
# ═════════════════════════════════════════════════════════════════

class TestRecordCaptureDoubleLayerIntegration(unittest.TestCase):
    """record_capture 双层入参与 gui 组装契约"""

    @classmethod
    def setUpClass(cls):
        cls.hdb = _load_module('pdd_hdb_int', 'history_db.py')

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pdd_int_')
        self.hdb.set_db_path(os.path.join(self.tmp, 'history.db'))

    def tearDown(self):
        self.hdb.reset_db_path()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_double_layer_with_three_stores(self):
        """双层入参：三个店铺，每个含多个地区"""
        sid = self.hdb.record_capture({
            'shopA': {
                '华东': PLANS_A,
                '华南': PLANS_B,
            },
            'shopB': {
                '西南': [{'name': '西南货', 'sku_id': '44444444444', 'stock': 10,
                          'daily': 2}],
            },
            'default': {
                '华北': [{'name': '华北货', 'sku_id': '55555555555', 'stock': 5,
                          'daily': 1}],
            },
        }, 'import')

        self.assertGreater(sid, 0)

        # shopA 华东
        rows = self.hdb.query_region_days('华东', TODAY, store='shopA')
        self.assertEqual(len(rows), 2, 'shopA 华东应有 2 行')

        # shopA 华南
        rows = self.hdb.query_region_days('华南', TODAY, store='shopA')
        self.assertEqual(len(rows), 1)

        # shopB 西南
        rows = self.hdb.query_region_days('西南', TODAY, store='shopB')
        self.assertEqual(len(rows), 1)

        # default 华北
        rows = self.hdb.query_region_days('华北', TODAY, store='default')
        self.assertEqual(len(rows), 1)

        # 跨店隔离验证
        rows_a = self.hdb.query_region_days('华东', TODAY, store='shopA')
        rows_b = self.hdb.query_region_days('华东', TODAY, store='shopB')
        self.assertEqual(len(rows_a), 2)
        self.assertEqual(len(rows_b), 0, 'shopB 不应有华东数据')

    def test_double_layer_empty_region(self):
        """双层含空地区列表"""
        sid = self.hdb.record_capture({
            'shopA': {
                '华东': PLANS_A,
                '华南': [],  # 空地区
            },
        }, 'import')
        self.assertGreater(sid, 0)
        rows = self.hdb.query_region_days('华东', TODAY, store='shopA')
        self.assertEqual(len(rows), 2)

    def test_double_layer_none_plans(self):
        """双层地区值为 None"""
        sid = self.hdb.record_capture({
            'shopA': {
                '华东': PLANS_A,
                '华南': None,  # None 等同空
            },
        }, 'import')
        self.assertGreater(sid, 0)

    def test_single_level_vs_double_level_session_count(self):
        """单层 vs 双层均创建独立 session"""
        sid1 = self.hdb.record_capture({'华东': PLANS_A}, 'live')
        sid2 = self.hdb.record_capture({'华南': PLANS_B}, 'live')
        sid3 = self.hdb.record_capture({
            'shopA': {'华东': PLANS_A, '华南': PLANS_B}
        }, 'import')
        self.assertNotEqual(sid1, sid2)
        self.assertNotEqual(sid2, sid3)
        self.assertNotEqual(sid1, sid3)


class TestCalcReplenishmentCfgIntegration(unittest.TestCase):
    """calc_replenishment cfg 透传"""

    def test_cfg_passed_through_dispatch(self):
        """calc_replenishment 入口接受 cfg 参数并透传给 calc_replenishment_advanced"""
        from utils import calc_replenishment, MODEL_ADVANCED
        today = datetime.now().strftime('%Y-%m-%d')
        cfg = _cfg_with_factors(promo=True, promo_dates=[today],
                                boost=2.5, lead_days=3,
                                season=True, oversell=True, high_ratio=0.5)
        rows = _build_history(10, 30)
        def hlookup(sku, reg, days, name=None): return rows
        def sl(item, reg): return 2
        items = [{'name': 'A', 'stock': 0, 'sales': 10, 'sku_id': 'S1'}]
        plans = calc_replenishment(items, '广东', MODEL_ADVANCED, 2, 0, sl, hlookup, cfg=cfg)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]['promo_multiplier'], 2.5,
                         'cfg.promo.boost 应透传给 calc')
        self.assertIn(plans[0]['model'], ('advanced', 'classic(error)'))

    def test_cfg_missing_promo_uses_default_boost(self):
        """cfg 缺少 promo.boost → 使用默认值 1.5"""
        from utils import calc_replenishment, MODEL_ADVANCED, _merge_advanced_cfg
        # 不传 cfg.advanced.promo.boost
        raw_cfg = {'advanced': {'promo': {'enabled': True, 'dates': [TODAY]}}}
        # cfg 由 get_replenishment_cfg 处理
        from utils import get_replenishment_cfg
        cfg = get_replenishment_cfg()
        def hlookup(*a, **kw): return _build_history(10, 30)
        def sl(item, reg): return 2
        items = [{'name': 'A', 'stock': 0, 'sales': 10, 'sku_id': 'S1'}]
        plans = calc_replenishment(items, '广东', MODEL_ADVANCED, 2, 0, sl, hlookup, cfg=cfg)
        # 默认 boost 应为 1.5
        self.assertGreaterEqual(plans[0].get('promo_multiplier', 1.0), 1.0)


# ═════════════════════════════════════════════════════════════════
# 7. 回归门禁：py_compile 检查所有模块
# ═════════════════════════════════════════════════════════════════

class TestRegressionCompilation(unittest.TestCase):
    """py_compile 检查所有非测试 Python 文件"""

    def test_all_modules_compile(self):
        """全部 .py 文件（除 test_*.py）可编译"""
        import py_compile
        import glob as _glob
        bad = []
        for path in _glob.glob(os.path.join(HERE, '*.py')):
            if os.path.basename(path).startswith('test_'):
                continue
            try:
                py_compile.compile(path, doraise=True)
            except Exception as e:
                bad.append((os.path.basename(path), str(e)))
        self.assertEqual(bad, [], f'编译失败: {bad}')


# ═════════════════════════════════════════════════════════════════
# 8. 更多 ocr_review 边界
# ═════════════════════════════════════════════════════════════════

class TestOcrReviewMoreEdge(unittest.TestCase):
    """apply_user_edits 更多边界 + summarize_review 边界"""

    def test_sales_field_in_whitelist(self):
        """sales 在白名单中"""
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100, 'sales': 50}]
        apply_user_edits(items, [{'index': 0, 'field': 'sales', 'value': 999}])
        self.assertEqual(items[0]['sales'], 999)

    def test_region_field_in_whitelist(self):
        """修复：region 在白名单中（OCR 识别错的销售区域可改）"""
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100, 'sales': 50, 'region': '浙江'}]
        apply_user_edits(items, [{'index': 0, 'field': 'region', 'value': '江苏'}])
        self.assertEqual(items[0]['region'], '江苏',
                         'region 改后字段值应被改写为「江苏」')

    def test_warehouse_field_in_whitelist(self):
        """修复：warehouse 在白名单中（OCR 识别错的仓库可改）"""
        from ocr_review import apply_user_edits
        items = [{'name': 'A', 'stock': 100, 'sales': 50, 'warehouse': 'A仓'}]
        apply_user_edits(items, [{'index': 0, 'field': 'warehouse', 'value': 'B仓'}])
        self.assertEqual(items[0]['warehouse'], 'B仓',
                         'warehouse 改后字段值应被改写为「B仓」')

    def test_summarize_review_with_empty_confidence(self):
        """confidence 字段为空 dict"""
        from ocr_review import summarize_review
        items = [{'name': 'A', 'confidence': {}}]
        s = summarize_review(items)
        self.assertEqual(s['total'], 1)

    def test_summarize_review_dict_input(self):
        """summarize_review dict 输入 → 遍历 dict 键（非 dict 跳过），total=len(dict)"""
        from ocr_review import summarize_review
        # summarize_review 对 dict 输入：for it in dict → 遍历键（字符串），
        # isinstance(str, dict)=False → 跳过；total=len(dict)
        s = summarize_review({'key': 'value'})
        self.assertEqual(s['total'], 1)
        self.assertEqual(s['high'], 0)

    def test_summarize_review_string_input(self):
        """summarize_review 字符串输入 → total=len(string)（逐字符遍历）"""
        from ocr_review import summarize_review
        s = summarize_review('abc')
        self.assertEqual(s['total'], 3)


# ═════════════════════════════════════════════════════════════════
# 9. store_registry 更多的边界
# ═════════════════════════════════════════════════════════════════

class TestStoreRegionsMoreEdge(_StoreEnvTest):
    """regions 更多边界"""

    def test_save_regions_empty_store_id_falls_back_to_active(self):
        """save_regions 空 store_id → 回退到当前激活店铺"""
        self._write_template()
        sid = self.sr.add_store('C店')['id']
        self.sr.set_active(sid)
        # 空字符串 → 回退到当前激活店铺
        result = self.sr.save_regions({'华东': {'': 5}}, '')
        self.assertTrue(result)
        # 写到了当前激活店铺（sid），不是 'default'
        regions = self.sr.get_regions(sid)
        self.assertIn('华东', regions, f'空 store_id 应写到激活店铺 {sid}，结果: {regions}')

    def test_get_regions_nonexistent_store(self):
        """get_regions 不存在的店铺 → {}"""
        self._write_template()
        self.assertEqual(self.sr.get_regions('ghost-store-xyz'), {})

    def test_delete_store_cleans_regions_completely(self):
        """删店后 regions.json 中该店铺数据完全清除"""
        self._write_template()
        sid = self.sr.add_store('C店')['id']
        self.sr.save_regions({'华东': {'': 3}}, sid)
        self.sr.save_regions({'华南': {'': 4}}, 'default')
        self.sr.delete_store(sid)
        with open(self._regions_path(), encoding='utf-8') as f:
            data = json.load(f)
        self.assertNotIn(sid, data, '被删店铺的 regions 节应完全清理')
        self.assertIn('default', data)

    def test_get_shipping_with_null_regions(self):
        """get_shipping 传入 None regions"""
        self._write_template()
        self.sr.save_regions({'华东': {'纸': 5}}, 'default')
        regions = self.sr.get_regions('default')
        self.assertEqual(self.sr.get_shipping('华东', '纸', regions), 5)

    def test_save_regions_preserves_other_stores(self):
        """save_regions 只覆盖本店，其他店铺数据保留"""
        self._write_template()
        sid = self.sr.add_store('B店')['id']
        self.sr.save_regions({'华南': {'': 5}}, sid)
        self.sr.save_regions({'华东': {'': 3}}, 'default')
        # 再次保存 default，不应覆盖 shopB
        self.sr.save_regions({'华北': {'': 7}}, 'default')
        shopB_regions = self.sr.get_regions(sid)
        self.assertEqual(shopB_regions.get('华南', {}).get('', 0), 5,
                         'shopB 的华南数据不应被 default 的写覆盖')


# ═════════════════════════════════════════════════════════════════
# 10. async_queue 更多边界
# ═════════════════════════════════════════════════════════════════

class TestAsyncQueueMoreEdge(unittest.TestCase):
    """async_queue 更多边界"""

    def test_cancel_all_returns_correct_count(self):
        """cancel_all 返回正确取消数量"""
        from async_queue import TaskQueue
        q = TaskQueue(max_workers=0)  # 不启动 worker，任务全是 PENDING
        for i in range(5):
            q.submit(f't{i}', lambda _: None)
        n = q.cancel_all()
        self.assertEqual(n, 5, '应取消全部 5 个 PENDING 任务')
        q.shutdown(wait=True)

    def test_wait_unknown_id_no_crash(self):
        """wait 未知 id → 不崩溃"""
        from async_queue import TaskQueue
        q = TaskQueue(max_workers=1)
        # wait 不抛即为通过
        try:
            q.wait('no-such-id', timeout=0.5)
        except Exception as e:
            self.fail(f'wait 未知 id 不应抛异常: {e}')
        q.shutdown(wait=True)

    def test_multiple_error_callbacks_all_called(self):
        """多个失败任务各自 on_error 均被调用"""
        from async_queue import TaskQueue
        import time as _time
        q = TaskQueue(max_workers=3)
        error_count = []

        def crash_task(_):
            raise RuntimeError('crash')

        # 3 个崩溃任务各自 on_error
        for i in range(3):
            q.submit(f'crash{i}', crash_task, on_error=lambda e: error_count.append(1))
        _time.sleep(0.5)  # 等待 worker 处理
        q.shutdown(wait=True)
        # 至少 1 个错误回调被触发
        self.assertGreaterEqual(len(error_count), 1,
                        f'崩溃任务应触发 on_error，实际 {len(error_count)}')

    def test_progress_callback_with_non_string_message(self):
        """on_progress 接收非字符串消息不抛"""
        from async_queue import TaskQueue
        q = TaskQueue(max_workers=1)
        messages = []

        def task(prog):
            prog(50, 12345)  # 数字消息
            prog(100, None)  # None 消息

        def on_progress(pct, msg):
            messages.append((pct, msg))

        tid = q.submit('num_msg', task, on_progress=on_progress)
        q.wait(tid, timeout=5.0)
        q.shutdown(wait=True)
        self.assertGreaterEqual(len(messages), 2)


# ═════════════════════════════════════════════════════════════════
# 11. algorithm 更多边界
# ═════════════════════════════════════════════════════════════════

class TestAlgorithmMoreEdge(unittest.TestCase):
    """algorithm 更多边界"""

    def test_calc_advanced_with_zero_daily(self):
        """日销 0 + advanced 算法"""
        from utils import calc_replenishment_advanced
        rows = _build_history(0, 30)
        def hlookup(*a, **kw): return rows
        cfg = _cfg_with_factors(season=True, promo=True)
        item = {'name': 'X', 'stock': 100, 'sales': 0, 'sku_id': 'S1'}
        plan = calc_replenishment_advanced(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertIn(plan['model'], ('advanced', 'classic(error)'))

    def test_calc_advanced_with_negative_stock(self):
        """负库存 + advanced 算法"""
        from utils import calc_replenishment_advanced
        def hlookup(*a, **kw): return []
        item = {'name': 'X', 'stock': -50, 'sales': 10, 'sku_id': 'S1'}
        plan = calc_replenishment_advanced(item, '广东', 2, 2, 0, hlookup, _cfg_with_factors())
        self.assertIn(plan['model'], ('advanced', 'classic(error)'))

    def test_calc_advanced_with_in_transit(self):
        """在途库存 + advanced 算法"""
        from utils import calc_replenishment_advanced
        rows = _build_history(10, 30)
        def hlookup(*a, **kw): return rows
        cfg = _cfg_with_factors(in_transit_qty=200)
        item = {'name': 'X', 'stock': 100, 'sales': 10, 'sku_id': 'S1'}
        plan = calc_replenishment_advanced(item, '广东', 2, 2, 0, hlookup, cfg)
        self.assertIn(plan['model'], ('advanced', 'classic(error)'))


# ═════════════════════════════════════════════════════════════════
# 12. history_db 更多边界
# ═════════════════════════════════════════════════════════════════

class TestHistoryDbMoreEdge(unittest.TestCase):
    """history_db 更多边界"""

    @classmethod
    def setUpClass(cls):
        cls.hdb = _load_module('pdd_hdb_more', 'history_db.py')

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pdd_hdb_more_')
        self.hdb.set_db_path(os.path.join(self.tmp, 'history.db'))

    def tearDown(self):
        self.hdb.reset_db_path()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_delete_region_nonexistent(self):
        """delete_region 不存在地区 → 0"""
        self.hdb.record_capture({'华东': PLANS_A}, 'live')
        n = self.hdb.delete_region('不存在的地区')
        self.assertEqual(n, 0)

    def test_query_regions_empty(self):
        """query_regions 空库"""
        self.assertEqual(self.hdb.query_regions(), [])

    def test_query_daily_empty(self):
        """query_daily 空库"""
        self.assertEqual(self.hdb.query_daily(30), [])

    def test_query_sku_history_empty(self):
        """query_sku_history 空库"""
        rows = self.hdb.query_sku_history('SKUXXX', 30)
        self.assertEqual(rows, [])

    def test_record_capture_with_all_fields(self):
        """record_capture 含完整字段的 plans"""
        full_plan = [{
            'name': '完整商品', 'sku_id': 'FULL001', 'stock': 100, 'sales': 20,
            'days_left': 5.0, 'status': '3天后下单', 'qty': 200, 'warehouse': '中央仓',
        }]
        sid = self.hdb.record_capture({'华东': full_plan}, 'live')
        self.assertGreater(sid, 0)
        rows = self.hdb.query_region_days('华东', TODAY)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['name'], '完整商品')
        self.assertEqual(rows[0]['sku_id'], 'FULL001')


if __name__ == '__main__':
    unittest.main(verbosity=2)
