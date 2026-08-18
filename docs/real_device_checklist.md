# 剩余真机验证 —— 可操作清单

> 只列**需真机/需在线对端**才能闭环的项（硬件、抓包、本机 GUI 动态验证）。
> 纯离线工程层工作已全部完成（codec 对齐、握手、文档、seew 测试均通过）。
> 每个待办含：目标 / 前置 / 命令 / 预期 / 判定。安全约束统一见文末 §A。

> ⚠️ 工具现状（2026-08-12 实测）：
> - 此前会话声称的 `probe_mailslot_semantics.py`、`hook_mailslot_subcmd.js`、
>   `docs/live_e2e_verification.md` **在本 worktree 不存在**（未提交或落在另一 worktree）。
>   下方涉及它们的步骤给出**手动替代**或标记"需重建"。
> - `scripts/e2e_civ_loop.py` 仍写死旧 LE 会话 ID（§5.2），与新握手模型（pkt3/4/6 + BE）冲突，
>   **不可直接作为回程验证**，先走探针路径，后按 §T2 定案再统一。

工作区根：`c:\Users\ASUS\.trae-cn\worktrees\rs-ba1-reverse\feat-civ-via-execcmd-n9t6LM`（下称 `<root>`）
Python：用项目虚拟环境里的解释器（任一路径能 import `src/rsba1` 即可）。

---

## T0 — 前置确认（每次验证前先做）

1. `RemoteUtility.exe` 在跑（Mailslot 通道的前提）。
2. 关掉 `RemoteController.exe`（否则 `RemoteUtyCtrlRes` 被其独占，读不到响应）。
3. IC-705 上电并连到 RemoteUty 所在机器（回程验证前提）。
4. 网络：本机 127.0.0.1 需开启 Npcap loopback 捕获；否则用 LAN IP（如 192.168.0.23）。
5. 抓包：Wireshark 已装，过滤 `udp.port==50001 or udp.port==50002 or udp.port==50003`。

---

## T1 — 回程验证（P0#4 核心）：服务器→电台→客户端 CI-V 响应

> capture_todo P0#4：此前只确证"去程"（客户端→服务器→串口），"回程"待 IC-705 上电。

**目标**：确认 Serial(50002) 收到 CI-V 应答并解析出频率。

**前置**：T0 全部满足（尤其 IC-705 上电）。

**命令**（探针方式，逐个实验，看原始回包）：
```
python tools/tmp_probe_civ.py            # ✅ 已验证 (2026-08-18)
python tools/probe_serial_handshake.py --host <IP> --port 50002
```
观察 read_freq/read_mode 是否有回包；回包格式应含 Serial 帧 + CI-V（`fe fe 00 a4 03 ... fd`）。

预期 / 判定：
| 现象 | 结论 |
|---|---|
| 收到 CI-V 应答且解析频率成功 | ✅ 回程闭环打通 |
| 收到回包但解析失败 | ⚠ 帧/CI-V 布局与真机不符，抓 hex 回填文档 |
| 无回包 | → 检查 ① source/from 地址是否 0x00（0xE0 会无应答） ② IC-705 RS-BA1 Server Function 是否开启 ③ 源端口 50002 绑定 |

> ✅ 2026-08-18 线上已闭环：`tmp_probe_civ.py`（from=0x00）→ read_freq 得
> `fe fe 00 a4 03 00 00 54 45 01 fd` = **14.554 MHz**，read_mode 得 `fe fe 00 a4 04 05 01 fd`。

> ⚠️ 2026-08-18 复测（未找回闭环，反证 §5.14 判定标准）：
> - 当时本机 RemoteUty(pid 46776) **在跑但仅监听** `0.0.0.0:50001-50003`，**无到 705(.31) 的出站连接**。
> - 裸 `SerialClient` 传输握手成功、服务器按本端 SID(`fc=0x0017C352`) 路由 keepalive，但 read_freq
>   重发 2 次均无 `fe fe 00 a4 ...` 应答 → 705 侧无"已认证/已建立"控制会话。
> - 推论：**能否闭环取决于 705 侧是否有已认证客户端 hold 住会话（RemoteUty 须实际登录 705），
>   而非进程是否存活**。复现前置列于 §5.14。

