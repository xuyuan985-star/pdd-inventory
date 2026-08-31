"""
PDD EZ — 更新器
独立小程序：从 GitHub Releases 拉取最新版本，替换后重启主程序。

v1.4 更新器重大修复（借鉴 March7thAssistant 更新引擎）：
- 下载进度通过 %TEMP%\\pdd_upd_progress.json 实时上报，GUI 轮询显示
- 覆盖安装改为逐文件 os.replace（同盘原子替换）+ 占用预检测 + 自身改名让位，
  绕开 Windows 不允许 rename 含运行中 exe 目录的 WinError 32
- 覆盖顺序（March7th 同款）：先覆盖其他文件 → 自身改名让位 → 最后覆盖自身，
  中途失败 updater 自身始终完好；无回滚概念，失败就是失败，程序目录零破坏
- 固定 exe 名 PDD EZ.exe（版本号只在程序内展示，取消版本号命名）
- 保留 v1.4 安全修复：target 目录校验（必须是 PDD EZ 程序目录）、
  绝不 rmtree 用户目录、自我转移（从待替换目录复制到 %TEMP% 运行）
"""
import os, sys, json, shutil, time, tempfile, zipfile, re
from urllib.request import urlopen, Request

# 更新器日志：与主程序共用 logs/ 目录（打包后同目录），更新过程全程留痕
try:
    from logger import log
except Exception:
    class _NullLog:
        def __getattr__(self, _):
            return lambda *a, **k: None
    log = _NullLog()

# REPO 常量已移至 github_api.py（get_latest_release/fetch 统一走该模块）
# 固定 exe 名（v1.4+ 取消版本号命名，版本号只在程序内展示——与 March7th 固定名设计一致）。
# 与 utils.VERSION 保持同步（当前 v1.4.6）；GUI 调用更新器时始终传 --target，此值仅作默认。
EXE_NAME = "PDD EZ.exe"

# ── 进度上报 ─────────────────────────────────────────────────────────
# 注：v1.4 起 GUI 下载在程序内完成（线程内进度条），updater 仅做 finalize——
# 此进度文件当前无读取方（保留供未来 GUI 轮询 finalize 进度用），写入成本极低不删。
PROGRESS_FILE = os.path.join(tempfile.gettempdir(), "pdd_upd_progress.json")
# 下载进度上报粒度：每 256KB 且 ≥0.1s 一次（防刷屏）
PROGRESS_REPORT_BYTES = 256 * 1024
PROGRESS_REPORT_MIN_INTERVAL = 0.1
PROGRESS_REPORT_MAX_INTERVAL = 0.5

def _write_progress(stage: str, message: str, current=None, total=None,
                    done: bool = False, error: str = ""):
    """写进度文件，GUI 轮询读取显示。全程 try/except，进度上报失败不影响更新。"""
    try:
        data = {"stage": stage, "message": message,
                "current": current, "total": total,
                "done": done, "error": error,
                "ts": time.time()}
        tmp = PROGRESS_FILE + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        os.replace(tmp, PROGRESS_FILE)
    except Exception:
        pass

def _clear_progress():
    try:
        if os.path.exists(PROGRESS_FILE):
            os.remove(PROGRESS_FILE)
    except Exception:
        pass


# ── v1.6.0 ：更新健康状态与自动回滚（docs/_v160.md §5.5 ）──────
# update_health.json 状态机：pending（finalize 落盘，待新版本自证可用）
#   → ok（主程序首帧渲染成功，mark_update_ok；同时清空启动计数与回滚池）
#   → rolled_back（启动失败 ≥3 次触发 rollback_to_old 成功）
#   → rollback_failed（自动回滚失败——显式报错引导人工，绝不重试，§4 防自我锁死）
# 宪法 §5 红线：update_health.json 在程序目录只由 updater（本模块函数）写入，
# 不进 git；_rollback_pool/ 是 updater 自建资产，清理绝不越出该目录、绝不 rmtree
# 用户程序目录本身。
UPDATE_HEALTH_FILE = "update_health.json"
ROLLBACK_POOL_DIR = "_rollback_pool"
ROLLBACK_POOL_MAX_AGE = 14 * 24 * 3600   # 回滚份过期阈值：14 天
ROLLBACK_FAIL_THRESHOLD = 3              # 启动失败计数 ≥3 → 触发自动回滚（gui 消费）


def _version_of(utils_py_path: str) -> str:
    """从 utils.py 源码提取 VERSION 常量（正则）；取不到 → ''（守卫，绝不抛）。"""
    try:
        with open(utils_py_path, 'r', encoding='utf-8', errors='replace') as f:
            m = re.search(r"^VERSION\s*=\s*['\"]([^'\"]+)['\"]", f.read(65536), re.M)
        return m.group(1) if m else ''
    except Exception:
        return ''


