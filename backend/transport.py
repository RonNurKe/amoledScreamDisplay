# -*- coding: utf-8 -*-
"""
transport.py - 统一数据传输抽象层

设计目标：
1. 前端通过统一的 DataService 接收 JSON 数据，不关心底层传输方式；
2. 默认优先尝试蓝牙 / BLE（BluetoothTransport），
   但 Windows + Python 3.7 环境下缺少稳定的“BLE 外设”实现，
   因此本项目同时提供 WebSocketTransport（局域网推送）与
   HTTPPollingTransport（HTTP 轮询）作为兼容桥接模式，并自动降级；
3. 所有传输方式发送的数据均为统一 JSON 字符串（UTF-8）。

包含的传输实现：
- WebSocketTransport   局域网 WebSocket 推送（默认兼容模式，纯标准库实现 RFC6455）
- HTTPPollingTransport HTTP 轮询（前端定期 GET /api/data，最兜底的兼容模式）
- BluetoothTransport   蓝牙 / BLE（接口已定义，当前环境不可用时自动降级）
- MockTransport        调试用：把 JSON 打印到控制台

说明：本文件的 WebSocket 服务器为纯标准库（socket + threading）实现，
无需 websockets / aiohttp 等第三方依赖，降低 Python 3.7 环境的安装负担。
"""

import base64
import hashlib
import json
import socket
import struct
import threading

# WebSocket 协议魔法字符串（RFC 6455）
WS_MAGIC_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def to_json(data):
    """统一的数据序列化出口：所有传输层都使用它生成 JSON 字符串。"""
    return json.dumps(data, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 极简 WebSocket 服务器（RFC 6455 子集实现）
# ---------------------------------------------------------------------------

class WebSocketServer(object):
    """
    线程化的极简 WebSocket 服务器：
    - 完成 HTTP Upgrade 握手（Sec-WebSocket-Key / Sec-WebSocket-Accept）；
    - 周期性向所有客户端广播文本帧（监控数据 JSON）；
    - 处理客户端的 close / ping 帧以维持连接健康。
    仅实现服务端需要的最小子集，代码保持简短可读。
    """

    def __init__(self, host="0.0.0.0", port=8081, path="/ws", debug=False):
        self.host = host
        self.port = int(port)
        self.path = path
        self.debug = debug
        self._clients = {}  # conn -> {"lock": Lock, "addr": str}
        self._clients_lock = threading.Lock()
        self._listen_sock = None
        self._running = False
        # 可选回调：客户端握手完成并注册后调用，入参为 conn。
        # 供高频通道（如 keys.py）在接入瞬间补发一份完整状态。
        self.on_client_connect = None

    # ---- 生命周期 ----

    def start(self):
        self._listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen_sock.bind((self.host, self.port))
        self._listen_sock.listen(5)
        self._running = True
        thread = threading.Thread(target=self._accept_loop, name="ws-accept")
        thread.daemon = True
        thread.start()

    def stop(self):
        self._running = False
        if self._listen_sock is not None:
            try:
                self._listen_sock.close()
            except Exception:
                pass
        with self._clients_lock:
            conns = list(self._clients.keys())
        for conn in conns:
            self._drop(conn)

    @property
    def client_count(self):
        with self._clients_lock:
            return len(self._clients)

    # ---- 连接 accept 与客户端线程 ----

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._listen_sock.accept()
            except OSError:
                break  # 服务被关闭
            t = threading.Thread(target=self._handle_client,
                                 args=(conn, addr), daemon=True)
            t.start()

    def _handle_client(self, conn, addr):
        """握手 -> 注册 -> 读帧循环 -> 清理。"""
        try:
            path, headers = self._read_http_request(conn)
            key = headers.get("sec-websocket-key", "")
            upgrade = headers.get("upgrade", "").lower()
            if not key or "websocket" not in upgrade:
                conn.close()
                return
            accept = base64.b64encode(
                hashlib.sha1((key + WS_MAGIC_GUID).encode("ascii")).digest()
            ).decode("ascii")
            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Accept: %s\r\n\r\n" % accept
            )
            conn.sendall(response.encode("ascii"))

            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # 读超时用于周期性唤醒读循环、检查退出标志
            conn.settimeout(30.0)

            with self._clients_lock:
                self._clients[conn] = {"lock": threading.Lock(), "addr": str(addr)}
            if self.debug:
                print("[ws] 客户端已连接: %s (在线 %d)" % (addr, self.client_count))

            # 接入回调：在进入读循环前下发初始状态（失败不影响连接）
            if self.on_client_connect is not None:
                try:
                    self.on_client_connect(conn)
                except Exception:
                    pass

            self._read_frames(conn)
        except Exception:
            pass
        finally:
            self._drop(conn)
            if self.debug:
                print("[ws] 客户端断开: %s" % str(addr))

    # ---- HTTP 请求读取（握手用） ----

    @staticmethod
    def _read_http_request(conn):
        """读取 HTTP 头部直到空行，返回 (path, headers)。"""
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                raise IOError("连接在握手期间关闭")
            buf += chunk
            if len(buf) > 65536:
                raise IOError("HTTP 请求过大")
        head = buf.split(b"\r\n\r\n", 1)[0].decode("utf-8", "replace")
        lines = head.split("\r\n")
        parts = lines[0].split(" ")
        path = parts[1] if len(parts) > 1 else "/"
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        return path, headers

    # ---- 帧读取与解析（仅用于维持连接：close/ping/pong） ----

    def _read_frames(self, conn):
        buf = b""
        while self._running:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue  # 空闲超时，仅用于检查退出标志
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while True:
                frame, buf = self._parse_frame(buf)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:  # close
                    try:
                        self._send_frame(conn, payload[:2], 0x8)
                    except Exception:
                        pass
                    return
                if opcode == 0x9:  # ping -> pong
                    try:
                        self._send_frame(conn, payload, 0xA)
                    except Exception:
                        pass
                # 0xA pong、文本/二进制（前端不上行业务数据）直接忽略

    @staticmethod
    def _parse_frame(buf):
        """解析一个客户端帧（客户端帧必须带掩码）。返回 ((opcode, payload), 剩余字节)。"""
        if len(buf) < 2:
            return None, buf
        b1, b2 = buf[0], buf[1]
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F
        offset = 2
        if length == 126:
            if len(buf) < 4:
                return None, buf
            length = struct.unpack(">H", buf[2:4])[0]
            offset = 4
        elif length == 127:
            if len(buf) < 10:
                return None, buf
            length = struct.unpack(">Q", buf[2:10])[0]
            offset = 10
        if masked:
            if len(buf) < offset + 4:
                return None, buf
            mask = buf[offset:offset + 4]
            offset += 4
        if len(buf) < offset + length:
            return None, buf
        payload = buf[offset:offset + length]
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return (opcode, payload), buf[offset + length:]

    # ---- 帧发送与广播 ----

    @staticmethod
    def _send_frame(conn, payload, opcode):
        """发送一个服务端帧（服务端帧不掩码）。payload 可为 str 或 bytes。"""
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        header = bytearray()
        header.append(0x80 | opcode)  # FIN=1
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header.extend(struct.pack(">H", n))
        else:
            header.append(127)
            header.extend(struct.pack(">Q", n))
        conn.sendall(bytes(header) + payload)

    def broadcast_text(self, text):
        """向所有在线客户端广播一条文本消息，自动剔除失效连接。"""
        with self._clients_lock:
            snapshot = list(self._clients.items())
        dead = []
        for conn, meta in snapshot:
            try:
                with meta["lock"]:
                    self._send_frame(conn, text, 0x1)
            except Exception:
                dead.append(conn)
        for conn in dead:
            self._drop(conn)

    def _drop(self, conn):
        with self._clients_lock:
            self._clients.pop(conn, None)
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 传输层抽象与各实现
# ---------------------------------------------------------------------------

