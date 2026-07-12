import os
os.chdir(r"F:\ai workspace\pdd-inventory")
with open("gui.py","rb") as f:
    data = f.read()
idx = data.find(b'self.export_path_var.set(path)')
chunk = data[idx-50:idx+80]
for i,b in enumerate(chunk):
    c = chr(b) if 32<=b<127 else '.'
    print(f"{i:3d} | 0x{b:02x} | {c}")