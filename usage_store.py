"""
PDD EZ — WS-C 用量落账与聚合（usage_store，v1.4.7）

实现规格：docs/USAGE_ARCHIVE_SPEC.md §5.1 / §5.4 / §6 / §11.6。

本模块提供：
  - record(provider, api_type, model, endpoint, usage, cost_cny, is_estimate,
            call_site='', batch_id='', ts=None)
      写一行 JSON 到 <get_base_dir()>/usage_log.jsonl。
      模块级 Lock + 全 try/except；任何异常吞掉，不外抛。
  - aggregate(window=None) -> dict
      4 档聚合：今日 / 本周 / 本月 / 总计（SPEC §5.4）。
      is_estimate=true 行不计 cost、计 token。
  - month_summary(month=None) -> dict
      按模型/按用途 分布 + 缺 usage 次数 + 估算行占比。
  - batch_id(...) / session_reset() / session_total() ...
      （P1 简单实现：batch_id 由调用方传入字符串；session 用模块级 list 暂存。）

铁律（与宪法 §4 失败哲学一致）：
  - usage.enabled=False → record 不写盘、aggregate 返空
  - 写盘失败/序列化失败全部吞掉
  - 禁数据库 / 禁 tiktoken / 禁内置价格
  - 失败日志走 ocr_dlog.txt（复用现有 _ocr_dlog，不新建日志文件）
"""

import os
import json
import threading
import datetime

from utils import get_base_dir

# 复用 ocr.py 的 _ocr_dlog（写 ocr_dlog.txt，标准 try/except 模式）
try:
    from ocr import _ocr_dlog
except Exception:
    def _ocr_dlog(msg):
        try:
            _p = os.path.join(get_base_dir(), 'output', 'ocr_dlog.txt')
            os.makedirs(os.path.dirname(_p), exist_ok=True)
            with open(_p, 'a', encoding='utf-8') as _f:
                _f.write(msg + '\n')
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────
# 模块级状态
# ──────────────────────────────────────────────────────────────────

_RECORD_LOCK = threading.Lock()  # 写盘并发安全
# v1.6.0 TC-Q4：schema v2 —— record() 行新增 run_id / prompt_version 两键；
# v1（schema_version=1、无此两键）行继续可读可聚合（读取方按缺省空串处理）。
_SCHEMA_VERSION = 2
_LOG_FILE = 'usage_log.jsonl'
# session 暂存：用于「本次识别」聚合（= GUI 启动后到当前批次的累计 cost）
_SESSION_STATE = {
    'costs': [],  # list[(ts_iso, cost_cny, is_estimate)]
}

# v1.4.7 P3-R1-L2：写盘失败连续计数——超过阈值后由 GUI 显示提示（见 get_write_failure_state()）。
# 一次写成功即清零；阈值默认 5（轻打扰：短暂磁盘抖动不打扰用户；持续故障明显提醒）。
_WRITE_FAIL_THRESHOLD = 5
_WRITE_FAIL_STATE = {
    'consecutive': 0,  # 当前连续失败次数（写成功清零）
    'last_error': '',  # 最近一次错误摘要
    'alerted': False,  # 是否已向 GUI 报过警（防重复弹）
}

# v1.4.7 P3-R1-L3：本月 cost 内存增量缓存（_refresh_cost_label 热路径优化）。
# 每次 record() 增量更新：跨月时重置并从 jsonl 全量重算一次（重建缓存）。
# 此后每 60s 轮询仅读内存，不再扫 jsonl（10 万行级别从 ~50ms 降到常数时间）。
# 注：仅统计非估算且 cost_cny 非空行（与 aggregate('month').cost_cny 同口径）。
_MONTH_COST_CACHE = {
    'year': -1,  # 缓存对应的年份；跨月变化时强制重建
    'month': -1,  # 缓存对应的月份
    'cost': 0.0,  # 累计 cost（float）
    'built': False,  # 是否已初始化（首次走 jsonl 重算，之后走增量）
}

