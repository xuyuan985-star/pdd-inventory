"""
PDD EZ v1.6.0 — Run ID 诊断系统（TC-Q4 / docs/PLAN_v160.md §5.2，相位 1）

一轮批量识别 = 一个 Run。本模块提供：
  - RunContext dataclass：run_id / app_version / 模型 / prompt 版本 / 行计数 /
    tokens / cost / error 的一次性快照；
  - new_run(model_main, model_secondary) -> RunContext：工厂（生成 run_id）；
  - finish_run(ctx, **updates) -> bool：回填统计并把 run 终态写成
    usage_log.jsonl 的 event='run_end' 行；
  - run_events(run_id) -> list[dict]：按 run_id 检索 run_end 事件（诊断/TC-Q3 报告用）。

铁律：
  - **单一存储**：run_end 行写进 usage_log.jsonl（event='run_end'），写盘复用
    usage_store 的 _RECORD_LOCK 与 _log_path() —— 禁止第三存储（卡面红线）；
  - usage.enabled=False → finish_run 不写盘（与 usage_store.record 同开关语义）；
  - 全程 try/except 吞异常（宪法 §4）：诊断落账失败绝不影响识别主流程；
  - 相位 2 才接线 ocr/vision/gui/history_db/stats_ui。
"""
from __future__ import annotations

import re
import os as _os
import datetime
from dataclasses import dataclass, fields
from typing import Optional

__all__ = ['RunContext', 'new_run', 'finish_run', 'run_events', 'recent_runs',
           'set_current_run', 'clear_current_run', 'current_run_id',
           'RUN_ID_PATTERN']

RUN_ID_PATTERN = re.compile(r'^RUN-\d{8}-\d{6}-[0-9A-Fa-f]{4}$')

# 当前活动 run（相位 2 接线：gui 入口 set，ocr/vision 落账取）。
# 模块级单槽：GUI 一次只有一个活动识别入口（批量/图片批量互斥守卫）；
# live/import 与批量理论上可交叠——取"最近触发者"，诊断语义可接受（卡面相位 2）。
_CURRENT = {'ctx': None}


def set_current_run(ctx) -> None:
    """标记当前活动 run（gui 入口调用）；None=清除。"""
    _CURRENT['ctx'] = ctx if isinstance(ctx, RunContext) else None


def clear_current_run() -> None:
    _CURRENT['ctx'] = None


def current_run() -> Optional[RunContext]:
    return _CURRENT.get('ctx')


def current_run_id() -> str:
    """ocr/vision 落账取当前 run_id（无活动 run → ''，行保持 v1 形状）。"""
    ctx = _CURRENT.get('ctx')
    return str(ctx.run_id) if isinstance(ctx, RunContext) else ''


def _app_version() -> str:
    try:
        from utils import VERSION
        return str(VERSION or '')
    except Exception:
        return ''


def _prompt_version() -> str:
    try:
        from prompts.manifest import prompt_version
        return str(prompt_version() or '')
    except Exception:
        return ''


def _now_iso() -> str:
    try:
        return datetime.datetime.now().astimezone().isoformat(timespec='milliseconds')
    except Exception:
        return ''


@dataclass
class RunContext:
    """一次识别运行的诊断快照（TC-Q4）。

    run_id 形如 'RUN-YYYYMMDD-HHMMSS-XXXX'（本地时间 + 4 位随机十六进制防撞）。
    """
    run_id: str
    app_version: str
    model_main: str
    model_secondary: str
    prompt_version: str
    started_at: str
    duration_ms: int = 0
    rows_total: int = 0
    rows_ok: int = 0
    rows_low: int = 0
    rows_error: int = 0
    tokens: int = 0
    cost_cny: float = 0.0
    error: str = ''

    def as_event_line(self) -> dict:
        """run_end 事件行（写进 usage_log.jsonl 的 dict 形状）。"""
        return {
            'event': 'run_end',
            'run_id': self.run_id,
            'app_version': self.app_version,
            'model_main': self.model_main,
            'model_secondary': self.model_secondary,
            'prompt_version': self.prompt_version,
            'started_at': self.started_at,
            'duration_ms': int(self.duration_ms),
            'rows_total': int(self.rows_total),
            'rows_ok': int(self.rows_ok),
            'rows_low': int(self.rows_low),
            'rows_error': int(self.rows_error),
            'tokens': int(self.tokens),
            'cost_cny': float(self.cost_cny or 0.0),
            'error': str(self.error or ''),
        }


def new_run(model_main: str = '', model_secondary: str = '') -> RunContext:
    """工厂：生成 run_id 并填充版本上下文。绝不抛（版本解析失败按空串）。"""
    now = datetime.datetime.now()
    token = f"{int.from_bytes(_os.urandom(2), 'big'):04X}"
    return RunContext(
        run_id=f"RUN-{now:%Y%m%d-%H%M%S}-{token}",
        app_version=_app_version(),
        model_main=str(model_main or ''),
        model_secondary=str(model_secondary or ''),
        prompt_version=_prompt_version(),
        started_at=_now_iso(),
    )


