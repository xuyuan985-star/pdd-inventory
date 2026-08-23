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
# ⚠️ key 统一小写：get_changed_packages 解析 requirements 时 pkg.lower()，
# 大写 key（Pillow/PyYAML）会导致匹配失败漏打包（v1.4 审查修复）
PIP_TO_INTERNAL = {
    'opencv-python':        ['cv2'],
    'numpy':                ['numpy', 'numpy.libs'],          # + numpy-*.dist-info 通配
    'pillow':               ['PIL'],
    'pywin32':              ['win32', 'pywin32_system32'],
    'lxml':                 ['lxml'],
    'cryptography':         ['cryptography'],                 # + cryptography-*.dist-info
    'certifi':              ['certifi'],
    'pyyaml':               ['yaml'],
    'charset-normalizer':   ['charset_normalizer'],
}

SKIP_DIRS = {'__pycache__', 'tests', 'test'}
SKIP_EXTENSIONS = {'.pyc', '.pyo', '.log', '.tmp'}
# 敏感文件：settings.json 含用户 API key/账号密码，keys.json/keys.enc 同理——
# 即使 _internal 里的 settings.json 是模板，也绝不进包（防覆盖客户 %APPDATA% 已配置副本）
SKIP_FILES = {'settings.json', 'keys.json', 'keys.enc', 'config.ini', '.env'}


def _run(cmd: list) -> str:
    """运行命令，返回 stripped stdout；失败返回空字符串"""
    try:
        r = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True,
                          timeout=10, encoding='utf-8', errors='replace')
        return r.stdout.strip() if r.returncode == 0 else ''
    except Exception:
        return ''


def _read_version_from_utils() -> str:
    """从 utils.py 读取 VERSION = "vX.Y"（固定目录名后版本号来源）。失败返回空串"""
    try:
        src = open(os.path.join(REPO_ROOT, 'utils.py'), encoding='utf-8').read()
        m = re.search(r'VERSION\s*=\s*["\'](v[\d.]+)["\']', src)
        return m.group(1) if m else ''
    except Exception:
        return ''


def get_last_release_tag(exclude: str = '') -> str:
    """获取最近一次 Release 对应的 tag（按版本号排序），排除当前构建版本。

    只取 ≤ 当前版本的 tag 作基准：历史遗留的"更大版本号"干扰 tag
    （如 api_keys 时代的 v2.2）会劫持基准导致增量包退化成全量（v1.4 修复）。
    exclude 传当前版本号（如 v1.4）时，基准 = 最新一个 ≤v1.4 且 ≠v1.4 的 tag。
    """
    tags = _run(['git', 'tag', '--sort=-version:refname'])
    if not tags:
        return ''
    # 版本元组比较，跳过非 vX.Y[.Z] 格式（如 v2.2-beta）
    import re as _re
    def _key(t):
        m = _re.search(r'^v(\d+)\.(\d+)(?:\.(\d+))?', t.strip())
        if not m:
            return None
        return tuple(int(x) for x in m.groups(default='0'))
    cur = _key(exclude)
    for t in tags.split('\n'):
        t = t.strip()
        k = _key(t)
        if not k or t == exclude:
            continue
        if cur is not None and k > cur:
            continue  # 排除大于当前版本的 tag（防 v2.2 之类劫持）
        return t
    return ''


def get_changed_files_since(tag: str) -> set:
    """返回从 tag 到当前工作区的变更文件列表（含暂存、未暂存、未跟踪）。

    未跟踪（untracked）文件不会出现在 git diff 里——新增的模板/模块
    未 git add 时会被漏掉，导致更新包缺文件（v1.4 审查修复）。
    """
    if not tag:
        return set()
    changed = set()
    # 1) 已跟踪文件的变更（staged + unstaged）
    # core.quotepath=false：中文/特殊字符文件名不被转义成 \xxx，否则变更列表会错（漏包/多包）
    out = _run(['git', '-c', 'core.quotepath=false', 'diff', '--name-only', tag])
    if out:
        changed.update(out.split('\n'))
    # 2) 未跟踪文件（未 add 的新文件，含被 .gitignore 忽略的除外）
    out = _run(['git', '-c', 'core.quotepath=false', 'ls-files', '--others', '--exclude-standard'])
    if out:
        changed.update(out.split('\n'))
    return changed


def get_changed_packages(changed_files: set, exclude_tag: str = '') -> set:
    """
    从变更文件列表中反推哪些 pip 包可能变了。
    规则：requirements.txt 变更 → 解析 diff 找出被改动的包名。
    """
    if 'requirements.txt' not in changed_files:
        return set()

    # 获取 requirements.txt 从上次 tag 到现在的 diff
    tag = get_last_release_tag(exclude=exclude_tag)
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


