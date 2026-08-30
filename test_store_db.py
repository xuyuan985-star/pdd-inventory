"""t1 多店铺隔离-后端回归（store_registry.py + history_db.py 店铺维度）

覆盖：
1. 店铺清单 CRUD（首启自愈建「默认店铺」/ 改名 id 稳定 / active 回落 / 删店守卫）；
2. regions.json 旧格式迁移（旧顶层 → default 店铺，{region: int} → {"": int}）
   与按店铺隔离读写；
3. history_rows 老库 ALTER 迁移（列存在性检查，绝不丢老数据；旧行 store='' ≡ default）；
4. record_capture 双层/单层入参兼容；全部查询/删除的 store 过滤与店铺隔离；
5. 失败路径不抛（R8 铁律保持）。

测试隔离：utils / store_registry / history_db 均按 test_smoke 同款 importlib
加载独立副本；store_registry 经 sr.utils = u 整体替换 utils 副本，Config 与
get_base_dir 全部落到 tmp 目录，绝不触碰真实用户配置。
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import shutil
import unittest
from datetime import datetime

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


# 与 gui._calc_from_items 产物 plans 同形状（日销量键 = daily）
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

# 旧版（无 store 列）schema，用于老库 ALTER 迁移测试
OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS capture_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    region      TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL,
    item_count  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS history_rows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES capture_sessions(id),
    captured_at TEXT NOT NULL,
    region      TEXT NOT NULL,
    sku_id      TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL,
    stock       INTEGER NOT NULL DEFAULT 0,
    sales       INTEGER NOT NULL DEFAULT 0,
    days_left   REAL,
    status      TEXT NOT NULL DEFAULT '',
    qty         INTEGER NOT NULL DEFAULT 0,
    warehouse   TEXT NOT NULL DEFAULT ''
);
"""


