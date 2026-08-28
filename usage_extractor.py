"""
PDD EZ — WS-C 用量形态抽取层（UsageExtractor，v1.4.7）

实现规格：docs/USAGE_ARCHIVE_SPEC.md（v0.2）。
本模块提供：
  - extract(data, provider, api_type) -> dict | None
      6 步降级链，函数体全 try/except 兜底，绝不外抛。
  - estimate_fallback(content, prompt, provider, max_side, max_tok) -> dict
      §3 兜底估算（混合启发式 + 图片 max_side² 折算 + 极端失败 fallback_max_tok）。
  - compute_cost(usage, pricing_entry) -> float
      §5.3 费用计算；pricing 缺失返回 0.0（面板显示 ?）。
  - _debug_dump_response(provider, model, endpoint, data, usage_extracted)
      §11.2 debug 落盘（受 usage.debug_archive_enabled 控制，默认 False）。
  - resolve_api_type(endpoint, custom_endpoint=None) -> str
      §2.4 endpoint URL → api_type 推断（custom_endpoint 一律 chat.completions）。

铁律（与宪法 §4 失败哲学一致）：
  - 任何路径异常吞掉，不外抛。
  - 禁 tiktoken / 禁联网 / 禁内置默认价 / 禁真实 API 请求。
"""

import os
import json
import time
import datetime

from utils import get_base_dir


# ──────────────────────────────────────────────────────────────────
# §2.4 API 类型推断（endpoint URL 规则）
# ──────────────────────────────────────────────────────────────────

# 合法 api_type 集合（白名单）：不在集合内的值视为 chat.completions 兜底
_API_TYPES = ('chat.completions', 'responses', 'multimodal-generation')

# URL 子串 → api_type 映射（按 SPEC §2.4 表格）
_API_TYPE_RULES = (
    ('/multimodal-generation', 'multimodal-generation'),
    ('/api/v1/services/aigc/multimodal-generation', 'multimodal-generation'),
    ('/responses', 'responses'),
    ('/chat/completions', 'chat.completions'),
    ('/paas/v4/chat/completions', 'chat.completions'),
)


def resolve_api_type(endpoint: str, custom_endpoint: str = None) -> str:
    """按 endpoint URL 推断 api_type；custom_endpoint 一律 chat.completions 兜底。

    实现要点（SPEC §2.4）：
      - 含 /chat/completions 或 /paas/v4/chat/completions → chat.completions
      - 含 /responses → responses
      - 含 /multimodal-generation 或 /api/v1/services/aigc/multimodal-generation → multimodal-generation
      - 其余（含 custom_endpoint ep-xxx）→ chat.completions（保守，不尝试 multimodal）
    """
    try:
        if custom_endpoint:
            # custom_endpoint 形态太多，不靠子串模糊判断——一律 chat.completions
            return 'chat.completions'
        ep = (endpoint or '').lower()
        for needle, api_type in _API_TYPE_RULES:
            if needle in ep:
                return api_type
        return 'chat.completions'
    except Exception:
        return 'chat.completions'


# ──────────────────────────────────────────────────────────────────
# §2.1 extract：6 步降级链 + _pack total 自校验
# ──────────────────────────────────────────────────────────────────

def _to_int(v) -> int:
    """安全转 int；任何异常返回 0（不外抛）。"""
    try:
        if v is None:
            return 0
        if isinstance(v, bool):
            return int(v)
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return 0


def _pack(prompt: int, completion: int, total: int, image, source: str) -> dict:
    """统一出口：total 自校验（缺失/小于求和则用求和补齐）。"""
    try:
        p = _to_int(prompt)
        c = _to_int(completion)
        t = _to_int(total)
        if not t or t < p + c:
            t = p + c
        return {
            'prompt': p,
            'completion': c,
            'total': t,
            'image_tokens': image,
            'source': source,
        }
    except Exception:
        # 极端兜底：任何异常都构造出最小合法 dict
        return {'prompt': 0, 'completion': 0, 'total': 0, 'image_tokens': None, 'source': source}