# _internal 目录名 → dist-info 前缀映射
# （目录名如 cv2/PIL/win32，但 dist-info 用 pip 包名如 opencv_python/Pillow/pywin32）
DIR_TO_DIST_PREFIX = {
    'cv2': 'opencv_python',
    'PIL': 'Pillow',
    'win32': 'pywin32',
    'yaml': 'PyYAML',
    'charset_normalizer': 'charset_normalizer',
    'numpy': 'numpy',
    'cryptography': 'cryptography',
    'lxml': 'lxml',
    'certifi': 'certifi',
    'numpy.libs': 'numpy',
}


def _match_dist_info(name: str, pkg_dirs: set) -> bool:
    """判断 dist-info 目录是否属于变更的包（目录名 → pip 包名前缀映射）"""
    for pkg in pkg_dirs:
        prefix = DIR_TO_DIST_PREFIX.get(pkg, pkg)
        if name.startswith(prefix + '-') and '.dist-info' in name:
            return True
    return False


def build_update_zip(onedir_path: str, output_path: str, force: bool = False):
    onedir = os.path.abspath(onedir_path)
    name = os.path.basename(onedir)
    dist_parent = os.path.dirname(onedir)
    internal = os.path.join(onedir, '_internal')

    # ── 1. 确定变更范围 ──
    # 基准 = 最近一个『不是当前版本』的 tag（刚打 v1.2 tag 时不能拿自己当基准）
    # v1.4+ 固定目录名（PDD EZ）后，版本号从 utils.py 的 VERSION 读取，不再从目录名推断
    current_version = _read_version_from_utils()
    tag = get_last_release_tag(exclude=current_version)
    changed_files = get_changed_files_since(tag)

    if force or not tag:
        print(f"[增量打包] 模式: {'强制全量' if force else '首次发布（无 tag）'}")
        include_all = True
        changed_packages = set(PIP_TO_INTERNAL.keys())
        deleted_files = []
    else:
        changed_packages = get_changed_packages(changed_files, exclude_tag=current_version)
        include_all = False
        print(f"[增量打包] 基准 tag: {tag}")
        print(f"[增量打包] 变更文件: {len(changed_files)} 个")
        if changed_packages:
            print(f"[增量打包] 依赖变更: {changed_packages}")
        # v1.4.5（bug hunt F15）：生成"从此版本起删除的文件"清单——git diff --diff-filter=D
        # 的运行时资源/模板/文档写进包内 deleted-files.txt，updater 安装时按白名单删除，
        # 避免老客户旧 dll/旧模板/旧文档永久残留（新旧混用）
        _del_out = _run(['git', '-c', 'core.quotepath=false',
                         'diff', '--name-only', '--diff-filter=D', tag])
        _ok_prefix = ('templates/',)
        _ok_exact = {'icon.ico', 'regions.json', 'settings_template.json', '使用说明.txt'}
        deleted_files = [d for d in (_del_out.splitlines() if _del_out else [])
                         if d.startswith(_ok_prefix) or d in _ok_exact]
        if deleted_files:
            print(f"[增量打包] 删除清单: {deleted_files}")

    # 判断是否需要包含更新器 EXE
    # v1.4.5（bug hunt F16）：updater 为 onefile、PYZ 内嵌 github_api.py/logger.py——
    # 共享代码变更时旧判定漏打新 updater，客户端 updater 长期持旧逻辑（行为分裂）
    updater_changed = include_all or any(
        f in changed_files for f in ['updater.py', 'updater.spec', 'github_api.py', 'logger.py']
    )
    # 模板/资源文件是否变更
    templates_changed = include_all or any(
        f.startswith('templates/') for f in changed_files
    )
    # 注意：settings.json 是用户本机配置（含 API key/账号密码），
    # 绝不进更新包——更新会覆盖用户配置并泄露开发机凭据
    # v1.4.5（bug hunt F17）：spec datas 里的 settings_template.json/使用说明.txt 也需进包，
    # 否则老客户永远拿不到新模板/新文档（如新增 provider 预设）
    resources_changed = include_all or any(
        f in changed_files for f in
        ('icon.ico', 'regions.json', 'settings_template.json', '使用说明.txt')
    )

    # 主 EXE（含编译后 PYZ）是否变更：代码/依赖/打包配置变更才需要——
    # 纯模板/资源变更时 EXE 内容没变，塞进增量包只会白白拉大体积
    # （v1.4 审查修复：此前无条件包含，老客户升级体积被撑大）
    main_exe_changed = include_all or any(
        f.endswith(('.py', '.spec')) or f == 'requirements.txt' or f.startswith('gui')
        for f in changed_files
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

    # ── 3. 打包 zip ──
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        added = 0

        # v1.4.5（bug hunt F15）：包内携带删除清单，updater 安装时白名单删除旧残留
        if deleted_files:
            zf.writestr(os.path.join(name, 'deleted-files.txt'), '\n'.join(deleted_files))
            added += 1
            print(f"  + deleted-files.txt ({len(deleted_files)} 项)")

        # 主 EXE / 壳 EXE — 仅代码/依赖变更时包含；PyInstaller 每次构建都会重新生成，
        # 内含编译后的 PYZ，是代码变更的核心载体。
        # 纯模板/资源变更不塞 EXE（增量体积优化，v1.4 审查修复）
        # v1.4+ 固定名：exe 名不再依赖目录名（防目录被改名后拼错）
        if main_exe_changed:
            exe = os.path.join(onedir, 'PDD EZ.exe')
            if os.path.exists(exe):
                arcname = os.path.join(name, os.path.basename(exe))
                zf.write(exe, arcname)
                added += 1
                print(f"  + {os.path.basename(exe)}")

        # 更新器 EXE（仅源码变更时）— 放入 name/ 目录，与 updater.py 自升级查找逻辑一致
        updater_exe = os.path.join(dist_parent, 'PDD EZ Updater.exe')
        if os.path.exists(updater_exe) and updater_changed:
            zf.write(updater_exe, os.path.join(name, os.path.basename(updater_exe)))
            added += 1
            print(f"  + {os.path.basename(updater_exe)}")

        # 资源文件（仅当变更时）— settings.json 是用户本机配置，绝不进包
        if resources_changed:
            for res in ['icon.ico', 'regions.json']:
                for src in [os.path.join(onedir, res), os.path.join(internal, res)]:
                    if os.path.exists(src):
                        zf.write(src, os.path.join(name, res))
                        added += 1
                        print(f"  + {res}")

        # templates 目录（onedir 根，spec datas 放这里）— 仅当变更时进包，
        # 否则客户永远拿旧模板（v1.4 修复：此前从未打包）
        if templates_changed:
            tpl_src = os.path.join(onedir, 'templates')
            if os.path.isdir(tpl_src):
                for root, dirs, files in os.walk(tpl_src):
                    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                    for f in files:
                        if f in SKIP_FILES or any(f.endswith(ext) for ext in SKIP_EXTENSIONS):
                            continue
                        src = os.path.join(root, f)
                        rel = os.path.relpath(src, onedir)
                        zf.write(src, os.path.join(name, rel))
                        added += 1
                        print(f"  + templates/{os.path.relpath(src, tpl_src)}")

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
                    # 跳过临时/编译文件 + 敏感配置文件
                    if f in SKIP_FILES or any(f.endswith(ext) for ext in SKIP_EXTENSIONS):
                        continue
                    src = os.path.join(root, f)
                    rel = os.path.relpath(src, internal)

                    # 跳过未变更包的 .dist-info 元数据；
                    # 变更包的 dist-info 保留（certifi 运行时要读元数据定位证书路径）
                    if '.dist-info' in rel and not _match_dist_info(rel.split(os.sep)[0], include_dirs):
                        continue

                    arcname = os.path.join(name, '_internal', rel)
                    zf.write(src, arcname)
                    added += 1

    size = os.path.getsize(output_path)
    print(f"\n[增量打包] 完成: {size/1024:.0f} KB ({added} 个文件)")
    if added <= 3:
        print("[增量打包] 极小更新 — 仅必要文件")


if __name__ == '__main__':
    # v1.4.5（bug hunt F18-②）：git 不可用时明确报错退出——此前静默按"首次发布"打
    # 全量包且文件仍叫 _update.zip，会误导发布（增量基准丢失）
    if _run(['git', 'rev-parse', '--is-inside-work-tree']) != 'true':
        print('错误: 当前目录不是 git 仓库（或 git 不可用）。增量包依赖 git tag/diff 基准，'
              '请在有完整历史与 tag 的克隆中执行打包。')
        sys.exit(1)
    dist = os.path.join(REPO_ROOT, 'dist')
    # dist 不存在时给出明确错误，而不是 os.listdir 抛 FileNotFoundError
    if not os.path.isdir(dist):
        print(f'错误: dist/ 目录不存在: {dist}\n请先执行 PyInstaller 打包')
        sys.exit(1)
    # 自动发现 dist 下的 PDD EZ onedir（固定目录名 PDD EZ）
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
    # zip 文件名仍带版本号（GitHub 资产名区分版本），版本号从 utils.py 读取
    version = _read_version_from_utils()
    if not version:
        print('错误: 无法从 utils.py 读取 VERSION')
        sys.exit(1)
    output = os.path.join(dist, f'PDD_EZ_{version}_update.zip')
    print(f'[增量打包] 源: {onedir}')
    print(f'[增量打包] 输出: {output}')
    build_update_zip(onedir, output, force='--force' in sys.argv)
    # v1.4.5（bug hunt F18-①）：构建后自动写 .sha256——之前只靠人工/发布时补传，漏传
    # 即触发更新器 fail-open（跳过校验安装）
    import hashlib
    _h = hashlib.sha256(open(output, 'rb').read()).hexdigest()
    with open(output + '.sha256', 'w', encoding='utf-8') as _f:
        _f.write(f'{_h}  {os.path.basename(output)}')
    print(f'[增量打包] 已生成 {os.path.basename(output)}.sha256: {_h}')
