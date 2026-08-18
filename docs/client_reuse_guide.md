# rsba1 客户端复用手册

> 面向：陆墨 / 二次开发者如何复用 `src/rsba1/` 纯 Python 实现，摆脱对
> RemoteController.exe / UtyCtrl.dll / CivCtrl.dll / RemoteUty.exe 的依赖。
> 状态：2026-08-11，Phase 4 纯协议栈已实现；**codec 层已与线上抓包证据对齐，
> 端到端（真实电台 + 真实服务器）尚未闭环**，见 §7 状态与待办。

---

## 0. 三端口架构速览（需真机复核）

| UDP 端口 | 信道 | 承载 | 会话前置 |
|----------|------|------|----------|
| **50001** | Command | ConnectServer 认证 / KeepAlive / GetInfo 等 | 无（登录入口） |
| **50002** | Serial | CI-V 指令透传（读频率/设频率/PTT/S-meter） | **需先经 50001 认证**（见 §7.2） |
| **50003** | Audio | 音频流（RS-BA1 远程语音） | 语义未抓，存疑 |

> ⚠️ **端口架构疑点未解**（P0）：服务器 `netstat` 只监听 50001，但抓包见客户端源端口
> 为 50003，且 probe 发到 50002 曾被 `ConnectionResetError(10054)` 拒绝。三端口到底是
> "服务器各监听一个"还是"客户端各用不同源端口"，需用 `tools/hook_bind_ports.js` /
> `tools/hook_recv_localport.js`（Frida 钩子）在服务器上复核。

---

## 1. 三条可用路径总览

| 路径 | 适用场景 | 平台 | 依赖 | 是否需 ConnectServer 登录 |
|------|----------|------|------|---------------------------|
| **A. Mailslot IPC**（本机） | RemoteUtility 与 RemoteController 同机，陆墨扮演"第二 RemoteController" | 仅 Windows | pywin32 或 ctypes（自动回退） | 否（Mailslot 无认证） |
| **B. Serial 信道**（UDP 50002） | 陆墨直连 RadioCom 端（运行 RemoteUty 的机器），下发 CI-V 命令 | 跨平台 | 仅标准库 socket | 建议先 Login（见 C） |
| **C. Command 信道**（UDP 50001） | ConnectServer 登录 + KeepAlive，确立会话标识供 Serial 信道使用 | 跨平台 | 仅标准库 socket | 是（本路径即登录） |

> 简单起见：**本机控制走 A**；**远程 UDP 控制走 B + C**（C 先登录拿会话，B 发 CI-V）。

---

## 2. 通用安装与导入

```bash
pip install pywin32   # 仅路径 A 需要；B/C 纯标准库
```

```python
import sys, os
sys.path.insert(0, "path/to/icom-remote-utility-rev-HjgIT8/src")
from rsba1 import serial, mailslot, ctypes_wrappers  # 各子包顶层别导入
```

---

## 3. 路径 A：Mailslot IPC（本机，推荐起步）

### 3.1 直接写命令包

```python
from rsba1.mailslot import MailslotClient
from rsba1.mailslot.protocol import CMD_GET_COUNT_CLIENT_TRANS

with MailslotClient() as c:            # 默认 \\.\mailslot\RemoteUtyCtrlCmd
    n = c.write_command(CMD_GET_COUNT_CLIENT_TRANS)  # n = 4
```

- Mailslot 名已动态确证为 `\\.\mailslot\RemoteUtyCtrlCmd`（非早期占位 `civsend`）。
- `MailslotClient` 自动选择 backend：有 pywin32 用 `pywin32`，否则 `ctypes`。
- 写操作是**单向**（fire-and-forget），响应走 `RemoteUtyCtrlRes`（见 §3.4）。

### 3.2 用高层发送器发 CI-V 命令（最简单）

```python
from rsba1.mailslot.civ_via_execcmd import CivViaExecCmdSender

with CivViaExecCmdSender() as s:       # 默认 to=0xA4 (IC-705), from=0x00
    s.send_read_freq()                 # 读频率
    s.send_ptt_on();  ... ; s.send_ptt_off()   # PTT 控制
    s.send_set_freq(14_270_000)        # 设频 14.270 MHz
```

该方法把 CI-V 命令包成 **ExecCmd（cmd_code=2）** payload 写入 Mailslot，
由本机 RemoteUtility 转发到电台。全部 fire-and-forget。

