"""
PDD EZ — 公共工具函数
提供数据目录路径和设置读取，消除 main/ocr/gui 中的重复定义。
"""
import os, sys, json

VERSION = "v1.1"
EXE_NAME = f"PDD EZ {VERSION}.exe"


def version_newer(remote: str, local: str) -> bool:
    """比较两个 vX.Y[.Z] 格式的版本号，返回 remote > local"""
    def _parse(v):
        # 去掉前缀 v/V，按 . 拆分转整数元组
        v = v.lstrip('vV')
        return tuple(int(x) for x in v.split('.') if x.isdigit())
    try:
        return _parse(remote) > _parse(local)
    except Exception:
        return remote != local  # fallback: 不相等即视为更新


class Config:
    """配置单例：唯一读写 settings.json，原子写入。"""
    @staticmethod
    def load():
        try:
            sf = os.path.join(get_base_dir(), 'settings.json')
            if os.path.exists(sf):
                with open(sf, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    @staticmethod
    def save(data: dict):
        sf = os.path.join(get_base_dir(), 'settings.json')
        tmp = sf + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, sf)

    @staticmethod
    def get(key, default=None):
        return Config.load().get(key, default)

    @staticmethod
    def set(key, value):
        data = Config.load()
        data[key] = value
        Config.save(data)


def get_base_dir() -> str:
    """可写数据目录：打包后 → %APPDATA%/PDD补货助手，源码 → 脚本目录"""
    if getattr(sys, 'frozen', False):
        data_dir = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'PDD补货助手')
        os.makedirs(data_dir, exist_ok=True)
        return data_dir
    return os.path.dirname(os.path.abspath(__file__))


def get_api_config() -> dict:
    """读取 settings.json 中的 API 配置，自动迁移旧格式"""
    try:
        s = Config.load()
        api = s.get('api', {})
        # 已是新格式直接返回
        if 'providers' in api:
            return api
        # 迁移旧格式 → 新格式
        old_model = api.get('builtin_model', '') or api.get('custom_model', '')
        old_key = api.get('key', '')
        # 推断提供商
        if old_model.lower().startswith('doubao') or 'doubao' in old_model.lower():
            active = 'doubao'; ep = 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'
        elif old_model.startswith('qwen'):
            active = 'qwen'; ep = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
        elif old_model.startswith('glm'):
            active = 'glm'; ep = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
        else:
            active = 'doubao'; ep = 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'
        new_api = {
            'active_provider': active,
            'providers': {
                active: {'api_key': old_key, 'model': old_model, 'endpoint': ep, 'model_history': [old_model] if old_model else []}
            }
        }
        s['api'] = new_api
        Config.save(s)
        return new_api
    except (json.JSONDecodeError, IOError, OSError):
        pass
    return {}


def capture_pdd_screenshot(output_path: str) -> bool:
    """
    锁定浏览器窗口截图 → 按设置裁剪 → 保存。
    返回 True 表示截到窗口，False 表示未找到窗口（已 fallback 全屏）。
    """
    import os as _os, json as _json, time as _time
    _os.makedirs(_os.path.dirname(output_path) or '.', exist_ok=True)

    # 读裁剪比例
    crop_cfg = {'left': 0.11, 'top': 0.40}
    try:
        sf = _os.path.join(get_base_dir(), 'settings.json')
        if _os.path.exists(sf):
            with open(sf, 'r', encoding='utf-8') as f:
                crop_cfg = _json.load(f).get('crop', crop_cfg)
    except Exception:
        pass

    import pyautogui as pg
    from PIL import Image as PILImage

    found_window = False
    try:
        import pygetwindow as gw
        for title in ['拼多多', 'pinduoduo', 'Microsoft Edge', 'Edge', 'Chrome', 'Firefox']:
            wins = gw.getWindowsWithTitle(title)
            if wins:
                win = wins[0]
                found_window = True
                if win.isMinimized:
                    win.restore()
                win.activate()
                _time.sleep(0.2)
                img = pg.screenshot(region=(win.left, win.top, win.width, win.height))
                break
    except Exception:
        pass

    if not found_window:
        img = pg.screenshot()

    w, h = img.size
    sidebar = int(w * crop_cfg['left'])
    img = img.crop((sidebar, int(h * crop_cfg['top']), w, h))
    cw, ch = img.size  # 裁剪后尺寸
    if cw > 2560:
        img = img.resize((2560, int(ch * 2560 / cw)), PILImage.LANCZOS)
    img.save(output_path)
    return found_window
