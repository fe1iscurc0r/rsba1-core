# RemoteUtility 完全逆向专项字据

> 立字据日期：2026-08-10
> 立字据人：沈遥（AI 逆向工程师）
> 目标组件：`RemoteUtility`（= `RemoteUty.exe` + 其 UDP 底层 `RadioSch.dll` 网络栈）
> 最终方向：**客户端复用**（模拟 `RemoteController` 客户端，走 UDP 50001 Command 信道 + CUDPCtrl2 协议），完成标准为**文档级逆向**。

---

## 0. 为什么窗口开到"完全逆向 RemoteUtility"

此前通过 Mailslot（`RemoteUtyCtrlCmd` + `cmd_code=2` ExecCmd + CI-V 转发）尝试控制 IC-705：

- ✅ **S-meter 有响应** — 命令确实进入了 RemoteUty 的处理链
- ❌ **频率设置 / PTT 无效果** — 被证实的原因：Mailslot 的 `sub_cmd` 处理器（`0x43a3f0/0x43a5f0/0x43a800/0x43aa70`）最终走 `SendMessageA(WM_USER+5)` 路由到 **GUI 窗口**，不直接驱动 UDP 网络栈去控制电台。

因此要让"智能体不再也能用"（客户端复用），**正路是复活 ICOM 官方的控制链路**：`RemoteController` ⇄ `RemoteUty.exe` ⇄ 电台。本字据把目标锁定为把这一条线上 RemoteUty 一侧**逆向到文档级**，让第三方 Python 客户端能补全链路的另一端。

---

## 1. "完全逆向"的书面定义（达成标准）

满足以下 **6 条** 即为"完全逆向 RemoteUtility"完成：

| # | 标准 | 交付物 |
|---|---|---|
| R1 | 三层 UDP 类（`CUdp` / `CUDPCtrl` / `CUDPCtrl2`）**每个字段字节级定位**，不再有"推测"标注 | `re/radiosch/classes.h` 去 `[x]`+`(推测)`，全部 `[确证]`；附反汇编证据行号 |
| R2 | **UDP2_HEADER 完整字节布局**：magic 值、flag 位、seq/ack 语义、payload 封装全部定死 | `re/protocols/cudp_ctrl2_protocol.md`（含抓包 hex 对照） |
| R3 | **CUDPCtrl2 状态机完整**：SYNC / FSYNC / NOP 心跳 / RESEND(NACK) / ACK / DISCONNECT 六种包类型的触发条件与字节载荷 | 同上 + `re/protocols/cudp_ctrl2_state_machine.md` |
| R4 | **Command 信道业务层**：`CServerCommandCtrl` 每个 Exec 命令（ConnectServer / GetInfo / ConnectTrans / KeepAlive / Disconnect / Debug）的请求/响应字节格式 | `re/protocols/command_channel_cmd.md` |
| R5 | **凭证 + 会话管理**：User1/User2 校验、csid/ssid 分配、连入→踢人→心跳超时全流程字节级 | 注册表结构 + UDP 包字段对照 |
| R6 | **客户端复用手册**：一份"如何用 Python 摸拟 RemoteController"的端到端报文序列（含握手示例 hex） | `docs/client_reuse_guide.md` |

> 注：本字据**不承诺**交付可运行的 Python UDP 栈（那属于后续 implementation 阶段）；只承诺"文档足以让一名工程师据此写出可工作的客户端"。

---

## 2. 现状基线（已确证，无需重做）

