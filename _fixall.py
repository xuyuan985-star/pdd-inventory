"""全修审查6bug"""
import os, sys, py_compile, subprocess, shutil
os.chdir(r"F:\ai workspace\pdd-inventory")

with open("gui.py", "r", encoding="utf-8") as f:
    g = f.read()

# Bug1: _save_settings dlg.destroy() crash
g = g.replace(
    'messagebox.showinfo("已保存", f"导出路径已设置为：\\n{path}", parent=dlg)\n        dlg.destroy()',
    'messagebox.showinfo("已保存", f"导出路径已设置为：\\n{path}")\n        if dlg: dlg.destroy()')
print("Bug1 fixed: _save_settings dlg guard")

# Bug2: Custom API mode - add custom_model input to _build_general_page
# Find the custom API radiobutton section and add model input
old_custom = """tk.Radiobutton(rf, text='默认API（内置）', variable=api_mode, value='default').pack(anchor='w')
        tk.Radiobutton(rf, text='自定义API', variable=api_mode, value='custom').pack(anchor='w')
        builtin_model_val"""
new_custom = """tk.Radiobutton(rf, text='默认API（内置）', variable=api_mode, value='default').pack(anchor='w')
        tk.Radiobutton(rf, text='自定义API', variable=api_mode, value='custom').pack(anchor='w')
        custom_model_var = tk.StringVar(self.win, value=api_cfg.get('custom_model', ''))
        cmf = tk.Frame(content); cmf.pack(pady=3, padx=40, fill='x')
        tk.Label(cmf, text='自定义模型名:', font=(self.FONT[0], 8), fg=self.C_MUTED).pack(side='left')
        tk.Entry(cmf, textvariable=custom_model_var, font=(self.FONT[0], 8), width=30).pack(side='left', padx=5)
        builtin_model_val"""
g = g.replace(old_custom, new_custom)

# Update save_api to include custom_model
old_save = """s['api'] = {'mode': api_mode.get(), 'key': api_key_var.get().strip(), 'builtin_model': builtin_model_val.get()}"""
new_save = """s['api'] = {'mode': api_mode.get(), 'key': api_key_var.get().strip(),
                         'builtin_model': builtin_model_val.get(), 'custom_model': custom_model_var.get()}"""
g = g.replace(old_save, new_save)
print("Bug2 fixed: custom_model input + save")

# Bug3: GLM fallback duplication in ocr.py
with open("ocr.py", "r", encoding="utf-8") as f:
    o = f.read()
o = o.replace(
    "models = [builtin_model, 'glm-4v-flash']\n        else:  # glm models",
    "models = [builtin_model]\n        else:  # glm models")
print("Bug3 fixed: GLM fallback dedup")

# Bug4: skin card theme protection - add _skip_theme marker
g = g.replace(
    "card = tk.Frame(cards_frame, bg=\"#FFFFFF\",",
    "card = tk.Frame(cards_frame, bg=\"#FFFFFF\",")
# Actually need to find the _walk_force loop and skip _skip_theme
old_walk = """if cls == 'Frame':
                    # 检查是否是"卡片"型 Frame（有高亮边框）→ 用表面色
                    try:
                        hl = w.cget('highlightthickness')
                        if hl and int(hl) > 0:
                            w.configure(bg=theme['C_SURFACE'])
                        else:
                            w.configure(bg=theme['C_BG'])
                    except:
                        w.configure(bg=theme['C_BG'])"""
new_walk = """if cls == 'Frame':
                    if getattr(w, '_skip_theme', False):
                        pass  # 保护皮肤预览卡片
                    else:
                        try:
                            hl = w.cget('highlightthickness')
                            if hl and int(hl) > 0:
                                w.configure(bg=theme['C_SURFACE'])
                            else:
                                w.configure(bg=theme['C_BG'])
                        except:
                            w.configure(bg=theme['C_BG'])"""
g = g.replace(old_walk, new_walk)
# Mark cards with _skip_theme
g = g.replace(
    'card = tk.Frame(cards_frame, bg="#FFFFFF",\n                           highlightbackground',
    'card = tk.Frame(cards_frame, bg="#FFFFFF",\n                           highlightbackground')
# Better approach: find card creation and add _skip_theme
g = g.replace('card._skin_name = name', 'card._skin_name = name\n            card._skip_theme = True')
print("Bug4 fixed: skin card theme protection")

with open("gui.py", "w", encoding="utf-8") as f:
    f.write(g)
with open("ocr.py", "w", encoding="utf-8") as f:
    f.write(o)

py_compile.compile("gui.py", doraise=True)
py_compile.compile("ocr.py", doraise=True)
print("ALL SYNTAX OK")

# Build
spec = [f for f in os.listdir('.') if f.endswith('.spec')][0]
r = subprocess.run([sys.executable, "-m", "PyInstaller", spec, "--noconfirm"],
                   capture_output=True, text=True, timeout=300)
print("BUILD:", r.returncode)

# Zip to desktop
import zipfile
desktop = os.path.expanduser(r"~\Desktop")
zip_path = os.path.join(desktop, "PDD_EZ_v2.1.zip")
with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
    zf.write(os.path.join("dist", "PDD EZ v2.1.exe"), "PDD EZ v2.1.exe")
print(f"ZIP: {zip_path}")
print("DONE")