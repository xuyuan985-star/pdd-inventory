"""
PDD EZ — 配置常量与偏好读写
皮肤系统 / 分辨率预设 / 主题偏好持久化
主题 = 完整设计 spec：C_* 语义色 + components 组件 token + decor 装饰 token
"""
import json
import os
from utils import get_base_dir

# ── 皮肤系统 — 完整设计 token（颜色 + 组件 + 装饰全跟随）────────────────────
THEMES = {
    "极简白": {
        "label": "极简白",
        "desc": "纯白底·灰蓝字·蓝点缀",
        "C_PRIMARY": "#1E293B",
        "C_SECONDARY": "#64748B",
        "C_ACCENT": "#2563EB",
        "C_BG": "#FFFFFF",
        "C_SURFACE": "#F8FAFC",
        "C_TEXT": "#0F172A",
        "C_MUTED": "#94A3B8",
        "C_BORDER": "#E2E8F0",
        "C_RED": "#DC2626",
        "C_BTN_BLUE": "#2563EB",
        "C_CARD_HDR": "#1E293B",
        "C_YELLOW_BG": "#FEF9C3",
        "C_GREEN_BG": "#DCFCE7",
        "C_RED_BG": "#FEE2E2",
        "C_BLUE_LIGHT": "#EFF6FF",
        "components": {
            "on_accent": "#FFFFFF", "on_primary": "#FFFFFF",
            "btn": {
                "corner": 2,
                "primary": {"bg": "#2563EB", "fg": "#FFFFFF", "edge": "#2563EB", "bg_hover": "#3B82F6"},
                "dark":    {"bg": "#1E293B", "fg": "#FFFFFF", "edge": "#1E293B", "bg_hover": "#334155"},
                "ghost":   {"bg": "", "fg": "#0F172A", "edge": "#CBD5E1", "bg_hover": "#F1F5F9"},
                "text":    {"bg": "#FFFFFF", "fg": "#0F172A", "underline": "#2563EB"},
                "tag":     {"bg": "#2563EB", "fg": "#FFFFFF", "edge": "#2563EB", "bg_hover": "#3B82F6"},
                "disabled": {"bg": "#E2E8F0", "edge": "#CBD5E1", "fg": "#94A3B8"},
            },
            "table": {
                "accent_line": "#2563EB", "accent_line_h": 2,
                "header_bg": "#1E293B", "header_fg": "#FFFFFF", "header_sub": "#94A3B8",
                "col_sep": "#E2E8F0",
            },
            "pill": {
                "bg": "#FFFFFF", "fg": "#0F172A",
                "free": {"bg": "#2563EB", "fg": "#FFFFFF", "edge": "#1D4ED8"},
                "pro":  {"bg": "#0F172A", "fg": "#FFFFFF", "edge": "#0F172A"},
            },
            "field": {"normal_bg": "#FFFFFF", "bad_bg": "#FEE2E2", "bad_fg": "#B91C1C"},
            "nav": {"rail": "#2563EB", "item_bg": "#FFFFFF", "item_fg": "#0F172A", "active_bg": "#F8FAFC"},
            "card": {"selected_border": "#2563EB", "border": "#E2E8F0", "label_fg": "#0F172A"},
        },
        "decor": {
            "topbar": {
                "height": 66, "bg": "#2563EB",
                "title_fg": "#FFFFFF", "sub_fg": "#DBEAFE",
                "block1": "#1D4ED8", "block2": "#3B82F6", "line": "#DBEAFE",
                "ver_bg": "#1E3A8A", "ver_fg": "#FFFFFF", "ver_edge": "#93C5FD",
            },
            "section": {"sep": "#E2E8F0"},
            "result": {"accent_line": "#2563EB", "accent_line_h": 2},
        },
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
        "C_BTN_BLUE": "#D97706",
        "C_CARD_HDR": "#292524",
        "C_YELLOW_BG": "#FEF3C7",
        "C_GREEN_BG": "#DCFCE7",
        "C_RED_BG": "#FEE2E2",
        "C_BLUE_LIGHT": "#F5F0E8",
        "components": {
            "on_accent": "#FFFFFF", "on_primary": "#FFFFFF",
            "btn": {
                "corner": 2,
                "primary": {"bg": "#D97706", "fg": "#FFFFFF", "edge": "#D97706", "bg_hover": "#F59E0B"},
                "dark":    {"bg": "#292524", "fg": "#FFFFFF", "edge": "#292524", "bg_hover": "#44403C"},
                "ghost":   {"bg": "", "fg": "#1C1917", "edge": "#D6C3A8", "bg_hover": "#F5F0E8"},
                "text":    {"bg": "#FEF7ED", "fg": "#1C1917", "underline": "#D97706"},
                "tag":     {"bg": "#D97706", "fg": "#FFFFFF", "edge": "#D97706", "bg_hover": "#F59E0B"},
                "disabled": {"bg": "#E7D8C4", "edge": "#D6C3A8", "fg": "#A8A29E"},
            },
            "table": {
                "accent_line": "#D97706", "accent_line_h": 2,
                "header_bg": "#292524", "header_fg": "#FFFFFF", "header_sub": "#A8A29E",
                "col_sep": "#E7D8C4",
            },
            "pill": {
                "bg": "#FEF7ED", "fg": "#1C1917",
                "free": {"bg": "#D97706", "fg": "#FFFFFF", "edge": "#B45309"},
                "pro":  {"bg": "#292524", "fg": "#FFFFFF", "edge": "#292524"},
            },
            "field": {"normal_bg": "#FFFFFF", "bad_bg": "#FEE2E2", "bad_fg": "#B91C1C"},
            "nav": {"rail": "#D97706", "item_bg": "#FEF7ED", "item_fg": "#1C1917", "active_bg": "#F5F0E8"},
            "card": {"selected_border": "#D97706", "border": "#E7D8C4", "label_fg": "#1C1917"},
        },
        "decor": {
            "topbar": {
                "height": 66, "bg": "#D97706",
                "title_fg": "#FFFFFF", "sub_fg": "#FDE9CE",
                "block1": "#B45309", "block2": "#F59E0B", "line": "#FDE9CE",
                "ver_bg": "#7C2D12", "ver_fg": "#FFFFFF", "ver_edge": "#FCD9A1",
            },
            "section": {"sep": "#E7D8C4"},
            "result": {"accent_line": "#D97706", "accent_line_h": 2},
        },
    },
    "终末地": {
        "label": "终末地",
        "desc": "黄白机能·黑斜切·工业终端",
        "C_PRIMARY": "#111111",
        "C_SECONDARY": "#333333",
        "C_ACCENT": "#FFE600",
        "C_BG": "#FFFFFF",
        "C_SURFACE": "#F7F7F2",
        "C_TEXT": "#222222",
        "C_MUTED": "#6B6B6B",
        "C_BORDER": "#EAEAEA",
        "C_RED": "#DC2626",
        "C_BTN_BLUE": "#1E88E5",
        "C_CARD_HDR": "#1F1F1F",
        "C_YELLOW_BG": "#FFE600",
        "C_GREEN_BG": "#E8F5E9",
        "C_RED_BG": "#FFEBEE",
        "C_BLUE_LIGHT": "#FFF3B0",
        "components": {
            "on_accent": "#111111", "on_primary": "#FFFFFF",
            "btn": {
                "corner": 2,
                "primary": {"bg": "#FFE600", "fg": "#111111", "edge": "#FFE600", "bg_hover": "#F2D500"},
                "dark":    {"bg": "#111111", "fg": "#FFFFFF", "edge": "#111111", "bg_hover": "#262626"},
                "ghost":   {"bg": "", "fg": "#222222", "edge": "#EAEAEA", "bg_hover": "#F5F5F0"},
                "text":    {"bg": "#FFFFFF", "fg": "#111111", "underline": "#FFE600"},
                "tag":     {"bg": "#FFE600", "fg": "#111111", "edge": "#FFE600", "bg_hover": "#F2D500"},
                "disabled": {"bg": "#E8E8E3", "edge": "#C9C9C2", "fg": "#9E9E9E"},
            },
            "table": {
                "accent_line": "#FFE600", "accent_line_h": 2,
                "header_bg": "#1F1F1F", "header_fg": "#FFFFFF", "header_sub": "#BDBDBD",
                "col_sep": "#E0E0E0",
            },
            "pill": {
                "bg": "#FFFFFF", "fg": "#111111",
                "free": {"bg": "#111111", "fg": "#FFE600", "edge": "#111111"},
                "pro":  {"bg": "#FFE600", "fg": "#111111", "edge": "#111111"},
            },
            "field": {"normal_bg": "#FFFFFF", "bad_bg": "#FFEBEE", "bad_fg": "#B71C1C"},
            "nav": {"rail": "#FFE600", "item_bg": "#FFFFFF", "item_fg": "#111111", "active_bg": "#F7F7F2"},
            "card": {"selected_border": "#FFE600", "border": "#111111", "label_fg": "#111111"},
        },
        "decor": {
            "topbar": {
                "height": 66, "bg": "#FFE600",
                "title_fg": "#111111", "sub_fg": "#333333",
                "block1": "#111111", "block2": "#333333", "line": "#111111",
                "ver_bg": "#111111", "ver_fg": "#FFE600", "ver_edge": "#FFE600",
            },
            "section": {"sep": "#E0E0E0"},
            "result": {"accent_line": "#FFE600", "accent_line_h": 2},
        },
    },
}

