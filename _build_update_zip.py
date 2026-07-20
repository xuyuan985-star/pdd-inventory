"""
PDD EZ — 增量更新包生成器（Git diff 版）
用 Git 历史判断源码变更范围，只把真正变了的文件打进 _update.zip。
不再依赖本地快照文件或文件时间戳——GitHub Releases 场景下正确运作。
"""
import os, sys, zipfile, subprocess, re

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# ── pip 包名 → _internal 目录映射 ──
# 只有带原生扩展的包才会在 _internal 下产生独立目录；
# 纯 Python 包（pyautogui/openpyxl/requests 等）编译进 PYZ，由壳 EXE 承载。
PIP_TO_INTERNAL = {
    'opencv-python':        ['cv2'],
    'numpy':                ['numpy', 'numpy.libs'],          # + numpy-*.dist-info 通配
    'Pillow':               ['PIL'],
    'pywin32':              ['win32', 'pywin32_system32'],
    'lxml':                 ['lxml'],
    'cryptography':         ['cryptography'],                 # + cryptography-*.dist-info
    'certifi':              ['certifi'],
    'PyYAML':               ['yaml'],
    'charset-normalizer':   ['charset_normalizer'],
}

SKIP_DIRS = {'__pycache__', 'tests', 'test'}
SKIP_EXTENSIONS = {'.pyc', '.pyo', '.log', '.tmp'}


def _run(cmd: list) -> str:
    """运行命令，返回 stripped stdout；失败返回空字符串"""
    try:
        r = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True,
                          timeout=10, encoding='utf-8', errors='replace')
        return r.stdout.strip() if r.returncode == 0 else ''
    except Exception:
        return ''


def get_last_release_tag() -> str:
    """获取最近一次 Release 对应的 tag（按版本号排序）"""
    tags = _run(['git', 'tag', '--sort=-version:refname'])
    if not tags:
        return ''
    return tags.split('\n')[0]


def get_changed_files_since(tag: str) -> set:
    """返回从 tag 到当前工作区的变更文件列表（含暂存和未暂存）"""
    if not tag:
        return set()
    # git diff tag 比较到工作区（含 staged + unstaged）
    out = _run(['git', 'diff', '--name-only', tag])
    if not out:
        return set()
    return set(out.split('\n'))


def get_changed_packages(changed_files: set) -> set:
    """
    从变更文件列表中反推哪些 pip 包可能变了。
    规则：requirements.txt 变更 → 解析 diff 找出被改动的包名。
    """
    if 'requirements.txt' not in changed_files:
        return set()

    # 获取 requirements.txt 从上次 tag 到现在的 diff
    tag = get_last_release_tag()
    if not tag:
        return set(PIP_TO_INTERNAL.keys())  # 首次发布，全量

    diff = _run(['git', 'diff', tag, '--', 'requirements.txt'])
    if not diff:
        return set()

    # 解析 diff 中被修改的行（+/- 开头的依赖声明）
    changed = set()
    for line in diff.split('\n'):
        line = line.strip()
        if not line.startswith(('+', '-')):
            continue
        line = line.lstrip('+-').strip()
        # 提取包名: "opencv-python>=4.8.0" → "opencv-python"
        m = re.match(r'^([a-zA-Z0-9_-]+)', line)
        if m:
            pkg = m.group(1).lower()
            if pkg in PIP_TO_INTERNAL:
                changed.add(pkg)
    return changed


def _match_dist_info(name: str, pkg_dirs: set) -> bool:
    """判断 dist-info 目录是否属于变更的包"""
    for pkg in pkg_dirs:
        if name.startswith(pkg + '-') and '.dist-info' in name:
            return True
    return False