def extract(data, provider: str, api_type: str):
    """从已 JSON 解析的响应 dict 抽取 usage 形态；失败返 None。

    6 步降级链（SPEC §2.1）：
      1) data.usage 含 prompt/completion 命名（OpenAI 兼容 chat.completions）
      2) data.usage 含 input/output 命名（Responses API 顶层）
      3) data.output[-1].usage（doubao responses 端点历史怪癖）
      4) data.usage 含 image_tokens（qwen multimodal-generation OCR 专项）
      5) 自校验失败（total=0 但 prompt+completion>0）—— 上面 4 步任一命中后
         _pack 内部已做自校验
      6) 返回 None，调用方走 §3 兜底估算

    返回：
      None  —— 6 步全部失败
      dict   —— 固定 5 字段 schema（prompt/completion/total/image_tokens/source）
    """
    if not isinstance(data, dict):
        return None
    try:
        u = data.get('usage')
    except Exception:
        return None

    # ── 步 1：标准 OpenAI 兼容 chat.completions（prompt/completion/total）──
    try:
        if isinstance(u, dict) and ('prompt_tokens' in u or 'completion_tokens' in u):
            pt = _to_int(u.get('prompt_tokens'))
            ct = _to_int(u.get('completion_tokens'))
            tt = _to_int(u.get('total_tokens'))
            if pt or ct:
                return _pack(pt, ct, tt, image=None, source='data.usage')
    except Exception:
        pass

    # ── 步 2：Responses API 顶层（input/output 命名）──
    try:
        if isinstance(u, dict) and ('input_tokens' in u or 'output_tokens' in u):
            pt = _to_int(u.get('input_tokens'))
            ct = _to_int(u.get('output_tokens'))
            tt = _to_int(u.get('total_tokens'))
            if pt or ct:
                return _pack(pt, ct, tt, image=None, source='data.usage')
    except Exception:
        pass

    # ── 步 3：Doubao responses 兜底：data.output[-1].usage ──
    try:
        out = data.get('output') or []
        if out and isinstance(out, list) and isinstance(out[-1], dict):
            u2 = out[-1].get('usage')
            if isinstance(u2, dict) and ('input_tokens' in u2 or 'output_tokens' in u2):
                pt = _to_int(u2.get('input_tokens'))
                ct = _to_int(u2.get('output_tokens'))
                tt = _to_int(u2.get('total_tokens'))
                if pt or ct:
                    return _pack(pt, ct, tt, image=None, source='data.output[-1].usage')
    except Exception:
        pass

    # ── 步 4：Qwen multimodal-generation 专项：image_tokens ──
    try:
        if isinstance(u, dict) and 'image_tokens' in u:
            it = _to_int(u.get('image_tokens'))
            ct = _to_int(u.get('output_tokens')) or _to_int(u.get('completion_tokens'))
            tt = _to_int(u.get('total_tokens'))
            if it or ct:
                # multimodal 下 prompt ≈ image_tokens（OCR 场景文本极少）
                return _pack(prompt=it, completion=ct, total=tt, image=it, source='data.usage.multimodal')
    except Exception:
        pass

    # ── 步 5/6：全失败 → None（调用方走 §3 兜底估算）──
    return None


# ──────────────────────────────────────────────────────────────────
# §3 兜底估算（estimate_fallback）
# ──────────────────────────────────────────────────────────────────

# 经验常数（SPEC §3.2）
_IMG_TOKEN_PER_PX = {
    'doubao': 1.0,
    'qwen': 1.0,
    'glm': 0.5,  # 智谱历史定价更便宜
}


def _count_chinese_alnum_other(s: str):
    """统计字符串中的中文字符数 / 字母数字字符数 / 其他字符数。
    中文按 Unicode CJK 统一汉字区段统计；字母数字按 isalnum() & ascii。
    """
    chinese = 0
    alnum = 0
    if not s:
        return 0, 0, 0
    for c in s:
        o = ord(c)
        if 0x4E00 <= o <= 0x9FFF:
            chinese += 1
        elif c.isalnum() and c.isascii():
            alnum += 1
    other = len(s) - chinese - alnum
    return chinese, alnum, other


def _estimate_completion_from_text(text: str) -> int:
    """基于正文长度的混合启发式分词估算（SPEC §3.2）。
    公式：中文×2 + 字母数字×0.25 + 其他×1，再 × 1.25 安全冗余。
    空串 → 0。
    """
    if not text:
        return 0
    try:
        chinese, alnum, other = _count_chinese_alnum_other(text)
        est = (chinese * 2 + alnum * 0.25 + other * 1) * 1.25
        return int(round(est))
    except Exception:
        return 0


def _estimate_prompt_text(prompt: str) -> int:
    """纯文本 prompt 的 token 估算（不含图片）。"""
    return _estimate_completion_from_text(prompt)


