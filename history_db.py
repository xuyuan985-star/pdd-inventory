"""
WS-A 本地历史库（SQLite · 纯 stdlib，零新增依赖）
================================================

定位：把每次识别/导入产出的商品计划（plans）按"次"（session 化）追加持久化到
本机 SQLite，供历史趋势页做按日聚合 / 单商品时间序列 / 地区当日明细查询。
数据资产定位是"增值、非核心"——**历史库的任何故障都绝不允许影响识别主流程**。

设计基线（唯一实施基线 = docs/PLAN_commercial_upgrade.md）：
- §1.1.2  SCHEMA_v1 与接口签名；§1.1.3 A1-A8 改动点；§3.0 R8 失败安全铁律；§3.4 T-A2。
- 并发（§2.1.2 采纳）：每次操作短连接 + `journal_mode=WAL` + `busy_timeout=5s`
  + 模块级 threading.Lock 兜底（批量线程与实时线程极端并发不损坏库）；
  写事务统一 `BEGIN IMMEDIATE`，避免 BUSY 反复重试。
- 失败安全（R8 铁律）：`record_capture` 全路径 try/except，任何异常（磁盘满 /
  只读 / 锁 / 损坏）仅经诊断日志（ocr_dlog.txt，与 ocr._ocr_dlog 同款模式）记一行
  并返回 -1，**绝不外抛**；打开库时 `PRAGMA quick_check`，损坏改名
  `history.db.corrupt` 后重建（与 utils.Config.load 的 .corrupt 处置同模式）。
- 体积（§2.1.3 采纳）：只持久化业务字段，不存 `_raw` 全列原文；
  `prune` 双阈值（天数保留 + 行数上限），低于阈值时为廉价 no-op。

线程/Tk 约束：本模块可在任意线程调用（内部自持锁），但**不知道 Tk 的存在**——
趋势页刷新由 GUI 层一律 `win.after` 回主线程（本模块不触碰任何 UI）。

输入契约（供 gui 集成任务 T-A2/A3 使用，gui._calc_from_items 产物 plans）：
    record_capture({地区: [plans 字典, ...]}, source)
    - plans 期望键：name / stock / sales / days_left / status / qty / warehouse /
      sku_id；**缺键容忍**，按默认值落库（sku_id 在 gui A1 改动后才携带，此前为 ''）。
    - 注意：gui plans 里"日销量"键名为 **daily**（_calc_from_items 产物），
      本模块按 `sales` 优先、`daily` 兜底取值，两个键都兼容。
    - source：'live'（实时截图）| 'batch'（批量）| 'file'（图片文件）| 'import'（表格导入）。
    - 返回 session_id；一次识别/导入 = 一个 session（按次追加而非按日快照，
      captured_at 冗余 session.ts，可 SQL 聚合出任意粒度日视图，信息无损）。
    - 空 dict / 空 plans 也入账一个 item_count=0 的 session（诚实审计；
      所有查询只读 history_rows，不受空 session 影响）。

查询语义：
    - query_daily(days, region)   → 按 (日, 地区) 聚合：items 记录数 / alerts 预警数 /
      stock_total 库存合计。**alerts 只统计 status=='立刻补货'（红色硬预警）**——
      黄色"N天后下单"的天数阈值依赖各地区运输时效配置，历史库不做二次推导，
      语义宁可窄而准（DESIGN §4 显式不猜）。
    - query_sku_history(sku_key, days, region, name) → 单商品时间序列（升序）。
      sku_key（SKU 权威关联键，与 ocr.dedup_items 同语义）非空走 sku 索引；
      无 ID 行回退 (region, name) 精确匹配走 (region, name) 索引，内部二选一。
    - query_region_days(region, day) → 某地区某日（'YYYY-MM-DD'）明细行（升序）。
    - prune(retention_days, max_rows) → 双阈值清理；某阈值 <=0 视为不启用该规则；
      返回清理的总行数（含孤儿 session 清理不计入），失败 -1。
    - delete_region(region) → 删除地区联动（settings_ui 调用），返回删除行数，失败 -1。
    - clear_all() → 清空全部历史（gui「清空全部历史」按钮用，二次确认在 GUI 层），
      成功 True。本模块不主动 VACUUM 日常写入，仅清空时回收空间。

测试路径注入：set_db_path(path) / reset_db_path()（单测把库重定向到 tmp 目录）。
"""