**手动替代 E2E**（Command 认证前置 + Serial 收发）：待 `e2e_civ_loop.py` 会话 ID 与新握手对齐后：
```
python scripts/e2e_civ_loop.py --host <IP> --user u --pwd p --bind-ip <本机LANIP>
```

---

## T2 — field_8/field_C 端序定案（LE vs BE）

> capture_todo 存疑项。本地抓包字节 `02 bc 94 2a` 可读为 LE `0x2A94BC02` 或 BE `0x02BC942A`。
> codec 已统一为 LE（对齐 main 权威与 Command 信道）。

**目标**：确认服务器端序。

**命令**：用 `tmp_probe_civ.py`（LE 编解码）握手并在 pkt4 观察服务器是否原样回显本端 SID
及是否据其路由回包。

**判定**：
| 结果 | 结论 |
|---|---|
| 服务器识别并回显 LE 编码 SID、会话路由正常 | ✅ **LE 定案** （2026-08-18 线上双确证，改动已提交） |
| 仅 BE 值能触发 | 改回 BE（未发生） |

> ✅ 2026-08-18 已定案 **LE**：客户端 LE 打包 SID `0x0017C352`，服务器 pkt4 `field_C`
> 原样回显并据以维持会话路由回包。`serial_codec` 全 `<IHHII` LE。已写入
> `re/protocols/serial_channel.md` §5.10。

---

## T3 — Command 信道逐条抓包（P1#8/9/10）

> 7 条命令 Connect/Disconnect/GetInfo/ConnectTrans/DisconnectTrans/KeepAlive/Debug，
> ConnectServer 载荷三字段定名、响应类型完整映射。

**目标**：定死每条请求/响应 `buf[2]/[4]/[6]/[8]/[0xC]` 与载荷三块（UserName/Password/Memo）。

**命令**：
```
python tools/probe_command_connect.py --host <IP> --user u --pwd p --wait 1.0
```
Wireshark 并行抓 `udp.port==50001`，把每命令请求/响应 hex 填入
`re/protocols/command_channel_cmd.md` §4 / `credential_and_session.md` §4。

**判定**：每条命令均有响应，响应 `buf[4]`（响应类型）落入已知枚举（ConnectServer=0x100、
GetInfo=0x202、KeepAlive=0x502 ...），未枚举类型登记为新增。

> ⚠️ 2026-08-18 线上部分定案（`probe_command_connect.py` 对 IC-705 直连）：
> - ✅ 传输握手 pkt3/4/6 在 50001 复用 UDP2 语义，pkt4 `field_C` 原样回显客户端 SID。
> - ⚠ 半开会话后服务器对该源端口 keepalive 洪泛（0x0007 21B + 空 0x0000 16B）淹没应答，
>   V1~V6 ConnectServer 变体均未见 0x0002 认证 ack。
> - 故 ConnectServer 每命令响应表仍为**静态确证 + 存疑**；完整认证应答需以真实
>   RemoteUtility 客户端→服务器会话抓包定死。
> - ✅ 关键事实：**CI-V 经 Serial 直通，无需先 Command 认证**（见 T1）。
> 已写入 `re/protocols/command_channel_cmd.md` §4.1。

---

## T4 — Heartbeat / RESEND 现场值（P1#6/7）

**目标**：空闲 ≥10s 空闲心跳 type、f8/fc 是否不变、seq 是否不递增、间隔；RESEND 重传负载。

**命令**：
- 心跳：保持空闲 ≥10s，`tools/tmp_probe_keepalive.py` 抓 50002；
- RESEND：断电/拔线制造乱序，抓 `type` 与 `[start,end]` 区间负载。

**判定**：心跳 type、f8/fc/seq 行为与文档假设一致；RESEND 负载 `[start,end]` 每项 4 字节；
`buf[6]` 单区间 carry 语义确认。

