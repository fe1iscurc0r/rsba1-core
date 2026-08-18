"""rsba1.mcp.server — FastMCP 服务定义 + CI-V 控制 tool 实现。

底层依赖:
    CivViaExecCmdSender (rsba1.mailslot.civ_via_execcmd) 提供两类原语:
        - fire-and-forget: send_*  (写 RemoteUtyCtrlCmd, 不读响应)
        - 闭环查询:        query_*  (写命令 + 读 RemoteUtyCtrlRes + 解析)
    前者在任何时候可用 (RemoteController 开关均可); 后者要求
    RemoteController.exe 未运行 (否则响应 mailslot 被占用)。

本模块把这些封装成 MCP tool。默认 tool 采用闭环查询 (query_*)
以返回结构化结果给陆墨; 优先用 query_* 实现, 并提供 set_freq/ptt
这类"写"操作 (本身无应答, 走 fire-and-forget)。

tool 清单:
    read_freq()           -> int 频率 Hz
    read_mode()           -> dict {mode_code, mode_name, filter}
    read_smeter()         -> int S-meter 原始数据字节
    set_freq(hz)          -> bool 设置业余频段内频率
    ptt(press: bool)      -> bool 控制 PTT (TX/RX)
    get_status()          -> dict 查询频率+模式+PTT 组合状态

注意: 物理前置条件见 __init__.py 模块 docstring。所有 tool 在 RemoteController
占用响应 mailslot 时, 闭环查询 (read_*) 会抛 ResponseTimeoutError。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from rsba1.mailslot.civ_via_execcmd import CivViaExecCmdSender
from rsba1.mailslot import civ_response as civresp


# ============================================================
# FastMCP 惰性加载
# ============================================================


def _get_fastmcp():
    """惰性导入 fastmcp, 未安装时给出清晰报错。"""
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise ImportError(
            "封装 MCP 接口需要安装 fastmcp:  `pip install fastmcp`"
        ) from exc
    return FastMCP


# ============================================================
# 服务工厂
# ============================================================


def create_server(
    name: str = "ic705-rsba1",
    *,
    to_addr: int = 0xA4,
    from_addr: int = 0x00,
    query_timeout_ms: int = 2000,
    sender: Optional[CivViaExecCmdSender] = None,
) -> Any:
    """创建并返回 FastMCP 服务实例 (含全部 tool 注册)。

    参数:
        name:            MCP 服务名 (客户端发现用)。
        to_addr:         目标电台 CI-V 地址 (默认 0xA4 = IC-705)。
        from_addr:       源控制器 CI-V 地址 (默认 0x00)。
        query_timeout_ms: 闭环查询超时 (ms, 默认 2000)。
        sender:          可选共享 CivViaExecCmdSender; None 时内部按参数创建,
                         并在服务生命周期内复用 (惰性打开 mailslot)。

    返回:
        fastmcp.FastMCP 实例 (已注册 read_freq/set_freq/read_mode/ptt/
        read_smeter/get_status 等 tool)。

    物理前置:
        - RemoteUty.exe 运行中
        - read_* 闭环查询要求 RemoteController.exe 未运行
    """
    FastMCP = _get_fastmcp()

    # 共享 sender: 惰性打开, 服务生命周期内复用。
    if sender is None:
        sender = CivViaExecCmdSender(
            to_addr=to_addr, from_addr=from_addr, sub_cmd=0
        )

    def _query(fn):
        """封装闭环查询: 确保 mailslot 已打开, 调用 fn。"""
        return fn()

    mcp = FastMCP(name)

    # ----------------------------------------------------------
    # read_freq — 闭环查询频率
    # ----------------------------------------------------------
    @mcp.tool()
    def read_freq() -> int:
        """读取电台当前 VFO 频率 (Hz)。

        返回:
            int - 频率 Hz, 如 14270000 = 14.270 MHz。

        异常时抛 Mailslot 错误 / 超时 (见工具说明)。
        """
        sender.open()
        try:
            return sender.query_freq(timeout_ms=query_timeout_ms)
        finally:
            pass

    # ----------------------------------------------------------
    # read_mode — 闭环查询模式
    # ----------------------------------------------------------
    @mcp.tool()
    def read_mode() -> Dict[str, Any]:
        """读取电台当前工作模式。

        返回:
            {
                "mode_code": int  模式码 (见 MODE_NAMES),
                "mode_name": str  模式名 (LSB/USB/AM/CW/FM/WFM...),
                "filter":    int  滤波器编号,
            }
        """
        sender.open()
        try:
            code, filt = sender.query_mode(timeout_ms=query_timeout_ms)
        finally:
            pass
        return {
            "mode_code": code,
            "mode_name": civresp.MODE_NAMES.get(code, "UNKNOWN"),
            "filter": filt,
        }

    # ----------------------------------------------------------
    # read_smeter — 闭环查询 S-meter
    # ----------------------------------------------------------
    @mcp.tool()
    def read_smeter() -> int:
        """读取电台 S-meter 原始数据字节。

        返回:
            int - S-meter 原始值 (0-255)。真机需查 S 表换算为 dB/S 档位显示。
        """
        sender.open()
        try:
            return sender.query_smeter(timeout_ms=query_timeout_ms)
        finally:
            pass

    # ----------------------------------------------------------
    # set_freq — 设置频率 (fire-and-forget)
    # ----------------------------------------------------------
    @mcp.tool()
    def set_freq(hz: int) -> bool:
        """设置电台 VFO 频率 (Hz)。

        安全约束: 仅允许业余频段 (1.8-30MHz / 50-54MHz / 144-148MHz),
        越界抛 ValueError。

        参数:
            hz: 频率 Hz, 如 14270000 = 14.270 MHz。

        返回:
            bool - 命令已写入 Mailslot (True)。为 fire-and-forget, 不等待应答。
        """
        sender.open()
        try:
            sender.send_set_freq(hz)
        finally:
            pass
        return True

    # ----------------------------------------------------------
    # ptt — 控制 PTT (fire-and-forget)
    # ----------------------------------------------------------
    @mcp.tool()
    def ptt(press: bool) -> bool:
        """控制电台 PTT 状态 (发射/接收)。

        参数:
            press: True=PTT 按下 (TX 发射), False=松开 (RX 接收)。

        返回:
            bool - 命令已写入 Mailslot (True)。为 fire-and-forget, 不等待应答。
        """
        sender.open()
        try:
            if press:
                sender.send_ptt_on()
            else:
                sender.send_ptt_off()
        finally:
            pass
        return True

    # ----------------------------------------------------------
    # get_status — 组合状态查询
    # ----------------------------------------------------------
    @mcp.tool()
    def get_status() -> Dict[str, Any]:
        """一站式读取电台当前频率 + 模式 + S-meter。

        返回:
            {
                "freq":    int 频率 Hz,
                "mode_code": int,
                "mode_name": str,
                "filter":  int,
                "smeter":  int S-meter 原始值,
            }

        说明: 依次执行三次闭环查询, 任一失败则对应字段为 None。
        """
        sender.open()
        status: Dict[str, Any] = {}
        try:
            status["freq"] = sender.query_freq(timeout_ms=query_timeout_ms)
        except Exception:
            status["freq"] = None
        try:
            code, filt = sender.query_mode(timeout_ms=query_timeout_ms)
            status["mode_code"] = code
            status["mode_name"] = civresp.MODE_NAMES.get(code, "UNKNOWN")
            status["filter"] = filt
        except Exception:
            status["mode_code"] = None
            status["mode_name"] = None
            status["filter"] = None
        try:
            status["smeter"] = sender.query_smeter(timeout_ms=query_timeout_ms)
        except Exception:
            status["smeter"] = None
        return status

    return mcp


__all__ = ["create_server"]