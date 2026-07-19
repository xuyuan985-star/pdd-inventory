"""
PDD EZ — 更新器
独立小程序：从 GitHub Releases 拉取最新 EXE，替换后重启主程序。
"""
import os, sys, json, shutil, time, tempfile
from urllib.request import urlopen, Request

REPO = "xuyuan985-star/pdd-inventory"
from utils import EXE_NAME

def _appdata():
    return os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'PDD补货助手')

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
    """下载 release 附件"""
    url = asset["browser_download_url"]
    name = asset["name"]
    print(f"[更新器] 下载 {name} ({asset['size']} bytes)...")
    try:
        req = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "PDD-EZ-Updater"})
        with urlopen(req, timeout=120) as resp:
            with open(dest, 'wb') as f:
                shutil.copyfileobj(resp, f)
        return True
    except Exception as e:
        print(f"[更新器] 下载失败: {e}")
        return False

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
    args = ap.parse_args()
    
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
    
    # 找 EXE 附件
    exe_asset = None
    for a in assets:
        if a["name"].endswith(".exe") or a["name"].endswith(".zip"):
            exe_asset = a
            break
    
    if not exe_asset:
        print(f"[更新器] Release 中未找到 EXE 附件")
        input("按回车退出...")
        return
    
    # 等待主程序退出
    if args.restart:
        print("[更新器] 等待主程序退出...")
        time.sleep(3)
    
    # 下载到临时目录
    tmp = os.path.join(tempfile.gettempdir(), "pdd_update")
    os.makedirs(tmp, exist_ok=True)
    new_exe = os.path.join(tmp, exe_asset["name"])
    
    if not download_asset(exe_asset, new_exe):
        print("[更新器] 下载失败")
        input("按回车退出...")
        return
    
    # 替换
    try:
        target_dir = os.path.dirname(target)
        if new_exe.endswith(".zip"):
            import zipfile
            print("[更新器] 解压更新包...")
            extract_dir = os.path.join(tmp, "extracted")
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(new_exe, 'r') as zf:
                zf.extractall(extract_dir)
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
                for root, dirs, files in os.walk(new_dir):
                    rel = os.path.relpath(root, new_dir)
                    dest_root = target_dir if rel == '.' else os.path.join(target_dir, rel)
                    os.makedirs(dest_root, exist_ok=True)
                    for f in files:
                        try:
                            shutil.copy2(os.path.join(root, f), os.path.join(dest_root, f))
                        except PermissionError:
                            pass
                print(f"[更新器] 已更新: {target_dir}")
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


def _do_replace(src, target):
    if sys.platform == 'win32' and os.path.exists(target):
        old = target + ".old"
        if os.path.exists(old):
            os.remove(old)
        os.rename(target, old)
        import ctypes
        ctypes.windll.kernel32.MoveFileExW(old, None, 4)
    try:
        shutil.copy2(src, target)
        print(f"[更新器] 已更新: {target}")
    except PermissionError:
        tmp = target + ".new"
        shutil.copy2(src, tmp)
        print(f"[更新器] 文件被占用，已保存为 {tmp}，请手动替换或重启后重试")
    
    # 启动主程序
    if args.restart and os.path.exists(target):
        print("[更新器] 启动主程序...")
        os.startfile(target)
    
    # 清理
    try:
        shutil.rmtree(tmp)
    except:
        pass
    
    print("[更新器] 完成")

if __name__ == "__main__":
    main()
