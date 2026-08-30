"""
多店铺隔离——店铺清单注册表（t1 后端 · 唯一店铺权威）
====================================================

定位：
1. settings.json `stores` 节点的唯一读写通道（店铺清单 + 当前激活店铺），
   全部经 utils.Config 走（原子写 / mtime 缓存 / 深拷贝，模板自愈合并）；
2. regions.json（商品运输时效）按店铺隔离的读写通道——旧顶层格式自动迁移。

数据结构（settings.json）：
    "stores": {
        "active": "default",
        "list": [ {"id": "default", "name": "默认店铺"}, ... ]
    }
- id 稳定（'store_' + uuid 片段，创建后永不变）；name 可随时改；
- 「默认店铺」id 固定 'default'，首启自动创建（模板自愈 + 本模块读路径自愈双保险），
  **禁止删除**；
- 读路径发现 stores 节点缺失/损坏/active 失效 → 原地自愈并写回。

regions.json 从旧顶层 {region: {product: days}} 升级为按店铺：
    {"<store_id>": {"<region>": {"<product>": days}}, ...}
- 旧格式迁移：整份旧数据并入 default 店铺；{region: int} → {region: {"": int}}
  （与 gui._load_regions 旧兼容语义一致；_get_shipping 的
  product 优先 → "" 回退 → 全局 3 语义保持不变）；
- 新旧格式判定：顶层 'default' 键的值为「全 dict 值的 dict」→ 新格式
  （迁移写入后必有 default 店铺键），否则按旧格式。判定幂等，可安全反复读写。

失败哲学（DESIGN §4 + history_db R8 同款铁律）：全部公开 API 全路径 try/except，
任何异常仅诊断日志（ocr_dlog.txt）绝不外抛——读失败返回安全默认值，
写失败返回 False / None。调用方（GUI/识别主流程）永远拿不到本模块的异常。

线程约束：本模块不触碰 Tk；内部自持锁串行化"读-改-写"，可任意线程调用。

t6 接入契约（GUI 层改造用，本模块不改 gui.py）：
    self.regions = store_registry.get_regions()  # 替代 _load_regions
    store_registry.save_regions(self.regions)  # 替代 _save_regions（写入当前激活店铺）
    store_registry.get_shipping(region, product, regions)  # 可选：与 _get_shipping 同语义的纯函数
    店铺切换 → set_active(id) 后重取 get_regions()；删店 → delete_store(id) 返回店铺名，
    再调 history_db.delete_store(id) 联动清历史。
"""

import json
import os
import shutil
import sys
import threading
import time
import uuid

# 晚绑定：经模块属性访问 utils（单测可整体替换 utils 副本，绝不写真实用户配置）
import utils

try:
    from history_db import _dlog  # 与历史库共用同一诊断日志通道
except Exception:  # pragma: no cover - 独立加载兜底
    _dlog = None

__all__ = [
    'DEFAULT_STORE_ID', 'DEFAULT_STORE_NAME', 'REGIONS_FILE',
    'get_stores', 'get_store_name', 'get_active', 'set_active',
    'add_store', 'rename_store', 'delete_store',
    'get_regions', 'save_regions', 'get_shipping',
]

DEFAULT_STORE_ID = 'default'
"""默认店铺固定 id（与 history_db.DEFAULT_STORE_ID 同值；'' 旧历史行也归此店）。"""

DEFAULT_STORE_NAME = '默认店铺'

REGIONS_FILE = 'regions.json'
"""运输时效配置文件名（位于 get_base_dir() 下，打包后在 %APPDATA% 目录）。"""

# 读-改-写串行锁（Config.save 自身有锁，但 load→改→save 的组合必须原子）
_LOCK = threading.RLock()


if _dlog is None:  # pragma: no cover
    def _dlog(msg: str):
        """兜底诊断日志（与 history_db._dlog_fallback 同款，异常全吞）。"""
        try:
            base = utils.get_base_dir()
            if not base:
                return
            p = os.path.join(base, 'output', 'ocr_dlog.txt')
            d = os.path.dirname(p)
            if not d:
                return
            os.makedirs(d, exist_ok=True)
            with open(p, 'a', encoding='utf-8') as f:
                f.write(msg + '\n')
        except Exception:
            pass


# ── settings.json stores 节点 ────────────────────────────────────────

