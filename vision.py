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


def _load_template(name):
    """向后兼容：取第一个匹配的模板"""
    tmpls = _load_templates(name)
    return tmpls[0] if tmpls else None


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


def orb_match(screenshot, template_name, min_matches=8, threshold=0.6):
    """
    ORB 特征点匹配：抗遮挡/旋转/形变
    返回 (center_x, center_y, match_count) 或 None
    """
    # 依赖检查提前：无 cv2/np 时 _load_template 内部 cv2.imread 会先崩
    if cv2 is None or np is None:
        return None
    template = _load_template(template_name)
    if template is None:
        return None
    
    orb = cv2.ORB_create(nfeatures=500)
    kp1, des1 = orb.detectAndCompute(template, None)
    kp2, des2 = orb.detectAndCompute(screenshot, None)
    
    if des1 is None or des2 is None or len(des1) < 4 or len(des2) < 4:
        return None
    
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(des1, des2)
    matches = sorted(matches, key=lambda x: x.distance)
    
    # 取前 N 个高质量匹配
    good = [m for m in matches if m.distance < 50]
    if len(good) < min_matches:
        return None
    
    # 计算变换后的中心点
    src_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    
    try:
        M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
        if M is None or mask.sum() < min_matches * threshold:
            return None
        h, w = template.shape[:2]
        center = np.float32([[w/2, h/2]]).reshape(-1, 1, 2)
        center_dst = cv2.perspectiveTransform(center, M)
        return (int(center_dst[0][0][0]), int(center_dst[0][0][1]), mask.sum())
    except Exception:
        return None


def locate_element(screenshot_path, template_name, method='auto', threshold=0.75):
    """
    融合识别：模板匹配 + ORB 交叉校验
    method: 'template' | 'orb' | 'auto'（默认两法交叉校验）
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
    if method == 'template':
        result = template_match(screenshot, template_name, threshold)
    elif method == 'orb':
        result = orb_match(screenshot, template_name, threshold=threshold)
    else:
        r1 = template_match(screenshot, template_name, threshold)
        r2 = orb_match(screenshot, template_name)
        if r1 and r2:
            dist = ((r1[0] - r2[0])**2 + (r1[1] - r2[1])**2)**0.5
            if dist < 50:
                result = (int((r1[0]+r2[0])/2), int((r1[1]+r2[1])/2))
            else:
                result = r1
        elif r1:
            result = (r1[0], r1[1])
        elif r2:
            result = (r2[0], r2[1])
        else:
            result = None
    if result and scale != 1.0:
        return (int(result[0] / scale), int(result[1] / scale))
    return result


def _call_vision_api(img_b64: str, prompt: str, max_tokens: int = 256, timeout: int = 30) -> str:
    """
    调用配置的视觉 API（doubao responses / glm chat 两种格式），返回模型文本响应。
    失败抛异常（由调用方包装层兜底）。
    """
    from utils import get_api_config
    api_cfg = get_api_config()
    active = api_cfg.get('active_provider', 'doubao')
    providers = api_cfg.get('providers', {})
    provider = (providers.get(active, {}) or {}) if isinstance(providers, dict) else {}
    endpoint = provider.get('endpoint', 'https://ark.cn-beijing.volces.com/api/v3/chat/completions')
    key = provider.get('api_key', '')
    if not key:
        raise RuntimeError('API Key 未设置')

    import requests as _req
    use_responses = 'responses' in endpoint.lower()  # 大小写不敏感，与 ocr.py 一致
    mdl = provider.get('model', 'Doubao-Seed-2.1-pro')
    mdl_l = (mdl or '').lower()
    is_glm = mdl_l.startswith('glm-') or mdl_l == 'glm'
    if use_responses:
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
            }, timeout=timeout)
        data = resp.json()
        if 'output' not in data:
            raise RuntimeError(f'API 返回异常: {data}')
        return data['output'][-1]['content'][0]['text']
    # Chat Completions 分支：GLM API 不识别 thinking 参数，仅非智谱模型发送
    payload = {
        'model': mdl,
        'messages': [{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{img_b64}'}},
            {'type': 'text', 'text': prompt}
        ]}],
        'temperature': 0.0, 'max_tokens': max_tokens,
    }
    if not is_glm:
        payload['thinking'] = {'type': 'disabled'}
    resp = _req.post(endpoint,
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        json=payload, timeout=timeout)
    data = resp.json()
    if 'choices' not in data:
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


def ai_locate_elements(screenshot_path: str = None) -> dict:
    """
    AI 智能视觉定位：截图 → Vision API → 返回下拉框和查询按钮坐标。
    返回 {'dropdown': {x,y}, 'query': {x,y}, 'confidence': float, 'screen_width': int, 'screen_height': int}
    失败返回 None（任何异常都不外抛，避免批量识别线程崩溃）。
    """
    try:
        img_b64, screen_w, screen_h = _load_screenshot_b64(screenshot_path)
        if not img_b64:
            return None
        prompt = """识别这张PDD商家后台截图中的两个UI元素坐标（相对于整张截图的像素比例）：