### 3.3 自定义 CI-V 命令体

```python
from rsba1.ctypes_wrappers import civ_commands as civcmd
from rsba1.mailslot.civ_via_execcmd import CivViaExecCmdSender

frame = civcmd.build_frame(0xA4, 0x00, bytes([0x03]))   # 完整帧 FE FE A4 00 03 FD
with CivViaExecCmdSender() as s:
    s.send_civ_frame(frame)            # 透传任意 CI-V 帧
```

### 3.4 读取响应（可选，仅 RemoteController 未运行时）

```python
from rsba1.mailslot.civ_via_execcmd import ResponseReader

with ResponseReader(timeout_ms=2000) as r:
    # 在另一线程/进程用 CivViaExecCmdSender 发送命令...
    resp = r.read()                    # bytes 或 None(超时)
```

> ⚠️（P4 实测坐实，2026-08-11）**CI-V 应答不回写 RemoteUtyCtrlRes Mailslot**。
> sub_cmd=0/1 发 read_freq 均超时无应答；sub_cmd=1 已确证能触发 IC-705 PTT TX，
> **应答走 UDP Serial(50002) 信道，不经 Mailslot**。因此路径 A 只能下发命令，
> 读取 CI-V 应答须走路径 B。RemoteController 运行时已创建 `RemoteUtyCtrlRes`，
> 本方可读端会创建失败，此时仅能 fire-and-forget。

---

## 4. 路径 C：Command 信道登录（UDP 50001）

```python
from rsba1.serial import CommandClient

with CommandClient(host="192.168.1.10", username="u", password="p") as c:
    ok = c.connect()                   # 认证成功 → True, 并取得 field_8/field_C
    if ok:
        c.keepalive()                  # 心跳（阈值 90s）
```

- 端口默认 50001（注册表 CommandPort）。
- `connect()` 成功后以 `c.field_8 / c.field_C` 保存会话标识，供 SerialClient 使用。
- 服务器端空密码可直通（`password=""`）。
- **codec 依据**：Command 业务命令包 header 为 `<HHHHII`（totalLen word LE + version word BE
  + type word BE + seq word BE + f8 dword LE + fc dword LE），见
  `re/protocols/serial_channel.md §5.8` 与 `command_channel_cmd.md`。

---

## 5. 路径 B：Serial 信道发 CI-V（UDP 50002）

### 5.1 直接构造 codec（纯代码层，可离线测试）

```python
from rsba1.serial import build_udp_packet, parse_udp_packet

pkt = build_udp_packet(b"\x03", sseq=0, seq=0, field_8=0x2A94BC02, field_C=0x19F8B4F7)
wire, frame = parse_udp_packet(pkt)   # frame.payload == b"\x03"
```

### 5.2 用 SerialClient 收发 CI-V

```python
from rsba1.serial import SerialClient

with SerialClient(host="192.168.1.10",
                  field_8=<c.field_8>, field_C=<c.field_C>) as s:
    s.send_read_freq()
    freq_resp = s.read_civ_response(timeout=2.0)   # CI-V 响应帧 bytes
```

高层方法：`send_read_freq / send_read_mode / send_set_freq(hz) / send_ptt_on /
send_ptt_off / send_read_smeter`，以及底层 `send_civ(frame)` / `send_civ_body(body)`。

> **源端口绑定**：服务器按源端口识别会话，真机客户端源端口 = 50002。
> `SerialClient(..., bind_port=50002, bind_ip="127.0.0.1"|LAN)`，需 `SO_REUSEADDR` 共享绑定。
> 绑定源端口后自己发的包会回环，`read_civ_response` 已按已发 wire seq 过滤。

### 5.3 解析 CI-V 响应

```python
from rsba1.ctypes_wrappers import civ_commands as civcmd

to, frm, cmd, payload = civcmd.parse_frame(freq_resp)
hz = civcmd.bytes_to_freq(payload)     # payload = 5 字节 BCD
```

---

## 6. 常量与子包速查