def _normalize_stores(raw):
    """归一化 stores 节点 → {'active': str, 'list': [{'id','name'},...]}。

    返回 (node, changed)：过滤脏条目（非 dict / 缺 id / id 重复）、保证 default
    店铺存在（缺失时插到队首）、active 失效回落 default。只有真正修复了数据
    才报 changed=True（避免纯读路径反复写配置）。
    """
    changed = False
    if isinstance(raw, dict):
        node = raw
    else:
        node = {}  # 缺失/非 dict（损坏）→ 空节点按需自愈
        changed = True
    src_list = node.get('list')
    out, seen = [], set()
    if isinstance(src_list, list):
        for it in src_list:
            if not isinstance(it, dict):
                changed = True
                continue
            sid = str(it.get('id') or '').strip()
            name = str(it.get('name') or '').strip()
            if not sid or sid in seen:
                changed = True
                continue
            # R2 问题 修复：原代码「if name != it.get('name'): changed = True」
            # 会把「店 A 」末尾空格（用户原样保存）误判为数据脏——触发自愈写盘、
            # 静默吞掉用户空格。改为：仅当原值类型非 str 才标 changed（dict 原生
            # 损坏容忍交给 try/except + JSON dump，不在此处抹平用户原值）。
            raw_name = it.get('name')
            if not isinstance(raw_name, str):
                changed = True
            seen.add(sid)
            out.append({'id': sid, 'name': name or sid})
    if DEFAULT_STORE_ID not in seen:
        out.insert(0, {'id': DEFAULT_STORE_ID, 'name': DEFAULT_STORE_NAME})
        seen.add(DEFAULT_STORE_ID)
        changed = True
    active = str(node.get('active') or '').strip()
    if not active or active not in seen:
        active = DEFAULT_STORE_ID
        changed = True
    return {'active': active, 'list': out}, changed


def _load_settings():
    """Config.load 深拷贝读取（失败抛给上层统一吞）。"""
    return utils.Config.load()


def _save_settings(data):
    """Config.save 原子写回（内部清 mtime 缓存）。"""
    utils.Config.save(data)


def get_stores() -> list:
    """店铺清单 [{'id','name'},...]（副本，改返回值不影响内部状态）。

    首启/节点损坏时自愈：保证 default 店铺存在并把修复写回 settings.json。
    任何异常仅日志，返回 [default] 兜底，绝不外抛。
    """
    try:
        with _LOCK:
            data = _load_settings()
            node, changed = _normalize_stores(data.get('stores'))
            if changed:
                data['stores'] = node
                _save_settings(data)
            return [dict(s) for s in node['list']]
    except Exception as e:
        _dlog(f'[store] get_stores 失败（返回默认店铺兜底）：{e}')
        return [{'id': DEFAULT_STORE_ID, 'name': DEFAULT_STORE_NAME}]


def get_store_name(store_id) -> str:
    """id → 店铺名；未知/失败返回 ''（GUI 显示用便利接口）。"""
    try:
        sid = str(store_id or '').strip()
        if not sid:
            return ''
        for s in get_stores():
            if s['id'] == sid:
                return s['name']
        return ''
    except Exception as e:
        _dlog(f'[store] get_store_name 失败：{e}')
        return ''


def get_active() -> str:
    """当前激活店铺 id；无效回落 'default'（读路径自愈）。绝不外抛。"""
    try:
        with _LOCK:
            data = _load_settings()
            node, changed = _normalize_stores(data.get('stores'))
            if changed:
                data['stores'] = node
                _save_settings(data)
            return node['active']
    except Exception as e:
        _dlog(f'[store] get_active 失败（回落 default）：{e}')
        return DEFAULT_STORE_ID


def set_active(store_id) -> bool:
    """切换激活店铺；店铺必须存在。成功 True；失败（不存在/空/异常）False。"""
    try:
        sid = str(store_id or '').strip()
        if not sid:
            _dlog('[store] set_active 拒绝：空店铺 id')
            return False
        with _LOCK:
            data = _load_settings()
            node, _ = _normalize_stores(data.get('stores'))
            if not any(s['id'] == sid for s in node['list']):
                _dlog(f'[store] set_active 拒绝：店铺不存在 {sid!r}')
                return False
            if node['active'] == sid and isinstance(data.get('stores'), dict):
                return True  # 幂等：已激活且配置形态正常，免写盘
            node['active'] = sid
            data['stores'] = node
            _save_settings(data)
            return True
    except Exception as e:
        _dlog(f'[store] set_active 失败：{e}')
        return False


