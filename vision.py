"""
纯视觉无侵入识别引擎 (MAA 架构)
- 模板匹配 + ORB 特征点 + OCR 多层融合
- 分辨率无关，所有坐标来自识别结果
- 状态机驱动，交叉校验
"""
import os, sys
try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None


# ── 批量紧急停止钩子（v1.4.2）：与 ocr.py 同构。光靠调用方 Event 轮询要等
# 当前定位/状态机请求（采样 2~3 次 × 30s+）跑完，紧急终止不"立刻"。
# gui 批量线程注入 set_cancel_check(stop.is_set)，请求前/重试间立即中断。──
_CANCEL_CHECK = None


class VisionCancelled(RuntimeError):
    """批量识别被 F9 紧急终止（视觉/定位调用链）"""


def set_cancel_check(fn):
    """设置/清除取消检查回调：fn() 返回 True 表示需要取消；传 None 清除。"""
    global _CANCEL_CHECK
    _CANCEL_CHECK = fn


def _check_cancel():
    """API 请求前调用：取消已触发则抛 VisionCancelled，立即中断当前请求链。"""
    fn = _CANCEL_CHECK
    if fn is not None:
        try:
            if fn():
                raise VisionCancelled("紧急停止（F9）")
        except VisionCancelled:
            raise
        except Exception:
            pass

# ── 模板库路径（兼容打包）──
if getattr(sys, 'frozen', False):
    _TEMPLATE_DIR = os.path.join(sys._MEIPASS, 'templates')
else:
    _TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')


def _load_templates(name):
    """加载模板图片，支持多变体：query_button_1.png, query_button_2.png..."""
    templates = []
    if not os.path.isdir(_TEMPLATE_DIR):
        return templates
    for f in sorted(os.listdir(_TEMPLATE_DIR)):
        if f.startswith(name) and f.endswith('.png'):
            img = cv2.imread(os.path.join(_TEMPLATE_DIR, f))
            if img is not None:
                templates.append(img)
    return templates


def template_match(screenshot, template_name, threshold=0.75):
    """
    模板匹配：归一化相关系数 + 多尺度，支持多变体模板
    返回 (center_x, center_y, confidence) 或 None
    """
    # 依赖检查提前：无 cv2/np 时 _load_templates 内部 cv2.imread 会先崩
    if cv2 is None or np is None:
        return None
    templates = _load_templates(template_name)
    if not templates:
        return None
    
    screen_gray = cv2.cvtColor(screenshot, cv2.COLOR_BGR2GRAY)
    best_val, best_loc, best_scale, best_tw, best_th = -1, None, 1.0, 0, 0

    for template in templates:
        template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
        # 多尺度档位：覆盖 4K/ultrawide 下 UI 元素缩放差异（0.5x~1.5x）
        for scale in [0.5, 0.7, 0.9, 1.0, 1.1, 1.3, 1.5]:
            try:
                scaled = cv2.resize(template_gray, None, fx=scale, fy=scale,
                                   interpolation=cv2.INTER_LINEAR)
                if scaled.shape[0] > screen_gray.shape[0] or scaled.shape[1] > screen_gray.shape[1]:
                    continue
                result = cv2.matchTemplate(screen_gray, scaled, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)
                if max_val > best_val:
                    best_val = max_val
                    best_loc = max_loc
                    best_scale = scale
                    # 记录缩放后的实际模板尺寸，避免浮点乘法舍入误差
                    best_tw = scaled.shape[1]
                    best_th = scaled.shape[0]
            except (cv2.error if cv2 else Exception):
                continue
    
    if best_val < threshold or best_loc is None:
        return None
    
    tw = best_tw
    th = best_th
    cx = best_loc[0] + tw // 2
    cy = best_loc[1] + th // 2
    return (cx, cy, best_val)


def locate_element(screenshot_path, template_name, threshold=0.75):
    """
    模板匹配定位元素（v1.4 起只保留 template 单路径，orb/auto 已废弃）
    返回 (x, y) 或 None
    """
    if cv2 is None or np is None:
        return None
    screenshot = cv2.imread(screenshot_path)
    if screenshot is None:
        return None
    h, w = screenshot.shape[:2]
    scale = 1.0
    if w > 1920:
        scale = 1920 / w
        screenshot = cv2.resize(screenshot, (1920, int(h * scale)))
    result = template_match(screenshot, template_name, threshold)
    if result and scale != 1.0:
        return (int(result[0] / scale), int(result[1] / scale))
    return result


