"""
PDD EZ v1.6.0 — 启动健康检查（TC-C2 / WS-C2，docs/PLAN_v160.md §5.1）

聚合六项启动检查 → `startup_health() -> list[HealthItem]`：
  config   配置可读性（utils.Config.load）
  db       历史库 quick_check（只读 PRAGMA，绝不写库、不触发建表）
  model    模型配置（active provider 的 model/api_key，含 DPAPI 解密后判定）
  license  授权状态（enforce=false 默认全免 → GREEN；启用时验签+过期检查）
  updater  更新器残留（.old_upd/.old 残留文件 + tempdir 进度文件孤儿）
  updater_pending 更新健康状态（TC-C3，读 update_health.json——只读，updater 独家写）

设计纪律（§5 TC-C2 卡 + 宪法 §4）：
- **纯函数、零 Tk 依赖**——GUI 侧经 `win.after` 异步消费，本模块可在无显示环境单测；
- **绝不抛异常**——单项检查内部异常降级为该检查的 RED/YELLOW + detail 说明，
  聚合层永远拿到完整 5 项（失败显式落徽章，绝不静默 `except: pass`）；
- **只读**——db 检查用 sqlite URI mode=ro，文件不存在按"首次运行"YELLOW 处理，
  绝不创建/迁移历史库（那是 history_db._ensure_ready 的职责）；
- **可注入**——`startup_health(**overrides)` 每项检查可传替身函数，便于单测；
- 单次调用目标 ≤200ms（quick_check 有 _READY 类缓存语义之外的实测开销，
  这里每次启动只跑一次，量级可控）。

常量对齐声明：`.old_upd` / `.old` / `pdd_upd_progress.json` 三个名字与
updater.py:462 / :613 / :35 一致（updater 源为唯一事实源，此处仅探测不改名）。
"""
from __future__ import annotations

import glob as _glob
import os
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable, List, Optional

__all__ = ['HealthItem', 'startup_health', 'run_all_checks']

CHECK_ITEMS = ('config', 'db', 'model', 'license', 'updater', 'updater_pending')
LEVELS = ('GREEN', 'YELLOW', 'RED')


@dataclass
class HealthItem:
    """单项健康结论。item ∈ CHECK_ITEMS；level ∈ LEVELS；detail 为一句话中文说明。"""
    item: str
    level: str
    detail: str

    def as_dict(self) -> dict:
        return {'item': self.item, 'level': self.level, 'detail': self.detail}


def _item(item: str, level: str, detail: str, started: float) -> HealthItem:
    """统一出口：detail 追加耗时（ms），任何路径都不抛。"""
    cost_ms = int((time.monotonic() - started) * 1000)
    if level not in LEVELS:  # 防御：替身返回非法 level → 显式 RED，不静默
        level = 'RED'
        detail = f'检查项返回非法 level({level!r})：{detail}'
    return HealthItem(item=item, level=level, detail=f'{detail}（{cost_ms}ms）')


# ============================================================
# config —— 配置可读性
# ============================================================

def check_config(config_loader: Optional[Callable[[], object]] = None) -> HealthItem:
    """utils.Config.load() 可读且为映射 → GREEN；空配置 → YELLOW；异常 → RED。"""
    started = time.monotonic()
    loader = config_loader or _default_config_loader
    try:
        cfg = loader()
    except Exception as e:
        return _item('config', 'RED', f'配置读取失败：{str(e)[:80]}', started)
    if isinstance(cfg, dict) and cfg:
        return _item('config', 'GREEN', '配置可读', started)
    if isinstance(cfg, dict):
        return _item('config', 'YELLOW', '配置为空（将使用默认值）', started)
    return _item('config', 'YELLOW', f'配置类型异常：{type(cfg).__name__}', started)


def _default_config_loader():
    from utils import Config
    return Config.load()


# ============================================================
# db —— 历史库 quick_check（只读）
# ============================================================