class BaseTransport(object):
    """传输层基类：定义统一接口。"""

    name = "base"
    available = True

    def __init__(self, config=None, debug=False):
        self.config = config or {}
        self.debug = bool(debug)

    def start(self):
        pass

    def stop(self):
        pass

    def send_json(self, data):
        """发送统一 JSON（dict 入参）。"""

    def status(self):
        return {"name": self.name, "available": self.available, "clients": 0}


class WebSocketTransport(BaseTransport):
    """局域网 WebSocket 推送（默认兼容桥接模式）。"""

    name = "websocket"

    def __init__(self, config=None, debug=False):
        BaseTransport.__init__(self, config, debug)
        self.server = WebSocketServer(
            host="0.0.0.0",
            port=int(self.config.get("websocket_port", 8091)),
            debug=self.debug,
        )

    def start(self):
        self.server.start()
        print("[transport] WebSocket 推送已启动: ws://0.0.0.0:%d/ws"
              % self.server.port)

    def stop(self):
        self.server.stop()

    def send_json(self, data):
        self.server.broadcast_text(to_json(data))

    def status(self):
        return {"name": self.name, "available": True,
                "clients": self.server.client_count}


class HTTPPollingTransport(BaseTransport):
    """
    HTTP 轮询传输（最兜底的兼容模式）。

    它是被动的：前端定期 GET /api/data 获取最新快照（由 DataStore 提供），
    后端无需主动推送，因此 send_json 为空操作。
    即使 WebSocket 被防火墙拦截，只要能打开页面，轮询就能工作。
    """

    name = "http-polling"


