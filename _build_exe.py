import subprocess, os, sys
os.chdir(r"F:\ai workspace\pdd-inventory")
# delete old exe
for f in os.listdir("dist"):
    if f.endswith(".exe"):
        os.remove(os.path.join("dist", f))
r = subprocess.run([sys.executable, "-m", "PyInstaller", "PDD䃧货助手.spec", "--noconfirm"],
                   capture_output=True, text=True, timeout=300)
print(r.stdout[-300:] if r.stdout else "")
print(r.stderr[-200:] if r.stderr else "")
print("RC:", r.returncode)
print("DIST:", os.listdir("dist"))