class _StoreEnvTest(unittest.TestCase):
    """公共基类：tmp 目录 + 独立 utils/store_registry 副本。"""

    @classmethod
    def setUpClass(cls):
        cls.u = _load_module('pdd_utils_store_env', 'utils.py')
        cls.sr = _load_module('pdd_store_registry_env', 'store_registry.py')
        cls.sr.utils = cls.u  # 晚绑定替换：Config/get_base_dir 全走副本

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pdd_store_')
        self.u.get_base_dir = lambda: self.tmp
        self._reset_config_cache()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reset_config_cache(self):
        self.u.Config._load_cache = {'mtime': -1, 'data': None}
        self.u.Config._template_cache = None

    def _write_template(self, stores=None):
        """tmp 内放最小 settings_template.json（含/不含 stores 节点均可）。"""
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

    def _settings_path(self):
        return os.path.join(self.tmp, 'settings.json')

    def _read_settings(self):
        with open(self._settings_path(), encoding='utf-8') as f:
            return json.load(f)

    def _write_settings_raw(self, data):
        with open(self._settings_path(), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self._reset_config_cache()

    def _regions_path(self):
        return os.path.join(self.tmp, 'regions.json')


class TestStoreRegistry(_StoreEnvTest):
    """店铺清单：首启自愈 / CRUD / active 守卫 / id 稳定"""

    def test_first_boot_creates_default_store(self):
        """首启：无 stores 节点 → 自动建「默认店铺」id=default 并写回 settings.json。"""
        self._write_template()  # 模板里没有 stores 节点（模拟老用户升级）
        self.assertFalse(os.path.exists(self._settings_path()), '前置：settings.json 不存在')
        stores = self.sr.get_stores()
        self.assertEqual(stores, [{'id': 'default', 'name': '默认店铺'}])
        self.assertEqual(self.sr.get_active(), 'default')
        on_disk = self._read_settings()
        self.assertEqual(on_disk['stores']['active'], 'default', '自愈结果应写回配置')
        self.assertEqual([s['id'] for s in on_disk['stores']['list']], ['default'])

    def test_add_rename_set_active_delete_roundtrip(self):
        """增→改→切换→删 全链路；改名后 id 稳定；删店返回店铺名。"""
        self._write_template()
        item = self.sr.add_store('二号仓店')
        self.assertIsInstance(item, dict)
        self.assertTrue(item['id'] and item['id'] != 'default', '新店 id 非空且不是 default')
        self.assertEqual(item['name'], '二号仓店')
        self.assertEqual(len(self.sr.get_stores()), 2)

        sid = item['id']
        self.assertTrue(self.sr.rename_store(sid, '旗舰二店'))
        self.assertEqual(self.sr.get_store_name(sid), '旗舰二店')
        self.assertTrue(any(s['id'] == sid for s in self.sr.get_stores()),
                        '改名后 id 必须不变（name 变 id 稳）')

        self.assertTrue(self.sr.set_active(sid))
        self.assertEqual(self.sr.get_active(), sid)

        deleted = self.sr.delete_store(sid)
        self.assertEqual(deleted, '旗舰二店', 'delete_store 必须返回被删店铺名供 GUI 联动')
        self.assertEqual(len(self.sr.get_stores()), 1)
        self.assertEqual(self.sr.get_active(), 'default', '删激活店后 active 回落 default')

    def test_delete_and_add_guards(self):
        """守卫：default 禁删；空/不存在 id 拒绝；空名拒绝——全部不抛。"""
        self._write_template()
        self.assertIsNone(self.sr.delete_store('default'), '默认店铺不可删除')
        self.assertIsNone(self.sr.delete_store(''))
        self.assertIsNone(self.sr.delete_store(None))
        self.assertIsNone(self.sr.delete_store('no-such-id'))
        self.assertIsNone(self.sr.add_store(''))
        self.assertIsNone(self.sr.add_store('   '))
        self.assertIsNone(self.sr.add_store(None))
        self.assertFalse(self.sr.rename_store('no-such-id', 'x'))
        self.assertFalse(self.sr.rename_store('default', ''))
        self.assertFalse(self.sr.set_active('no-such-id'))
        self.assertFalse(self.sr.set_active(''))
        # 守卫全触发后 default 依然健在
        self.assertEqual([s['id'] for s in self.sr.get_stores()], ['default'])

    def test_active_falls_back_and_self_heals(self):
        """active 指向已不存在的店铺（手工损坏）→ get_active 回落 default 并自愈写回。"""
        self._write_template()
        sid = self.sr.add_store('B店')['id']
        self.assertTrue(self.sr.set_active(sid))
        data = self._read_settings()
        data['stores']['active'] = 'ghost-id'
        self._write_settings_raw(data)

        self.assertEqual(self.sr.get_active(), 'default')
        on_disk = self._read_settings()
        self.assertEqual(on_disk['stores']['active'], 'default', '失效 active 应自愈写回')
        self.assertTrue(any(s['id'] == sid for s in on_disk['stores']['list']),
                        '自愈不得误删其他店铺')

    def test_stores_survive_corrupt_node(self):
        """stores 节点损坏（list 非 list / 条目非 dict / 空 id）→ 丢弃脏数据保留 default。"""
        self._write_template()
        bad = {'stores': {'active': 'x', 'list': ['垃圾', 42, {'id': '', 'name': '空id'}]}}
        self._write_settings_raw(bad)
        stores = self.sr.get_stores()
        self.assertEqual([s['id'] for s in stores], ['default'])

    def test_get_stores_returns_copies(self):
        """返回值是副本：改返回的 list/dict 不影响内部状态与落盘配置。"""
        self._write_template()
        stores = self.sr.get_stores()
        stores.append({'id': 'hack', 'name': '黑客店'})
        stores[0]['name'] = '篡改'
        again = self.sr.get_stores()
        self.assertEqual([s['id'] for s in again], ['default'])
        self.assertEqual(again[0]['name'], '默认店铺')

    def test_store_ids_unique(self):
        """连续建店 id 不重复。"""
        self._write_template()
        ids = {self.sr.add_store(f'店{i}')['id'] for i in range(5)}
        self.assertEqual(len(ids), 5)
        self.assertNotIn('default', ids)


class TestStoreRegions(_StoreEnvTest):
    """regions.json 按店铺隔离 + 旧格式迁移"""

    def test_legacy_regions_migrate_into_default(self):
        """旧顶层格式 → 全部并入 default 店铺；{region: int} → {region: {"": int}}。"""
        with open(self._regions_path(), 'w', encoding='utf-8') as f:
            json.dump({'华东': 3, '华南': {'纸品': 5}, '脏值区': '不是数字'}, f,
                      ensure_ascii=False)
        got = self.sr.get_regions('default')
        self.assertEqual(got, {'华东': {'': 3}, '华南': {'纸品': 5}, '脏值区': {}})
        self.assertEqual(self.sr.get_regions('shopB'), {},
                         '其他店铺不受旧数据影响（迁移只进 default）')

    def test_save_regions_per_store_isolated(self):
        """save_regions 只覆盖本店节；其他店铺数据保留；落盘即新格式且幂等。"""
        with open(self._regions_path(), 'w', encoding='utf-8') as f:
            json.dump({'华东': 3}, f, ensure_ascii=False)
        self.assertTrue(self.sr.save_regions({'华南': {'纸': 2}}, 'shopB'))

        self.assertEqual(self.sr.get_regions('shopB'), {'华南': {'纸': 2}})
        self.assertEqual(self.sr.get_regions('default'), {'华东': {'': 3}},
                         '旧数据迁移进 default 且不被 shopB 保存覆盖')

        with open(self._regions_path(), encoding='utf-8') as f:
            on_disk = json.load(f)
        self.assertEqual(set(on_disk.keys()), {'default', 'shopB'}, '落盘应为按店铺新格式')
        self.assertEqual(on_disk['default'], {'华东': {'': 3}})
        # 新格式再读幂等（不会被误判成旧格式二次合并）
        self.assertEqual(self.sr.get_regions('shopB'), {'华南': {'纸': 2}})

    def test_save_regions_defaults_to_active_store(self):
        """store_id 缺省 → 写入当前激活店铺（GUI 切店后保存落对位置）。"""
        self._write_template()
        sid = self.sr.add_store('B店')['id']
        self.assertTrue(self.sr.set_active(sid))
        self.assertTrue(self.sr.save_regions({'华东': {'': 1}}))
        self.assertEqual(self.sr.get_regions(sid), {'华东': {'': 1}})
        self.assertEqual(self.sr.get_regions('default'), {})

    def test_get_shipping_semantics(self):
        """get_shipping：product 优先 → "" 地区默认 → 全局 3；脏数据不抛。"""
        regions = {'华东': {'纸': 5, '': 2}, '脏值': 9}
        self.assertEqual(self.sr.get_shipping('华东', '纸', regions), 5)
        self.assertEqual(self.sr.get_shipping('华东', '没配的商品', regions), 2)
        self.assertEqual(self.sr.get_shipping('未知地区', 'x', regions), 3)
        self.assertEqual(self.sr.get_shipping('脏值', 'x', regions), 3,
                         'int 地区数据（旧格式未归一）按全局默认处理')
        self.assertEqual(self.sr.get_shipping('华东', '纸'), 3, '不传 regions 时查当前店铺（空）')

    def test_corrupt_regions_file_returns_empty(self):
        """regions.json 损坏 → 读失败安全返回 {}，不外抛。"""
        with open(self._regions_path(), 'w', encoding='utf-8') as f:
            f.write('not json {{{')
        self.assertEqual(self.sr.get_regions('default'), {})
        # 损坏文件上保存 → 自动重建为按店铺新格式
        self.assertTrue(self.sr.save_regions({'华东': {'': 4}}, 'default'))
        self.assertEqual(self.sr.get_regions('default'), {'华东': {'': 4}})

    def test_delete_store_cleans_regions_node(self):
        """删店联动：regions.json 里该店铺节被清理，default 保留。"""
        self._write_template()
        sid = self.sr.add_store('B店')['id']
        self.assertTrue(self.sr.save_regions({'华东': {'': 1}}, sid))
        self.assertTrue(self.sr.save_regions({'华东': {'': 2}}, 'default'))
        self.assertEqual(self.sr.delete_store(sid), 'B店')
        with open(self._regions_path(), encoding='utf-8') as f:
            on_disk = json.load(f)
        self.assertNotIn(sid, on_disk, '被删店铺的 regions 节应联动清理')
        self.assertIn('default', on_disk)


class TestHistoryStoreSchema(unittest.TestCase):
    """history_db 店铺列：新库建列 / 老库 ALTER 迁移不丢数据"""

    @classmethod
    def setUpClass(cls):
        cls.hdb = _load_module('pdd_history_db_store_schema', 'history_db.py')

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pdd_hdb_schema_')
        self.hdb.set_db_path(os.path.join(self.tmp, 'history.db'))

    def tearDown(self):
        self.hdb.reset_db_path()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _raw(self, sql, params=()):
        conn = sqlite3.connect(self.hdb.db_path())
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _cols(self):
        return {r[1] for r in self._raw('PRAGMA table_info(history_rows)')}

    def test_fresh_db_has_store_column_and_index(self):
        """新库：建表即含 store 列 + idx_rows_store 索引。"""
        self.assertGreater(self.hdb.record_capture({'华东': PLANS_A}, 'live'), 0)
        self.assertIn('store', self._cols())
        idx = self._raw("SELECT name FROM sqlite_master WHERE type='index'"
                        " AND name='idx_rows_store'")
        self.assertEqual(len(idx), 1, 'store 索引应存在')
        rows = self.hdb.query_region_days('华东', TODAY)
        self.assertEqual(rows[0]['store'], 'default')

    def test_old_db_alter_migration_keeps_rows(self):
        """老库（无 store 列）→ ALTER 补列成功、老数据零丢失、旧行归 default 店铺。"""
        old_path = os.path.join(self.tmp, 'old.db')
        conn = sqlite3.connect(old_path)
        try:
            conn.executescript(OLD_SCHEMA)
            conn.execute("INSERT INTO capture_sessions (ts, region, source, item_count)"
                         " VALUES (?, '华东', 'live', 2)", (TODAY + ' 09:00:00',))
            conn.execute("INSERT INTO history_rows (session_id, captured_at, region, sku_id,"
                         " name, stock, sales, days_left, status, qty, warehouse)"
                         " VALUES (1, ?, '华东', '11111111111', '老商品A', 10, 2, 5.0,"
                         " '3天后下单', 0, '旧仓')", (TODAY + ' 09:00:00',))
            conn.execute("INSERT INTO history_rows (session_id, captured_at, region, sku_id,"
                         " name, stock, sales, days_left, status, qty, warehouse)"
                         " VALUES (1, ?, '华东', '22222222222', '老商品B', 20, 3, 6.6,"
                         " '3天后下单', 0, '旧仓')", (TODAY + ' 09:00:00',))
            conn.commit()
        finally:
            conn.close()

        self.hdb.set_db_path(old_path)  # 指向老库，首次操作触发迁移
        daily = self.hdb.query_daily(30)
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0]['items'], 2, '迁移后老数据必须完整可查')
        self.assertEqual(daily[0]['stock_total'], 30)

        self.assertIn('store', self._cols(), '首次操作应完成 ALTER 补列')
        idx = self._raw("SELECT name FROM sqlite_master WHERE type='index'"
                        " AND name='idx_rows_store'")
        self.assertEqual(len(idx), 1, '补列后 store 索引应补建')

        # 旧行 store='' 出口归一化 default，且被 default 店铺查询命中
        rows = self.hdb.query_region_days('华东', TODAY, store='default')
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['store'] == 'default' for r in rows))
        self.assertEqual(self.hdb.query_region_days('华东', TODAY, store='other'), [],
                         '旧行不得泄漏到其他店铺')
        # 迁移后新库可正常写入新店铺数据
        self.assertGreater(self.hdb.record_capture({'华南': PLANS_B}, 'batch'), 0)

    def test_migration_idempotent_second_init(self):
        """迁移幂等：set_db_path 同路径强制重检（重跑 _ensure_ready）不再重复加列。"""
        self.assertGreater(self.hdb.record_capture({'华东': PLANS_A}, 'live'), 0)
        self.hdb.set_db_path(self.hdb.db_path())  # 清 _READY → 下次操作重检
        self.assertGreater(self.hdb.record_capture({'华东': PLANS_B}, 'batch'), 0)
        self.assertIn('store', self._cols())
        rows = self.hdb.query_region_days('华东', TODAY)
        self.assertEqual(len(rows), 3, '重检后数据不受影响')


