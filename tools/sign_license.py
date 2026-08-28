"""PDD EZ License 签发工具（开发者侧手动运行）。

铁律：
- 私钥只在此脚本里生成或使用，**绝不**入库、**绝不**入包
- 私钥保存到 keys.json（已在 .gitignore），开发者自行妥善保管
- 公钥可放入 auth/ed25519_verify.py 的 _PUBKEY 常量（已硬编码为占位 32 字节零）

用法：
    python tools/sign_license.py genkey        # 生成新密钥对到 keys.json
    python tools/sign_license.py sign <fingerprint> <tier> <days>  # 签发 license
    python tools/sign_license.py verify <license_text>              # 验证 license

签发 license 文本格式：
    <base64(json_payload)>.<hex(signature)>
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time

# === 配置 ===
KEYS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys.json")
PUBKEY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pubkey.hex")


def _ensure_crypto():
    """签发 license 需要 Ed25519 sign；本地开发可用 cryptography 库（仅 dev 工具，不入包）。"""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        return Ed25519PrivateKey, serialization
    except ImportError:
        print("签发需要 cryptography 库（开发期依赖，不入仓不入包）", file=sys.stderr)
        print("安装: pip install cryptography", file=sys.stderr)
        sys.exit(1)


def genkey():
    """生成新的 Ed25519 密钥对，私钥存到 keys.json，公钥输出到 stdout。"""
    Ed25519PrivateKey, serialization = _ensure_crypto()
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()

    # 私钥：32 字节原始 + 公钥（64 字节 PKCS8 也行；这里直接保存 expanded 形式 64 字节便于 back up）
    raw_sk = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw_pk = pk.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    keys = {"private_hex": raw_sk.hex(), "public_hex": raw_pk.hex()}
    with open(KEYS_PATH, "w", encoding="utf-8") as fp:
        json.dump(keys, fp, indent=2, ensure_ascii=False)
    print(f"✓ 密钥对已生成 → {KEYS_PATH}（已在 .gitignore）")
    print(f"✓ 公钥（32 字节 hex）→ {raw_pk.hex()}")
    print(f"\n请将公钥粘贴到 auth/ed25519_verify.py 的 _PUBKEY 常量并删除旧的 32 字节零占位。")
    print(f"私钥请妥善保管，建议备份到独立密码管理器；不要提交到 git。")


def _load_keys():
    if not os.path.exists(KEYS_PATH):
        print(f"未找到 {KEYS_PATH}，请先运行 genkey", file=sys.stderr)
        sys.exit(1)
    with open(KEYS_PATH, "r", encoding="utf-8") as fp:
        return json.load(fp)


def sign_license(fingerprint: str, tier: str, days: int = 365):
    """签发 license 文本，输出 <base64(json)>.<hex(sig)>。"""
    Ed25519PrivateKey, serialization = _ensure_crypto()
    keys = _load_keys()
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(keys["private_hex"]))

    payload = {
        "fingerprint": fingerprint,
        "tier": tier,
        "issued_at": int(time.time()),
        "expire_at": int(time.time()) + days * 86400,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sig = sk.sign(raw)
    b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    license_text = f"{b64}.{sig.hex()}"
    print(f"=== License Text ===")
    print(license_text)
    print(f"\n=== 解析 ===")
    print(f"fingerprint: {fingerprint}")
    print(f"tier: {tier}")
    print(f"expire_at: {payload['expire_at']} (≈{days} days)")
    print(f"issued_at: {payload['issued_at']}")
    print(f"\n=== 私钥已保存在 {KEYS_PATH}（gitignored），公钥需粘贴到 auth/ed25519_verify.py ===")


def verify_license_text(license_text: str):
    """验证 license 文本（使用我们自己的 verify 路径）。"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from auth.license import verify_license as _v
    result = _v(license_text)
    if result:
        print(f"✓ License 有效: tier={result.get('tier')}, expire_at={result.get('expire_at')}")
    else:
        print(f"✗ License 无效（验签失败/已过期/指纹不匹配）")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "genkey":
        genkey()
    elif cmd == "sign":
        if len(sys.argv) != 5:
            print("用法: python tools/sign_license.py sign <fingerprint> <tier> <days>", file=sys.stderr)
            sys.exit(1)
        sign_license(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    elif cmd == "verify":
        if len(sys.argv) != 3:
            print("用法: python tools/sign_license.py verify <license_text>", file=sys.stderr)
            sys.exit(1)
        verify_license_text(sys.argv[2])
    else:
        print(f"未知命令: {cmd}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
