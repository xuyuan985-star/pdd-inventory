"""
PDD EZ — 更新器
独立小程序：从 GitHub Releases 拉取最新 EXE，替换后重启主程序。
"""
import os, sys, json, shutil, time, tempfile
from urllib.request import urlopen, Request

REPO = "xuyuan985-star/pdd-inventory"
from utils import EXE_NAME

def get_latest_release():
    """从 GitHub API 获取最新 release 信息"""
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    try:
        req = Request(url, headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "PDD-EZ-Updater"})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data.get("tag_name", ""), data.get("assets", [])
    except Exception as e:
        print(f"[更新器] 检查失败: {e}")
        return None, []

def download_asset(asset, dest):
    """下载 release 附件，返回 (success, sha256_hex 或 None)"""
    url = asset["browser_download_url"]
    name = asset["name"]
    print(f"[更新器] 下载 {name} ({asset['size']} bytes)...")
    try:
        req = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "PDD-EZ-Updater"})
        with urlopen(req, timeout=120) as resp:
            with open(dest, 'wb') as f:
                shutil.copyfileobj(resp, f)
        return True, None
    except Exception as e:
        print(f"[更新器] 下载失败: {e}")
        return False, None

def _wait_pid_exit(pid: int, expected_exe: str = '', timeout: float = 30.0):
    """通过 Windows API 等待指定 PID 的进程退出。超时返回 False。
    若 expected_exe 给出，先校验进程路径是否匹配，防止 PID 回收误判。"""
    import ctypes
    from ctypes import wintypes
    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32

    h = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return True  # 进程已不存在

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
                        return True  # PID 已被回收，原进程已死

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


