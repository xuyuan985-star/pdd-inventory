"""
PDD EZ — GitHub API 访问模块（借鉴 March7thAssistant version_check.py）
镜像列表 + 并发测速选最快 + 统一 fetch 入口。
主程序（gui.py）和更新器（updater.py）共用，避免重复实现。
"""
import json
import threading
import concurrent.futures
from urllib.request import urlopen, Request

REPO = "xuyuan985-star/pdd-inventory"

# GitHub API 镜像列表（官方 + kotori 镜像，国内网络可访问性不同）
_GITHUB_API_URLS = [
    f"https://api.github.com/repos/{REPO}/releases/latest",
    f"https://github.kotori.top/https://api.github.com/repos/{REPO}/releases/latest",
]

# 下载镜像（browser_download_url 替换前缀，客户网络下载 GitHub 资产失败时用）
_DOWNLOAD_MIRRORS = [
    "",  # 官方直连
    "https://github.kotori.top/",  # kotori 镜像前缀
]


def _find_fastest_api(timeout: int = 5) -> str:
    """并发测速，返回最快的镜像 URL；全部失败时回退第一个（March7th 同款）。"""
    stop_event = threading.Event()

    def _ping(url: str):
        if stop_event.is_set():
            return url, None
        try:
            start = __import__('time').monotonic()
            req = Request(url, method='HEAD',
                          headers={"Accept": "application/vnd.github.v3+json",
                                   "User-Agent": "PDD-EZ"})
            with urlopen(req, timeout=timeout) as resp:
                if stop_event.is_set():
                    return url, None
                elapsed = __import__('time').monotonic() - start
                if resp.status == 200:
                    return url, elapsed
        except Exception:
            pass
        return url, None

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(_GITHUB_API_URLS)))
    futures = {executor.submit(_ping, url): url for url in _GITHUB_API_URLS}
    try:
        for future in concurrent.futures.as_completed(futures):
            url, elapsed = future.result()
            if elapsed is not None:
                stop_event.set()
                executor.shutdown(wait=False, cancel_futures=True)
                return url
    except Exception:
        stop_event.set()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    executor.shutdown(wait=False)
    return _GITHUB_API_URLS[0]


def fetch_latest_release(timeout: int = 15) -> tuple:
    """获取最新 release 信息，返回 (tag_name, body, assets)。
    多镜像并发测速选最快；GET 失败时逐个尝试剩余镜像；全部失败抛异常。"""
    # 1) 测速选最快的 API URL
    url = _find_fastest_api()
    # 2) GET 拉取；失败逐个尝试其他镜像（HEAD 通 GET 不通的镜像会误杀，逐个兜底）
    _candidates = [url] + [u for u in _GITHUB_API_URLS if u != url]
    _last_err = None
    for cand in _candidates:
        try:
            req = Request(cand, headers={"Accept": "application/vnd.github.v3+json",
                                         "User-Agent": "PDD-EZ"})
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
                return (data.get("tag_name", ""), data.get("body", ""), data.get("assets", []))
        except Exception as e:
            _last_err = e
            continue
    raise _last_err if _last_err else RuntimeError("GitHub API 全部镜像不可达")


def mirror_download_url(url: str, prefer_mirror: bool = True) -> str:
    """下载 URL 镜像化：客户网络 GitHub 直连失败时可用 kotori 镜像前缀。
    prefer_mirror=True 时优先镜像（国内网络通常更快）；False 保持官方直连。"""
    if not url or 'github.com' not in url:
        return url
    if prefer_mirror:
        return "https://github.kotori.top/" + url
    return url