def build_update_zip(onedir_path: str, output_path: str, force: bool = False):
    onedir = os.path.abspath(onedir_path)
    name = os.path.basename(onedir)
    dist_parent = os.path.dirname(onedir)
    internal = os.path.join(onedir, '_internal')

    # ── 1. 确定变更范围 ──
    tag = get_last_release_tag()
    changed_files = get_changed_files_since(tag)

    if force or not tag:
        print(f"[增量打包] 模式: {'强制全量' if force else '首次发布（无 tag）'}")
        include_all = True
        changed_packages = set(PIP_TO_INTERNAL.keys())
    else:
        changed_packages = get_changed_packages(changed_files)
        include_all = False
        print(f"[增量打包] 基准 tag: {tag}")
        print(f"[增量打包] 变更文件: {len(changed_files)} 个")
        if changed_packages:
            print(f"[增量打包] 依赖变更: {changed_packages}")

    # 判断是否需要包含更新器 EXE
    updater_changed = include_all or any(
        f in changed_files for f in ['updater.py', 'updater.spec']
    )
    # 模板/资源文件是否变更
    templates_changed = include_all or any(
        f.startswith('templates/') for f in changed_files
    )
    resources_changed = include_all or any(
        f in changed_files for f in ('icon.ico', 'settings.json', 'regions.json')
    )

    # ── 2. 确定需要打包的 _internal 目录 ──
    include_dirs = set()

    if include_all:
        # 全量：打包所有非运行时文件
        include_dirs = set().union(*PIP_TO_INTERNAL.values())
    else:
        for pkg in changed_packages:
            dirs = PIP_TO_INTERNAL.get(pkg, [])
            include_dirs.update(dirs)

    # 业务资源目录
    if templates_changed:
        include_dirs.add('templates')

    # ── 3. 打包 zip ──
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        added = 0

        # 主 EXE / 壳 EXE — 始终包含；PyInstaller 每次构建都会重新生成，
        # 内含编译后的 PYZ，是代码变更的核心载体。
        exe = os.path.join(onedir, f'{name}.exe')
        if os.path.exists(exe):
            arcname = os.path.join(name, os.path.basename(exe))
            zf.write(exe, arcname)
            added += 1
            print(f"  + {os.path.basename(exe)}")

        # 更新器 EXE（仅源码变更时）
        updater_exe = os.path.join(dist_parent, 'PDD EZ Updater.exe')
        if os.path.exists(updater_exe) and updater_changed:
            zf.write(updater_exe, os.path.basename(updater_exe))
            added += 1
            print(f"  + {os.path.basename(updater_exe)}")

        # 资源文件（仅当变更时）
        if resources_changed:
            for res in ['icon.ico', 'settings.json', 'regions.json']:
                for src in [os.path.join(onedir, res), os.path.join(internal, res)]:
                    if os.path.exists(src):
                        zf.write(src, os.path.join(name, res))
                        added += 1
                        print(f"  + {res}")

        # _internal 目录内容 — 仅在全量模式或依赖变更时才遍历
        need_internal = include_all or bool(include_dirs)
        if need_internal and os.path.isdir(internal):
            for root, dirs, files in os.walk(internal):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

                rel_dir = os.path.relpath(root, internal)
                top_dir = rel_dir.split(os.sep)[0] if rel_dir != '.' else ''

                # 增量模式：判断当前目录是否属于变更范围
                if not include_all:
                    if top_dir:
                        in_scope = top_dir in include_dirs or _match_dist_info(top_dir, include_dirs)
                        if not in_scope:
                            dirs[:] = []
                            continue
                    else:
                        # _internal 根目录：仅保留变更范围内的子目录，跳过根文件
                        dirs[:] = [d for d in dirs
                                   if d in include_dirs or _match_dist_info(d, include_dirs)]
                        continue

                for f in files:
                    # 跳过临时/编译/元数据文件
                    if any(f.endswith(ext) for ext in SKIP_EXTENSIONS):
                        continue
                    src = os.path.join(root, f)
                    rel = os.path.relpath(src, internal)

                    # 跳过 pip 的 .dist-info 元数据目录
                    if '.dist-info' in rel:
                        continue

                    arcname = os.path.join(name, '_internal', rel)
                    zf.write(src, arcname)
                    added += 1

    size = os.path.getsize(output_path)
    print(f"\n[增量打包] 完成: {size/1024:.0f} KB ({added} 个文件)")
    if added <= 3:
        print("[增量打包] 极小更新 — 仅必要文件")


if __name__ == '__main__':
    dist = os.path.join(REPO_ROOT, 'dist')
    # 自动发现 dist 下最新的 PDD EZ onedir
    candidates = [
        d for d in os.listdir(dist)
        if d.startswith('PDD EZ') and os.path.isdir(os.path.join(dist, d))
    ]
    if not candidates:
        print('错误: dist/ 下未找到 PDD EZ onedir，请先执行 PyInstaller 打包')
        sys.exit(1)
    candidates.sort(
        key=lambda d: os.path.getmtime(os.path.join(dist, d)), reverse=True
    )
    onedir = os.path.join(dist, candidates[0])
    version = candidates[0].replace('PDD EZ ', '')
    output = os.path.join(dist, f'PDD_EZ_{version}_update.zip')
    print(f'[增量打包] 源: {onedir}')
    print(f'[增量打包] 输出: {output}')
    build_update_zip(onedir, output, force='--force' in sys.argv)
