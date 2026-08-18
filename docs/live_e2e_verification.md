# 真机 Live E2E 手动验证手册

> 面向：陆墨 / 二次开发者，在**真实硬件 + 真实进程 + 真实网络**上手动跑通
> `src/rsba1/` 纯 Python 协议栈，闭环验证「客户端复用」的全部路径。
> 状态：2026-08-12。此文档只含**需真机手动执行**的步骤；纯代码层可离线验证的
> 部分见 `client_reuse_guide.md`。
> 前置：① 电台 **IC-705 上电**（回程验证前置）；② RemoteUty（服务端）+
> RemoteController（客户端）版本一致并在线；③ Wireshark 已装
> （过滤 `udp.port==50001 or 50002 or 50003`）；④ Python + pywin32。

---

## 0. 环境速查

| 项 | 值 | 说明 |
|---|---|---|
| 基准端口 | 50001 | Command 信道（登录/会话/心跳） |
| Serial 端口 | 50002 | CI-V 透传（=基准+1） |
| Audio 端口 | 50003 | 音频流（=基准+2） |
| 命令 Mailslot | `\\.\mailslot\RemoteUtyCtrlCmd` | RemoteUty 创建/读，客户端写 |
| 响应 Mailslot | `\\.\mailslot\RemoteUtyCtrlRes` | RemoteController 创建/读（RemoteUty 写） |
| IC-705 CI-V 地址 | `0xA4` | `civcmd.IC705_TO_ADDR` |
| 会话标识 | `field_8`=本端 / `field_C`=对端 | SYNC 首包 fc=0，服务端响应回填 |

**三条验证路径**（对应 `client_reuse_guide.md` §1）：
- **A. Mailslot IPC**（本机，无认证）→ 本机控制电台
- **B. Serial 信道**（UDP 50002）→ 远程下发 CI-V
- **C. Command 信道**（UDP 50001）→ ConnectServer 登录拿会话标识

> 建议顺序：**A 先做**（门槛最低，无需登录/抓包），再 B+C（需会话标识）。

---

## 1. 路径 A —— Mailslot IPC 实机验证（推荐先做）

### 1.1 前置确认

```bash
# 确认 RemoteUty 已运行并创建命令 mailslot（两条都要存在）
# 在 PowerShell 中手动检查（无需代码）：
python -c "import win32file; \
  open(r'\\\\.\\mailslot\\RemoteUtyCtrlCmd', 'r') if False else None; \
  print('见下方探针')"
```

> 最省事：直接运行 1.2 的探针脚本，它会自动尝试打开命令 mailslot。

### 1.2 最小写入测试（确认 mailslot 可写）

```bash
python tools/probe_mailslot_semantics.py --no-resp --cmd 0x00
```

**预期输出**：
- `打开命令 mailslot ... backend=pywin32 open=True`
- `[GetCountClientTrans] cmd=0x00 ... -> 写入 4B OK`

**判定**：`-> 写入 ... OK` 出现即证明本机可写 RemoteUtyCtrlCmd，路径 A 打通。
若报 `MailslotNotFoundError`，说明 RemoteUty 未运行或 mailslot 名不对。

### 1.3 响应捕获（需 RemoteController 未运行）

> RemoteController 运行时已创建 `RemoteUtyCtrlRes`，本探针创建会失败（回退
> fire-and-forget）。要捕获响应，需先**关闭 RemoteController**，只留 RemoteUty。

```bash
python tools/probe_mailslot_semantics.py
```

**预期输出**：
- `已创建响应 mailslot \\.\mailslot\RemoteUtyCtrlRes (独占接收) ✓`
- 每条 Get* 命令后出现 `resp[...] = cmd_echo=0x.. psize=.. payload=..`（若有响应）

**判定**：若出现带 `cmd_echo=0x<cmd>` 的响应，则确证：
- 响应格式 `[cmd_code echo, payload_size, align, payload]` ✓
- RemoteController 未运行时，本方可独占接收响应 ✓

### 1.4 ExecCmd sub_cmd 语义（P2#11-15，核心）

搭配 Frida hook 观察 sub_cmd 分发副作用（UDP sendto / 状态变化）：

```bash
# 终端 1：附加到 RemoteUty，观察 sub_cmd 分发 + UDP 副作用（持续 60s）
python tools/frida/run_hook.py <RemoteUty_PID> 60 tools/frida/hook_mailslot_subcmd.js

# 终端 2：发送 ExecCmd，遍历 sub_cmd 0-5（各带 CI-V 读频率）
python tools/probe_mailslot_semantics.py --no-resp --wait 0.5
```

