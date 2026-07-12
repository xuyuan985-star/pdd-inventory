"""API Keys — AES-256 encrypted, key split across 3 modules."""
import os, sys, json, base64

# -- 密钥碎片 1/3 --
_K1 = 'n2PaH3Y2VSAsqWQ='

# 从另外两个模块收集碎片
from dpi_utils import _K2
from gui import _K3

def _get_master_key():
    """从 3 个碎片重建 AES 密钥。"""
    raw = base64.b64decode(_K1) + base64.b64decode(_K2) + base64.b64decode(_K3)
    return raw

def _load_keys():
    """解密 keys.enc 返回 dict。"""
    # 1. 优先环境变量
    env_map = {'zhipu': 'ZHIPU_API_KEY', 'ark': 'ARK_API_KEY', 'qwen': 'DASHSCOPE_API_KEY'}
    result = {}
    for service, var in env_map.items():
        val = os.environ.get(var, '')
        if val:
            result[service] = val
    
    # 2. 解密 keys.enc
    import os as _os
    kf = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'keys.enc')
    if _os.path.exists(kf):
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend
            with open(kf, 'r') as f:
                data = base64.b64decode(f.read().strip())
            iv, ct = data[:16], data[16:]
            cipher = Cipher(algorithms.AES(_get_master_key()), modes.CBC(iv), backend=default_backend())
            decryptor = cipher.decryptor()
            pt = decryptor.update(ct) + decryptor.finalize()
            # 去填充
            pad = pt[-1]
            pt = pt[:-pad]
            decrypted = json.loads(pt.decode())
            for k, v in decrypted.items():
                if k not in result:
                    result[k] = v
        except Exception:
            pass
    
    return result

_keys = _load_keys()

def get_key(service):
    """获取指定服务的 API Key。"""
    return _keys.get(service, '')
