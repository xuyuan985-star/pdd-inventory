# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[],
    datas=[('main.py', '.'), ('pdd_import.py', '.'), ('ocr.py', '.'), ('vision.py', '.'), ('dpi_utils.py', '.'), ('settings.json', '.'), ('icon.ico', '.'), ('templates', 'templates')],
    hiddenimports=['cryptography','api_keys','pyautogui', 'openpyxl', 'PIL', 'requests', 'cv2', 'numpy', 'pyperclip'],
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
    name='PDD EZ v2.1',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',
)
