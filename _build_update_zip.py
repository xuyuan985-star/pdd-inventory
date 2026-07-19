"""PDD EZ — 轻量更新包生成器。仅打包相对上次变更的文件。"""
import os, sys, zipfile, json

SNAPSHOT_FILE = "_prev_build.json"

def load_snapshot(snapshot_path):
    if os.path.exists(snapshot_path):
        with open(snapshot_path) as f:
            return json.load(f)
    return {}

def save_snapshot(snapshot_path, data):
    with open(snapshot_path, 'w') as f:
        json.dump(data, f, indent=2)

def build_update_zip(onedir_path, output_path, force=False):
    onedir = os.path.abspath(onedir_path)
    name = os.path.basename(onedir)
    dist_parent = os.path.dirname(onedir)
    snapshot_path = os.path.join(dist_parent, SNAPSHOT_FILE)
    prev = load_snapshot(snapshot_path)
    curr = {}
    changed = 0

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 主 EXE（每次必带）
        exe = os.path.join(onedir, f"{name}.exe")
        if os.path.exists(exe):
            arcname = os.path.join(name, os.path.basename(exe))
            zf.write(exe, arcname)
            curr["/"] = os.path.getmtime(exe)
            changed += 1
        # 更新器 EXE（始终包含，避免增量更新跳过更新器自身升级）
        updater_exe = os.path.join(dist_parent, "PDD EZ Updater.exe")
        if os.path.exists(updater_exe):
            zf.write(updater_exe, os.path.basename(updater_exe))
            changed += 1

        internal = os.path.join(onedir, "_internal")
        if os.path.isdir(internal):
            for root, dirs, files in os.walk(internal):
                for f in files:
                    src = os.path.join(root, f)
                    rel = os.path.relpath(src, internal)
                    mtime = os.path.getmtime(src)
                    curr[rel] = mtime
                    # 跳过超大库文件
                    if os.path.getsize(src) > 5 * 1024 * 1024:
                        continue
                    # 比较上次时间戳
                    if not force and rel in prev and abs(mtime - prev[rel]) < 1:
                        continue
                    arcname = os.path.join(name, "_internal", rel)
                    zf.write(src, arcname)
                    changed += 1

    save_snapshot(snapshot_path, curr)
    size = os.path.getsize(output_path)
    print(f"更新包: {size/1024:.0f} KB ({changed} files changed)")
    if not force and changed <= 2:
        print("提示: 改动极少，仅打包了必要文件")

if __name__ == "__main__":
    dist = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")
    onedir = os.path.join(dist, "PDD EZ v1.0")
    output = os.path.join(dist, "PDD_EZ_v1.0_update.zip")
    build_update_zip(onedir, output, force="--force" in sys.argv)
