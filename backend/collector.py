# -*- coding: utf-8 -*-
"""
collector.py - 本机硬件数据采集模块

职责：
1. 通过 psutil 采集 CPU / 内存 / 磁盘 / 网络等硬件数据；
2. 输出统一的 JSON 结构（所有字段均有默认值，采集失败不会抛出异常）；
3. 提供 MockCollector（模拟数据采集器），在无 psutil 或演示模式下保证项目可运行；
4. 提供 DataStore（线程安全的最新数据快照存储），供 Web 服务与各传输层共享。

数据结构约定（与 README / plan.md 中的“数据格式要求”保持一致）：
{
    "timestamp": "2026-01-01T00:00:00",
    "source": "backend",          # backend = 真实采集, mock = 模拟数据
    "system": { ... },            # 系统硬件指标
    "extensions": { ... }         # 由 extensions.py 填充（在 main.py 中合并）
}
"""

import platform
import random
import socket
import threading
import time
from datetime import datetime

# psutil 为可选依赖：缺失时自动退化为 Mock 模式，保证项目仍能启动演示
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None
    PSUTIL_AVAILABLE = False

# 默认采集间隔（秒）
DEFAULT_INTERVAL = 2.0


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _safe_call(func, default=None, *args, **kwargs):
    """安全调用一个函数：出错时返回默认值，保证采集过程永不崩溃。"""
    try:
        return func(*args, **kwargs)
    except Exception:
        return default


