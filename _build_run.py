import os, subprocess, sys
os.chdir(r"F:\ai workspace\pdd-inventory")

# Fix indent
with open("gui.py","rb") as f: data=f.read()
data=data.replace(b"set(path)\r\n\n    def _get_export_path", b"set(path)\r\n\r\n    def _get_export_path")
with open("gui.py","wb") as f: f.write(data)

import py_compile
try:
    py_compile.compile("gui.py", doraise=True)
    print("INDENT OK")
except py_compile.PyCompileError as e:
    print("INDENT STILL BROKEN, brute force...")
    # Brute force: rewrite _get_export_path with correct indent
    with open("gui.py","r",encoding="utf-8") as f:
        lines = f.readlines()
    for i,line in enumerate(lines):
        if 'def _get_export_path(self):' in line and not line.startswith('    def'):
            lines[i] = '    ' + line.lstrip()
            print(f"Fixed line {i+1}")
            break
    with open("gui.py","w",encoding="utf-8") as f:
        f.writelines(lines)
    py_compile.compile("gui.py", doraise=True)
    print("BRUTE FORCE OK")

# Build
spec = [f for f in os.listdir('.') if f.endswith('.spec')][0]
print("Building...")
r = subprocess.run([sys.executable,"-m","PyInstaller",spec,"--noconfirm"], capture_output=True, text=True, timeout=300)
print("BUILD:", r.returncode)

# Run
exe = os.path.join("dist","PDD EZ v2.1.exe")
if os.path.exists(exe):
    print("Launching...")
    subprocess.Popen(exe)
else:
    print("EXE NOT FOUND")