# v1.4.7 P3-R1-L1：settings.json 读取 mtime+size 缓存（参考 utils.Config 同款模式）。
# 批量识别每轮多次调 _is_usage_enabled，无缓存时每次都开文件+JSON 解析（10×IO）。
# 文件 mtime 和 size 都未变 → 复用缓存；任一变化 → 重读。
# 单独 mtime 不够安全（Windows NTFS 时间精度 100ns 但 Python float 在快速覆写时
# 可能返回同值；size 一起判定可解决"同 mtime 不同内容"边界）。
_ENABLED_CACHE = {'mtime': -1, 'size': -1, 'enabled': True, 'exists': False}

# ── v1.5.12 按用途汉化（用户反馈：用量明细「按用途」大量英文未汉化）──
# call_site 是埋点审计标签（中文/英文混合：'OCR 识别'/'定位' 与 'locate_elements'
# 等并存）。展示层统一映射中文；未知 key 保留原样（不猜不丢）。
CALL_SITE_L10N = {
    'OCR 识别': '表格识别（OCR）',
    '定位': '界面定位',
    'vision': '视觉调用',
    'live': '实时截图识别',
    'live_screenshot': '实时截图识别',
    'real_time': '实时截图识别',
    'batch': '批量识别',
    'file': '文件图片识别',
    'import': '表格导入',
    'ocr_table': '表格识别（OCR）',
    'ocr_verify': '二次识别复核',
    'locate': '界面定位',
    'locate_elements': '按钮/元素定位',
    'locate_table': '表格区域定位',
    'read_total_count': '读取官方总条数',
    'read_selected_province': '读取省市区',
    'check_page_state': '页面状态检测',
    'detect_anomaly': '页面异常检测',
    'probe_columns': '识别列探测',
    'total_count': '读取总条数',
    'unknown': '未知用途',
}


def display_call_site(site) -> str:
    """call_site → 展示用中文名（v1.5.12 汉化；未知 key 原文兜底，不抛）。"""
    try:
        s = str(site or '')
        return CALL_SITE_L10N.get(s, s or '未知用途')
    except Exception:
        return '未知用途'


def _log_path() -> str:
    return os.path.join(get_base_dir(), _LOG_FILE)


def _is_usage_enabled() -> bool:
    """读 settings.json.usage.enabled；缺省 True；任何异常返 True（默认开）。

    v1.4.7 P3-R1-L1：mtime+size 缓存——文件指纹未变时直接返回缓存值，避免每次 record
    都开文件 + 解析 JSON（批量识别热路径优化，与 utils.Config.load 同步模式）。
    """
    try:
        cfg_path = os.path.join(get_base_dir(), 'settings.json')
        try:
            _mtime = os.path.getmtime(cfg_path)
            _size = os.path.getsize(cfg_path)
        except OSError:
            _mtime, _size = -1, -1
        _c = _ENABLED_CACHE
        if _c['mtime'] == _mtime and _c['size'] == _size:
            return _c['enabled']
        # 指纹变化或首次：重读
        enabled = True
        exists = os.path.exists(cfg_path)
        if exists:
            try:
                with open(cfg_path, 'r', encoding='utf-8') as _f:
                    _raw = json.load(_f)
                if isinstance(_raw, dict):
                    u = _raw.get('usage')
                    if isinstance(u, dict) and 'enabled' in u:
                        enabled = bool(u.get('enabled'))
            except Exception:
                enabled = True
        _c['mtime'] = _mtime
        _c['size'] = _size
        _c['enabled'] = enabled
        _c['exists'] = exists
        return enabled
    except Exception:
        return True


def _invalidate_enabled_cache() -> None:
    """外部修改 settings.json 后可调用此函数强制下次重读。"""
    try:
        _ENABLED_CACHE['mtime'] = -1
        _ENABLED_CACHE['size'] = -1
    except Exception:
        pass


