"""PDD EZ — OCR 复核流程（v1.4.8 P2-OCR t9）

任务 t9 落地：低置信行人工确认 + 容错收口。

设计目标（铁律 ④：纯 stdlib；不依赖 Tk，便于单测）：
- build_review_list(items) → 抽取低置信行清单（pure）
- apply_user_edits(items, edits) → 把用户在弹窗里改的字段写回 items（pure）
- summarize_review(items) → 弹窗顶部统计文本（pure）
- categorize_error(e) → 把异常归类到 USER_MSG_* 常量之一（pure）

弹窗/按钮的 Tk 实现位于 gui.py；本模块只放可单测的纯逻辑。

约束（来自 t3 + t8 + docs/DESIGN.md §1/§4）：
- 不改 ocr_dual_verify_generic 本身；只消费 build_confidence_meta 产出。
- 复用 t3 容错文案常量 ocr.USER_MSG_*，避免重复定义。
- 任何异常路径 fail-safe：弹窗出错也允许主流程继续（避免阻塞识别）。
"""
from __future__ import annotations

import re as _re
from typing import List, Dict, Any, Optional, Tuple


# 工具：把异常消息归类到用户可读提示
_ERROR_PATTERNS = (
    # 顺序：先匹配更具体的（更长的）字符串；命中即返回。
    # 类别标识符（小写）→ (ocr.USER_MSG_*, 默认标题)
    ('csv_encoding', r'csv.{0,5}编码|utf-?8|gbk', 'USER_MSG_CSV_ENCODING', '导入失败'),
    ('xlsx_corrupt', r'xlsx.{0,8}(损坏|不识别|格式)|openpyxl|zip', 'USER_MSG_XLSX_CORRUPT', '导入失败'),
    ('import_too_large', r'数据行.{0,10}超过上限|10000', 'USER_MSG_IMPORT_TOO_LARGE', '导入失败'),
    ('legacy_xls', r'\.xls.{0,20}不支持|老格式', 'USER_MSG_LEGACY_XLS', '导入失败'),
    ('mapping_missing', r'列映射不完整|缺关键字段', 'USER_MSG_MAPPING_MISSING', '导入失败'),
    # v1.5.11 新增分类（顺序敏感）：model_not_found 必须先于 fatal_quota——
    # 1211 InvalidEndpointOrModel.NotFound 等"模型名打错"错误若落进 quota 正则会误导用户查余额。
    ('model_not_found', r'invalid\s*(endpoint|model)|invalidparam|1211|模型不存在|不存在的模型|model.{0,20}not\s*found',
     'USER_MSG_MODEL_NOT_FOUND', '模型无效'),
    ('dual_config_invalid', r'主副模型相同|OCR专用模型|双模型验证无意义|副模型.{0,12}(无效|OCR专用)',
     'USER_MSG_DUAL_CONFIG', '双模型配置'),
    ('api_unreachable', r'connection\s*(refused|error)|网络|proxy|dns|ssl|certificate|unreachable|timed out connecting',
     'USER_MSG_API_UNREACHABLE', '网络连接失败'),
    ('no_table_detected', r'未识别到表格|未识别到商品|未识别到任何数据',
     'USER_MSG_NO_TABLE', '未识别到表格'),
    ('api_key_missing', r'api\s*key.{0,8}未设置|api_key.{0,8}未设置', 'USER_MSG_API_KEY_MISSING', 'API 未配置'),
    ('fatal_quota', r'quota.{0,10}(exhaust|exceed)|insufficient.{0,5}quota|余额不足|403|unauthorized|invalid_api_key',
     'USER_MSG_FATAL_QUOTA', 'API 权限或额度'),
    ('api_timeout', r'timeout|timed?\s*out|read timed', 'USER_MSG_API_TIMEOUT', '识别失败'),
    ('json_parse', r'json|无法解析的内容|截断', 'USER_MSG_JSON_PARSE_FAIL', '识别失败'),
    ('no_model', r'没有可用的识别模型|未配置可用的识别模型', 'USER_MSG_NO_MODEL_AVAILABLE', '识别失败'),
    ('blur_ocr', r'截图模糊|图片模糊', 'USER_MSG_BLUR', '识别失败'),
    ('low_confidence', r'低置信|low_confidence|name_unmatched|dual_degraded',
     'USER_MSG_LOW_CONFIDENCE', '识别结果需复核'),
)


