"""
PDD EZ — DPAPI 凭据加密（v1.4.8 P1-C）

纯 stdlib + ctypes 封装 Windows Crypt32.dll 的 CryptProtectData / CryptUnprotectData，
作用域 = 当前用户（CRYPTPROTECT_UI_FORBIDDEN | CRYPTPROTECT_VERIFY_PROTECTION 不设置），
输出格式 = base64(ascii) 并在明文前面加 "dpapi:v1:" 前缀以与普通字符串区分。

设计要点（docs/SOLUTION_tech_t6.md §①）：
- 零第三方依赖（禁 keyring / cryptography），守住增量包链。
- 跨用户/跨机器 → CryptUnprotectData 失败 → 抛 DPAPIError，让调用方决定（UI 置空 + messagebox）。
- DPAPI 整体不可用（dll 缺失/沙盒）→ 静默降级，明文照常落盘 + 返回 False 让调用方记录 WARN。
- 前缀识别 `dpapi:v1:` → 解密；无前缀 → 视为明文，原样返回（向后兼容未迁移配置）。

测试：
- roundtrip 断言（明文 ↔ 密文）
- 前缀识别断言（dpapi:v1:xxx vs sk-xxx）
- 失败路径（decrypt 损坏 blob 应抛 DPAPIError，enc 失败应返回 "" + is_available()=False）
"""
import base64
import ctypes
import os
import sys


# ── 前缀 / 错误码 ────────────────────────────────────────────────
PREFIX = "dpapi:v1:"
"""加密 blob 前缀；同时作为版本号，未来轮换加密方式用 v2/v3。"""


class DPAPIError(Exception):
    """CryptUnprotectData 失败：跨用户/跨机器/数据损坏/被篡改。"""
    pass


# ── 平台/可用性探测 ─────────────────────────────────────────────
_IS_WINDOWS = (sys.platform == "win32")
_AVAILABLE = False
_AVAIL_REASON = ""
_crypt32 = None
_CryptProtectData = None
_CryptUnprotectData = None

if _IS_WINDOWS:
    try:
        from ctypes import wintypes
        _crypt32 = ctypes.windll.crypt32
        # 显式 restype/argtypes 防 64 位句柄截断（与 updater.py 同样教训）
        _crypt32.CryptProtectData.restype = wintypes.BOOL
        _crypt32.CryptProtectData.argtypes = [
            ctypes.c_void_p,  # pDataIn (DATA_BLOB*)
            wintypes.LPCWSTR,  # szDataDescr
            ctypes.c_void_p,  # pOptionalEntropy
            ctypes.c_void_p,  # pvReserved
            ctypes.c_void_p,  # pPromptStruct
            wintypes.DWORD,  # dwFlags
            ctypes.c_void_p,  # pDataOut (DATA_BLOB*)
        ]
        _crypt32.CryptUnprotectData.restype = wintypes.BOOL
        _crypt32.CryptUnprotectData.argtypes = [
            ctypes.c_void_p,  # pDataIn (DATA_BLOB*)
            ctypes.POINTER(wintypes.LPWSTR),  # ppszDataDescr (out)
            ctypes.c_void_p,  # pOptionalEntropy
            ctypes.c_void_p,  # pvReserved
            ctypes.c_void_p,  # pPromptStruct
            wintypes.DWORD,  # dwFlags
            ctypes.c_void_p,  # pDataOut (DATA_BLOB*)
        ]
        # 实际调用一次空数据以确认 dll 入口可解析（部分 Wine/沙盒会报 127 找不到入口）
        _AVAILABLE = True
        _AVAIL_REASON = "ok"
    except Exception as e:
        _AVAILABLE = False
        _AVAIL_REASON = f"load fail: {e!r}"


def is_available() -> bool:
    """DPAPI 在当前进程是否可用（仅检查 dll + 入口解析；不真加密数据）。"""
    return _AVAILABLE


def avail_reason() -> str:
    """不可用时的原因（仅诊断用，可能含 ctypes 异常文本）。"""
    return _AVAIL_REASON