# 老主题缺键回退默认
_DEFAULTS = {
    "components": {
        "on_accent": "#111111", "on_primary": "#FFFFFF",
        "btn": {}, "table": {}, "pill": {}, "field": {}, "nav": {}, "card": {},
    },
    "decor": {"topbar": {}, "section": {}, "result": {}},
}


def _merge_theme(spec):
    """老主题缺键回退默认，保证不崩"""
    import copy
    base = copy.deepcopy(_DEFAULTS)
    for k in ("components", "decor"):
        for kk, vv in (spec.get(k) or {}).items():
            if isinstance(vv, dict) and isinstance(base[k].get(kk), dict):
                base[k][kk].update(vv)
            else:
                base[k][kk] = vv
    return {**spec, "components": base["components"], "decor": base["decor"]}


def _read_settings():
    """读取 settings.json，失败返回 {}"""
    try:
        with open(os.path.join(get_base_dir(), 'settings.json'), 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError, OSError):
        return {}


def _write_settings(s):
    """写入 settings.json（原子替换 + 失败重试 + .bak 备份，防 Windows 文件锁丢配置）"""
    import os, time
    path = os.path.join(get_base_dir(), 'settings.json')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    # 写入前备份现有配置（杀毒/云同步短暂锁定时可恢复）
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as _f:
                _bak = _f.read()
            with open(path + '.bak', 'w', encoding='utf-8') as _f:
                _f.write(_bak)
    except Exception:
        pass
    # os.replace 原子替换；Windows 上目标被短暂锁定会抛 PermissionError → 重试 3 次
    for _attempt in range(3):
        try:
            os.replace(tmp, path)
            return
        except OSError as _e:
            if _attempt >= 2:
                raise  # 重试耗尽：显式抛给调用方提示，不静默吞（避免用户无感知丢设置）
            time.sleep(0.2)


def load_theme_pref() -> str:
    """读取皮肤偏好，返回主题名"""
    name = _read_settings().get('theme', '终末地')
    return name if name in THEMES else '终末地'


def save_theme_pref(name: str):
    """保存皮肤偏好"""
    s = _read_settings()
    s['theme'] = name
    _write_settings(s)
