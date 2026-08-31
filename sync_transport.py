# !/usr/bin/env python
# -*- coding: utf-8 -*-
"""sync_transport.py — 云同步传输协议空壳（TC-C5 · v1.6.0 架构预留）。

本模块仅为多平台/云同步的**接口预留**（§5.1 TC-C5）：
- 不包含任何网络/加密/上传下载实现；
- 未被任何生产代码 import；
- v1.6.0 运行时零感知（零行为变更）。

设计依据：docs/SYNC_DESIGN.md §4（云同步预留边界）。
上云默认关闭 + 显式开启 + 服务端零知识；冲突策略在 GUI 层显式处置。

接入方（未来 v2.x）：实现 `SyncTransport` 的具体类（如 LocalEncryptedTransport /
HttpTransport），由 settings 开关控制，禁止静默默认开启。
"""
from typing import Protocol, runtime_checkable


@runtime_checkable
class SyncTransport(Protocol):
    """云同步传输契约（仅类型声明，无运行时行为）。

    - upload: 上传一个快照字节块（调用方负责加密与幂等标记，见 SYNC_DESIGN §4）
    - download: 下载最新快照字节块；无可用快照时抛 FileNotFoundError 语义异常
    - health: 返回 (ok: bool, detail: str)，供未来健康检查接入
    """

    def upload(self, snapshot: bytes) -> None: ...

    def download(self) -> bytes: ...

    def health(self) -> tuple[bool, str]: ...


def default_transport() -> SyncTransport:
    """返回未配置时的默认空传输（当前版本恒为 None 语义：不可用）。

    v1.6.0 固定返回 None——任何调用方收到 None 必须显式降级为「本地优先」
    并提示「云同步未启用」，绝不静默吞掉。
    """
    return None  # type: ignore[return-value]