# Remote Utility 发现摘要

## 之前漏掉的远程工具套件！
RS-BA1 安装包实际上安装了**两套软件**：
1. **RS-BA1/RemoteController** — 遥控主程序（我们一直分析的）
2. **RemoteUtility** — 远程辅助工具（之前完全漏了！）

## RemoteUtility 文件

| 文件 | 大小 | 功能 |
|------|------|------|
| RemoteUty.exe | 3.0 MB | 远程工具主程序 — Delphi, UDP音频流+串口遥控 |
| RadioSch.dll | 1.9 MB | 无线电调度/频谱DLL — USB设备发现、频谱显示 |
| UtilityCk.dll | 200 KB | 许可证检查 — 同RS-BA1V2Ck.dll类似 |
| english.dll | 812 KB | 英文资源DLL（语言包） |
| models.ini | 1.1 KB | 21款机型表，含IC-705 (115200波特率!) |
| RadioSch.ini | 349 B | USB设备VID/PID配置 |
| RemoteUty.dat | 17 B | 版本文件 |

## 关键发现: IC-705 波特率差异
- RemoteController (RS-BA1): **19200** (IC-705.ini BAUD=7)
- RemoteUtility: **115200** (models.ini BAUD=115200)
- 说明遥控和远程工具用不同波特率连接IC-705

## VirtualDriver
包含虚拟声卡驱动(icom_vaudio)和虚拟串口驱动(icom_vserial)，
用于网络音频传输和远程串口转发。

