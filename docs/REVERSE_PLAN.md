# RS-BA1 完全逆向工程计划 — rsba1-core

> 仓库：`fe1iscurc0r/rsba1-core` (Gitee, private)
> 创建：2026-08-09
> 目标：粒度 3 完全逆向 — DLL 原型还原 + 协议栈重写 + 纯 Python 可替代实现 + 自定义控制内核
> 战略目的：摆脱对 ICOM 二进制组件的依赖，支持深度自定义（多电台调度、自动化脚本、Web 控制、与陆墨深度集成）

---

## 0. 仓库目录结构

```
rsba1-core/
├── docs/                      # 文档
│   ├── REVERSE_PLAN.md        # 本文件 — 逆向总计划
│   ├── PROGRESS.md            # 进度追踪
│   ├── conventions/           # 逆向与编码约定
│   └── architecture/          # 架构设计文档
├── re/                        # 逆向工程笔记（每组件独立目录）
│   ├── civctrl/               # CivCtrl.dll 逆向
│   │   ├── exports.md         # 18 导出函数签名 + 伪代码
│   │   ├── structures.md      # CIVDriver 等结构体布局
│   │   ├── protocols/         # CI-V 协议细节
│   │   ├── disasm/            # 反汇编输出
│   │   └── notes/             # 分析笔记
│   ├── utyctrl/               # UtyCtrl.dll 逆向
│   ├── radiosch/              # RadioSch.dll 逆向
│   ├── hidctrl/               # HidCtrl.dll 逆向
│   ├── remotectrl/            # RemoteCtrl.exe 逆向
│   ├── remoteuty/             # RemoteUty.exe 逆向
│   ├── license/               # RS-BA1V2Ck.dll / UtilityCk.dll 许可证逆向
│   ├── protocols/             # CI-V / CUDPCtrl2 / Mailslot 协议文档
│   └── notes/                 # 通用逆向笔记
├── src/                       # 纯 Python 可运行实现（逆向产物）
│   └── rsba1/
│       ├── __init__.py
│       ├── ctypes_wrappers/   # ctypes 包装层（粒度 2 过渡用）
│       │   ├── civctrl.py
│       │   ├── utyctrl.py
│       │   └── radiosch.py
│       ├── protocols/         # 协议栈重写（粒度 3 目标）
│       │   ├── ci_v.py        # CI-V 帧编解码
│       │   ├── cudp_ctrl2.py  # CUDPCtrl2 可靠 UDP 栈
│       │   └── mailslot_ipc.py
│       ├── transports/        # 传输层
│       │   ├── serial_transport.py
│       │   ├── hid_transport.py
│       │   └── udp_transport.py
│       ├── radio/             # 电台抽象层
│       │   ├── base.py        # Radio 抽象基类
│       │   ├── ic705.py       # IC-705 专用实现
│       │   └── models.py      # 电台型号注册表
│       └── cli/               # CLI 工具（独立使用）
├── tests/                     # 测试（mock + com0com + 硬件）
├── tools/                     # 逆向辅助工具
│   ├── pe_analysis/           # PE 分析脚本
│   ├── disasm/                # 反汇编辅助
│   └── packet_capture/        # 抓包分析工具
├── data/                      # 参考数据
│   ├── ini/                   # 21 个电台 INI 配置
│   ├── reference/             # 官方手册、CHM 提取
│   └── packet_captures/       # Wireshark 抓包文件（.pcap）
└── scripts/                   # 构建/测试脚本
```

---

## 1. 逆向目标与定义

### 1.1 逆向定义（粒度 3）

"完全逆向"达成标准：
- **文档完整**：每个 DLL/EXE 的导出函数 100% 有签名、伪代码、调用关系文档
- **结构精确**：核心 C++ 对象（CIVDriver / CUDPCtrl2 等）布局字节级精确
- **可替代**：`src/rsba1/` 纯 Python 实现能在不依赖任何 ICOM 二进制文件的情况下，完成以下最小功能集：
  - 连接电台（串口 / USB HID / UDP WLAN 三种方式）
  - 频率读取与设置
  - 模式读取与设置
  - PTT 控制
  - S-meter 读取
- **协议独立**：`protocols/cudp_ctrl2.py` 实现完整握手/重传/心跳/同步逻辑
- **可扩展**：新增电台型号只需添加型号配置，不需改核心逻辑

### 1.2 不做的事（边界）