def check_db(db_path: Optional[str] = None, prober: Optional[Callable[[str], str]] = None) -> HealthItem:
    """quick_check=ok → GREEN；库不存在 → YELLOW（首次运行，写路径会自建）；异常 → RED。

    prober(db_path) -> 'ok' | 'corrupt' | 'missing'（注入替身用）；默认真连只读 PRAGMA。
    """
    started = time.monotonic()
    path = db_path
    if not path:
        try:
            import history_db as _hdb
            path = _hdb.db_path()
        except Exception as e:
            return _item('db', 'YELLOW', f'历史库路径解析失败（首用时会重试）：{str(e)[:60]}', started)
    probe = prober or _default_db_probe
    try:
        verdict = probe(path)
    except Exception as e:
        return _item('db', 'RED', f'历史库探测异常：{str(e)[:80]}', started)
    if verdict == 'ok':
        return _item('db', 'GREEN', '历史库 quick_check 通过', started)
    if verdict == 'missing':
        return _item('db', 'YELLOW', '历史库尚未创建（首次运行属正常，首次写入时自动建库）', started)
    return _item('db', 'RED', '历史库 quick_check 未通过（损坏？首次写入将自动隔离重建）', started)


def _default_db_probe(path: str) -> str:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return 'missing'
    from pathlib import Path as _P
    uri = 'file:' + urllib.parse.quote(_P(path).as_posix()) + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    try:
        row = conn.execute('PRAGMA quick_check').fetchone()
        ok = bool(row) and str(row[0]).lower() == 'ok'
        return 'ok' if ok else 'corrupt'
    finally:
        conn.close()


# ============================================================
# model —— 模型配置（active provider 的 model / api_key）
# ============================================================

def check_model(api_cfg: Optional[dict] = None) -> HealthItem:
    """model+key 齐备 → GREEN；缺 model 或 key → YELLOW（识别入口会显式拦截，见 v1.5.9 预检）。"""
    started = time.monotonic()
    try:
        cfg = api_cfg if api_cfg is not None else _default_api_cfg()
    except Exception as e:
        return _item('model', 'YELLOW', f'模型配置读取失败：{str(e)[:80]}', started)
    if not isinstance(cfg, dict):
        return _item('model', 'YELLOW', '模型配置形状异常', started)
    active = str(cfg.get('active_provider', '') or '')
    providers = cfg.get('providers') or {}
    provider = providers.get(active) if isinstance(providers, dict) else None
    provider = provider if isinstance(provider, dict) else {}
    model = str(provider.get('model', '') or provider.get('custom_endpoint', '') or '')
    key = str(provider.get('api_key', '') or '')
    if not model:
        return _item('model', 'YELLOW', '未配置识别模型（设置 → API 管理）', started)
    if not key:
        return _item('model', 'YELLOW', '未配置 API Key（设置 → API 管理）', started)
    try:  # DPAPI 密文解密后判定（与 ocr._ocr_api_call 同款语义；失败=空 key）
        from utils import decrypt_secret
        key = decrypt_secret(key) or ''
    except Exception:
        key = key  # 解密路径异常按原文非空对待（明文 key 直通）
    if not key:
        return _item('model', 'YELLOW', 'API Key 解密后为空（设置 → API 管理）', started)
    return _item('model', 'GREEN', f'模型已配置（{active}）', started)


def _default_api_cfg() -> dict:
    from utils import get_api_config
    return get_api_config()


# ============================================================
# license —— 授权状态
# ============================================================

