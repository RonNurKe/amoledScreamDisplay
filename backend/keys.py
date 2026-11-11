# -*- coding: utf-8 -*-
"""
keys.py - 全局按键采集（Windows Raw Input）+ 按键事件推送通道

为什么单独一条通道：
    主数据流（collector -> DataStore -> transport）是 2 秒一轮的快照，
    而按键是毫秒级事件，塞进快照会丢失绝大部分按键。因此本模块自带一个
    独立的 WebSocket 服务（默认 8092），收到按键立即推送。

采集方式：Raw Input
    RegisterRawInputDevices(usage page 0x01 / usage 0x06, RIDEV_INPUTSINK)
    + 一个隐藏窗口接收 WM_INPUT。
    与全局键盘钩子(WH_KEYBOARD_LL)相比：
      - 不注入任何进程，不修改系统输入链路，行为上不是"键盘记录器"；
      - 无需管理员权限（但受 UIPI 限制：前台窗口若以管理员运行，收不到按键）；
      - 能拿到 hDevice 句柄，因此可以区分是哪一台键盘、走 USB 还是蓝牙。

事件格式（尽量短，高频推送）：
    {"t":"k","code":"KeyA","vk":65,"sc":30,"down":true,"src":"usb","dev":"E118:0001","ts":169...}
    code 使用与浏览器 KeyboardEvent.code 相同的物理键名（按扫描码映射，
    与系统输入法/键盘布局无关）。

混合模式（方案 A + 方案 B）：
    Raw Input 拿到的是固件做完层解析之后的输出键码，层号在系统侧不可见。
    这里同时用两条来源：

      方案 A（始终可用）：若配置了 keys.keymap（Vial 导出的 .vil），
        用键位表反查「输出码 -> 物理位置 -> 当前层」，事件追加 rc / layer，
        换层时广播 {"t":"layer","n":N}。层键靠空闲超时近似（会慢 0.6s）。

      方案 B（矩阵直读，见 vialmatrix.py）：走厂商自定义接口 MI_02
        （Usage Page 0xFF60）直接读固件矩阵位图。层键、TD 长按、KC_NO 死键
        一按下就能看到，层号零延迟、松手立刻回 0。
        额外广播 {"t":"mx","held":["r,c",...],"layer":N} 供前端点亮这些键。

    矩阵读不到时（Vial 占用接口 / 未装 hidapi / 键盘未连）自动退回方案 A，
    不需要重启，接口一空出来就会自动接管。

依赖：标准库（ctypes）+ 可选 hidapi（矩阵直读）。
"""

import ctypes
import json
import os
import re
import sys
import threading
import time

import keymap
import vialmatrix

IS_WINDOWS = (sys.platform == "win32")

# ---------------------------------------------------------------------------
# 扫描码 -> KeyboardEvent.code（set 1，物理键位，与布局无关）
# ---------------------------------------------------------------------------

_SCAN_MAP = {}


def _add_scan(start, codes, extended=0):
    for i, code in enumerate(codes):
        _SCAN_MAP[(extended, start + i)] = code


_add_scan(0x02, ["Digit1", "Digit2", "Digit3", "Digit4", "Digit5",
                 "Digit6", "Digit7", "Digit8", "Digit9", "Digit0"])
_add_scan(0x10, ["KeyQ", "KeyW", "KeyE", "KeyR", "KeyT",
                 "KeyY", "KeyU", "KeyI", "KeyO", "KeyP"])
_add_scan(0x1E, ["KeyA", "KeyS", "KeyD", "KeyF", "KeyG",
                 "KeyH", "KeyJ", "KeyK", "KeyL"])