def categorize_error(exc: BaseException | str) -> Tuple[str, str, str]:
    """把异常归类为 (category, user_message, title)。

    Args:
        exc: Exception 实例或 str（异常 message）。

    Returns:
        (category, user_message, title) —— category 是 _ERROR_PATTERNS 里的 key
        或 'unknown'；user_message 来自 ocr.USER_MSG_*；title 用于弹窗标题。

    设计：所有归类失败 → 返回 ('unknown', '识别或导入过程中出现异常，请重试或检查文件后重试', '出错')。
    fail-safe 兜底，绝不抛错阻塞 GUI 弹窗。
    """
    try:
        msg = str(exc) if exc is not None else ''
    except Exception:
        msg = ''
    if not msg:
        return ('unknown', '识别或导入过程中出现异常，请重试或检查文件后重试', '出错')
    # 优先匹配更长的 key 写在前的（_ERROR_PATTERNS 已经是这个顺序）
    for cat, pat, const_name, default_title in _ERROR_PATTERNS:
        try:
            if _re.search(pat, msg, _re.IGNORECASE):
                # 动态取 ocr.USER_MSG_*；缺则用 default_title
                try:
                    import ocr as _ocr
                    user_msg = getattr(_ocr, const_name, msg)
                except Exception:
                    user_msg = msg
                return (cat, user_msg, default_title)
        except Exception:
            continue
    return ('unknown', '识别或导入过程中出现异常，请重试或检查文件后重试', '出错')


def _row_confidence_level(item) -> str:
    """从 item 读 confidence.level；兼容 confidence 字段缺失的老路径。"""
    if not isinstance(item, dict):
        return 'high'
    c = item.get('confidence')
    if isinstance(c, dict) and c.get('level') in ('high', 'medium', 'low'):
        return c['level']
    # 兜底：基于旧 _low_confidence / _name_unmatched / _missing_id 等标记判定
    if item.get('_low_confidence') or item.get('_name_unmatched') or item.get('_dual_degraded'):
        return 'low'
    if item.get('_missing_id'):
        return 'medium'
    return 'high'


def _row_audit_issues(item: dict) -> List[Tuple[str, str]]:
    """从 confidence.reasons 提取可展示的(field, reason) 对（纯展示用）。"""
    c = item.get('confidence')
    if not (isinstance(c, dict) and isinstance(c.get('reasons'), list)):
        return []
    out = []
    for r in c['reasons']:
        if not isinstance(r, str):
            continue
        # 形式：'数字异常(stock):销量>0 但库存=0' / '图片模糊(Laplacian方差=...)' / 双模型差异...
        if r.startswith('数字异常(') and '):' in r:
            f, reason = r[len('数字异常('):].split('):', 1)
            out.append((f.strip(), reason.strip()))
        else:
            out.append(('overall', r))
    return out


def summarize_review(items: List[dict]) -> Dict[str, int]:
    """统计 confidence 分布，供弹窗顶部展示。

    Returns: {'total': N, 'high': x, 'medium': y, 'low': z, 'need_review': z + y>0}
    v1.6.0 TC-Q2 增补：存在任何信号时额外携带 'signals': {信号名: 计数}；
    **无信号时不携带该键**（向后兼容 test_review_flow 的整字典等值断言——
    空档形状与 v1.5.13 完全一致）。消费方用 s.get('signals') or {} 读取。
    非 dict 元素计入 total 但不参与高/中/低分类（与 build_review_list 行为一致）。
    """
    if not items:
        return {'total': 0, 'high': 0, 'medium': 0, 'low': 0, 'need_review': 0}
    high = med = low = 0
    sig_counts: Dict[str, int] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        lv = _row_confidence_level(it)
        if lv == 'high':
            high += 1
        elif lv == 'medium':
            med += 1
        else:
            low += 1
        for s in _row_signals(it):
            sig_counts[s] = sig_counts.get(s, 0) + 1
    out = {
        'total': len(items),
        'high': high,
        'medium': med,
        'low': low,
        'need_review': low + med,
    }
    if sig_counts:
        out['signals'] = sig_counts
    return out


