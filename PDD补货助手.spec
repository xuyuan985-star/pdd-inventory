# -*- mode: python ; coding: utf-8 -*-
# v1.3 目录版（onedir）：依赖在 _internal/ 目录，支持增量更新包
# （onefile 会把依赖打进单个 exe，更新只能全量下载 78MB，增量机制失效）

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[],
    datas=[('ocr.py', '.'), ('vision.py', '.'), ('utils.py', '.'), ('config.py', '.'), ('export_xlsx.py', '.'), ('settings_ui.py', '.'), ('stats_ui.py', '.'), ('settings_template.json', '.'), ('icon.ico', '.'), ('templates', 'templates'), ('regions.json', '.'), ('使用说明.txt', '.'), ('history_db.py', '.'), ('usage_extractor.py', '.'), ('usage_store.py', '.'), ('table_import.py', '.')],
    hiddenimports=['pyautogui', 'openpyxl', 'PIL', 'requests', 'cv2', 'numpy', 'pyperclip', 'history_db', 'usage_extractor', 'usage_store', 'table_import', 'stats_ui'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PDD EZ',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PDD EZ',
)