_add_scan(0x2C, ["KeyZ", "KeyX", "KeyC", "KeyV", "KeyB", "KeyN", "KeyM"])
_add_scan(0x3B, ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10"])

_SCAN_MAP.update({
    (0, 0x01): "Escape",
    (0, 0x0C): "Minus", (0, 0x0D): "Equal", (0, 0x0E): "Backspace",
    (0, 0x0F): "Tab",
    (0, 0x1A): "BracketLeft", (0, 0x1B): "BracketRight", (0, 0x1C): "Enter",
    (0, 0x1D): "ControlLeft",
    (0, 0x27): "Semicolon", (0, 0x28): "Quote", (0, 0x29): "Backquote",
    (0, 0x2A): "ShiftLeft", (0, 0x2B): "Backslash",
    (0, 0x33): "Comma", (0, 0x34): "Period", (0, 0x35): "Slash",
    (0, 0x36): "ShiftRight",
    (0, 0x37): "NumpadMultiply", (0, 0x38): "AltLeft", (0, 0x39): "Space",
    (0, 0x3A): "CapsLock",
    (0, 0x45): "NumLock", (0, 0x46): "ScrollLock",
    (0, 0x47): "Numpad7", (0, 0x48): "Numpad8", (0, 0x49): "Numpad9",
    (0, 0x4A): "NumpadSubtract",
    (0, 0x4B): "Numpad4", (0, 0x4C): "Numpad5", (0, 0x4D): "Numpad6",
    (0, 0x4E): "NumpadAdd",
    (0, 0x4F): "Numpad1", (0, 0x50): "Numpad2", (0, 0x51): "Numpad3",
    (0, 0x52): "Numpad0", (0, 0x53): "NumpadDecimal",
    (0, 0x56): "IntlBackslash", (0, 0x57): "F11", (0, 0x58): "F12",

    # 扩展键（E0 前缀）
    (1, 0x1C): "NumpadEnter", (1, 0x1D): "ControlRight",
    (1, 0x35): "NumpadDivide", (1, 0x37): "PrintScreen", (1, 0x38): "AltRight",
    (1, 0x47): "Home", (1, 0x48): "ArrowUp", (1, 0x49): "PageUp",
    (1, 0x4B): "ArrowLeft", (1, 0x4D): "ArrowRight",
    (1, 0x4F): "End", (1, 0x50): "ArrowDown", (1, 0x51): "PageDown",
    (1, 0x52): "Insert", (1, 0x53): "Delete",
    (1, 0x5B): "MetaLeft", (1, 0x5C): "MetaRight", (1, 0x5D): "ContextMenu",
    (1, 0x10): "MediaTrackPrevious", (1, 0x19): "MediaTrackNext",
    (1, 0x20): "AudioVolumeMute", (1, 0x2E): "AudioVolumeDown",
    (1, 0x30): "AudioVolumeUp", (1, 0x22): "MediaPlayPause",
    (1, 0x24): "MediaStop",
    (1, 0x65): "BrowserSearch", (1, 0x66): "BrowserFavorites",
    (1, 0x67): "BrowserRefresh", (1, 0x68): "BrowserStop",
    (1, 0x69): "BrowserForward", (1, 0x6A): "BrowserBack",
    (1, 0x6B): "BrowserHome",
    (1, 0x5E): "Power", (1, 0x5F): "Sleep", (1, 0x63): "WakeUp",
})

# 扫描码缺失时的虚拟键兜底
_VK_MAP = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Escape",
    0x20: "Space", 0x14: "CapsLock", 0x5B: "MetaLeft", 0x5C: "MetaRight",
    0x5D: "ContextMenu", 0x90: "NumLock", 0x91: "ScrollLock",
}

# ---------------------------------------------------------------------------
# Win32 绑定（仅 Windows）
# ---------------------------------------------------------------------------

