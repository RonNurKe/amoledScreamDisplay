# -*- coding: utf-8 -*-
"""
web_server.py - Web 服务器模块

职责：
1. 托管 frontend 目录下的前端静态文件（安卓手机通过局域网访问）；
2. 提供 HTTP API：
   - GET /api/data    最新监控数据快照（HTTP 轮询兼容模式的数据来源）
   - GET /api/status  后端运行状态（传输层、扩展、运行时长等）
   - GET /api/config  前端需要的公开配置（端口、间隔、蓝牙 UUID 等）
3. 所有 API 响应均带 CORS 头，方便本地调试；
4. 可选 HTTPS（config.enable_https），证书生成方法见 README“常见问题”。

实现说明：基于标准库 http.server 的 ThreadingHTTPServer（线程池处理请求），
无任何第三方依赖；路径解析做了严格的安全校验（防目录穿越），
并使用 unquote 支持 URL 中的中文与空格。
"""

import json
import mimetypes
import os
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import agents_status

# 常见静态文件的字符集（mimetypes 不带 charset，需要手动补）
TEXT_TYPES = ("text/html", "text/css", "application/javascript",
              "application/json", "text/plain")

# AI 状态推送端点（末尾的段就是状态名）
AGENT_STATUS_ENDPOINTS = ("/thinking", "/reading", "/writing",
                          "/working", "/idle", "/error")

# 旧版插件直接 GET 这些裸路径（不带 source），统一归入 opencode 来源
OPENCODE_STATUS_ENDPOINTS = AGENT_STATUS_ENDPOINTS

# 前端心跳记录（诊断用）：src -> 最近一次心跳时间（epoch 秒）
_PINGS = {}
_PING_LOCK = threading.Lock()


class AppContext(object):
    """应用上下文：把 main.py 组装好的各模块传递给 HTTP Handler。"""

    def __init__(self, config, data_store, transport_manager,
                 frontend_dir, started_at, key_bus=None):
        self.config = config
        self.data_store = data_store
        self.transport_manager = transport_manager
        self.frontend_dir = os.path.abspath(frontend_dir)
        self.started_at = started_at  # time.monotonic()
        self.key_bus = key_bus        # 按键通道（可为 None）


class DashboardHTTPServer(ThreadingHTTPServer):
    """带应用上下文的线程池 HTTP 服务器。"""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler_class, app):
        ThreadingHTTPServer.__init__(self, address, handler_class)
        self.app = app


