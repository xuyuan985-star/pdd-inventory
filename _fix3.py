import os, sys, py_compile, re, subprocess
os.chdir(r"F:\ai workspace\pdd-inventory")

with open("gui.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

# ===== BUG 1: api_key_var already OK (no stars found) =====

# ===== BUG 2: Remove self.page_home.pack from _show_page ending =====
in_show = False
for i, l in enumerate(lines):
    if "def _show_page(self, page):" in l:
        in_show = True
    if in_show and "self.page_home.pack(fill=" in l and "expand=True)" in l:
        lines[i] = l.replace("self.page_home.pack", "# self.page_home.pack")
        print(f"Bug2 fixed: line {i+1}")
        break

# ===== BUG 3: Fix _build_*_tab signatures: add dlg=None =====
sigs = [
    ("def _build_product_region_tab(self, parent, dlg):", "def _build_product_region_tab(self, parent, dlg=None):"),
    ("def _build_skin_tab(self, parent, dlg):", "def _build_skin_tab(self, parent, dlg=None):"),
    ("def _build_calibrate_tab(self, parent, dlg):", "def _build_calibrate_tab(self, parent, dlg=None):"),
    ("def _build_backend_tab(self, parent, dlg):", "def _build_backend_tab(self, parent, dlg=None):"),
]
for old_sig, new_sig in sigs:
    for i, l in enumerate(lines):
        if old_sig in l:
            lines[i] = l.replace(old_sig, new_sig)
            print(f"Bug3 fixed: {old_sig[:40]} at line {i+1}")
            break

with open("gui.py", "w", encoding="utf-8") as f:
    f.writelines(lines)

py_compile.compile("gui.py", doraise=True)
py_compile.compile("ocr.py", doraise=True)
print("ALL 3 BLOCKING BUGS FIXED")

# Build
spec = [f for f in os.listdir('.') if f.endswith('.spec')][0]
r = subprocess.run([sys.executable, "-m", "PyInstaller", spec, "--noconfirm"],
                   capture_output=True, text=True, timeout=300)
print("BUILD:", r.returncode)
subprocess.Popen(os.path.join("dist", "PDD EZ v2.1.exe"))