def main():
    print("=" * 40)
    print("  PDD EZ 更新器")
    print("=" * 40)
    
    me = sys.executable if getattr(sys, 'frozen', False) else __file__
    me_dir = os.path.dirname(os.path.abspath(me))
    target = os.path.join(me_dir, EXE_NAME)
    
    # 检查是否被主程序调用（带 --target 参数）
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="")
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--resume-update", action="store_true")
    ap.add_argument("--pid", type=int, default=0)
    args = ap.parse_args()
    
    if args.resume_update:
        print("[更新器] 自升级完成，继续更新流程...")
    
    if args.target:
        target = args.target
    
    print(f"[更新器] 目标: {target}")
    
    # 获取最新版本
    tag, assets = get_latest_release()
    if not tag:
        print("[更新器] 无法获取版本信息，请检查网络")
        input("按回车退出...")
        return
    
    print(f"[更新器] 最新版本: {tag}")
    
    # 优先增量更新包 → 全量 zip → 单个 EXE
    exe_asset = None
    for a in assets:
        if a["name"].endswith("_update.zip"):
            exe_asset = a; break
    if not exe_asset:
        for a in assets:
            if a["name"].endswith(".zip") and "_update" not in a["name"]:
                exe_asset = a; break
    if not exe_asset:
        for a in assets:
            if a["name"].endswith(".exe"):
                exe_asset = a; break
    
    if not exe_asset:
        print(f"[更新器] Release 中未找到 EXE 附件")
        input("按回车退出...")
        return
    
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
    new_exe = os.path.join(tmp, exe_asset["name"])
    
    if not download_asset(exe_asset, new_exe)[0]:
        print("[更新器] 下载失败")
        input("按回车退出...")
        return

    # SHA256 校验：查找同名 .sha256 文件并验证
    sha_asset = None
    for a in assets:
        if a["name"] == exe_asset["name"] + ".sha256":
            sha_asset = a; break
    if sha_asset:
        sha_path = new_exe + ".sha256"
        ok, _ = download_asset(sha_asset, sha_path)
        if ok:
            with open(sha_path, 'r') as sf:
                expected = sf.read().strip().split()[0]
            if not _verify_sha256(new_exe, expected):
                print("[更新器] SHA256 校验失败！文件可能被篡改，已拒绝安装")
                os.remove(new_exe)
                input("按回车退出...")
                return
            print("[更新器] SHA256 校验通过")
            os.remove(sha_path)
        else:
            print("[更新器] SHA256 校验文件下载失败，已拒绝安装（安全策略）")
            os.remove(new_exe)
            input("按回车退出...")
            return
    else:
        print("[更新器] 未找到 .sha256 校验文件，跳过签名验证")
    
    # 替换
    try:
        target_dir = os.path.dirname(target)
        if new_exe.endswith(".zip"):
            import zipfile
            print("[更新器] 解压更新包...")
            extract_dir = os.path.join(tmp, "extracted")
            os.makedirs(extract_dir, exist_ok=True)
            extract_dir_real = os.path.realpath(extract_dir) + os.sep
            with zipfile.ZipFile(new_exe, 'r') as zf:
                for zi in zf.infolist():
                    # 路径遍历防护：规范化后校验必须在 extract_dir 内
                    # 拒绝绝对路径、.. 穿越、Windows 盘符等
                    member_path = os.path.realpath(os.path.join(extract_dir, zi.filename))
                    if not member_path.startswith(extract_dir_real):
                        print(f"[更新器] 拒绝路径遍历: {zi.filename}")
                        continue
                    zf.extract(zi, extract_dir)
            new_dir = None
            single_exe = None
            for item in os.listdir(extract_dir):
                item_path = os.path.join(extract_dir, item)
                if os.path.isdir(item_path) and item.startswith("PDD EZ"):
                    new_dir = item_path; break
                elif item.endswith(".exe") and "PDD" in item:
                    single_exe = item_path
            if new_dir:
                print("[更新器] 覆盖程序文件夹...")
                # 备份旧版本
                backup_dir = target_dir + "_backup"
                if os.path.exists(backup_dir):
                    shutil.rmtree(backup_dir, ignore_errors=True)
                try:
                    if os.path.exists(target_dir):
                        os.rename(target_dir, backup_dir)
                    # 复制新文件
                    skipped = 0
                    for root, dirs, files in os.walk(new_dir):
                        rel = os.path.relpath(root, new_dir)
                        dest_root = target_dir if rel == '.' else os.path.join(target_dir, rel)
                        os.makedirs(dest_root, exist_ok=True)
                        for f in files:
                            try:
                                shutil.copy2(os.path.join(root, f), os.path.join(dest_root, f))
                            except PermissionError:
                                skipped += 1
                    if skipped:
                        print(f"[更新器] {skipped} 个文件被占用跳过，重启后生效")
                    print(f"[更新器] 已更新: {target_dir}")
                    shutil.rmtree(backup_dir, ignore_errors=True)
                except Exception:
                    # 回滚：逐文件从备份恢复
                    print("[更新器] 更新失败，正在回滚...")
                    if os.path.exists(target_dir):
                        shutil.rmtree(target_dir, ignore_errors=True)
                    if os.path.exists(backup_dir):
                        for root, dirs, files in os.walk(backup_dir):
                            rel = os.path.relpath(root, backup_dir)
                            dest = target_dir if rel == '.' else os.path.join(target_dir, rel)
                            os.makedirs(dest, exist_ok=True)
                            for f in files:
                                try:
                                    shutil.copy2(os.path.join(root, f), os.path.join(dest, f))
                                except OSError:
                                    pass
                        shutil.rmtree(backup_dir, ignore_errors=True)
                    print("[更新器] 已回滚至旧版本")
                    input("按回车退出...")
                    return
            elif single_exe:
                _do_replace(single_exe, target)
            else:
                print("[更新器] 未找到有效更新内容")
        else:
            _do_replace(new_exe, target)
    except Exception as e:
        print(f"[更新器] 替换失败: {e}")
        input("按回车退出...")
        return
    
    # 更新器自升级：zip 中有新 updater.exe → bat 脚本替换
    if new_dir:
        for f in os.listdir(new_dir):
            fp = os.path.join(new_dir, f)
            if f.lower().startswith("pdd ez updater") and f.endswith(".exe"):
                my_path = sys.executable if getattr(sys, 'frozen', False) else os.path.join(os.path.dirname(__file__), 'PDD EZ Updater.exe')
                new_updater = os.path.join(os.path.dirname(my_path), "updater.exe.new")
                shutil.copy2(fp, new_updater)
                # 写 bat 脚本等待当前进程退出后替换
                bat = os.path.join(tempfile.gettempdir(), "update_updater.bat")
                with open(bat, 'w') as bf:
                    bf.write(f'''@echo off
set cnt=0
:loop
timeout /t 1 /nobreak >nul
set /a cnt+=1
if %cnt% geq 30 goto :done
if exist "{new_updater}" (
    move /y "{new_updater}" "{my_path}"
    start "" "{my_path}" --resume-update
)
goto :loop
:done
del "%~f0"
''')
                os.startfile(bat)
                break


def _do_replace(src, target):
    if sys.platform == 'win32' and os.path.exists(target):
        old = target + ".old"
        if os.path.exists(old):
            try:
                os.remove(old)
            except PermissionError:
                import ctypes
                ctypes.windll.kernel32.MoveFileExW(old, None, 4)
        os.rename(target, old)
        try:
            os.remove(old)  # 主程序已退出，句柄应释放
        except PermissionError:
            import ctypes
            ctypes.windll.kernel32.MoveFileExW(old, None, 4)
    try:
        shutil.copy2(src, target)
        print(f"[更新器] 已更新: {target}")
    except PermissionError:
        fallback = target + ".new"
        shutil.copy2(src, fallback)
        print(f"[更新器] 文件被占用，已保存为 {fallback}，请手动替换或重启后重试")

if __name__ == "__main__":
    main()