def _now_iso():
    """§5.1 ts 字段格式：本地时间 ISO 8601（带时区偏移）。"""
    try:
        return datetime.datetime.now().astimezone().isoformat(timespec='milliseconds')
    except Exception:
        try:
            return datetime.datetime.now().isoformat()
        except Exception:
            return ''


# ──────────────────────────────────────────────────────────────────
# record —— 落账入口
# ──────────────────────────────────────────────────────────────────

def record(provider: str, api_type: str, model: str, endpoint: str,
           usage, cost_cny, is_estimate: bool,
           call_site: str = '', batch_id: str = '',
           ts: str = None, run_id: str = '', prompt_version: str = '') -> None:
    """追加一行 JSON 到 usage_log.jsonl。

    行格式（SPEC §5.1；v1.6.0 TC-Q4 schema v2 增 run_id/prompt_version）：
      {schema_version, ts, event, provider, api_type, model, endpoint,
       usage: {prompt, completion, total, image_tokens, source},
       cost_cny, is_estimate, call_site, batch_id, run_id, prompt_version, extra}

    v1.6.0 TC-Q4：run_id（RUN-YYYYMMDD-HHMMSS-XXXX，run_context 生成）与
    prompt_version（prompts.manifest.prompt_version()）为可选入参，默认空串——
    旧调用零影响；v1 旧行缺此两键，聚合/明细按缺省处理，互相兼容。

    异常吞掉：写盘失败/序列化失败 → _ocr_dlog 记一行 [usage]...。
    usage.enabled=False → 整条链路不写盘，但 _SESSION_STATE 仍记（费用面板可
    显示「当前未启用用量采集」之类提示；P1 简单实现：直接 return，不写 session）。
    """
    if not _is_usage_enabled():
        return
    if not isinstance(usage, dict):
        # usage 必为 dict（即使字段缺失）；None 时按 fallback 兜底值
        usage = {
            'prompt': 0, 'completion': 0, 'total': 0,
            'image_tokens': None, 'source': 'missing',
        }
    try:
        line = {
            'schema_version': _SCHEMA_VERSION,
            'ts': ts or _now_iso(),
            'event': 'usage',
            'provider': str(provider or ''),
            'api_type': str(api_type or ''),
            'model': str(model or ''),
            'endpoint': str(endpoint or ''),
            'usage': {
                'prompt': int(usage.get('prompt') or 0),
                'completion': int(usage.get('completion') or 0),
                'total': int(usage.get('total') or 0),
                'image_tokens': usage.get('image_tokens', None),
                'source': str(usage.get('source') or ''),
            },
            'cost_cny': (None if cost_cny is None else float(cost_cny)),
            'is_estimate': bool(is_estimate),
            'call_site': str(call_site or ''),
            'batch_id': str(batch_id or ''),
            'run_id': str(run_id or ''),
            'prompt_version': str(prompt_version or ''),
            'extra': {},
        }
        line_str = json.dumps(line, ensure_ascii=False, separators=(',', ':'))
    except Exception as e:
        # 序列化失败（极罕见：image_tokens 含不可序列化对象等）
        try:
            _ocr_dlog('[usage] 行序列化失败: {} | provider={} model={}'.format(
                str(e)[:120], provider, model))
        except Exception:
            pass
        return

    # session 暂存 + 本月 cost 增量缓存（都在 _RECORD_LOCK 内）
    try:
        with _RECORD_LOCK:
            _SESSION_STATE['costs'].append(
                (line['ts'], line.get('cost_cny'), bool(is_estimate))
            )
            # v1.4.7 P3-R1-L3：本月 cost 内存增量（行入账时同步累计，避免 _refresh_cost_label
            # 每次都全量读 jsonl）
            try:
                _ts = line.get('ts', '') or ''
                _y, _m = _ts[:4], _ts[5:7]
                if _y.isdigit() and _m.isdigit():
                    _y, _m = int(_y), int(_m)
                    _mc = _MONTH_COST_CACHE
                    if (not _mc['built']) or _mc['year'] != _y or _mc['month'] != _m:
                        # 跨月或首次：标记待重建（_month_cost() 检测到 built=False 时全量重算）
                        _mc['built'] = False
                        _mc['year'] = _y
                        _mc['month'] = _m
                        _mc['cost'] = 0.0
                    if (not bool(is_estimate)) and (cost_cny is not None):
                        try:
                            _mc['cost'] = round(_mc['cost'] + float(cost_cny), 4)
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception:
        pass

    # 写盘
    _write_ok = False
    _write_err = ''
    try:
        with _RECORD_LOCK:
            _p = _log_path()
            with open(_p, 'a', encoding='utf-8') as _f:
                _f.write(line_str + '\n')
        _write_ok = True
    except Exception as e:
        _write_err = str(e)[:120]
        try:
            _ocr_dlog('[usage] 写盘失败: {} | path={}'.format(_write_err, _log_path()))
        except Exception:
            pass

    # v1.4.7 P3-R1-L2：连续写盘失败计数（GUI 端 _refresh_cost_label 周期性查并提示）
    try:
        with _RECORD_LOCK:
            if _write_ok:
                if _WRITE_FAIL_STATE['consecutive'] > 0:
                    _WRITE_FAIL_STATE['consecutive'] = 0
                    _WRITE_FAIL_STATE['alerted'] = False
                    _WRITE_FAIL_STATE['last_error'] = ''
            else:
                _WRITE_FAIL_STATE['consecutive'] += 1
                _WRITE_FAIL_STATE['last_error'] = _write_err
    except Exception:
        pass