```python
# 端口
serial.DEFAULT_SERIAL_PORT   # 50002
serial.DEFAULT_COMMAND_PORT  # 50001

# 会话标识默认占位（首包；收到服务器应答后应回显对调值）
serial.DEFAULT_SESSION_F8    # 0x2A94BC02
serial.DEFAULT_SESSION_FC    # 0x19F8B4F7

# CI-V 地址
civcmd.IC705_TO_ADDR         # 0xA4
civcmd.IC7300_TO_ADDR        # 0x04
civcmd.DEFAULT_FROM_ADDR     # 0xEE->0x00（线上实测对端 from=0xE0）

# Mailslot
mailslot.MailslotClient      # 默认名 RemoteUtyCtrlCmd
mailslot.protocol.CMD_EXEC_CMD   # 2 (CI-V 经 ExecCmd)
```

---

## 7. 端到端状态与待办（codec ✓ / E2E 待真机）

### 7.1 codec 层与线上证据一致性（已复核，2026-08-11）

| 组件 | 线上证据 | codec 现状 | 结论 |
|------|----------|-----------|------|
| Serial wire 头 | `<IHHII` 全 LE（totalLen dword + type word + seq word + f8 + fc） | `serial_codec.build_wire_header` 一致 | ✅ 对齐 |
| Serial 帧 | `flags(0xC0|bit0) + frameLen(LE) + sseq(BE) + payload` | `serial_codec.build_serial_frame` 一致 | ✅ 对齐 |
| Command 业务头 | `<HHHHII`（含 version 字段） | `command_client.build_command_header` 一致 | ✅ 对齐 |
| 传输层控制包 | 50001/50002 均 `<IHHII`（type=0/7） | `probe_dual_session.build_wire16` 一致 | ✅ 对齐 |
| CI-V 透传 | 双向 `c1 [len][sseq][CI-V]` | `serial_client` 透传 | ✅ 对齐 |

结论：**codec 层可离线编解码并通过 mock 测试，命令类型（CMD_* 0x0100~0x0106）与
业务 header 均已对齐权威实现**（早期骨架 `phase4-implement/rsba1/udp_link.py` 已标注
不可用并同步常量）。

### 7.2 仍需真机验证（E2E 未闭环）

- **回程验证**（P0）：目前只确证"去程"（客户端→服务器→串口），"回程"
  （服务器→电台→客户端返回 CI-V 响应）因电台 IC-705 未上电无法验证，待供电后重试。
- **Command 前置（根因，§5.8）**：抓包显示服务器只向"已认证源端口"主动发探测包，
  源端口信息来自 Command(50001) ConnectServer 认证。**仅发 Serial 注册包无法触发
  服务器应答**。完整流程应为：
  ① Command(50001) 认证 → ② 服务器向客户端 50002 发探测包 → ③ 客户端对调 field 回传 →
  ④ CI-V 双向流动。
- **端到端脚本**：`scripts/e2e_civ_loop.py`（Command→Serial 串联，见 §8）待真机执行。

> 真机验证前置：RemoteUtility.exe 已开、RemoteController 已关、IC-705 已上电连接、
> 频率白名单（`civ_commands.is_allowed_freq`，1.8-30/50-54/144-148 MHz）已生效。

---

## 8. 端到端 E2E 验证脚本（真机执行用）

```bash
# 按 §5.8 流程：先 Command(50001) 认证，再 Serial(50002) 收发 CI-V
python scripts\e2e_civ_loop.py --host 192.168.0.23 \
    --user <用户名> --pwd <密码> --bind-ip 192.168.0.23
```

脚本会：① `CommandClient.connect()` 认证；② 绑定本机 50002 源端口；③ `SerialClient`
发 `read_freq` 并解析频率。成功打印 `✓ 频率: xx.xxxxxx MHz`，失败给出阶段与原始包。

- 纯 Serial 对照（已知可超时，供归因）：`python scripts\verify_serial_loop.py --host 127.0.0.1`
- 双端口注册探测（复刻真实客户端绑定 50001+50002）：`python tools\probe_dual_session.py --host 127.0.0.1`
- ConnectServer 布局穷举：`python tools\probe_connect_bs.py --user u --pwd p`

---

## 9. 参考文档

- `re/protocols/serial_channel.md`（Serial 50002 全链路确证，含 §5.7/5.8/5.9）
- `re/protocols/command_channel_cmd.md`（Command 50001 命令分发与 header 布局）
- `re/protocols/credential_and_session.md`（ConnectServer 凭证与会话标识）
- `re/protocols/cudp_ctrl2_resend_analysis.md`（UDP2 重传/keepalive 语义）
- `re/protocols/capture_todo.md`（剩余待抓包/待复核优先级清单）