def finish_run(ctx: RunContext, **updates) -> bool:
    """回填统计并落 run_end 事件行。返回 True=已写盘。

    updates 仅接受 RunContext 已有字段（未知键忽略——防调用方拼错字段名静默
    制造脏数据）；duration_ms 未显式给出时按 started_at→now 自动计算。
    写盘走 usage_store._RECORD_LOCK + usage_store._log_path()（单一存储红线）；
    usage.enabled=False → 不写盘返回 False（与 record 同开关）。
    """
    if not isinstance(ctx, RunContext):
        return False
    try:
        known = {f.name for f in fields(RunContext)} - {'run_id', 'started_at'}
        for k, v in (updates or {}).items():
            if k in known:
                setattr(ctx, k, v)
        if 'duration_ms' not in (updates or {}):
            try:
                t0 = datetime.datetime.fromisoformat(str(ctx.started_at))
                ctx.duration_ms = max(0, int((datetime.datetime.now(
                    t0.tzinfo) - t0).total_seconds() * 1000))
            except Exception:
                pass
        line = ctx.as_event_line()
        line['schema_version'] = _usage_store()._SCHEMA_VERSION
        line['ts'] = _now_iso()
        return _append_line(line)
    except Exception:
        return False


def run_events(run_id: str) -> list:
    """按 run_id 检索 run_end 事件行（走 usage_store 读路径=同锁同文件）。"""
    out = []
    try:
        rid = str(run_id or '')
        if not rid:
            return out
        us = _usage_store()
        for ln in us._read_jsonl_lines():
            if not isinstance(ln, dict):
                continue
            if ln.get('event') != 'run_end':
                continue
            if str(ln.get('run_id') or '') == rid:
                out.append(ln)
    except Exception:
        return []
    return out


def recent_runs(limit: int = 20) -> list:
    """最近 N 个 run（倒序）：run_end 行 + 该 run 的 usage 费用/调用次数聚合。

    相位 2 stats_ui「最近运行」数据源。行形状：
      {run_id, app_version, model_main, prompt_version, started_at,
       duration_ms, rows_total, rows_ok, rows_low, rows_error, error,
       api_calls, cost_cny}
    全程守卫；usage.enabled=False / 读盘失败 → []。
    """
    try:
        us = _usage_store()
        if not us._is_usage_enabled():
            return []
        ends = {}
        order = []
        costs = {}
        for ln in us._read_jsonl_lines():
            if not isinstance(ln, dict):
                continue
            # run 维度键：schema v2 行取 run_id；落账走 batch_id=run_id（卡面相位 2
            # 约定）时回退 batch_id（RUN- 前缀判定，避免误吞普通 batch_id）
            rid = str(ln.get('run_id') or '')
            if not rid:
                _b = str(ln.get('batch_id') or '')
                if _b.startswith('RUN-'):
                    rid = _b
            if not rid:
                continue
            if ln.get('event') == 'run_end':
                if rid not in ends:
                    order.append(rid)
                ends[rid] = ln
            elif ln.get('event') == 'usage':
                c = ln.get('cost_cny')
                if isinstance(c, (int, float)) and not ln.get('is_estimate'):
                    cc = costs.setdefault(rid, [0.0, 0])
                    cc[0] += float(c)
                    cc[1] += 1
        out = []
        for rid in reversed(order[-max(1, int(limit or 20)):]):
            ev = ends.get(rid) or {}
            cc = costs.get(rid, [0.0, 0])
            out.append({
                'run_id': rid,
                'app_version': ev.get('app_version', ''),
                'model_main': ev.get('model_main', ''),
                'prompt_version': ev.get('prompt_version', ''),
                'started_at': ev.get('started_at', ''),
                'duration_ms': int(ev.get('duration_ms') or 0),
                'rows_total': int(ev.get('rows_total') or 0),
                'rows_ok': int(ev.get('rows_ok') or 0),
                'rows_low': int(ev.get('rows_low') or 0),
                'rows_error': int(ev.get('rows_error') or 0),
                'error': str(ev.get('error') or ''),
                'api_calls': cc[1],
                'cost_cny': round(cc[0], 6),
            })
        return out
    except Exception:
        return []


# ── 内部：usage_store 通道（锁 + 路径 + 开关，禁止第三存储） ──────────────

def _usage_store():
    import usage_store
    return usage_store


def _append_line(line: dict) -> bool:
    """持 usage_store._RECORD_LOCK 追加一行 JSON（与其 record() 写盘同锁同路径）。"""
    try:
        us = _usage_store()
        if not us._is_usage_enabled():
            return False
        import json as _json
        line_str = _json.dumps(line, ensure_ascii=False, separators=(',', ':'))
        with us._RECORD_LOCK:
            with open(us._log_path(), 'a', encoding='utf-8') as f:
                f.write(line_str + '\n')
        return True
    except Exception:
        return False
