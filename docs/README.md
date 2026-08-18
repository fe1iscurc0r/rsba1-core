# rsba1-core

**Icom RS-BA1 V2 纯 Python 协议栈** — 通过网络控制 IC-705、IC-9700 等支持 CI-V 的电台。不依赖任何 Icom 原生程序。

**Pure Python implementation of the Icom RS-BA1 V2 protocol stack** — control IC-705, IC-9700 and any CI-V transceiver over the network. No Icom binaries required.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-green.svg)](https://python.org)
[![CI](https://github.com/fe1iscurc0r/rsba1-core/actions/workflows/ci.yml/badge.svg)](https://github.com/fe1iscurc0r/rsba1-core/actions)

---

## 这是什么？ / What is this?

Icom 原厂 RS-BA1 软件只能在 Windows 上运行，且闭源。本项目通过逆向工程完整解析其通信协议，提供**纯 Python 实现**，可在任何平台运行（Windows / Linux / macOS / 树莓派）。

Icom's RS-BA1 software runs on Windows only and is closed-source. This project reverse-engineers the protocol and provides a **pure Python replacement** that runs anywhere Python runs.

**获得的能力 / What you get:**
- 纯 Python CI-V 协议栈（无 DLL，无 Windows 依赖）
- MCP 服务器 —— AI Agent 可通过自然语言控制电台
- 简洁的 CLI 界面
- 跨平台：Windows、Linux、macOS、树莓派

**真机验证 / Hardware tested:** IC-705（2026-08-18 端到端实测通过）

## 快速开始 / Quick Start

### 前提条件 / Prerequisites

- Python 3.11+
- IC-705 已开启 RS-BA1 服务器（`菜单 → 设置 → WLAN设置 → 远程设置 → 远程服务器 → 开`）
- 电台的 IP 地址，以及在电台里设置的 RS-BA1 用户名和密码

### 安装 / Install

```bash
pip install rsba1-core
```

从源码安装 / From source:

```bash
git clone https://github.com/fe1iscurc0r/rsba1-core.git
cd rsba1-core
pip install -e ".[all]"
```

### 运行 / Run

```bash
# 设置凭证环境变量（推荐 —— 避免密码进 shell 历史）
export RADIO_HOST=192.168.0.31
export RADIO_USER=linnan
export RADIO_PASSWORD=你的密码

# 单次命令（无需启动服务器）
python -m rsba1.mcp read-freq

# 或启动 MCP 服务器，供 AI Agent 调用
python -m rsba1.mcp
```

### 端到端测试 / E2E Test

```bash
python scripts/e2e_civ_loop.py \
  --host 192.168.0.31 \
  --user linnan \
  --pwd 你的密码
```

预期输出：
```
=== E2E: RS-BA1 CI-V loopback 192.168.0.31 (user=linnan) ===
[0] Original frequency: 144.920000 MHz
[1] read_freq loop (3x)
  1. 144.920000 MHz  mode=FM
  2. 144.920000 MHz  mode=FM
  3. 144.920000 MHz  mode=FM
  ✓ All 3 reads stable at 144.920000 MHz
=== PASS: all stages OK ===
```

## 架构 / Architecture

```
┌──────────────────────────────────────────────────────┐
│  你的应用 / AI Agent                                 │
│  (MCP 客户端 / CLI / Python 导入)                   │
└────────────────────┬─────────────────────────────────┘
                     │ stdio / 网络
┌────────────────────▼────────────────────────────────┐
│  rsba1-core（纯 Python，无原生依赖）                │
│                                                       │
│  radio_link.py — UDP 会话管理器                       │
│    ├── Command 信道 (UDP 50001): 登录 / 认证        │
│    └── Serial 信道 (UDP 50002): CI-V 透传           │
│                                                       │
│  serial_codec.py — 线缆格式编码                       │
│  civ_commands.py — CI-V 帧构造                        │
└────────────────────┬─────────────────────────────────┘
                     │ UDP（仅用标准库 socket）
┌────────────────────▼────────────────────────────────┐
│  IC-705 内置 RS-BA1 服务器（或 RemoteUty.exe）      │
└──────────────────────────────────────────────────────┘
```

**关键设计 / Key design:**
- **无 Windows API**：仅用 Python 标准库 `socket`，全平台通用
- **无 Icom 二进制**：完整开源协议实现
- **RadioLink 会话复用**：每次 MCP 调用不复用会话
- **业余频段白名单**：`set_freq` 自动拒绝范围外频率

## 频段白名单 / Frequency Bands (whitelist)

| 波段 | 频率范围（MHz） | Band |
|------|----------------|------|
| 160m | 1.800 – 2.000 | ✅ |
| 80m | 3.500 – 4.000 | ✅ |
| 60m | 5.330 – 5.368 | ✅ |
| 40m | 7.000 – 7.300 | ✅ |
| 30m | 10.100 – 10.150 | ✅ |
| 20m | 14.000 – 14.350 | ✅ |
| 17m | 18.068 – 18.168 | ✅ |
| 15m | 21.000 – 21.450 | ✅ |
| 12m | 24.890 – 24.990 | ✅ |
| 10m | 28.000 – 29.700 | ✅ |
| 6m | 50.000 – 54.000 | ✅ |
| 2m | 144.000 – 148.000 | ✅ |
| 70cm | 420.000 – 450.000 | ✅ |

## 硬件要求 / Hardware Requirements

| 项目 | 要求 |
|------|------|
| 电台 | IC-705（已实测）/ IC-9700（协议兼容） |
| 连接模式 | 直连网络（推荐）/ USB CI-V / RemoteUty 代理 |
| 网络 | 电台与控制端在同一局域网，UDP 50001/50002 可达 |

## 项目结构 / Repository Structure

```
rsba1-core/
├── src/rsba1/
│   ├── radio_link.py           # 高层会话管理器（推荐使用）
│   ├── ctypes_wrappers/       # DLL 调用包装（仅参考）
│   ├── serial/                 # UDP Serial 信道
│   │   ├── serial_codec.py   # 线缆格式编码
│   │   └── command_client.py  # UDP Command 信道
│   ├── mailslot/               # Windows Mailslot IPC（旧版，保留参考）
│   └── mcp/
│       ├── radio_link_server.py  # 跨平台 MCP 服务器（推荐）
│       └── _server_mailslot_ref.py  # Windows-only 参考实现
├── tests/                      # 单元测试（mock，无硬件可运行）
├── scripts/                    # 实用脚本（含 e2e 测试）
├── docs/                       # 文档（本目录）
└── pyproject.toml             # 包配置
```

## 功能状态 / Feature Status

| 功能 | 状态 |
|------|------|
| CI-V 读取（频率/模式/S表） | ✅ 实测 |
| CI-V 写入（设频率） | ✅ 实测 |
| PTT 控制 | ✅ 实测 |
| RS-BA1 认证 | ✅ 实测 |
| Serial 信道（UDP 50002） | ✅ 实测 |
| Command 信道（UDP 50001） | ✅ 实测 |
| 音频流（UDP 50003） | ❌ 未实现 |
| RemoteUty.exe 代理模式 | ⚠️ 未测试 |

## 参与贡献 / Contributing

欢迎提交 Issue 和 PR。报告 bug 请包含：
1. 运行 `python scripts/e2e_civ_loop.py --dry-run` 验证环境
2. 说明硬件型号和固件版本
3. 附上完整错误输出

## 免责声明 / Disclaimer

本项目仅供**教育和研究目的**。RS-BA1 V2 和 Icom 电台是 **Icom Inc.**（https://www.icomjapan.com）的专有产品。本项目与 Icom 没有任何关联、认可或连接。

您有责任遵守适用法律和 Icom 的许可条款。在没有有效业余无线电执照的情况下，不得使用本软件操作发射机。

## 许可 / License

MIT — 参见 [LICENSE](LICENSE) 和 [DISCLAIMER.md](DISCLAIMER.md)。