> ✅ 2026-08-18 线上测得 KEEPALIVE/DATA 现场值（`tmp_probe_keepalive.py` 对 IC-705 50002）：
> | type | 间隔 | 4s 计数 | payload | f8 / fc | seq |
> |---|---|---|---|---|---|
> | 0x0007 KEEPALIVE | ~100ms | 40 | 5B，小端[1:5]每跳 `+0x6400` | 服务器SID / 客户端SID（**未对调**） | 递增 |
> | 0x0000 DATA | ~30ms | 134 | 空（仅 16B 头） | 同上 | 递增 |
> | 0x0006 | 会话初始化一次 | 1 | 空 | — | 2 |
>
> 结论：KEEPALIVE/DATA 均**单向服务器→客户端**，f8/fc 恒为（服务器SID，客户端SID）不随包变化，
> 与"对调回传"无关（对调仅发生在客户端应答服务器探测时）。seq 在各自信道上独立、持续递增。
> payload 递增步长=0x6400/跳（LE 读 [1:5]），疑似时间/采样计数器。

---

## T5 — Mailslot ExecCmd sub_cmd 语义（P2#11-15）

> 本机 GUI 运行即可验证，无需对端。当前 worktree **缺探针脚本**，先做存在性/响应探活，
> 再用 query_civ 逐 sub_cmd 手动跑。

**命令序列**：
```
# ① 确认 Mailslot 存在 + 探活写入 (cmd_code=0 GetCountClientTrans, 无副作用)
python scripts/mailslot_probe.py

# ② 确认响应通道可独占创建
python tools/probe_res_mailslot.py

# ③ 逐个 sub_cmd 0-5 发只读查询，观察 CI-V 是否被触发、响应是否回
python tools/tmp_probe_subcmd.py --sub-cmds 0,1,2,3   # sub_cmd 0-3
python tools/tmp_probe_subcmd_ctrl.py                # 实验A对照 + sub_cmd 4,5
```
（可选）重建自动遍历探针 `tools/probe_mailslot_semantics.py`：复用 `rsba1.mailslot`
的 `MailslotClient` / `ResponseReader` / `build_exec_cmd`，遍历 `cmd_code=2` + `sub_cmd 0-5`，
带 CI-V 载荷，打印写/读结果。

**判定**：锁定 `packet[0x14]` 六个取值与跳表映射、`SendMessageA` hwnd/wParam/lParam
业务含义、sub_cmd_3 操作码 0-3、sub_cmd_4/5 事件语义、handler 是否间接触发 CI-V（预期否）。
回填 `re/protocols/exec_cmd_subcmd.md` §8。

> ✅ 2026-08-18 本机实测（`tmp_probe_subcmd.py` / `tmp_probe_subcmd_ctrl.py`）：
> - 前置闭环：`RemoteUtyCtrlCmd` 存在且可写；`RemoteUtyCtrlRes` 可被陆墨独占创建。
> - **sub_cmd 0-5 均返回相同固定响应 `ff000000`**（4 字节，cmd字节=0xFF，
>   CI-V 数据 sub_cmd 0-5，首字节 0xFF 既非 cmd echo 0x02 也非 CI-V 帧）。
> - **对照实验**：仅创建 reader 不发命令 → 无任何包，证明 `ff000000` 是
>   RemoteUtility 对 ExecCmd 的固定应答，非独立事件广播。
> - 静态跳表（0x43a3f0/0x43a5f0/0x43a800/0x43aa70）的**业务级差异**（CI-V 触发、
>   SendMessageA 参数语义）无法仅凭 Mailslot 单通道区分；sub_cmd 语义仍存疑。
> - 结合 P4/T1：**CI-V 应答不经 Mailslot**，正确读路走 UDP Serial(50002)。

---

## T6 — Audio 50003 实机收发

**目标**：确认音频包 `0x18` 头 + `[0x16]BE16` 载荷长 + 12000Hz LPCM 编解码。

**命令**：Wireshark 抓 50003，观察音频包；有波形工具则校验 PCM/µ-law。
（无解码验证工具可只确认包格式，编解码验证标"部分"。）

