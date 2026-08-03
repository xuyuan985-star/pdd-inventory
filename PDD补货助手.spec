# -*- mode: python ; coding: utf-8 -*-
# v1.3 单文件版：双击即开（onefile），运行时可写目录在 %APPDATA%/PDD补货助手

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[],
    datas=[('ocr.py', '.'), ('vision.py', '.'), ('utils.py', '.'), ('config.py', '.'), ('export_xlsx.py', '.'), ('settings_ui.py', '.'), ('settings.json', '.'), ('icon.ico', '.'), ('templates', 'templates'), ('regions.json', '.'), ('使用说明.txt', '.')],
    hiddenimports=['pyautogui', 'openpyxl', 'PIL', 'requests', 'cv2', 'numpy', 'pyperclip'],
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
    a.binaries,
    a.datas,
    [],
    name='PDD EZ v1.3',
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