def build_review_list(items: List[dict]) -> List[Dict[str, Any]]:
    """抽取需要复核的行（low 优先级，其次 medium）。

    返回的每条 = {
      'index': items 里的下标,
      'name': str,           # 商品名（截断 40 字）
      'field': str,          # 异常字段（stock / sales / overall / name）
      'reason': str,         # 人类可读原因
      'raw': str,            # 原文（来自 _raw[field]，缺失则空串）
      'parsed': int,         # 解析值
      'level': 'low' | 'medium',
      'all_reasons': [str]   # 该行全部 reasons 列表
    }

    Args:
        items: parse_items_generic 产出（已注入 confidence 元数据）。

    Returns:
        按 (level asc: low 在前, index asc) 排序的复核行列表。
        无异常 → []。
    """
    if not items:
        return []
    out = []
    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        lv = _row_confidence_level(it)
        if lv == 'high':
            continue
        # 把该行的全部原因汇总成单行复核条目（多原因合并到 reason 用 '；' 隔开）
        issues = _row_audit_issues(it)
        if not issues:
            # 兜底：有 level 但 reasons 缺失（说明 t3 未注入）→ 用旧标记
            if it.get('_low_confidence'):
                issues = [('overall', '双模型结果差异较大或 name 配对异常')]
            elif it.get('_name_unmatched'):
                issues = [('name', '主副模型商品名未能配对')]
            elif it.get('_dual_degraded'):
                issues = [('overall', '双模型校验未生效（副模型失败）')]
            elif it.get('_missing_id'):
                issues = [('overall', '缺少商品 ID，依赖 name 模糊匹配可能误并')]
            else:
                issues = [('overall', '识别置信度低')]
        # 取主字段（第一个 issue 的 field）
        primary_field = issues[0][0] if issues else 'overall'
        primary_reason = '；'.join(r for (_f, r) in issues) if issues else '识别置信度低'
        # raw / parsed 取主字段（不跨字段 fallback —— primary_field 决定主语料）
        _raw = it.get('_raw') or {}
        if not isinstance(_raw, dict):
            _raw = {}
        if primary_field in ('stock', 'sales'):
            raw_val = _raw.get(primary_field, '')
        else:
            # overall/name 等无单一字段的：取主字段之外的常用字段
            raw_val = _raw.get('stock', '') or _raw.get('sales', '') or ''
        if raw_val is None:
            raw_val = ''
        raw_str = str(raw_val)[:60]
        # parsed
        if primary_field in ('stock', 'sales'):
            try:
                parsed = int(it.get(primary_field, 0) or 0)
            except Exception:
                parsed = 0
        else:
            parsed = 0
        name = str(it.get('name', '') or '').strip()
        if len(name) > 40:
            name = name[:39] + '…'
        out.append({
            'index': idx,
            'name': name,
            'field': primary_field,
            'reason': primary_reason,
            'raw': raw_str,
            'parsed': parsed,
            'level': lv,
            'signal': (_row_signals(it) or ['ok'])[0],   # v1.6.0 TC-Q2：主信号名（无信号='ok'）
            'all_reasons': [r for (_f, r) in issues],
        })
    # 排序：low 优先，按 index 升序
    out.sort(key=lambda r: (0 if r['level'] == 'low' else 1, r['index']))
    return out


