"""
PDD EZ — 公共工具函数
提供数据目录路径和设置读取，消除 main/ocr/gui 中的重复定义。
"""
import os, sys, json

VERSION = "v1.4"


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
        # 解析失败（如 v1.4.0-beta 非纯数字段）不视为更新——静默判"有更新"
        # 会导致每次启动都弹提示（v1.4 审查修复）
        return False


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
    """配置单例：唯一读写 settings.json，原子写入。
    v1.4 升级（借鉴 March7thAssistant config.py）：加载时与 settings_template.json
    递归合并——用户配置优先，缺失字段从模板补全并写回（配置自愈，损坏/缺字段不崩）。"""

    _template_cache = None

    @staticmethod
    def _load_template():
        """加载 settings_template.json 默认结构（缓存，失败返回 {}）"""
        if Config._template_cache is not None:
            return Config._template_cache
        tpl = {}
        try:
            # 打包后模板在 _MEIPASS；源码在脚本目录
            for cand in [os.path.join(get_base_dir(), 'settings_template.json'),
                         os.path.join(sys._MEIPASS, 'settings_template.json') if getattr(sys, 'frozen', False) else '']:
                if cand and os.path.exists(cand):
                    with open(cand, 'r', encoding='utf-8') as f:
                        tpl = json.load(f)
                    break
        except Exception:
            tpl = {}
        Config._template_cache = tpl
        return tpl

    @staticmethod
    def _merge(base: dict, override: dict) -> dict:
        """递归合并：override 优先，base 提供默认（March7th _update_config 同款）。
        用户 null 值按缺字段处理（补模板默认），防手动损坏配置崩程序。"""
        out = dict(base)
        for key, value in override.items():
            if value is None:
                continue  # 用户 null 视为缺字段，保留模板默认
            if key in out and isinstance(out[key], dict) and isinstance(value, dict):
                out[key] = Config._merge(out[key], value)
            else:
                out[key] = value
        return out

    @staticmethod
    def load():
        """读取 settings.json，与模板递归合并（用户配置优先，缺字段补默认）"""
        data = {}
        try:
            sf = os.path.join(get_base_dir(), 'settings.json')
            if os.path.exists(sf):
                with open(sf, 'r', encoding='utf-8') as f:
                    data = json.load(f)
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        # 模板合并：补全缺失字段
        tpl = Config._load_template()
        if tpl:
            merged = Config._merge(tpl, data)
            # 有补全 → 写回自愈（用户配置缺失字段被补上）
            if merged != data:
                try:
                    Config.save(merged)
                except Exception:
                    pass
            return merged
        return data

    @staticmethod
    def save(data: dict):
        """原子写 settings.json + 写前 .bak 备份 + 重试（防 Windows 文件锁丢配置）"""
        import time as _time
        sf = os.path.join(get_base_dir(), 'settings.json')
        tmp = sf + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # 写前备份现有配置（杀毒/云同步短暂锁定时可恢复）
        try:
            if os.path.exists(sf):
                with open(sf, 'r', encoding='utf-8') as _f:
                    _bak = _f.read()
                with open(sf + '.bak', 'w', encoding='utf-8') as _f:
                    _f.write(_bak)
        except Exception:
            pass
        # os.replace 原子替换；Windows 上目标被短暂锁定会抛 PermissionError → 重试 3 次
        for _attempt in range(3):
            try:
                os.replace(tmp, sf)
                return
            except OSError:
                if _attempt >= 2:
                    raise
                _time.sleep(0.2)

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
    实测耗时 ~0.7s，无需线程超时包装；窗口恢复由调用方主线程 after 负责。
    v1.4 修复：优先 PrintWindow 后台截图（借鉴 March7thAssistant screenshot.py）——
    不抢焦点、窗口被遮挡也能截到内容；失败才回退前台截图。
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
        # 窗口选择（v1.4 全量审查修复）：
        # 1) 优先标题含「拼多多/pinduoduo」的窗口（商家后台标签激活时窗口标题带站点名）
        # 2) 没有 → 所有浏览器窗口中选「当前激活」的那个（用户刚在看的就是 PDD 页面）
        # 3) 再没有 → 第一个浏览器窗口（多窗口时可能有偏差，但比截错窗口好）
        # 旧逻辑直接 wins[0]：用户开多个 Edge/Chrome 窗口时可能截到别的网站窗口。
        def _pick_window(titles):
            for title in titles:
                wins = gw.getWindowsWithTitle(title)
                if not wins:
                    continue
                if '拼多多' in title or 'pinduoduo' in title.lower():
                    return wins[0]  # 精确站点名优先
                for w in wins:      # 浏览器窗口：优先当前激活的
                    try:
                        if w.isActive:
                            return w
                    except Exception:
                        pass
                return wins[0]
            return None
        win = _pick_window(['拼多多', 'pinduoduo', 'Microsoft Edge', 'Edge', 'Chrome', 'Firefox'])
        if win is not None:
            found_window = True
            win_left, win_top = win.left, win.top
            # 1) 优先后台截图（PrintWindow）：不抢焦点、不遮挡、窗口被盖住也能截
            try:
                img = _capture_window_background(win)
            except Exception:
                img = None
            if img is not None:
                # v1.4 修复：PrintWindow 截的是**客户区**（不含标题栏/边框），
                # 偏移必须用客户区左上角的全屏坐标（ClientToScreen）——外框坐标
                # win.left/top 含边框/标题栏，非最大化窗口会系统性偏左上，
                # 客户反馈"AI 定位后点击查询按钮偏左"即此因（本机测试窗口
                # 最大化时外框≈客户区，偏差被掩盖）。DPI 感知进程返回物理像素。
                try:
                    _co = _client_origin(win)
                    if _co:
                        win_left, win_top = _co[0], _co[1]
                except Exception:
                    pass  # 客户区坐标失败则保留外框坐标（近似，最大化时无差）
            if img is None:
                # 2) 后台失败 → 前台截图（激活窗口，pyautogui region）
                if win.isMinimized:
                    win.restore()
                win.activate()
                _time.sleep(0.2)
                img = pg.screenshot(region=(win.left, win.top, win.width, win.height))
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
    # 截图缩放系数：AI 返回的是保存后图上的坐标（宽 ≤2560），
    # 调用方要把坐标还原到原始窗口/全屏像素（4K/带鱼屏必须，v1.4 审查修复）
    if isinstance(out_window_pos, dict):
        _saved_w = img.size[0]
        out_window_pos['scale_x'] = (cw / _saved_w) if _saved_w else 1.0
        out_window_pos['scale_y'] = (ch / img.size[1]) if img.size[1] else 1.0
    return found_window


