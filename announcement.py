"""
PDD EZ — 公告推送模块（借鉴 March7thAssistant app/tools/announcement.py）
从 GitHub 拉 announcement.json，启动时展示公告（标题+内容，可选图片 URL）。
静默失败：客户网络问题/JSON 格式错误都不打扰用户。

公告 JSON 格式（放仓库根，GitHub raw 拉取）：
{
  "hasAnnouncement": true,
  "announcement": {
    "title": "更新提示",
    "content": "欢迎使用 v1.4.1...",
    "image": {"type": "normal", "url": "https://..."}   # 可选
  }
}
"""
import json
import os
import threading
import urllib.request

# 公告 JSON 地址（默认 GitHub raw，仓库 Public 供客户拉取）
# 优先读 settings.json 的 announcement.url（仓库改名/fork/私有化部署时可改），
# 未配置时回退硬编码默认值（v1.4 审查修复）
DEFAULT_ANNOUNCEMENT_URL = "https://raw.githubusercontent.com/xuyuan985-star/pdd-inventory/main/announcement.json"
# kotori 镜像兜底（raw 直连失败时）
DEFAULT_ANNOUNCEMENT_URL_MIRROR = "https://github.kotori.top/https://raw.githubusercontent.com/xuyuan985-star/pdd-inventory/main/announcement.json"

_TIMEOUT = 8


def _get_announcement_urls() -> tuple:
    """读取公告 URL 配置（settings.json → announcement.url / announcement.mirror_url）。

    返回 (url, mirror_url)；读取失败/未配置时回退默认值。
    """
    url, mirror = DEFAULT_ANNOUNCEMENT_URL, DEFAULT_ANNOUNCEMENT_URL_MIRROR
    try:
        # 复用 utils.Config 唯一配置通道（settings.json 读写统一走它）
        from utils import Config
        cfg = Config.load()
        ann_cfg = cfg.get('announcement') or {}
        if isinstance(ann_cfg, dict):
            if ann_cfg.get('url'):
                url = str(ann_cfg['url'])
            if ann_cfg.get('mirror_url'):
                mirror = str(ann_cfg['mirror_url'])
    except Exception:
        pass  # 配置读取失败不阻塞公告（回退默认）
    return url, mirror


def _fetch_json(url: str):
    """拉取并解析 JSON；失败抛异常。"""
    req = urllib.request.Request(url, headers={"User-Agent": "PDD-EZ"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode('utf-8'))


def fetch_announcement():
    """拉取公告。返回 (title, content, image_url) 或 None（无公告/失败）。"""
    url, mirror = _get_announcement_urls()
    try:
        data = _fetch_json(url)
    except Exception:
        try:
            data = _fetch_json(mirror)
        except Exception:
            return None
    if not data or not data.get('hasAnnouncement'):
        return None
    ann = data.get('announcement', {})
    if not ann:
        return None
    # 字段类型强转：服务器 JSON 可能给 null/异常类型（dict/list），
    # 直接透传会炸 GUI Label（v1.4 审查修复）
    title = str(ann.get('title') or '公告')
    content = str(ann.get('content') or '')
    image_url = ''
    img = ann.get('image')
    if isinstance(img, dict) and img.get('url'):
        image_url = str(img['url'])
    return title, content, image_url


def check_announcement(win, show_func):
    """后台检查公告，有公告时在 GUI 主线程展示。
    win: tk 主窗口（用于 after 调度）；show_func(title, content, image_url) 展示回调。"""
    def _worker():
        try:
            result = fetch_announcement()
        except Exception:
            return
        if result:
            win.after(0, lambda: show_func(*result))
    threading.Thread(target=_worker, daemon=True).start()