- **不做** RemoteCtrl.exe 42.8MB 的完整源码级反编译（Delphi VCL 层无复用价值）
- **不做** GUI 重写（陆墨通过 MCP 控制，不依赖桌面 UI）
- **不做** 音频通道重写（初始阶段跳过 50002/50003 音频，聚焦 50001 控制通道）
- **不做** 许可证绕过（保留合法使用，仅做技术分析）

---

## 2. 组件体量总览

| 组件 | 编译器 | .text 代码段 | 导出数 | 逆向优先级 |
|---|---|---|---|---|
| CivCtrl.dll (148KB) | Borland C++ | ~90KB | 18 | **P0** |
| UtyCtrl.dll (200KB) | MSVC 9.0 + MFC | ~120KB | 9 | **P0** |
| HidCtrl.dll (16KB) | MSVC | ~10KB | 10 | P1 |
| RadioSch.dll (1.97MB) | MSVC 9.0 | ~1.2MB | 5 | **P0** |
| RemoteUty.exe (3MB) | MSVC 9.0 | ~1.5MB | 0 | **P0** |
| RS-BA1V2Ck.dll (1.9MB) | MSVC 9.0 | ~1MB | 1 | P2 |
| UtilityCk.dll (200KB) | MSVC 9.0 | ~120KB | 1 | P2 |
| RemoteCtrl.exe (42.8MB) | Delphi VCL | **2.1MB** | 300 | P1 |
| english.dll (812KB) | — | — | 0 | P3 |

**总代码段：~5.2MB x86 机器码**

---

## 3. 分阶段计划（Phase 0 - Phase 6）

### Phase 0：基础建设 ✅ 已完成

| 任务 | 状态 | 交付 |
|---|---|---|
| 创建 Gitee 仓库 rsba1-core | ✅ | private repo |
| 初始化目录结构 | ✅ | 7 大类 30+ 子目录 |
| 编写本逆向计划 | 进行中 | REVERSE_PLAN.md |
| 迁移已有成果（rs-ba1-reverse Phase 1-3） | 待办 | PE 分析、PDF/CHM 提取、INI 解析 |

估时：**已包含在当前会话内**

---

### Phase 1：PE 全量分析深化（2-3 人日）

在已有 pe_analyzer.py 静态分析基础上做**每组件二阶分析**：

| 任务 | 交付 | 估时 |
|---|---|---|
| CivCtrl.dll：18 导出函数完整反汇编（每个函数 >200 条指令，不只入口） | `re/civctrl/disasm/` + `exports.md` 初版 | 4-6h |
| CivCtrl.dll：全局对象 + 字符串交叉引用定位 CIVDriver 创建/销毁点 | `re/civctrl/structures.md` 初版 | 2-3h |
| UtyCtrl.dll：9 导出函数完整反汇编 + Mailslot 读写点定位 | `re/utyctrl/disasm/` + `exports.md` 初版 | 4-6h |
| RadioSch.dll：CUdp / CUDPCtrl / CUDPCtrl2 三个类完整反汇编（构造 + 虚表方法） | `re/radiosch/classes.md` 初版 | 6-8h |
| RadioSch.dll：串口 `CreateFile("COMx")` + USB HID `CreateFile("HID")` 完整调用链 | `re/radiosch/io_flow.md` | 2-3h |
| RemoteUty.exe：UDP socket 创建/绑定/recv/send 全链路 + 注册表端口读取完整路径 | `re/remoteuty/network_flow.md` | 3-5h |
| RemoteCtrl.exe：300 导出函数按功能分类（UI / IPC / CI-V 命令层） | `re/remotectrl/exports_index.md` | 4-6h |

**小计：25-37h = 3-5 人日**

---

### Phase 2：DLL 原型还原（3-4 人日）★ 里程碑

核心目标：每个 DLL 导出函数有**精确 C 原型**（参数个数、类型、调用约定、返回值）。

| 任务 | 交付 | 估时 |
|---|---|---|
| CivCtrl.dll 18 导出函数完整 C 原型 + 结构体 | `re/civctrl/prototypes.h` + `structures.md` v2 | 4-6h |
| CivCtrl.dll：调用序列还原（Init → Open → Send → Recv → Close） | `re/civctrl/call_sequence.md` | 2-3h |
| CivCtrl.dll ctypes wrapper MVP（最小跑通） | `src/rsba1/ctypes_wrappers/civctrl.py` | 4-6h |
| UtyCtrl.dll 9 导出函数完整 C 原型 + Mailslot 命令包格式 | `re/utyctrl/prototypes.h` + `packet_format.md` | 4-6h |
| UtyCtrl.dll ctypes wrapper MVP | `src/rsba1/ctypes_wrappers/utyctrl.py` | 2-3h |
| RadioSch.dll 5 导出函数完整 C 原型 + USB 设备描述结构 | `re/radiosch/prototypes.h` + `structures.md` | 4-6h |
| RadioSch.dll ctypes wrapper MVP | `src/rsba1/ctypes_wrappers/radiosch.py` | 3-5h |
| 集成测试：ctypes wrapper 能搜设备、开设备、切频、读 S-meter | `tests/test_ctypes_mvp.py` | 3-5h |

