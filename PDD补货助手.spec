# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[],
    datas=[('main.py', '.'), ('pdd_import.py', '.'), ('ocr.py', '.'), ('vision.py', '.'), ('utils.py', '.'), ('config.py', '.'), ('export_xlsx.py', '.'), ('settings_ui.py', '.'), ('settings.json', '.'), ('icon.ico', '.'), ('templates', 'templates')],
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
    [],
    exclude_binaries=True,
    name='PDD EZ v1.1',
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
    name='PDD EZ v1.1',
)