class DashboardHandler(BaseHTTPRequestHandler):
    """请求处理器：静态文件 + JSON API + CORS。"""
    app = None  # 类属性，create_server() 中赋值

    # ---- 基础工具 ----

    def _json_body(self, obj, status=200):
        """输出 JSON 响应（带 CORS 头）。"""
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        # 静默默认请求日志，保持控制台输出简洁（仅错误可见）
        if self.app and self.app.config.get("debug"):
            BaseHTTPRequestHandler.log_message(self, fmt, *args)

    # ---- HTTP 动词 ----

    def do_OPTIONS(self):
        """CORS 预检请求。"""
        self.send_response(204)
        self._cors_headers()
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        source = None
        # 兼容 /api/opencode/* 前缀：opencode-status-sync 的配置里 mapping
        # 可能统一写成 /api/opencode/thinking 等路径（多目标共用一份 mapping），
        # 这里剥掉前缀后按标准端点处理
        if path.startswith("/api/opencode/"):
            path = path[len("/api/opencode"):]
            source = "opencode"
        # 通用入口：/api/agents/<state>?source=<来源>&sid=&title=&project=
        elif path.startswith("/api/agents/"):
            path = path[len("/api/agents"):]
            source = (parse_qs(parsed.query).get("source") or ["other"])[0]
            if path not in AGENT_STATUS_ENDPOINTS:
                self._json_body({"error": "unknown state",
                                 "valid": list(AGENT_STATUS_ENDPOINTS)}, 404)
                return
        if path == "/api/data":
            self._handle_api_data()
        elif path == "/api/status":
            self._handle_api_status()
        elif path == "/api/config":
            self._handle_api_config()
        elif path == "/api/keys":
            self._handle_api_keys()
        elif path == "/api/ping":
            self._handle_api_ping(parsed.query)
        elif path in AGENT_STATUS_ENDPOINTS:
            self._handle_agent_status(path, parsed.query, source)
        elif path.startswith("/api/"):
            self._json_body({"error": "unknown api"}, 404)
        else:
            self._serve_static(path)

    def do_POST(self):
        """只用于其它 AI 代理的状态推送（JSON body 也可带参数）。"""
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/agents/"):
            state_path = path[len("/api/agents"):]
            if state_path not in AGENT_STATUS_ENDPOINTS:
                self._json_body({"error": "unknown state"}, 404)
                return
            body = {}
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if 0 < length <= 8192:
                    raw = self.rfile.read(length)
                    body = json.loads(raw.decode("utf-8", "ignore")) or {}
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            # 查询串优先于 body，便于快速 curl 调试
            query = parsed.query
            if body:
                extra = "&".join("%s=%s" % (k, v) for k, v in body.items()
                                 if isinstance(v, (str, int, float)))
                query = "&".join([q for q in (query, extra) if q])
            self._handle_agent_status(state_path, query,
                                      body.get("source") or "other")
            return
        self._json_body({"error": "unknown api"}, 404)

    # ---- API 实现 ----

    def _handle_agent_status(self, path, query="", source=None):
        """
        接收 AI 代理的状态推送（GET /thinking 等，或 /api/agents/thinking）。
        可选查询参数：source=来源、sid=会话ID、title=对话名、project=项目名，
        用于多来源/多会话/多项目区分；缺省时归入全局会话。
        """
        state = path.lstrip("/")

        def _first(key):
            values = parse_qs(query or "").get(key)
            return values[0] if values else None

        try:
            agents_status.record(state, source=source or _first("source"),
                                 sid=_first("sid"),
                                 title=_first("title"),
                                 project=_first("project"))
        except Exception:
            # 状态记录失败不影响 HTTP 响应，下一轮推送会覆盖
            pass
        self._json_body({"ok": True, "state": state, "source": source or "opencode"})

    def _handle_api_ping(self, query=""):
        """
        前端心跳（诊断用）：记录各来源最近一次心跳时间。
        src 参数区分来源（如 widget=计时器驱动、widget-data=数据驱动）。
        """
        values = parse_qs(query or "").get("src")
        src = (values[0] if values else "unknown")[:32]
        with _PING_LOCK:
            _PINGS[src] = time.time()
        self._json_body({"ok": True, "src": src})

    def _handle_api_data(self):
        """最新监控数据快照（HTTP 轮询模式数据源）。"""
        data = self.app.data_store.get()
        if data is None:
            self._json_body({"error": "no data yet", "data": None}, 503)
        else:
            # HTTP 轮询获取的数据打上 transport 标记
            out = dict(data)
            out["transport"] = "http-polling"
            self._json_body(out)

    def _handle_api_status(self):
        tm = self.app.transport_manager
        ext_list = []
        try:
            from extensions import list_extensions
            ext_list = list_extensions()
        except Exception:
            pass
        with _PING_LOCK:
            pings = dict(_PINGS)
        keys_status = None
        bus = getattr(self.app, "key_bus", None)
        if bus is not None:
            try:
                keys_status = bus.status()
            except Exception:
                keys_status = None
        self._json_body({
            "status": "running",
            "uptime_seconds": round(time.monotonic() - self.app.started_at, 1),
            "transport": tm.status(),
            "extensions": ext_list,
            "keys": keys_status,
            "mock": bool(self.app.config.get("use_mock")),
            "pings": {k: round(v, 1) for k, v in pings.items()},
        })

    def _handle_api_config(self):
        """输出前端需要的公开配置（不含服务器内部路径等敏感信息）。"""
        cfg = self.app.config
        keys_cfg = cfg.get("keys") or {}
        self._json_body({
            "collect_interval": cfg.get("collect_interval", 2.0),
            "static_port": cfg.get("static_port", 8080),
            "websocket_port": cfg.get("websocket_port", 8081),
            "keys_port": int(keys_cfg.get("port", 8092)),
            "keys_enabled": bool(keys_cfg.get("enabled", True)),
            "enable_https": bool(cfg.get("enable_https")),
            "enable_bluetooth": bool(cfg.get("enable_bluetooth")),
            "bluetooth_device_name": cfg.get("bluetooth_device_name", ""),
            "bluetooth_service_uuid": cfg.get("bluetooth_service_uuid", ""),
            "bluetooth_characteristic_uuid": cfg.get("bluetooth_characteristic_uuid", ""),
            "burn_in_shift": bool(cfg.get("burn_in_shift", True)),
            "use_mock": bool(cfg.get("use_mock")),
            "debug": bool(cfg.get("debug")),
        })

    def _handle_api_keys(self):
        """按键通道状态（诊断用：是否可用、在线客户端数、各设备计数）。"""
        bus = getattr(self.app, "key_bus", None)
        if bus is None:
            self._json_body({"enabled": False, "available": False,
                             "reason": "按键通道未初始化"}, 503)
            return
        try:
            self._json_body(bus.status())
        except Exception as exc:
            self._json_body({"error": str(exc)}, 500)

    # ---- 静态文件 ----

    def _serve_static(self, url_path):
        root = os.path.realpath(self.app.frontend_dir)
        rel = unquote(url_path).lstrip("/")
        if rel == "" or rel.endswith("/"):
            rel += "index.html"

        full = os.path.realpath(os.path.join(root, rel))
        # 防目录穿越：解析后的真实路径必须仍位于 frontend 目录内
        if not (full == root or full.startswith(root + os.sep)):
            self._send_text(403, "Forbidden")
            return
        if not os.path.isfile(full):
            self._send_text(404, "Not Found")
            return

        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype in TEXT_TYPES:
            ctype += "; charset=utf-8"

        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send_text(500, "Internal Server Error")
            return

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # index.html 与 service-worker.js 不缓存：
        #   html 不缓存保证前端更新即时生效；
        #   SW 脚本若被缓存，浏览器会跳过更新检查，改了代码也要等缓存过期才生效。
        # 图标等其它静态资源可短缓存
        if full.endswith(".html") or full.endswith("service-worker.js"):
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "max-age=300")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# 服务器工厂
# ---------------------------------------------------------------------------

def create_server(app):
    """
    创建并返回 HTTP 服务器实例（未开始 serve）。
    若 config.enable_https 为真，则用 SSL 包裹监听套接字。
    """
    cfg = app.config
    host = "0.0.0.0"
    port = int(cfg.get("static_port", 8090))
    # 把应用上下文挂到处理器类属性上，Handler 内通过 self.app 访问
    DashboardHandler.app = app
    server = DashboardHTTPServer((host, port), DashboardHandler, app)

    if cfg.get("enable_https"):
        cert = cfg.get("https_certfile", "certs/server.crt")
        key = cfg.get("https_keyfile", "certs/server.key")
        if os.path.isfile(cert) and os.path.isfile(key):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            print("[web] HTTPS 已启用 (证书: %s)" % cert)
        else:
            print("[web][警告] enable_https=true 但证书文件不存在，已回退为 HTTP。")
            print("[web][警告] 证书生成方法见 README『常见问题-页面不是安全上下文』。")

    return server