**Frida 预期输出**（终端 1）：
```
[Mailslot] <- ExecCmd cmd=0x02 sub_cmd=0 packet[0:0x20]=...
[Mailslot] <- ExecCmd cmd=0x02 sub_cmd=1 ...
...
>>> [UDP] sendto len=... totalLen=... type=0x00 ...
```

**判定**（回填 `re/protocols/exec_cmd_subcmd.md` §8 待复核项）：
- **sub_cmd 实际取值** = 探针发送的 0-5，确认跳表映射 ✓（P2#11）
- **sub_cmd 是否触发 UDP 发包**：若 sub_cmd=0 后出现 `[UDP] sendto`，
  说明该 sub_cmd 间接触发 CI-V 转发（否则仅内部状态管理）→ 回填 P2#15
- **SendMessageA hwnd**（P2#12）、**sub_cmd_3 操作码**（P2#13）、
  **sub_cmd_4/5 事件**（P2#14）需逐条对照 Frida 输出记录

### 1.5 电台联动（真机 IC-705 上电后）

```bash
# 用高层发送器直接发 CI-V（读频率 / PTT / 设频），电台若上电会响应
python tools/probe_mailslot_semantics.py --no-resp --civ read_freq --subs 0
# 观察电台面板/RemoteUty GUI 是否更新频率
```

**判定**：电台面板频率值变化 / RemoteUty GUI 刷新 → Mailslot→CI-V 全链路闭环。

---

## 2. 路径 C + B —— UDP 登录 + CI-V 直连（真机）

### 2.1 先登录（路径 C，UDP 50001）拿会话标识

```bash
python tools/probe_command_connect.py --host <电台IP> --user <用户名> --pass <密码>
```

> `probe_command_connect.py` 会打印握手后的 `field_8 / field_C`，供路径 B 使用。

### 2.2 CI-V 透传（路径 B，UDP 50002）

```bash
# 复用上一步拿到的 f8/fc
python tools/probe_civ_transit.py --host <电台IP> --f8 <field_8> --fc <field_C> --send-mode 1
```

**预期**：收到 CI-V 响应帧（读频率回 `fe fe e0 a4 03 ...`），去程+回程闭环。

### 2.3 Audio 信道（可选，UDP 50003）

```bash
# 需电台上电 + 会话建立；音频包 = 0x18 头 + 载荷，采样率 12000Hz
# （具体发包探针待补，先用 Wireshark 抓 50003 确认格式）
```

---

## 3. 真机联动最佳实践与安全约束

> 重要：**频率设置必须白名单到业余频段**（用户硬约束）。

| 频段 | 范围 |
|---|---|
| HF | 1.8–30 MHz |
| VHF | 50–54 MHz |
| UHF | 144–148 MHz |

- **PTT 测试**：`send_ptt_on()` 后会真实发射，务必接假负载或天线，避免空载。
- **设频测试**：只允许用上面白名单内的频率（如 `send_set_freq(14_270_000)` =
  14.270 MHz，在 1.8–30MHz 内）。
- **时段隔离**：路径 A 与 RemoteController 同时控制电台时，避免并发发相同命令。

---

## 4. 验证跟踪表（回填用）

| # | 验证项 | 命令/脚本 | 通过判据 | 结果 |
|---|---|---|---|---|
| A1 | Mailslot 可写 | 1.2 探针 | `写入 OK` | ☐ |
| A2 | 响应独占接收 | 1.3 探针 | 收到 `cmd_echo` 响应 | ☐ |
| A3 | sub_cmd 分发 | 1.4 Frida | `sub_cmd=N` 打印 | ☐ |
| A4 | sub_cmd→UDP | 1.4 Frida | 某 sub_cmd 后 `[UDP] sendto` | ☐ |
| A5 | Mailslot→电台 | 1.5 高层发送 | 电台面板/GUI 更新 | ☐ |
| B1 | 登录拿会话 | 2.1 | `field_8/field_C` 打印 | ☐ |
| B2 | CI-V 回程 | 2.2 | 收到 CI-V 响应帧 | ☐ |
| C1 | Audio 格式 | 2.3 | 抓包 0x18 头 | ☐ |

> 完成后回填 `tools/pktmon_out/`、更新 `client_reuse_guide.md` §7 待确认项，
> 并把确证值写回 `src/rsba1/` 对应常量与注释。