if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WM_INPUT = 0x00FF
    WM_CLOSE = 0x0010
    RIDEV_INPUTSINK = 0x00000100
    RIDEV_REMOVE = 0x00000001
    RID_INPUT = 0x10000003
    RIM_TYPEKEYBOARD = 1
    RI_KEY_BREAK = 0x01     # 抬起
    RI_KEY_E0 = 0x02        # 扩展键
    RI_KEY_E1 = 0x04        # 扩展键（Pause 一类）
    RIDI_DEVICENAME = 0x20000007

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    class RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [("usUsagePage", wintypes.USHORT),
                    ("usUsage", wintypes.USHORT),
                    ("dwFlags", wintypes.DWORD),
                    ("hwndTarget", ctypes.c_void_p)]

    class RAWINPUTHEADER(ctypes.Structure):
        _fields_ = [("dwType", wintypes.DWORD),
                    ("dwSize", wintypes.DWORD),
                    ("hDevice", ctypes.c_void_p),
                    ("wParam", wintypes.WPARAM)]

    class RAWKEYBOARD(ctypes.Structure):
        _fields_ = [("MakeCode", wintypes.USHORT),
                    ("Flags", wintypes.USHORT),
                    ("Reserved", wintypes.USHORT),
                    ("VKey", wintypes.USHORT),
                    ("Message", wintypes.UINT),
                    ("ExtraInformation", wintypes.ULONG)]

    class RAWINPUT(ctypes.Structure):
        _fields_ = [("header", RAWINPUTHEADER),
                    ("keyboard", RAWKEYBOARD)]

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT),
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", ctypes.c_void_p),
                    ("hIcon", ctypes.c_void_p),
                    ("hCursor", ctypes.c_void_p),
                    ("hbrBackground", ctypes.c_void_p),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                    ("hIconSm", ctypes.c_void_p)]

    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.RegisterClassExW.restype = wintypes.WORD
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    user32.RegisterRawInputDevices.restype = wintypes.BOOL
    user32.RegisterRawInputDevices.argtypes = [
        ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT]
    user32.GetRawInputData.restype = wintypes.UINT
    user32.GetRawInputData.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                       ctypes.c_void_p,
                                       ctypes.POINTER(wintypes.UINT),
                                       wintypes.UINT]
    user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
    user32.GetRawInputDeviceInfoW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                              ctypes.c_void_p,
                                              ctypes.POINTER(wintypes.UINT)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD)]
    kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD

    def current_session():
        """返回 (本进程会话ID, 当前控制台会话ID)。"""
        sid = wintypes.DWORD(0)
        kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(),
                                      ctypes.byref(sid))
        return sid.value, kernel32.WTSGetActiveConsoleSessionId()

else:
    def current_session():
        return -1, -1


_VID_PID_RE = re.compile(r"VID[_&](?:02)?([0-9A-F]{4})[_&]PID[_&]([0-9A-F]{4})")


def _parse_vid_pid(text, default=(0xE118, 0x0001)):
    """把 "E118:0001" / "e118-0001" 解析成 (vid, pid)。"""
    try:
        parts = re.split(r"[:,\-&]", str(text or "").strip())
        if len(parts) >= 2:
            return (int(parts[0], 16), int(parts[1], 16))
    except (ValueError, TypeError):
        pass
    return default


# ---------------------------------------------------------------------------
# 键盘采集器
# ---------------------------------------------------------------------------

