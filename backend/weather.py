# -*- coding: utf-8 -*-
"""
weather.py - Open-Meteo 天气扩展实现（免费服务，无需 API Key）

数据源：https://open-meteo.com/
  - 实况:  https://api.open-meteo.com/v1/forecast?latitude=..&longitude=..
           &current=temperature_2m,relative_humidity_2m,weather_code&timezone=auto
  - 地理:  https://geocoding-api.open-meteo.com/v1/search?name=<城市>&count=1

设计要点：
1. 仅用标准库 urllib（Python 3.7 兼容，零第三方依赖）；
2. 低频刷新：默认 15 分钟拉一次（免费额度 1 万次/天，绰绰有余），
   采集线程每 2s 调 current() 只是读缓存，不会打爆 API；
3. 失败容错：网络失败时沿用上次成功数据并只打印一次告警，
   从未成功过则返回占位结构（前端显示"等待天气数据接入"）；
4. 城市名 -> 经纬度：未配置坐标时用 Open-Meteo 地理解析 API 自动换算
   （进程内缓存，只解析一次）；
5. 配置来源 config.json 的 "weather" 段（见 main.py DEFAULT_CONFIG）。
"""

import json
import threading
import time
import urllib.parse
import urllib.request

# WMO weather code -> 中文天气现象
WMO_CONDITIONS = {
    0: "晴", 1: "基本晴", 2: "局部多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}

_LOCK = threading.Lock()
_CFG = {}        # configure() 写入的配置
_GEO = None      # (lat, lon, name) 地理解析缓存
_LAST = None     # 最近一次成功获取的天气数据
_LAST_AT = 0.0   # 上次成功获取时刻（monotonic）
_FAIL_LOGGED = False


def configure(config):
    """main/extensions 启动时调用一次：读取 weather 配置段与 mock 开关。
    兼容两种入参：完整 config（含 "weather" 段）或 weather 配置段本身。"""
    global _CFG
    cfg = {}
    if isinstance(config, dict):
        section = config.get("weather")
        if isinstance(section, dict):
            cfg = dict(section)
        else:
            cfg = dict(config)
        cfg["use_mock"] = bool(config.get("use_mock"))
    _CFG = cfg


def _placeholder():
    """从未成功获取时的占位结构（前端据此显示等待接入）。"""
    return {
        # 兼容旧契约
        "city": _CFG.get("city") or "",
        "temperature": None,
        "humidity": None,
        "condition": "",
        # 扩展项
        "apparent_temperature": None,
        "is_day": None,
        "precipitation": None,
        "cloud_cover": None,
        "pressure": None,
        "wind_speed": None,
        "wind_direction": None,
        "uv_index": None,
        "temp_max": None,
        "temp_min": None,
        "sunrise": None,
        "sunset": None,
        "observed_at": None,
        "latitude": None,
        "longitude": None,
        "elevation": None,
    }


def _hhmm(value):
    """'2026-09-23T05:42' -> '05:42'（非法输入返回 None）。"""
    if not isinstance(value, str) or len(value) < 16:
        return None
    return value[11:16]


def _http_json(url, timeout=8.0):
    """GET 一个 JSON URL（标准库，8s 超时）。"""
    req = urllib.request.Request(
        url, headers={"User-Agent": "amoled-dashboard-weather/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _geocode(city):
    """城市名 -> (latitude, longitude, name)，进程内缓存只解析一次。"""
    global _GEO
    if _GEO is not None:
        return _GEO
    q = urllib.parse.urlencode(
        {"name": city, "count": 1, "language": "zh", "format": "json"})
    data = _http_json("https://geocoding-api.open-meteo.com/v1/search?" + q)
    results = data.get("results") or []
    if not results:
        raise ValueError("geocoding: 未找到城市 %r" % city)
    first = results[0]
    _GEO = (first.get("latitude"), first.get("longitude"),
            first.get("name") or city)
    return _GEO


def _fetch():
    """拉取一次实况天气并整理为统一契约结构。"""
    lat = _CFG.get("latitude")
    lon = _CFG.get("longitude")
    city = _CFG.get("city") or ""
    if (lat is None or lon is None) and city:
        lat, lon, _geo_name = _geocode(city)
    if lat is None or lon is None:
        raise ValueError("weather: 未配置 city 或 latitude/longitude")

    q = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "current": ",".join((
            "temperature_2m",
            "relative_humidity_2m",
            "apparent_temperature",
            "is_day",
            "precipitation",
            "weather_code",
            "cloud_cover",
            "pressure_msl",
            "wind_speed_10m",
            "wind_direction_10m",
            "uv_index",
        )),
        "daily": "temperature_2m_max,temperature_2m_min,sunrise,sunset",
        "timezone": "auto",
        "forecast_days": 1,
    })
    data = _http_json("https://api.open-meteo.com/v1/forecast?" + q)
    cur = data.get("current") or {}
    daily = data.get("daily") or {}

    def first_day(key):
        arr = daily.get(key)
        if isinstance(arr, list) and arr:
            return arr[0]
        return None

    code = cur.get("weather_code")
    return {
        # 兼容旧契约（熄屏飘屏组件等消费 city/temperature/humidity/condition）
        "city": city,
        "temperature": cur.get("temperature_2m"),
        "humidity": cur.get("relative_humidity_2m"),
        "condition": WMO_CONDITIONS.get(code, "") if code is not None else "",
        # 实况扩展项
        "apparent_temperature": cur.get("apparent_temperature"),
        "is_day": cur.get("is_day"),
        "precipitation": cur.get("precipitation"),
        "cloud_cover": cur.get("cloud_cover"),
        "pressure": cur.get("pressure_msl"),
        "wind_speed": cur.get("wind_speed_10m"),
        "wind_direction": cur.get("wind_direction_10m"),
        "uv_index": cur.get("uv_index"),
        "observed_at": cur.get("time"),
        # 今日概览（daily[0]）
        "temp_max": first_day("temperature_2m_max"),
        "temp_min": first_day("temperature_2m_min"),
        "sunrise": _hhmm(first_day("sunrise")),
        "sunset": _hhmm(first_day("sunset")),
        # 位置元数据
        "latitude": data.get("latitude"),
        "longitude": data.get("longitude"),
        "elevation": data.get("elevation"),
    }


def current():
    """
    扩展采集入口：返回天气契约结构
        {city, temperature, humidity, condition}
    带时间戳缓存：过期才真正联网刷新；失败沿用旧数据。
    """
    global _LAST, _LAST_AT, _FAIL_LOGGED
    if _CFG.get("use_mock"):
        return _placeholder()

    try:
        interval = float(_CFG.get("update_minutes", 15)) * 60
    except (TypeError, ValueError):
        interval = 15.0 * 60
    if interval < 60:
        interval = 60.0

    with _LOCK:
        expired = (time.monotonic() - _LAST_AT) >= interval
        if _LAST is None or expired:
            try:
                _LAST = _fetch()
                _LAST_AT = time.monotonic()
                _FAIL_LOGGED = False
            except Exception as exc:
                if not _FAIL_LOGGED:
                    print("[weather] 获取失败（继续使用旧数据）: %s" % exc)
                    _FAIL_LOGGED = True
        return dict(_LAST) if _LAST is not None else _placeholder()