class TestHistoryStoreQueries(unittest.TestCase):
    """record_capture 店铺维度 + 查询/删除的店铺过滤与隔离"""

    @classmethod
    def setUpClass(cls):
        cls.hdb = _load_module('pdd_history_db_store_q', 'history_db.py')

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pdd_hdb_q_')
        self.hdb.set_db_path(os.path.join(self.tmp, 'history.db'))

    def tearDown(self):
        self.hdb.reset_db_path()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _raw(self, sql, params=()):
        conn = sqlite3.connect(self.hdb.db_path())
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _exec(self, sql, params=()):
        conn = sqlite3.connect(self.hdb.db_path())
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def test_single_level_backward_compat(self):
        """旧单层入参 {region: [plans]} 仍可用 → 归 default（或 store_id 参数指定店）。"""
        sid = self.hdb.record_capture({'华东': PLANS_A}, 'live')
        self.assertGreater(sid, 0, '旧调用形态必须原样兼容')
        rows = self.hdb.query_region_days('华东', TODAY)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['store'] == 'default' for r in rows))

        sid2 = self.hdb.record_capture({'华东': PLANS_B}, 'batch', store_id='shopX')
        self.assertGreater(sid2, 0)
        rows_x = self.hdb.query_region_days('华东', TODAY, store='shopX')
        self.assertEqual(len(rows_x), 1)
        self.assertEqual(rows_x[0]['store'], 'shopX')
        # 单层+指定店铺 不影响 default 店铺旧数据
        self.assertEqual(len(self.hdb.query_region_days('华东', TODAY, store='default')), 2)

    def test_double_level_record_per_store(self):
        """双层入参 {store: {region: [plans]}}：一次入账多店铺，查询互不串店。"""
        sid = self.hdb.record_capture({
            'shopA': {'华东': PLANS_A, '华南': PLANS_B},
            'default': {'华东': [{'name': '默认店货', 'sku_id': '99999999999',
                                  'stock': 1, 'daily': 1}]},
        }, 'import')
        self.assertGreater(sid, 0)

        daily_all = self.hdb.query_daily(30)  # None = 全部店铺
        # query_daily 按 (日,地区) 聚合（store 只是过滤维度不参与分组）
        # 华东 = default 1 行 + shopA 2 行 = 3 行；华南 = shopA 1 行
        self.assertEqual(len(daily_all), 2)
        hd = {r['region']: r for r in daily_all}
        self.assertEqual(hd['华东']['items'], 3)
        self.assertEqual(hd['华东']['stock_total'], 56)
        self.assertEqual(hd['华南']['items'], 1)
        daily_a = self.hdb.query_daily(30, store='shopA')
        self.assertEqual({r['region'] for r in daily_a}, {'华东', '华南'})
        self.assertEqual(daily_a[0]['items'], 2)  # 华东 2 行
        daily_d = self.hdb.query_daily(30, store='default')
        self.assertEqual(len(daily_d), 1)
        self.assertEqual(daily_d[0]['items'], 1)

        self.assertEqual(self.hdb.query_regions(), ['华东', '华南'])
        self.assertEqual(self.hdb.query_regions(store='shopA'), ['华东', '华南'])
        self.assertEqual(self.hdb.query_regions(store='default'), ['华东'])

        rows = self.hdb.query_region_days('华南', TODAY, store='shopA')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['store'], 'shopA')
        self.assertEqual(self.hdb.query_region_days('华南', TODAY, store='default'), [])

    def test_mixed_single_and_double_levels(self):
        """混合入参（同 dict 里既有地区→list 又有店铺→dict）：逐项判别互不干扰。"""
        sid = self.hdb.record_capture({
            '华东': PLANS_A,  # 单层 → default
            'shopA': {'华南': PLANS_B},  # 双层 → shopA
        }, 'batch')
        self.assertGreater(sid, 0)
        self.assertEqual(len(self.hdb.query_region_days('华东', TODAY, store='default')), 2)
        self.assertEqual(len(self.hdb.query_region_days('华南', TODAY, store='shopA')), 1)
        sess = self._raw('SELECT region, item_count FROM capture_sessions WHERE id=?', (sid,))
        self.assertEqual(sess[0][0], '华东', '首地区取第一个非空 plans 的地区')
        self.assertEqual(sess[0][1], 3, 'item_count 跨店铺累计')

    def test_store_empty_rows_alias_default(self):
        """store=''（手工/极老数据）与 'default' 同店：查询命中、出口归一化、同删。"""
        self.hdb.record_capture({'华东': PLANS_A}, 'live')  # store='default'
        self._exec("INSERT INTO history_rows (session_id, captured_at, store, region, sku_id,"
                   " name, stock, sales, days_left, status, qty, warehouse)"
                   " VALUES (1, ?, '', '华东', '', '手插旧行', 7, 1, 2.0, '', 0, '')",
                   (TODAY + ' 08:00:00',))
        rows = self.hdb.query_region_days('华东', TODAY, store='default')
        self.assertEqual(len(rows), 3, "'' 旧行必须与 default 行一同命中")
        self.assertTrue(all(r['store'] == 'default' for r in rows),
                        "'' 出口应归一化为 default")

        n = self.hdb.delete_store('default')
        self.assertEqual(n, 3, "delete_store('default') 显式清空 ''+default 同店行")
        self.assertEqual(self.hdb.query_daily(30), [])
        sids = self._raw('SELECT COUNT(*) FROM capture_sessions')[0][0]
        self.assertEqual(sids, 0, '孤儿 session 应顺带清理')

    def test_query_sku_history_store_filter(self):
        """同 sku 落两店铺：全量 2 行、按店各 1 行；行内 store 字段正确。"""
        self.hdb.record_capture({'shopA': {'华东': PLANS_A}, 'shopB': {'华东': PLANS_A}},
                                'import')
        self.assertEqual(len(self.hdb.query_sku_history('11111111111', 30)), 2)
        a = self.hdb.query_sku_history('11111111111', 30, store='shopA')
        b = self.hdb.query_sku_history('11111111111', 30, store='shopB')
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertEqual({a[0]['store'], b[0]['store']}, {'shopA', 'shopB'})
        # 无 ID 回退 (region, name) 路径同样受店铺过滤：同名两店各命中本店行
        fb_b = self.hdb.query_sku_history('', 30, region='华东', name='洗衣液2kg',
                                          store='shopB')
        self.assertEqual(len(fb_b), 1)
        self.assertEqual(fb_b[0]['store'], 'shopB')
        self.assertEqual(self.hdb.query_sku_history('', 30, region='华东',
                                                    name='不存在的商品', store='shopB'), [])

    def test_delete_region_with_store(self):
        """delete_region(region, store)：限定店铺只删该店；旧签名仍跨店删。"""
        self.hdb.record_capture({'shopA': {'华东': PLANS_A}, 'default': {'华东': PLANS_B}},
                                'import')
        n = self.hdb.delete_region('华东', store='shopA')
        self.assertEqual(n, 2)
        self.assertEqual(self.hdb.query_region_days('华东', TODAY, store='shopA'), [])
        self.assertEqual(len(self.hdb.query_region_days('华东', TODAY, store='default')), 1,
                         '其他店铺同地区数据必须保留')
        n2 = self.hdb.delete_region('华东')  # 旧签名：全部店铺
        self.assertEqual(n2, 1)
        self.assertEqual(self.hdb.query_daily(30), [])
        self.assertEqual(self.hdb.delete_region(''), -1, '空地区仍为无效调用')

    def test_delete_store_semantics(self):
        """delete_store：精确删本店；''/None 无效 -1；孤儿 session 清理。"""
        self.hdb.record_capture({'shopA': {'华东': PLANS_A}}, 'live')
        self.hdb.record_capture({'shopB': {'华南': PLANS_B}}, 'live')
        self.hdb.record_capture({'华东': PLANS_A}, 'live')

        self.assertEqual(self.hdb.delete_store(''), -1)
        self.assertEqual(self.hdb.delete_store(None), -1)
        n = self.hdb.delete_store('shopA')
        self.assertEqual(n, 2)
        self.assertEqual(self.hdb.query_region_days('华东', TODAY, store='shopA'), [])
        self.assertEqual(len(self.hdb.query_region_days('华南', TODAY, store='shopB')), 1,
                         '其他店铺不受影响')
        self.assertEqual(len(self.hdb.query_region_days('华东', TODAY, store='default')), 2)
        live_sids = self._raw('SELECT id FROM capture_sessions ORDER BY id')
        self.assertEqual(len(live_sids), 2, 'shopA 全删后其孤儿 session 应被清理')
        self.assertEqual(self.hdb.delete_store('no-such-store'), 0, '不存在的店铺删 0 行')

    def test_record_capture_failure_paths_no_raise(self):
        """R8 铁律：非法入参一律 -1 / 容忍，绝不外抛。"""
        self.assertEqual(self.hdb.record_capture(None), -1)
        self.assertEqual(self.hdb.record_capture('not-a-dict'), -1)
        self.assertEqual(self.hdb.record_capture(12345), -1)
        # 脏值跳过但 session 仍入账（item_count=0，诚实审计）
        sid = self.hdb.record_capture({'广西': None}, 'live')
        self.assertGreater(sid, 0)
        self.assertEqual(self._raw('SELECT item_count FROM capture_sessions WHERE id=?',
                                   (sid,))[0][0], 0)
        sid2 = self.hdb.record_capture({'shopA': '脏值'}, 'live')
        self.assertGreater(sid2, 0)
        # store_id 空/None → 归 default 落库
        sid3 = self.hdb.record_capture({'华东': [{'name': 'x'}]}, 'live', store_id='')
        self.assertGreater(sid3, 0)
        rows = self.hdb.query_region_days('华东', TODAY)
        self.assertEqual(rows[0]['store'], 'default')

    def test_store_id_normalized_on_write(self):
        """store_id 前后空白/空串 → 落库归一化，不产生脏店铺键。"""
        self.hdb.record_capture({'华东': PLANS_A}, 'live', store_id='  shopA  ')
        rows = self.hdb.query_region_days('华东', TODAY, store='shopA')
        self.assertEqual(len(rows), 2, '写入时店铺 id 应 strip')
        self.assertEqual(rows[0]['store'], 'shopA')


