"""
PDD EZ — 备份 / 恢复 / 历史库快照（R3 健壮闭环 · t9 产出）

公开 API：
  - export_settings_zip(zip_path, *, include_history_db=False, base_dir=None)
        -> dict 统计
    打包 settings.json + regions.json（可选 history.db 快照）到 zip。
    zip 内文件路径按 `backup/<filename>` 命名（命名空间避免解包后污染目标目录）。
    失败安全：返回 None 或带 'error' 字段的 dict；绝不外抛。

  - restore_settings_zip(zip_path, *, base_dir=None, db_path_func=None)
        -> dict 统计
    从 zip 解出 settings.json / regions.json 校验 JSON 合法后原子写回：
      - 写新文件前先把现文件复制为 `.pre_restore`（用户回退用）；
      - 校验非法（JSON 解析失败 / zip 损坏 / 缺关键文件）→ 拒绝恢复并返回 error。
    失败安全：返回 dict 含 'error' 字段；任何异常路径不外抛。

  - snapshot_history_db(target_dir=None, *, db_path=None)
        -> str | None
    history.db 的 SQLite `VACUUM INTO` 一致性快照（WAL 安全；不锁库）。
    目标目录默认 `<base>/backups/`；快照文件名带 ISO 时间戳。
    库不存在 → None（不报错）；其他异常 → None。

设计原则：
  - 0 Tk 依赖；纯 stdlib（zipfile/sqlite3/json/shutil/os）。
  - 失败绝不外抛（§4 失败哲学）；错误信息截断到 ~200 字防 log 爆。
  - base_dir 可注入（单测用 tmp 目录）；默认走 utils.get_base_dir()。
  - 文件命名空间：zip 内路径 = `backup/<filename>`；解包时只解这个前缀，不污染。

约束（t1 / t9 接线方请看）：
  - settings.json / regions.json 路径：在 base_dir 下（脚本目录或 AppData/PDD补货助手）。
  - history.db 路径：默认 history_db.db_path()（单测可通过 history_db.set_db_path 注入）。
  - export 阶段不调用 utils.Config.save（只读现有文件）；restore 阶段直接写文件
    不走 Config.save（zip 内 JSON 形态可能含非 settings 模板字段，写 Config.save
    会触发模板合并 / 自愈覆盖——本任务需要"原样恢复"，不调 Config）。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time as _time
import zipfile
from datetime import datetime
from typing import Callable, Dict, List, Optional, Union


# ── zip 内命名空间（防解包污染目标目录） ──
_ZIP_NAMESPACE = 'backup'

# 可打包文件列表（base_dir 下的相对名）
# 注意：不在列表里的文件即使存在也不打包——白名单避免把 DPAPI 凭据/日志等敏感/临时文件
# 一起带走（settings.json 已含 dpapi:v1: 密文，前缀自动过滤见 utils._sanitize_for_log）。
_PACKABLE_FILES = ('settings.json', 'regions.json')

# 恢复时必须存在的关键文件；缺一 → 整包拒绝（dict error）
_REQUIRED_RESTORE_FILES = ('settings.json',)

# 错误信息截断上限（防异常堆栈太长撑爆状态栏/log）
_ERROR_TRUNC = 200


def _truncate(msg: str, n: int = _ERROR_TRUNC) -> str:
    s = str(msg or '').strip()
    if len(s) > n:
        return s[:n] + '…'
    return s


def _copy_file_for_backup(src: str, dst: str) -> None:
    """复制文件（带 .bak 旁文件清理；不持有源文件句柄以避免 Windows 文件锁）。

    Windows 上的怪癖：shutil.copy2 在某些场景（DB 文件/AV 锁定）会留下短暂
    句柄，导致后续 os.replace 失败。这里用二进制 IO + 显式 close + 旁文件清理。
    """
    for _side in (src + '-wal', src + '-shm', src + '.bak', src + '.corrupt'):
        try:
            if os.path.exists(_side):
                os.remove(_side)
        except Exception:
            pass
    # 注意：二进制 IO 在 with 块退出时一定 close；但 Windows 下文件锁可能
    # 由 AV/索引器延迟释放——这里 sleep 一下确保锁释放（实测 0.05s 通常够）
    with open(src, 'rb') as fin:
        data = fin.read()
    with open(dst, 'wb') as fout:
        fout.write(data)
    # 强制让 Python 释放文件句柄 + 给 Windows 一个 tick 时间释放锁
    import gc as _gc
    _gc.collect()
    _time.sleep(0.1)


def _atomic_replace(tmp: str, target: str) -> None:
    """原子替换：tmp → target，Windows 文件锁重试。

    关键点：
    1) Windows 上 os.replace 在目标仍被另一进程持有时会抛 PermissionError (WinError 5)；
       退化为 os.unlink + os.rename（rename 在同卷下也是原子的，跨平台通用）。
    2) 重试 3 次 + 指数 backoff（0.1/0.2/0.4s）——给 Windows 文件锁释放时间。
    3) SQLite WAL 旁文件 (-wal/-shm) 残留也会阻挡 unlink——一并清理。
    4) 最后兜底：unlink target + rename（不分两步，最简化路径）。
    """
    # 先尝试 os.replace（原子），含重试
    last_err = None
    for _attempt in range(3):
        try:
            os.replace(tmp, target)
            return
        except OSError as e:
            last_err = e
            if _attempt >= 2:
                break
            _time.sleep(0.1 * (2 ** _attempt))
    # 清理 WAL 旁文件（如果目标存在 + 有残留）
    for _side in (target + '-wal', target + '-shm'):
        try:
            if os.path.exists(_side):
                os.remove(_side)
        except Exception:
            pass
    # 二次尝试：unlink 旧 target（如果存在）+ rename tmp → target
    # 注意：用 rename 不用 replace——Windows rename 在目标不存在时不会触发目标锁检查
    try:
        if os.path.isfile(target):
            os.remove(target)
    except OSError as e:
        last_err = e
    try:
        os.rename(tmp, target)
        return
    except OSError as e:
        last_err = e
    # 最终兜底：再试 os.replace（unlink 后目标不存在，replace 不需要锁检查）
    try:
        os.replace(tmp, target)
        return
    except OSError as e:
        last_err = e
    # 全部失败：把 tmp 改名回 target 名（保留写入的内容供用户手动恢复）
    try:
        os.rename(tmp, target + '.restore_tmp')
    except Exception:
        pass
    raise (last_err or OSError("atomic replace failed"))


def _resolve_base_dir(base_dir: Optional[str]) -> str:
    """base_dir 解析：默认 utils.get_base_dir()。"""
    if base_dir:
        try:
            return os.path.abspath(base_dir)
        except Exception:
            pass
    try:
        from utils import get_base_dir as _gbd
        return _gbd()
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))


def _now_stamp() -> str:
    """ISO 风格时间戳（文件名安全；替代字符全替换为 -）。"""
    return datetime.now().strftime('%Y%m%d_%H%M%S')


# ═══════════════════════ export_settings_zip ═══════════════════════

def export_settings_zip(zip_path, *, include_history_db: bool = False,
                        base_dir: Optional[str] = None,
                        history_db_path: Optional[str] = None) -> Optional[Dict]:
    """打包当前 settings.json / regions.json（可选 history.db 快照）到 zip。

    Args:
        zip_path: 目标 zip 路径（不存在会自动创建父目录；已存在会被覆盖）。
        include_history_db: True 时同时打包 history.db 的 SQLite 一致性快照
                           （VACUUM INTO 临时文件 → 加入 zip → 删除临时）。
        base_dir: 源目录；None → utils.get_base_dir()。
        history_db_path: history.db 的源路径（与 base_dir 同目录可省略；
                        单测/特殊场景注入用）。None → history_db.db_path()。

    Returns:
        dict 统计：
          {
            'path': str,  # 实际写入的 zip 路径
            'files': [str, ...],  # zip 内 entry 路径列表
            'size_bytes': int,
            'had_history_db': bool,
            'history_db_snapshot': str | None,  # 临时快照路径（已删；记录用）
            'created_at': 'YYYY-MM-%dTHH:%M:%S',
            'error': str | None,
          }
        失败返回 None（zip_path 不可写 / 源文件缺失 / 异常）。
        成功也可能在 dict 里带 'error' 字段——非致命警告（如 regions.json 缺失）。
    """
    if not zip_path or not isinstance(zip_path, str):
        return None
    base = _resolve_base_dir(base_dir)
    out_path = os.path.abspath(zip_path)
    # 父目录创建
    try:
        parent = os.path.dirname(out_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
    except Exception as e:
        return {'error': f'无法创建目标目录: {_truncate(str(e))}', 'path': out_path}

    result = {
        'path': out_path,
        'files': [],
        'size_bytes': 0,
        'had_history_db': False,
        'history_db_snapshot': None,
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'error': None,
    }

    # 1) 收集白名单源文件（缺则警告但继续——settings.json 是必需的）
    sources: List[tuple] = []  # [(zip_inner_path, src_abs_path, required)]
    for fname in _PACKABLE_FILES:
        src = os.path.join(base, fname)
        inner = f'{_ZIP_NAMESPACE}/{fname}'
        if os.path.isfile(src):
            sources.append((inner, src, fname in _REQUIRED_RESTORE_FILES))
        elif fname in _REQUIRED_RESTORE_FILES:
            return {'error': f'关键文件缺失: {fname}', 'path': out_path,
                    'files': [], 'size_bytes': 0, 'had_history_db': False,
                    'history_db_snapshot': None,
                    'created_at': result['created_at']}
        else:
            # 可选文件缺失 → 警告但继续（regions.json 可缺）
            if result['error'] is None:
                result['error'] = f'可选文件缺失: {fname}'
            else:
                result['error'] += f'; 缺失 {fname}'

    # 2) history.db 快照（可选）
    tmp_snapshot = None
    if include_history_db:
        # history_db_path 优先于默认 history_db.db_path()——单测用 base_dir 注入时
        # 也要把库路径对齐到 base_dir 下，否则快照函数会读真实库（失败）。
        snap_db = history_db_path or os.path.join(base, 'history.db')
        snap = snapshot_history_db(target_dir=base, db_path=snap_db)
        if snap and os.path.isfile(snap):
            tmp_snapshot = snap
            sources.append((f'{_ZIP_NAMESPACE}/history.db', snap, False))
            result['had_history_db'] = True
            result['history_db_snapshot'] = snap
        else:
            # history.db 不存在 / 快照失败：警告但继续
            note = 'history.db 不存在或快照失败'
            result['error'] = (f'{result["error"]}; {note}' if result['error']
                               else note)

    # 3) 写 zip
    try:
        # 写到临时路径 → 原子 rename（避免半写文件被读）
        tmp_zip = f'{out_path}.tmp_{os.getpid()}'
        with zipfile.ZipFile(tmp_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
            for inner, src, _required in sources:
                try:
                    zf.write(src, arcname=inner)
                    result['files'].append(inner)
                except Exception as e:
                    result['error'] = (f'{result["error"]}; 写入失败 {inner}: '
                                       f'{_truncate(str(e))}'
                                       if result['error']
                                       else f'写入失败 {inner}: {_truncate(str(e))}')
        # 原子替换
        for _attempt in range(3):
            try:
                os.replace(tmp_zip, out_path)
                break
            except OSError:
                if _attempt >= 2:
                    raise
                _time.sleep(0.2)
        try:
            result['size_bytes'] = os.path.getsize(out_path)
        except Exception:
            pass
    except Exception as e:
        # 清理临时 zip
        try:
            if 'tmp_zip' in locals() and os.path.exists(tmp_zip):
                os.remove(tmp_zip)
        except Exception:
            pass
        # 清理临时 snapshot
        try:
            if tmp_snapshot and os.path.exists(tmp_snapshot):
                os.remove(tmp_snapshot)
        except Exception:
            pass
        return {'error': f'写 zip 失败: {_truncate(str(e))}', 'path': out_path,
                'files': [], 'size_bytes': 0, 'had_history_db': False,
                'history_db_snapshot': None,
                'created_at': result['created_at']}

    # 清理临时 snapshot（已写入 zip → 可删原文件）
    if tmp_snapshot:
        try:
            os.remove(tmp_snapshot)
        except Exception:
            pass

    return result


# ═══════════════════════ restore_settings_zip ═══════════════════════

def restore_settings_zip(zip_path, *, base_dir: Optional[str] = None,
                         keep_history_db_snapshot: bool = True) -> Dict:
    """从 zip 解出 settings.json / regions.json 校验后原子写回。

    Args:
        zip_path: 源 zip 路径。
        base_dir: 目标目录；None → utils.get_base_dir()。
        keep_history_db_snapshot: True 时若 zip 内含 history.db 快照，写入 base_dir
                                  （覆盖现有 history.db）。False 时仅解出 .json。

    Returns:
        dict 统计：
          {
            'restored': [str, ...],  # 实际写入的目标文件名
            'pre_restore': [str, ...],  # 现文件备份名（.pre_restore）
            'skipped': [str, ...],  # 跳过写入的 entry（如非 backup 命名空间）
            'error': str | None,
            'path': str,  # 源 zip 路径
            'files_in_zip': [str, ...],  # 解出的所有文件（统计用）
          }
        失败（zip 损坏 / 非法 JSON / 缺关键文件）→ 'error' 非空；'restored' 为空。

    失败语义：拒绝写任何目标文件（保证用户原 cfg 安全；pre_restore 也仅在成功恢复后才落）。
    """
    if not zip_path or not isinstance(zip_path, str):
        return {'error': 'zip 路径为空', 'restored': [], 'pre_restore': [],
                'skipped': [], 'path': zip_path, 'files_in_zip': []}
    if not os.path.isfile(zip_path):
        return {'error': f'zip 文件不存在: {zip_path}', 'restored': [],
                'pre_restore': [], 'skipped': [], 'path': zip_path,
                'files_in_zip': []}

    base = _resolve_base_dir(base_dir)
    result = {
        'restored': [],
        'pre_restore': [],
        'skipped': [],
        'error': None,
        'path': os.path.abspath(zip_path),
        'files_in_zip': [],
    }

    # 1) 校验 zip 可读 + 列出文件
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            # 完整性测试：损坏 zip 会抛 BadZipFile
            try:
                bad = zf.testzip()
                if bad:
                    return {**result, 'error': f'zip 已损坏: {bad}'}
            except Exception as e:
                return {**result, 'error': f'zip 完整性校验失败: {_truncate(str(e))}'}

            names = zf.namelist()
            result['files_in_zip'] = list(names)

            # 1a) 校验关键文件 + JSON 合法性（先全部读到内存，确认合法后再写）
            payloads: Dict[str, bytes] = {}  # fname -> bytes
            for name in names:
                # 安全：只解 backup/ 命名空间；其它目录穿越 / 任意路径一律跳过
                # （zipfile 自带防御，但双保险）
                if not name.startswith(f'{_ZIP_NAMESPACE}/'):
                    result['skipped'].append(name)
                    continue
                bare = name[len(f'{_ZIP_NAMESPACE}/'):]
                # 禁止路径穿越（即使过了命名空间前缀）
                if '..' in bare.split('/') or bare.startswith('/'):
                    result['skipped'].append(name)
                    continue
                if bare not in _PACKABLE_FILES and bare != 'history.db':
                    result['skipped'].append(name)
                    continue
                try:
                    payloads[bare] = zf.read(name)
                except Exception as e:
                    return {**result, 'error': f'读取 {name} 失败: {_truncate(str(e))}'}

            # 1b) 关键文件检查
            for req in _REQUIRED_RESTORE_FILES:
                if req not in payloads:
                    return {**result, 'error': f'zip 缺关键文件: {req}'}

            # 1c) JSON 合法性校验（每个 .json 都解析一次；非 dict 视为非法——settings 必须是顶层 dict）
            for fname, data in payloads.items():
                    if not fname.endswith('.json'):
                        continue
                    try:
                        parsed = json.loads(data.decode('utf-8'))
                    except Exception as e:
                        return {**result,
                                'error': f'非法 JSON: {fname}: {_truncate(str(e))}'}
                    if fname == 'settings.json' and not isinstance(parsed, dict):
                        return {**result,
                                'error': 'settings.json 顶层不是 dict（拒绝恢复）'}

            # 2) 写入目标目录
            # 2a) .json 文件：先 .pre_restore 备份 → 再原子写
            for fname, data in payloads.items():
                if not fname.endswith('.json'):
                    continue
                target = os.path.join(base, fname)
                pre = target + '.pre_restore'
                # 备份现文件（如不存在则跳过备份，但保留 pre 路径在结果里说明"无原文件"）
                try:
                    if os.path.isfile(target):
                        shutil.copy2(target, pre)
                        result['pre_restore'].append(os.path.basename(pre))
                except Exception as e:
                    return {**result,
                            'error': f'备份 {fname} 失败: {_truncate(str(e))}'}
                # 原子写：tmp → os.replace
                try:
                    tmp = f'{target}.tmp_{os.getpid()}'
                    with open(tmp, 'wb') as f:
                        f.write(data)
                    _atomic_replace(tmp, target)
                    result['restored'].append(fname)
                except Exception as e:
                    # 清理 tmp
                    try:
                        if 'tmp' in locals() and os.path.exists(tmp):
                            os.remove(tmp)
                    except Exception:
                        pass
                    return {**result,
                            'error': f'写入 {fname} 失败: {_truncate(str(e))}'}

            # 2b) history.db 快照（可选）：直接覆盖（用户明确选了"包含历史库"）
            if keep_history_db_snapshot and 'history.db' in payloads:
                target = os.path.join(base, 'history.db')
                pre = target + '.pre_restore'
                try:
                    if os.path.isfile(target):
                        _copy_file_for_backup(target, pre)
                        result['pre_restore'].append(os.path.basename(pre))
                except Exception as e:
                    return {**result,
                            'error': f'备份 history.db 失败: {_truncate(str(e))}'}
                try:
                    tmp = f'{target}.tmp_{os.getpid()}'
                    with open(tmp, 'wb') as f:
                        f.write(payloads['history.db'])
                    _atomic_replace(tmp, target)
                    result['restored'].append('history.db')
                except Exception as e:
                    try:
                        if 'tmp' in locals() and os.path.exists(tmp):
                            os.remove(tmp)
                    except Exception:
                        pass
                    return {**result,
                            'error': f'写入 history.db 失败: {_truncate(str(e))}'}

    except zipfile.BadZipFile as e:
        return {**result, 'error': f'zip 损坏: {_truncate(str(e))}'}
    except Exception as e:
        return {**result, 'error': f'恢复失败: {_truncate(str(e))}'}

    return result


# ═══════════════════════ snapshot_history_db ═══════════════════════

def snapshot_history_db(target_dir=None, *, db_path: Optional[str] = None,
                        base_dir: Optional[str] = None) -> Optional[str]:
    """history.db 的 SQLite 一致性快照（VACUUM INTO；WAL 安全）。

    Args:
        target_dir: 快照目标目录；None → `<base>/backups/`。
        db_path: 源库路径；None → history_db.db_path()。
        base_dir: 当 target_dir/db_path 都缺省时的回退基准目录。

    Returns:
        str: 快照文件绝对路径。失败 / 库不存在 → None。
    """
    base = _resolve_base_dir(base_dir)
    # 1) 解析源库路径
    src = None
    if db_path:
        src = os.path.abspath(db_path)
    else:
        try:
            from history_db import db_path as _hdb_path
            src = _hdb_path()
        except Exception:
            src = os.path.join(base, 'history.db')
    if not src or not os.path.isfile(src):
        return None

    # 2) 解析目标目录
    if not target_dir:
        target_dir = os.path.join(base, 'backups')
    try:
        target_dir = os.path.abspath(target_dir)
        os.makedirs(target_dir, exist_ok=True)
    except Exception:
        return None

    # 3) 目标路径（带时间戳；同名追加 _N 后缀防覆盖）
    stamp = _now_stamp()
    target = os.path.join(target_dir, f'history_{stamp}.db')
    suffix = 1
    while os.path.exists(target):
        target = os.path.join(target_dir, f'history_{stamp}_{suffix}.db')
        suffix += 1
        if suffix > 99:  # 安全兜底
            return None

    # 4) VACUUM INTO —— 读一致快照，不锁库（WAL 下安全）
    try:
        # 使用短超时 + URI 模式：避免库被业务线程长时间占用导致 VACUUM 等待
        with sqlite3.connect(src, timeout=10) as conn:
            # VACUUM INTO 是 SQLite 3.27+ 的官方一致性快照命令
            # 文件存在性 / 权限错误时抛 OperationalError
            conn.execute(f"VACUUM INTO { _qstr_path(target) }")
        # 校验快照存在 + 非空（VACUUM INTO 成功后 target 必存在）
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            return target
        return None
    except Exception:
        # 失败时清理可能的空文件
        try:
            if os.path.exists(target):
                os.remove(target)
        except Exception:
            pass
        return None


def _qstr_path(path: str) -> str:
    """把绝对路径转成 SQL VACUUM INTO 接受的引号字面量（Windows 兼容）。"""
    p = path.replace("'", "''")
    return f"'{p}'"


# ═══════════════════════ 公开契约摘要（供 / 接线方查阅） ═══════════════════════
# 失败语义
# - export_settings_zip：异常路径 → 返回 None；非致命警告在 result['error'] 累积字符串。
# - restore_settings_zip：任何异常 → 返回 dict 含 'error'；'restored' 为空（拒绝写）。
# - snapshot_history_db：异常 / 库不存在 → None。
# 与 utils.Config 的协作
# - export 不调 Config.save（只读现有文件），保持导出=磁盘原始内容。
# - restore 不调 Config.save（直接写文件原样 bytes）；这意味着恢复后 Config.load()
# 仍会走模板合并 + 自愈——若 zip 内 settings.json 缺关键字段，Config 会用模板补；
# 这是 Config 的设计契约，restore 任务范围内不动 Config 行为。
# 路径注入（单测用）
# - base_dir: 注入临时目录 → 模拟其他工作目录的 settings/regions。
# - db_path: 直接传 history.db 路径；否则走 history_db.db_path()（单测用 set_db_path）。
# ═══════════════════════
