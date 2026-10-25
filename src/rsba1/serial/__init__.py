"""serial — RS-BA1 Command/Serial 信道 (UDP 50001/50002) 客户端层.

子模块:
    serial_codec:    UDP2 wire 头 + Serial 帧的序列化/反序列化 (纯代码层)
    serial_client:   UDP SerialClient (经 Serial 50002 发送/接收/解析 CI-V 响应)
    command_client:  UDP CommandClient (经 Command 50001 完成 ConnectServer 认证与会话管理)

双 API 并存 (2026-08-18 合并定案):
    新链路 (radio_link 直连电台, kappanhang 权威线序): passcode / build_login_request /
        build_auth_request / build_connect_trans_request / build_pkt3/6/7 / ...
    旧链路 (PC 服务器 RemoteUty.exe / build_command_header 兼容层): CommandClient /
        build_command_header / build_connect_request / build_keepalive_request /
        encode_icom_credential / parse_command_header / ...
    两者在 command_client 内共存, 导出面一并保留。
"""

from rsba1.serial.command_client import (
    CMD_CONNECT,
    CMD_CONNECTTRANS,
    CMD_DEBUG,
    CMD_DISCONNECT,
    CMD_DISCONNECTTRANS,
    CMD_GETINFO,
    CMD_HEADER_SIZE,
    CMD_KEEPALIVE,
    DEFAULT_COMMAND_PORT,
    VERSION_AUTH,
    VERSION_CONNECT,
    VERSION_CONNECT_TRANS,
    AuthFailedError,
    # 旧链路 API (PC 服务器 / build_command_header 兼容层)
    CommandClient,
    CommandClientError,
    CommandTimeoutError,
    build_auth_request,
    build_command_header,
    build_command_packet,
    build_connect_request,
    build_connect_trans_request,
    build_disconnect_pkt,
    build_idle_pkt0,
    build_keepalive_request,
    build_login_request,
    build_pkt3,
    build_pkt6,
    build_pkt7,
    build_transport_header,
    encode_icom_credential,
    extract_a8_reply_id,
    is_a8_packet,
    is_idle_pkt0,
    is_pkt7,
    make_local_sid,
    parse_auth_reply_magic,
    parse_command_header,
    parse_connect_trans_response,
    parse_login_response,
    # 新链路 API (直连电台, kappanhang 权威线序)
    passcode,
)
from rsba1.serial.serial_client import (
    DEFAULT_SERIAL_PORT,
    DEFAULT_SESSION_F8,
    DEFAULT_SESSION_FC,
    SerialClient,
    SerialClientError,
    SerialTimeoutError,
)
from rsba1.serial.serial_codec import (
    SERIAL_FLAGS_BASE,
    SERIAL_FLAGS_BULK,
    SERIAL_FRAME_HEADER_SIZE,
    UDP2_PKT_TYPE_DATA,
    UDP2_PKT_TYPE_KEEPALIVE,
    # 结构常量
    WIRE_HEADER_SIZE,
    SerialFrame,
    # 类型
    UDP2WireHeader,
    build_serial_frame,
    build_udp_packet,
    # 函数
    build_wire_header,
    parse_serial_frame,
    parse_udp_packet,
    parse_wire_header,
)

__all__ = [
    "WIRE_HEADER_SIZE",
    "SERIAL_FRAME_HEADER_SIZE",
    "UDP2_PKT_TYPE_DATA",
    "UDP2_PKT_TYPE_KEEPALIVE",
    "SERIAL_FLAGS_BASE",
    "SERIAL_FLAGS_BULK",
    "UDP2WireHeader",
    "SerialFrame",
    "build_wire_header",
    "parse_wire_header",
    "build_serial_frame",
    "parse_serial_frame",
    "build_udp_packet",
    "parse_udp_packet",
    "DEFAULT_SERIAL_PORT",
    "DEFAULT_SESSION_F8",
    "DEFAULT_SESSION_FC",
    "SerialClientError",
    "SerialTimeoutError",
    "SerialClient",
    "DEFAULT_COMMAND_PORT",
    "CMD_HEADER_SIZE",
    "CMD_CONNECT",
    "CMD_DISCONNECT",
    "CMD_GETINFO",
    "CMD_CONNECTTRANS",
    "CMD_DISCONNECTTRANS",
    "CMD_KEEPALIVE",
    "CMD_DEBUG",
    "VERSION_CONNECT",
    "VERSION_AUTH",
    "VERSION_CONNECT_TRANS",
    "CommandClientError",
    "CommandTimeoutError",
    "AuthFailedError",
    # 旧链路 API
    "CommandClient",
    "encode_icom_credential",
    "build_command_packet",
    "build_command_header",
    "parse_command_header",
    "build_connect_request",
    "build_keepalive_request",
    # 新链路 API
    "passcode",
    "make_local_sid",
    "build_transport_header",
    "build_pkt3",
    "build_pkt6",
    "build_disconnect_pkt",
    "build_pkt7",
    "build_idle_pkt0",
    "build_login_request",
    "build_auth_request",
    "build_connect_trans_request",
    "parse_login_response",
    "parse_connect_trans_response",
    "parse_auth_reply_magic",
    "is_pkt7",
    "is_idle_pkt0",
    "is_a8_packet",
    "extract_a8_reply_id",
]