1. 省份/地区下拉选择框的中心点
2. "查询"按钮的中心点
输出严格JSON: {"dropdown": {"x": 0.XX, "y": 0.YY}, "query": {"x": 0.XX, "y": 0.YY},"confidence":0.XX}"""
        content = _call_vision_api(img_b64, prompt, max_tokens=256)
        result = _parse_json_obj(content)
        if not result:
            return None
        dd = result.get('dropdown', {})
        qq = result.get('query', {})
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
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[AI定位] 失败: {e}")
        return None


def ai_locate_table(screenshot_path: str = None) -> dict:
    """
    AI 智能表格定位：截图 → Vision API → 返回商品表格区域 bbox、是否还有更多商品、
    以及省份/仓库下拉框坐标（用于分仓库批量识别）。

    返回 {
      'table': {'left': px, 'top': px, 'right': px, 'bottom': px},   # 表格区域（像素）
      'has_more': bool,           # 表格底部是否被截断（还有更多商品未显示）
      'dropdown': {x, y},         # 省份下拉框中心（像素）
      'warehouse_dropdown': {x, y},  # 城市仓下拉框中心（像素，可能为 None）
      'query': {x, y},            # 查询按钮中心（像素）
      'confidence': float,
      'screen_width': int, 'screen_height': int,
    }
    失败返回 None（任何异常都不外抛，避免批量识别线程崩溃）。
    """
    try:
        img_b64, screen_w, screen_h = _load_screenshot_b64(screenshot_path)
        if not img_b64:
            return None
        prompt = """识别这张PDD商家后台「订货管理」页面截图中的 UI 元素（坐标均为相对整张截图的像素比例 0~1）：
1. table：商品表格区域的边界框（left/top/right/bottom，表格主体含表头，不含底部工具栏）
2. has_more：表格底部是否被截断——即页面还有更多商品需要滚动才能看到（看表格最后一行是否被切掉一半、或底部有滚动条未到底/加载更多提示）
3. dropdown：省份/地区下拉选择框的中心点
4. warehouse_dropdown：城市仓下拉选择框的中心点（若页面上没有该元素则填 null）
5. query："查询"按钮的中心点
输出严格JSON: {"table": {"left": 0.XX, "top": 0.YY, "right": 0.XX, "bottom": 0.YY}, "has_more": true, "dropdown": {"x": 0.XX, "y": 0.YY}, "warehouse_dropdown": {"x": 0.XX, "y": 0.YY} 或 null, "query": {"x": 0.XX, "y": 0.YY}, "confidence": 0.XX}"""
        content = _call_vision_api(img_b64, prompt, max_tokens=512)
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
        # 下拉框/查询按钮：可选字段，逐个校验（无则 None，调用方走模板/校准坐标）
        for key, dim in (('dropdown', (screen_w, screen_h)),
                         ('warehouse_dropdown', (screen_w, screen_h)),
                         ('query', (screen_w, screen_h))):
            el = result.get(key) or {}
            x = _px(el.get('x'), dim[0])
            y = _px(el.get('y'), dim[1])
            if x is not None and y is not None and 0 < x < screen_w and 0 < y < screen_h:
                out[key] = {'x': x, 'y': y}
            else:
                out[key] = None
        return out
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[AI表格定位] 失败: {e}")
        return None