def get_write_failure_state() -> dict:
    """返回当前写盘失败状态（GUI 端读取并按需提示）。

    返回：{consecutive, last_error, should_alert, threshold}
      - consecutive: 当前连续失败次数
      - last_error: 最近一次错误摘要（截 120 字符）
      - should_alert: 是否该提示用户（连续失败 ≥ 阈值 且 本进程未提示过）
      - threshold: 阈值（常量 _WRITE_FAIL_THRESHOLD）

    调用方在读 should_alert 为 True 后，可调用 ack_write_failure_alert() 把 alerted 置位。
    """
    try:
        with _RECORD_LOCK:
            _st = _WRITE_FAIL_STATE
            return {
                'consecutive': int(_st['consecutive']),
                'last_error': str(_st['last_error'] or ''),
                'should_alert': (int(_st['consecutive']) >= _WRITE_FAIL_THRESHOLD
                                 and not bool(_st['alerted'])),
                'threshold': _WRITE_FAIL_THRESHOLD,
            }
    except Exception:
        return {'consecutive': 0, 'last_error': '', 'should_alert': False,
                'threshold': _WRITE_FAIL_THRESHOLD}


def ack_write_failure_alert() -> None:
    """GUI 端展示完提示后调用——把 alerted 置位，避免后续 60s 轮询重复提示。"""
    try:
        with _RECORD_LOCK:
            _WRITE_FAIL_STATE['alerted'] = True
    except Exception:
        pass


def get_month_cost() -> float:
    """返回本月 cost_cny 增量值（v1.4.7 P3-R1-L3 热路径）。

    优先用 _MONTH_COST_CACHE（O(1)）；缓存未初始化或跨月时全量重算一次。
    调用方负责主线程外安全（_refresh_cost_label 仅在主线程/after 回调中调用）。
    """
    try:
        with _RECORD_LOCK:
            _mc = _MONTH_COST_CACHE
            now = datetime.datetime.now()
            cur_y, cur_m = now.year, now.month
            if (not _mc['built']) or _mc['year'] != cur_y or _mc['month'] != cur_m:
                # 跨月或首次：全量重建一次（仅这一次走 jsonl）
                _lines = _read_jsonl_lines_unlocked()
                _cost = 0.0
                for _ln in _lines:
                    try:
                        _ts = _ln.get('ts', '') or ''
                        if not _ts.startswith('{:04d}-{:02d}'.format(cur_y, cur_m)):
                            continue
                        if bool(_ln.get('is_estimate', False)):
                            continue
                        _c = _ln.get('cost_cny')
                        if _c is None:
                            continue
                        _cost += float(_c)
                    except Exception:
                        continue
                _mc['built'] = True
                _mc['year'] = cur_y
                _mc['month'] = cur_m
                _mc['cost'] = round(_cost, 4)
            return float(_mc['cost'])
    except Exception:
        return 0.0