def _now_iso():
    """当前本地时间，ISO 格式字符串（不带微秒）。"""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def get_lan_ip():
    """
    获取本机局域网 IP（不真正发包，仅借助 UDP socket 的路由探测）。
    失败时回退到 127.0.0.1。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))       # 不会真的发送数据
        ip = sock.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        sock.close()
    return ip


# ---------------------------------------------------------------------------
# 真实硬件采集器
# ---------------------------------------------------------------------------

class HardwareCollector(object):
    """基于 psutil 的硬件数据采集器。"""

    def __init__(self):
        self._last_net = None  # (time.monotonic, bytes_sent, bytes_recv)
        # 预热一次 cpu_percent：psutil 首次调用总是返回 0.0
        _safe_call(psutil.cpu_percent, None, interval=None) if PSUTIL_AVAILABLE else None

    # ---- 各子模块采集 ----

    def _cpu(self):
        return {
            "cpu_percent": _safe_call(psutil.cpu_percent, 0.0, interval=None),
            "cpu_count": _safe_call(psutil.cpu_count, 0),
        }

    def _memory(self):
        mem = _safe_call(psutil.virtual_memory, None)
        if mem is None:
            return {
                "memory_total": 0, "memory_used": 0,
                "memory_available": 0, "memory_percent": 0.0,
            }
        return {
            "memory_total": getattr(mem, "total", 0),
            "memory_used": getattr(mem, "used", 0),
            "memory_available": getattr(mem, "available", 0),
            "memory_percent": getattr(mem, "percent", 0.0),
        }

    def _disks(self):
        """
        采集所有磁盘分区。
        返回 (disk_usage_summary, partitions_list)。
        disk_usage_summary 为所有分区的汇总值，同时会以
        "disk" 与 "disk_usage_summary" 两个键输出（兼容两种字段命名要求）。
        """
        partitions = []
        total = used = free = 0
        try:
            raw_parts = psutil.disk_partitions(all=False)
        except Exception:
            raw_parts = []

        for p in raw_parts:
            usage = _safe_call(psutil.disk_usage, None, p.mountpoint)
            if usage is None:
                continue
            partitions.append({
                "device": getattr(p, "device", ""),
                "mountpoint": getattr(p, "mountpoint", ""),
                "fstype": getattr(p, "fstype", ""),
                "total": getattr(usage, "total", 0),
                "used": getattr(usage, "used", 0),
                "free": getattr(usage, "free", 0),
                "percent": getattr(usage, "percent", 0.0),
            })
            total += getattr(usage, "total", 0)
            used += getattr(usage, "used", 0)
            free += getattr(usage, "free", 0)

        summary = {
            "total": total,
            "used": used,
            "free": free,
            "percent": round(used * 100.0 / total, 1) if total > 0 else 0.0,
        }
        return summary, partitions

    def _network(self):
        """采集网络累计流量，并结合上次采样计算每秒上传/下载速率。"""
        info = {
            "bytes_sent": 0,
            "bytes_recv": 0,
            "send_speed": 0.0,
            "recv_speed": 0.0,
        }
        io = _safe_call(psutil.net_io_counters, None)
        if io is None:
            return info

        now = time.monotonic()
        if self._last_net is not None:
            last_time, last_sent, last_recv = self._last_net
            dt = now - last_time
            if dt > 0:
                # max(0) 防止计数器重置（如网卡重启）导致负速率
                info["send_speed"] = round(max(0.0, (io.bytes_sent - last_sent) / dt), 1)
                info["recv_speed"] = round(max(0.0, (io.bytes_recv - last_recv) / dt), 1)
        self._last_net = (now, io.bytes_sent, io.bytes_recv)

        info["bytes_sent"] = io.bytes_sent
        info["bytes_recv"] = io.bytes_recv
        return info

    # ---- 对外主入口 ----

    def collect(self):
        """采集一次完整数据，返回统一 JSON 结构（dict）。"""
        system = {
            "hostname": _safe_call(socket.gethostname, "UNKNOWN"),
            "os": _safe_call(lambda: "%s %s" % (platform.system(), platform.release()), "Unknown"),
            "cpu_percent": 0.0,
            "cpu_count": 0,
            "memory_total": 0,
            "memory_used": 0,
            "memory_available": 0,
            "memory_percent": 0.0,
            "disk": {"total": 0, "used": 0, "free": 0, "percent": 0.0},
            "disk_usage_summary": {"total": 0, "used": 0, "free": 0, "percent": 0.0},
            "disk_partitions": [],
            "network": {"bytes_sent": 0, "bytes_recv": 0,
                        "send_speed": 0.0, "recv_speed": 0.0},
        }

        if PSUTIL_AVAILABLE:
            try:
                system.update(self._cpu())
                system.update(self._memory())
                summary, parts = self._disks()
                system["disk"] = summary
                system["disk_usage_summary"] = summary  # 兼容另一种字段命名
                system["disk_partitions"] = parts
                system["network"] = self._network()
            except Exception:
                # 单次采集失败时保留默认值，下一轮继续
                pass

        return {
            "timestamp": _now_iso(),
            "source": "backend",
            "system": system,
        }


# ---------------------------------------------------------------------------
# 模拟数据采集器（Mock）
# ---------------------------------------------------------------------------

class MockCollector(object):
    """
    模拟数据采集器：
    - 无 psutil / 无硬件环境时用于演示；
    - 使用随机游走生成平滑变化的数值，肉眼看起来像真实数据。
    """

    def __init__(self):
        self._cpu = 20.0
        self._mem = 45.0
        self._net_up = 100.0 * 1024
        self._net_down = 800.0 * 1024
        self._host = "MOCK-PC"

    def _walk(self, value, low, high, step):
        """随机游走：让数值在 [low, high] 内平滑波动。"""
        value += random.uniform(-step, step)
        if value < low:
            value = low
        if value > high:
            value = high
        return value

    def collect(self):
        self._cpu = self._walk(self._cpu, 3.0, 95.0, 8.0)
        self._mem = self._walk(self._mem, 20.0, 90.0, 3.0)
        self._net_up = self._walk(self._net_up, 0, 2 * 1024 * 1024, 80 * 1024)
        self._net_down = self._walk(self._net_down, 0, 20 * 1024 * 1024, 400 * 1024)

        total_mem = 16 * 1024 ** 3
        used_mem = int(total_mem * self._mem / 100.0)
        disk_total = 1024 * 1024 ** 3
        disk_used = int(disk_total * 0.62)

        system = {
            "hostname": self._host,
            "os": "MockOS 10 (simulated)",
            "cpu_percent": round(self._cpu, 1),
            "cpu_count": 8,
            "memory_total": total_mem,
            "memory_used": used_mem,
            "memory_available": total_mem - used_mem,
            "memory_percent": round(self._mem, 1),
            "disk": {"total": disk_total, "used": disk_used,
                     "free": disk_total - disk_used, "percent": 62.0},
            "disk_usage_summary": {"total": disk_total, "used": disk_used,
                                   "free": disk_total - disk_used, "percent": 62.0},
            "disk_partitions": [{
                "device": "C:",
                "mountpoint": "C:\\",
                "fstype": "NTFS",
                "total": disk_total,
                "used": disk_used,
                "free": disk_total - disk_used,
                "percent": 62.0,
            }],
            "network": {
                "bytes_sent": int(time.time() * 1000),
                "bytes_recv": int(time.time() * 900),
                "send_speed": round(self._net_up, 1),
                "recv_speed": round(self._net_down, 1),
            },
        }
        return {
            "timestamp": _now_iso(),
            "source": "mock",
            "system": system,
        }


# ---------------------------------------------------------------------------
# 采集器工厂 + 数据快照存储
# ---------------------------------------------------------------------------

def create_collector(use_mock=False):
    """
    根据配置创建采集器：
    - use_mock=True 或 psutil 不可用时返回 MockCollector（保证项目始终可运行）。
    """
    if use_mock or not PSUTIL_AVAILABLE:
        return MockCollector()
    return HardwareCollector()


class DataStore(object):
    """线程安全的最新数据快照存储（Web 服务与传输层共享）。"""

    def __init__(self):
        self._data = None
        self._lock = threading.Lock()
        self._event = threading.Event()  # 首次数据到达信号

    def update(self, data):
        with self._lock:
            self._data = data
        self._event.set()

    def get(self):
        with self._lock:
            return self._data

    def wait_first(self, timeout=10.0):
        """等待第一份数据生成（用于启动自检）。"""
        return self._event.wait(timeout)