import os
import sqlite3
import threading
from datetime import datetime, timedelta

from utils import get_base_dir

__all__ = [
    'DB_NAME', 'db_path', 'set_db_path', 'reset_db_path',
    'record_capture', 'query_daily', 'query_sku_history', 'query_region_days',
    'query_regions', 'prune', 'delete_region', 'clear_all',
]

DB_NAME = 'history.db'
"""历史库文件名，默认位于 get_base_dir()/history.db（打包后 %APPDATA%/PDD补货助手）。"""

_BUSY_TIMEOUT_MS = 5000
"""写锁等待上限（毫秒）：多进程双开/批量与实时极端并发时最多等 5s，超时报锁不崩。"""

# ── 模块级状态（进程内一次初始化 / 一次健康检查；写入全局串行）──────────
_DB_OVERRIDE = {'path': None}       # 测试/重定向用；None = 走 get_base_dir() 默认
_READY = set()                      # 已确认健康且 schema 就绪的库路径（quick_check 每进程每路径只做一次）
_INIT_LOCK = threading.Lock()       # 串行化"健康检查 + 建表"，防并发重复初始化
_WRITE_LOCK = threading.RLock()     # 写操作全局串行（record/prune/delete/clear），读不加锁（WAL 允许并发读）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS capture_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,              -- ISO 本地时间 'YYYY-MM-DD HH:MM:SS'
    region      TEXT NOT NULL DEFAULT '',   -- session 首地区
    source      TEXT NOT NULL,              -- 'live' | 'batch' | 'file' | 'import'
    item_count  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS history_rows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES capture_sessions(id),
    captured_at TEXT NOT NULL,              -- 冗余 session.ts，按日聚合免 join
    region      TEXT NOT NULL,
    sku_id      TEXT NOT NULL DEFAULT '',   -- SKU 权威关联键（与 ocr.dedup_items 同语义）
    name        TEXT NOT NULL,
    stock       INTEGER NOT NULL DEFAULT 0,
    sales       INTEGER NOT NULL DEFAULT 0,
    days_left   REAL,
    status      TEXT NOT NULL DEFAULT '',
    qty         INTEGER NOT NULL DEFAULT 0,
    warehouse   TEXT NOT NULL DEFAULT ''
    -- _raw 全列原文不持久化（§2.1.3：只存业务字段控体积）
);
CREATE INDEX IF NOT EXISTS idx_rows_sku  ON history_rows(sku_id, captured_at DESC) WHERE sku_id != '';
CREATE INDEX IF NOT EXISTS idx_rows_rn   ON history_rows(region, name, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_rows_day  ON history_rows(captured_at);
CREATE INDEX IF NOT EXISTS idx_rows_sess ON history_rows(session_id);
"""


# ── 路径管理 ──────────────────────────────────────────────────────────

def db_path() -> str:
    """当前生效的历史库完整路径（默认 get_base_dir()/history.db）。"""
    p = _DB_OVERRIDE.get('path')
    if p:
        return p
    try:
        base = get_base_dir()
    except Exception:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, DB_NAME)


def set_db_path(path: str):
    """重定向历史库路径（单测注入 tmp 目录用）。

    同时清空进程内"已就绪"标记，使下一次操作对新路径重新做健康检查/建表；
    同一路径重复调用亦可强制触发一次重检（损坏重建测试依赖此行为）。
    """
    with _INIT_LOCK:
        _DB_OVERRIDE['path'] = str(path) if path else None
        _READY.clear()


def reset_db_path():
    """恢复默认路径（get_base_dir()/history.db）。"""
    set_db_path(None)


# ── 内部工具 ──────────────────────────────────────────────────────────

# v1.4.7 P3-R2-L4：_dlog 兜底路径防护
# - 模块级一次性 import 尝试（不每次函数内 re-import）
# - try 引入失败 → fallback 用本地 _dlog_fallback
# - 兜底写盘路径额外 try 嵌套，吸收 makedirs/open/write 任一异常
try:
    from ocr import _ocr_dlog as _OCR_DLOG
except Exception:
    _OCR_DLOG = None  # 标记：模块加载时 import 失败，运行期每次仍 try 但立即 fallback


def _dlog_fallback(msg: str):
    """模块加载时 import ocr._ocr_dlog 失败时的兜底写盘（异常全吞）。"""
    try:
        _base = get_base_dir()
        if not _base:  # 防御：base_dir 为空时 dirname 是 'output' 但 join 出 'output/ocr_dlog.txt'
            return
        _p = os.path.join(_base, 'output', 'ocr_dlog.txt')
        _dir = os.path.dirname(_p)
        if not _dir:  # 防御：dirname 为空时 makedirs('') 必抛
            return
        os.makedirs(_dir, exist_ok=True)
        with open(_p, 'a', encoding='utf-8') as _f:
            _f.write(msg + '\n')
    except Exception:
        pass


def _dlog(msg: str):
    """诊断日志：与 ocr._ocr_dlog 同款模式（写 ocr_dlog.txt，异常全吞）。

    优先复用 ocr._ocr_dlog（应用内 ocr 必已加载，零额外开销）；
    单测/独立场景导入失败时回退为直写同一文件。绝不外抛。

    v1.4.7 P3-R2-L4：模块加载时 try import 一次性解析；运行期不再每次 re-import
    （旧实现每次都 from ocr import _ocr_dlog 在大循环调用下累积开销）。兜底路径加
    _base 空校验与 _dir 空校验（防 makedirs('') 抛 FileNotFoundError）。
    """
    try:
        if _OCR_DLOG is not None:
            _OCR_DLOG(msg)
            return
    except Exception:
        pass
    _dlog_fallback(msg)


def _to_int(v, default=0) -> int:
    """宽容整数化：None/''/非法串 → default；'12.9' → 12。"""
    try:
        if v is None or v == '':
            return default
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def _to_float(v, default=None):
    """宽容浮点化：None/''/非法串 → default（days_left 允许 NULL）。"""
    try:
        if v is None or v == '':
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_str(v) -> str:
    return '' if v is None else str(v).strip()


def _now_ts() -> str:
    """ISO 本地时间（字典序 == 时间序，可直接做字符串比较与按日前缀聚合）。"""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _day_floor(days) -> str:
    """days 天窗口的起始日（含当天往前数 days 天）；days<=0 返回 None（不限窗口）。

    返回 'YYYY-MM-DD' 字符串：与 'YYYY-MM-DD HH:MM:SS' 做字典序 >= 比较即"含首日全天"。
    """
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = 30
    if d <= 0:
        return None
    return (datetime.now() - timedelta(days=d - 1)).strftime('%Y-%m-%d')


def _connect(path: str) -> sqlite3.Connection:
    """短连接：busy_timeout 兜底（connect 的 timeout 与 PRAGMA 双保险）。"""
    conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_MS / 1000.0)
    conn.isolation_level = None  # 关闭隐式事务，写路径用显式 BEGIN IMMEDIATE 全权控制
    conn.execute(f'PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}')
    return conn


def _rollback_safe(conn: sqlite3.Connection):
    """尽力回滚（事务可能已自动结束，回滚失败不掩盖原异常）。"""
    try:
        conn.execute('ROLLBACK')
    except sqlite3.Error:
        pass


def _quarantine(path: str):
    """损坏库隔离：改名 .corrupt（沿用 utils.Config.load 同模式）+ 清 WAL 侧文件。

    隔离失败（文件被锁等）只记日志——后续建表失败会走失败安全路径，不外抛。
    """
    _dlog(f'[history] 历史库损坏（quick_check 未通过），改名重建：{path}')
    try:
        corrupt = path + '.corrupt'
        if os.path.exists(corrupt):
            os.remove(corrupt)
        os.replace(path, corrupt)
    except OSError as e:
        _dlog(f'[history] 损坏库隔离失败（本次读写将不可用）：{e}')
        return
    for suffix in ('-wal', '-shm'):
        side = path + suffix
        try:
            if os.path.exists(side):
                os.remove(side)
        except OSError:
            pass  # WAL 侧文件被锁不致命，sqlite 下次打开会自行处理


def _ensure_ready() -> bool:
    """确保库目录存在、损坏库已隔离重建、schema 就绪；失败返回 False（不外抛）。

    quick_check 每进程每路径只做一次（_READY 缓存）——大库上每次操作都查太贵；
    进程中途出现损坏由操作路径的 DatabaseError 自愈重试兜底（见 _run_db）。
    """
    path = db_path()
    try:
        base = os.path.dirname(path)
        if base:
            os.makedirs(base, exist_ok=True)
    except OSError as e:
        _dlog(f'[history] 数据目录不可创建，历史库停用：{e}')
        return False
    with _INIT_LOCK:
        if path in _READY:
            return True
        # 存在且非空 → 做一次健康检查；新库/空文件（sqlite 视为合法空库）跳过
        if os.path.exists(path) and os.path.getsize(path) > 0:
            ok = False
            try:
                conn = _connect(path)
                try:
                    row = conn.execute('PRAGMA quick_check').fetchone()
                    ok = bool(row) and str(row[0]).lower() == 'ok'
                finally:
                    conn.close()
            except sqlite3.Error:
                ok = False
            if not ok:
                _quarantine(path)
        try:
            conn = _connect(path)
            try:
                try:
                    conn.execute('PRAGMA journal_mode=WAL')  # 持久属性，已 WAL 时为 no-op
                except sqlite3.Error:
                    pass  # 只读库等场景不阻塞初始化（后续写入会显式失败并被吞）
                conn.executescript(_SCHEMA)
            finally:
                conn.close()
        except sqlite3.Error as e:
            _dlog(f'[history] 历史库初始化失败（磁盘满/只读/被锁），本次停用：{e}')
            return False
        _READY.add(path)
        return True


def _run_db(fn, default):
    """统一执行骨架：ensure → 短连接 → fn(conn)；DatabaseError 自愈重试一次。

    自愈语义：操作中途遇"no such table / not a database"（库被外部删除或调包）→
    丢弃 _READY 强制下次重检，立即重试一次；仍失败则记日志返回 default。
    绝不外抛——调用方（含识别主流程挂点）永远拿不到本模块的异常。
    """
    path = db_path()
    last_err = None
    for _attempt in (0, 1):
        try:
            if not _ensure_ready():
                return default
            conn = _connect(path)
            try:
                return fn(conn)
            finally:
                conn.close()
        except sqlite3.Error as e:
            last_err = e
            with _INIT_LOCK:
                _READY.discard(path)
    _dlog(f'[history] 数据库操作失败（已按默认值降级，识别主流程不受影响）：{last_err}')
    return default


_ROW_COLS = ('id', 'session_id', 'captured_at', 'region', 'sku_id', 'name', 'stock',
             'sales', 'days_left', 'status', 'qty', 'warehouse')


def _row_dicts(cur) -> list:
    """游标 → _ROW_COLS 顺序的 dict 列表（查询出口统一形状）。"""
    out = []
    for r in cur:
        row = dict(zip(_ROW_COLS, r))
        row['stock'] = _to_int(row.get('stock'))
        row['sales'] = _to_int(row.get('sales'))
        row['qty'] = _to_int(row.get('qty'))
        row['days_left'] = _to_float(row.get('days_left'))
        out.append(row)
    return out


# ── 写入 ──────────────────────────────────────────────────────────────

def record_capture(plans_by_region, source='live') -> int:
    """记录一次识别/导入 = 一个 session（详见模块 docstring 输入契约）。

    executemany 批量 INSERT + BEGIN IMMEDIATE 单事务；返回 session_id；
    **任何异常仅记日志并返回 -1，绝不外抛（R8 铁律）**。
    """
    try:
        if not isinstance(plans_by_region, dict):
            _dlog('[history] record_capture 入参非 dict（期望 {region: [plans]}），忽略')
            return -1
        ts = _now_ts()
        src = _to_str(source)[:32] or 'live'
        rows = []           # (region, sku_id, name, stock, sales, days_left, status, qty, warehouse)
        first_region = ''   # session 首地区（取第一个非空地区）
        for region, plans in plans_by_region.items():
            reg = _to_str(region)
            if not isinstance(plans, (list, tuple)):
                continue
            if plans and not first_region:
                first_region = reg
            for p in plans:
                if not isinstance(p, dict):
                    continue  # 单条脏数据跳过，不影响其余行
                rows.append((
                    reg,
                    _to_str(p.get('sku_id')),
                    _to_str(p.get('name')),
                    _to_int(p.get('stock', 0)),
                    _to_int(p.get('sales', p.get('daily', 0))),  # gui plans 的日销量键是 daily
                    _to_float(p.get('days_left')),
                    _to_str(p.get('status')),
                    _to_int(p.get('qty', 0)),
                    _to_str(p.get('warehouse')),
                ))

        def op(conn):
            conn.execute('BEGIN IMMEDIATE')
            try:
                cur = conn.execute(
                    'INSERT INTO capture_sessions (ts, region, source, item_count) VALUES (?,?,?,?)',
                    (ts, first_region, src, len(rows)))
                sid = int(cur.lastrowid)
                conn.executemany(
                    'INSERT INTO history_rows (session_id, captured_at, region, sku_id, name,'
                    ' stock, sales, days_left, status, qty, warehouse)'
                    ' VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    [(sid, ts) + r for r in rows])
                conn.execute('COMMIT')
                return sid
            except Exception:
                _rollback_safe(conn)
                raise

        with _WRITE_LOCK:
            sid = _run_db(op, None)
        return sid if sid else -1
    except Exception as e:  # 兜底铁律（R8）：拼装/锁/任何意外都不外抛
        _dlog(f'[history] 记录历史失败（不影响识别主流程）：{e}')
        return -1


def prune(retention_days=180, max_rows=200000) -> int:
    """双阈值保留策略：按天数删过期 + 超行数上限删最旧；孤儿 session 一并清理。

    某阈值 <=0 视为不启用该规则；低于阈值时为廉价 no-op（只做一次 COUNT）。
    返回清理的 history_rows 总行数；失败记日志返回 -1。
    """
    try:
        r = _to_int(retention_days, 0)
        m = _to_int(max_rows, 0)

        def op(conn):
            conn.execute('BEGIN IMMEDIATE')
            try:
                deleted = 0
                if r > 0:
                    floor = (datetime.now() - timedelta(days=r)).strftime('%Y-%m-%d')
                    cur = conn.execute('DELETE FROM history_rows WHERE captured_at < ?', (floor,))
                    deleted += max(cur.rowcount, 0)
                if m > 0:
                    total = conn.execute('SELECT COUNT(*) FROM history_rows').fetchone()[0]
                    if total > m:
                        cur = conn.execute(
                            'DELETE FROM history_rows WHERE id NOT IN'
                            ' (SELECT id FROM history_rows ORDER BY id DESC LIMIT ?)', (m,))
                        deleted += max(cur.rowcount, 0)
                conn.execute('DELETE FROM capture_sessions WHERE id NOT IN'
                             ' (SELECT DISTINCT session_id FROM history_rows)')
                conn.execute('COMMIT')
                return deleted
            except Exception:
                _rollback_safe(conn)
                raise

        with _WRITE_LOCK:
            return _run_db(op, -1)
    except Exception as e:
        _dlog(f'[history] prune 失败：{e}')
        return -1


def delete_region(region) -> int:
    """删除某地区全部历史行 + 孤儿 session（settings_ui 删除地区联动用）。

    返回删除行数；region 为空视为无效调用，失败记日志返回 -1。
    """
    try:
        reg = _to_str(region)
        if not reg:
            return -1

        def op(conn):
            conn.execute('BEGIN IMMEDIATE')
            try:
                cur = conn.execute('DELETE FROM history_rows WHERE region = ?', (reg,))
                n = max(cur.rowcount, 0)
                conn.execute('DELETE FROM capture_sessions WHERE id NOT IN'
                             ' (SELECT DISTINCT session_id FROM history_rows)')
                conn.execute('COMMIT')
                return n
            except Exception:
                _rollback_safe(conn)
                raise

        with _WRITE_LOCK:
            return _run_db(op, -1)
    except Exception as e:
        _dlog(f'[history] delete_region 失败：{e}')
        return -1


def clear_all() -> bool:
    """清空全部历史（两表全删 + VACUUM 回收空间）。成功 True，失败 False。

    二次确认交互在 GUI 层完成；本函数不删库文件本身（保留 schema 与 WAL 状态）。
    """
    try:
        def op(conn):
            conn.execute('BEGIN IMMEDIATE')
            try:
                conn.execute('DELETE FROM history_rows')
                conn.execute('DELETE FROM capture_sessions')
                conn.execute('COMMIT')
            except Exception:
                _rollback_safe(conn)
                raise
            # VACUUM 不能在事务内执行：COMMIT 之后连接处于自动提交态
            conn.execute('VACUUM')
            return True

        with _WRITE_LOCK:
            return bool(_run_db(op, False))
    except Exception as e:
        _dlog(f'[history] clear_all 失败：{e}')
        return False


# ── 查询（只读；失败返回空列表不外抛，趋势页可安全调用）───────────────

def query_daily(days=30, region=None) -> list:
    """按 (日, 地区) 聚合的趋势数据源：[{day, region, items, alerts, stock_total}, ...]。

    days：最近 N 天窗口（<=0 不限）；region：None/'' 查全部地区，否则单地区。
    按日期降序、地区升序。alerts = 立刻补货（红色硬预警）行数（语义见模块 docstring）。

    t12 P2-C：enforce=true 且 free 时把 days 钳制到 FREE_HISTORY_DAYS（30），数据库数据不删。
    用户裁定：默认全免（enforce=false），所有用户不受限；Pro 也不受限。
    """
    try:
        # t12 P2-C：免费版历史趋势窗口钳制（仅 enforce=true 时生效）
        try:
            from auth.license import get_history_days_limit, is_pro
            # 默认 enforce=false，所有人 unlimited；显式 enforce=true 才钳制
            try:
                from utils import Config
                _cfg = Config.load() if hasattr(Config, "load") else {}
                _lic_cfg = _cfg.get("license", {}) if isinstance(_cfg, dict) else {}
                _enforce = bool(_lic_cfg.get("enforce", False))
                _key = _lic_cfg.get("key", "") or ""
            except Exception:
                _enforce = False
                _key = ""
            if _enforce and not is_pro(_key, enforce=True) and days > 0:
                from auth.license import FREE_HISTORY_DAYS
                if days > FREE_HISTORY_DAYS:
                    days = FREE_HISTORY_DAYS
        except Exception:
            pass  # 失败安全：钳制不生效 → 返全量
        floor = _day_floor(days)
        reg = _to_str(region)

        def op(conn):
            sql = ['SELECT substr(captured_at, 1, 10) AS day, region, COUNT(*) AS items,',
                   "       SUM(CASE WHEN status = '立刻补货' THEN 1 ELSE 0 END) AS alerts,",
                   '       SUM(stock) AS stock_total',
                   'FROM history_rows WHERE 1=1']
            params = []
            if floor:
                sql.append('AND captured_at >= ?')
                params.append(floor)
            if reg:
                sql.append('AND region = ?')
                params.append(reg)
            sql.append('GROUP BY day, region ORDER BY day DESC, region ASC')
            out = []
            for day, r, items, alerts, stock_total in conn.execute(' '.join(sql), params):
                out.append({'day': day, 'region': r,
                            'items': _to_int(items), 'alerts': _to_int(alerts),
                            'stock_total': _to_int(stock_total)})
            return out

        return _run_db(op, [])
    except Exception as e:
        _dlog(f'[history] query_daily 失败：{e}')
        return []


def query_sku_history(sku_key='', days=90, region='', name='') -> list:
    """单商品时间序列（captured_at 升序），行形状 = 全业务字段 dict。

    关联键二选一（SKU 权威，无 ID 回退，与 ocr.dedup_items 同语义）：
    - sku_key 非空 → WHERE sku_id = ?（走 idx_rows_sku）；
    - 否则 region+name 均非空 → WHERE region = ? AND name = ?（走 idx_rows_rn）；
    - 两者皆无 → 返回 []（不给全表——调用方必须明确要哪件商品）。
    """
    try:
        sku = _to_str(sku_key)
        reg = _to_str(region)
        nm = _to_str(name)
        if not sku and not (reg and nm):
            return []
        floor = _day_floor(days)

        def op(conn):
            sql = ('SELECT id, session_id, captured_at, region, sku_id, name, stock, sales,'
                   ' days_left, status, qty, warehouse FROM history_rows WHERE ')
            params = []
            if sku:
                sql += 'sku_id = ?'
                params.append(sku)
            else:
                sql += 'region = ? AND name = ?'
                params.extend([reg, nm])
            if floor:
                sql += ' AND captured_at >= ?'
                params.append(floor)
            sql += ' ORDER BY captured_at ASC, id ASC'
            return _row_dicts(conn.execute(sql, params))

        return _run_db(op, [])
    except Exception as e:
        _dlog(f'[history] query_sku_history 失败：{e}')
        return []


def query_region_days(region, day) -> list:
    """某地区某日（day='YYYY-MM-DD'，容忍更长前缀）明细行，captured_at/name 升序。"""
    try:
        reg = _to_str(region)
        d = _to_str(day)
        if not reg or not d:
            return []

        def op(conn):
            sql = ('SELECT id, session_id, captured_at, region, sku_id, name, stock, sales,'
                   ' days_left, status, qty, warehouse FROM history_rows'
                   " WHERE region = ? AND captured_at LIKE ? || '%'")
            return _row_dicts(conn.execute(sql + ' ORDER BY captured_at ASC, name ASC, id ASC',
                                           (reg, d)))

        return _run_db(op, [])
    except Exception as e:
        _dlog(f'[history] query_region_days 失败：{e}')
        return []


def query_regions() -> list:
    """历史库中出现过的地区列表（升序，趋势页地区下拉数据源）；失败返回 []。"""
    try:
        def op(conn):
            return [r[0] for r in conn.execute(
                "SELECT DISTINCT region FROM history_rows WHERE region != '' ORDER BY region ASC")]

        return _run_db(op, [])
    except Exception as e:
        _dlog(f'[history] query_regions 失败：{e}')
        return []