| 模块 | 状态 | 关键结论 |
|---|---|---|
| [network_flow.md](file:///c:/Users/ASUS/.trae-cn/worktrees/rs-ba1-reverse/feat-civ-via-execcmd-n9t6LM/re/remoteuty/network_flow.md) | ✅ | 纯 UDP；三路信道 50001/50002/50003；注册表端口；凭证明文；踢人逻辑 |
| [mailslot_server.md](file:///c:/Users/ASUS/.trae-cn/worktrees/rs-ba1-reverse/feat-civ-via-execcmd-n9t6LM/re/remoteuty/mailslot_server.md) | ✅ | 仅 1 个 Mailslot 服务端；`cmd_code` 9 跳表；`sub_cmd` 6 子表；无来源校验 |
| [classes.h](file:///c:/Users/ASUS/.trae-cn/worktrees/rs-ba1-reverse/feat-civ-via-execcmd-n9t6LM/re/radiosch/classes.h) | ⚠️ 大多"推测" | `CUdp` 大部分字段已确证；`CUDPCtrl2` 多数字段仍靠日志推断，**待动态验证** |
| 已确认方向 | ✅ | Mailslot 走 GUI，不做 CI-V 控制；**改用 UDP 50001 Command 信道** |

**核心欠账**：一切 CUDPCtrl2 的字节级细节（R2/R3）都依赖**抓包动态验证**，这是本字据最大的外部依赖。

---

## 3. 分阶段任务（R-U0 → R-U5）

### R-U0 环境与抓包准备（外部依赖前置）
- [ ] 确认 RemoteUtility 本机可运行、RS-BA1 GUI 在线
- [ ] 本机安装 Wireshark，确认能抓 UDP 50001/50002/50003
- [ ] 准备 IC-705（或至少能发起 RemoteController 连接的环境）
- **交付**：抓包环境就绪；若硬件/抓包不可用，本字据暂停于 R-U2

### R-U1 连接握手抓包（UDP 50001）
- [ ] 启动 RemoteUty，用 RemoteController 发起一次连接 → 抓 UDP 50001
- [ ] 解析首包：确认 `magic` 字节、首包类型（SYNC/FSYNC?）、seq/ack 初值、csid/ssid 载荷
- [ ] 对照 classes.h 修正 `UDP2_HEADER` 布局
- **交付**：`UDP2_HEADER` 字段定死 + 握手首个 hex 包记录

### R-U2 CUDPCtrl2 六类型包格式定死
- [ ] 抓心跳（NOP）确认 flag 字段与间隔
- [ ] 人为丢包/延迟触发 RESEND(NACK)，确认 `[startSeq,endSeq]` 载荷
- [ ] 抓 ACK、DISCONNECT 包
- **交付**：`cudp_ctrl2_protocol.md` 六种包类型全部有真实 hex 佐证

### R-U3 状态机 + 会话/凭证
- [ ] 关闭一个客户端再连，观察 SYNC×重传、FSYNC 协商序列
- [ ] 用错误凭证连接，抓"踢回"包；确认 User1/User2 校验路径
- [ ] 触发 KeepAlive 超时踢人，抓 DISCONNECT 时序
- **交付**：`cudp_ctrl2_state_machine.md` + 凭证/会话章节

### R-U4 Command 信道业务命令
- [ ] 逐条发 ConnectServer / GetInfo / ConnectTrans / KeepAlive / Disconnect
- [ ] 逐条记录请求字节 + 响应字节对应关系
- **交付**：`command_channel_cmd.md` 完整命令映射表

### R-U5 客户端复用手册（最终交付）
- [ ] 汇总 R2/R3/R4，写一份端到端"Python 摸拟 RemoteController"报文序列
- [ ] 含最小握手示例 hex + 频率读取/设置示例
- **交付**：`docs/client_reuse_guide.md`（R6 达成即字据完成）

---

## 4. 外部依赖与配合清单（需你配合）

| 依赖 | 用于 | 若缺失 |
|---|---|---|
| RS-BA1 GUI + RemoteUtility 在线 | R-U1 抓包 | 中止于 R-U2 |
| Wireshark（UDP 50001） | R-U1~R-U3 | 中止于 R-U2 |
| IC-705 硬件 | 验证命令是否真正驱动电台 | 可降级为"仅确证报文格式" |
| RemoteController 可运行 | 作为被复用的参考客户端 | 用 UtyCtrl.dll 替代抓参考报文 |

---

## 5. 边界（本字据明确不做）

- ❌ 不做 RemoteCtrl.exe 42.8MB 源码级反编译
- ❌ 不做音频信道（50003）与串口信道（50002）的完整逆推（仅记录格式，不作为控制主路径）
- ❌ 不做 Mailslot 控制的继续深挖（已证走 GUI，改走 UDP）
- ❌ 不做许可证绕过
- ❌ 不承诺交付可运行 Python UDP 栈（那是下一 implementation 阶段）

---

## 6. 风险

| 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|
| UDP 抓不到包（硬件/网络隔离） | 中 | 卡死 R-U1 | 退化为"静态字节级反汇编推格式"，标注为推断 |
| RemoteController 版本与 RemoteUty 不匹配，握手格式差异 | 低 | 误导格式 | 双端版本一致记录，抓包为主 |
| CUDPCtrl2 对象大小/偏移推断错误 | 中 | 文档字段错位 | 每字段附反汇编证据，动态验证优先 |

---

## 7. 签署

- **本字据验收标准**：R1~R6 全部交付物落盘，`classes.h` 无遗留"推测"项。
- **完成形态**：`docs/client_reuse_guide.md` 存在且可据之写出客户端。

> 立字据人：沈遥 · 2026-08-10