def apply_user_edits(items: List[dict], edits: List[Dict[str, Any]]) -> List[dict]:
    """把用户在复核弹窗里改的字段写回 items（in-place）。

    Args:
        items: 原始 items 列表（通常是 build_review_list 的来源）。
        edits: 每条 = {'index': int, 'field': 'stock'|'sales'|'name', 'value': Any}
            index 必须指向 items 里的下标；field 限定白名单（防越权改内部 _raw）。
            解析失败 → 该 edit 跳过（不抛）。

    Returns:
        同一 items 引用（链式友好）。空 edits → 直接返回原 items。

    设计：仅修改白名单字段（stock/sales/name），不改 _raw 内部缓存（避免破坏
    后续 _raw[field] 引用）。若需要改 raw，调用方应自行处理。
    """
    if not items or not edits:
        return items
    # R2 BUG-4 修复（t1 BUG-4 / 上期 P3 ③）：白名单扩 region/warehouse。
    # 复核弹窗里用户可改识别错的销售区域/仓库，避免改 stock/sales 后仍按
    # 错区域查时效的逻辑漏洞。数字字段强制 int；文本字段（name/region/
    # warehouse）保留原值转 str。qty 仍不在白名单（其由公式计算得出）。
    allowed = ('stock', 'sales', 'name', 'region', 'warehouse')
    for ed in edits:
        if not isinstance(ed, dict):
            continue
        idx = ed.get('index')
        field = ed.get('field')
        val = ed.get('value')
        if not isinstance(idx, int) or field not in allowed:
            continue
        if idx < 0 or idx >= len(items):
            continue
        it = items[idx]
        if not isinstance(it, dict):
            continue
        if field in ('stock', 'sales'):
            try:
                it[field] = int(val) if val not in (None, '') else 0
            except (ValueError, TypeError):
                continue  # 解析失败跳过该 edit
        else:
            it[field] = str(val) if val is not None else ''
    return items


def has_low_confidence(items: List[dict]) -> bool:
    """快路径：是否存在任一 low 行（用于 _fill_from_ocr 决定是否弹窗）。"""
    if not items:
        return False
    for it in items:
        if not isinstance(it, dict):
            continue
        if _row_confidence_level(it) == 'low':
            return True
    return False


def has_review_items(items: List[dict]) -> bool:
    """是否存在需要复核的行（low 或 medium）。"""
    if not items:
        return False
    for it in items:
        if not isinstance(it, dict):
            continue
        lv = _row_confidence_level(it)
        if lv in ('low', 'medium'):
            return True
    return False


# ══════════════════════════════════════════════════════════════════
# v1.6.0 TC-Q2（§5.2 核心卡 / §1.2 WS-Q2）：三色闸门 + 补货安全闸
#
# 设计红线：
# - 纯函数、零 Tk 依赖（延续本模块铁律 ④）；
# - **算后覆盖**——apply_safety_gate 只改 plan 的 qty/status/color/trust/signal
#   五个键，绝不进入任何补货公式分支（宪法 §7 经典公式冻结）；
# - RED 禁自动补货 = qty 强制 0（宪法 §4：不替用户做不可逆动作；
#   用户仍可手动改表/导出——闸门拦的是"自动建议值"不是用户操作）；
# - NO_DATA 不等于 RED：数据不足只是降级提醒（⚠前缀），不替用户清零。
# ══════════════════════════════════════════════════════════════════