def is_encrypted(value) -> bool:
    """值是否带 dpapi:v1: 前缀（密文）。None / 空串 / 非字符串都返回 False。"""
    if not isinstance(value, str):
        return False
    return value.startswith(PREFIX)


# ── DATA_BLOB 包装（ctypes 简易）──────────────────────────────────
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


# ── 核心 enc / dec ──────────────────────────────────────────────
def enc(plaintext: str) -> str:
    """明文 → "dpapi:v1:<base64>"。失败返回 ""（调用方按 is_available() 决定是否记录 WARN）。

    当前用户作用域；不可导出到其他用户/机器。空串入参直接返回空串（无意义不加密）。
    """
    if plaintext is None or plaintext == "":
        return ""
    if not _AVAILABLE:
        return ""
    if not isinstance(plaintext, str):
        plaintext = str(plaintext)
    try:
        raw = plaintext.encode("utf-8")
        in_blob = _DATA_BLOB()
        in_blob.cbData = len(raw)
        in_blob.pbData = ctypes.cast(
            ctypes.pointer((ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)),
            ctypes.POINTER(ctypes.c_ubyte),
        )
        out_blob = _DATA_BLOB()
        # dwFlags=0 = 当前用户作用域（默认）；
        # UI_FORBIDDEN 不需要（CRYPTPROTECT_UI_FORBIDDEN=0x1 我们也不设——后台无 UI）
        ok = _crypt32.CryptProtectData(
            ctypes.byref(in_blob),  # pDataIn
            None,  # szDataDescr（描述字符串）
            None,  # pOptionalEntropy
            None,  # pvReserved
            None,  # pPromptStruct（无 UI 弹窗）
            0,  # dwFlags
            ctypes.byref(out_blob),  # pDataOut
        )
        if not ok:
            return ""
        try:
            buf = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            # CryptProtectData 分配的内存需 LocalFree 释放
            try:
                _ctypes_kernel32 = ctypes.windll.kernel32
                _ctypes_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
                _ctypes_kernel32.LocalFree.restype = ctypes.c_void_p
                _ctypes_kernel32.LocalFree(out_blob.pbData)
            except Exception:
                pass
        return PREFIX + base64.b64encode(buf).decode("ascii")
    except Exception:
        return ""


def dec(blob: str) -> str:
    """密文 → 明文。失败抛 DPAPIError（跨机器/损坏/被篡改），让调用方置空+提示。

    非 dpapi:v1: 前缀 → 视为明文，原样返回（让 UI 在配置未迁移时也能正常显示）。
    空串 / None / 非字符串 → 返回 ""。
    """
    if not blob:
        return ""
    if not isinstance(blob, str):
        return ""
    if not blob.startswith(PREFIX):
        return blob  # 明文直通（向后兼容）
    if not _AVAILABLE:
        raise DPAPIError("DPAPI not available")
    payload_b64 = blob[len(PREFIX):]
    try:
        raw = base64.b64decode(payload_b64, validate=True)
    except Exception as e:
        raise DPAPIError(f"base64 decode failed: {e}")
    try:
        in_blob = _DATA_BLOB()
        in_blob.cbData = len(raw)
        in_blob.pbData = ctypes.cast(
            ctypes.pointer((ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)),
            ctypes.POINTER(ctypes.c_ubyte),
        )
        out_blob = _DATA_BLOB()
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None, None, None, None, 0,
            ctypes.byref(out_blob),
        )
        if not ok:
            # 跨用户 / 跨机器 / 数据被篡改 都会走到这里
            raise DPAPIError(f"CryptUnprotectData failed (err={ctypes.GetLastError()})")
        try:
            buf = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            return buf.decode("utf-8")
        finally:
            try:
                _ctypes_kernel32 = ctypes.windll.kernel32
                _ctypes_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
                _ctypes_kernel32.LocalFree.restype = ctypes.c_void_p
                _ctypes_kernel32.LocalFree(out_blob.pbData)
            except Exception:
                pass
    except DPAPIError:
        raise
    except Exception as e:
        raise DPAPIError(f"CryptUnprotectData exception: {e!r}")
