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
    非 dict 元素计入 total 但不参与高/中/低分类（与 build_review_list 行为一致）。
    """
    if not items:
        return {'total': 0, 'high': 0, 'medium': 0, 'low': 0, 'need_review': 0}
    high = med = low = 0
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
    return {
        'total': len(items),
        'high': high,
        'medium': med,
        'low': low,
        'need_review': low + med,
    }


def build_review_list(items: List[dict]) -> List[Dict[str, Any]]:
    """抽取需要复核的行（low 优先级，其次 medium）。

    返回的每条 = {
      'index': items 里的下标,
      'name': str,  # 商品名（截断 40 字）
      'field': str,  # 异常字段（stock / sales / overall / name）
      'reason': str,  # 人类可读原因
      'raw': str,  # 原文（来自 _raw[field]，缺失则空串）
      'parsed': int,  # 解析值
      'level': 'low' | 'medium',
      'all_reasons': [str]  # 该行全部 reasons 列表
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
            # 兜底：有 level 但 reasons 缺失（说明 未注入）→ 用旧标记
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
    # R2 问题 修复：白名单扩 region/warehouse。
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
