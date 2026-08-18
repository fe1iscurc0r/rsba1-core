"""mailslot — Windows Mailslot 客户端 (RemoteController -> RemoteUtility IPC).

包定位:
    模拟 Icom RS-BA1 V2 中 RemoteController 通过 UtyCtrl.dll 写 Mailslot
    通知本机 RemoteUtility 进程的"第二 RemoteController" MVP。即: 不依赖
    RemoteController.exe / UtyCtrl.dll, 直接由 Python 进程把命令包写入
    RemoteUtility 已经在监听的 Mailslot, 让本机 RemoteUtility 误以为收到
    了来自真实 RemoteController 的指令。

子模块:
    protocol : 命令包序列化/反序列化 + 9 命令码常量
    client   : MailslotClient 类 (pywin32 主路径 + ctypes 后备路径)

参考逆向资料 (本仓库):
    - phase2-re/notes/pe_analysis/UtyCtrl_deep_analysis.md
        * Mailslot 名 \\.\mailslot\\RemoteUtyCtrlCmd (RemoteUtility 创建/读)
        * 响应 Mailslot 名 \\.\mailslot\\RemoteUtyCtrlRes (UtyCtrl 创建/读)
        * Mutex 名 "Icom RemoteUtyCtrl" (5000ms 超时)
        * 命令包格式: {cmd_code:1, data_len:1, reserved:2, payload[data_len]}
        * 9 个 cmd_code (0..8) 与每个 Get*/ExecCmd 导出函数对应
"""

from rsba1.mailslot import protocol  # noqa: F401  (re-export)
from rsba1.mailslot.protocol import (
    COMMAND_HEADER_FORMAT,
    COMMAND_HEADER_SIZE,
    MAX_PAYLOAD_SIZE,
    serialize_command,
    deserialize_command,
)
from rsba1.mailslot.client import MailslotClient, MailslotError

__all__ = [
    "protocol",
    "COMMAND_HEADER_FORMAT",
    "COMMAND_HEADER_SIZE",
    "MAX_PAYLOAD_SIZE",
    "serialize_command",
    "deserialize_command",
    "MailslotClient",
    "MailslotError",
]