def _pick_vision_model():
    """选择 vision 定位任务的模型配置。

    返回 (active, provider, endpoint, key, mdl, use_responses)。

    规则：
    1. 主模型能定位（非 qwen*-ocr 纯文字模型）→ 用主模型。
    2. 主模型是 OCR 专用 → 副模型（get_secondary_model）若属于已配置 provider 且非 OCR
       型，则用副模型所在 provider 的配置（额度/权限归用户自己配的副模型，不硬编码）。
    3. 主副都无法定位（都是 OCR 型/未配置）→ raise RuntimeError 明确提示，
       绝不静默回退到硬编码模型。
    """
    from utils import get_api_config
    from ocr import _is_qwen_ocr as _qocr

    api_cfg = get_api_config()
    active = api_cfg.get('active_provider', 'doubao')
    providers = api_cfg.get('providers', {})
    providers = providers if isinstance(providers, dict) else {}

    def _resolve(prov_name, mdl_name):
        """按 provider 解析 endpoint/key/model，返回 (endpoint, key, mdl, use_responses)"""
        p = providers.get(prov_name, {}) or {}
        if not isinstance(p, dict):
            p = {}
        endpoint = p.get('endpoint', '') or ''
        if prov_name == 'doubao':
            if not endpoint:
                endpoint = 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'
            default_mdl = 'Doubao-Seed-2.1-pro'
        elif prov_name == 'qwen':
            if not endpoint:
                endpoint = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
            default_mdl = 'qwen3.5-omni-flash'
        else:  # glm
            if not endpoint:
                endpoint = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
            default_mdl = 'glm-4v-flash'
        mdl = p.get('custom_endpoint', '') or p.get('model', '') or mdl_name or default_mdl
        key = p.get('api_key', '') or os.environ.get(
            {'doubao': 'ARK_API_KEY', 'qwen': 'DASHSCOPE_API_KEY', 'glm': 'ZHIPU_API_KEY'}.get(prov_name, ''), '')
        use_responses = 'responses' in endpoint.lower()
        return endpoint, key, mdl, use_responses

    # 主模型
    _main_mdl = providers.get(active, {}).get('model', '') if isinstance(providers.get(active), dict) else ''
    if not _qocr(_main_mdl):
        endpoint, key, mdl, use_responses = _resolve(active, _main_mdl)
        return active, providers.get(active, {}), endpoint, key, mdl, use_responses

    # 主模型是 OCR 型 → 尝试副模型
    from utils import get_secondary_model
    _sec = get_secondary_model() or ''
    _sec = str(_sec).strip()
    _sec_prov = None
    for _pn, _pp in providers.items():
        if not isinstance(_pp, dict):
            continue
        _pm = _pp.get('model', '')
        if _pm and str(_pm).strip().lower() == _sec.lower():
            _sec_prov = _pn
            break
    if _sec_prov and not _qocr(_sec):
        # 副模型是通用视觉且能找到 provider → 用副模型配置
        endpoint, key, mdl, use_responses = _resolve(_sec_prov, _sec)
        return _sec_prov, providers.get(_sec_prov, {}), endpoint, key, mdl, use_responses

    # 主副都无法定位
    raise RuntimeError(
        f'视觉定位不可用：主模型 {_main_mdl or "(未配置)"} 是 Qwen OCR 专用模型（仅文字提取，不做定位），'
        f'副模型 {_sec or "(未配置)"} 也无法定位。请在「API 管理」把主/副模型改为通用视觉模型'
        f'（如 qwen3.5-omni-flash、glm-4.6v、Doubao-Seed-2.1-pro），或把 OCR 专用模型仅用于文字识别。')


