"""
PDD EZ — 公告推送模块（借鉴 March7thAssistant app/tools/announcement.py）
从 GitHub 拉 announcement.json，启动时展示公告（标题+内容，可选图片 URL）。
静默失败：客户网络问题/JSON 格式错误都不打扰用户。

公告 JSON 格式（放仓库根，GitHub raw 拉取）：
{
  "hasAnnouncement": true,
  "announcement": {
    "title": "更新提示",
    "content": "欢迎使用 v1.4...",
    "image": {"type": "normal", "url": "https://..."}   # 可选
  }
}
"""
import json
import threading
import urllib.request

# 公告 JSON 地址（GitHub raw，仓库 Public 供客户拉取）
ANNOUNCEMENT_URL = "https://raw.githubusercontent.com/xuyuan985-star/pdd-inventory/main/announcement.json"
# kotori 镜像兜底（raw 直连失败时）
ANNOUNCEMENT_URL_MIRROR = "https://github.kotori.top/https://raw.githubusercontent.com/xuyuan985-star/pdd-inventory/main/announcement.json"

_TIMEOUT = 8


def _fetch_json(url: str):
    """拉取并解析 JSON；失败抛异常。"""
    req = urllib.request.Request(url, headers={"User-Agent": "PDD-EZ"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode('utf-8'))


def fetch_announcement():
    """拉取公告。返回 (title, content, image_url) 或 None（无公告/失败）。"""
    try:
        data = _fetch_json(ANNOUNCEMENT_URL)
    except Exception:
        try:
            data = _fetch_json(ANNOUNCEMENT_URL_MIRROR)
        except Exception:
            return None
    if not data or not data.get('hasAnnouncement'):
        return None
    ann = data.get('announcement', {})
    if not ann:
        return None
    title = ann.get('title', '公告')
    content = ann.get('content', '')
    image_url = ''
    img = ann.get('image')
    if isinstance(img, dict) and img.get('url'):
        image_url = img['url']
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