def _gen_store_id(existing_ids) -> str:
    """生成不冲突的稳定店铺 id：'store_' + uuid 片段。"""
    while True:
        sid = f"store_{uuid.uuid4().hex[:12]}"
        if sid not in existing_ids and sid != DEFAULT_STORE_ID:
            return sid


def add_store(name):
    """新增店铺，返回 {'id','name'}；失败（名字为空/异常）返回 None。

    允许重名（id 才是权威键）；新店 active 不变，不自动切换。
    """
    try:
        nm = str(name or '').strip()
        if not nm:
            _dlog('[store] add_store 拒绝：店铺名为空')
            return None
        with _LOCK:
            data = _load_settings()
            node, _ = _normalize_stores(data.get('stores'))
            item = {'id': _gen_store_id({s['id'] for s in node['list']}), 'name': nm}
            node['list'].append(item)
            data['stores'] = node
            _save_settings(data)
            return dict(item)
    except Exception as e:
        _dlog(f'[store] add_store 失败：{e}')
        return None


def rename_store(store_id, name) -> bool:
    """改店铺名（id 永不变）。成功 True；店铺不存在/名字为空/异常 → False。"""
    try:
        sid = str(store_id or '').strip()
        nm = str(name or '').strip()
        if not sid or not nm:
            _dlog('[store] rename_store 拒绝：id/名字为空')
            return False
        with _LOCK:
            data = _load_settings()
            node, _ = _normalize_stores(data.get('stores'))
            hit = next((s for s in node['list'] if s['id'] == sid), None)
            if not hit:
                _dlog(f'[store] rename_store 拒绝：店铺不存在 {sid!r}')
                return False
            if hit['name'] == nm:
                return True  # 幂等
            hit['name'] = nm
            data['stores'] = node
            _save_settings(data)
            return True
    except Exception as e:
        _dlog(f'[store] rename_store 失败：{e}')
        return False


def delete_store(store_id):
    """删除店铺，**返回被删店铺名**（供 GUI 联动调 history_db.delete_store 清历史）。

    铁律：default 店铺禁止删除（返回 None）；id 不存在/异常也返回 None。
    删除成功时：若它是激活店 → active 回落 default；并尽力清掉该店的
    regions.json 配置节（失败不影响删除结果）。
    """
    try:
        sid = str(store_id or '').strip()
        if not sid:
            _dlog('[store] delete_store 拒绝：空店铺 id')
            return None
        if sid == DEFAULT_STORE_ID:
            _dlog('[store] delete_store 拒绝：默认店铺不可删除')
            return None
        with _LOCK:
            data = _load_settings()
            node, _ = _normalize_stores(data.get('stores'))
            hit = next((s for s in node['list'] if s['id'] == sid), None)
            if not hit:
                _dlog(f'[store] delete_store 拒绝：店铺不存在 {sid!r}')
                return None
            node['list'] = [s for s in node['list'] if s['id'] != sid]
            if node['active'] == sid:
                node['active'] = DEFAULT_STORE_ID
            data['stores'] = node
            _save_settings(data)
        try:
            _drop_store_regions(sid)  # regions 配置联动清理（尽力而为）
        except Exception as e:
            _dlog(f'[store] 删店后清理 regions 失败（不影响删店结果）：{e}')
        return hit['name']
    except Exception as e:
        _dlog(f'[store] delete_store 失败：{e}')
        return None


# ── regions.json 按店铺隔离（运输时效）────────────────────────────────

def _regions_path() -> str:
    return os.path.join(utils.get_base_dir(), REGIONS_FILE)


