# RS-BA1 Reverse Engineering Project

**完全逆向 Icom RS-BA1 V2 远程控制软件** — 理解其通信协议并实现跨平台替代方案。

## 项目目标

| Phase | 目标 | 状态 |
|-------|------|------|
| Phase 0 | 原材料收集（已安装的 exe/DLL/INI） | ✅ 完成 |
| Phase 1 | 安装包拆解 + 文件提取 | ✅ 完成 |
| Phase 2 | PE 静态逆向核心 DLL（CivCtrl/HidCtrl/UtyCtrl） + 主程序 | ✅ 完成 |
| Phase 3 | CI-V 协议文档化 + 指令表完整映射 | ✅ 完成 |
| **Phase 4** | **Python 核心库（跨平台 CLI）** | **⬜ 进行中** |
| Phase 5 | C++/Qt6 桌面 GUI（Windows .exe） | ⬜ 远期 |
| Phase 6 | Android 前端（APK / Flutter） | ⬜ 远期 |

## 项目总览

### 架构演进路线

```
Phase 4 (当前)          Phase 5              Phase 6
───────────────────    ────────────────    ────────────────
Python 核心库           C++ Qt6 核心库       C++ Qt6 核心库
  ├── ci_v.py           ├── libciv/          ├── libciv/
  ├── udp_link.py       ├── libudp/          ├── libudp/
  ├── audio.py          ├── libaudio/        ├── libaudio/
  └── cli.py            │                    │
                        ├── Desktop GUI      ├── Desktop GUI
                        │ (Qt6 Widgets)      │ (Qt6 Widgets)
                        └── Server/CLI       │
                                             └── Android 前端
                                              (Qt6 for Android
                                               或 Flutter 桥接)

Python 阶段: 快速验证协议、跑通全链路
C++/Qt6 阶段: 高性能桌面 GUI + 跨平台原生体验
```

### 为什么分两阶段

Python 阶段解决 **"协议跑通了没"** 的问题——串口读写、CI-V 帧组包、UDP 三通道、音频流，这些逻辑用 Python 验证最快。

C++/Qt6 阶段解决 **"好不好用"** 的问题——频谱/瀑布图要高性能渲染、GUI 要原生手感、单二进制部署省心。

两阶段共享同一套接口定义（CI-V 帧格式、UDP 包结构、音频参数），协议在 Python 阶段定型后，Qt6 阶段直接翻译成 C++ 即可。

## 原材料

- `raw/exe/` — 已安装的 RS-BA1 程序（RemoteCtrl.exe + 4 个 DLL + dat）
- `raw/ini/` — 各 Icom 机型 CI-V 配置（含 IC-705）
- `raw/utility/` — RemoteUtility 套件（RemoteUty.exe + RadioSch.dll 等）

详见 [`docs/材料清单.md`](docs/材料清单.md)

## 仓库结构

```
rs-ba1-reverse/
├── raw/                    # 原始文件
│   ├── exe/               # RemoteCtrl.exe + DLL
│   ├── ini/               # 各机型模型 INI
│   └── utility/           # RemoteUtility 套件
├── phase1-extraction/     # 拆包脚本 + 提取结果
├── phase2-re/             # Ghidra 项目 + 分析笔记
├── phase3-protocol/       # 协议文档化
├── phase4-crossplat/      # 跨平台实现 ← 当前
│   ├── rsba1/             # Python 核心库包
│   │   ├── ci_v.py        # CI-V 串口协议
│   │   ├── udp_link.py    # UDP 三通道通信
│   │   ├── audio.py       # 音频流
│   │   ├── hid_link.py    # USB HID 备选
│   │   ├── models.py      # 机型配置
│   │   └── cli.py         # 命令行入口
│   ├── tests/             # 单元测试
│   └── setup.py           # 打包
├── phase5-qt6-desktop/    # Qt6 桌面 GUI（远期）
├── phase6-android/        # Android 前端（远期）
├── docs/                  # 文档
└── tools/                 # 辅助工具脚本
```

## 技术栈

### 当前 (Phase 4)
- **语言**: Python 3.11+
- **串口**: pyserial
- **UDP 网络**: asyncio / socket
- **音频**: pyaudio / sounddevice
- **频谱**: matplotlib / numpy（可选）
- **CLI**: click / argparse
- **Web 可选**: FastAPI + uvicorn

### 远期 (Phase 5-6)
- **语言**: C++17
- **框架**: Qt6 (Widgets + SerialPort + Network)
- **GUI 编译**: CMake + MinGW (Windows) / NDK (Android)
- **音频**: Qt6 Multimedia
- **频谱**: QCustomPlot / Qt6 Charts

## 逆向成果

### 已分析 DLL

| DLL | 大小 | 导出 | 功能 |
|-----|------|------|------|
| HidCtrl.dll | 16KB | 10 | HID 传输层（重叠 I/O + 接收线程） |
| CivCtrl.dll | 148KB | 18 | **CI-V 协议栈核心**（COM API 直连串口） |
| UtyCtrl.dll | 200KB | 9 | 远程传输/状态/音频管理层 |
| RS-BA1V2Ck.dll | 1.9MB | 1 | 许可证检查（含 GUI） |

### RemoteCtrl.exe 主程序

| 项目 | 值 |
|------|------|
| 大小 | 42.9 MB |
| 语言 | Borland Delphi（VCL 框架） |
| 单元 | 48 个功能模块 |
| 资源 | 930 张 BMP（38 MB）, 46 个 Delphi 表单 |
| 音效 | 6 个 WAV（BEEP_ERR/OK/STBY/WC/EXIT） |

### IC-705 CI-V 参数

| 参数 | 值 |
|------|-----|
| CI-V 地址 | **0xA4** |
| 控制器地址 | **0xE0** |
| 默认波特率 | 19200（遥控）/ 115200（远程工具） |
| 帧格式 | `FE FE TO FR DATA FD` |
| 频率编码 | 5 字节 BCD |

详见 `phase3-protocol/ci-v-spec.md`

## 许可

研究目的。RS-BA1 版权归 Icom Inc. 所有。
