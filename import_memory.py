"""
PDD EZ — 导入映射记忆模块（R1 流程效率 · t2 产出）

把"上一次成功导入 CSV/XLSX 时用的列映射"持久化到 settings.json，
下次导入相同结构的文件时可直接复用（避免每次都重新猜 mapping）。

公开 API（纯函数 / 守卫式访问，settings_ui 调用）：
  - get_last_mapping() -> dict | None
        读上次映射。空/损坏 → None（不抛——§4 失败哲学）。
  - save_last_mapping(mapping) -> bool
        原子写。mapping 非 dict → False（拒绝）；写盘失败 → False。
        同步落 'saved_at' 时间戳（ISO 格式、本机时间）。
  - clear_last_mapping() -> bool
        从 settings 删 import_memory 节点。无节点/写盘失败 → False。
  - last_mapping_matches(headers, mapping) -> tuple[match: bool, hit_rate: float]
        判定 headers 是否仍命中 mapping。
        - normalize 后精确比：字段目标列名 ∈ normalize 后的 headers。
        - hit_rate = 命中字段数 / 期望字段数（期望=name/stock/sales 三键，0~1）。
        - mapping 非 dict / 缺核心字段 → (False, 0.0)。

约束：
  - settings 通道：走 utils.Config.load/save（mtime 缓存 + 原子写 + 锁）；
    不在 import_memory 维护自己的配置文件，避免和 settings.json 双源不一致。
  - 损坏容忍：Config.load 已把损坏配置隔离成 .corrupt + 恢复 .bak，
    本模块对返回的 settings 再加一道空值/类型守卫，绝不抛。
  - 0 Tk 依赖：纯逻辑，便于 test_import_memory 单测。

键结构（settings.json 上）：
    "import_memory": {
        "mapping": {"name": "商品信息", "stock": "仓库总库存", ...},
        "saved_at": "2026-08-30T12:34:56"
    }
"""
from __future__ import annotations

import datetime as _dt
from typing import Iterable, Tuple

# 导入映射经此通道持久化，与 settings 原子写/锁/备份共用（utils.Config 已实现）
from utils import Config

# 期望字段集（hit_rate 分母）。region/warehouse 可选，不参与"是否仍可用"判定
# ——PDD 后台列名可能随版本变化，但 name/stock/sales 三核心字段是识别/补货计算
# 的硬要求；二者缺一即映射作废（缺字段 → 走默认重猜）。
_EXPECTED_FIELDS: Tuple[str, ...] = ('name', 'stock', 'sales')

# settings.json 内本模块的根节点键名（与 settings_ui / gui 配置面板约定保持一致）
_SETTINGS_KEY = 'import_memory'


def _now_iso() -> str:
    """本地时间 ISO 字符串（秒精度）。失败兜底 → ''（不影响映射本体写入）。"""
    try:
        return _dt.datetime.now().isoformat(timespec='seconds')
    except Exception:
        return ''


def _safe_get_memory_node(cfg: dict) -> dict:
    """从 Config.load 返回的 dict 中安全取出 import_memory 子树。

    类型/键校验失败 → 返回 {}（调用方按空节点处理，绝不抛）。
    """
    if not isinstance(cfg, dict):
        return {}
    node = cfg.get(_SETTINGS_KEY)
    if not isinstance(node, dict):
        return {}
    return node


def get_last_mapping() -> dict | None:
    """读上次导入映射。

    Returns:
        dict：{field: 列名, ...}，仅含非空字符串的键值对。
        None：未保存过 / 节点为空 / 损坏 / 字段全空。

    Raises:
        无（Config.load 自身已做损坏隔离；本函数只加类型守卫）。
    """
    try:
        cfg = Config.load()
    except Exception:
        return None
    node = _safe_get_memory_node(cfg)
    mapping = node.get('mapping')
    if not isinstance(mapping, dict):
        return None
    # 过滤非字符串 / 空字符串 / 纯空白字符串的键值对（损坏容忍）
    out = {}
    for k, v in mapping.items():
        if isinstance(k, str) and k and isinstance(v, str) and v.strip():
            out[k] = v
    if not out:
        return None
    return out