def _estimate_image_tokens(provider: str, max_side: int) -> int:
    """按 max_side 估算图片 token（保守不细分 image/text）。
    公式：max_side² × px_per_token / 1024（1024 px ≈ 1024 token）。
    """
    try:
        px = int(max_side or 0)
        if px <= 0:
            return 0
        px_per_token = _IMG_TOKEN_PER_PX.get(provider or '', 1.0)
        return int(px * px * px_per_token / 1024)
    except Exception:
        return 0


def estimate_fallback(content: str, prompt: str, provider: str,
                      max_side: int = 0, max_tok: int = 0) -> dict:
    """§3 兜底估算入口。

    参数：
      content  - 模型返回的正文（已截断后的实际内容）；None/空也算"极端失败"
      prompt   - 发送的提示词文本
      provider - doubao / qwen / glm
      max_side - 图片最大边（OCR 漏斗通常 1920/2560）
      max_tok  - 请求的 max_tokens

    返回 dict（统一 5 字段 schema，source='fallback'，is_estimate=True 由调用方打标）：
      - 正常情况：source='fallback'，prompt≈text+image，completion≈text
      - 极端失败（content 为空等）：source='fallback_max_tok'，total=max_tok
    """
    try:
        c = _estimate_completion_from_text(content or '')
        p_text = _estimate_prompt_text(prompt or '')
        p_img = _estimate_image_tokens(provider or '', max_side or 0)
        prompt_est = p_text + p_img
        total_est = prompt_est + c

        # 极端失败：正文空 + 文本 prompt 也没值 + max_tok 也 0 → 无任何信号
        if not c and not prompt_est and not (max_tok or 0):
            return {
                'prompt': 0,
                'completion': 0,
                'total': 0,
                'image_tokens': None,
                'source': 'fallback_max_tok',
            }
        return {
            'prompt': prompt_est,
            'completion': c,
            'total': total_est,
            'image_tokens': p_img if p_img else None,
            'source': 'fallback',
        }
    except Exception:
        # 最末兜底：max_tok 当 completion 上限（保证 jsonl 不丢行）
        try:
            mt = int(max_tok or 0)
        except Exception:
            mt = 0
        return {
            'prompt': 0,
            'completion': 0,
            'total': mt,
            'image_tokens': None,
            'source': 'fallback_max_tok',
        }


# ──────────────────────────────────────────────────────────────────
# §5.3 费用计算 compute_cost
# ──────────────────────────────────────────────────────────────────

def compute_cost(usage, pricing_entry) -> float:
    """按 §5.3 计算 cost_cny（元）；pricing 缺失返回 0.0（面板显示 ?）。

    公式：
      cost = prompt / 1e6 * input_per_million
           + completion / 1e6 * output_per_million
           + (image_per_call if usage.image_tokens 存在)
      round(cost, 4) 四位小数

    异常吞掉，价格字符串异常按 0.0 算（符合 §6.2 表格）。
    """
    try:
        if not usage or not pricing_entry:
            return 0.0
        if not isinstance(usage, dict) or not isinstance(pricing_entry, dict):
            return 0.0

        try:
            pt = float(usage.get('prompt') or 0)
        except Exception:
            pt = 0.0
        try:
            ct = float(usage.get('completion') or 0)
        except Exception:
            ct = 0.0

        cost = 0.0
        try:
            inp = pricing_entry.get('input_per_million')
            if inp:
                cost += pt / 1_000_000.0 * float(inp)
        except Exception:
            pass
        try:
            outp = pricing_entry.get('output_per_million')
            if outp:
                cost += ct / 1_000_000.0 * float(outp)
        except Exception:
            pass
        try:
            ipc = pricing_entry.get('image_per_call')
            if ipc and usage.get('image_tokens'):
                cost += float(ipc)  # 按张计（OCR 一次调用一张图）
        except Exception:
            pass
        return round(cost, 4)
    except Exception:
        return 0.0


# ──────────────────────────────────────────────────────────────────
# §11.2 Debug 落盘钩子（_debug_dump_response）
# ──────────────────────────────────────────────────────────────────

_USAGE_CFG_CACHE = {'enabled': None, 'debug': None}