def _row_signals(item) -> List[str]:
    """提取一行 ITEM 的稳定信号名清单（§1.2 WS-Q2 信号矩阵，去重保序）。

    信号名（稳定契约，供 tests/TC-Q3 消费）：
      dual_mismatch        双模型结果差异大（_low_confidence / reasons 双模型差异）
      name_unmatched       主副 name 配对失败
      dual_degraded        副模型失败降级单模型
      missing_id           缺商品 ID（依赖 name 模糊匹配风险）
      numeric_anomaly      数字异常审计命中（销量>0库存=0 / >999999 / 负数 / 残留噪音）
      blur                 图片模糊（Laplacian 方差低于阈值）
      row_count_mismatch   行数与页面总数不一致
      thin_history         历史样本不足
      no_history           补货计算侧无历史信号（item['signal']==NO_DATA_SIGNAL）
      fallback_error       补货计算侧异常回退信号
      other                其他未归类 reason（兜底，不丢信息）
    """
    if not isinstance(item, dict):
        return []
    out: List[str] = []
    c = item.get('confidence')
    reasons = c.get('reasons') if isinstance(c, dict) else None
    for r in (reasons or []):
        if not isinstance(r, str):
            continue
        if r.startswith('数字异常'):
            out.append('numeric_anomaly')
        elif '模糊' in r:
            out.append('blur')
        elif '行数' in r:
            out.append('row_count_mismatch')
        elif ('历史' in r) or ('样本' in r):
            out.append('thin_history')
        elif '双模型' in r or '差异' in r:
            out.append('dual_mismatch')
        else:
            out.append('other')
    # 旧标记兜底（confidence 引擎未注入的老路径，§1.2 :97-100）
    if item.get('_low_confidence') and 'dual_mismatch' not in out:
        out.append('dual_mismatch')
    if item.get('_name_unmatched') and 'name_unmatched' not in out:
        out.append('name_unmatched')
    if item.get('_dual_degraded') and 'dual_degraded' not in out:
        out.append('dual_degraded')
    if item.get('_missing_id') and 'missing_id' not in out:
        out.append('missing_id')
    # 补货计算侧信号（utils.calc_replenishment 派发器注入 item/plan 的 signal）
    sig = str(item.get('signal') or '')
    if sig == 'no_history' and 'no_history' not in out:
        out.append('no_history')
    elif sig == 'fallback_error' and 'fallback_error' not in out:
        out.append('fallback_error')
    # 去重保序（同签多 reason，如两字段各自数字异常 → 一个 numeric_anomaly）
    return list(dict.fromkeys(out))


def trust_level(item) -> str:
    """三色可信度（TC-Q2 纯函数，§1.2 实现点设计原文）。

    映射：confidence.level 'low'→'RED'、'medium'→'YELLOW'、'high'→'GREEN'；
    confidence 缺失的老路径按 _row_confidence_level（:90）的同款兜底判定。
    NO_DATA 不在这里返回——它由 plan['signal']=='no_history' 表达（四态之二值分离：
    trust 描述"识别可信度"，signal 描述"计算数据充分性"）。

    Returns: 'GREEN' | 'YELLOW' | 'RED'（任何输入都不抛——非 dict → 'GREEN'）
    """
    try:
        lv = _row_confidence_level(item)
    except Exception:
        return 'GREEN'
    if lv == 'low':
        return 'RED'
    if lv == 'medium':
        return 'YELLOW'
    return 'GREEN'


def _no_data_signal_const() -> str:
    """读 utils.NO_DATA_SIGNAL（懒加载防环；异常回退字面量，语义不漂移）。"""
    try:
        from utils import NO_DATA_SIGNAL
        return NO_DATA_SIGNAL
    except Exception:
        return 'no_history'


_STATUS_PREFIX_RED = '⛔高风险·建议人工核对'
_STATUS_PREFIX_ND = '⚠数据不足'
_STATUS_PREFIX_FE = '⚠降级计算'   # CROSS#2 发布前修复：fallback_error 显式标注


def _strip_gate_prefixes(status: str) -> str:
    """剥旧闸门前缀（幂等）：重复过闸 / RED 接管 NO_DATA 行时，⛔ 与 ⚠ 不叠加。

    CROSS#2（v1.6.0 发布前修复批次）：纳入降级计算前缀（⚠降级计算），
    保证 RED 接管 NO_DATA / FE 行时也不叠加。
    """
    s = status
    changed = True
    while changed and s:
        changed = False
        for p in (_STATUS_PREFIX_RED, _STATUS_PREFIX_ND, _STATUS_PREFIX_FE):
            if s.startswith(p):
                s = s[len(p):]
                changed = True
        if s.startswith('｜'):
            s = s[1:]
            changed = True
    return s


