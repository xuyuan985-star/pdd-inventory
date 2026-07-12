"""DPI 缩放检测（Windows）"""
import ctypes

def get_dpi_scale() -> float:
    """返回 Windows DPI 缩放比例（1.0=100%, 1.25=125%, 1.5=150%...）"""
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()
        hdc = user32.GetDC(0)
        dpi_x = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        user32.ReleaseDC(0, hdc)
        return dpi_x / 96.0  # 96 DPI = 100%
    except Exception:
        return 1.0

def get_virtual_screen_size() -> tuple:
    """返回真实屏幕物理像素（VirtualScreen）"""
    try:
        from win32api import GetSystemMetrics
        from win32con import SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN
        x = GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = GetSystemMetrics(SM_CYVIRTUALSCREEN)
        return (w, h)
    except Exception:
        import pyautogui
        s = pyautogui.size()
        return (s.width, s.height)