def _load_usage_cfg():
    """懒加载 settings.json.usage 节点（避免每次调用都 IO）。
    enabled 默认 True（usage 整链默认开），debug_archive_enabled 默认 False。
    缺 key 时给默认值。
    """
    if _USAGE_CFG_CACHE['enabled'] is not None and _USAGE_CFG_CACHE['debug'] is not None:
        return _USAGE_CFG_CACHE
    try:
        # v1.4.7 P3-R2-L3：删死 import `from utils import get_api_config`（仅用于
        # 触发 utils 加载但实际未调用——utils 在模块顶部已 import，副作用重复；删之）
        cfg_path = os.path.join(get_base_dir(), 'settings.json')
        enabled = True
        debug = False
        if os.path.exists(cfg_path):
            with open(cfg_path, 'r', encoding='utf-8') as _f:
                _raw = json.load(_f)
            if isinstance(_raw, dict):
                u = _raw.get('usage')
                if isinstance(u, dict):
                    if 'enabled' in u:
                        enabled = bool(u.get('enabled'))
                    if 'debug_archive_enabled' in u:
                        debug = bool(u.get('debug_archive_enabled'))
        _USAGE_CFG_CACHE['enabled'] = enabled
        _USAGE_CFG_CACHE['debug'] = debug
    except Exception:
        # 任何异常：保持默认（enabled=True, debug=False；落账照常，写 debug 跳过）
        _USAGE_CFG_CACHE['enabled'] = True
        _USAGE_CFG_CACHE['debug'] = False
    return _USAGE_CFG_CACHE


def reset_usage_cfg_cache():
    """测试 / 设置更新后清空缓存（保证下次重新读 settings.json）。"""
    _USAGE_CFG_CACHE['enabled'] = None
    _USAGE_CFG_CACHE['debug'] = None


def _debug_dump_response(provider: str, model: str, endpoint: str,
                         data, usage_extracted) -> None:
    """§11.2 落盘真实响应原文 + 抽取结果。

    行为契约：
      - usage.debug_archive_enabled=False（默认）→ 直接 return，不写盘
      - enabled=False → 也直接 return（usage 整链关时绝不留痕）
      - 写盘路径：<get_base_dir()>/output/usage_archive/<ts>_<provider>_<model>_<source>.json
      - 文件名 model 中的 / 与 : 替换为 _（防子目录注入）
      - 7 天自动清理：启动 / 调用时扫一遍
      - 任何异常吞掉，绝不外抛
    """
    try:
        cfg = _load_usage_cfg()
        if not cfg.get('enabled', True):
            return
        if not cfg.get('debug', False):
            return
        archive_dir = os.path.join(get_base_dir(), 'output', 'usage_archive')
        try:
            os.makedirs(archive_dir, exist_ok=True)
        except Exception:
            return
        # 7 天清理（启动期与每次写入都做；目录大也不卡——os.scandir 一次性）
        try:
            now = time.time()
            for _fn in os.listdir(archive_dir):
                _fp = os.path.join(archive_dir, _fn)
                try:
                    if os.path.isfile(_fp) and (now - os.path.getmtime(_fp)) > 7 * 86400:
                        os.remove(_fp)
                except Exception:
                    continue
        except Exception:
            pass

        ts = time.strftime('%Y%m%d_%H%M%S')
        try:
            safe_model = str(model or 'unknown').replace('/', '_').replace(':', '_')
        except Exception:
            safe_model = 'unknown'
        try:
            source = 'none'
            if isinstance(usage_extracted, dict):
                source = str(usage_extracted.get('source') or 'none')
        except Exception:
            source = 'none'
        fname = '{}_{}_{}_{}.json'.format(ts, str(provider or 'unk'), safe_model, source)
        record = {
            'ts': ts,
            'provider': str(provider or ''),
            'model': str(model or ''),
            'endpoint': str(endpoint or ''),
            'usage_extracted': usage_extracted,
            'raw_response': data,
        }
        with open(os.path.join(archive_dir, fname), 'w', encoding='utf-8') as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except Exception:
        # §6.2 失败安全：绝不外抛
        pass


# ──────────────────────────────────────────────────────────────────
# §5.1 / §11 工具函数（供 usage_store 调用）
# ──────────────────────────────────────────────────────────────────

def now_iso() -> str:
    """返回本地时间 ISO 8601 字符串（含时区偏移，§5.1 示例格式）。"""
    try:
        return datetime.datetime.now().astimezone().isoformat(timespec='milliseconds')
    except Exception:
        try:
            return datetime.datetime.now().isoformat()
        except Exception:
            return ''
