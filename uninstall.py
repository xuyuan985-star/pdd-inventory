"""PDD EZ 卸载工具 — 清除所有数据和残留"""
import os, sys, shutil

def main():
    print("=" * 50)
    print("  PDD EZ 卸载工具")
    print("=" * 50)
    
    cleaned = []
    
    # 1. 清除 AppData 数据目录
    appdata = os.path.join(os.environ.get('APPDATA', ''), 'PDD补货助手')
    if os.path.exists(appdata):
        try:
            shutil.rmtree(appdata)
            cleaned.append(f"数据目录: {appdata}")
        except Exception as e:
            print(f"⚠ 无法删除 {appdata}: {e}")
    
    # 2. 清除 EXE 所在目录的残留文件（settings.json, regions.json 等）
    exe_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
    for f in ['settings.json', 'regions.json', 'icon.ico']:
        fp = os.path.join(exe_dir, f)
        if os.path.exists(fp):
            try:
                os.remove(fp)
                cleaned.append(f"残留文件: {fp}")
            except: pass
    
    # 3. 清除 output 目录
    output_dir = os.path.join(exe_dir, 'output')
    if os.path.exists(output_dir):
        try:
            shutil.rmtree(output_dir)
            cleaned.append(f"截图缓存: {output_dir}")
        except: pass
    
    # 4. 清除 templates 目录
    tmpl_dir = os.path.join(exe_dir, 'templates')
    if os.path.exists(tmpl_dir):
        try:
            shutil.rmtree(tmpl_dir)
            cleaned.append(f"模板缓存: {tmpl_dir}")
        except: pass
    
    print()
    if cleaned:
        print("✅ 已清除以下内容:")
        for c in cleaned:
            print(f"   • {c}")
    else:
        print("ℹ 没有找到需要清除的数据")
    
    print()
    print("卸载完成。可手动删除 EXE 文件。")
    input("\n按 Enter 退出...")

if __name__ == '__main__':
    main()
