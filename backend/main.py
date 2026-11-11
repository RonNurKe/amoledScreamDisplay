# -*- coding: utf-8 -*-
"""
main.py - 后端程序入口

启动流程：
1. 读取 config.json（缺失/损坏时使用内置默认配置）；
2. 注册内置扩展（时间/天气/Token 用量/OpenCode AI 状态）；
3. 创建数据采集器（真实 psutil 采集 或 Mock 模拟）并启动采集线程；
4. 启动传输层（WebSocket 推送；可选蓝牙；HTTP 轮询由 web_server 天然支持）；
5. 启动 Web 服务器托管前端静态页面；
6. 在控制台打印局域网访问地址、前端 URL、WebSocket 地址、
   蓝牙/兼容模式状态，并可选打印访问二维码。

依赖：psutil（必需）、qrcode（可选，用于打印二维码）。
"""

import json
import os
import sys
import threading
import time
from datetime import datetime

# 保证中文路径下也能正确导入本目录模块
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import collector            # noqa: E402
import extensions           # noqa: E402
import keys                 # noqa: E402
import transport            # noqa: E402
import web_server           # noqa: E402

PROJECT_ROOT = os.path.dirname(BASE_DIR)
FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# 内置默认配置：config.json 缺失或字段缺失时逐项回退
DEFAULT_CONFIG = {
    "collect_interval": 2.0,          # 采集间隔（秒）
    "static_port": 8090,              # 静态页面 / HTTP API 端口
    "websocket_port": 8091,           # WebSocket 推送端口
    "enable_https": False,            # 是否启用 HTTPS
    "https_certfile": "certs/server.crt",
    "https_keyfile": "certs/server.key",
    "enable_bluetooth": False,        # 是否启用蓝牙模式
    "bluetooth_device_name": "WinMonitor",
    "bluetooth_service_uuid": "0000ffe0-0000-1000-8000-00805f9b34fb",
    "bluetooth_characteristic_uuid": "0000ffe1-0000-1000-8000-00805f9b34fb",
    "debug": False,                   # 调试模式（打印数据流/请求日志）
    "use_mock": False,                # 使用模拟数据（无 psutil 时也会自动启用）
    "extensions": {                   # 扩展开关
        "clock": True,
        "weather": False,
        "token_usage": False,
        "agents_status": True,        # AI 代理状态（OpenCode 插件 + WorkBuddy 观察器）
        "opencode_usage": True,       # 本地 OpenCode token 用量（直读 opencode.db）
    },
    "opencode_usage": {               # 本地 token 统计（只读，不干扰运行中的 opencode）
        "db_path": None,              # 留空 = ~/.local/share/opencode/opencode.db
        "cache_seconds": 60,          # 统计缓存（秒）
    },
    "opencode_stale_timeout": 300,    # [弃用] 旧键名，仅作 agents_status.stale_timeout 的回退
    "agents_status": {                # AI 代理状态（多来源）
        "stale_timeout": 300,         # 超时无推送视为过期（秒）
        "workbuddy": {                # WorkBuddy 会话观察器（读 .jsonl 推断状态）
            "enabled": True,
            "roots": ["~/.workbuddy/projects"],
            "scan_interval": 1.0,     # 轮询间隔（秒）
            "idle_quiet": 6.0,        # 文件安静多少秒判定空闲
            "max_sessions": 8,        # 最多上报多少个会话
            "only_projects": [],      # 非空时只保留项目名含这些子串的会话
        },
    },
    "weather": {                      # Open-Meteo 天气（免费无 Key，需外网）
        "city": "苏州",
        "latitude": None,             # 经纬度留空时用 city 自动地理解析
        "longitude": None,
        "update_minutes": 15,         # 实况刷新间隔（分钟，免费额度足够）
    },
    "burn_in_shift": True,            # 防烧屏开关（下发给前端使用）
    "keys": {                         # 全局按键采集（Raw Input，独立 WebSocket）
        "enabled": True,
        "port": 8092,                 # 按键事件推送端口（与主数据 8091 分开）
        "device_filter": "",          # 只统计设备路径含该子串的键盘，如 "VID_E118"
        "keymap": "keymap.vil",       # Vial 导出的键位表，用于层解析；留空则关闭
        "layer_idle_revert": 0.6,     # 空闲多少秒后把层号收回 0（矩阵可用时不生效）
        "matrix": {                   # 混合模式：直读固件矩阵（厂商接口 0xFF60）
            "enabled": True,
            "vid_pid": "E118:0001",
            "interval_ms": 20,        # 轮询间隔，20ms = 50Hz
            "retry_seconds": 5,       # 读不到时的重试间隔
        },
    },
}