> ⚠️ 当前环境结论（2026-08-18）：**Audio(50003) 会话无法在纯工控下建立**。
> 依据 `serial_channel.md` §5.11：服务器开放 50003 对端需先在 Command(50001)
> 发送 `0x90 request-serial-audio` 请求；而该请求前提是 Command 认证链
> （ConnectServer 等）闭环——T3 已证该链被 keepalive 洪泛淹没、ConnectServer
> 变体均无 ack。`pktmon_out` 经查**无任何 50003 抓包产物**。
> 故 T6 依赖前置项（T3 Command 认证闭环 或 真实 RemoteUtility 客户端→服务器
> 逐条抓包）。已记录为**阻塞/待前置**，不强行伪造音频包格式结论。
> 需音频包 `0x18` 头 / `[0x16]BE16` / 12000Hz LPCM 的 wire 级确证，后续在
> Command 认证可用环境用 `probe_command_connect.py` + pktmon 50003 补齐。

---

## T7 — 真机 live E2E（另一主机完整链路）

**目标**：客户端所在机器整链路：Command 登录 → Serial 读频率 → 解析。

**前置**：客户端主机设置 `RSBA1_E2E_HOST` 指向有可用会话的服务器；需在客户端那台机器跑
（方向/前置与当前环境不符，故生成此手动清单）。

**命令**（见 `tests/test_e2e_serial_command.py` LiveE2ETest 的用法）：
```
set RSBA1_E2E_HOST=<服务器IP>
python -m pytest tests/test_e2e_serial_command.py -k LiveE2E -v
```

**判定**：登录 + 读频率全链路绿。

> ⚠️ 2026-08-18 IC-705 直连实测结论（改判 `e2e_civ_loop.py` 的适用前提）：
> - **CI-V 经 Serial(50002) 直通，无需先走 Command 认证**（from 地址须为 0x00，见 T1）。
> - 但该直通依赖**705 侧已存在"已授权/已激活"控制会话**（通常由 RemoteUty.exe 持有）。
>   停止 RemoteUty 后，705 只把裸 Serial 客户端当"未授权旁听"：仅回显命令、不驱动电台、
>   不推 CI-V 应答（详见 `re/protocols/serial_channel.md` §5.14）。
> - 因此 T7 的"Command 登录 → Serial 读频率"仅在已有 RemoteUty 授权会话护航下成立；
>   自建 Command 授权会话（替换 RemoteUty）仍属待定疑项（T3，keepalive 洪泛阻断 ack）。

---

## T8 — 安全 / 收尾（开始真机验证前必查）

1. 抓包产物仅留在被 gitignore 的路径；提交前跑 `git status` 确认无 `.pcap/.etl/.hex/.log` 混入。
2. 抓包可能含加密凭据密文 + 主机信息；解析脚本**只落 hex、不落明文凭据**。
3. 本清单验证完成后，回填上方各节"判定"到对应 `re/protocols/*.md`，并把 capture_todo 对应项划掉。

---

## §A 安全约束（每次发包前）

- 频率仅限业余频段白名单：1.8–30MHz、50–54MHz、144–148MHz。
  （`civ_commands.is_allowed_freq` / `build_set_freq_payload` 已强制校验）
- 慎用 PTT ON（CI-V `A4 00 1C 00 01`）——会触发 IC-705 发射，仅在确认频率合法且愿意发射时使用。
- 只读命令（读频率/读模式/读 S-meter）无安全风险，可放心用于各探针。

---

## 执行顺序建议

1. **T0** 前置确认 → **T1** 回程（最高优先，决定能否闭环）→ **T8** 安全核查。
2. **T2** 端序定案（T1 用到）→ **T3/T4** Command/心跳抓包（有对端时）。
3. **T5** Mailslot sub_cmd（本机 GUI 即可，可与 1-2 并行准备）。
4. **T6** Audio、**T7** 另一主机 live E2E（硬件/环境到位后）。

> 非阻塞提示：T1 回程 + T2 端序是唯一"差一步"的核心闭环；其余按优先级补。