def _read_jsonl_lines_unlocked():
    """读 jsonl 全量（无锁版——调用方必须持 _RECORD_LOCK）。

    保留作内部 helper（M3 之前已被 get_month_cost() 用作锁内读路径，复用不动）。
    """
    p = _log_path()
    if not os.path.exists(p):
        return []
    out = []
    try:
        with open(p, 'r', encoding='utf-8') as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line:
                    continue
                try:
                    out.append(json.loads(_line))
                except Exception:
                    try:
                        _ocr_dlog('[usage] 行解析失败，跳过: {}'.format(_line[:120]))
                    except Exception:
                        pass
                    continue
    except Exception as e:
        try:
            _ocr_dlog('[usage] 读 jsonl 失败: {}'.format(str(e)[:120]))
        except Exception:
            pass
    return out


def session_reset() -> None:
    """重置「本次识别」累计（P1 简单实现：清空 session list）。"""
    try:
        with _RECORD_LOCK:
            _SESSION_STATE['costs'].clear()
    except Exception:
        pass


def session_total() -> float:
    """返回 session 累计 cost_cny（is_estimate 行不计）。"""
    try:
        total = 0.0
        for _ts, cost, is_est in _SESSION_STATE['costs']:
            if is_est:
                continue
            if cost is None:
                continue
            try:
                total += float(cost)
            except Exception:
                continue
        return round(total, 4)
    except Exception:
        return 0.0


# ──────────────────────────────────────────────────────────────────
# 4 档聚合（SPEC §5.4）
# ──────────────────────────────────────────────────────────────────

def _parse_ts(ts_str: str):
    """解析 ISO 字符串为 datetime；失败返 None。"""
    if not ts_str:
        return None
    try:
        # 兼容 '+08:00' 与 'Z'（后者较少见但健壮）
        s = ts_str.replace('Z', '+00:00')
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None


def _read_jsonl_lines():
    """读取全部 jsonl 行（解析失败的行跳过，记 dlog）。

    v1.4.7 P3-R2-M3：加 _RECORD_LOCK 保护——与 record() 写盘同锁，防止 reader 读到
    record() 写到一半的半截 JSON 行（极端竞态：record 内部 f.write(line_str + '\\n')
    在两次 write 之间被打断；旧实现 reader 会拿到一个不完整 JSON 行被 try/except 吞，
    不崩但丢一行数据）。锁内读与锁内写原子化——以增加 reader 等待开销换得「永远不会
    读到半截行」的硬保证。
    """
    try:
        with _RECORD_LOCK:
            return _read_jsonl_lines_unlocked()
    except Exception:
        return []


def _date_bounds(window: str):
    """按 window 名返回 (start_dt, end_dt)；window ∈ {'today','week','month','all','today_zero'}。
    today_zero = 当天 00:00 起算（与 today 等价，但语义明示用于"今日"聚合）。
    """
    now = datetime.datetime.now()
    if window in ('today', 'today_zero'):
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now
    if window == 'week':
        # 周一为起点（SPEC §5.4）
        start_date = now.date() - datetime.timedelta(days=now.weekday())
        start = datetime.datetime.combine(start_date, datetime.time(0, 0, 0))
        return start, now
    if window == 'month':
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, now
    return None, None  # 'all' or unknown


