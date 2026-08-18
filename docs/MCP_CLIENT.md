# IC-705 MCP 客户端接入说明

> 让外置 MCP 客户端（如陆墨）通过 MCP 协议远程控制 IC-705 电台。
> 底层走 Mailslot ExecCmd 桥接，无需 RemoteController / UtyCtrl / CivCtrl。

## 1. 前置条件

| 项 | 要求 |
|----|------|
| Python | 3.9+ |
| fastmcp | 3.4.7（`pip install fastmcp`） |
| RemoteUty.exe | 运行中（MCP 服务的 Mailslot 通信底座） |
| IC-705 电台 | 已连接并经 RemoteUty 建立会话 |
| RemoteController.exe | **关闭**（否则占用 `RemoteUtyCtrlRes` 响应 mailslot，`read_*` 闭环查询会超时） |

> 说明：`set_freq` / `ptt` 是 fire-and-forget，不依赖响应 mailslot，RemoteController 开关均可；`read_freq` / `read_mode` / `read_smeter` / `get_status` 是闭环查询，要求 RemoteController **未运行**。

## 2. 启动方式

### 2.1 stdio（推荐，供 MCP 客户端作为子进程调用）

```bash
PYTHONPATH=src python -m rsba1.mcp
```

可选参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `--transport` | `stdio` | `stdio` / `sse` |
| `--to` | `0xA4` | 电台 CI-V 地址（IC-705） |
| `--from` | `0x00` | 源控制器 CI-V 地址 |
| `--query-timeout` | `2000` | 闭环查询超时 ms |
| `--name` | `ic705-rsba1` | MCP 服务名 |

### 2.2 sse（HTTP，供远程/可视化客户端）

```bash
PYTHONPATH=src python -m rsba1.mcp --transport sse --host 127.0.0.1 --port 8765
```

## 3. 注册到陆墨（MCP 客户端配置）

在 MCP 客户端的 `mcpServers` 配置里添加一个 stdio 服务。以通用 JSON 配置为例：

```json
{
  "mcpServers": {
    "ic705": {
      "command": "python",
      "args": [
        "-m", "rsba1.mcp"
      ],
      "env": {
        "PYTHONPATH": "D:\\path\\to\\feat-civ-via-execcmd-n9t6LM\\src"
      }
    }
  }
}
```

> - `PYTHONPATH` 必须指向本仓库的 `src` 目录（绝对路径），否则 `rsba1` 包无法导入。
> - 若 Python 不在 PATH，`command` 改成 Python 的绝对路径（如 `C:\\...\\python.exe`）。
> - 陆墨若采用自己的配置文件格式（如 `claude_desktop_config.json`、`mcp.json` 等），结构大同小异，核心是 `mcpServers.<name>` 的 `command`/`args`/`env` 三要素。

## 4. 可用 Tool 清单

| tool | 参数 | 返回 | 说明 |
|------|------|------|------|
| `read_freq` | — | `int` Hz | 读 VFO 频率 |
| `read_mode` | — | `{mode_code, mode_name, filter}` | 读工作模式 |
| `read_smeter` | — | `int` | 读 S-meter 原始值 (0-255) |
| `set_freq` | `hz: int` | `bool` | 设频率（仅业余频段，越界报错） |
| `ptt` | `press: bool` | `bool` | 控制 PTT（TX/RX） |
| `get_status` | — | `{freq, mode_*, smeter}` | 一站式组合状态 |

## 5. 自测（离线，不连真机）

不依赖 RemoteUty / 电台，用 mock sender 验证 MCP 服务本身：

```bash
python tools/mcp/mcp_smoke_test.py
```

预期输出含 `tool 齐全` 与 `全部通过`。

## 6. 常见问题

### 6.1 `read_*` 超时（`ResponseTimeoutError`）
- 确认 RemoteController.exe **未运行**（它占用 `RemoteUtyCtrlRes`）。
- 确认 RemoteUty.exe 运行中、电台已连接。
- 可调大 `--query-timeout`。

### 6.2 `set_freq` 报"频率不在业余频段白名单内"
- 安全约束：仅允许 1.8-30MHz / 50-54MHz / 144-148MHz。改用白名单内频率。

### 6.3 提示找不到 `rsba1` 模块
- `PYTHONPATH` 未指向 `src` 目录，或路径拼写错误。

### 6.4 提示找不到 `fastmcp`
- 执行 `pip install fastmcp`。模块为惰性导入，未安装时仅启动服务才报错。