def _call_vision_api(img_b64: str, prompt: str, max_tokens: int = 256, timeout: int = 30) -> str:
    """
    调用配置的视觉 API（doubao responses / glm chat 两种格式），返回模型文本响应。
    失败抛异常（由调用方包装层兜底）。
    定位类任务（表格 bbox/页面状态/元素定位）必须用通用视觉模型：
    Qwen OCR 专用模型（qwen*-ocr）是纯文字提取模型，不做定位——若主模型是 OCR 型，
    尝试用副模型（双模型验证配置里的通用视觉模型），两者都不可用则明确报错，
    不静默回退到硬编码模型（额度/权限不可控）。
    """
    active, provider, endpoint, key, mdl, use_responses = _pick_vision_model()
    if not key:
        raise RuntimeError('API Key 未设置')
    import requests as _req
    mdl_l = (mdl or '').lower()
    is_glm = mdl_l.startswith('glm-') or mdl_l == 'glm'
    # 与 ocr.py 一致：模型是 glm 但 endpoint 是官方阿里/豆包端点时，自动切回智谱端点+key
    # （自定义代理 endpoint 含子串不误切，只认官方域名）
    ep_l = endpoint.lower()
    if is_glm and ('dashscope.aliyuncs.com' in ep_l or 'ark.cn-beijing.volces.com' in ep_l):
        endpoint = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
        from utils import get_api_config as _gac
        _providers = (_gac().get('providers') or {})
        _glm = (_providers.get('glm', {}) or {}) if isinstance(_providers, dict) else {}
        _glm_key = _glm.get('api_key', '') or os.environ.get('ZHIPU_API_KEY', '')
        if not _glm_key:
            raise RuntimeError('GLM API Key 未设置（当前模型为 glm 但端点/Key 是阿里/豆包）')
        key = _glm_key
        use_responses = False
    # glm-4v-flash 输出上限 1024；glm-4.6v 等有 reasoning 需要更大预算
    # 统一钳制避免 400（flash 超 1024 报错），同时保证 4.6v 的定位/OCR 有足够 token
    if mdl_l.startswith('glm-4v-flash') or mdl_l == 'glm-4v-flash':
        max_tokens = min(max_tokens, 1024)
    if use_responses:
        _check_cancel()  # v1.4.2 紧急停止：发请求前检查
        resp = _req.post(endpoint,
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            json={
                'model': mdl,
                'thinking': {'type': 'disabled'},
                'input': [{'role': 'user', 'content': [
                    {'type': 'input_image', 'image_url': f'data:image/jpeg;base64,{img_b64}', 'detail': 'low'},
                    {'type': 'input_text', 'text': prompt}
                ]}],
                'temperature': 0.0, 'stream': False,
                'max_output_tokens': max_tokens,
            }, timeout=(5, timeout))
        data = resp.json()
        if not data.get('output'):
            raise RuntimeError(f'API 返回异常: {data}')
        return data['output'][-1]['content'][0]['text']
    # Chat Completions 分支：GLM-4.6v 默认开 reasoning 会吃满 max_tokens 导致正文截断，
    # 必须显式禁用 thinking；glm-4v-flash 等已实测接受该参数。统一发送保证一致。
    payload = {
        'model': mdl,
        'messages': [{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{img_b64}'}},
            {'type': 'text', 'text': prompt}
        ]}],
        'temperature': 0.0, 'max_tokens': max_tokens,
        'thinking': {'type': 'disabled'},
    }
    _check_cancel()  # v1.4.2 紧急停止：发请求前检查
    resp = _req.post(endpoint,
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        json=payload, timeout=(5, timeout))
    data = resp.json()
    if not data.get('choices'):
        raise RuntimeError(f'API 返回异常: {data}')
    return data['choices'][0]['message']['content']


def _load_screenshot_b64(screenshot_path: str = None, max_side: int = 1280, quality: int = 75) -> tuple:
    """
    截图（或读图）→ 压缩 JPEG → base64。
    返回 (img_b64, screen_w, screen_h)；截图失败返回 (None, 0, 0)。
    """
    import base64 as _b64, io as _io
    try:
        from PIL import Image as PILImg
        if screenshot_path:
            img = PILImg.open(screenshot_path)
        else:
            import pyautogui as pg
            img = pg.screenshot()
    except ImportError:
        return None, 0, 0
    screen_w, screen_h = img.size
    buf = _io.BytesIO()
    # RGBA/P 模式 JPEG 不支持，先转 RGB（截图/粘贴图常见 RGBA）
    if img.mode != 'RGB':
        img = img.convert('RGB')
    r = max_side / max(screen_w, screen_h)
    if r < 1:
        img = img.resize((int(screen_w * r), int(screen_h * r)), PILImg.LANCZOS)
    img.save(buf, format='JPEG', quality=quality)
    return _b64.b64encode(buf.getvalue()).decode(), screen_w, screen_h