class TestStoresTemplateContract(unittest.TestCase):
    """settings_template.json 契约：stores 节点模板 + Config 合并兼容"""

    def test_template_has_stores_node(self):
        with open(os.path.join(HERE, 'settings_template.json'), encoding='utf-8') as f:
            tpl = json.load(f)
        node = tpl.get('stores')
        self.assertIsInstance(node, dict, '模板必须含 stores 节点（t1 独家修改）')
        self.assertEqual(node.get('active'), 'default')
        lst = node.get('list')
        self.assertIsInstance(lst, list)
        self.assertTrue(lst and lst[0].get('id') == 'default')
        self.assertEqual(lst[0].get('name'), '默认店铺')

    def test_config_merge_keeps_stores_defaults(self):
        """Config._merge：用户无 stores → 补模板默认；用户有 stores → 用户优先。"""
        u = _load_module('pdd_utils_store_tpl', 'utils.py')
        tpl = {'stores': {'active': 'default', 'list': [{'id': 'default', 'name': '默认店铺'}]}}
        merged = u.Config._merge(tpl, {'theme': 'x'})
        self.assertEqual(merged['stores']['list'][0]['id'], 'default',
                         '缺 stores 字段应从模板补全（自愈）')
        user = {'stores': {'active': 'shopB', 'list': [{'id': 'shopB', 'name': 'B'}]}}
        merged2 = u.Config._merge(tpl, user)
        self.assertEqual(merged2['stores']['active'], 'shopB')
        self.assertEqual([s['id'] for s in merged2['stores']['list']], ['shopB'],
                         'list 整体用户优先（缺 default 由注册表读路径自愈）')

    def test_store_registry_exports(self):
        """模块 API 面契约（t6 接入依赖的函数必须存在）。"""
        sr = _load_module('pdd_store_registry_exports', 'store_registry.py')
        for fn in ('get_stores', 'get_active', 'set_active', 'add_store',
                   'rename_store', 'delete_store', 'get_regions', 'save_regions',
                   'get_shipping', 'get_store_name'):
            self.assertTrue(callable(getattr(sr, fn, None)), f'缺少 API: {fn}')
        self.assertEqual(sr.DEFAULT_STORE_ID, 'default')