def check_license(license_cfg: Optional[dict] = None) -> HealthItem:
    """enforce=false（默认全免）→ GREEN；启用且卡密有效未过期 → GREEN；无效/过期 → YELLOW。"""
    started = time.monotonic()
    try:
        cfg = license_cfg if license_cfg is not None else _default_license_cfg()
    except Exception as e:
        return _item('license', 'YELLOW', f'授权配置读取失败：{str(e)[:80]}', started)
    cfg = cfg if isinstance(cfg, dict) else {}
    if not bool(cfg.get('enforce', False)):
        return _item('license', 'GREEN', '授权门控未启用（全免）', started)
    key = str(cfg.get('key', '') or '')
    try:
        from auth import license as _lic
        info = _lic.verify_license(key)
    except Exception as e:
        return _item('license', 'YELLOW', f'授权验签异常：{str(e)[:80]}', started)
    if not isinstance(info, dict):
        return _item('license', 'YELLOW', '卡密无效（设置 → 授权）', started)
    expire_raw = str(info.get('expire_at', '') or '')
    if expire_raw:
        try:
            from datetime import datetime as _dt
            if _dt.fromisoformat(expire_raw[:19]) < _dt.now():
                return _item('license', 'YELLOW', f'卡密已过期（{expire_raw[:10]}）', started)
        except ValueError:
            pass  # 过期字段格式异常不阻塞（验签已通过）
    tier = str(info.get('tier', '') or 'free')
    return _item('license', 'GREEN', f'卡密有效（{tier}）', started)


def _default_license_cfg() -> dict:
    cfg = _default_config_loader()
    node = cfg.get('license') if isinstance(cfg, dict) else None
    return node if isinstance(node, dict) else {}


# ============================================================
# updater —— 更新器残留
# ============================================================

def _updater_leftovers(base_dir: str = None) -> tuple:
    """探测更新残留：程序目录 .old_upd/.old 文件 + tempdir 进度文件孤儿。

    常量与 updater.py:462(.old_upd)/:613(.old)/:35(PROGRESS_FILE) 对齐；只探测、不动文件。
    Returns: (残留文件名列表, 进度文件路径或 '')
    """
    import tempfile
    base = base_dir
    if not base:
        try:
            from utils import get_base_dir
            base = get_base_dir()
        except Exception:
            base = os.path.dirname(os.path.abspath(__file__))
    leftovers: List[str] = []
    try:
        for pattern in ('*.old_upd', '*.old'):
            leftovers.extend(os.path.basename(p) for p in _glob.glob(os.path.join(base, pattern)))
    except Exception:
        pass
    progress = os.path.join(tempfile.gettempdir(), 'pdd_upd_progress.json')
    return leftovers, (progress if os.path.exists(progress) else '')


def check_updater(base_dir: str = None, prober: Optional[Callable[[str], tuple]] = None) -> HealthItem:
    """无残留 → GREEN；有 .old_upd/.old 或进度孤儿 → YELLOW（下次更新会清理/覆盖）。"""
    started = time.monotonic()
    probe = prober or _updater_leftovers
    try:
        leftovers, progress = probe(base_dir if base_dir else '')
    except Exception as e:
        return _item('updater', 'YELLOW', f'更新残留探测异常：{str(e)[:80]}', started)
    msgs = []
    if leftovers:
        msgs.append(f'{len(leftovers)} 个 .old_upd/.old 残留')
    if progress:
        msgs.append('tempdir 进度文件孤儿')
    if msgs:
        return _item('updater', 'YELLOW', '发现更新残留（' + '；'.join(msgs) + '）', started)
    return _item('updater', 'GREEN', '无更新残留', started)


# ============================================================
# updater_pending —— 更新健康状态（TC-C3，读 update_health.json）
# ============================================================

def _read_update_health_stub(target_dir: str) -> dict:  # pragma: no cover - 替身锚点
    raise NotImplementedError