def load_config(path):
    """加载配置文件并与默认配置合并（浅合并一层 + 扩展字典合并）。"""
    config = dict(DEFAULT_CONFIG)
    config["extensions"] = dict(DEFAULT_CONFIG["extensions"])
    try:
        with open(path, "r", encoding="utf-8") as fh:
            user_cfg = json.load(fh)
        if isinstance(user_cfg, dict):
            for key, value in user_cfg.items():
                if key == "extensions" and isinstance(value, dict):
                    config["extensions"].update(value)
                else:
                    config[key] = value
        print("[config] 已加载配置文件: %s" % path)
    except FileNotFoundError:
        print("[config] 未找到 config.json，使用默认配置。")
    except Exception as exc:
        print("[config] 配置文件解析失败(%s)，使用默认配置。" % exc)
    return config


def collect_loop(data_store, collector_obj, interval, stop_event, transport_manager=None):
    """
    采集线程主循环：
    采集硬件数据 -> 合并扩展数据 -> 通过传输层广播 -> 写入 DataStore。
    """
    if interval <= 0:
        interval = collector.DEFAULT_INTERVAL
    while not stop_event.is_set():
        started = time.monotonic()
        snapshot = collector_obj.collect()
        try:
            snapshot["extensions"] = extensions.get_extension_data()
        except Exception:
            snapshot["extensions"] = {"custom": []}
        # 先经传输层广播（打上 transport 标记），再把同一份数据写入快照
        stamped = snapshot
        if transport_manager is not None:
            try:
                stamped = transport_manager.publish(snapshot) or snapshot
            except Exception:
                stamped = snapshot
        data_store.update(stamped)
        # 让出时间片，保证采集间隔准确
        elapsed = time.monotonic() - started
        stop_event.wait(max(0.1, interval - elapsed))


def optional_qrcode(url):
    """
    可选功能：在控制台打印访问二维码。
    需要 `pip install qrcode`，未安装时静默跳过，不影响启动。
    """
    try:
        import qrcode
    except ImportError:
        print("[qrcode] 未安装 qrcode 库，跳过二维码生成（可选: pip install qrcode）")
        return
    try:
        qr = qrcode.QRCode(border=2)
        qr.add_data(url)
        qr.print_ascii(invert=True)
        print("手机扫码访问: %s" % url)
    except Exception as exc:
        print("[qrcode] 二维码生成失败: %s" % exc)


def print_banner(config, lan_ip):
    """打印启动信息横幅：所有访问地址与模式状态。"""
    static_port = config.get("static_port", 8080)
    ws_port = config.get("websocket_port", 8081)
    scheme = "https" if config.get("enable_https") else "http"
    ws_scheme = "wss" if config.get("enable_https") else "ws"
    frontend_url = "%s://%s:%d/" % (scheme, lan_ip, static_port)
    ws_url = "%s://%s:%d/ws" % (ws_scheme, lan_ip, ws_port)

    print("=" * 62)
    print("      Windows 硬件监控 AMOLED 手机仪表盘  Backend")
    print("=" * 62)
    print("  本机局域网地址 : %s://%s:%d  (API/静态资源)" % (scheme, lan_ip, static_port))
    print("  前端访问 URL   : %s" % frontend_url)
    print("  WebSocket 地址 : %s" % ws_url)
    keys_cfg = config.get("keys") or {}
    if keys_cfg.get("enabled", True):
        print("  按键通道       : %s://%s:%d/ws  (Raw Input 实时)"
              % (ws_scheme, lan_ip, int(keys_cfg.get("port", 8092))))
    else:
        print("  按键通道       : 未启用 (keys.enabled=false)")
    print("  数据采集间隔   : %.1f 秒" % float(config.get("collect_interval", 2.0)))
    print("  数据来源       : %s" % ("Mock 模拟数据" if config.get("use_mock")
                                        or not collector.PSUTIL_AVAILABLE
                                        else "psutil 真实采集"))
    print("  蓝牙模式       : %s" % ("已启用" if config.get("enable_bluetooth")
                                        else "未启用（兼容模式运行）"))
    if config.get("enable_bluetooth"):
        bt = transport.BluetoothTransport(config)
        print("                   可用性: %s" %
              ("可用" if bt.available else "不可用 -> 已自动降级"))
        if not bt.available:
            print("                   原因  : %s" % bt.unavailable_reason)
    print("  兼容模式       : WebSocket 推送 + HTTP 轮询（始终可用）")
    print("-" * 62)
    print("  请确保安卓手机与电脑处于同一局域网，")
    print("  并在手机 Chrome 中打开上方『前端访问 URL』。")
    print("  首次启动如无法访问，请检查 Windows 防火墙是否放行端口 %d/%d。"
          % (static_port, ws_port))
    print("=" * 62)
    return frontend_url


