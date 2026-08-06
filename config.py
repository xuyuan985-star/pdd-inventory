"""
PDD EZ — 配置常量与偏好读写
皮肤系统 / 分辨率预设 / 主题偏好持久化
"""
import json
import os
from utils import get_base_dir

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
    "机能黄白黑": {
        "label": "机能黄白黑",
        "desc": "亮黄通栏·黑白分割·斜切机能",
        "C_PRIMARY": "#111111",
        "C_SECONDARY": "#333333",
        "C_ACCENT": "#FFE600",
        "C_BG": "#FFFFFF",
        "C_SURFACE": "#F7F7F2",
        "C_TEXT": "#111111",
        "C_MUTED": "#6B6B6B",
        "C_BORDER": "#111111",
        "C_RED": "#DC2626",
        "C_BTN_BLUE": "#1E88E5",
        "C_CARD_HDR": "#1F1F1F",
        "C_YELLOW_BG": "#FFE600",
        "C_GREEN_BG": "#E8F5E9",
        "C_RED_BG": "#FFEBEE",
        "C_BLUE_LIGHT": "#FFF3B0",
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
    path = os.path.join(get_base_dir(), 'settings.json')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 原子替换，防崩溃丢配置


def load_theme_pref() -> str:
    """读取皮肤偏好，返回主题名"""
    name = _read_settings().get('theme', '机能黄白黑')
    return name if name in THEMES else '机能黄白黑'


def save_theme_pref(name: str):
    """保存皮肤偏好"""
    s = _read_settings()
    s['theme'] = name
    _write_settings(s)