def save_last_mapping(mapping) -> bool:
    """写入上次导入映射。

    Args:
        mapping: {field: 列名, ...}。非 dict 或全空 → False（拒绝写入）。

    Returns:
        bool：True=写盘成功；False=拒绝/写盘失败。

    写入失败时本模块不抛（§4 失败哲学）；调用方按状态栏提示。
    """
    if not isinstance(mapping, dict):
        return False
    # 清洗输入：只留 非空字符串 字段（纯空白也丢弃：占位但无意义）
    clean = {}
    for k, v in mapping.items():
        if isinstance(k, str) and k and isinstance(v, str) and v.strip():
            clean[k] = v
    if not clean:
        return False

    try:
        cfg = Config.load()
    except Exception:
        return False
    if not isinstance(cfg, dict):
        cfg = {}

    # 构造节点：mapping + saved_at；不破坏同层其他键
    node = _safe_get_memory_node(cfg)
    node['mapping'] = clean
    node['saved_at'] = _now_iso()
    cfg[_SETTINGS_KEY] = node

    try:
        Config.save(cfg)
        return True
    except Exception:
        return False


def clear_last_mapping() -> bool:
    """清除上次导入映射。

    Returns:
        bool：True=节点已删除或本就不存在；False=写盘失败。

    不存在时返回 True（幂等）："清除一个空记忆"也是成功——避免 GUI 误判"清除失败"。
    """
    try:
        cfg = Config.load()
    except Exception:
        return False
    if not isinstance(cfg, dict):
        return True  # 视作空
    if _SETTINGS_KEY not in cfg:
        return True  # 已清
    # 移除本节点（不动其他键）
    try:
        del cfg[_SETTINGS_KEY]
    except Exception:
        return False
    try:
        Config.save(cfg)
        return True
    except Exception:
        return False


def _normalize(s) -> str:
    """列名归一化（复用 ocr.normalize_col_name，保证与 guess_mapping 行为一致）。

    ocr 内部已实现此函数；导入映射比对必须用同一份归一化规则，否则 PDD 列名
    含全角空格/半角空格时无法对齐（实测 "仓库总库存 " vs "仓库总库存" 不归一会误判）。
    """
    try:
        from ocr import normalize_col_name
        return normalize_col_name(s)
    except Exception:
        # 兜底：去前后空格 + 全角空格→半角（与 ocr.normalize_col_name 同语义最小集）
        if s is None:
            return ''
        return str(s).strip().replace('\u3000', ' ').replace(' ', '')


def last_mapping_matches(headers, mapping) -> Tuple[bool, float]:
    """判定 headers 是否仍命中 mapping（用于设置页「清除/恢复」决策）。

    Args:
        headers: 文件表头（list[str] / tuple[str] / 可迭代）。
        mapping: 候选 mapping（dict / None）。

    Returns:
        (match: bool, hit_rate: float)
        - match：核心字段（name/stock/sales）全部命中 → True；
                 任一缺失 → False（mapping 不可直接复用，须走 guess_mapping 重猜）。
        - hit_rate：命中字段数 / 期望字段数（3），范围 [0.0, 1.0]。
                     全部命中=1.0；缺一=2/3≈0.667；全缺=0.0。
                     mapping 非 dict → (False, 0.0)。

    Raises:
        无（headers 非可迭代 / 含 None → 按"未命中"处理）。
    """
    if not isinstance(mapping, dict) or not mapping:
        return (False, 0.0)

    # 1) headers 归一化集合（容忍 None / 非字符串）
    headers_norm: set = set()
    try:
        iter_headers = list(headers) if headers is not None else []
    except TypeError:
        return (False, 0.0)
    for h in iter_headers:
        if isinstance(h, str) and h:
            headers_norm.add(_normalize(h))

    if not headers_norm:
        return (False, 0.0)

    # 2) 逐字段判定（核心字段 + 可选字段独立累计；hit_rate 仅看核心）
    core_hit = 0
    for f in _EXPECTED_FIELDS:
        target = mapping.get(f)
        if isinstance(target, str) and target and _normalize(target) in headers_norm:
            core_hit += 1

    hit_rate = core_hit / len(_EXPECTED_FIELDS) if _EXPECTED_FIELDS else 0.0
    match = core_hit == len(_EXPECTED_FIELDS)
    return (match, round(hit_rate, 4))