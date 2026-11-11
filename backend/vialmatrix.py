# -*- coding: utf-8 -*-
"""
vialmatrix.py - 直读固件矩阵（方案 B）

为什么需要它
------------
Raw Input 只能看到"主机收到的 HID 报告"。层键 MO(n)、tap-dance 长按、
KC_NO 死键都不会产生报告，所以方案 A（见 keymap.py）只能靠输出码反推，
按下层键的瞬间看不到，松手也看不到。

Vial 的 Matrix Tester 能实时看到这些，是因为它走的是**厂商自定义接口**
（MI_02，Usage Page 0xFF60）直接向固件要矩阵位图 —— 那是 QMK 做完去抖、
还没做键码解析的原始状态，任何开关闭合都在里面。

协议（与 Vial GUI 完全一致，只读）
-----------------------------------
    请求  [0x02, 0x03] + 30 字节 0        # VIA_GET_KEYBOARD_VALUE / SWITCH_MATRIX_STATE
    回复  32 字节：前 2 字节是命令回显，之后按行排列
          每行 ceil(cols/8) 字节，高位字节在前，位序 LSB = 最低列

限制
----
* 接口是独占性质的：Vial GUI 打开时无法共用（回包会被对方抢读），
  因此本模块失败不抛异常，只把 available 置 False 并定期重试；
* **Vial 固件默认锁定，且解锁状态只存在 RAM 里**（vial.c: `int vial_unlocked = 0;`），
  键盘断电/重启/切换连接后必然回到锁定。锁定时固件对 0x03 直接 `goto skip`，
  回包就是请求原样回显（≈全零），看起来"读取成功"却永远是空矩阵 —— 必须显式查
  0xFE 0x05 才能识别。本模块每次连上都先查锁，锁定则降级并每 retry 秒复检一次；
* 未安装 hidapi 时静默降级（方案 A 照常工作）。
"""

import threading
import time

VENDOR_USAGE_PAGE = 0xFF60
VENDOR_USAGE = 0x61

CMD_VIA_GET_KEYBOARD_VALUE = 0x02
VIA_SWITCH_MATRIX_STATE = 0x03
CMD_VIAL_PREFIX = 0xFE
CMD_VIAL_GET_UNLOCK_STATUS = 0x05


