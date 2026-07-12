import os
os.chdir(r"F:\ai workspace\pdd-inventory")
with open("gui.py", "rb") as f:
    data = f.read()
# Find the lone \n before _get_export_path
# Pattern: .set(path)\r\n\n    def _get_export_path
old = b"set(path)\r\n\n    def _get_export_path"
new = b"set(path)\r\n\r\n    def _get_export_path"
data = data.replace(old, new)
with open("gui.py", "wb") as f:
    f.write(data)
import py_compile
py_compile.compile("gui.py", doraise=True)
print("OK - INDENT FIXED")