class TestHistoryStoreMigrationEdgeCases(unittest.TestCase):
    """补边界测试：老库 ALTER 迁移的边界情况"""

    @classmethod
    def setUpClass(cls):
        cls.hdb = _load_module('pdd_hdb_edge', 'history_db.py')

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pdd_hdb_edge_')
        cls = type(self)
        cls.hdb.set_db_path(os.path.join(self.tmp, 'history.db'))

    def tearDown(self):
        self.hdb.reset_db_path()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _raw(self, sql, params=()):
        import sqlite3
        conn = sqlite3.connect(self.hdb.db_path())
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def test_empty_old_db_migration(self):
        """老库（无 store 列）但无数据 → ALTER 成功，零数据丢失"""
        import sqlite3
        old_path = os.path.join(self.tmp, 'empty.db')
        conn = sqlite3.connect(old_path)
        try:
            conn.executescript(OLD_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        # 指向空老库，首次操作触发迁移
        self.hdb.set_db_path(old_path)
        self.hdb.record_capture({'华东': []}, 'live')  # 空capture也触发迁移
        cols = {r[1] for r in self._raw('PRAGMA table_info(history_rows)')}
        self.assertIn('store', cols, '空老库 ALTER 补列应成功')

    def test_old_db_null_store_normalized_to_default(self):
        """老库迁移后：store 列中实际为 '' (空字符串) 归一化为 default"""
        import sqlite3
        old_path = os.path.join(self.tmp, 'null.db')
        conn = sqlite3.connect(old_path)
        try:
            conn.executescript(OLD_SCHEMA)
            conn.execute("INSERT INTO capture_sessions (ts, region, source, item_count)"
                         " VALUES (?, '华东', 'live', 1)", (TODAY + ' 10:00:00',))
            # 插入行（老库无 store 列），迁移后 store='' 归一化为 default
            conn.execute("INSERT INTO history_rows (session_id, captured_at, region, sku_id,"
                         " name, stock, sales, days_left, status, qty, warehouse)"
                         " VALUES (1, ?, '华东', 'SKU001', '商品X', 5, 1, 3.0,"
                         " '3天后下单', 0, '仓')", (TODAY + ' 10:00:00',))
            conn.commit()
        finally:
            conn.close()
        self.hdb.set_db_path(old_path)
        rows = self.hdb.query_region_days('华东', TODAY, store='default')
        self.assertEqual(len(rows), 1, '旧行 store 应归一化为 default')
        self.assertEqual(rows[0]['store'], 'default')

    def test_multi_store_isolation_in_query(self):
        """多店铺数据隔离：shopA 查不到 shopB 数据"""
        # 写入两个店铺的数据
        self.hdb.record_capture({'华东': PLANS_A}, 'live', store_id='shopA')
        self.hdb.record_capture({'华东': PLANS_B}, 'batch', store_id='shopB')
        rows_a = self.hdb.query_region_days('华东', TODAY, store='shopA')
        rows_b = self.hdb.query_region_days('华东', TODAY, store='shopB')
        # 两个店铺都应有数据
        self.assertGreater(len(rows_a), 0, 'shopA 应有数据')
        self.assertGreater(len(rows_b), 0, 'shopB 应有数据')
        # shopA 的商品不包含 shopB 的商品（通过 sku_id 区分）
        sku_a = {r['sku_id'] for r in rows_a}
        sku_b = {r['sku_id'] for r in rows_b}
        self.assertNotEqual(sku_a, sku_b, '两个店铺的 SKU 应不同')
        # shopA 包含 PLANS_A 的商品
        self.assertIn('11111111111', sku_a)  # PLANS_A 第一个商品
        self.assertIn('33333333333', sku_b)  # PLANS_B 第一个商品


if __name__ == '__main__':
    unittest.main()