class MatrixReader(object):
    """后台轮询固件矩阵，变化时回调 on_change(pressed_set)。"""

    def __init__(self, vid=0xE118, pid=0x0001, rows=8, cols=7,
                 interval=0.02, retry=5.0, on_change=None, debug=False):
        self.vid = int(vid)
        self.pid = int(pid)
        self.rows = int(rows or 8)
        self.cols = int(cols or 7)
        self.interval = max(0.005, float(interval))
        self.retry = max(0.5, float(retry))
        self.on_change = on_change
        self.debug = bool(debug)

        self.available = False
        self.reason = "未启动"
        self.warn = ""
        self.total = 0             # 成功读取次数
        self.last_ms = 0.0         # 最近一次往返耗时
        self.pressed = set()       # {(r, c)}
        self.locked = False        # True = 键盘处于 Vial 锁定态，矩阵读不到
        self.unlock_keys = []      # 解锁组合键的矩阵位置 [(r, c), ...]

        self._dev = None
        self._hid = None
        self._thread = None
        self._running = False
        self._ready = threading.Event()
        self._rows_bytes = (self.cols + 7) // 8

    # ---- 生命周期 ----

    def start(self):
        if self._running:
            return self.available
        self._running = True
        self._thread = threading.Thread(target=self._run, name="keyboard-matrix")
        self._thread.daemon = True
        self._thread.start()
        self._ready.wait(timeout=3.0)
        return self.available

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.5)
        self._close()

    def snapshot(self):
        return {
            "available": self.available,
            "reason": self.reason,
            "warn": self.warn,
            "rows": self.rows,
            "cols": self.cols,
            "hz": round(1.0 / self.interval, 1),
            "last_ms": round(self.last_ms, 1),
            "total": self.total,
            "held": len(self.pressed),
            "locked": self.locked,
            "unlock_keys": [[r, c] for (r, c) in self.unlock_keys],
        }

    # ---- 线程主体 ----

    def _run(self):
        while self._running:
            if self._dev is None:
                if not self._open():
                    self._ready.set()
                    self._wait(self.retry)
                    continue
                self._ready.set()

            t0 = time.time()
            try:
                grid = self._read_matrix()
            except Exception as exc:
                self._fail("读取失败: %s" % exc)
                continue

            self.last_ms = (time.time() - t0) * 1000.0
            self.total += 1
            pressed = set()
            for r, row in enumerate(grid):
                for c, bit in enumerate(row):
                    if bit:
                        pressed.add((r, c))

            if pressed != self.pressed:
                self.pressed = pressed
                if self.on_change:
                    try:
                        self.on_change(pressed)
                    except Exception as exc:
                        if self.debug:
                            print("[matrix] 回调异常: %s" % exc)

            spent = time.time() - t0
            if spent < self.interval:
                self._wait(self.interval - spent)

        self._close()

    def _wait(self, seconds):
        """可被 stop() 打断的等待。"""
        end = time.time() + max(0.0, seconds)
        while self._running and time.time() < end:
            time.sleep(min(0.02, end - time.time()))

    # ---- 设备 ----

    def _open(self):
        try:
            if self._hid is None:
                import hid
                self._hid = hid
        except ImportError:
            self._fail("未安装 hidapi（pip install hidapi），矩阵直读关闭，"
                       "层号退回输出码推断")
            return False

        try:
            candidates = self._hid.enumerate(self.vid, self.pid)
        except Exception as exc:
            self._fail("枚举 HID 设备失败: %s" % exc)
            return False

        for info in candidates:
            page = info.get("usage_page") or 0
            usage = info.get("usage") or 0
            if page != VENDOR_USAGE_PAGE or usage != VENDOR_USAGE:
                continue
            try:
                dev = self._hid.device()
                dev.open_path(info["path"])
            except Exception as exc:
                self._fail("厂商接口被占用（Vial 是否开着？）: %s" % exc)
                return False
            self._dev = dev
            self.locked = False
            self.unlock_keys = []

            # 连着就查一次锁：锁定时固件把 0x03 当未处理命令，回包是请求回显，
            # 解析出来是全零矩阵 —— 不查锁就会"读取成功"却永远没有键按下。
            unlock = self._read_unlock()
            if unlock is not None:
                unlocked, keys = unlock
                if unlocked == 0:
                    self.locked = True
                    self.unlock_keys = keys
                    self._fail("键盘处于 Vial 锁定态，矩阵读取被固件禁用"
                               "（需在 Vial 里解锁并按住组合键）")
                    return False

            self.available = True
            self.reason = ""
            self.warn = ""
            if self.debug:
                print("[matrix] 已连接厂商接口 iface=%s"
                      % info.get("interface_number"))
            return True

        self._fail("未找到 Usage Page 0xFF60 接口（键盘未连接？）")
        return False

    def _close(self):
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:
                pass
            self._dev = None

    def _fail(self, reason):
        if self.available or reason != self.reason:
            print("[matrix] %s" % reason)
        self.available = False
        self.reason = reason
        self.warn = "" if self.total else "矩阵直读不可用，层号退回输出码推断"
        self.pressed = set()
        self._close()

    # ---- 协议 ----

    def _read_unlock(self):
        """查 Vial 解锁状态。返回 (unlocked, [(r, c), ...]) 或 None（不支持查询）。"""
        buf = [0x00, CMD_VIAL_PREFIX, CMD_VIAL_GET_UNLOCK_STATUS]
        buf += [0] * (32 - len(buf))
        try:
            self._dev.write(buf)
            data = self._dev.read(32, 300)
        except Exception:
            return None
        if not data or len(data) < 2:
            return None
        # 固件未实现该命令时回 id_unhandled(0xFF)，此时不做锁定判定
        if data[0] == 0xFF:
            return None
        keys = []
        for i in range(2, len(data) - 1, 2):
            r, c = data[i], data[i + 1]
            if r == 0xFF or c == 0xFF:
                break
            keys.append((r, c))
        return data[0], keys

    def _read_matrix(self):
        buf = [0x00, CMD_VIA_GET_KEYBOARD_VALUE, VIA_SWITCH_MATRIX_STATE]
        buf += [0] * (32 - len(buf))
        self._dev.write(buf)
        data = self._dev.read(32, 300)
        if not data:
            raise IOError("无回复")
        need = 2 + self.rows * self._rows_bytes
        if len(data) < need:
            raise IOError("回复过短 (%d < %d)" % (len(data), need))

        grid = []
        for r in range(self.rows):
            start = 2 + r * self._rows_bytes
            chunk = data[start:start + self._rows_bytes]
            row = []
            for c in range(self.cols):
                # 多字节行里高位字节在前，位序 LSB 对应最小列
                byte = chunk[len(chunk) - 1 - (c // 8)]
                row.append((byte >> (c % 8)) & 1)
            grid.append(row)
        return grid