def aggregate(window: str = None) -> dict:
    """4 档聚合。window ∈ {'today','week','month','all'}；None 返 4 档全集。

    返回（每档）：
      {cost_cny, total_tokens, prompt_tokens, completion_tokens,
       image_calls, estimate_count, real_count, missing_count}

    行为（SPEC §5.4）：
      - is_estimate=true 行**不计 cost**、**计 token**（用于"用了几百万 token"展示）
      - 缺 usage / cost_cny=None / 字段缺失：对应行不计入 cost，token 累加用 0
    """
    lines = _read_jsonl_lines()
    if window is not None:
        return _aggregate_one(lines, window)
    return {
        'today': _aggregate_one(lines, 'today'),
        'week': _aggregate_one(lines, 'week'),
        'month': _aggregate_one(lines, 'month'),
        'all': _aggregate_one(lines, 'all'),
    }


def _aggregate_one(lines, window: str) -> dict:
    cost = 0.0
    total_tokens = 0
    prompt_tokens = 0
    completion_tokens = 0
    image_calls = 0
    estimate_count = 0
    real_count = 0
    missing_count = 0
    start, end = _date_bounds(window)
    for ln in lines:
        try:
            ts = _parse_ts(ln.get('ts', ''))
            if start is not None and ts is not None and ts < start:
                continue
            if end is not None and ts is not None and ts > end:
                continue
        except Exception:
            pass
        u = ln.get('usage') or {}
        if not isinstance(u, dict):
            missing_count += 1
            continue
        is_est = bool(ln.get('is_estimate', False))
        # token 累加（估算行也计——用于"用了几百万 token"展示）
        try:
            prompt_tokens += int(u.get('prompt') or 0)
        except Exception:
            pass
        try:
            completion_tokens += int(u.get('completion') or 0)
        except Exception:
            pass
        try:
            total_tokens += int(u.get('total') or 0)
        except Exception:
            pass
        # image_tokens 存在 → 计一张图
        try:
            if u.get('image_tokens'):
                image_calls += 1
        except Exception:
            pass
        # cost 累加（is_estimate 不计）
        if is_est:
            estimate_count += 1
        else:
            real_count += 1
            c = ln.get('cost_cny')
            if c is not None:
                try:
                    cost += float(c)
                except Exception:
                    pass
            if c is None:
                # pricing 缺失（is_estimate=False 也可能 cost_cny=None）
                missing_count += 1
    return {
        'cost_cny': round(cost, 4),
        'total_tokens': total_tokens,
        'prompt_tokens': prompt_tokens,
        'completion_tokens': completion_tokens,
        'image_calls': image_calls,
        'estimate_count': estimate_count,
        'real_count': real_count,
        'missing_count': missing_count,
    }


# ──────────────────────────────────────────────────────────────────
# month_summary —— 按模型/按用途分布（费用面板数据源，SPEC §5.4）
# ──────────────────────────────────────────────────────────────────

def month_summary(month: str = None) -> dict:
    """按模型 / 按 call_site 分布本月用量。

    month 形如 'YYYY-MM'；None 时取当前月。
    返回：{month, total_cost, total_tokens, by_model: {model: {cost, tokens, count}},
           by_call_site: {site: {cost, tokens, count}}, missing_count, estimate_count}
    """
    if month is None:
        now = datetime.datetime.now()
        month = '{:04d}-{:02d}'.format(now.year, now.month)
    lines = _read_jsonl_lines()
    by_model = {}
    by_call_site = {}
    total_cost = 0.0
    total_tokens = 0
    missing_count = 0
    estimate_count = 0
    for ln in lines:
        ts_str = ln.get('ts', '') or ''
        if month not in ts_str[:7]:  # ISO 字符串前 7 字符 = YYYY-MM
            continue
        u = ln.get('usage') or {}
        if not isinstance(u, dict):
            missing_count += 1
            continue
        is_est = bool(ln.get('is_estimate', False))
        try:
            total_tokens += int(u.get('total') or 0)
        except Exception:
            pass
        mdl = str(ln.get('model') or '(unknown)')
        site = str(ln.get('call_site') or '(unknown)')
        # cost：is_estimate 不计
        c = ln.get('cost_cny')
        cost_v = 0.0
        if (not is_est) and c is not None:
            try:
                cost_v = float(c)
            except Exception:
                cost_v = 0.0
            total_cost += cost_v
        try:
            tok_v = int(u.get('total') or 0)
        except Exception:
            tok_v = 0
        # 累加 by_model
        bm = by_model.setdefault(mdl, {'cost': 0.0, 'tokens': 0, 'count': 0})
        bm['cost'] = round(bm['cost'] + cost_v, 4)
        bm['tokens'] += tok_v
        bm['count'] += 1
        # 累加 by_call_site
        bc = by_call_site.setdefault(site, {'cost': 0.0, 'tokens': 0, 'count': 0})
        bc['cost'] = round(bc['cost'] + cost_v, 4)
        bc['tokens'] += tok_v
        bc['count'] += 1
        if is_est:
            estimate_count += 1
    return {
        'month': month,
        'total_cost': round(total_cost, 4),
        'total_tokens': total_tokens,
        'by_model': by_model,
        'by_call_site': by_call_site,
        'missing_count': missing_count,
        'estimate_count': estimate_count,
    }


