"""PDD EZ 离线卡密授权验证（v1.5.0 引入）。

设计原则：
- 零第三方依赖（仅 stdlib + 同目录的 ed25519_verify）
- 离线校验：无 license server，无网络
- 卡密载荷 JSON 格式，包含 {fingerprint, expire_at, tier, issued_at}
- 载荷用 Ed25519 私钥签名；公钥内嵌（仅 verify 路径在生产代码中触发）
- 机器指纹 = sha256(uuid.getnode() + platform.node() + USERNAME) 前 16 字节
- 失败安全：任何异常均返回 free tier（不阻塞任何现有功能）
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import time
import uuid
from typing import Any

from auth.ed25519_verify import verify as _ed25519_verify

# ============================================================
# 公钥（内嵌 32 字节 Ed25519 公钥；仅 verify 用）
# 私钥绝不入仓——签发由 tools/sign_license.py 在开发者侧手动完成
# ============================================================
# 默认公钥 = 32 字节零（占位）。正式发版前由开发者用 tools/sign_license.py 生成后回填。
# 任何签名在公钥未替换前都会被拒绝，verify_license 退回 free tier
_DEFAULT_PUBKEY_HEX = "00" * 32
_PUBKEY = bytes.fromhex(_DEFAULT_PUBKEY_HEX)

# tier 枚举
TIER_FREE = "free"
TIER_PRO = "pro"

# ============================================================
# t12 P2-C：Pro 门控阈值常量（仅在 enforce=true 且 tier=free 时生效）
# 用户裁定：默认全免；现有功能永久免费；仅新增高级功能门控
# ============================================================
FREE_DAILY_LIVE_SCREENSHOT = 50  # 免费版实时截图识别次数/日
FREE_HISTORY_DAYS = 30           # 免费版历史趋势查看窗口（天）
UNLIMITED = 999999               # Pro 实际不限，用大整数表示

# 缓存：避免每次 get_tier() 都跑一遍 verify
# t24 修复包 A：缓存值改为 (tier, ts) 二元组；TTL=300s 后强制重验，
# 修复 BUG-1（enforce 热切换/外部改 settings.json 后缓存陈旧至重启）。
# 注意：缓存值仍可被 reset_cache() 显式清空（外部主动写盘路径调用）
_CACHE_TTL_SECONDS = 300
_CACHE: dict[str, tuple] = {}


def get_machine_fingerprint() -> str:
    """机器指纹 = sha256(uuid.getnode() + platform.node() + USERNAME) 前 16 字节 hex。

    16 字节 = 128 bit，足够避免一般碰撞；不进注册表/磁盘，重启后稳定（同设备同用户不变）。
    """
    try:
        parts = [
            str(uuid.getnode()),
            platform.node() or "",
            os.environ.get("USERNAME", "") or os.environ.get("USER", ""),
        ]
        h = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
        return h[:16].hex()
    except Exception:
        # 极端失败（极罕见），退化为常量指纹（仍可通过 verify 验签，但绑不到机器）
        return "00" * 16


def _decode_license(license_text: str) -> dict | None:
    """license_text = base64(json) + '.' + base64(sig_64bytes)

    解析失败返回 None。
    """
    try:
        if not license_text or "." not in license_text:
            return None
        payload_b64, sig_b64 = license_text.rsplit(".", 1)
        # payload 是标准 base64；sig 是 64 字节 Ed25519 签名，直接 hex 编码
        import base64
        payload = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        sig = bytes.fromhex(sig_b64)
        if len(sig) != 64:
            return None
        obj = json.loads(payload.decode("utf-8"))
        return {"payload": obj, "sig": sig, "raw_payload": payload}
    except Exception:
        return None


def verify_license(license_text: str) -> dict | None:
    """验证 license 文本。成功返回 license dict（{fingerprint, expire_at, tier, issued_at, _valid: True}）；失败返回 None。

    校验顺序：
    1. 解析格式
    2. Ed25519 验签（公钥内嵌）
    3. 检查 expire_at（Unix 时间戳，未过期）
    4. 检查 fingerprint（若提供则需匹配本机）

    任何步骤失败 → 返回 None（不抛异常）
    """
    if not license_text or not isinstance(license_text, str):
        return None
    decoded = _decode_license(license_text)
    if not decoded:
        return None
    payload = decoded["payload"]
    sig = decoded["sig"]
    raw = decoded["raw_payload"]
    if not isinstance(payload, dict):
        return None

    # 2. Ed25519 验签
    try:
        if not _ed25519_verify(_PUBKEY, raw, sig):
            return None
    except Exception:
        return None

    # 3. 过期检查
    try:
        expire_at = int(payload.get("expire_at", 0))
        if expire_at > 0 and time.time() > expire_at:
            return None  # 已过期
    except Exception:
        return None

    # 4. 指纹检查（若 payload 提供）
    fp = payload.get("fingerprint", "")
    if fp and isinstance(fp, str) and len(fp) >= 16:
        if fp.lower() != get_machine_fingerprint().lower():
            return None  # 机器不匹配

    # 校验通过
    out = dict(payload)
    out["_valid"] = True
    return out


def get_tier(license_text: str = "", enforce: bool = False) -> str:
    """查询当前 tier。返回 'free' 或 'pro'。

    enforce=False → 恒返回 'pro'（v1.5.0 默认全免，所有 Pro 功能默认开放，
                      即便没有 license 也不限制——本任务硬约束：本任务绝不限制任何现有行为）
    enforce=True  → 按 license 实际校验：有效且非 free → 'pro'；否则 'free'

    缓存（t24 修复包 A）：_CACHE[cache_key] = (tier, ts)；命中时若 ts 距今
    超过 _CACHE_TTL_SECONDS（默认 300s）则强制重验。修复 BUG-1 实质：
    外部改 settings.json 后缓存陈旧至重启的窗口从"无限"缩到 5 分钟。
    """
    if not enforce:
        return TIER_PRO
    if not license_text:
        return TIER_FREE
    # 缓存命中（带 TTL）
    cache_key = f"{enforce}:{license_text}"
    if cache_key in _CACHE:
        cached_tier, cached_ts = _CACHE[cache_key]
        try:
            if time.time() - cached_ts < _CACHE_TTL_SECONDS:
                return cached_tier
        except Exception:
            # time.time() 异常（极罕见）→ 走重验路径
            pass
    # 实际校验
    lic = verify_license(license_text)
    tier = TIER_PRO if lic and lic.get("tier") == TIER_PRO else TIER_FREE
    _CACHE[cache_key] = (tier, time.time())
    return tier


def is_pro(license_text: str = "", enforce: bool = False) -> bool:
    """t12 P2-C：是否 Pro 用户。enforce=false 时恒 True（默认全免）。"""
    return get_tier(license_text, enforce) == TIER_PRO


def get_license_info(license_text: str = "", enforce: bool = False) -> dict:
    """t12 P2-C：返回 {tier, expire_at, is_pro, enforce, status_text} 供设置面板显示。

    expire_at：Unix 时间戳；若未提供或非数值则为 0。
    status_text：中文显示文案（"试用期：所有限制未启用" / "Pro 到期 YYYY-MM-DD" / "免费版" 等）。
    """
    tier = get_tier(license_text, enforce)
    if not enforce:
        return {
            "tier": tier,
            "is_pro": True,
            "enforce": False,
            "expire_at": 0,
            "status_text": "试用期：所有限制未启用（enforce=false）",
        }
    # enforce=true
    if not license_text:
        return {
            "tier": TIER_FREE,
            "is_pro": False,
            "enforce": True,
            "expire_at": 0,
            "status_text": "免费版（无 license）",
        }
    lic = verify_license(license_text)
    if lic:
        try:
            from datetime import datetime
            exp = int(lic.get("expire_at", 0) or 0)
            exp_str = datetime.fromtimestamp(exp).strftime("%Y-%m-%d") if exp else "永久"
        except Exception:
            exp_str = "未知"
        return {
            "tier": lic.get("tier", TIER_FREE),
            "is_pro": lic.get("tier") == TIER_PRO,
            "enforce": True,
            "expire_at": int(lic.get("expire_at", 0) or 0),
            "status_text": f"{lic.get('tier', '?')} 到期 {exp_str}",
        }
    return {
        "tier": TIER_FREE,
        "is_pro": False,
        "enforce": True,
        "expire_at": 0,
        "status_text": "免费版（license 无效或已过期）",
    }


def check_live_quota(used_today: int, license_text: str = "", enforce: bool = False) -> dict:
    """t12 P2-C：实时截图识别次数门控检查。

    返回 {allowed: bool, limit: int, used: int, remaining: int, reason: str}

    enforce=False 或 is_pro=True → 恒 allowed=True，limit=UNLIMITED。
    enforce=True + tier=free + used_today >= FREE_DAILY_LIVE_SCREENSHOT → allowed=False。
    """
    if not enforce or is_pro(license_text, enforce):
        return {
            "allowed": True,
            "limit": UNLIMITED,
            "used": int(used_today or 0),
            "remaining": UNLIMITED,
            "reason": "pro_or_trial",
        }
    used = max(0, int(used_today or 0))
    limit = FREE_DAILY_LIVE_SCREENSHOT
    remaining = max(0, limit - used)
    if used >= limit:
        return {
            "allowed": False,
            "limit": limit,
            "used": used,
            "remaining": 0,
            "reason": f"免费版每日 {limit} 次实时截图识别上限已用完，请升级 Pro 或明日再试",
        }
    return {
        "allowed": True,
        "limit": limit,
        "used": used,
        "remaining": remaining,
        "reason": "ok",
    }


def get_history_days_limit(license_text: str = "", enforce: bool = False) -> int:
    """t12 P2-C：历史趋势查询窗口（天）。

    enforce=False 或 is_pro=True → UNLIMITED（999999）。
    enforce=True + tier=free → FREE_HISTORY_DAYS（30）。
    """
    if not enforce or is_pro(license_text, enforce):
        return UNLIMITED
    return FREE_HISTORY_DAYS


def reset_cache() -> None:
    """清空缓存（开发态/测试态用）。"""
    _CACHE.clear()