**小计：26-40h = 3-5 人日**

---

### Phase 3：协议栈完整文档化（2-3 人日）

| 任务 | 交付 | 估时 |
|---|---|---|
| CI-V 协议：帧格式 + 运行时构造 + 220+ 命令字节格式 | `re/protocols/ci_v_full.md` | 4-6h |
| **CUDPCtrl2：握手包格式 + 序列号机制 + 重传策略 + 心跳包 + 同步请求**（抓包 + 二进制双验证） | `re/protocols/cudp_ctrl2_protocol.md` | **8-12h** |
| Mailslot IPC：`\\.\mailslot\civsend` / `\\.\mailslot\*` 完整命令格式 + 响应格式 | `re/protocols/mailslot_ipc.md` | 4-6h |
| 三路 UDP 信道（50001/2/3）作用域与同步机制 | `re/protocols/udp_channels.md` | 2-3h |

**小计：18-27h = 2-4 人日**

⚠️ CUDPCtrl2 依赖用户配合抓包（Wireshark 192.168.0.31 ↔ 本机 UDP），可能阻塞。

---

### Phase 4：纯 Python 协议栈重写（3-5 人日）★★ 里程碑

目标：`src/rsba1/protocols/` 三个协议模块独立可测，不依赖 ICOM DLL。

| 任务 | 交付 | 估时 |
|---|---|---|
| `ci_v.py`：CI-V 帧编解码 + 220 命令注册表 | `src/rsba1/protocols/ci_v.py` + 单测 | 6-8h |
| `cudp_ctrl2.py`：握手 + 会话 + 序列号 + 重传 + 心跳 | `src/rsba1/protocols/cudp_ctrl2.py` + 单测 | **12-18h** |
| `mailslot_ipc.py`：Windows Mailslot 客户端/服务器 | `src/rsba1/protocols/mailslot_ipc.py` + 单测 | 3-5h |
| 协议层 mock 测试 | `tests/test_protocols_*.py` | 3-5h |
| 协议层基准测试 | `tests/bench_protocols.py` | 1-2h |

**小计：25-38h = 3-5 人日**

---

### Phase 5：电台抽象 + 传输层实现（2-3 人日）

| 任务 | 交付 | 估时 |
|---|---|---|
| `serial_transport.py`：COM 端口 RS232（pyserial） | `src/rsba1/transports/serial_transport.py` | 2-3h |
| `hid_transport.py`：Windows HID（hidapi） | `src/rsba1/transports/hid_transport.py` | 4-6h |
| `udp_transport.py`：三通道 UDP + CUDPCtrl2 会话 | `src/rsba1/transports/udp_transport.py` | 4-6h |
| `radio/base.py`：Radio ABC（connect/freq/mode/ptt/s_meter/scan） | `src/rsba1/radio/base.py` | 2-3h |
| `radio/ic705.py`：IC-705 具体实现 + Transport 路由 | `src/rsba1/radio/ic705.py` + 单测 | 4-6h |
| `radio/models.py`：21 电台型号注册表 | `src/rsba1/radio/models.py` | 1-2h |
| `cli/__init__.py` + `__main__.py`：CLI MVP（6 命令） | `src/rsba1/cli/` | 3-5h |

**小计：20-31h = 3-4 人日**

---

### Phase 6：集成测试 + 硬件联调 + 文档（2-3 人日）

| 任务 | 交付 | 估时 |
|---|---|---|
| mock 全链路测试（假 Radio → 假 Transport → 假 Protocol） | `tests/test_radio_mock.py` | 3-5h |
| com0com 虚拟串口测试（CI-V 往返） | `tests/test_radio_com0com.py` | 3-5h |
| **硬件联调**：IC-705 三种 transport 端到端 | 测试报告 | 5-8h |
| MCP adapter（陆墨控制）| scratchpad 侧 | 4-6h |
| 完整 README + API 文档 + 架构图 | `docs/` | 2-3h |
| 进度文档 + 遗留 issue 清单 | `docs/PROGRESS.md` | 1-2h |

