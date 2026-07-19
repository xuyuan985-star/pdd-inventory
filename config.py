"""
PDD EZ — 配置常量与偏好读写
皮肤系统 / 分辨率预设 / 主题偏好持久化
"""
import json
import os
from utils import get_base_dir

# ── 分辨率预设 ──────────────────────────────────────────────────
# 坐标是相对于屏幕的比例 (0.0~1.0)，适配所有分辨率
RESOLUTION_PRESETS = {
    "1920×1080 (Full HD)": {"w": 1920, "h": 1080, "dropdown_x": 0.28, "dropdown_y": 0.38, "query_x": 0.82, "query_y": 0.38},
    "2560×1440 (2K)":      {"w": 2560, "h": 1440, "dropdown_x": 0.28, "dropdown_y": 0.38, "query_x": 0.82, "query_y": 0.38},
    "3840×2160 (4K)":      {"w": 3840, "h": 2160, "dropdown_x": 0.26, "dropdown_y": 0.36, "query_x": 0.83, "query_y": 0.36},
    "1366×768 (HD)":       {"w": 1366, "h": 768,  "dropdown_x": 0.30, "dropdown_y": 0.40, "query_x": 0.80, "query_y": 0.40},
}

# ── 皮肤系统 — New Minimalism ────────────────────────────────────
THEMES = {
    "极简白": {
        "label": "极简白",
        "desc": "纯白底·灰蓝字·蓝点缀",
        # Flat Design: 无阴影无渐变，4-6色限制，高对比
        "C_PRIMARY": "#1E293B",       # Slate 800 — 标题/表头
        "C_SECONDARY": "#64748B",     # Slate 500 — 辅助
        "C_ACCENT": "#2563EB",        # Blue 600 — 仅一处强调
        "C_BG": "#FFFFFF",            # 纯白背景
        "C_SURFACE": "#F8FAFC",       # Slate 50 — 微妙区分
        "C_TEXT": "#0F172A",          # Slate 900 — 正文
        "C_MUTED": "#94A3B8",        # Slate 400 — 淡化
        "C_BORDER": "#E2E8F0",       # Slate 200 — 极细分割
        "C_RED": "#DC2626",
        "C_YELLOW_BG": "#FEF9C3",
        "C_GREEN_BG": "#DCFCE7",
        "C_RED_BG": "#FEE2E2",
        "C_BLUE_LIGHT": "#EFF6FF",
    },
    "极简墨": {
        "label": "极简墨",
        "desc": "墨灰底·浅灰字·白线",
        "C_PRIMARY": "#64748B",
        "C_SECONDARY": "#94A3B8",
        "C_ACCENT": "#60A5FA",
        "C_BG": "#1E293B",
        "C_SURFACE": "#0F172A",
        "C_TEXT": "#F1F5F9",
        "C_MUTED": "#64748B",
        "C_BORDER": "#334155",
        "C_RED": "#EF4444",
        "C_YELLOW_BG": "#3B2F00",
        "C_GREEN_BG": "#052E16",
        "C_RED_BG": "#450A0A",
        "C_BLUE_LIGHT": "#1E293B",
    },
    "极简暖": {
        "label": "极简暖",
        "desc": "暖杏底·褐字·金点缀",
        "C_PRIMARY": "#292524",
        "C_SECONDARY": "#78716C",
        "C_ACCENT": "#D97706",
        "C_BG": "#FEF7ED",
        "C_SURFACE": "#F5F0E8",
        "C_TEXT": "#1C1917",
        "C_MUTED": "#A8A29E",
        "C_BORDER": "#E7D8C4",
        "C_RED": "#DC2626",
        "C_YELLOW_BG": "#FEF3C7",
        "C_GREEN_BG": "#DCFCE7",
        "C_RED_BG": "#FEE2E2",
        "C_BLUE_LIGHT": "#F5F0E8",
    },
}


def _read_settings():
    """读取 settings.json，失败返回 {}"""
    try:
        with open(os.path.join(get_base_dir(), 'settings.json'), 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError, OSError):
        return {}


def _write_settings(s):
    """写入 settings.json"""
    import os
    with open(os.path.join(get_base_dir(), 'settings.json'), 'w', encoding='utf-8') as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def load_theme_pref() -> str:
    """读取皮肤偏好，返回主题名"""
    name = _read_settings().get('theme', '极简白')
    return name if name in THEMES else '极简白'


def save_theme_pref(name: str):
    """保存皮肤偏好"""
    s = _read_settings()
    s['theme'] = name
    _write_settings(s)


def load_resolution_pref() -> str:
    """读取分辨率偏好"""
    return _read_settings().get('resolution', '1920×1080 (Full HD)')


def save_resolution_pref(name: str):
    """保存分辨率偏好"""
    s = _read_settings()
    s['resolution'] = name
    _write_settings(s)

