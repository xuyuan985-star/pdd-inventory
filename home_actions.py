# -*- coding: utf-8 -*-
"""R2 主页按钮交互契约（t1 任务源起，v1.5.7 布局定版更新，纯 stdlib 纯函数模块）。

不依赖 Tk，可单测；gui.py 的实现侧按本契约替换：

★ v1.5.7 布局定版（用户拍板，替代 v1.5.6 截图合并菜单方案）
     第一排：批量 | 导入 | 识图 | 导出     第二排：刷新 | 🛡 双模型
     - 「批量」保留一键直达（自动滚动采集整页，多省份）
     - 「导入」= 文件数据源统一入口：弹菜单二选一【导入表格文件】【选择图片文件】
       （本地图片选择识别并入导入——v1.5.6 的 SHOT_MENU_ITEMS 相应解散）
     - 「识图」= 截取当前窗口（最小化→截 PDD 窗口→恢复→识别，1 张，含每日门控）
       ——不再弹菜单，也未并入任何菜单，命令直连 _live_screenshot
     - 全部按钮统一宽度（BTN_WIDTH_HOME 单一常量驱动）

★ 历史语义（防回归误解）
     - v1.5.6「截图」合并菜单（live_capture + pick_images）已按用户新拍板解散：
       live_capture → 「识图」按钮直连 _live_screenshot；pick_images → 「导入」菜单。
     - busy 语义 key：批量忙时【导入】显示『导入中…』（含图片路径）、
       【识图】显示『识图中…』、【导出】『导出中…』——与 gui._BATCH_BUSY_BTNS 对齐。

★ DESIGN 约束
     - 契约只描述行为/数据/形态；不持有任何 Tk 对象；可单测
     - 单一事实源：IMPORT_MENU_ITEMS、busy_label_for、btn_width_for 三者不重复声明
"""
from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# 1. 导入按钮二级菜单项（v1.5.7 布局定版）
# 「导入」统一管理文件类数据源：表格文件 或 图片文件（1..N 张，可多选）。
# 「识图」按钮 = 截取当前窗口（最小化→截 PDD 窗口→恢复→识别，无需菜单）。
# 「批量」按钮 = 自动滚动采集整页（多省份），保留一键直达。
# ─────────────────────────────────────────────────────────────────────────────
# 元素是 dict（不用 dataclass，保持纯 stdlib + 易单测）
# key - 唯一事件标识（gui 按 key 分派到 _import_table / _batch_images）
# label - 菜单项显示文字
# hint - 悬停提示（tk.Menu 当前未用，留作未来 Tooltip；现仅契约备查）
IMPORT_MENU_ITEMS = (
    {'key': 'import_table', 'label': '导入表格文件', 'hint': 'CSV / XLSX 表格导入（列映射预览可改）'},
    {'key': 'pick_images',  'label': '选择图片文件', 'hint': '本地挑选 1 张或多张图片批量识别'},
)


def find_menu_item(key, items=IMPORT_MENU_ITEMS):
    """按 key 查菜单项；未命中返回 None（绝不抛）。"""
    if not isinstance(key, str):
        return None
    for it in items or ():
        if it['key'] == key:
            return it
    return None


def menu_labels(items=IMPORT_MENU_ITEMS):
    """菜单项 label 列表（按 items 顺序），供 gui 渲染 tk.Menu 用。"""
    return [it['label'] for it in items or ()]


# ─────────────────────────────────────────────────────────────────────────────
# 2. 主页按钮统一宽度映射（用户意见 ：全部按钮同尺寸）
# ─────────────────────────────────────────────────────────────────────────────
# BTN_WIDTH_HOME 是「字符数」语义（与 _mk_btn 的 width 参数语义一致——
# 9pt 中文约 12px/字符；_mk_btn 内部 max(width*12, measure(text)+padx*2+22)，
# 短文本会被 width 拉到统一宽度，长文本自然宽胜出但视觉仍同框）。
BTN_WIDTH_HOME = 6

# kind → width 的可选微调表（保守默认：所有 kind 同宽，文本自然宽胜出）
_WIDTH_OVERRIDES = {}


def btn_width_for(kind):
    """主页按钮宽度：返回 _mk_btn 的 width 参数。

    所有 kind（dark/primary/text/ghost）默认 = BTN_WIDTH_HOME。
    返回 int；非法 kind 兜底 BTN_WIDTH_HOME（不抛，保持调用方零分支）。
    """
    if not isinstance(kind, str):
        return BTN_WIDTH_HOME
    return _WIDTH_OVERRIDES.get(kind, BTN_WIDTH_HOME)


# ─────────────────────────────────────────────────────────────────────────────
# 3. 批量忙时段按钮文案（gui._set_batch_btn_state 消费）
# ─────────────────────────────────────────────────────────────────────────────
# v1.5.7 定版后 busy 语义 key
# image → 「识图」按钮（实时截窗口路径，显示『识图中…』）
# import → 「导入」按钮（表格/图片统一入口，显示『导入中…』）
# export → 「导出」按钮 refresh/batch 同名字面
_BUSY_LABEL_SUFFIX = {
    'import':     '导入中…',
    'image':      '识图中…',
    'refresh':    '刷新中…',
    'batch':      '批量中…',
    'export':     '导出中…',
    'capture':    '截图中…',  # 预留（capture 语义与 image 同域，未来拆分用）
}


def busy_label_for(btn_key, orig_text):
    """批量忙时段按钮文案（替换 gui.busy_btn_text 的契约层）。

    行为：
      - btn_key ∈ BUSY_BTN_KEYS 且 orig_text 非空 → 按 key 查 _BUSY_LABEL_SUFFIX
      - btn_key 已知 + orig_text 为空 → 仍走 suffix，保证状态可见
      - btn_key 未知 / 非字符串 → 兜底（原文 + 中…）；任何异常 → '处理中…'
    绝不抛。
    """
    try:
        if isinstance(btn_key, str) and btn_key in _BUSY_LABEL_SUFFIX:
            return _BUSY_LABEL_SUFFIX[btn_key]
        t = str(orig_text or '').strip()
        return f'{t}中…' if t else '处理中…'
    except Exception:
        return '处理中…'


# 暴露给 gui.py 校验/对齐的常量集合
BUSY_BTN_KEYS = frozenset(_BUSY_LABEL_SUFFIX.keys())


# ─────────────────────────────────────────────────────────────────────────────
# 4. 双模型勾选位置契约（v1.5.7 定版：第二排 刷新 | 双模型）
# ─────────────────────────────────────────────────────────────────────────────
DUAL_MODEL_CHECKBUTTON_ROW = 'second'
DUAL_MODEL_CHECKBUTTON_LABEL = '🛡 双模型'


__all__ = [
    'IMPORT_MENU_ITEMS',
    'find_menu_item',
    'menu_labels',
    'BTN_WIDTH_HOME',
    'btn_width_for',
    'busy_label_for',
    'BUSY_BTN_KEYS',
    'DUAL_MODEL_CHECKBUTTON_ROW',
    'DUAL_MODEL_CHECKBUTTON_LABEL',
]