def main():
    started_at = time.monotonic()
    print("[main] 启动时间: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # 1. 配置
    config = load_config(CONFIG_PATH)
    debug = bool(config.get("debug"))

    # 2. 扩展注册
    extensions.register_builtin_extensions(config)

    # 3. 采集器与数据存储
    use_mock = bool(config.get("use_mock"))
    if not collector.PSUTIL_AVAILABLE and not use_mock:
        print("[main][警告] 未安装 psutil，自动切换为 Mock 模拟数据（pip install psutil 可启用真实采集）")
    collector_obj = collector.create_collector(use_mock=use_mock)
    data_store = collector.DataStore()

    # 4. 传输层（先于采集线程创建，采集循环中直接广播）
    transport_manager = transport.TransportManager(config, debug=debug)
    transport_manager.start()

    # 4-B. 按键通道（独立于 2 秒主数据流：Raw Input 采集 + 8092 端口推送）
    key_bus = keys.KeyBus(config, debug=debug)
    key_bus.start()
    # 同时把采集状态作为扩展挂进主快照，前端无需连按键通道也能看到概况
    try:
        extensions.register_extension(
            "keyboard", key_bus.extension_data,
            enabled=bool((config.get("keys") or {}).get("enabled", True)))
    except Exception as exc:
        print("[keys] 注册扩展失败: %s" % exc)

    # 4-C. WorkBuddy 会话观察器：扫 .jsonl 推断状态，写入 agents_status
    wb_watcher = None
    try:
        import agents_status
        import workbuddy_watch
        wb_watcher = workbuddy_watch.start(config, agents_status.get_store(),
                                           debug=debug)
    except Exception as exc:
        print("[workbuddy_watch] 启动失败（面板仅显示其它来源）: %s" % exc)

    stop_event = threading.Event()
    collect_thread = threading.Thread(
        target=collect_loop,
        args=(data_store, collector_obj, float(config.get("collect_interval", 2.0)),
              stop_event, transport_manager),
        name="collector",
    )
    collect_thread.daemon = True
    collect_thread.start()

    # 等待第一份数据，避免前端刚打开时拿到空数据
    data_store.wait_first(timeout=10.0)

    # 5. Web 服务器（托管前端 + API）
    app = web_server.AppContext(config, data_store, transport_manager,
                                FRONTEND_DIR, started_at, key_bus=key_bus)
    try:
        server = web_server.create_server(app)
    except OSError as exc:
        print("[main][错误] Web 服务器端口绑定失败: %s" % exc)
        print("[main][错误] 端口可能被占用，请修改 config.json 中的 static_port/websocket_port。")
        stop_event.set()
        transport_manager.stop()
        sys.exit(1)

    # 6. 控制台信息 + 可选二维码
    lan_ip = collector.get_lan_ip()
    frontend_url = print_banner(config, lan_ip)
    optional_qrcode(frontend_url)

    # 主线程运行 HTTP 服务，Ctrl+C 优雅退出
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[main] 收到退出信号，正在关闭...")
    finally:
        stop_event.set()
        transport_manager.stop()
        try:
            key_bus.stop()
        except Exception:
            pass
        if wb_watcher is not None:
            try:
                wb_watcher.stop()
            except Exception:
                pass
        try:
            server.shutdown()
        except Exception:
            pass
        print("[main] 已退出。")


if __name__ == "__main__":
    main()
