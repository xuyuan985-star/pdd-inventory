"""
PDD EZ — 公共工具函数
提供数据目录路径和设置读取，消除 main/ocr/gui 中的重复定义。
"""
import os, sys, json

VERSION = "v1.1"
EXE_NAME = "PDD EZ v1.1.exe"


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
        sf = os.path.join(get_base_dir(), 'settings.json')
        if os.path.exists(sf):
            with open(sf, 'r', encoding='utf-8') as f:
                s = json.load(f)
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
            # 写回
            s['api'] = new_api
            with open(sf, 'w', encoding='utf-8') as f:
                json.dump(s, f, ensure_ascii=False, indent=2)
            return new_api
    except (json.JSONDecodeError, IOError, OSError):
        pass
    return {}
