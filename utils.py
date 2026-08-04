"""
PDD EZ — 公共工具函数
提供数据目录路径和设置读取，消除 main/ocr/gui 中的重复定义。
"""
import os, sys, json

VERSION = "v1.3"
EXE_NAME = f"PDD EZ {VERSION}.exe"


def version_newer(remote: str, local: str) -> bool:
    """比较两个 vX.Y[.Z] 格式的版本号，返回 remote > local"""
    def _parse(v):
        # 去掉前缀 v/V，按 . 拆分转整数列表
        v = v.lstrip('vV')
        return [int(x) for x in v.split('.') if x.isdigit()]
    try:
        r = _parse(remote)
        l = _parse(local)
        # 补齐到相同长度：v1.1 vs v1.1.0 → [1,1,0] vs [1,1,0]，避免元组长度比较误判
        n = max(len(r), len(l))
        r += [0] * (n - len(r))
        l += [0] * (n - len(l))
        return tuple(r) > tuple(l)
    except Exception:
        return remote != local  # fallback: 不相等即视为更新


# 核心列映射默认值：真实 PDD 后台表头（glm-4.6v 实测确认）
# 商品信息列含商品名 + 商品ID（小字 ID:xxx），识别时拆分为 name + sku_id
DEFAULT_COL_MAPPING = {
    'name': '商品信息',
    'stock': '仓库总库存',
    'sales': '仓库预估总销售数',
    'region': '销售区域',
    'warehouse': '仓库信息',
}
"""核心列映射默认值：通用列名 → 业务字段。可在设置页修改（后台列名变化时）。"""


def get_ocr_columns() -> dict:
    """
    读取识别列配置：{all: [探测到的全部列], selected: [客户勾选列], mapping: {字段: 列名}}。
    缺省时用默认映射，selected 为空则默认全部列。
    """
    s = Config.load()
    cfg = s.get('ocr_columns') or {}
    if not isinstance(cfg, dict):
        cfg = {}
    mapping = dict(DEFAULT_COL_MAPPING)
    saved_map = cfg.get('mapping') or {}
    if isinstance(saved_map, dict):
        # 旧默认检测：用户保存过但从未自定义（值恰为 v1.3 旧默认全集）时，
        # 视为"默认映射未动过"，允许新默认覆盖——否则升级后旧列名（商品名称/省份/仓库）
        # 匹配不上真实表头（商品信息/销售区域/仓库信息），导致识别为空。
        _OLD_DEFAULTS = {
            'name': '商品名称', 'stock': '仓库总库存', 'sales': '仓库预估总销售数',
            'region': '省份', 'warehouse': '仓库',
        }
        _is_old_default = bool(saved_map) and all(
            saved_map.get(k) == v for k, v in _OLD_DEFAULTS.items())
        if not _is_old_default:
            for k, v in saved_map.items():
                if v:  # 空值不覆盖默认
                    mapping[k] = v
    return {
        'all': list(cfg.get('all') or []),
        'selected': list(cfg.get('selected') or []),
        'mapping': mapping,
    }


def save_ocr_columns(all_cols: list = None, selected: list = None, mapping: dict = None):
    """持久化识别列配置到 settings.json（原子写入）。"""
    s = Config.load()
    cur = s.get('ocr_columns') or {}
    if not isinstance(cur, dict):
        cur = {}
    if all_cols is not None:
        cur['all'] = list(all_cols)
    if selected is not None:
        cur['selected'] = list(selected)
    if mapping is not None:
        cur['mapping'] = dict(mapping)
    s['ocr_columns'] = cur
    Config.save(s)


def get_secondary_model() -> str:
    """读取双模型验证的副模型，默认 glm-4v-flash。"""
    s = Config.load()
    cfg = s.get('ocr_columns') or {}
    if not isinstance(cfg, dict):
        return 'glm-4v-flash'
    return cfg.get('secondary_model') or 'glm-4v-flash'


def save_secondary_model(name: str):
    """保存双模型验证的副模型（原子写入）。"""
    s = Config.load()
    cur = s.get('ocr_columns') or {}
    if not isinstance(cur, dict):
        cur = {}
    cur['secondary_model'] = name
    s['ocr_columns'] = cur
    Config.save(s)


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
        # mode 字段可能是旧版唯一标识（{"api": {"mode": "qwen", "key": "xxx"}}），
        # 需作为模型名回退，否则迁移后一律判成 doubao
        old_model = (api.get('builtin_model', '') or api.get('custom_model', '')
                     or api.get('mode', ''))
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


def capture_pdd_screenshot(output_path: str, out_window_pos: dict = None) -> bool:
    """
    锁定浏览器窗口截图 → 按设置裁剪 → 保存。
    返回 True 表示截到窗口，False 表示未找到窗口（已 fallback 全屏）。
    out_window_pos: 可选 dict，调用方传入后填充 {'left': int, 'top': int}（窗口左上角
    在全屏坐标系中的位置）。滚动/点击坐标换算时用窗口位置还原全屏偏移，
    避免窗口未最大化时坐标错位（如 1920 窗口在 4K 屏上）。
    """
    import os as _os, time as _time
    _os.makedirs(_os.path.dirname(output_path) or '.', exist_ok=True)

    # AI 自动定位表格后不再需要手动裁剪比例；截图全图交给 AI bbox 定位
    import pyautogui as pg
    from PIL import Image as PILImage

    found_window = False
    img = None
    win_left = win_top = 0
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
                win_left, win_top = win.left, win.top
                img = pg.screenshot(region=(win.left, win.top, win.width, win.height))
                break
    except Exception:
        pass

    if img is None:
        # 未找到窗口，或找到窗口但截图失败（句柄无效等）→ fallback 全屏
        img = pg.screenshot()
        win_left = win_top = 0

    # 调用方需要窗口位置（滚动坐标换算）时回传
    if isinstance(out_window_pos, dict):
        out_window_pos['left'] = int(win_left)
        out_window_pos['top'] = int(win_top)
        # 窗口原始宽高（截图缩放前），供滚动坐标按真实比例还原
        if img is not None:
            out_window_pos['width'] = int(img.size[0])
            out_window_pos['height'] = int(img.size[1])

    w, h = img.size
    cw, ch = w, h  # AI 定位表格自行 bbox，不再按比例预裁剪
    if cw > 2560:
        img = img.resize((2560, int(ch * 2560 / cw)), PILImage.LANCZOS)
    img.save(output_path)
    return found_window
