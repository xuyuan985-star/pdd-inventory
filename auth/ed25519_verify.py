"""Pure-Python Ed25519 verify (RFC 8032). No third-party deps. Verified against RFC 8032 §7.1 test vectors.

仅做 verify。签名（sign）由 tools/sign_license.py 在开发者侧手动调用，
私钥不入包不入仓。

实现策略：使用仿射坐标（affine）做点加法，标量乘用 double-and-add。
对单次 verify 而言速度够用（~100-300ms/次），代码量小、正确性易于验证。
公式参考：RFC 8032 + 经典 twisted Edwards 加法（a=-1 完整加法形式）。

零第三方依赖，仅 stdlib hashlib。
"""
from __future__ import annotations

import hashlib

# === Field parameters ===
_P = (1 << 255) - 19  # 域阶
_L = (1 << 252) + 27742317777372353535851937790883648493  # 曲线阶
# Ed25519 曲线参数 d（a=-1 twisted Edwards 形式，ristretto255 §2.1 标准值）
_D = 37095705934669439343138083508754565189542113879843219016388785533085940283555
# 基点 B：y = 4/5 mod p；x 是满足曲线 -x²+y²=1+dx²y² 的"sign=0 解"（末位偶数）
_BASE_POINT_X = 15112221349535400772501151409588531511454012693041857206046113283949847762202
_BASE_POINT_Y = 46316835694926478169428394003475163141307993866256225615783033603165251855960


# ============================================================
# Modular arithmetic
# ============================================================
def _modp_inv(x: int) -> int:
    """Fermat 模逆。"""
    return pow(x, _P - 2, _P)


# ============================================================
# Affine point operations (x, y) on Ed25519
# Curve: -x² + y² = 1 + d*x²*y²  (mod p)
# ============================================================
def _point_add(P, Q):
    """仿射点加法 P + Q（Ed25519 twisted Edwards a=-1 形式：-x² + y² = 1 + d·x²·y²）。

    公式（Bernstein/Lange 2007，a=-1 twisted Edwards 完整加法）：
        denom = d·x1·x2·y1·y2
        x3 = (x1·y2 + y1·x2) / (1 + denom)
        y3 = (y1·y2 + x1·x2) / (1 - denom)

    注意 y3 公式里 a=-1 用了 +x1·x2（普通 Edwards 公式是 -a*x1*x2 = -x1*x2 for a=1，
    但 Ed25519 a=-1 所以是 +x1·x2）。
    """
    x1, y1 = P
    x2, y2 = Q
    denom = (_D * x1 * x2 * y1 * y2) % _P
    num_x = (x1 * y2 + y1 * x2) % _P
    num_y = (y1 * y2 + x1 * x2) % _P
    inv_denom_x = _modp_inv((1 + denom) % _P)
    inv_denom_y = _modp_inv((1 - denom) % _P)
    x3 = (num_x * inv_denom_x) % _P
    y3 = (num_y * inv_denom_y) % _P
    return (x3, y3)


def _point_neg(P):
    """仿射点取负 -P。

    For twisted Edwards a*x^2 + y^2 = 1 + d*x^2*y^2, the additive inverse is
    -(x, y) = (-x, y). (For a=1 Edwards, it's (x, -y), but Ed25519 is a=-1.)
    """
    x, y = P
    return ((-x) % _P, y)


def _point_double(P):
    """仿射倍点 2P。"""
    return _point_add(P, P)


def _scalar_mult(n: int, P):
    """标量乘 nP（double-and-add, left-to-right）。"""
    if n == 0:
        return (0, 1)  # identity
    Q = (0, 1)  # identity
    addend = P
    while n > 0:
        if n & 1:
            Q = _point_add(Q, addend)
        addend = _point_double(addend)
        n >>= 1
    return Q


# ============================================================
# Point encoding/decoding (RFC 8032 §5.1.2)
# ============================================================
def _point_decode(s: bytes):
    """32-byte 压缩点 → (x, y) 仿射坐标（Ed25519 twisted Edwards a=-1 形式）。

    编码：低 255 位是 y，最高位（32 字节最高位）是 x 的符号位。
    曲线方程 -x² + y² = 1 + d·x²·y²
    → y² - 1 = x²(1 + d·y²)
    → x² = (y² - 1) / (1 + d·y²)
    """
    if len(s) != 32:
        raise ValueError("ed25519: public key must be 32 bytes")
    y = int.from_bytes(s, 'little') & ((1 << 255) - 1)
    sign = (s[31] >> 7) & 1
    y2 = (y * y) % _P
    denom = (1 + _D * y2) % _P
    x2 = ((y2 - 1) * _modp_inv(denom)) % _P
    if x2 == 0:
        if sign:
            raise ValueError("ed25519: invalid encoding (sign=1, x=0)")
        return (0, y)
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x) % _P != x2:
        SQRTM1 = pow(2, (_P - 1) // 4, _P)
        x = (x * SQRTM1) % _P
        if (x * x) % _P != x2:
            raise ValueError("ed25519: invalid x² (no sqrt)")
    if (x & 1) != sign:
        x = _P - x
    return (x, y)


def _point_encode(P):
    """仿射 (x, y) → 32 字节压缩编码。"""
    x, y = P
    raw = y.to_bytes(32, 'little')
    if x & 1:
        ba = bytearray(raw)
        ba[31] |= 0x80
        raw = bytes(ba)
    return raw


# ============================================================
# Verify (RFC 8032 §5.1.6)
# ============================================================
def verify(public_key: bytes, msg: bytes, signature: bytes) -> bool:
    """Ed25519 verify。返回 True=有效，False=无效。"""
    if len(public_key) != 32:
        return False
    if len(signature) != 64:
        return False

    try:
        A = _point_decode(public_key)
    except Exception:
        return False

    R_bytes = signature[:32]
    S = int.from_bytes(signature[32:], 'little')
    if S >= _L:
        return False

    try:
        R = _point_decode(R_bytes)
    except Exception:
        return False

    # h = SHA-512(R || A || M) mod L
    h = hashlib.sha512(R_bytes + public_key + msg).digest()
    h_int = int.from_bytes(h, 'little') % _L

    # [S]B == R + [h]A
    # 即 R == [S]B - [h]A == [S]B + (-[h]A)
    B = (_BASE_POINT_X, _BASE_POINT_Y)
    SB = _scalar_mult(S, B)
    hA = _scalar_mult(h_int, A)
    neg_hA = _point_neg(hA)
    P = _point_add(SB, neg_hA)

    return P == R


# ============================================================
# RFC 8032 §7.1 测试向量（供 test_smoke.TestLicense 使用）
# ============================================================
# TEST 1: 空消息
TEST1_PK = bytes.fromhex(
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
)
TEST1_MSG = b""
TEST1_SIG = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
    "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)

# TEST 2: 1-byte 消息
TEST2_PK = bytes.fromhex(
    "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"
)
TEST2_MSG = b"\x72"
TEST2_SIG = bytes.fromhex(
    "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
    "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
)