class KeyboardWatcher(object):
    """后台键盘采集器：Raw Input 线程 + 状态维护。"""

    def __init__(self, on_event=None, device_filter="", debug=False):
        self.on_event = on_event
        self.device_filter = (device_filter or "").upper()
        self.debug = bool(debug)

        self.available = False
        self.reason = ""
        self.warn = ""
        self.total = 0
        self.held = set()
        self.last_src = "?"
        self.last_code = ""
        self.devices = {}          # "E118:0001|usb" -> {...}

        self._lock = threading.Lock()
        self._devcache = {}        # hDevice -> info dict
        self._thread = None
        self._hwnd = None
        self._ready = threading.Event()
        self._running = False

    # ---- 生命周期 ----

    def start(self):
        if not IS_WINDOWS:
            self.reason = "仅支持 Windows（当前平台: %s）" % sys.platform
            return False

        # Raw Input 是会话隔离的：SYSTEM 计划任务跑在会话 0，
        # 收不到交互桌面（会话 1+）的按键，这里提前告警。
        sid, active = current_session()
        if active not in (-1, 0xFFFFFFFF) and sid != active:
            self.warn = ("后端运行在会话 %d（例如 SYSTEM 计划任务），交互桌面在会话 %d；"
                         "Raw Input 会话隔离，将收不到按键。"
                         "请改为以当前登录用户身份启动后端。" % (sid, active))
            print("[keys][警告] " + self.warn)

        self._running = True
        self._thread = threading.Thread(target=self._run, name="keyboard-raw")
        self._thread.daemon = True
        self._thread.start()
        self._ready.wait(timeout=3.0)
        return self.available

    def stop(self):
        self._running = False
        if IS_WINDOWS and self._hwnd:
            try:
                user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.5)

    # ---- 线程主体 ----

    def _run(self):
        try:
            self._hwnd = self._create_window()
        except Exception as exc:
            self.reason = "创建消息窗口失败: %s" % exc
            self._ready.set()
            return
        if not self._hwnd:
            self.reason = "创建消息窗口失败 (CreateWindowExW 返回 0)"
            self._ready.set()
            return

        rid = RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, self._hwnd)
        if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                              ctypes.sizeof(RAWINPUTDEVICE)):
            self.reason = ("RegisterRawInputDevices 失败 (err=%d)"
                           % ctypes.get_last_error())
            self._ready.set()
            return

        self.available = True
        self._ready.set()
        if self.debug:
            print("[keys] Raw Input 采集已启动")

        msg = wintypes.MSG()
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        try:
            rid.dwFlags = RIDEV_REMOVE
            rid.hwndTarget = None
            user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                           ctypes.sizeof(RAWINPUTDEVICE))
        except Exception:
            pass

    def _create_window(self):
        hinst = kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._on_message)   # 必须持引用防 GC
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "AmoledDashKeyWatch"
        user32.RegisterClassExW(ctypes.byref(wc))   # 已注册时失败可忽略
        return user32.CreateWindowExW(
            0, "AmoledDashKeyWatch", "keywatch", 0,
            0, 0, 0, 0, None, None, hinst, None)

    # ---- 窗口消息 ----

    def _on_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                self._handle_raw(lparam)
            except Exception:
                pass
            return 0
        if msg == WM_CLOSE:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _handle_raw(self, lparam):
        hdr_size = ctypes.sizeof(RAWINPUTHEADER)
        size = wintypes.UINT(0)
        if user32.GetRawInputData(lparam, RID_INPUT, None,
                                  ctypes.byref(size), hdr_size) != 0:
            return
        if not size.value:
            return
        buf = ctypes.create_string_buffer(size.value)
        got = user32.GetRawInputData(lparam, RID_INPUT, buf,
                                     ctypes.byref(size), hdr_size)
        if got != size.value:
            return
        ri = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
        if ri.header.dwType != RIM_TYPEKEYBOARD:
            return

        kb = ri.keyboard
        if kb.VKey == 0xFF:          # 假键：仅用于标识特殊序列
            return
        flags = kb.Flags
        if flags & RI_KEY_E1:
            return
        down = not (flags & RI_KEY_BREAK)
        extended = 1 if (flags & RI_KEY_E0) else 0
        code = _SCAN_MAP.get((extended, kb.MakeCode)) or _VK_MAP.get(kb.VKey)
        if not code:
            code = "VK_%02X" % kb.VKey

        info = self._device_info(ri.header.hDevice)
        if self.device_filter and self.device_filter not in info["path"].upper():
            return

        with self._lock:
            if down:
                self.held.add(code)
                self.total += 1
            else:
                self.held.discard(code)
            self.last_src = info["src"]
            self.last_code = code
            dev = self.devices.get(info["key"])
            if dev is None:
                dev = {"key": info["key"], "src": info["src"],
                       "vid": info["vid"], "pid": info["pid"], "count": 0}
                self.devices[info["key"]] = dev
            dev["count"] += 1

        if self.on_event:
            self.on_event({
                "t": "k",
                "code": code,
                "vk": kb.VKey,
                "sc": kb.MakeCode,
                "down": bool(down),
                "src": info["src"],
                "dev": info["key"],
                "ts": round(time.time(), 3),
            })

    # ---- 设备识别（区分 USB / 蓝牙） ----

    def _device_info(self, hdevice):
        key = int(hdevice or 0)
        cached = self._devcache.get(key)
        if cached is not None:
            return cached

        path = ""
        try:
            need = wintypes.UINT(0)
            user32.GetRawInputDeviceInfoW(hdevice, RIDI_DEVICENAME, None,
                                          ctypes.byref(need))
            if need.value:
                buf = ctypes.create_unicode_buffer(need.value)
                user32.GetRawInputDeviceInfoW(
                    hdevice, RIDI_DEVICENAME,
                    ctypes.cast(buf, ctypes.c_void_p), ctypes.byref(need))
                path = buf.value or ""
        except Exception:
            path = ""

        upper = path.upper()
        if "00001812" in upper or "BTHLE" in upper or "BTHENUM" in upper:
            src = "bt"
        elif "VID_" in upper or "VID&" in upper:
            src = "usb"
        else:
            src = "hid"

        m = _VID_PID_RE.search(upper)
        vid = m.group(1) if m else "----"
        pid = m.group(2) if m else "----"
        info = {"key": "%s:%s" % (vid, pid), "src": src,
                "vid": vid, "pid": pid, "path": path}
        self._devcache[key] = info
        if self.debug:
            print("[keys] 设备 hDevice=%s -> %s (%s)" % (key, info["key"], src))
        return info

    # ---- 状态快照 ----

    def snapshot(self):
        with self._lock:
            return {
                "available": self.available,
                "reason": self.reason,
                "warn": self.warn,
                "total": self.total,
                "held": sorted(self.held),
                "src": self.last_src,
                "devices": sorted(self.devices.values(),
                                  key=lambda d: -d["count"])[:6],
            }


