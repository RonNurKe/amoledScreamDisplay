# -*- coding: utf-8 -*-
"""
extensions.py - 扩展数据注册机制

设计目标：
1. 主监控数据（CPU/内存/磁盘/网络）之外的一切数据都视为“扩展”；
   例如：时间、天气、某平台 Token 用量、自定义指标等。
2. 提供统一的注册接口：
       register_extension(name, collector_function, enabled=True)
   其中 collector_function() 返回一个 dict（扩展自身的数据字段）。
3. get_extension_data() 输出统一结构：
       {
           "clock":       {"enabled": true,  ...数据字段...},
           "weather":     {"enabled": false, ...数据字段...},
           "token_usage": {"enabled": false, ...数据字段...},
           "custom": [ {"name": "...", "enabled": true, "data": {...}}, ... ]
       }
   内置扩展（clock / weather / token_usage）单独占一个键；
   其他第三方注册的扩展统一放入 "custom" 列表，便于前端枚举展示。
4. 任何一个扩展出错都不影响主数据与其它扩展。
"""

import threading
import time
from collections import OrderedDict
from typing import Optional

import opencode_usage
import token_merge
import weather
import workbuddy_usage

# 内置扩展名（拥有独立的顶级键，前端有对应专用面板）
BUILTIN_NAMES = ("clock", "weather", "token_usage", "agents_status",
                 "opencode_status", "opencode_usage", "workbuddy_usage",
                 "token_usage_all")

# 兼容别名：旧前端/脚本读 extensions.opencode_status，双写一个版本后移除
ALIASES = {"agents_status": "opencode_status"}

# name -> {"func": callable, "enabled": bool}
_registry = OrderedDict()
_registry_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 注册机制
# ---------------------------------------------------------------------------

def register_extension(name, collector_function, enabled=True):
    """
    注册一个扩展数据采集器。

    :param name:               扩展唯一名称，例如 "weather"
    :param collector_function: 无参函数，返回 dict（扩展数据字段），失败可返回 {}
    :param enabled:            是否启用（禁用时仍会输出 enabled=false 的占位结构）
    :return:                   是否注册成功
    """
    if not name or not isinstance(name, str):
        return False
    if not callable(collector_function):
        return False
    with _registry_lock:
        _registry[name] = {
            "func": collector_function,
            "enabled": bool(enabled),
        }
    return True


def unregister_extension(name):
    """注销扩展。"""
    with _registry_lock:
        return _registry.pop(name, None) is not None


def set_extension_enabled(name, enabled):
    """运行时切换扩展开关（可用于后续接入 Web 配置接口）。"""
    with _registry_lock:
        item = _registry.get(name)
        if item is not None:
            item["enabled"] = bool(enabled)
            return True
    return False


def list_extensions():
    """列出所有已注册扩展的名称与状态（供 /api/status 使用）。"""
    with _registry_lock:
        return [{"name": n, "enabled": v["enabled"]} for n, v in _registry.items()]


# ---------------------------------------------------------------------------
# 数据输出
# ---------------------------------------------------------------------------

def _collect_one(name, item):
    """采集单个扩展，保证永不抛异常。"""
    result = {"enabled": item["enabled"]}
    if item["enabled"]:
        try:
            data = item["func"]()
            if isinstance(data, dict):
                result.update(data)
        except Exception:
            # 扩展数据出错时静默降级，保留 enabled 标记
            pass
    return result


def get_extension_data():
    """
    采集全部扩展数据，输出统一结构（见模块 docstring）。
    该函数在每次采集循环中被调用，其返回值会合并进主 JSON 的 "extensions" 字段。
    """
    output = {"custom": []}
    with _registry_lock:
        items = list(_registry.items())
    for name, item in items:
        data = _collect_one(name, item)
        if name in BUILTIN_NAMES:
            output[name] = data
            alias = ALIASES.get(name)
            if alias:
                # 同一份数据双写，旧字段名标记 deprecated（下个大版本删除）
                output[alias] = data
        else:
            output["custom"].append({"name": name, **data})
    return output


# ---------------------------------------------------------------------------
# 内置扩展：时间 / 天气占位 / Token 用量占位
# ---------------------------------------------------------------------------

