"""
PDD EZ v1.6.0 — 补货回测器 v0

对历史库（history.db）中的 SKU 日销序列做**滑动窗口前滚回测**（walk-forward）：
每个截断日 t 只用 ≤t 的数据拟合模型，预测第 t+horizon 天日销，与真实值对比，
产出 MAE / MAPE / RMSE / Bias 四指标。

设计红线（§1.9 勘察结论）：
- **只读**：对本库用 sqlite URI mode=ro 打开，绝不写历史库；不碰 history_db 主路径
  （识别主流程的生命线 = history_db._run_db 永不外抛契约，本工具不经过它）。
- **隐私**：报告与 JSON 输出**绝不含商品名/SKU 原文**（对齐口径），
  SKU 组一律掩码为 sku_0001...（按 (sku_id, region, store) 排序后顺序编号）。
- **显式失败**：库不存在/表结构缺失/参数非法 → stderr 明确报错，不静默兜底。
- **纯函数优先**：指标与模型全部纯函数，便于回归单测；IO 只做薄壳。

模型（v0 四个基线，全部基于日销序列 series）：
- ses      简单指数平滑（与 algorithm_ui.forecast_next_period 同公式：S0=x0，
           St=α·xt+(1−α)·S(t−1)，α 默认 0.5=DEFAULT_FORECAST_ALPHA）
- weighted 加权日销（与 utils._weighted_daily 同语义：0.5×近7日均值 + 0.3×近14日
           + 0.2×近30日，各自仅统计 >0 样本；全 0 → 0.0）
- ma7      近 7 日简单均值（不足 7 点 → None，该评估点跳过）
- last     上一日日销（朴素基线）

退出码：0 = 完成回测（含"指标难看"的正常结果）；2 = 无可回测数据
（无 SKU 组 / 全部被样本门禁跳过 / 评估点为 0）；1 = 程序错误。

用法：
  python tools/backtest.py                          # 默认库 + 全模型 + 近 90 天
  python tools/backtest.py --days 60 --min-samples 14 --models ses,ma7
  python tools/backtest.py --json data/synthetic/_reports/backtest.json
  python tools/backtest.py --selftest               # 内置合成序列 sanity 自检
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DEFAULT_ALPHA = 0.5          # 与 algorithm_ui.DEFAULT_FORECAST_ALPHA 同值（避免 import algorithm_ui 引入 tkinter 链）
DEFAULT_DAYS = 90
DEFAULT_MIN_SAMPLES = 14     # §4 替代方案：每 SKU 序列 < 14 点 → 跳过并计数
DEFAULT_HORIZON = 1


# ============================================================
# 指标（纯函数）：输入等长 y_true / y_pred 数值序列
# ============================================================

def _check_pairs(y_true: Sequence[float], y_pred: Sequence[float]) -> Tuple[List[float], List[float]]:
    """公共输入守卫：非空、等长、可转 float；失败抛 ValueError（显式，§4）。"""
    if y_true is None or y_pred is None:
        raise ValueError('y_true/y_pred 不能为 None')
    if len(y_true) != len(y_pred):
        raise ValueError(f'长度不一致: y_true={len(y_true)} y_pred={len(y_pred)}')
    if len(y_true) == 0:
        raise ValueError('输入为空序列')
    yt, yp = [], []
    for a, b in zip(y_true, y_pred):
        yt.append(float(a))
        yp.append(float(b))
    return yt, yp


def mae(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """平均绝对误差 MAE = mean(|y − p|）。"""
    yt, yp = _check_pairs(y_true, y_pred)
    return sum(abs(a - b) for a, b in zip(yt, yp)) / len(yt)


def mape(y_true: Sequence[float], y_pred: Sequence[float]) -> Tuple[Optional[float], int]:
    """平均绝对百分比误差（仅统计 y>0 样本，y=0 无定义）。

    Returns:
        (mape 百分数 或 None, 参与统计的样本数 n)
        全部 y=0（或无有效样本）→ (None, 0)。
    """
    yt, yp = _check_pairs(y_true, y_pred)
    vals = [abs(a - b) / a * 100.0 for a, b in zip(yt, yp) if a > 0]
    n = len(vals)
    if n == 0:
        return None, 0
    return sum(vals) / n, n


def rmse(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """均方根误差 RMSE = sqrt(mean((y − p)²)）。"""
    yt, yp = _check_pairs(y_true, y_pred)
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(yt, yp)) / len(yt))


def bias(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """偏置 Bias = mean(p − y)。>0 = 模型系统性预测偏高；<0 = 偏低。"""
    yt, yp = _check_pairs(y_true, y_pred)
    return sum(b - a for a, b in zip(yt, yp)) / len(yt)


# ============================================================
# 日销序列归约（纯函数）
# ============================================================

def daily_series(history_rows: List[dict]) -> List[float]:
    """history_rows（history_db.query_sku_history 同款形态）→ 连续日销序列。

    - 同日多行 sales 求和；captured_at 取前 10 位解析日期，解析失败/缺字段行跳过；
    - **首日至末日之间的缺失日补 0**（与 algorithm_ui._history_to_daily_series
      的窗口补零语义一致，保证"下一日"有定义）；
    - 序列按日期升序；无有效行 → []。
    """
    if not history_rows or not isinstance(history_rows, list):
        return []
    per_day: dict = {}
    days = []
    for r in history_rows:
        if not isinstance(r, dict):
            continue
        try:
            sv = float(r.get('sales') or 0)
        except Exception:
            continue
        ts = str(r.get('captured_at') or '')
        if len(ts) < 10:
            continue
        try:
            d = datetime.strptime(ts[:10], '%Y-%m-%d').date()
        except ValueError:
            continue
        per_day[d] = per_day.get(d, 0.0) + sv
        days.append(d)
    if not days:
        return []
    first, last = min(days), max(days)
    span = (last - first).days + 1
    return [float(per_day.get(_ord_date(first.toordinal() + off), 0.0)) for off in range(span)]


def _ord_date(ordinal: int):
    from datetime import date as _d
    return _d.fromordinal(ordinal)


# ============================================================
# 预测模型（纯函数）：series(截断前缀) → 下一期预测值；数据不足返回 None
# ============================================================

def model_last(series: Sequence[float]) -> Optional[float]:
    """朴素基线：上一日日销。"""
    if not series:
        return None
    return float(series[-1])


def model_ma7(series: Sequence[float]) -> Optional[float]:
    """近 7 日简单均值；不足 7 点 → None（该评估点对本模型跳过）。"""
    if series is None or len(series) < 7:
        return None
    win = [float(x) for x in series[-7:]]
    return sum(win) / 7.0


def model_ses(series: Sequence[float], alpha: float = DEFAULT_ALPHA) -> Optional[float]:
    """简单指数平滑（与 algorithm_ui.forecast_next_period 同公式）。α 钳 [0,1]。"""
    if series is None or len(series) < 2:
        return None
    a = min(1.0, max(0.0, float(alpha)))
    s = float(series[0])
    for x in series[1:]:
        s = a * float(x) + (1.0 - a) * s
    return max(0.0, s)


def model_weighted(series: Sequence[float]) -> Optional[float]:
    """加权日销 0.5×近7 + 0.3×近14 + 0.2×近30（各自仅统计 >0 样本；全 0 → 0.0）。

    与 utils._weighted_daily 同语义（该函数吃 history_rows，本函数吃序列，
    数值行为一致：<14 点窗口按实际可得天数归约——窗口不足时对应分量退化为
    全部可得 >0 样本均值，与 utils 版在短序列上的行为一致）。
    """
    if not series:
        return None

    def _avg(window: int) -> float:
        tail = [float(x) for x in series[-window:] if float(x) > 0]
        if not tail:
            return 0.0
        return sum(tail) / len(tail)

    d7, d14, d30 = _avg(7), _avg(14), _avg(30)
    if d7 == 0 and d14 == 0 and d30 == 0:
        return 0.0
    return 0.5 * d7 + 0.3 * d14 + 0.2 * d30


MODELS: Dict[str, Callable[[Sequence[float]], Optional[float]]] = {
    'ses': model_ses,
    'weighted': model_weighted,
    'ma7': model_ma7,
    'last': model_last,
}


# ============================================================
# 前滚回测（纯函数）：不碰任何 IO
# ============================================================

def walk_forward(series: Sequence[float], min_samples: int, horizon: int = 1) -> List[Tuple[List[float], float]]:
    """生成全部 (train 前缀, actual) 评估对。

    - min_samples：拟合所需最少历史点数（训练前缀长度下限）；
    - horizon：向前预测步长（v0 默认 1）；
    - 评估对数量 = max(0, len(series) − horizon − min_samples + 1)。
    """
    if min_samples < 1:
        raise ValueError(f'min_samples 必须 ≥1，收到 {min_samples}')
    if horizon < 1:
        raise ValueError(f'horizon 必须 ≥1，收到 {horizon}')
    n = len(series)
    out = []
    for i in range(min_samples, n - horizon + 1):
        out.append((list(series[:i]), float(series[i + horizon - 1])))
    return out


def run_backtest(groups: Dict[str, List[dict]], models: Dict[str, Callable] = None,
                 min_samples: int = DEFAULT_MIN_SAMPLES, horizon: int = 1,
                 alpha: float = DEFAULT_ALPHA) -> dict:
    """对多组 SKU 序列跑回测，返回聚合报告（纯统计量，无原文）。

    Args:
        groups: {掩码键 sku_0001: history_rows}（调用方负责掩码，本函数不感知原文）。
        models: {模型名: fn(series)->float|None}；None = 全部内置模型。
    Returns:
        {'sku_groups', 'skipped', 'eval_points', 'models': {name: {n, mae, mape, mape_n, rmse, bias}}}
        单模型在某评估点返回 None（数据不足）→ 该点对该模型跳过（n 单独计）。
    """
    use_models = dict(MODELS if models is None else models)
    skipped = 0
    evals: List[Tuple[List[float], float]] = []
    for key in sorted(groups.keys()):
        series = daily_series(groups[key])
        pairs = walk_forward(series, min_samples=min_samples, horizon=horizon)
        if not pairs and len(series) < min_samples:
            skipped += 1
            continue
        evals.extend(pairs)
    report = {
        'sku_groups': len(groups),
        'skipped': skipped,
        'eval_points': len(evals),
        'models': {},
    }
    for name, fn in use_models.items():
        yt, yp = [], []
        for train, actual in evals:
            try:
                pred = fn(train)
            except Exception:
                pred = None
            if pred is None:
                continue
            yt.append(actual)
            yp.append(float(pred))
        if yt:
            m, mn = mape(yt, yp)
            report['models'][name] = {
                'n': len(yt),
                'mae': round(mae(yt, yp), 4),
                'mape': (round(m, 2) if m is not None else None),
                'mape_n': mn,
                'rmse': round(rmse(yt, yp), 4),
                'bias': round(bias(yt, yp), 4),
            }
        else:
            report['models'][name] = {'n': 0, 'mae': None, 'mape': None,
                                      'mape_n': 0, 'rmse': None, 'bias': None}
    return report


# ============================================================
# IO 薄壳：只读历史库 + 掩码分组
# ============================================================

def _open_ro(db_path: str) -> sqlite3.Connection:
    """只读打开 sqlite（URI mode=ro；路径经 quote 处理空格）。失败抛异常由调用方报错。"""
    from pathlib import Path as _P
    uri = 'file:' + urllib.parse.quote(_P(db_path).as_posix()) + '?mode=ro'
    return sqlite3.connect(uri, uri=True)


def collect_sku_groups(db_path: str, days: int = DEFAULT_DAYS) -> Tuple[Dict[str, List[dict]], int]:
    """只读扫描历史库 → {掩码键: history_rows}；返回 (groups, bad_rows)。

    分组键 = (store, region, sku_id)（sku_id 非空；与 ocr.dedup_items「sku 权威去重、
    多仓库各自保留」同语义）。行形状 = {captured_at, sales}（回测所需最小集）。
    掩码：按 (sku_id, region, store) 排序后顺序编号 sku_0001...——**报告零原文**。
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f'历史库不存在: {db_path}')
    floor = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d 00:00:00')
    conn = _open_ro(db_path)
    try:
        cur = conn.execute(
            'SELECT store, region, sku_id, MAX(name) FROM history_rows '
            'WHERE sku_id != \'\' AND captured_at >= ? '
            'GROUP BY store, region, sku_id '
            'ORDER BY sku_id ASC, region ASC, store ASC', (floor,))
        keys = cur.fetchall()
        groups: Dict[str, List[dict]] = {}
        bad_rows = 0
        for idx, (store, region, sku_id, _name) in enumerate(keys, start=1):
            mask = f'sku_{idx:04d}'
            rows = conn.execute(
                'SELECT captured_at, sales FROM history_rows '
                'WHERE store=? AND region=? AND sku_id=? AND captured_at >= ? '
                'ORDER BY captured_at ASC', (store, region, sku_id, floor)).fetchall()
            out = []
            for ts, sales in rows:
                if ts is None:
                    bad_rows += 1
                    continue
                try:
                    out.append({'captured_at': str(ts), 'sales': int(sales or 0)})
                except Exception:
                    bad_rows += 1
            groups[mask] = out
        return groups, bad_rows
    finally:
        conn.close()


def format_report(report: dict, meta: dict) -> str:
    """报告 → 三段式文本（库信息/模型指标表/口径说明）。仅统计量，无任何原文。"""
    lines = []
    lines.append('== PDD EZ 补货回测 v0 ==')
    lines.append(f"库: {meta.get('db', '')}（只读）")
    lines.append(f"窗口: 近 {meta.get('days')} 天 | min_samples={meta.get('min_samples')} "
                 f"| horizon={meta.get('horizon')} | alpha={meta.get('alpha')}")
    lines.append(f"SKU 组: {report['sku_groups']}（样本不足跳过 {report['skipped']}）"
                 f" | 评估点: {report['eval_points']}")
    lines.append('')
    lines.append(f"{'模型':<10}{'评估点':>6}{'MAE':>10}{'MAPE%':>10}{'RMSE':>10}{'Bias':>10}")
    for name in sorted(report['models'].keys()):
        m = report['models'][name]
        if m['n'] == 0:
            lines.append(f'{name:<10}{0:>6}{"—":>10}{"—":>10}{"—":>10}{"—":>10}')
            continue
        mape_txt = f"{m['mape']:.2f}" if m['mape'] is not None else '—'
        lines.append(f"{name:<10}{m['n']:>6}{m['mae']:>10.4f}{mape_txt:>10}{m['rmse']:>10.4f}{m['bias']:>+.4f}")
    lines.append('')
    lines.append('口径: MAPE 仅统计真实值>0 的评估点（避免除零，样本数见 n 与 mape_n 的差）；'
                 'Bias = mean(预测-真实)，>0 = 预测系统性偏高。')
    lines.append('v0 为测量工具，无达标门禁；指标解读与算法取舍归  后续任务。')
    return '\n'.join(lines)


# ============================================================
# 内置自检（--selftest）：合成序列 sanity，不依赖任何库
# ============================================================

def selftest() -> int:
    """内置 sanity：常量序列 SES/MA7/加权/last 都应还原常量；指标满足已知恒等式。"""
    ok = True
    const = [5.0] * 20
    for name, fn in (('ses', model_ses), ('ma7', model_ma7),
                     ('weighted', model_weighted), ('last', model_last)):
        v = fn(const)
        if v is None or abs(v - 5.0) > 1e-9:
            print(f'[selftest] FAIL {name} 常量序列应还原 5.0，得到 {v}')
            ok = False
    yt, yp = [1.0, 2.0, 3.0], [2.0, 2.0, 2.0]
    # bias(y_true, y_pred) = mean(p − y)：
    #   yt=[1,2,3], yp=[2,2,2] → p−y = (1,0,−1) → mean = 0（不是 1/3）
    # 旧断言曾误写为 1/3（即 mean(|p−y|)=mae 的近似值），已修复()
    if abs(mae(yt, yp) - 2.0 / 3.0) > 1e-9 or abs(bias(yt, yp) - 0.0) > 1e-9:
        print('[selftest] FAIL mae/bias 恒等式')
        ok = False
    # bias 符号验证()：偏高 → 正；偏低 → 负
    if abs(bias([10.0, 10.0], [12.0, 12.0]) - 2.0) > 1e-9:
        print('[selftest] FAIL bias 偏高应为 +2')
        ok = False
    if abs(bias([10.0, 10.0], [8.0, 8.0]) - (-2.0)) > 1e-9:
        print('[selftest] FAIL bias 偏低应为 -2')
        ok = False
    if abs(rmse(yt, yp) - math.sqrt(2.0 / 3.0)) > 1e-9:
        print('[selftest] FAIL rmse 恒等式')
        ok = False
    pairs = walk_forward([0.0] * 20, min_samples=14)
    if len(pairs) != 6:
        print(f'[selftest] FAIL walk_forward 评估点数应=6，得到 {len(pairs)}')
        ok = False
    print('[selftest] OK' if ok else '[selftest] 存在失败项')
    return 0 if ok else 1


# ============================================================
# CLI
# ============================================================

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='PDD EZ 补货回测器 v0（只读，MAE/MAPE/RMSE/Bias）')
    parser.add_argument('--db', default=None, help='history.db 路径（默认 history_db.db_path()）')
    parser.add_argument('--days', type=int, default=DEFAULT_DAYS, help='历史窗口天数（默认 90）')
    parser.add_argument('--min-samples', type=int, default=DEFAULT_MIN_SAMPLES,
                        help='每 SKU 拟合最少历史点数（默认 14，不足跳过并计数）')
    parser.add_argument('--horizon', type=int, default=DEFAULT_HORIZON, help='向前预测步长（默认 1）')
    parser.add_argument('--alpha', type=float, default=DEFAULT_ALPHA, help='SES 平滑系数（默认 0.5）')
    parser.add_argument('--models', default='ses,weighted,ma7,last',
                        help='逗号分隔模型名（可选 ses/weighted/ma7/last）')
    parser.add_argument('--json', default=None, help='可选：报告 JSON 输出路径')
    parser.add_argument('--selftest', action='store_true', help='内置合成序列 sanity 自检后退出')
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.db:
        db_path = args.db
    else:
        try:
            import history_db as _hdb
            db_path = _hdb.db_path()
        except Exception as e:
            print(f'[backtest] 程序错误：无法解析默认历史库路径: {e}', file=sys.stderr)
            return 1

    if args.days <= 0:
        print('[backtest] 参数错误：--days 必须 >0', file=sys.stderr)
        return 1
    if args.min_samples < 2:
        print('[backtest] 参数错误：--min-samples 必须 ≥2（至少 1 点拟合 + 1 点评估）', file=sys.stderr)
        return 1
    if args.horizon < 1:
        print('[backtest] 参数错误：--horizon 必须 ≥1', file=sys.stderr)
        return 1

    use_models = {}
    for name in [s.strip() for s in args.models.split(',') if s.strip()]:
        if name not in MODELS:
            print(f'[backtest] 参数错误：未知模型 {name}（可选: {",".join(MODELS)}）', file=sys.stderr)
            return 1
        use_models[name] = MODELS[name]
    if not use_models:
        print('[backtest] 参数错误：--models 为空', file=sys.stderr)
        return 1

    try:
        groups, bad_rows = collect_sku_groups(db_path, days=args.days)
    except FileNotFoundError as e:
        print(f'[backtest] {e}', file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        print(f'[backtest] 历史库只读打开/查询失败（表结构缺失或文件损坏）: {e}', file=sys.stderr)
        return 1

    report = run_backtest(groups, models=use_models, min_samples=args.min_samples,
                          horizon=args.horizon, alpha=args.alpha)
    meta = {'db': db_path, 'days': args.days, 'min_samples': args.min_samples,
            'horizon': args.horizon, 'alpha': args.alpha, 'bad_rows': bad_rows,
            'generated_at': datetime.now().isoformat(timespec='seconds')}
    text = format_report(report, meta)
    print(text)
    if bad_rows:
        print(f'[backtest] 提示: {bad_rows} 行 captured_at/sales 无法解析，已跳过（不计入指标）')

    if args.json:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
            payload = dict(meta)
            payload.update(report)
            with open(args.json, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f'[backtest] JSON 已写出: {args.json}')
        except OSError as e:
            print(f'[backtest] JSON 写出失败: {e}', file=sys.stderr)
            return 1

    # 退出码：无可回测数据（无组 / 全跳过 / 零评估点）→ 2；否则 0
    if report['sku_groups'] == 0 or report['eval_points'] == 0:
        print('[backtest] 无可回测数据（历史库为空或全部 SKU 未达样本门禁）')
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