def _app_dir() -> str:
    """程序目录（update_health.json / _rollback_pool 所在）：源码形态=脚本目录，
    打包形态=exe 所在目录。健康检查与 GUI 默认走这里。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _update_health_path(target_dir: str = "") -> str:
    base = target_dir if target_dir else _app_dir()
    return os.path.join(base, UPDATE_HEALTH_FILE)


def _read_update_health(target_dir: str = "") -> dict:
    """守卫式读取 update_health.json：缺失/损坏/非 dict → {}（绝不抛）。"""
    try:
        with open(_update_health_path(target_dir), 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_update_health(target_dir: str, state: str, new_ver: str = "",
                         old_ver: str = "", extra: dict = None) -> bool:
    """原子写 update_health.json（tmp→os.replace）。失败不影响更新主流程，绝不抛。

    仅 updater 模块函数可调用本函数（宪法 §5：程序目录中该文件只由 updater 写入）。
    """
    try:
        data = {"state": state, "new_ver": new_ver, "old_ver": old_ver,
                "ts": time.time(), "launch_count": 0}
        if isinstance(extra, dict) and extra:
            data.update(extra)
        sf = _update_health_path(target_dir)
        tmp = sf + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, sf)
        return True
    except Exception:
        return False


def mark_update_ok(target_dir: str = "") -> bool:
    """主程序首帧渲染成功 → 新版本自证可用。仅在 state=='pending' 时写 'ok'
    （幂等保护：rolled_back/rollback_failed 终态不被覆盖）。"""
    try:
        st = _read_update_health(target_dir)
        if str(st.get('state') or '') != 'pending':
            return False
        ok = _write_update_health(target_dir, 'ok',
                                  new_ver=str(st.get('new_ver') or ''),
                                  old_ver=str(st.get('old_ver') or ''))
        if ok:
            _prune_rollback_pool(_rollback_pool_dir(target_dir), remove_all=True)
        return ok
    except Exception:
        return False


def bump_launch_count(target_dir: str = "") -> int:
    """pending 期间每次启动计数 +1（渲染失败即崩溃的版本靠它累积证据）。
    写者仍是 updater 模块（宪法 §5 边界内）。返回新计数；异常返回 0。"""
    try:
        st = _read_update_health(target_dir)
        if str(st.get('state') or '') != 'pending':
            return 0
        n = int(st.get('launch_count') or 0) + 1
        extra = {k: v for k, v in st.items() if k not in (
            'state', 'new_ver', 'old_ver', 'ts', 'launch_count')}
        _write_update_health(target_dir, 'pending',
                             new_ver=str(st.get('new_ver') or ''),
                             old_ver=str(st.get('old_ver') or ''),
                             extra=dict(extra, launch_count=n))
        return n
    except Exception:
        return 0


def _rollback_pool_dir(target_dir: str = "") -> str:
    base = target_dir if target_dir else _app_dir()
    return os.path.join(base, ROLLBACK_POOL_DIR)


def _prune_rollback_pool(pool_dir: str, max_age: int = ROLLBACK_POOL_MAX_AGE,
                         remove_all: bool = False) -> int:
    """回滚池过期清理：删 mtime 早于 max_age 的池内文件（remove_all=True 全清）。
    只碰 pool_dir 内部，绝不越出（宪法 §5）；返回删除文件数。"""
    try:
        if not pool_dir or not os.path.isdir(pool_dir):
            return 0
        pool_real = os.path.realpath(pool_dir) + os.sep
        now = time.time()
        removed = 0
        for name in os.listdir(pool_dir):
            p = os.path.join(pool_dir, name)
            if not os.path.isfile(p):
                continue
            if not (os.path.realpath(p) + os.sep).startswith(pool_real):
                continue  # 防 symlink 逃逸
            try:
                if remove_all or (now - os.path.getmtime(p)) > max_age:
                    os.remove(p)
                    removed += 1
            except Exception:
                pass
        try:
            # 仅终态清池（remove_all）才移除空目录；finalize 入口的常规过期
            # 清理不得删除刚创建的空池（否则随后的备份 os.replace 会 WinError 3）
            if remove_all and not os.listdir(pool_dir):
                os.rmdir(pool_dir)
        except Exception:
            pass
        return removed
    except Exception:
        return 0


def _pause():
    """等待按键退出；无 stdin（被 GUI 拉起）时直接通过，不抛 EOFError"""
    try:
        input("按回车退出...")
    except EOFError:
        pass
    except Exception:
        pass


def _stream_to_file(resp, dest, size, name, progress_stage):
    """流式下载到文件 + 进度上报（镜像/官方共用）。
    下载结束校验大小：服务器声明 Content-Length 但实际只传了一部分时
    必须失败（否则 zip 解压/exe 损坏，v1.4 审查修复）。"""
    total = size or 0
    if total == 0:
        try:
            total = int(resp.headers.get('Content-Length', 0) or 0)
        except Exception:
            total = 0
    downloaded = 0
    last_reported = 0
    last_report_time = time.monotonic()
    with open(dest, 'wb') as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            now = time.monotonic()
            if (downloaded - last_reported >= PROGRESS_REPORT_BYTES
                    and now - last_report_time >= PROGRESS_REPORT_MIN_INTERVAL) \
                    or now - last_report_time >= PROGRESS_REPORT_MAX_INTERVAL:
                _write_progress(progress_stage, f"正在下载 {name}", downloaded, total)
                last_reported = downloaded
                last_report_time = now
    _write_progress(progress_stage, f"正在下载 {name}", downloaded, total)
    # 大小校验：声明的总大小与实际下载不一致 → 下载不完整，抛错
    if total and downloaded != total:
        raise IOError(
            f"下载不完整: 声明 {total} 字节，实际 {downloaded} 字节"
        )



def get_latest_release():
    """从 GitHub API 获取最新 release 信息（多镜像测速选最快）"""
    try:
        from github_api import fetch_latest_release
        tag, body, assets = fetch_latest_release(timeout=15)
        return tag, assets
    except Exception as e:
        print(f"[更新器] 检查失败: {e}")
        return None, []

def download_asset(asset, dest, progress_stage: str = "download", expected_sha256=None):
    """下载 release 附件到 dest，返回 (success: bool, error: str)。
    下载过程中上报进度（current/total bytes）。

    v1.4.8 镜像链（按 settings.update.* 顺序尝试，空配置=跳过该源）：
      1) GitHub（kotori 镜像前缀，prefer_mirror=True）
      2) GitHub 官方直连（browser_download_url）
      3) settings.update.mirror_oss     阿里云 OSS 模板（URL 末尾追加 asset.name）
      4) settings.update.mirror_lanzou  蓝奏云直链模板（URL 末尾追加 asset.name）
    任一源成功后即返回；任一步骤失败（含 HTTP 错误/超时/校验不匹配）自动换下一源，
    失败的临时文件清理后继续。

    v1.4.8 -fix（）：expected_sha256 非空时，下载成功后立即算 SHA256 比对；
    不匹配 → 视为该源不可信，log 警告 + 清理残文件 + continue 换下一源（不再走下游校验）。
    expected_sha256 缺省 None 时保持原行为（下游 main() 仍做校验）。
    """
    from github_api import mirror_download_url
    name = asset["name"]
    size = asset.get("size", 0)
    print(f"[更新器] 下载 {name} ({size} bytes)...")
    _write_progress(progress_stage, f"正在下载 {name}", 0, size)
    # 构建下载源链：[(label, url), ...]，按 settings.update.* 顺序追加
    sources = [
        ("github-kotori", mirror_download_url(asset["browser_download_url"], prefer_mirror=True)),
        ("github", asset["browser_download_url"]),
    ]
    try:
        _cfg = _read_update_mirrors()
    except Exception:
        _cfg = {}
    _oss = (_cfg.get("mirror_oss") or "").strip().rstrip("/")
    if _oss:
        sources.append(("oss", f"{_oss}/{name}"))
    _lanzou = (_cfg.get("mirror_lanzou") or "").strip().rstrip("/")
    if _lanzou:
        sources.append(("lanzou", f"{_lanzou}/{name}"))

    attempted = []
    for label, url in sources:
        try:
            req = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "PDD-EZ-Updater"})
            with urlopen(req, timeout=120) as resp:
                _stream_to_file(resp, dest, size, name, progress_stage)
            # v1.4.8 -fix（）：期望哈希给定 → 立即就地校验；不匹配则换源
            if expected_sha256:
                try:
                    if not _verify_sha256(dest, expected_sha256):
                        attempted.append(f"{label}(hash_mismatch)")
                        print(f"[更新器] 源 {label} 哈希不匹配，已换下一源")
                        try:
                            log.warning(f"download source {label} hash mismatch, trying next")
                        except Exception:
                            pass
                        try:
                            if os.path.exists(dest):
                                os.remove(dest)
                        except OSError:
                            pass
                        continue
                except Exception as _he:
                    # 哈希计算本身失败（IO 错误等）→ 视为该源不可用，换下一源
                    attempted.append(f"{label}(hash_err:{_trunc_err(_he)})")
                    print(f"[更新器] 源 {label} 哈希校验异常: {_he}")
                    try:
                        log.warning(f"download source {label} hash error: {_he}")
                    except Exception:
                        pass
                    try:
                        if os.path.exists(dest):
                            os.remove(dest)
                    except OSError:
                        pass
                    continue
            print(f"[更新器] 源 {label} 下载成功")
            try:
                log.info(f"download source: {label} url={url}")
            except Exception:
                pass
            return True, ""
        except Exception as e:
            attempted.append(f"{label}({_trunc_err(e)})")
            print(f"[更新器] 源 {label} 失败: {e}")
            try:
                log.warning(f"download source {label} failed: {e}")
            except Exception:
                pass
            # 清理可能写了一半的临时文件，避免下一次源误用旧内容
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass
            continue
    msg = "所有下载源均失败: " + ", ".join(attempted)
    print(f"[更新器] {msg}")
    return False, msg


def _trunc_err(e: Exception) -> str:
    """异常信息截断到 ~80 字，日志更易读。"""
    s = str(e) or type(e).__name__
    if len(s) > 80:
        s = s[:80] + "..."
    return s


def _candidate_settings_paths() -> list:
    """v1.4.8 -fix（）：返回候选 settings.json 路径，按优先级排序。
    - frozen（PyInstaller 打包）：%APPDATA%/PDD补货助手 → exe 同目录（便携兜底）
    - 非 frozen（源码运行）：脚本目录
    与 utils.get_base_dir 的逻辑同款但内联（updater.py 禁止 import utils）。
    """
    paths = []
    if getattr(sys, 'frozen', False):
        # 1) APPDATA 优先（主程序实际写入位置）
        try:
            appdata = os.environ.get('APPDATA') or os.path.expanduser('~')
            paths.append(os.path.join(appdata, 'PDD补货助手', 'settings.json'))
        except Exception:
            pass
        # 2) exe 同目录兜底（便携模式 / 自定义部署）
        try:
            paths.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                                      'settings.json'))
        except Exception:
            pass
    else:
        # 源码运行：脚本目录
        try:
            paths.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      'settings.json'))
        except Exception:
            pass
    return paths


def _read_update_mirrors() -> dict:
    """读取 settings.update.mirror_oss / mirror_lanzou 镜像配置。
    不依赖 utils.Config（守住约束：updater.py 独立可移植，单文件 + 零 utils 依赖，
    防止 utils.py 变更反向污染更新器）。失败/缺键返回空 dict → 调用方按空=跳过处理。

    v1.4.8 -fix（）：路径查找顺序
    - frozen：先 %APPDATA%/PDD补货助手/settings.json（主程序写入位置），再 exe 同目录
    - 非 frozen：脚本目录/settings.json
    找到第一个存在的文件即用；都不存在返 {}。
    """
    for sf in _candidate_settings_paths():
        try:
            if not os.path.exists(sf):
                continue
            with open(sf, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            upd = data.get("update")
            if isinstance(upd, dict):
                return upd
        except Exception:
            continue
    return {}


def _wait_pid_exit(pid: int, expected_exe: str = '', timeout: float = 30.0):
    """通过 Windows API 等待指定 PID 的进程退出。超时返回 False。
    若 expected_exe 给出，先校验进程路径是否匹配，防止 PID 回收误判。"""
    import ctypes
    from ctypes import wintypes
    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    # 64 位句柄安全：显式 restype/argtypes（默认 c_int 会截断句柄）
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                    wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]

    h = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return True  # 进程已不存在（无句柄可泄漏）

    try:
        # 校验路径（如果提供了期望路径）
        if expected_exe:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            # QueryFullProcessImageNameW
            if hasattr(kernel32, 'QueryFullProcessImageNameW'):
                ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
                if ok:
                    actual = buf.value.lower().rstrip('\\')
                    expected = expected_exe.lower().rstrip('\\')
                    if actual != expected:
                        # PID 已被回收/复用（原进程已死，句柄由 finally 释放）
                        return True

        ret = kernel32.WaitForSingleObject(h, int(timeout * 1000))
        if ret == 0:  # WAIT_OBJECT_0
            return True
        return False
    finally:
        kernel32.CloseHandle(h)

def _verify_sha256(path: str, expected: str) -> bool:
    """验证文件 SHA256 哈希，不匹配返回 False"""
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    actual = h.hexdigest()
    return actual.lower() == expected.lower()


# ── 覆盖安装（抄 March7thAssistant update_engine 的逐文件覆盖）──────

def _is_file_locked(path: str) -> bool:
    """检测文件是否被占用（无法以独占方式打开）。
    用 CreateFileW + FILE_SHARE_NONE：比 os.open 更可靠——os.open 默认共享模式
    允许读写，检测不到共享读占用（如客户开着 Excel 读导出文件）。"""
    if not os.path.exists(path):
        return False
    try:
        import ctypes
        from ctypes import wintypes
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        # 显式 restype=HANDLE：默认 c_int 在 64 位下会截断句柄
        _kernel32 = ctypes.windll.kernel32
        _kernel32.CreateFileW.restype = wintypes.HANDLE
        _kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                          wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                          wintypes.HANDLE]
        _kernel32.CloseHandle.restype = wintypes.BOOL
        _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        # FILE_SHARE_NONE：任何其他句柄都算占用
        h = _kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE,
                                  0, None, OPEN_EXISTING, 0, None)
        # v1.4.5（）：restype=HANDLE 在 CPython 3.11 ctypes 下返回 int 而非对象
        # （旧代码 `h and h != -1` 恒真误判"未占用"；上一版 `h.value` 对 int 抛异常落入
        # 恒锁 fallback）。统一按整数判 INVALID_HANDLE_VALUE(0xFFFFFFFFFFFFFFFF/-1)/0。
        try:
            hv = int(h) if h is not None else -1
            if hv > 0x7FFFFFFFFFFFFFFF:  # 负数包装（-1 作为无符号 64 位）
                hv -= (1 << 64)
        except Exception:
            hv = -1
        if hv != -1 and hv != 0:
            _kernel32.CloseHandle(h)
            return False
        return True
    except Exception:
        # 非 Windows / ctypes 失败：回退 os.open 检测（O_RDWR 打开成功近似"可写"；
        # 不用 O_EXCL——它会因文件已存在恒失败，反而永远误报锁定）
        try:
            fd = os.open(path, os.O_RDWR)
            os.close(fd)
            return False
        except (OSError, PermissionError):
            return True

def _check_target_files_locked(files) -> list:
    """预检测目标文件占用，返回被锁定文件列表。"""
    locked = []
    self_path = _get_self_path()
    for src, dest in files:
        if self_path and os.path.normcase(os.path.normpath(os.path.abspath(dest))) == self_path:
            continue  # 自身文件后面单独处理（改名让位）
        if os.path.exists(dest) and _is_file_locked(dest):
            locked.append(dest)
    return locked

def _get_self_path() -> str:
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(sys.argv[0])))
    except Exception:
        return ""

def _needs_overwrite(src: str, dest: str) -> bool:
    """判断是否需要覆盖：目标不存在 / 大小不同 / 内容不同。"""
    if not os.path.exists(dest):
        return True
    try:
        if os.stat(src).st_size != os.stat(dest).st_size:
            return True
        if os.stat(src).st_size == 0 and os.stat(dest).st_size == 0:
            return False  # 双方都 0 字节：无需覆盖（避免旧内容残留：新版清空时大小必不同）
    except OSError:
        return True
    # 大小相同 → 比较内容（64KB 分块）
    try:
        with open(src, 'rb', buffering=65536) as sf, open(dest, 'rb', buffering=65536) as df:
            while True:
                sc, dc = sf.read(65536), df.read(65536)
                if sc != dc:
                    return True
                if not sc:
                    return False
    except OSError:
        return True

def _overwrite_files(files, progress_stage: str = "cover",
                     backup_pool: str = "", entries_out: list = None):
    """逐文件覆盖：os.replace 备份旧文件 + copy2 落新文件；失败逆序回滚并中断
    （v1.4.5 ：旧实现逐文件覆盖失败不回滚，程序目录留新旧混合版本）。
    返回 (ok, skipped_files)。

    v1.6.0 ：backup_pool 非空时（do_finalize 路径），旧文件备份集中到该池目录
    （文件名 = 目标相对路径扁平化 + 内容防撞后缀），成功后**保留**作为自动回滚份，
    并把 [{dest_rel, backup}] 追加到 entries_out；pool 为空时保持 v1.4 行为：
    备份散落 dest+'.old_upd'、成功即删。"""
    total = len(files)
    completed = 0
    skipped = []
    created_dirs = set()
    backed_up = []  # [(dest, backup)]
    _ent_base = len(entries_out) if entries_out is not None else 0

    def _rollback(backups):
        for dest, backup in reversed(backups):
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass
            try:
                if os.path.exists(backup):
                    os.replace(backup, dest)
            except OSError:
                print(f"[更新器] 回滚失败，请手动恢复 {backup} → {dest}")

    for src, dest in files:
        parent = os.path.dirname(dest)
        if parent and parent not in created_dirs:
            try:
                os.makedirs(parent, exist_ok=True)
            except Exception:
                pass
            created_dirs.add(parent)
        try:
            backup = None
            if os.path.exists(dest):
                if backup_pool:
                    # ：备份集中到 _rollback_pool/，扁平名防子目录缺失
                    _rel = os.path.relpath(dest, os.path.dirname(
                        os.path.abspath(backup_pool)))
                    _flat = _rel.replace('..', '__up__').replace(
                        ':', '_').replace(os.sep, '__')
                    backup = os.path.join(
                        backup_pool, f"{_flat}.{completed}.old_upd")
                else:
                    backup = dest + '.old_upd'
                if os.path.exists(backup):
                    try:
                        os.remove(backup)
                    except Exception:
                        pass
                os.replace(dest, backup)  # 原子挪走旧文件，留作回滚
                backed_up.append((dest, backup))
                if entries_out is not None:
                    entries_out.append({
                        'dest_rel': os.path.relpath(
                            dest, os.path.dirname(os.path.abspath(backup_pool))),
                        'backup': os.path.basename(backup)})
            shutil.copy2(src, dest)
            completed += 1
            _emit_cover_progress(progress_stage, completed, total)
        except Exception as e:
            print(f"[更新器] 覆盖失败 {dest}: {e}")
            if backed_up:
                print(f"[更新器] 回滚已覆盖的 {len(backed_up)} 个文件…")
            _rollback(backed_up)
            skipped.extend(d for d, _ in backed_up)
            backed_up.clear()
            if entries_out is not None:
                del entries_out[_ent_base:]  # 失败轮次的池条目已消费，作废
            skipped.append(dest)
            break  # 事务失败，中断整批覆盖
    # 全部成功：pool 模式保留备份（= 自动回滚份，由 mark_update_ok/过期清理回收）；
    # 散落模式维持旧行为成功即删（防 .old_upd 残留）
    for dest, backup in backed_up:
        if backup_pool:
            continue
        try:
            os.remove(backup)
        except Exception:
            pass
    return len(skipped) == 0, skipped

_last_cover_emit = [0]

def _emit_cover_progress(stage: str, completed: int, total: int):
    """覆盖进度：每 ~1% 上报一次，避免刷屏。"""
    if total <= 0:
        return
    pct_step = max(1, total // 100)
    if completed >= total or completed == 1 or completed - _last_cover_emit[0] >= pct_step:
        _write_progress(stage, f"正在覆盖安装 ({int(completed * 100 / total)}%)", completed, total)
        _last_cover_emit[0] = completed


def _extract_zip(zip_path: str, extract_dir: str) -> str:
    """解压更新包到 extract_dir，返回解压出的 PDD EZ 程序目录。
    保留 v1.4 的防护：symlink 拒绝、zip-bomb 上限、路径遍历校验。
    v1.4 审查加固：不用 zf.extract()（Windows 下可能先建文件再产生链接行为），
    改 open+copyfileobj 手动写文件，只接受常规文件，从根上杜绝链接逃逸。"""
    # 清理已有解压目录，防上次残留污染根目录识别（March7th extractor 同款）
    if os.path.isdir(extract_dir):
        shutil.rmtree(extract_dir, ignore_errors=True)
    os.makedirs(extract_dir, exist_ok=True)
    extract_dir_real = os.path.realpath(extract_dir) + os.sep
    _total_size = 0
    _MAX_EXTRACT = 2 * 1024**3  # 解压总量上限 2GB，防 zip-bomb
    with zipfile.ZipFile(zip_path, 'r') as zf:
        for zi in zf.infolist():
            # 拒绝 symlink/链接/特殊类型成员（Unix symlink 0120000；
            # Windows junction/reparse point 常以 dir 类型出现——统一只放行
            # 常规文件 0100000 与目录 0040000，其余全部拒绝）
            _mode = (zi.external_attr >> 16) & 0o170000
            if _mode == 0o120000 or (_mode and _mode not in (0o100000, 0o040000)):
                print(f"[更新器] 拒绝链接/特殊成员: {zi.filename}")
                continue
            # 解压总量上限，防 zip-bomb
            _total_size += zi.file_size
            if _total_size > _MAX_EXTRACT:
                print(f"[更新器] 解压总量超过上限，拒绝安装")
                raise RuntimeError("update package too large")
            # 路径遍历防护：规范化后校验必须在 extract_dir 内
            member_path = os.path.realpath(os.path.join(extract_dir, zi.filename))
            if not member_path.startswith(extract_dir_real):
                print(f"[更新器] 拒绝路径遍历: {zi.filename}")
                continue
            # 手动解压：目录 mkdir，常规文件 open+copyfileobj 流式写入
            if zi.is_dir():
                os.makedirs(member_path, exist_ok=True)
                continue
            if not zi.filename:
                continue
            try:
                os.makedirs(os.path.dirname(member_path), exist_ok=True)
                with zf.open(zi, 'r') as _src, open(member_path, 'wb') as _dst:
                    shutil.copyfileobj(_src, _dst, length=1024 * 256)
            except Exception as e:
                print(f"[更新器] 解压成员失败 {zi.filename}: {e}")
                raise
    # 找 PDD EZ 程序目录
    for item in os.listdir(extract_dir):
        item_path = os.path.join(extract_dir, item)
        if os.path.isdir(item_path) and item.startswith("PDD EZ"):
            return item_path
    return extract_dir


def _pick_main_exe(target_dir: str) -> str:
    """选取 PDD EZ 主 exe。
    v1.4+ 固定名 `PDD EZ.exe` 优先；找不到时回退版本扫描（兼容旧版 `PDD EZ v1.4.exe`
    升级场景——旧目录里只有带版本号的 exe）。无匹配返回空串。"""
    fixed = os.path.join(target_dir, EXE_NAME)
    if os.path.isfile(fixed):
        return fixed
    import re as _re
    _best = ""
    _best_key = (-1, -1, -1)
    try:
        _items = os.listdir(target_dir)
    except Exception:
        return _best
    for _f in _items:
        if not (_f.lower().startswith('pdd ez') and _f.lower().endswith('.exe')
                and 'updater' not in _f.lower()):
            continue
        _m = _re.search(r'v(\d+)\.(\d+)(?:\.(\d+))?', _f)
        _key = tuple(int(x) for x in _m.groups(default='0')) if _m else (0, 0, 0)
        _key = (_key + (0, 0, 0))[:3]
        if _key > _best_key:
            _best_key = _key
            _best = _f
    return os.path.join(target_dir, _best) if _best else ""


def _is_program_dir(target_dir: str) -> bool:
    """安全底线：target_dir 必须是 PDD EZ 程序目录。
    判断（v1.4 审查收紧 + v1.4.5 ）：目录内必须有 PDD EZ 主 exe，
    且必须有 PyInstaller onedir 的 _internal 目录——仅名字像（C:\\PDD EZ Backup、
    含 PDD EZ_v1.4.3.exe 的普通目录）不再放行，防止整体替换覆盖到非程序目录。"""
    if not os.path.isdir(target_dir):
        return False
    try:
        _names = os.listdir(target_dir)
    except Exception:
        return False
    _has_main = False
    for f in _names:
        fl = f.lower()
        if not (fl.startswith('pdd ez') and fl.endswith('.exe') and 'updater' not in fl):
            continue
        # 固定名 PDD EZ.exe（v1.4+ 主程序固定名）或带版本号的 PDD EZ_vX.Y(...).exe
        if fl == 'pdd ez.exe' or re.search(r'pdd ez[ _]?v\d', fl):
            _has_main = True
            break
    _has_internal = os.path.isdir(os.path.join(target_dir, '_internal'))
    return _has_main and _has_internal


def _ensure_self_renamed(files) -> bool:
    """自身文件改名让位（否则运行中的 updater 无法被覆盖）。返回 True 表示已处理/无需处理。"""
    self_path = os.path.abspath(sys.argv[0])
    norm_self = os.path.normcase(os.path.normpath(self_path))
    targets = {os.path.normcase(os.path.normpath(d)) for _, d in files}
    if norm_self not in targets:
        return True
    root, ext = os.path.splitext(self_path)
    backup = f"{root}.old{ext}"
    if os.path.exists(backup):
        try:
            os.remove(backup)
        except Exception:
            pass
    try:
        os.replace(self_path, backup)
        return True
    except Exception as e:
        print(f"[更新器] 重命名自身失败: {e}")
        return False


def _restore_self(backup: str) -> bool:
    """覆盖失败时恢复自身备份（.old → 原名）。返回是否恢复成功。

    恢复失败（杀软占用/权限变化）时用 copy2 兜底——updater 绝不能
    只剩 .old 名（主名缺失 = 更新器损坏，下次无法自升级）。
    """
    if not backup or not os.path.exists(backup):
        return False
    _target = os.path.abspath(sys.argv[0])
    try:
        os.replace(backup, _target)
        return True
    except Exception as e:
        print(f"[更新器] 恢复自身备份 os.replace 失败: {e}，尝试 copy2 兜底")
    try:
        shutil.copy2(backup, _target)
        # 兜底成功：清理 .old（失败也保留，下次更新 _ensure_self_renamed 会再清）
        try:
            os.remove(backup)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[更新器] 恢复自身备份 copy2 也失败: {e}")
        print(f"[更新器] 警告: 更新器遗留为 {backup}，请手动恢复为 {_target}")
        return False


def _cover_with_self_handling(files, backup_pool: str = "",
                              entries_out: list = None) -> tuple[bool, list]:
    """逐文件覆盖（March7th 顺序）：先覆盖其他文件 → 再自身改名让位 → 最后覆盖自身。
    中途失败时 updater 自身始终完好；自身覆盖失败时恢复 .old 备份。
    返回 (ok, skipped_files)。backup_pool/entries_out 透传 _overwrite_files（）。"""
    self_renamed_to = ""
    _self_abs = os.path.abspath(sys.argv[0])
    others = []
    self_items = []
    for src, dest in files:
        if os.path.normcase(os.path.normpath(os.path.abspath(dest))) == \
                os.path.normcase(os.path.normpath(_self_abs)):
            self_items.append((src, dest))
        else:
            others.append((src, dest))

    # 1) 先覆盖其他文件（不动 updater 自身）
    ok, skipped = _overwrite_files(others, backup_pool=backup_pool,
                                   entries_out=entries_out)
    if skipped:
        return False, skipped

    # 2) 自身改名让位（其他文件已成功，最后才动自身）
    if self_items:
        if not _ensure_self_renamed(files):
            return False, [os.path.basename(_self_abs) + " (自身改名失败)"]
        _root, _ext = os.path.splitext(_self_abs)
        _bak = f"{_root}.old{_ext}"
        if os.path.exists(_bak):
            self_renamed_to = _bak

    # 3) 最后覆盖自身文件
    ok, skipped = _overwrite_files(self_items, backup_pool=backup_pool,
                                   entries_out=entries_out)
    if skipped:
        # 恢复自身备份（覆盖失败，不让 updater 残留在 .old 名）
        if self_renamed_to:
            _restore_self(self_renamed_to)
        return False, skipped

    # 成功：清理自身 .old 备份（MoveFileExW 延迟删除——系统重启后自动清，
    # 等价 March7th 的 helper 进程清理，无需额外进程）
    if self_renamed_to and os.path.exists(self_renamed_to):
        try:
            import ctypes
            ok_del = ctypes.windll.kernel32.MoveFileExW(self_renamed_to, None, 4)  # MOVEFILE_DELAY_UNTIL_REBOOT
            if not ok_del:
                print(f"[更新器] 警告: 无法延迟删除自身备份 {self_renamed_to}，下次更新会覆盖清理")
        except Exception as e:
            print(f"[更新器] 警告: 清理自身备份失败: {e}")

    return True, []


def _apply_deleted_files(extracted_dir: str, target_dir: str) -> int:
    """v1.4.5 ：应用包内 deleted-files.txt —— 自上版本起删除的运行时资源/
    模板/文档，目标端白名单删除，防旧 dll/旧模板/旧文档永久残留。
    extracted_dir 须是 _extract_zip 返回的 PDD EZ 程序目录。返回删除文件数。"""
    _del_spec = os.path.join(extracted_dir, 'deleted-files.txt')
    if not os.path.exists(_del_spec):
        _del_spec = os.path.join(extracted_dir, 'PDD EZ', 'deleted-files.txt')  # 兼容未剥顶层
    if not os.path.exists(_del_spec):
        return 0
    try:
        with open(_del_spec, encoding='utf-8') as _f:
            _deleted = [l.strip() for l in _f if l.strip()]
    except Exception:
        return 0
    _deleted_ok = 0
    for _rel in _deleted:
        _nor = str(_rel).replace('/', os.sep)
        # 白名单：仅 templates/ 与固定资源文件；拒绝路径穿越/绝对路径；绝不删 exe/dll
        if not (_nor.startswith('templates' + os.sep) or _nor in (
                'icon.ico', 'regions.json', 'settings_template.json', '使用说明.txt')):
            continue
        if '..' in _nor.split(os.sep) or os.path.isabs(_nor):
            continue
        _dest = os.path.join(target_dir, _nor)
        if _dest.lower().endswith(('.exe', '.dll')):
            continue
        try:
            if os.path.exists(_dest):
                os.remove(_dest)
                _deleted_ok += 1
        except Exception:
            pass
    if _deleted_ok:
        print(f"[更新器] 删除旧文件 {_deleted_ok} 个")
    return _deleted_ok


def do_finalize(file_path: str, extract_dir: str, target_dir: str, wait_pid: int = 0,
                target_main: str = "") -> int:
    """finalize 模式：更新包已下载好，执行 等待退出→解压→覆盖→清理→启动。

    返回退出码：0=成功，1=失败（调用方 GUI 显示）。
    """
    try:
        # 0) 等待主程序退出
        if wait_pid:
            _write_progress("prepare", "正在等待主程序退出...")
            if not _wait_pid_exit(wait_pid, expected_exe=target_main, timeout=30.0):
                print("[更新器] 警告: 主程序未在 30 秒内退出，继续执行")

        # 1) 解压
        _write_progress("extract", "正在解压更新包...")
        extracted = _extract_zip(file_path, extract_dir)
        print(f"[更新器] 解压完成: {extracted}")
        log.info(f"解压完成: {extracted}")

        # 2) 安全底线：target_dir 必须是 PDD EZ 程序目录
        if not _is_program_dir(target_dir):
            msg = f"目标目录不是 PDD EZ 程序目录（{target_dir}），拒绝更新"
            print(f"[更新器] [拒绝] {msg}")
            log.error("[拒绝] " + msg)
            _write_progress("error", msg, error=msg)
            return 1

        # 2.5) ：回滚池入口（创建 + 过期清理）+ 旧版本号采集（必须在覆盖前读！）
        pool = _rollback_pool_dir(target_dir)
        try:
            os.makedirs(pool, exist_ok=True)
        except Exception:
            pool = ""  # 池不可建 → 退回散落 .old_upd 模式（自动回滚降级为不可用）
        _prune_rollback_pool(pool)
        _old_ver = _version_of(os.path.join(target_dir, 'utils.py'))
        pool_entries: list = []

        # 3) 收集待覆盖文件（仅变化的）
        # ⚠ 更新包 zip 顶层是 "PDD EZ/" 目录（_build_update_zip.py arcname 带目录名），
        #   必须剥掉再拼 target_dir——否则覆盖到 target_dir/PDD EZ/ 子目录，更新不生效
        #   （auto 模式有剥目录逻辑，finalize 漏了，v1.4.1 修复）
        _write_progress("cover", "正在检测文件占用...")
        files = []
        for root, _, fnames in os.walk(extracted):
            for fname in fnames:
                src = os.path.join(root, fname)
                rel = os.path.relpath(src, extracted)
                _parts = rel.split(os.sep)
                if len(_parts) >= 2 and _parts[0].startswith('PDD EZ'):
                    rel = os.path.join(*_parts[1:])
                dest = os.path.join(target_dir, rel)
                if _needs_overwrite(src, dest):
                    files.append((src, dest))
        print(f"[更新器] 待覆盖 {len(files)} 个文件")
        log.info(f"待覆盖 {len(files)} 个文件")

        # 4) 预检测占用（自身文件除外，后面单独处理）
        locked = _check_target_files_locked(files)
        if locked:
            _names = "、".join(os.path.basename(p) for p in locked[:10])
            msg = f"以下文件被占用，无法覆盖：{_names}（请关闭占用程序后重试）"
            print(f"[更新器] [阻塞] {msg}")
            log.error("[阻塞] " + msg)
            _write_progress("error", msg, error=msg)
            return 1

        # 5) 覆盖（March7th 顺序：先覆盖其他文件 → 再自身改名让位 → 最后覆盖自身，
        # 中途失败 updater 自身始终完好）。：备份集中 _rollback_pool/，
        # 成功后保留为自动回滚份（manifest 记录 dest_rel ↔ 池内文件名）
        ok, skipped = _cover_with_self_handling(files, backup_pool=pool,
                                                entries_out=pool_entries)
        if skipped:
            _names = "、".join(os.path.basename(p) for p in skipped[:10])
            msg = f"{len(skipped)} 个文件覆盖失败：{_names}"
            print(f"[更新器] {msg}")
            log.error(msg)
            _write_progress("error", msg, error=msg)
            return 1

        # 5.5) ：回滚清单落池（pending 状态的回滚依据；失败不阻断更新本身）
        try:
            if pool and pool_entries:
                with open(os.path.join(pool, 'manifest.json'), 'w',
                          encoding='utf-8') as _mf:
                    json.dump(pool_entries, _mf, ensure_ascii=False)
        except Exception as _e:
            print(f"[更新器] 警告: 回滚清单写入失败（自动回滚将不可用）: {_e}")

        # 3.5 删除清单（v1.4.5 ，置于覆盖成功之后）：包内 deleted-files.txt =
        # 自上版本起删除的运行时资源/模板/文档，目标端同步删除，防旧 dll/旧模板/旧文档永久残留。
        # 覆盖成功后再删，避免覆盖失败时旧模板/资源已被删、更新又不生效的中间态半损坏。
        _deleted_n = _apply_deleted_files(extracted, target_dir)

        # 6) ：落 update_health.json（state='pending'，待新版本首帧自证）。
        # 宪法 §5：该文件只由 updater 写入；new_ver 取覆盖后的目标端 utils.py。
        _write_update_health(target_dir, 'pending',
                             new_ver=_version_of(os.path.join(target_dir, 'utils.py')),
                             old_ver=_old_ver,
                             extra={'deleted': _deleted_n,
                                    'rollback_pool': os.path.basename(pool) if pool else ''})

        # 清理：删除更新包 + 解压目录（失败不阻断，下次启动自动清）
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass
        try:
            if os.path.isdir(extract_dir):
                shutil.rmtree(extract_dir, ignore_errors=True)
        except Exception:
            pass

        # 启动新版本主程序（March7th launch_application 同款：更新完自动拉起，用户无需手动找）
        # 固定名优先（PDD EZ.exe）；旧版升级目录里只有带版本号的 exe 时回退版本扫描
        import subprocess as _sp
        _launch = _pick_main_exe(target_dir)
        if not _launch and target_main and os.path.exists(target_main):
            _launch = target_main
        if _launch:
            try:
                _sp.Popen([_launch], cwd=os.path.dirname(_launch),
                          creationflags=getattr(_sp, 'DETACHED_PROCESS', 0))
                print(f"[更新器] 已启动新版本: {_launch}")
            except Exception as e:
                print(f"[更新器] 启动新版本失败（请手动启动）: {e}")

        _write_progress("done", "更新完成", done=True)
        print(f"[更新器] 已更新: {target_dir}")
        log.info(f"finalize 已更新: {target_dir}")
        return 0

    except Exception as e:
        print(f"[更新器] 更新失败: {e}")
        _write_progress("error", f"更新失败: {e}", error=str(e))
        return 1


def main():
    # 允许在 finalize/auto 分支重新初始化模块级 log（日志目录随 target 变化）
    global log
    print("=" * 40)
    print("  PDD EZ 更新器")
    print("=" * 40)

    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="")
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--resume-update", action="store_true")
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--mode", choices=("auto", "finalize", "rollback"), default="auto")
    ap.add_argument("--file", default="")          # finalize: 已下载的更新包路径
    ap.add_argument("--extract-dir", default="")   # finalize: 解压目录
    ap.add_argument("--wait-pid", type=int, default=0)
    args = ap.parse_args()

    # ── rollback 模式（）：恢复 _rollback_pool/ 内旧文件集，状态机终态落盘 ──
    if args.mode == "rollback":
        me = sys.executable if getattr(sys, 'frozen', False) else __file__
        target = args.target or os.path.join(os.path.dirname(os.path.abspath(me)), EXE_NAME)
        rc = rollback_to_old(target, target_dir=os.path.dirname(target))
        _clear_progress()
        return rc

    # ── finalize 模式：只做覆盖安装（下载已由 GUI 完成）──
    if args.mode == "finalize":
        if not args.file or not os.path.exists(args.file):
            print("[更新器] finalize 模式需要 --file 指向已下载的更新包")
            _write_progress("error", "未找到已下载的更新包", error="missing update file")
            return 1
        me = sys.executable if getattr(sys, 'frozen', False) else __file__
        me_dir = os.path.dirname(os.path.abspath(me))
        target = args.target or os.path.join(me_dir, EXE_NAME)
        target_dir = os.path.dirname(target)
        extract_dir = args.extract_dir or os.path.join(tempfile.gettempdir(), "pdd_update", "extracted")
        # 日志写到目标程序目录（updater 可能从 %TEMP% 自我转移运行，日志必须留在程序目录）
        try:
            _logdir = os.path.join(target_dir, 'logs')
            from logger import Logger
            log = Logger(log_dir=_logdir)
            log.hr("PDD EZ 更新器 finalize 开始", 0)
            log.info(f"target={target}")
            log.info(f"file={args.file}")
        except Exception:
            pass
        rc = do_finalize(args.file, extract_dir, target_dir,
                         wait_pid=args.wait_pid or args.pid,
                         target_main=target)
        try:
            log.hr(f"更新器退出 rc={rc}", 1)
        except Exception:
            pass
        _clear_progress()
        return rc

    # ── auto 模式：完整流程（检查→下载→解压→覆盖）──
    me = sys.executable if getattr(sys, 'frozen', False) else __file__
    me_dir = os.path.dirname(os.path.abspath(me))
    target = os.path.join(me_dir, EXE_NAME)
    # auto 模式日志：写目标程序目录（args.target 优先——自我转移后指向原程序目录，
    # 与 finalize 一致；无 target 时用当前目录）
    try:
        from logger import Logger
        _log_base = os.path.dirname(args.target) if args.target else me_dir
        log = Logger(log_dir=os.path.join(_log_base, 'logs'))
        log.hr("PDD EZ 更新器 auto 开始", 0)
        log.info(f"target={target}")
    except Exception:
        pass

    # ── 自我转移：若更新器运行在"将要被整体替换的目录"里（同目录有主 exe），
    # 必须先从目录里复制到 %TEMP% 再运行——否则 Windows 不允许 rename 包含
    # 运行中 exe 的目录（WinError 32），目录整体替换会失败（v1.4 修复） ──
    import subprocess
    if not args.resume_update:
        try:
            _tmp_self = os.path.join(tempfile.gettempdir(), 'PDD_EZ_Updater_tmp.exe')
            # 判定：当前目录下有"任何" PDD 主 exe（不管版本）→ 即将整体替换此目录
            # 复用 _pick_main_exe（固定名优先 + 版本号扫描），不自己列目录判断——
            # 自写 startswith 判断会把 "PDD EZ Backup.exe" 这类非主程序误判为主程序（v1.4 审查修复）
            _orig_main = _pick_main_exe(me_dir)
            _in_temp = me_dir.lower().startswith(os.path.abspath(tempfile.gettempdir()).lower())
            if _orig_main and not _in_temp:
                shutil.copy2(me, _tmp_self)
                # 显式把目标指向原目录的主 exe（temp 副本的 me_dir 是 %TEMP%，
                # 不带 --target 会找不到主程序；直接双击或 GUI 调用都能正确替换）
                _args = [_tmp_self] + ['--target', _orig_main]
                if args.target:
                    _args = [_tmp_self] + ['--target', args.target]
                if args.restart:
                    _args += ['--restart']
                if args.pid:
                    _args += ['--pid', str(args.pid)]
                _args += ['--resume-update']
                subprocess.Popen(_args, cwd=os.path.dirname(_tmp_self))
                print(f"[更新器] 已转移到临时目录运行: {_tmp_self} → 目标 {args.target or _orig_main}")
                return 0
        except Exception as _e:
            print(f"[更新器] 自我转移失败（继续原目录运行）: {_e}")

    if args.resume_update:
        print("[更新器] 自升级完成，继续更新流程...")

    if args.target:
        target = args.target

    print(f"[更新器] 目标: {target}")

    # 获取最新版本
    tag, assets = get_latest_release()
    if not tag:
        print("[更新器] 无法获取版本信息，请检查网络")
        _write_progress("error", "无法获取版本信息，请检查网络", error="network")
        _pause()
        return 1

    print(f"[更新器] 最新版本: {tag}")

    # v1.4.5（）：auto 模式版本比较——本地旧版名（PDD EZ vX.Y.exe）可提取版本，
    # 远端不更新则拒绝（防重复安装/降级）；固定名 PDD EZ.exe（v1.4+）无版本标记，由 GUI 主链 guard
    try:
        # 内联 version_newer（不能 from utils import——会把 utils 的函数内重依赖
        # pyautogui/PIL/cv2 等拖进 updater 单文件，体积 8MB→69MB）
        def _version_newer(remote, local):
            def _p(v):
                v = str(v).lstrip('vV')
                return [int(x) for x in v.split('.') if x.isdigit()]
            r, l = _p(remote), _p(local)
            n = max(len(r), len(l))
            return (r + [0] * (n - len(r))) > (l + [0] * (n - len(l)))
        _local_main_path = _pick_main_exe(os.path.dirname(target))
        _local_ver = ''
        if _local_main_path:
            _m2 = re.search(r'v(\d+\.\d+(?:\.\d+)?)', os.path.basename(_local_main_path), re.I)
            _local_ver = _m2.group(1) if _m2 else ''
        _remote_ver = re.sub(r'^v', '', tag or '')
        if _local_ver and _remote_ver and not _version_newer('v' + _remote_ver, 'v' + _local_ver):
            print(f"[更新器] 本地版本 v{_local_ver} 不低于远端 {tag}，无需更新")
            _write_progress("done", f"已是最新版本（v{_local_ver}）")
            _pause()
            return 0
    except Exception:
        pass

    # 跨版本策略（与 GUI 同款）：目标目录有固定名 PDD EZ.exe（v1.4+）→ 增量包；
    # 否则（旧版 PDD EZ vX.Y.exe）→ 全量包——旧版 _internal 结构可能不同，增量会崩
    _target_dir_pick = os.path.dirname(target)
    _local_main_fixed = os.path.join(_target_dir_pick, EXE_NAME)
    _use_incremental = os.path.exists(_local_main_fixed)
    print(f"[更新器] 本地为{'固定名(v1.4+)' if _use_incremental else '旧版名'} → 选择{'增量' if _use_incremental else '全量'}包")
    exe_asset = None
    for a in assets:
        name = a.get("name", "")
        if _use_incremental and name.endswith("_update.zip"):
            exe_asset = a; break
        if not _use_incremental and name.endswith(".zip") and "_update" not in name:
            exe_asset = a; break
    if not exe_asset:
        # 兜底：增量找不到就全量，反之亦然
        for a in assets:
            name = a.get("name", "")
            if _use_incremental and name.endswith(".zip") and "_update" not in name:
                exe_asset = a; break
            if not _use_incremental and name.endswith("_update.zip"):
                exe_asset = a; break
    if not exe_asset:
        for a in assets:
            if a["name"].endswith(".exe"):
                exe_asset = a; break

    if not exe_asset:
        print(f"[更新器] Release 中未找到 EXE 附件")
        _write_progress("error", "Release 中未找到更新包", error="no asset")
        _pause()
        return 1

    # 等待主程序退出（通过 PID 确认进程死亡）
    if args.restart:
        if args.pid:
            print(f"[更新器] 等待主程序 PID={args.pid} 退出...")
            if not _wait_pid_exit(args.pid, expected_exe=args.target, timeout=30.0):
                print("[更新器] 警告: 主程序未在 30 秒内退出，继续执行")
            else:
                print("[更新器] 主程序已退出")
        else:
            # 兼容旧版调用（无 --pid），回退到文件轮询
            print("[更新器] 等待主程序退出...")
            for _ in range(15):
                try:
                    with open(target, 'rb') as _f:
                        pass
                    time.sleep(0.5)
                except (PermissionError, OSError):
                    break
        time.sleep(1)  # 额外缓冲，确保文件句柄释放

    # 下载到临时目录
    tmp = os.path.join(tempfile.gettempdir(), "pdd_update")
    os.makedirs(tmp, exist_ok=True)
    # 文件名消毒：仅取 basename，防 GitHub asset 名含 ..\ 路径遍历写文件
    asset_name = os.path.basename(exe_asset["name"].replace('\\', '/'))
    new_exe = os.path.join(tmp, asset_name)

    # v1.4.8 -fix（）：先把 .sha256 拉下来学期望哈希，
    # 再用 download_asset(expected_sha256=...) 走「下载+就地校验+不匹配换源」一条龙。
    # 旧流程：先下载包 → 失败也不换源（哈希失败发生在下游）→ 直接 return 1。
    # 新流程：先下载 .sha256 学期望 → 传期望进 download_asset → 任一源哈希不匹配自动换源。
    expected_sha256 = None
    sha_asset = None
    for a in assets:
        if a["name"] == exe_asset["name"] + ".sha256":
            sha_asset = a; break
    if not sha_asset:
        # v1.4.5（）：fail-open → fail-closed——发布物未带 .sha256 即拒绝安装，
        # 防止截断/篡改包在无校验下直达安装
        print("[更新器] 未找到 .sha256 校验文件，已拒绝安装（安全策略）")
        _write_progress("error", "缺少 SHA256 校验文件，已拒绝安装", error="sha256 missing")
        _pause()
        return 1
    sha_path = new_exe + ".sha256"
    ok, _ = download_asset(sha_asset, sha_path, progress_stage="verify")
    if not ok:
        # 校验文件本身就拉不到 → 拒绝安装（安全策略：宁可让用户手动升级也不盲装）
        print("[更新器] SHA256 校验文件下载失败，已拒绝安装（安全策略）")
        _write_progress("error", "SHA256 校验文件下载失败", error="sha256 file")
        try:
            if os.path.exists(new_exe):
                os.remove(new_exe)
        except OSError:
            pass
        _pause()
        return 1
    try:
        with open(sha_path, 'r') as sf:
            expected_sha256 = sf.read().strip().split()[0]
    except Exception as _re:
        print(f"[更新器] SHA256 校验文件读取失败: {_re}")
        _write_progress("error", "SHA256 校验文件读取失败", error="sha256 read")
        try:
            os.remove(sha_path)
        except OSError:
            pass
        _pause()
        return 1
    # 校验文件格式合法性：必须是 64 位 hex，服务器给脏数据
    # （如 "abc"）时明确报格式错误，而不是误导"文件可能被篡改"（v1.4 审查修复）
    import re as _re_sha
    if not _re_sha.fullmatch(r'[0-9a-fA-F]{64}', expected_sha256):
        print(f"[更新器] SHA256 校验文件格式非法，已拒绝安装")
        _write_progress("error", "SHA256 校验文件格式非法", error="sha256 format")
        try:
            os.remove(sha_path)
        except OSError:
            pass
        _pause()
        return 1
    # 期望哈希有效 → 传给 download_asset 走「下载+就地哈希校验+不匹配换源」
    ok, err = download_asset(exe_asset, new_exe, expected_sha256=expected_sha256)
    if not ok:
        print(f"[更新器] 下载失败（所有源均不匹配或不可达）: {err}")
        _write_progress("error", f"下载失败: {err}", error=err)
        try:
            if os.path.exists(sha_path):
                os.remove(sha_path)
        except OSError:
            pass
        _pause()
        return 1
    # 走到这里 = 任一源下载成功 + 哈希已就地校验通过
    print("[更新器] SHA256 校验通过")
    try:
        os.remove(sha_path)
    except OSError:
        pass

    # 替换
    try:
        target_dir = os.path.dirname(target)
        if new_exe.endswith(".zip"):
            print("[更新器] 解压更新包...")
            extract_dir = os.path.join(tmp, "extracted")
            extracted = _extract_zip(new_exe, extract_dir)
            new_dir = None
            for item in os.listdir(extract_dir):
                item_path = os.path.join(extract_dir, item)
                if os.path.isdir(item_path) and item.startswith("PDD EZ"):
                    new_dir = item_path; break
            if new_dir:
                print("[更新器] 覆盖程序文件夹...")
                # ⚠️ 安全底线：target_dir 必须是"PDD EZ 程序目录"才允许整体替换
                if not _is_program_dir(target_dir):
                    print(f"[更新器] [拒绝] 目标目录不是 PDD EZ 程序目录（{target_dir}）")
                    print("[更新器] 请把更新器放到 PDD EZ 主程序所在的文件夹后再运行")
                    _write_progress("error", "目标目录不是 PDD EZ 程序目录", error="bad target")
                    _pause()
                    return 1

                # 收集待覆盖文件
                files = []
                for root, _, fnames in os.walk(new_dir):
                    for fname in fnames:
                        src = os.path.join(root, fname)
                        rel = os.path.relpath(src, new_dir)
                        dest = os.path.join(target_dir, rel)
                        if _needs_overwrite(src, dest):
                            files.append((src, dest))

                # 预检测占用（自身文件除外）
                locked = _check_target_files_locked(files)
                if locked:
                    _names = "、".join(os.path.basename(p) for p in locked[:10])
                    print(f"[更新器] [阻塞] 文件被占用：{_names}")
                    _write_progress("error", f"文件被占用：{_names}", error="locked")
                    _pause()
                    return 1

                # 覆盖（March7th 顺序：先其他 → 自身改名 → 自身，失败自身恢复）
                ok, skipped = _cover_with_self_handling(files)
                if skipped:
                    _names = "、".join(os.path.basename(p) for p in skipped[:10])
                    print(f"[更新器] {len(skipped)} 个文件覆盖失败：{_names}")
                    _write_progress("error", f"覆盖失败：{_names}", error="cover")
                    _pause()
                    return 1
                # v1.4.5 （置于覆盖成功之后）：auto 模式同样应用删除清单
                _apply_deleted_files(new_dir, target_dir)
                print(f"[更新器] 已更新: {target_dir}")
                log.info(f"auto 已更新: {target_dir}")

                # 清理
                try:
                    if os.path.exists(new_exe):
                        os.remove(new_exe)
                except Exception:
                    pass
                try:
                    if os.path.isdir(extract_dir):
                        shutil.rmtree(extract_dir, ignore_errors=True)
                except Exception:
                    pass
            elif new_dir is None:
                # 解压结果里没有 PDD EZ 目录 → 单 exe 替换
                single_exe = None
                for item in os.listdir(extract_dir):
                    item_path = os.path.join(extract_dir, item)
                    if item.endswith(".exe") and "PDD" in item and "updater" not in item.lower():
                        single_exe = item_path; break
                if single_exe:
                    _do_replace(single_exe, target)
                else:
                    print("[更新器] 未找到有效更新内容")
                    _write_progress("error", "未找到有效更新内容", error="no content")
                    _pause()
                    return 1
        else:
            _do_replace(new_exe, target)
    except Exception as e:
        print(f"[更新器] 替换失败: {e}")
        _write_progress("error", f"替换失败: {e}", error=str(e))
        _pause()
        return 1

    _write_progress("done", "更新完成", done=True)
    _clear_progress()

    # 更新器自升级：zip 中有新 updater.exe → bat 脚本替换
    # 注意：my_path 必须指向【目标程序目录】的 updater——auto 模式自我转移后
    # sys.executable 是 %TEMP% 副本，用它会导致替换 temp 副本、程序目录 updater 永远旧（v1.4 修复）
    if 'new_dir' in dir() and new_dir:
        for f in os.listdir(new_dir):
            fp = os.path.join(new_dir, f)
            if f.lower().startswith("pdd ez updater") and f.endswith(".exe"):
                _target_updater = os.path.join(target_dir, 'PDD EZ Updater.exe')
                my_path = _target_updater if os.path.exists(_target_updater) else \
                    (sys.executable if getattr(sys, 'frozen', False) else os.path.join(os.path.dirname(__file__), 'PDD EZ Updater.exe'))
                new_updater = os.path.join(os.path.dirname(my_path), "updater.exe.new")
                shutil.copy2(fp, new_updater)
                # 写 bat 脚本等待当前进程退出后替换
                _fd, bat = tempfile.mkstemp(prefix="pdd_upd_", suffix=".bat", dir=tempfile.gettempdir())
                os.close(_fd)
                # CMD 元字符转义：除引号/百分号外，& | ^ < > 都会被 cmd 拆解——
                # 路径含这些字符（如 C:\Users\Test&A\PDD）时 move 命令会错（v1.4 审查修复）
                _nu_esc = (new_updater.replace('^', '^^').replace('&', '^&')
                           .replace('|', '^|').replace('<', '^<').replace('>', '^>')
                           .replace('"', '""').replace('%', '%%'))
                _mp_esc = (my_path.replace('^', '^^').replace('&', '^&')
                           .replace('|', '^|').replace('<', '^<').replace('>', '^>')
                           .replace('"', '""').replace('%', '%%'))
                with open(bat, 'w') as bf:
                    bf.write(f'''@echo off
set cnt=0
:loop
timeout /t 1 /nobreak >nul
set /a cnt+=1
if %cnt% geq 30 goto :done
if exist "{_nu_esc}" (
    move /y "{_nu_esc}" "{_mp_esc}"
    if not exist "{_nu_esc}" (
        start "" "{_mp_esc}" --resume-update
        goto :done
    )
)
goto :loop
:done
del "%~f0"
''')
                os.startfile(bat)
                break
    return 0


def _do_replace(src, target):
    # v1.4.5（）：旧实现先 rename→remove(old) 再 copy2——copy2 失败（磁盘满/
    # 权限）时主 exe 已删且 .old 已删 → 程序目录主程序缺失。改为：复制成功后才删 .old，
    # 失败回滚恢复原文件。
    if sys.platform == 'win32' and os.path.exists(target):
        old = target + ".old"
        if os.path.exists(old):
            try:
                os.remove(old)
            except (PermissionError, OSError):  # v1.4.6 ：磁盘满等 OSError 也走延迟删除
                import ctypes
                ok = ctypes.windll.kernel32.MoveFileExW(old, None, 4)
                if not ok:
                    print(f"[更新器] 警告: 无法删除旧文件 {old}，请手动清理（或重启后自动清理）")
        os.rename(target, old)
    try:
        shutil.copy2(src, target)
        print(f"[更新器] 已更新: {target}")
    except (PermissionError, OSError):  # v1.4.6 ：磁盘满等 OSError 同样触发回滚，防主 exe 缺失
        # 覆盖失败：回滚旧文件（target 已被改名 .old），并保留 .new 供手动处理
        try:
            os.replace(target + ".old", target)
            print(f"[更新器] 覆盖失败，已恢复原文件 {target}")
        except Exception:
            print(f"[更新器] 覆盖失败且未能恢复，原文件保留在 {target}.old")
        fallback = target + ".new"
        try:
            shutil.copy2(src, fallback)
            print(f"[更新器] 文件被占用，已保存为 {fallback}，请手动替换或重启后重试")
        except Exception:
            pass
        return
    # 复制成功后才清理 .old
    if sys.platform == 'win32' and os.path.exists(target + ".old"):
        try:
            os.remove(target + ".old")
        except (PermissionError, OSError):
            import ctypes
            ok = ctypes.windll.kernel32.MoveFileExW(target + ".old", None, 4)
            if not ok:
                print(f"[更新器] 警告: 旧文件 {target}.old 待重启后自动清理")


def rollback_to_old(updater_cli_path: str, target_dir: str = "") -> int:
    """ 自动回滚入口（GUI 调用 / CLI --rollback 共用）：把 _rollback_pool/
    内的最后一份旧文件集恢复回程序目录。

    语义（宪法 §4/§5）：
    - 仅当 update_health.json state=='pending' 时动作；其余状态一律非 0 拒绝
      （幂等——防自我锁死：回滚后终态落盘，再调不重复动作）；
    - **仅尝试一次**：任何一步失败立即落 'rollback_failed' 终态并返回非 0，
      绝不重试、绝不循环；再失败由调用方显式报错引导人工恢复；
    - 恢复目标必须 realpath 落在程序目录内（拒绝 '..'/绝对路径/越界）；
      只逐文件 os.replace 回原位，绝不 rmtree 用户程序目录；
    - 返回 0=成功（state 落 'rolled_back'，池清空）。

    Args:
        updater_cli_path: 程序目录内 updater 自身路径（exe 或 py），用于定位程序目录；
        target_dir: 显式指定程序目录（测试/特殊布局用），空则取 updater_cli_path 同目录。
    """
    try:
        base = target_dir if target_dir else os.path.dirname(
            os.path.abspath(updater_cli_path or ''))
        if not base or not os.path.isdir(base):
            print("[更新器] [回滚] 程序目录无效，拒绝回滚")
            return 2
        # CROSS#4（v1.6.0 发布前修复批次）：源码形态（未打包，getattr(sys,'frozen',None) is None）
        # 下的回滚 base 锚到工作区路径，可能覆盖未提交改动。加一行警告日志（不
        # 改行为 —— 真实开发态/打包态下的行为不变，但日志能让操作者确认回滚目标）。
        try:
            if not getattr(sys, 'frozen', False):
                # 粗略探针：若 base 下含 vcs 标记（.git/.hg/.svn）→ 更可疑
                _vcs_marks = tuple(os.path.join(base, m) for m in ('.git', '.hg', '.svn'))
                _has_vcs = any(os.path.exists(p) for p in _vcs_marks)
                _vcs_tag = '（含 VCS 标记）' if _has_vcs else ''
                print(
                    f"[更新器] [回滚] 警告：当前为源码形态（未打包）{_vcs_tag}，"
                    f"回滚目标 = {base}。请确认目标目录正确，回滚将就地覆盖。"
                )
        except Exception:
            pass
        st = _read_update_health(base)
        if str(st.get('state') or '') != 'pending':
            print(f"[更新器] [回滚] 无待验证更新（state={st.get('state')!r}），不动作")
            return 3
        pool = _rollback_pool_dir(base)
        manifest_path = os.path.join(pool, 'manifest.json')
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                entries = json.load(f)
        except Exception:
            entries = []
        if not isinstance(entries, list) or not entries:
            print("[更新器] [回滚] 回滚清单缺失或为空，无法自动回滚")
            _write_update_health(base, 'rollback_failed',
                                 new_ver=str(st.get('new_ver') or ''),
                                 old_ver=str(st.get('old_ver') or ''),
                                 extra={'reason': 'manifest_missing'})
            return 4
        base_real = os.path.realpath(base) + os.sep
        restored = 0
        for ent in reversed(entries):  # 逆序：后覆盖的先恢复
            if not isinstance(ent, dict):
                continue
            rel = str(ent.get('dest_rel') or '')
            bak_name = str(ent.get('backup') or '')
            if not rel or not bak_name:
                continue
            dest = os.path.normpath(os.path.join(base, rel))
            src_bak = os.path.join(pool, bak_name)
            try:
                if not (os.path.realpath(dest) + os.sep).startswith(base_real):
                    print(f"[更新器] [回滚] 拒绝越界恢复: {rel}")
                    _write_update_health(base, 'rollback_failed',
                                         new_ver=str(st.get('new_ver') or ''),
                                         old_ver=str(st.get('old_ver') or ''),
                                         extra={'reason': 'escape_guard'})
                    return 5
                if not os.path.isfile(src_bak):
                    print(f"[更新器] [回滚] 池内备份缺失: {bak_name}")
                    _write_update_health(base, 'rollback_failed',
                                         new_ver=str(st.get('new_ver') or ''),
                                         old_ver=str(st.get('old_ver') or ''),
                                         extra={'reason': 'backup_missing'})
                    return 6
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.replace(src_bak, dest)
                restored += 1
            except Exception as e:
                print(f"[更新器] [回滚] 恢复失败 {rel}: {e}")
                _write_update_health(base, 'rollback_failed',
                                     new_ver=str(st.get('new_ver') or ''),
                                     old_ver=str(st.get('old_ver') or ''),
                                     extra={'reason': f'restore_failed:{str(e)[:40]}'})
                return 7  # 仅尝试一次：立即终态退出（§4 防自我锁死）
        _write_update_health(base, 'rolled_back',
                             new_ver=str(st.get('new_ver') or ''),
                             old_ver=str(st.get('old_ver') or ''),
                             extra={'restored': restored})
        _prune_rollback_pool(pool, remove_all=True)
        print(f"[更新器] [回滚] 已恢复 {restored} 个文件到更新前版本")
        try:
            log.info(f"[回滚] 已恢复 {restored} 个文件到更新前版本: {base}")
        except Exception:
            pass
        return 0
    except Exception as e:
        print(f"[更新器] [回滚] 回滚执行异常: {e}")
        return 8


if __name__ == "__main__":
    sys.exit(main())