# ──────────────────────────────────────────────────────────────────
# usage_panel_summary —— v1.4.7 P3-R2-M1/L1：费用面板统一入口
# ──────────────────────────────────────────────────────────────────

def usage_panel_summary() -> dict:
    """费用面板数据源（gui.py cost_label 与 stats_ui 用量明细页共用入口）。

    v1.4.7 P3-R2-M1/L1：把 gui 和明细面板拉到同一条路径上——避免「一个项目两个消费点
    走不同实现」的漂移。v1.4.x 导航重构后消费方 = gui 工具条 cost_label +
    stats_ui._usage_page_refresh（原 settings_ui._show_usage_detail 已迁为导航页）。
    返回 4 档聚合 + by_model + by_call_site + 本月 cost 估值（按月用
    `get_month_cost()` 内存缓存，与 gui 工具条 cost_label 同一来源，保证「本月 ¥X.XX」两边
    始终一致）。

    返回：
      {today, week, month, all, by_model, by_call_site, month_label, missing_count, estimate_count}
      每档结构同 aggregate()；month.cost_cny 来源于 get_month_cost()（与 P3-R1-L3 同源）。
    """
    try:
        agg = aggregate() or {}
        ms = month_summary() or {}
        try:
            _mon_cost = get_month_cost()  # O(1) 缓存命中；与 gui cost_label 同源
            if isinstance(agg.get('month'), dict):
                _mm = dict(agg['month'])
                _mm['cost_cny'] = round(float(_mon_cost), 4)
                agg['month'] = _mm
        except Exception:
            pass
        return {
            'today': agg.get('today') or {},
            'week': agg.get('week') or {},
            'month': agg.get('month') or {},
            'all': agg.get('all') or {},
            'by_model': ms.get('by_model') or {},
            'by_call_site': ms.get('by_call_site') or {},
            'month_label': ms.get('month', ''),
            'missing_count': ms.get('missing_count', 0),
            'estimate_count': ms.get('estimate_count', 0),
        }
    except Exception:
        return {
            'today': {}, 'week': {}, 'month': {}, 'all': {},
            'by_model': {}, 'by_call_site': {}, 'month_label': '',
            'missing_count': 0, 'estimate_count': 0,
        }


