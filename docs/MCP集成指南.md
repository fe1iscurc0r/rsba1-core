# MCP 客户端接入指南 / MCP Client Integration Guide

> 让 AI Agent（陆墨 / Claude Desktop / Cursor 等）通过 MCP 协议控制 IC-705 电台。

## 前置条件 / Prerequisites

| 项目 | 要求 |
|------|------|
| Python | 3.11+ |
| rsba1-core | 已安装 (`pip install rsba1-core`) |
| MCP 客户端 | 陆墨或其他 MCP 兼容客户端 |
| IC-705 | 已开启 RS-BA1 服务器，已连接 |

## 快速开始 / Quick Start

### 方式一：直接命令行调用（无需配置客户端）

```bash
export RADIO_HOST=192.168.0.31
export RADIO_USER=linnan
export RADIO_PASSWORD=你的密码

# 单次读取频率
python -m rsba1.mcp read-freq

# 设置频率（SSTV 频点 7.074 MHz）
python -m rsba1.mcp set-freq 7074000

# 读取 S 表
python -m rsba1.mcp read-smeter

# 读取模式
python -m rsba1.mcp read-mode

# 全状态查询
python -m rsba1.mcp get-status

# PTT（警告：会真正发射！）
python -m rsba1.mcp ptt tx
python -m rsba1.mcp ptt rx
```

### 方式二：MCP 服务器模式（供 AI Agent 调用）

启动 MCP 服务器：
```bash
python -m rsba1.mcp
```

## MCP 工具列表 / Available Tools

| 工具名 | 描述 | 返回值 |
|--------|------|--------|
| `read_freq` | 当前 VFO 频率 | `{freq_hz: int}` |
| `read_mode` | 当前模式 | `{mode: str, filter: int}` |
| `read_smeter` | S 表原始值 | `{s_unit: int}` |
| `set_freq` | 设置频率（Hz） | `{success: bool, freq_hz: int}` |
| `ptt` | PTT 控制 | `{"state":"tx"}` 或 `{"state":"rx"}` |
| `get_status` | 全状态查询 | 频率/模式/S表/PTT |
| `restore_freq` | 恢复到 MCP 启动时的频率 | — |
| `shutdown` | 关闭电台连接 | — |

## AI Agent 对话示例 / Example AI Agent Prompts

> "把电台调到 7.074 MHz"

> "当前信号强度是多少？"

> "帮我设置到 145 MHz FM，然后读取确认"

> "关闭 PTT"

## 频段限制 / Band Restrictions

`set_freq` 会自动拒绝不在业余频段内的频率设置请求：

| 频段 | 范围 |
|------|------|
| 40m | 7.000 – 7.300 MHz |
| 20m | 14.000 – 14.350 MHz |
| 2m | 144.000 – 148.000 MHz |
| 70cm | 420.000 – 450.000 MHz |
| ... | ...（完整列表见主 README） |

## 故障排除 / Troubleshooting

| 症状 | 可能原因 | 解决方法 |
|------|----------|----------|
| `TimeoutError` | 电台未连接/网络不通 | 检查 IP 和 RS-BA1 服务器是否开启 |
| `Auth failed` | 用户名或密码错误 | 在电台菜单中确认 RS-BA1 用户名/密码 |
| `Frequency out of band` | 频率超出业余频段 | 使用合法业余频段内的频率 |
| `Connection refused` | UDP 端口被防火墙阻断 | 开放 UDP 50001/50002 |