def clock_data():
    """时间扩展：供时间面板显示时区、后端时间等信息。"""
    local = time.localtime()
    weekday_names = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
    return {
        "timezone": time.tzname[local.tm_isdst and 1 or 0] if time.tzname else "local",
        "backend_time": time.strftime("%Y-%m-%dT%H:%M:%S", local),
        "weekday": weekday_names[local.tm_wday],
        "epoch": int(time.time()),
    }


def weather_data():
    """
    天气扩展：Open-Meteo 实现（backend/weather.py，无需 API Key）。
    数据契约 {city, temperature, humidity, condition}，
    配置见 config.json 的 "weather" 段；未配置/失败时返回占位结构，
    前端显示"等待天气数据接入"。
    """
    return weather.current()


def token_usage_data():
    """
    Token 用量扩展（占位实现）：
    目前返回空数据结构，前端显示“等待 Token 数据接入”。
    接入真实平台时返回：
        {"provider": "openai", "used": 123456, "limit": 1000000,
         "remaining": 876544, "percent": 12.3}
    详见 docs/extensions.md。
    """
    return {
        "provider": "example",
        "used": None,
        "limit": None,
        "remaining": None,
        "percent": None,
    }


def token_usage_all_data(switches=None, list_limit=6):
    """
    合并 OpenCode + WorkBuddy 两路本地 token 用量。

    :param switches:   {"opencode": bool, "workbuddy": bool}，关闭的来源直接跳过
    :param list_limit: models / by_session 保留条数
    :return:           与单一来源同结构的汇总 payload；两路都关时返回 {}
    说明：两个来源各自带缓存，这里只是加法，不会真的重复读磁盘/数据库。
    """
    flags = switches or {}
    parts = []
    if flags.get("opencode", True):
        try:
            parts.append(opencode_usage.current())
        except Exception:
            pass
    if flags.get("workbuddy", True):
        try:
            parts.append(workbuddy_usage.current())
        except Exception:
            pass
    if not parts:
        return {}
    return token_merge.combine(parts, list_limit)


def register_builtin_extensions(config=None):
    """
    注册全部内置扩展，并根据 config["extensions"] 中的开关决定默认启用状态。
    在 main.py 启动时调用一次。
    """
    config = config or {}
    switches = config.get("extensions", {})
    # 天气 / 本地 token 用量模块：传入完整 config
    weather.configure(config)
    opencode_usage.configure(config)
    workbuddy_usage.configure(config)
    register_extension("clock", clock_data, enabled=switches.get("clock", True))
    register_extension("weather", weather_data, enabled=switches.get("weather", False))
    register_extension("token_usage", token_usage_data,
                       enabled=switches.get("token_usage", False))

    # 本地 OpenCode token 用量（直读 opencode.db，只读）
    register_extension("opencode_usage", opencode_usage.current,
                       enabled=switches.get("opencode_usage", True))

    # WorkBuddy token 用量（增量扫描 ~/.workbuddy{,-ai}/projects/**/*.jsonl）
    register_extension("workbuddy_usage", workbuddy_usage.current,
                       enabled=switches.get("workbuddy_usage", True))

    # 两路本地用量的合并视图（面板的 ALL 视图直接读这份，规则见 token_merge.py）
    _usage_switches = {
        "opencode": switches.get("opencode_usage", True),
        "workbuddy": switches.get("workbuddy_usage", True),
    }
    register_extension(
        "token_usage_all",
        lambda: token_usage_all_data(_usage_switches),
        enabled=switches.get("token_usage_all", True))

    # AI 代理（Agents）状态：OpenCode 插件推送 + WorkBuddy 观察器写入
    # 接收端点见 web_server.py，WorkBuddy 侧见 workbuddy_watch.py
    import agents_status
    agents_cfg = config.get("agents_status") or {}
    store = agents_status.get_store(
        stale_timeout=agents_cfg.get(
            "stale_timeout",
            config.get("opencode_stale_timeout",
                       agents_status.DEFAULT_STALE_TIMEOUT)))
    register_extension("agents_status", store.collector,
                       enabled=switches.get("agents_status",
                                            switches.get("opencode_status", True)))