def _parse_json_obj(content: str) -> dict:
    """从模型回复中提取 JSON 对象（首尾 { } 配对，兼容嵌套）"""
    import json as _json
    start = content.find('{')
    end = content.rfind('}')
    if start < 0 or end <= start:
        return {}
    return _json.loads(content[start:end + 1])


def _median(values):
    """取中位数（多次采样补偿坐标偏差用）"""
    if not values:
        return 0
    s = sorted(values)
    n = len(s)
    if n % 2:
        return s[n // 2]
    # 偶数样本：均值。整数坐标用整除，float 置信度用真除（避免 // 把 0.85 变 0）
    a, b = s[n // 2 - 1], s[n // 2]
    if isinstance(a, int) and isinstance(b, int):
        return (a + b) // 2
    return (a + b) / 2


def ai_locate_elements(screenshot_path: str = None) -> dict:
    """
    AI 智能视觉定位：截图 → Vision API → 返回下拉框和查询按钮坐标。
    多次采样（3 次）取中位数，减少单次定位的坐标偏差。
    返回 {'dropdown': {x,y}, 'query': {x,y}, 'confidence': float, 'screen_width': int, 'screen_height': int}
    失败返回 None（任何异常都不外抛，避免批量识别线程崩溃）。
    """
    samples = []
    for _ in range(3):
        try:
            r = _locate_elements_once(screenshot_path)
            if r:
                samples.append(r)
        except VisionCancelled:
            raise  # v1.4.2 紧急停止：取消异常透传，立即中断采样
        except Exception as _e:
            # v1.4.2 透出：定位失败原因（限流/额度耗尽/key/网络），供省份验证段区分
            # "API 层故障"与"页面结构变化定位不到"
            try:
                from ocr import _ocr_dlog, _mark_api_fatal
                _mark_api_fatal(_e)
                _ocr_dlog(f"元素定位 API 失败: {str(_e)[:120]}")
            except Exception:
                pass
            continue
    if not samples:
        return None
    if len(samples) == 1:
        return samples[0]
    # 各坐标字段取中位数
    out = {
        'dropdown': {
            'x': _median([s['dropdown']['x'] for s in samples]),
            'y': _median([s['dropdown']['y'] for s in samples]),
        },
        'query': {
            'x': _median([s['query']['x'] for s in samples]),
            'y': _median([s['query']['y'] for s in samples]),
        },
        'confidence': _median([s['confidence'] for s in samples]),
        'screen_width': samples[0]['screen_width'],
        'screen_height': samples[0]['screen_height'],
    }
    return out


def _locate_elements_once(screenshot_path: str = None) -> dict:
    """单次 AI 元素定位（多次采样的底层调用）"""
    img_b64, screen_w, screen_h = _load_screenshot_b64(screenshot_path)
    if not img_b64:
        return None
    prompt = """识别这张PDD商家后台截图中的两个UI元素坐标（相对于整张截图的像素比例）：
1. 省份/地区下拉选择框的中心点
2. "查询"按钮的中心点
输出严格JSON: {"dropdown": {"x": 0.XX, "y": 0.YY}, "query": {"x": 0.XX, "y": 0.YY},"confidence":0.XX}"""
    # v1.4.5（bug hunt F9）：定位链读取超时对齐 180s（大表/4K 图模型处理可 >30s，30s 必误判）
    content = _call_vision_api(img_b64, prompt, max_tokens=2048, timeout=180)
    result = _parse_json_obj(content)
    if not result:
        return None
    dd = result.get('dropdown') or {}
    qq = result.get('query') or {}
    conf = float(result.get('confidence', 0.8) or 0)
    dd_x = int(float(dd.get('x', 0)) * screen_w)
    dd_y = int(float(dd.get('y', 0)) * screen_h)
    qq_x = int(float(qq.get('x', 0)) * screen_w)
    qq_y = int(float(qq.get('y', 0)) * screen_h)
    # 低置信度拒绝：模型没把握时返回 None，调用方走模板/校准坐标兜底
    if conf < 0.5:
        return None
    # 合理性校验（含 Y 轴越界，防止点击到屏幕外）
    if dd_x <= 0 or dd_y <= 0 or qq_x <= 0 or qq_y <= 0:
        return None
    if dd_x >= screen_w or qq_x >= screen_w:
        return None
    if dd_y >= screen_h or qq_y >= screen_h:
        return None
    return {
        'dropdown': {'x': dd_x, 'y': dd_y},
        'query': {'x': qq_x, 'y': qq_y},
        'confidence': min(max(conf, 0), 1),
        'screen_width': screen_w,
        'screen_height': screen_h,
    }


def ai_read_total_count(screenshot_path: str = None) -> int:
    """读页面右下角分页栏的商品总条数（如 '共有 9 条' → 9），失败返回 None。

    v1.4.2 特写化：右下角分页栏裁剪放大识别——整屏压缩到 1280 后右下角小字
    糊成一团（实测页面5个读成3），特写放大后才是权威总数（客户要求：直接抓
    官方真实数据对比，别靠重试撞）。右下角横带放大 2x + q95，读"共有N条"。
    """
    img_b64 = None
    try:
        from PIL import Image as PILImg
        import io as _io, base64 as _b64
        img = PILImg.open(screenshot_path) if screenshot_path else None
        if img is not None:
            if img.mode != 'RGB':
                img = img.convert('RGB')
            w, h = img.size
            # 右下角分页栏：宽度右侧 55%，高度底部 12%（含"共有N条/每页[N]条"）
            left = max(0, int(w * 0.45))
            top = max(0, int(h * 0.84))
            right = min(w, int(w * 0.99))
            bottom = min(h, int(h * 0.99))
            if right - left >= 80 and bottom - top >= 24:
                img = img.crop((left, top, right, bottom))
                img = img.resize((img.size[0] * 2, img.size[1] * 2), PILImg.LANCZOS)
                buf = _io.BytesIO()
                img.save(buf, format='JPEG', quality=95)
                img_b64 = _b64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass
    if not img_b64:
        img_b64, _, _ = _load_screenshot_b64(screenshot_path)  # fallback 整屏
        if not img_b64:
            return None
    prompt = ('识别这张截图右下角分页栏中的商品总条数。格式如 "共有 9 条" → 9、'
              '"共 128 条" → 128、"总共 5 条" → 5。忽略每页条数下拉框（如"每页10条"）。'
              '只输出数字，找不到输出 0。')
    try:
        content = _call_vision_api(img_b64, prompt, max_tokens=16)
        import re as _re
        # v1.4.5（bug hunt F11）：优先匹配「共(有)N条」权威模式——纯 \d+ 首匹配可能
        # 取到『每页10条』的 10 → 累计 10 即硬停，128 个只认 10 个
        _m = _re.search(r'共\s*(?:有|計)?\s*(?P<n>\d+)\s*条', content or '')
        if not _m:
            _m = _re.search(r'总(?:共|计)?\s*(?P<n>\d+)\s*条', content or '')
        if not _m:
            _m = _re.search(r'(?P<n>\d+)\s*条', content or '')
        if not _m:
            _m = _re.search(r'(?P<n>\d+)', content or '')
        n = int(_m.group('n')) if _m else 0
        return n if n > 0 else None
    except VisionCancelled:
        raise  # v1.4.2 紧急停止：取消异常透传
    except Exception:
        return None


def ai_read_selected_province(screenshot_path: str = None, region=None) -> str:
    """读筛选栏省份/地区下拉框当前显示的省份名（如 '云南'/'云南省'），失败返回 None。

    用于切换省份后验证筛选是否生效：粘贴省份+回车后读回当前值，
    与目标省份不一致说明切换失败（下拉框没选上/粘贴失败），需重新走 AI 定位+选择。
    region: 可选 (cx, cy, w, h)，裁剪该区域识别（中心点+宽高，与截图同坐标系）。
    省份下拉框是页面顶部小控件，整页截图压缩到 1280 宽后小字糊成一团，
    模型经常读不出（实测粘贴成功但验证误报"无法识别"）——传下拉框坐标
    裁剪放大后识别，识别率大幅提升。
    """
    import base64 as _b64, io as _io
    if region:
        # 特写路径：按下拉框坐标裁剪 → 放大 2 倍 → 高质量 JPEG
        try:
            from PIL import Image as PILImg
            img = PILImg.open(screenshot_path)
        except Exception:
            return None
        if img.mode != 'RGB':
            img = img.convert('RGB')
        cx, cy, w, h = region
        left = max(0, int(cx - w / 2))
        top = max(0, int(cy - h / 2))
        right = min(img.size[0], int(cx + w / 2))
        bottom = min(img.size[1], int(cy + h / 2))
        if right <= left or bottom <= top:
            return None
        img = img.crop((left, top, right, bottom))
        img = img.resize((img.size[0] * 2, img.size[1] * 2), PILImg.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format='JPEG', quality=95)
        img_b64 = _b64.b64encode(buf.getvalue()).decode()
        prompt = ('识别这张图片中当前显示的文本内容（是省份/地区筛选框的特写图）。'
                  '只输出文本本身，如 "云南" "云南省" "广东" "全部" 等；'
                  '没有任何文字或无法辨认时，输出空字符串。')
    else:
        # 原路径：整图缩放压缩
        img_b64, _, _ = _load_screenshot_b64(screenshot_path)
        if not img_b64:
            return None
        prompt = ('识别这张PDD商家后台「订货管理」页面顶部筛选栏的省份/地区下拉框'
                  '当前显示的省份名。只输出省份名（如 "云南" "广东省"），'
                  '如果显示 "全部"/"所有地区" 或无法识别，输出空字符串。')
    try:
        content = _call_vision_api(img_b64, prompt, max_tokens=32, timeout=15)
        content = (content or '').strip().strip('"').strip("'").strip()
        return content or None
    except VisionCancelled:
        raise  # v1.4.2 紧急停止：取消异常透传
    except Exception as _e:
        # v1.4.2 透出错误：API 失败（限流/额度耗尽/key/网络）与"读到空/全部"必须区分——
        # 之前全吞成 None 会误报"省份切换失败"（客户日志：两个省份全跳过）
        try:
            from ocr import _ocr_dlog, _mark_api_fatal
            _mark_api_fatal(_e)
            _ocr_dlog(f"省份读值 API 失败: {str(_e)[:120]}")
        except Exception:
            pass
        return None


def ai_check_page_state(screenshot_path: str = None) -> dict:
    """识别页面整体状态（v1.4 状态机，借鉴 granblue）：normal / login / captcha / modal / empty。
    省份循环开始时检查：login 停止整个批量，captcha/modal/empty 跳过该省份。
    失败返回 {'state': 'unknown'}（不阻断）。
    """
    img_b64, _, _ = _load_screenshot_b64(screenshot_path)
    if not img_b64:
        return {'state': 'unknown', 'hint': None}
    prompt = ('判断这张PDD商家后台页面截图当前处于什么状态，只选一个：\n'
              '1. normal：正常显示订货管理页面（有筛选栏和商品表格）\n'
              '2. login：登录页 / 会话过期 / 需要重新登录\n'
              '3. captcha：验证码或安全验证弹窗\n'
              '4. modal：模态弹窗遮挡（居中弹窗无法点击操作）\n'
              '5. empty：页面空白或加载失败\n'
              '输出严格JSON: {"state": "normal", "hint": "一句话简述"}')
    try:
        content = _call_vision_api(img_b64, prompt, max_tokens=128, timeout=15)
        import json as _json
        text = (content or '').strip()
        if '```' in text:
            for _p in text.split('```'):
                _p = _p.strip()
                if _p.startswith('json'):
                    _p = _p[4:].strip()
                if _p.startswith('{'):
                    text = _p
                    break
        if text.startswith('{'):
            data = _json.loads(text)
            st = str(data.get('state') or 'unknown').strip().lower()
            if st not in ('normal', 'login', 'captcha', 'modal', 'empty'):
                st = 'unknown'
            return {'state': st, 'hint': data.get('hint') or None}
        return {'state': 'unknown', 'hint': None}
    except VisionCancelled:
        raise  # v1.4.2 紧急停止：取消异常透传
    except Exception:
        return {'state': 'unknown', 'hint': None}


def ai_detect_anomaly(screenshot_path: str = None) -> dict:
    """检测页面异常（v1.4，借鉴 granblue 验证码检测）：验证码/安全验证、弹窗、
    横幅警告、错误提示等。返回 {'anomaly': bool, 'type': str|None, 'hint': str|None}。
    失败返回 {'anomaly': False, ...}（不阻断主流程）。
    """
    img_b64, _, _ = _load_screenshot_b64(screenshot_path)
    if not img_b64:
        return {'anomaly': False, 'type': None, 'hint': None}
    prompt = ('判断这张PDD商家后台截图是否有**阻断操作的异常**：验证码/安全验证弹窗、'
              '模态弹窗（居中遮挡导致无法点击操作）。'
              '注意：页面顶部的常驻提示横幅（如预约警告、系统公告、红色提示条）**不算异常**，'
              '它们不影响点击操作。'
              '输出严格JSON: {"anomaly": true或false, "type": "验证码"或"弹窗"或null, '
              '"hint": "一句话说明，无异常填null"}')
    try:
        content = _call_vision_api(img_b64, prompt, max_tokens=128, timeout=15)
        import json as _json
        text = (content or '').strip()
        if '```' in text:
            for _p in text.split('```'):
                _p = _p.strip()
                if _p.startswith('json'):
                    _p = _p[4:].strip()
                if _p.startswith('{'):
                    text = _p
                    break
        if text.startswith('{'):
            data = _json.loads(text)
            # v1.4.5（bug hunt F10）：bool("false")=True 会把模型输出的字符串 'false' 当异常
            # → 误跳过省份；显式判定真值
            _an = str(data.get('anomaly')).strip().lower()
            return {
                'anomaly': _an in ('true', '1', 'yes'),
                'type': data.get('type') or None,
                'hint': data.get('hint') or None,
            }
        return {'anomaly': False, 'type': None, 'hint': None}
    except VisionCancelled:
        raise  # v1.4.5（bug hunt F26）：取消异常透传，F9 立即中断省份异常检测
    except Exception:
        return {'anomaly': False, 'type': None, 'hint': None}


def ai_locate_table(screenshot_path: str = None, samples: int = 3) -> dict:
    """
    AI 智能表格定位：截图 → Vision API → 返回商品表格区域 bbox、是否还有更多商品、
    以及省份/仓库下拉框坐标（用于分仓库批量识别）。
    多次采样（默认 3 次）取中位数，减少单次定位的坐标偏差。
    samples=1 时单次调用（调用方明确只需粗略坐标时省 API）。
    失败返回 None（任何异常都不外抛，避免批量识别线程崩溃）。
    """
    samples = max(1, int(samples or 1))
    results = []
    for _ in range(samples):
        try:
            r = _locate_table_once(screenshot_path)
            if r:
                results.append(r)
        except VisionCancelled:
            raise  # v1.4.2 紧急停止：取消异常透传，立即中断采样
        except Exception:
            continue
    if not results:
        return None
    if len(results) == 1:
        return results[0]
    # 各字段取中位数；has_more 取多数票；可选坐标字段有值才参与
    out = {
        'table': {
            'left': _median([s['table']['left'] for s in results]),
            'top': _median([s['table']['top'] for s in results]),
            'right': _median([s['table']['right'] for s in results]),
            'bottom': _median([s['table']['bottom'] for s in results]),
        },
        'has_more': sum(1 for s in results if s.get('has_more')) >= 2,
        'confidence': _median([s['confidence'] for s in results]),
        'screen_width': results[0]['screen_width'],
        'screen_height': results[0]['screen_height'],
    }
    for key in ('dropdown', 'query'):
        vals = [s.get(key) for s in results if s.get(key)]
        if vals:
            out[key] = {
                'x': _median([v['x'] for v in vals]),
                'y': _median([v['y'] for v in vals]),
            }
        else:
            out[key] = None
    # 页面总条数：多采样取中位数（无有效值时 None）
    _totals = [s.get('total_count') for s in results
               if isinstance(s.get('total_count'), int) and s['total_count'] > 0]
    out['total_count'] = _median(_totals) if _totals else None
    # 行级 bbox：多采样时取行数最多的样本（行边界一致性优先于中位数）
    _rows_opt = [s.get('rows') for s in results if s.get('rows')]
    if _rows_opt:
        out['rows'] = max(_rows_opt, key=len)
    else:
        out['rows'] = None
    return out


def _locate_table_once(screenshot_path: str = None) -> dict:
    """单次 AI 表格定位（多次采样的底层调用）"""
    img_b64, screen_w, screen_h = _load_screenshot_b64(screenshot_path)
    if not img_b64:
        return None
    prompt = """识别这张PDD商家后台「订货管理」页面截图中的 UI 元素（坐标均为相对整张截图的像素比例 0~1）：
1. table：商品表格区域的边界框（left/top/right/bottom，表格主体含表头，不含底部工具栏）
2. has_more：表格底部是否被截断——即页面还有更多商品需要滚动才能看到（看表格最后一行是否被切掉一半、或底部有滚动条未到底/加载更多提示）
3. dropdown：省份/地区下拉选择框的中心点
4. query："查询"按钮的中心点
5. total_count：页面统计信息里显示的商品总条数（如 "共 3 条" / "共 128 条"），找不到则填 null
6. rows：表格内容行的垂直边界（相对整图比例 top/bottom），按从上到下顺序，含表头行。格式：[{"top": 0.XX, "bottom": 0.YY}, ...]，最多返回 20 行；识别不了填 []
输出严格JSON: {"table": {"left": 0.XX, "top": 0.YY, "right": 0.XX, "bottom": 0.YY}, "has_more": true, "dropdown": {"x": 0.XX, "y": 0.YY}, "query": {"x": 0.XX, "y": 0.YY}, "total_count": 3 或 null, "rows": [{"top": 0.XX, "bottom": 0.YY}], "confidence": 0.XX}"""
    # v1.4.5（bug hunt F9）：表格定位（滚动轮每轮）读取超时对齐 180s
    content = _call_vision_api(img_b64, prompt, max_tokens=2048, timeout=180)
    result = _parse_json_obj(content)
    if not result:
        return None

    def _px(v, dim, default=None):
        try:
            return int(float(v) * dim)
        except (TypeError, ValueError):
            return default

    tbl = result.get('table') or {}
    left = _px(tbl.get('left'), screen_w)
    top = _px(tbl.get('top'), screen_h)
    right = _px(tbl.get('right'), screen_w)
    bottom = _px(tbl.get('bottom'), screen_h)
    conf = float(result.get('confidence', 0.8) or 0)

    # 低置信度拒绝：模型没把握时返回 None，调用方回退 OpenCV/全图
    if conf < 0.5:
        return None

    # 表格区域合理性校验：至少覆盖 1/4 宽高，且 left<right, top<bottom
    if (left is None or right is None or top is None or bottom is None
            or right - left < screen_w // 4 or bottom - top < screen_h // 4
            or left < 0 or top < 0 or right > screen_w or bottom > screen_h):
        return None

    out = {
        'table': {'left': left, 'top': top, 'right': right, 'bottom': bottom},
        'has_more': str(result.get('has_more', '')).strip().lower() == 'true',
        'confidence': min(max(conf, 0), 1),
        'screen_width': screen_w,
        'screen_height': screen_h,
    }
    # 行级 bbox（v1.4：供按行切分识别，防整表乱编）；异常/空时置 None 不影响主流程
    _rows_raw = result.get('rows') or []
    _rows = []
    if isinstance(_rows_raw, list):
        for rr in _rows_raw:
            try:
                _rt = _px(rr.get('top'), screen_h)
                _rb = _px(rr.get('bottom'), screen_h)
                if isinstance(_rt, int) and isinstance(_rb, int) and 0 < _rt < _rb < screen_h:
                    _rows.append((_rt, _rb))
            except Exception:
                continue
    out['rows'] = _rows if len(_rows) >= 2 else None
    # 页面总条数（“共 X 条”统计，供滚动结束后对比识别数量，防假数据/漏识别）
    try:
        total = int(float(result.get('total_count')))
        out['total_count'] = total if total > 0 else None
    except (TypeError, ValueError):
        out['total_count'] = None
    # 下拉框/查询按钮：可选字段，逐个校验（无则 None，调用方走模板/校准坐标）
    for key, dim in (('dropdown', (screen_w, screen_h)),
                     ('query', (screen_w, screen_h))):
        el = result.get(key) or {}
        x = _px(el.get('x'), dim[0])
        y = _px(el.get('y'), dim[1])
        if x is not None and y is not None and 0 < x < screen_w and 0 < y < screen_h:
            out[key] = {'x': x, 'y': y}
        else:
            out[key] = None
    return out