def _read_regions_raw() -> dict:
    """读 regions.json 原始内容（dict）；缺失/损坏/非 dict → {}。

    EXE 首次运行：从内置资源复制模板（与 gui._load_regions 同行为）。
    """
    path = _regions_path()
    if not os.path.exists(path) and getattr(sys, 'frozen', False):
        bundled = os.path.join(getattr(sys, '_MEIPASS', '') or '', REGIONS_FILE)
        if bundled and os.path.exists(bundled):
            try:
                shutil.copy(bundled, path)
            except Exception:
                pass
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _norm_region_map(m) -> dict:
    """单店铺层归一化 {region: value} → {region: {product: days}}。

    与 gui._load_regions 同语义：int/float → {'': 值}（旧默认天数）；
    dict → 原样（product 名优先，"" 键为旧默认）；其他脏值 → {}。
    """
    out = {}
    if not isinstance(m, dict):
        return out
    for region, val in m.items():
        r = str(region)
        if isinstance(val, (int, float)):
            out[r] = {'': val}
        elif isinstance(val, dict):
            out[r] = dict(val)
        else:
            out[r] = {}
    return out


def _is_per_store(data: dict) -> bool:
    """新格式判定：顶层 'default' 键存在且其值为「全 dict 值的 dict」。

    迁移后的写入必含 default 店铺键（default 店铺无数据也写 {"default": {}}），
    故该判定幂等；旧格式（含 region 恰好叫 "default" 但值为天数的极端场景）
    不会误判——那时 default 键下的值是数字，不是全 dict。
    """
    d = data.get(DEFAULT_STORE_ID)
    return isinstance(d, dict) and all(isinstance(v, dict) for v in d.values())


def load_all_regions() -> dict:
    """regions.json → 按店铺全量 {store_id: {region: {product: days}}}（含旧格式迁移）。"""
    data = _read_regions_raw()
    if _is_per_store(data):
        return {str(k): _norm_region_map(v) for k, v in data.items()
                if isinstance(v, dict)}
    # 旧顶层格式：整份并入 default 店铺
    return {DEFAULT_STORE_ID: _norm_region_map(data)}


def _write_regions_file(allmap: dict) -> bool:
    """按店铺全量原子写 regions.json（tmp + os.replace，与 gui._save_regions 同模式）。"""
    path = _regions_path()
    d = os.path.dirname(path) or '.'
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f'regions.json.tmp{time.time()}')
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(allmap, f, ensure_ascii=False, indent=2)
            f.flush()
            if os.name != 'nt':
                os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def get_regions(store_id=None) -> dict:
    """某店铺的运输时效 {region: {product: days}}；store_id 缺省 = 当前激活店。

    旧格式 regions.json 自动迁移（旧数据归 default 店铺，内存生效，下次保存
    落为新格式）；失败返回 {}。返回值为全新构造，改它不影响文件。
    """
    try:
        sid = str(store_id or '').strip() or get_active()
        with _LOCK:
            allmap = load_all_regions()
        return allmap.get(sid, {})
    except Exception as e:
        _dlog(f'[store] get_regions 失败：{e}')
        return {}


def save_regions(regions, store_id=None) -> bool:
    """把单店铺运输时效写回 regions.json（只覆盖该店铺节，其他店铺数据保留）。

    store_id 缺省 = 当前激活店。写入时整份文件自动升级为按店铺新格式
    （旧格式数据在读取时已并入 default）。成功 True；失败 False 绝不外抛。
    """
    try:
        sid = str(store_id or '').strip() or get_active()
        with _LOCK:
            allmap = load_all_regions()
            allmap[sid] = _norm_region_map(regions)
            return _write_regions_file(allmap)
    except Exception as e:
        _dlog(f'[store] save_regions 失败：{e}')
        return False


def _drop_store_regions(store_id) -> bool:
    """删店联动：从 regions.json 移除该店铺节。店铺无数据也返回 True。"""
    sid = str(store_id or '').strip()
    if not sid:
        return False
    with _LOCK:
        allmap = load_all_regions()
        if sid not in allmap:
            return True
        del allmap[sid]
        return _write_regions_file(allmap)


def get_shipping(region, product_name, regions=None) -> int:
    """某地区某商品的运输天数（与 gui._get_shipping 同语义纯函数）。

    product 名优先 → 地区默认（"" 键）→ 全局默认 3。regions 缺省 = 当前激活店。
    """
    try:
        m = regions if isinstance(regions, dict) else get_regions()
        rd = m.get(str(region), {})
        if isinstance(rd, dict):
            v = rd.get(product_name, rd.get('', 3))
            try:
                return int(v)
            except (TypeError, ValueError):
                return 3
        return 3
    except Exception:
        return 3
