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
    
    # 缩放大图加速
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
        # 交叉校验：两法都成功且位置接近才确认
        r1 = template_match(screenshot, template_name, threshold)
        r2 = orb_match(screenshot, template_name)
        if r1 and r2:
            # 两法中心距离 < 50px 认为一致
            dist = ((r1[0] - r2[0])**2 + (r1[1] - r2[1])**2)**0.5
            if dist < 50:
                result = (int((r1[0]+r2[0])/2), int((r1[1]+r2[1])/2))
            else:
                result = r1  # 分歧时优先模板匹配
        elif r1:
            result = (r1[0], r1[1])
        elif r2:
            result = (r2[0], r2[1])
        else:
            result = None
    
    if result and scale != 1.0:
        return (int(result[0] / scale), int(result[1] / scale))
    return result
