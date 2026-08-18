"""rsba1 — Icom RS-BA1 V2 逆向工程 Python 工具包.

包定位:
    本包提供对 RS-BA1 V2 关键 DLL (CivCtrl / UtyCtrl / HidCtrl / RS-BA1V2Ck 等)
    的 Python ctypes 包装、CI-V 协议构造/解析工具, 以及跨平台协议栈重写
    (UDP 三信道 + Mailslot IPC), 目标是不依赖 ICOM 二进制即可远程控制电台。

子包:
    ctypes_wrappers : 基于 ctypes 的 DLL 调用层 (civctrl 包装 + civ_commands CI-V 帧)
    serial           : UDP Serial 信道 (50002) 客户端 + Command 信道 (50001) 登录客户端
    mailslot         : Windows Mailslot IPC (UtyCtrl <-> RemoteUtility 命令协议) 客户端
"""

__version__ = "0.1.0"