def reset_month(month: str = None) -> bool:
    """重置某月用量：删除该月全部行并追加一条 config_change 审计事件。

    「费用面板 → 重置本月数据」入口（T-C6）；二次确认交互在 GUI 层完成。
    month 形如 'YYYY-MM'；None 取当前月。全程 try/except，成功 True。

    v1.4.7 P3-R2-C1/M2：改用 `os.replace(tmp, _log_path())` 原子替换（与 utils.Config.save
    同款 Windows-same-volume-atomic 模式）。旧实现是 truncate-then-write 非原子——进程
    在 truncate 与 copy 之间崩溃会丢整个 jsonl。原子替换后：tmp 落盘成功即替换，崩溃窗口
    期间原文件完整保留；Windows 文件锁瞬态拒绝时重试 3 次。
    """
    try:
        import time as _time
        if not month:
            now = datetime.datetime.now()
            month = '{:04d}-{:02d}'.format(now.year, now.month)
        month = str(month)[:7]
        with _RECORD_LOCK:
            lines = _read_jsonl_lines_unlocked()  # 已在 _RECORD_LOCK 内，用无锁版
            kept = [ln for ln in lines if not str(ln.get('ts', ''))[:7] == month]
            removed = len(lines) - len(kept)
            audit = {
                'schema_version': _SCHEMA_VERSION,
                'ts': _now_iso(),
                'event': 'config_change',
                'action': 'reset_month',
                'month': month,
                'removed_lines': removed,
            }
            # tmp 名按 pid 唯一化（防多线程/多进程互相覆盖）
            target = _log_path()
            tmp = f"{target}.tmp{os.getpid()}"
            with open(tmp, 'w', encoding='utf-8') as _f:
                for ln in kept:
                    _f.write(json.dumps(ln, ensure_ascii=False,
                                        separators=(',', ':')) + '\n')
                _f.write(json.dumps(audit, ensure_ascii=False,
                                    separators=(',', ':')) + '\n')
            # 原子替换：与 utils.Config.save 同款；Windows 文件锁瞬态拒绝重试 3 次
            _replaced = False
            for _attempt in range(3):
                try:
                    os.replace(tmp, target)
                    _replaced = True
                    break
                except OSError:
                    if _attempt >= 2:
                        raise
                    _time.sleep(0.2)
            if not _replaced:
                # 防御：3 次都失败——保留 tmp 供人工恢复（不静默丢）
                try:
                    _ocr_dlog(f'[usage] reset_month 原子替换失败 3 次，保留 tmp={tmp}')
                except Exception:
                    pass
                return False
            # 替换成功：tmp 已被 os.replace 消费，不需要再 remove
            # v1.4.7 P3-R1-L3：删除行后失效本月 cost 缓存（重置当月时强制重算为 0）
            try:
                now = datetime.datetime.now()
                cur_ym = '{:04d}-{:02d}'.format(now.year, now.month)
                if month == cur_ym:
                    _MONTH_COST_CACHE['built'] = False
                    _MONTH_COST_CACHE['cost'] = 0.0
            except Exception:
                pass
        try:
            _ocr_dlog(f"[usage] 已重置 {month} 用量数据（删除 {removed} 行）")
        except Exception:
            pass
        return True
    except Exception as e:
        try:
            _ocr_dlog(f'[usage] reset_month 失败: {str(e)[:120]}')
        except Exception:
            pass
        return False


# ──────────────────────────────────────────────────────────────────
# P2-C：实时截图识别当日计数（按日期/call_site 双键匹配）
# 用于免费版每日 50 次门控（enforce=true 时）
# ──────────────────────────────────────────────────────────────────
def count_today_live_screenshot() -> int:
    """统计今天「实时截图」识别次数（call_site 包含 'live' 或 'live_screenshot'）。

    复用 _read_jsonl_lines；只读不写，失败返 0（绝不阻塞 GUI）。
    用户裁定：enforce=false 默认全免时本函数不被调用，调用方先查 is_pro/enforce。
    """
    try:
        today = datetime.datetime.now().strftime('%Y-%m-%d')
        n = 0
        for line in _read_jsonl_lines():
            try:
                if not isinstance(line, dict):
                    continue
                ts = str(line.get('ts') or '')
                if not ts.startswith(today):
                    continue
                # call_site = 'live_screenshot' / 'live' / 'real_time' 等都算实时截图
                cs = str(line.get('call_site') or '').lower()
                extra = line.get('extra') or {}
                if not isinstance(extra, dict):
                    extra = {}
                source = str(extra.get('source') or '').lower()
                if 'live' in cs or 'screenshot' in cs or 'live' in source or 'screenshot' in source:
                    n += 1
            except Exception:
                continue
        return n
    except Exception:
        return 0