**小计：18-29h = 2-4 人日**

---

## 4. 总工期与里程碑

| 里程碑 | 估时 | 验收标准 |
|---|---|---|
| M0 基建完成 | 0h（当前会话） | 仓库 + 目录 + 计划文档 |
| M1 **DLL 原型还原**（Phase 1+2 结束） | **6-10 人日** | 3 个 ctypes wrapper + MVP 跑通切频 |
| M2 **协议栈文档**（Phase 3 结束） | **+2-4 人日**（累计 8-14） | CUDPCtrl2 协议文档含完整包格式 |
| M3 **纯 Python 替代**（Phase 4+5 结束） | **+5-9 人日**（累计 13-23） | 纯 Python 跑通 6 MVP 命令，不依赖 ICOM DLL |
| M4 **完全交付**（Phase 6 结束） | **+2-4 人日**（累计 15-27） | 硬件联调报告 + MCP adapter + 文档 |

**乐观：15 人日 / 悲观：27 人日 / 中位：约 20 人日**

按每天 4h 有效逆向时间（不连续，穿插硬件验证），实际日历时间：**5-7 周**。

---

## 5. 多智能体逆向流程

每个 DLL 组件遵循以下流程（沈遥主导）：

```
1. pe_analyzer.py 全量静态分析 → 结构初判
2. deep_disasm_v2.py 二阶反汇编（调用点上下文 + 字符串引用）
3. 沈遥智能体：逐函数结构化逆向（签名+伪代码+结构体）
4. 铁锚智能体：审查逆向准确性（调用约定、栈平衡、虚表偏移、IAT 对钩）
5. 写 ctypes wrapper MVP 动态验证
6. 文档归档到 re/ 子目录
```

---

## 6. 参考资料（已收集）

**二进制原件**：
- `d:\my git\RS-BA1\RemoteController\RemoteCtrl.exe` — 42.8MB Delphi VCL 主程序
- `d:\my git\RemoteUtility\RemoteUty.exe` — 3MB RemoteUtility 主程序
- `d:\my git\RemoteUtility\RadioSch.dll` — 1.97MB 电台调度核心
- `d:\my git\RS-BA1\RemoteController\CivCtrl.dll` — 148KB CI-V 协议层
- `d:\my git\RS-BA1\RemoteController\UtyCtrl.dll` — 200KB 控制层
- `d:\my git\RemoteUtility\HidCtrl.dll` — 16KB HID 传输层

**官方文档**：
- `d:\my git\icom RS-BA1 V2\RS-BA1_manual_ENG.pdf` — 91 页用户手册
- `d:\my git\RemoteUtility\RemoteUty_ENG.chm` — CHM 帮助（解包 214 文件）

**配置数据**：
- `d:\my git\RS-BA1\RemoteController\models\*.ini` — 21 个电台型号配置
- `d:\my git\RemoteUtility\models.ini` — 电台型号注册表
- `d:\my git\RemoteUtility\RadioSch.ini` — USB VID/PID 映射表

**已有逆向成果**：
- `d:\my git\rs-ba1-reverse\` — Phase 1-3 完成（PE 分析 + 深度分析 + 协议初版）
- `d:\my git\scratchpad\tools\pe_analysis_output\` — JSON/MD 全量静态分析

---

## 7. 风险与阻塞点

| 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|
| CUDPCtrl2 协议抓不到包 | 中 | 阻塞 Phase 3+4 | 退化为 Mailslot 桥接（B1）或独占串口（B0） |
| RemoteCtrl.exe Delphi RTTI 缺失 | 高 | Phase 1 RemoteCtrl 分析困难 | 不做源码级还原，只做字符串+导出分类 |
| 硬件不在身边无法验证 | 中 | 全链路验证阻塞 | mock + com0com 降级测试 |
| 你忙碌无法配合抓包 | 中 | CUDPCtrl2 文档阻塞 | 先推进 Phase 1+2，等你有空再抓 |
| C++ 对象布局推导错误 | 中 | 结构体不精确 → wrapper 崩溃 | 铁锚审查 + 动态验证修正 |

---

## 8. 立即启动的任务

1. ✅ 仓库创建
2. ✅ 目录结构初始化
3. ⏳ **REVERSE_PLAN.md**（本文档）编写中
4. 🔜 迁移 rs-ba1-reverse 已有成果
5. 🔜 沈遥：CivCtrl.dll 完整原型还原启动