def _client_origin(win) -> tuple:
    """窗口客户区左上角的全屏坐标（物理像素）。

    PrintWindow 截的是客户区（不含标题栏/边框），坐标换算偏移必须用客户区
    起点而非窗口外框 win.left/top——非最大化窗口两者差一个边框/标题栏，
    用外框会导致点击系统性偏左上（v1.4 修复：客户反馈查询按钮点击偏左）。
    DPI 感知进程下返回物理像素，与 pyautogui/pygetwindow 一致。
    """
    import ctypes
    from ctypes import wintypes
    try:
        hwnd = win._hWnd if hasattr(win, '_hWnd') else None
        if not hwnd:
            return None
        user32 = ctypes.windll.user32
        pt = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            return None
        return int(pt.x), int(pt.y)
    except Exception:
        return None


def _capture_window_background(win) -> object:
    """PrintWindow 后台截图（借鉴 March7thAssistant capture_window_background）。
    纯 ctypes 实现，零 pywin32 依赖——保证增量包（仅 exe+updater）对旧 v1.4 客户可用
    （旧版 _internal 无 win32，引入 pywin32 会导致旧客户升级后 ImportError 崩溃）。
    不激活窗口、不抢焦点、窗口被其他窗口遮挡时仍能截到内容。
    返回 PIL Image；失败返回 None（调用方回退前台截图）。
    flag=3：强制完整渲染 + 只抓客户区（不含标题栏/边框）。
    """
    import ctypes
    from ctypes import wintypes
    from PIL import Image as PILImage
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    kernel32 = ctypes.windll.kernel32

    hwnd = win._hWnd if hasattr(win, '_hWnd') else None
    if not hwnd:
        return None

    # 最小化窗口无法后台截图，回退（调用方会 restore + 前台）
    if win.isMinimized:
        return None

    # 客户区尺寸（不含标题栏边框）
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None

    # 设备上下文链：窗口 DC → 兼容 DC → 位图（每资源独立释放，防异常路径泄漏 GDI）
    hwndDC = memDC = None
    hBitmap = None
    try:
        hwndDC = user32.GetWindowDC(hwnd)
        if not hwndDC:
            return None
        memDC = gdi32.CreateCompatibleDC(hwndDC)
        if not memDC:
            return None
        hBitmap = gdi32.CreateCompatibleBitmap(hwndDC, width, height)
        if not hBitmap:
            return None
        gdi32.SelectObject(memDC, hBitmap)

        # PrintWindow flag=3：强制完整渲染 + 客户区
        result = user32.PrintWindow(hwnd, memDC, 3)
        if result != 1:
            return None

        # 位图 → PIL（BGRX raw：CreateCompatibleBitmap 是 32bpp BGRA，去掉 alpha）
        # ctypes.wintypes 无 BITMAPINFO，手动定义（GDI 标准结构）
        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ('biSize', wintypes.DWORD),
                ('biWidth', wintypes.LONG),
                ('biHeight', wintypes.LONG),
                ('biPlanes', wintypes.WORD),
                ('biBitCount', wintypes.WORD),
                ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD),
                ('biXPelsPerMeter', wintypes.LONG),
                ('biYPelsPerMeter', wintypes.LONG),
                ('biClrUsed', wintypes.DWORD),
                ('biClrImportant', wintypes.DWORD),
            ]
        class BITMAPINFO(ctypes.Structure):
            _fields_ = [('bmiHeader', BITMAPINFOHEADER)]

        bmpinfo = BITMAPINFO()
        bmpinfo.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmpinfo.bmiHeader.biWidth = width
        bmpinfo.bmiHeader.biHeight = -height  # 负值 = 自上而下（顶行在前）
        bmpinfo.bmiHeader.biPlanes = 1
        bmpinfo.bmiHeader.biBitCount = 32
        bmpinfo.bmiHeader.biCompression = 0  # BI_RGB
        buf = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetDIBits(hwndDC, hBitmap, 0, height, buf,
                        ctypes.byref(bmpinfo), 0)
        img = PILImage.frombuffer('RGB', (width, height),
                                  buf.raw, 'raw', 'BGRX', 0, 1)
        return img
    except Exception:
        return None
    finally:
        # 逆序释放：位图 → 兼容 DC → 窗口 DC（各自独立 try，防连锁）
        if hBitmap:
            try:
                gdi32.DeleteObject(hBitmap)
            except Exception:
                pass
        if memDC:
            try:
                gdi32.DeleteDC(memDC)
            except Exception:
                pass
        if hwndDC:
            try:
                user32.ReleaseDC(hwnd, hwndDC)
            except Exception:
                pass