def apply_safety_gate(plan: dict, item: dict = None) -> dict:
    """补货安全闸（TC-Q2 收口①，纯函数）：对**已算完**的 plan 做算后覆盖。

    规则（§5.2 TC-Q2 卡）：
    - trust==RED  → qty 强制 0 + status 前缀「⛔高风险·建议人工核对」+ color='red'；
    - signal==no_history（NO_DATA）→ status 前缀「⚠数据不足」（qty 不动——数据不足
      只降级提醒，不替用户清零；两者同现时 RED 优先，⛔ 与 ⚠ 语义不叠加）；
    - signal==fallback_error → status 前缀「⚠降级计算」（v1.6.0 CROSS#2：补货算法
      异常回退经典——仅标注降级来源，不动 qty/color，便于人工追因）；
    - YELLOW/GREEN → qty/status 原样（YELLOW 的计数提醒由调用方聚合处理）。

    signal 判定来源优先级：plan['model'] 标注（'classic(no_history)' /
    'classic(error)'，gui 三模式共用）> item['signal']（utils 派发器路径）。

    Args:
        plan: _calc_from_items / calc_replenishment 产出的 plan dict（就地修改并返回）。
        item: 对应 OCR item（trust_level 消费其 confidence/旧标记）；None 时仅按
              plan['model'] 判 signal，trust 默认 GREEN（纯计算路径无识别信号）。

    Returns:
        同一 plan 引用（trust/signal/status/qty/color 已按规则更新）。
    """
    if not isinstance(plan, dict):
        return plan
    it = item if isinstance(item, dict) else {}
    trust = trust_level(it) if it else 'GREEN'
    model_tag = str(plan.get('model') or '')
    nd = _no_data_signal_const()
    if 'no_history' in model_tag:
        sig = nd
    elif 'error' in model_tag:
        sig = 'fallback_error'
    else:
        sig = str(it.get('signal') or 'ok')
    plan['trust'] = trust
    plan['signal'] = sig
    status = str(plan.get('status') or '')
    if trust == 'RED':
        plan['qty'] = 0
        plan['color'] = 'red'
        core = _strip_gate_prefixes(status)   # RED 接管：剥旧 ⚠/⛔ 前缀，不叠加
        plan['status'] = (_STATUS_PREFIX_RED + '｜' + core) if core else _STATUS_PREFIX_RED
    elif sig == nd:
        core = _strip_gate_prefixes(status)   # 幂等：已带 ⚠ 前缀不重复叠
        plan['status'] = (_STATUS_PREFIX_ND + '｜' + core) if core else _STATUS_PREFIX_ND
    elif sig == 'fallback_error':
        # CROSS#2 发布前修复：fallback_error（补货算法降级）→ 显式 ⚠降级计算 前缀
        # 不动 qty/color（仅标注降级来源，便于人工追因），与 NO_DATA 同款幂等约定。
        core = _strip_gate_prefixes(status)
        plan['status'] = (_STATUS_PREFIX_FE + '｜' + core) if core else _STATUS_PREFIX_FE
    return plan


def trust_summary(plans: List[dict]) -> Dict[str, int]:
    """聚合 plans 的可信度计数（TC-Q3 自检报告 / gui 状态栏提醒共用）。

    Returns: {'total': N, 'green': g, 'yellow': y, 'red': r, 'no_data': d}
    """
    return {
        'total': len(plans or []),
        'green': sum(1 for p in (plans or []) if isinstance(p, dict) and p.get('trust') == 'GREEN'),
        'yellow': sum(1 for p in (plans or []) if isinstance(p, dict) and p.get('trust') == 'YELLOW'),
        'red': sum(1 for p in (plans or []) if isinstance(p, dict) and p.get('trust') == 'RED'),
        'no_data': sum(1 for p in (plans or []) if isinstance(p, dict)
                       and p.get('signal') == _no_data_signal_const()),
    }