def check_updater_pending(base_dir: str = None,
                          reader: Optional[Callable[[str], dict]] = None) -> HealthItem:
    """update_health.json 状态 → 健康等级（TC-C3；文件由 updater 独家写入，此处只读）。

    - 缺失/损坏 → GREEN（从未更新或无待验证更新，静默不扰）；
    - state='ok' → GREEN；'pending' → YELLOW（新版本待首帧自证）；
    - 'rolled_back' → YELLOW（已自动回滚，提示用户留意）；
    - 'rollback_failed' → RED（自动回滚失败——需人工恢复，§4 显式失败）。
    """
    started = time.monotonic()
    read = reader or _default_update_health_reader
    try:
        st = read(base_dir if base_dir else '')
    except Exception as e:
        return _item('updater_pending', 'YELLOW',
                     f'更新健康状态读取异常：{str(e)[:80]}', started)
    if not isinstance(st, dict) or not st:
        return _item('updater_pending', 'GREEN', '无待验证更新', started)
    state = str(st.get('state') or '')
    nv, ov = str(st.get('new_ver') or ''), str(st.get('old_ver') or '')
    ver = f'（{ov} → {nv}）' if (nv or ov) else ''
    if state == 'pending':
        return _item('updater_pending', 'YELLOW',
                     f'新版本待自证可用{ver}，首帧渲染成功后自动确认', started)
    if state == 'ok':
        return _item('updater_pending', 'GREEN', f'更新已验证{ver}', started)
    if state == 'rolled_back':
        return _item('updater_pending', 'YELLOW',
                     f'已自动回滚到更新前版本{ver}，请留意功能状态', started)
    if state == 'rollback_failed':
        return _item('updater_pending', 'RED',
                     f'自动回滚失败{ver}——请人工恢复程序目录（详见 logs）', started)
    return _item('updater_pending', 'YELLOW', f'更新健康状态未知：{state!r}', started)


def _default_update_health_reader(target_dir: str) -> dict:
    import json as _json
    import os as _os
    if not target_dir:
        try:
            import sys as _sys
            if getattr(_sys, 'frozen', False):
                target_dir = _os.path.dirname(_os.path.abspath(_sys.executable))
            else:
                target_dir = _os.path.dirname(_os.path.abspath(__file__))
        except Exception:
            target_dir = ''
    sf = _os.path.join(target_dir, 'update_health.json')
    try:
        with open(sf, 'r', encoding='utf-8') as f:
            data = _json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ============================================================
# 聚合入口
# ============================================================

_CHECK_RUNNERS = {
    'config': check_config,
    'db': check_db,
    'model': check_model,
    'license': check_license,
    'updater': check_updater,
    'updater_pending': check_updater_pending,
}


def run_all_checks(overrides: Optional[dict] = None) -> List[HealthItem]:
    """按 CHECK_ITEMS 顺序逐项执行（单线程序即可，总耗时受每项自身控制）。

    overrides: {'config': callable, 'db': (db_path, prober) 或 callable, ...} 形状自由：
    值为 callable 时直接替换该项检查函数；db/updater 也可传 tuple 位置参数。
    单项 runner 抛异常（替身实现 bug 等）→ 兜底 RED 项，聚合层绝不抛。
    """
    overrides = overrides or {}
    out: List[HealthItem] = []
    for name in CHECK_ITEMS:
        runner = _CHECK_RUNNERS[name]
        ov = overrides.get(name)
        try:
            if ov is None:
                result = runner()
            elif isinstance(ov, tuple):
                result = runner(*ov)
            elif isinstance(ov, HealthItem):
                result = ov
            else:
                result = ov()  # callable 替身：其返回值同样过下方 level 校验
            if not isinstance(result, HealthItem):
                raise TypeError(f'检查项返回类型异常：{type(result).__name__}')
            if result.level not in LEVELS:  # 白名单防御：非法 level → RED（§4 显式失败）
                result = HealthItem(item=name, level='RED',
                                    detail=f'检查项返回非法 level({result.level!r})：{result.detail}')
            out.append(result)
        except Exception as e:  # 替身/实现层异常 → 显式 RED（§4）
            out.append(HealthItem(item=name, level='RED',
                                  detail=f'检查执行异常：{type(e).__name__}: {str(e)[:60]}'))
    return out


def startup_health(overrides: Optional[dict] = None) -> List[HealthItem]:
    """TC-C2/TC-C3 对外契约：返回恰好 6 项（顺序 = CHECK_ITEMS），绝不抛异常。"""
    return run_all_checks(overrides)


if __name__ == '__main__':  # 手动体检入口：python health.py
    for it in startup_health():
        print(f'[{it.level:<6}] {it.item:<8} {it.detail}')