class BluetoothTransport(BaseTransport):
    """
    蓝牙 / BLE 传输（接口预留）。

    当前环境限制：
    - Web Bluetooth 只能在 HTTPS 或 localhost 安全上下文中使用；
    - Windows 上把 Python 3.7 进程模拟成 BLE 外设（GATT Server）缺少
      稳定可靠的官方方案（WinRT API 需要 Python 3.8+ 的 winsdk 等绑定）；
    - 因此默认 available=False，自动降级到 WebSocket / HTTP 轮询。

    如何接入真实蓝牙：参见 docs/bluetooth.md
    （替换本类的 start/send_json 实现，例如桥接一块真正的 ESP32 BLE 设备，
    或使用支持 BLE 外设的独立进程/外部蓝牙桥）。
    """

    name = "bluetooth"
    available = False
    unavailable_reason = (
        "Windows + Python 3.7 缺少稳定的 BLE 外设(GATT Server)实现，"
        "已自动降级为局域网兼容模式。详见 docs/bluetooth.md"
    )

    def __init__(self, config=None, debug=False):
        BaseTransport.__init__(self, config, debug)
        self.device_name = self.config.get("bluetooth_device_name", "WinMonitor")
        self.service_uuid = self.config.get("bluetooth_service_uuid", "")
        self.characteristic_uuid = self.config.get("bluetooth_characteristic_uuid", "")

    def start(self):
        print("[transport] 蓝牙传输已请求启用，但当前环境不可用：")
        print("[transport]   %s" % self.unavailable_reason)

    def status(self):
        return {"name": self.name, "available": False, "clients": 0,
                "reason": self.unavailable_reason}


class MockTransport(BaseTransport):
    """
    调试用传输：把要发送的 JSON 打印到控制台（截断显示）。
    在 config.debug=true 时随其他传输一起启动，便于观察数据。
    """

    name = "mock"

    def send_json(self, data):
        if self.debug:
            text = to_json(data)
            print("[mock] %s%s" % (text[:200], "..." if len(text) > 200 else ""))


# ---------------------------------------------------------------------------
# 传输管理器：负责组装、降级与统一广播
# ---------------------------------------------------------------------------

class TransportManager(object):
    """
    管理所有启用的传输层：
    - WebSocket 始终启动（兼容桥接的默认载体）；
    - enable_bluetooth=true 时加入 BluetoothTransport（不可用则打印降级原因）；
    - debug=true 时加入 MockTransport 打印数据流；
    - HTTP 轮询由 web_server 的 /api/data 天然支持，无需实例。

    publish() 是唯一的数据出口：为快照打上 transport 标记后，
    通过所有活动传输发送统一 JSON。
    """

    def __init__(self, config=None, debug=False):
        self.config = config or {}
        self.debug = bool(debug)
        self.transports = []
        self.primary_name = "websocket"

        # 1. WebSocket：默认兼容模式（始终启用）
        self.transports.append(WebSocketTransport(self.config, self.debug))

        # 2. 蓝牙：可选；不可用时自动降级并给出原因
        if self.config.get("enable_bluetooth"):
            bt = BluetoothTransport(self.config, self.debug)
            self.transports.append(bt)
            if bt.available:
                self.primary_name = "bluetooth"

        # 3. Mock：调试模式下打印数据流
        if self.debug:
            self.transports.append(MockTransport(self.config, self.debug))

    def start(self):
        for t in self.transports:
            if t.available:
                try:
                    t.start()
                except Exception as exc:
                    t.available = False
                    print("[transport] %s 启动失败: %s" % (t.name, exc))
                    if t.name == "websocket":
                        print("[transport] WebSocket 不可用，前端将自动使用 HTTP 轮询。")
        self._refresh_primary()

    def stop(self):
        for t in self.transports:
            try:
                t.stop()
            except Exception:
                pass

    def publish(self, snapshot):
        """为快照打上传输标记，并通过所有活动传输广播。"""
        if not snapshot:
            return
        data = dict(snapshot)
        data["transport"] = self.primary_name
        for t in self.transports:
            if t.available:
                try:
                    t.send_json(data)
                except Exception:
                    pass  # 单个传输失败不影响其他传输
        return data

    def _refresh_primary(self):
        if any(t.available and t.name == "bluetooth" for t in self.transports):
            self.primary_name = "bluetooth"
        elif any(t.available and t.name == "websocket" for t in self.transports):
            self.primary_name = "websocket"
        else:
            self.primary_name = "http-polling"

    def status(self):
        return {
            "primary": self.primary_name,
            "transports": [t.status() for t in self.transports],
        }
