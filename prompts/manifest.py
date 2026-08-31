"""Prompt manifest 与加载逻辑（TC-Q5）。

PROMPTS 字典的 key 命名与 docs/PLAN_v160.md §5.1 TC-Q5 一致：
- table_v1：表格识别（ocr.py:1244 全列 + :1222 指定列变体）
- locate_v1：UI 元素定位（vision.py:473 + :786）
- ocr_v1：OCR 行切分/复核（ocr.py:1352 + :1547 + :1557）
- status_v1：状态机/读数（vision.py:559 + :605 + :613 + :644 + :686）

每个文件路径相对 prompts 包根目录（运行时不依赖 cwd）。

文件格式约定：
- 单变体（如 locate_v1 / status_v1 / table_v1.full）：纯文本，`.format(**kwargs)` 替换占位符。
- 多变体（如 table_v1.cols / ocr_v1.verify / ocr_v1.split_cols / ocr_v1.split_full）：
  同文件含多个变体，用 sentinel `<<<VARIANT: name>>>` 分隔；load_prompt_variant(key, variant)
  返回指定变体；load_prompt(key, variant='full' or default) 等价于 load_prompt_variant。

`<<<VARIANT: name>>>` 行本身不出现在输出中；变体名见各 .txt 文件头注释。
"""
from __future__ import annotations

import os
from typing import Dict, List, Tuple

# ─── 版本常量 ─────────────────────────────────────────────────────────────
# 与 utils.VERSION 同号；变更时同步 utils.py:7 与 CHANGELOG.md（v1.6.0）
PROMPT_VERSION = 'v160'

# Sentinel：多变体 .txt 文件的变体分隔标记（行首）
_VARIANT_SENTINEL = '<<<VARIANT:'
_VARIANT_SENTINEL_END = '>>>'


# ─── Manifest：key → 文件名（相对 prompts/ 目录）─────────────────────────
# 锁顺序：与 PLAN §5.1 TC-Q5 一致；新增 key 时追加，删除时归档（保留至少 1 个版本）
PROMPTS: Dict[str, str] = {
    'table_v1':   'table_v1.txt',    # ocr.py:1244 全列（main）+ :1222 指定列（cols 变体）
    'locate_v1':  'locate_v1.txt',   # vision.py:473 + :786
    'ocr_v1':     'ocr_v1.txt',      # ocr.py:1352（verify）+ :1547（split_cols）+ :1557（split_full）
    'status_v1':  'status_v1.txt',   # vision.py:559 + :605 + :613 + :644 + :686
}


def _prompts_dir() -> str:
    """prompts/ 目录的绝对路径（兼容 PyInstaller frozen 打包）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return here


# ─── mtime 缓存（避免每次 OCR 调用都 stat + 读盘）───────────────────────
# key: prompt key → (mtime, full_text)；mtime 变化（dev 编辑 / 打包后）即刷新
_CACHE: Dict[str, Tuple[float, str]] = {}


def _read_file(key: str) -> str:
    """读 .txt 全文（带 mtime 缓存）。

    末尾空白行（trailing newlines）会被剥除——保证与历史内联字符串逐字节一致。
    """
    if key not in PROMPTS:
        raise KeyError(f'prompt key 不在 manifest 中: {key!r}（可用: {list(PROMPTS.keys())}）')
    fpath = os.path.join(_prompts_dir(), PROMPTS[key])
    try:
        mtime = os.path.getmtime(fpath)
    except OSError as e:
        raise FileNotFoundError(f'prompt 文件缺失: {fpath}（{e}）') from e
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    with open(fpath, 'r', encoding='utf-8') as f:
        text = f.read()
    # 标准化：剥除全部尾部空白行（保持与历史内联字符串一致——内联
    # 字符串尾随单换行由 triple-quoted 写法隐式提供；文件多出来的换行
    # 是 git/editor 加的，不影响语义但破坏逐字节 diff）
    _CACHE[key] = (mtime, text.rstrip('\n'))
    return _CACHE[key][1]


def load_prompt(key: str, variant: str = 'full') -> str:
    """读 prompt 原文；带 mtime 缓存。

    Args:
        key: manifest 中的 key（见 PROMPTS 字典）。
        variant: 变体名；单变体文件忽略此参数；多变体文件必填：
            - table_v1：'full'（:1244 主路径）或 'cols'（:1222 指定列变体）
            - ocr_v1：'full'（:1352）、'split_cols'（:1547）、'split_full'（:1557）
            - locate_v1：'full'（:473）、'table'（:786）
            - status_v1：'full'（:559）、'province_closeup'（:605）、'province_fullpage'（:613）、
              'page_state'（:644）、'anomaly'（:686）
            （locate_v1 与 status_v1 历史兼容：'full' 等价于 'read_total_count' 等同语义名）

    Returns:
        prompt 原文（str）。

    Raises:
        KeyError: key 不在 manifest 中，或 variant 不在该文件中（§4 显式失败；不静默回退）。
        FileNotFoundError: manifest 列出的 .txt 文件缺失（打包漏带 datas / 文件被误删）。
    """
    full = _read_file(key)
    # 单变体文件（无 sentinel）→ 直接返回全文
    if _VARIANT_SENTINEL not in full:
        if variant != 'full':
            raise KeyError(f'prompt {key!r} 是单变体文件，不支持 variant={variant!r}')
        return full
    # 多变体文件 → 按 sentinel 切片（首段名以首个 sentinel 为准）
    parts: Dict[str, str] = {}
    current_name: str | None = None
    current_buf: List[str] = []
    for line in full.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith(_VARIANT_SENTINEL) and stripped.endswith(_VARIANT_SENTINEL_END):
            # 切段：保存当前段（剥除尾部换行——保持与历史内联字符串一致）
            if current_name is not None:
                parts[current_name] = ''.join(current_buf).rstrip('\n')
            # 提取下一段名
            current_name = stripped[len(_VARIANT_SENTINEL):-len(_VARIANT_SENTINEL_END)].strip()
            current_buf = []
        else:
            current_buf.append(line)
    # 收尾段
    if current_name is not None:
        parts[current_name] = ''.join(current_buf).rstrip('\n')
    if variant not in parts:
        raise KeyError(f'prompt {key!r} 变体 {variant!r} 不存在（可用: {list(parts.keys())}）')
    return parts[variant]


def prompt_version() -> str:
    """返回 prompt 版本号（'v160'）——贯通 Run ID 与 Golden 评估。"""
    return PROMPT_VERSION


def list_prompts() -> List[str]:
    """返回 manifest 中的全部 key（测试与诊断用）。"""
    return list(PROMPTS.keys())


def list_variants(key: str) -> List[str]:
    """返回某 prompt 的全部变体名（测试用）。"""
    full = _read_file(key)
    if _VARIANT_SENTINEL not in full:
        return ['full']
    names: List[str] = []
    for line in full.splitlines():
        stripped = line.strip()
        if stripped.startswith(_VARIANT_SENTINEL) and stripped.endswith(_VARIANT_SENTINEL_END):
            names.append(stripped[len(_VARIANT_SENTINEL):-len(_VARIANT_SENTINEL_END)].strip())
    return names


def _cache_info() -> dict:
    """返回当前缓存状态（测试用：key → (mtime, byte_len)）。"""
    return {k: (v[0], len(v[1])) for k, v in _CACHE.items()}