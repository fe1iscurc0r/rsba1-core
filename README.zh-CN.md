# rsba1-core（中文版）

**Icom RS-BA1 V2 协议栈的纯 Python 实现** —— 通过网络控制 IC-705、IC-9700 及任何 CI-V 电台。无需任何 Icom 官方二进制文件。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-green.svg)](https://python.org)
[![CI](https://github.com/fe1iscurc0r/rsba1-core/actions/workflows/ci.yml/badge.svg)](https://github.com/fe1iscurc0r/rsba1-core/actions)

> English version: [README.md](README.md)

## 这是什么？

Icom 的 RS-BA1 远程控制软件**只能在 Windows 上运行且闭源**。本项目通过逆向工程还原其通信协议，提供一个**纯 Python 替代实现**——凡 Python 能跑的地方都能跑（Linux / macOS / Windows / 树莓派）。

**已在 IC-705 真机上完成端到端验证**（2026-08-18，见[真机验证报告](docs/真机验证报告.md)）。

## 快速开始

```bash
pip install "rsba1-core[mcp]"   # MCP server 需要 fastmcp；纯 CLI 用 pip install rsba1-core

export RADIO_HOST=192.168.0.31      # IC-705 的 IP（开远程服务器后）
export RADIO_USER=radio_user        # 电台里设置的远程 ID 用户名
export RADIO_PASSWORD=your_password

python -m rsba1.mcp read-freq       # 读频率
python -m rsba1.mcp set-freq 7074000  # 设频率（7.074 MHz，40m 段）
```

## 平台支持

| 平台 | 网络控制（radio-link） | 串口 | Mailslot / CivCtrl.dll |
|------|---------------------|------|------------------------|
| Linux   | ✅ 纯 Python UDP | ✅ `[serial]` | ❌ 仅 Windows |
| macOS   | ✅ 纯 Python UDP | ✅ `[serial]` | ❌ 仅 Windows |
| Windows | ✅ 纯 Python UDP | ✅ `[serial]` | ✅ 遗留后端 |

默认 `radio-link` 后端是**纯 Python UDP**——不需要 Icom 二进制、不需要 DLL、不需要 pywin32。Windows 专属后端（`--backend mailslot`、`CivCtrl.dll`）放在可选的 `[windows]` extras 里，并有平台守卫，保证在 Linux/macOS 上永远不会弄坏 import 或测试收集。串口支持需要 `pyserial`（`pip install "rsba1-core[serial]"`）。

## MCP Server（面向 AI Agent）

```bash
python -m rsba1.mcp   # 在 stdio 上启动 MCP server
```

可用工具：`read_freq`、`read_mode`、`read_smeter`、`set_freq`、`ptt`、`get_status`

这意味着你可以让 AI 助手（Claude / 其他 MCP 客户端）直接读电台状态、切换频率、控制 PTT——语音指令 → 频率切换的全链路。

## 架构

```
你的 AI Agent / CLI
        ↓
rsba1-core（纯 Python）
        ↓
IC-705 内置 RS-BA1 服务器（UDP 50001 + 50002）
```

核心原则：零 Windows API、零 Icom DLL、仅依赖 Python 3.11+。

## 功能状态

| 功能 | 状态 |
|------|------|
| CI-V 读（频率/模式/信号强度） | ✅ 真机验证通过 |
| CI-V 写（set_freq） | ✅ 真机验证通过 |
| PTT | ✅ 真机验证通过 |
| 音频流（UDP 50003） | ❌ 未实现（需进一步抓包分析） |

## 开发经历

| 阶段 | 时间 | 内容 |
|------|------|------|
| 协议逆向 | 2026-08 前 | 对本机安装的 RS-BA1 副本做私有逆向分析（Phase 0-4）：CI-V 帧格式（`FE FE TO FR DATA FD`）、5 字节 BCD 频率编码、UDP 50001/50002 认证与控制通道 |
| 跨平台实现 | 2026-08 | 从 Windows mailslot/CivCtrl.dll 遗留路径重写为纯 Python UDP 协议栈；pyserial + asyncio 串口支持 |
| 真机验证 | 2026-08-18 | IC-705 端到端闭环：认证 → 读频率 ×3 稳定 → set_freq 往返校验 0 Hz 差 → 模式读取（FM/滤波器）→ S-meter |
| 面板控制扩展 | 2026-08-25 | 补齐 IC-705 面板按键 CI-V 控制方法 + 业余波段白名单校验（防止误设业余段外频率）；MCP server 接入面板按键工具 |
| Linux 适配与收尾 | 2026-09-05 | pyproject 构建修复、civctrl 平台守卫、MCP 32 个工具补类型注解并清理示例凭据、CI Linux 管道修绿 |

## 文档

- [docs/README.md](docs/README.md) — 中英双语完整指南
- [docs/MCP集成指南.md](docs/MCP集成指南.md) — MCP 集成指南
- [docs/真机验证报告.md](docs/真机验证报告.md) — 端到端验证报告

## 许可与免责声明

MIT 许可——见 [LICENSE](LICENSE) 与 [DISCLAIMER.md](DISCLAIMER.md)。

本项目仅供**教育与互操作性研究**：与 Icom Inc. 无任何关联；不包含任何 Icom 专有代码或二进制；使用者须遵守当地无线电法规与业余波段划分。