# ---------------------------------------------------------------------------
# 推送通道：独立 WebSocket 服务
# ---------------------------------------------------------------------------

class KeyBus(object):
    """
    按键事件总线：
    - 持有 KeyboardWatcher（采集）与一个独立 WebSocketServer（推送）；
    - 客户端连接时立即下发当前状态，之后每个按键实时广播；
    - 每 5 秒心跳一次，顺便把 held 集合带给客户端做对账（断线重连自愈）。
    """

    def __init__(self, config=None, debug=False):
        import transport  # 复用项目自带的极简 WebSocket 服务

        self.config = config or {}
        cfg = self.config.get("keys") or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.debug = bool(debug)
        self.port = int(cfg.get("port", 8092))
        self.device_filter = cfg.get("device_filter", "") or ""

        self.server = transport.WebSocketServer(
            host="0.0.0.0", port=self.port, debug=self.debug)
        self.server.on_client_connect = self._on_connect
        self.watcher = KeyboardWatcher(on_event=self._broadcast,
                                       device_filter=self.device_filter,
                                       debug=self.debug)
        self._hb_thread = None
        self._revert_thread = None
        self._running = False

        # ---- 键位表（可选）：把输出码反查回物理位置与当前层 ----
        self.keymap = None
        self.tracker = None
        self.matrix_layers = None
        self.matrix = None
        self._mx_pressed = set()
        self._layer_dirty = False
        self._load_keymap(cfg)
        self._setup_matrix(cfg)

    def _setup_matrix(self, cfg):
        """
        混合模式（方案 B）：直接读固件矩阵，拿层键的按下/松开。

        Raw Input 仍然是按键事件的来源（哪个键产生了什么输出），矩阵只负责
        两件它做不到的事：层号、以及层键/死键这类"不产生 HID 报告"的按压。
        读不到（Vial 占用、未装 hidapi、键盘未连）就自动退回方案 A。
        """
        mx = cfg.get("matrix") or {}
        if self.keymap is None:
            return
        if not mx.get("enabled", True):
            print("[keys] keys.matrix.enabled=false，只用输出码推断层号")
            return

        vid, pid = _parse_vid_pid(mx.get("vid_pid", "E118:0001"))
        interval = float(mx.get("interval_ms", 20)) / 1000.0
        self.matrix_layers = keymap.MatrixLayers(self.keymap)
        self.matrix = vialmatrix.MatrixReader(
            vid=vid, pid=pid,
            rows=self.keymap.rows, cols=self.keymap.cols,
            interval=interval,
            retry=float(mx.get("retry_seconds", 5)),
            on_change=self._on_matrix,
            debug=self.debug)

    def _load_keymap(self, cfg):
        path = cfg.get("keymap", "keymap.vil") or ""
        if path and not os.path.isabs(path):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        if not path:
            print("[keys] 未配置 keys.keymap，层解析关闭（只按输出码点亮）")
            return
        if not os.path.isfile(path):
            print("[keys] 键位表不存在，层解析关闭: %s" % path)
            return

        km = keymap.Keymap(path)
        if not km.loaded:
            print("[keys] 键位表载入失败: %s" % km.error)
            return

        self.keymap = km
        self.tracker = keymap.LayerTracker(
            km, idle_revert=float(cfg.get("layer_idle_revert", 0.6)))
        print("[keys] 键位表已载入: %d 层 · %d x %d · %d 个输出码"
              % (km.layer_count(), km.rows, km.cols, len(km.by_code)))

    # ---- 生命周期 ----

    def start(self):
        if not self.enabled:
            print("[keys] 按键采集已在配置中关闭 (keys.enabled=false)")
            return False
        try:
            self.server.start()
        except Exception as exc:
            print("[keys] 按键通道端口 %d 绑定失败: %s" % (self.port, exc))
            return False

        ok = self.watcher.start()
        if ok:
            print("[keys] 按键通道已启动: ws://0.0.0.0:%d/ws" % self.port)
        else:
            print("[keys] 按键采集不可用: %s" % self.watcher.reason)

        if self.matrix is not None:
            if self.matrix.start():
                print("[keys] 矩阵直读已启动: %dx%d · %.0fHz · 层键按下即生效"
                      % (self.matrix.rows, self.matrix.cols,
                         1.0 / self.matrix.interval))
            else:
                print("[keys] 矩阵直读不可用: %s" % self.matrix.reason)

        self._running = True
        self._hb_thread = threading.Thread(target=self._heartbeat,
                                           name="keys-heartbeat")
        self._hb_thread.daemon = True
        self._hb_thread.start()

        self._revert_thread = threading.Thread(target=self._revert_loop,
                                               name="keys-layer-revert")
        self._revert_thread.daemon = True
        self._revert_thread.start()
        return ok

    def stop(self):
        self._running = False
        if self.matrix is not None:
            try:
                self.matrix.stop()
            except Exception:
                pass
        try:
            self.watcher.stop()
        except Exception:
            pass
        try:
            self.server.stop()
        except Exception:
            pass

    # ---- 推送 ----

    def _broadcast(self, event):
        try:
            if event.get("t") == "k" and self.tracker is not None:
                self._annotate(event)
                if self._layer_dirty:
                    self._layer_dirty = False
                    self._push_layer()
            self.server.broadcast_text(json.dumps(event, ensure_ascii=False))
        except Exception:
            pass

    def _matrix_active(self):
        return (self.matrix is not None and self.matrix.available
                and self.matrix_layers is not None)

    def _on_matrix(self, pressed):
        """
        矩阵按压变化（方案 B 的核心）。

        这里做两件 Raw Input 做不到的事：
          1. 层号直接由"按住了哪些物理位置"算出 —— MO(n)、TD(n) 长按一按下
             就有层号，松手立刻回 0，不需要空闲回退；
          2. 把不产生 HID 报告的按压（层键、KC_NO 死键）也推给前端点亮。
        """
        if self.matrix_layers is None:
            return
        layer, changed = self.matrix_layers.on_matrix(pressed)
        self._mx_pressed = set(pressed)

        # 让 tracker 的层号与矩阵保持一致：position() 依赖它选位置
        if self.tracker is not None:
            self.tracker.layer = layer
            self.tracker.candidates = set([layer])
            self.tracker.last_event = time.time()

        held = set("%d,%d" % (r, c) for (r, c) in pressed)
        # 补上其它键盘（例如本机自带键盘）按住、但不在矩阵里的键
        if self.tracker is not None:
            try:
                for code in self.watcher.snapshot()["held"]:
                    pos = self.tracker.position(code)
                    if pos:
                        held.add("%d,%d" % pos)
            except Exception:
                pass

        try:
            self.server.broadcast_text(json.dumps(
                {"t": "mx", "held": sorted(held), "n": len(held),
                 "layer": layer}, ensure_ascii=False))
        except Exception:
            pass
        if changed:
            self._push_layer()

    def _annotate(self, event):
        """给按键事件补上物理位置 rc 与当前层号 layer"""
        code = event.get("code")

        if self._matrix_active():
            # 方案 B：层号以矩阵为准，不再用输出码反推（也就没有 0.6s 回退）
            pos = self.tracker.position(code) if self.tracker is not None else None
            if pos:
                event["rc"] = [pos[0], pos[1]]
            event["layer"] = self.matrix_layers.layer
            return

        if self.tracker is None:
            return
        if event.get("down"):
            _layer, changed = self.tracker.on_down(code)
            if changed:
                self._layer_dirty = True
        else:
            self.tracker.on_up()

        pos = self.tracker.position(code)
        if pos:
            event["rc"] = [pos[0], pos[1]]
        event["layer"] = self.tracker.layer

    def _push_layer(self):
        self.server.broadcast_text(json.dumps(
            {"t": "layer", "n": self.tracker.layer}, ensure_ascii=False))

    def _keymap_message(self):
        return json.dumps({
            "t": "keymap",
            "layers": self.keymap.labels,
            "rows": self.keymap.rows,
            "cols": self.keymap.cols,
        }, ensure_ascii=False)

    def _state_message(self):
        snap = self.watcher.snapshot()
        if self.tracker is not None:
            snap["layer"] = self.tracker.layer
            snap["layers"] = self.keymap.layer_count()
            rc = set("%d,%d" % (r, c) for (r, c) in self._mx_pressed) \
                if self._matrix_active() else set()
            for code in snap["held"]:
                pos = self.tracker.position(code)
                if pos:
                    rc.add("%d,%d" % pos)
            snap["heldrc"] = sorted(rc)
            snap["layer_src"] = "matrix" if self._matrix_active() else "infer"
            if self.matrix is not None and self.matrix.locked:
                snap["locked"] = True
                snap["warn"] = self._lock_hint()
        snap["t"] = "state"
        return json.dumps(snap, ensure_ascii=False)

    def _lock_hint(self):
        """键盘处于 Vial 锁定态时给面板的提示（解锁组合键按层 0 标签翻译）。"""
        labels = []
        try:
            lab = self.keymap.labels_of(0) if self.keymap is not None else {}
            for r, c in (self.matrix.unlock_keys or []):
                labels.append(lab.get("%d,%d" % (r, c)) or "r%dc%d" % (r, c))
        except Exception:
            labels = []
        combo = "+".join(labels) if labels else "组合键"
        return "键盘已锁定：Vial 解锁后按住 %s 约 5 秒（层号暂退回推断）" % combo

    def _on_connect(self, conn):
        """新客户端接入：先补键位表与层号，再补一份完整状态。"""
        try:
            if self.keymap is not None:
                self.server._send_frame(conn, self._keymap_message(), 0x1)
                self.server._send_frame(conn, json.dumps(
                    {"t": "layer", "n": self.tracker.layer},
                    ensure_ascii=False), 0x1)
            self.server._send_frame(conn, self._state_message(), 0x1)
        except Exception:
            pass

    def _heartbeat(self):
        while self._running:
            time.sleep(5.0)
            if self.server.client_count <= 0:
                continue
            try:
                msg = json.loads(self._state_message())
                msg["t"] = "hb"
                self.server.broadcast_text(json.dumps(msg, ensure_ascii=False))
            except Exception:
                pass

    def _revert_loop(self):
        """
        空闲一段时间后把层号收回 0。

        层键是按住生效的，松手就一定回到层 0；但主机看不到 MO() 的松手，
        只能靠"安静了这么久，应该是放开了"来近似，否则松开层键后
        画面会一直停在上一层。
        """
        while self._running:
            time.sleep(0.1)
            if self.tracker is None or self.server.client_count <= 0:
                continue
            if self._matrix_active():
                # 矩阵能直接看到层键松手，不需要空闲近似
                continue
            try:
                if self.tracker.maybe_revert():
                    self._push_layer()
            except Exception:
                pass

    # ---- 状态（供 /api/status 与扩展使用） ----

    def status(self):
        snap = self.watcher.snapshot()
        data = {
            "enabled": self.enabled,
            "port": self.port,
            "clients": self.server.client_count,
            "available": snap["available"],
            "reason": snap["reason"],
            "warn": snap["warn"],
            "total": snap["total"],
            "held": len(snap["held"]),
            "src": snap["src"],
            "devices": [{"dev": d["key"], "src": d["src"], "count": d["count"]}
                        for d in snap["devices"]],
        }
        if self.tracker is not None:
            data["layer"] = self.tracker.layer
            data["layers"] = self.keymap.layer_count()
            data["keymap"] = os.path.basename(self.keymap.path or "")
        else:
            data["layer"] = None
            data["layers"] = 0
            data["keymap"] = ""

        # 混合模式：矩阵直读是否可用、层号来自哪一路
        data["layer_src"] = "matrix" if self._matrix_active() else "infer"
        if self.matrix is not None:
            data["matrix"] = self.matrix.snapshot()
            data["matrix"]["enabled"] = True
        else:
            data["matrix"] = {"enabled": False}
        return data

    def extension_data(self):
        """
        供主快照的 extensions 字段使用。
        去掉 enabled：该字段由 extensions 注册表统一填充，
        避免运行时 set_extension_enabled() 被这里覆盖。
        """
        data = self.status()
        data.pop("enabled", None)
        return data
