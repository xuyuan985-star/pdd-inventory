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
    numpy = None

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
    templates = _load_templates(template_name)
    if not templates:
        return None
    if cv2 is None:
        return None
    
    screen_gray = cv2.cvtColor(screenshot, cv2.COLOR_BGR2GRAY)
    best_val, best_loc, best_scale, best_tw, best_th = -1, None, 1.0, 0, 0
    
    for template in templates:
        template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
        for scale in [0.8, 0.9, 1.0, 1.1, 1.2]:
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
                    best_tw = template_gray.shape[1]
                    best_th = template_gray.shape[0]
            except (cv2.error if cv2 else Exception):
                continue
    
    if best_val < threshold or best_loc is None:
        return None
    
    tw = int(best_tw * best_scale)
    th = int(best_th * best_scale)
    cx = best_loc[0] + tw // 2
    cy = best_loc[1] + th // 2
    return (cx, cy, best_val)


def orb_match(screenshot, template_name, min_matches=8, threshold=0.6):
    """
    ORB 特征点匹配：抗遮挡/旋转/形变
    返回 (center_x, center_y, match_count) 或 None
    """
    template = _load_template(template_name)
    if template is None:
        return None
    if cv2 is None:
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


def ai_locate_elements(screenshot_path: str = None) -> dict:
    """
    AI 智能视觉定位：截图 → Vision API → 返回下拉框和查询按钮坐标。
    返回 {'dropdown': {x,y}, 'query': {x,y}, 'confidence': float, 'screen_width': int, 'screen_height': int}
    失败返回 None。
    """
    import json as _json, base64 as _b64, io as _io, time as _time
    try:
        from PIL import Image as PILImg
        import pyautogui as pg
    except ImportError:
        return None

    # 截图
    if screenshot_path:
        img = PILImg.open(screenshot_path)
    else:
        img = pg.screenshot()
    screen_w, screen_h = img.size

    # 压缩
    buf = _io.BytesIO()
    r = 1280 / max(screen_w, screen_h)
    if r < 1:
        img = img.resize((int(screen_w * r), int(screen_h * r)), PILImg.LANCZOS)
    img.save(buf, format='JPEG', quality=75)
    img_b64 = _b64.b64encode(buf.getvalue()).decode()

    # 获取 API 配置（与 OCR 共用）
    from utils import get_api_config
    api_cfg = get_api_config()
    active = api_cfg.get('active_provider', 'doubao')
    providers = api_cfg.get('providers', {})
    provider = (providers.get(active, {}) or {}) if isinstance(providers, dict) else {}
    endpoint = provider.get('endpoint', 'https://ark.cn-beijing.volces.com/api/v3/chat/completions')
    key = provider.get('api_key', '')
    if not key:
        return None

    import requests as _req
    prompt = """识别这张PDD商家后台截图中的两个UI元素坐标（相对于整张截图的像素比例）：
1. 省份/地区下拉选择框的中心点
2. "查询"按钮的中心点
输出严格JSON: {"dropdown": {"x": 0.XX, "y": 0.YY}, "query": {"x": 0.XX, "y": 0.YY},"confidence":0.XX}"""

    use_responses = 'responses' in endpoint
    if use_responses:
        resp = _req.post(endpoint,
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            json={
                'model': provider.get('model', 'Doubao-Seed-2.1-pro'),
                'thinking': {'type': 'disabled'},
                'input': [{'role': 'user', 'content': [
                    {'type': 'input_image', 'image_url': f'data:image/jpeg;base64,{img_b64}', 'detail': 'low'},
                    {'type': 'input_text', 'text': prompt}
                ]}],
                'temperature': 0.0, 'stream': False
            }, timeout=30)
        data = resp.json()
        if 'output' not in data:
            return None
        content = data['output'][-1]['content'][0]['text']
    else:
        resp = _req.post(endpoint,
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            json={
                'model': provider.get('model', 'Doubao-Seed-2.1-pro'),
                'messages': [{'role': 'user', 'content': [
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{img_b64}'}},
                    {'type': 'text', 'text': prompt}
                ]}],
                'temperature': 0.0, 'max_tokens': 256,
                'thinking': {'type': 'disabled'}
            }, timeout=30)
        data = resp.json()
        if 'choices' not in data:
            return None
        content = data['choices'][0]['message']['content']

    # 解析 JSON — 用首尾 { } 配对，兼容嵌套对象
    start = content.find('{')
    end = content.rfind('}')
    if start < 0 or end <= start:
        return None
    result = _json.loads(content[start:end+1])

    dd = result.get('dropdown', {})
    qq = result.get('query', {})
    conf = result.get('confidence', 0.8)

    # 比例 → 像素
    dd_x = int(float(dd.get('x', 0)) * screen_w)
    dd_y = int(float(dd.get('y', 0)) * screen_h)
    qq_x = int(float(qq.get('x', 0)) * screen_w)
    qq_y = int(float(qq.get('y', 0)) * screen_h)

    # 合理性校验
    if dd_x <= 0 or dd_y <= 0 or qq_x <= 0 or qq_y <= 0:
        return None
    if dd_x >= screen_w or qq_x >= screen_w:
        return None

    return {
        'dropdown': {'x': dd_x, 'y': dd_y},
        'query': {'x': qq_x, 'y': qq_y},
        'confidence': min(max(float(conf), 0), 1),
        'screen_width': screen_w,
        'screen_height': screen_h,
    }
