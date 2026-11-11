# -*- coding: utf-8 -*-
"""
Vial .vil 键位表解析 + 层状态推断（方案 A）

原理
----
Raw Input 拿到的是固件做完层解析之后的**输出键码**，层号在系统侧不可见。
但 .vil 里记录了每一层每个矩阵位置的键码，而这份键位表里同一个输出键码
几乎只对应一个物理位置 —— 于是可以反查：

    看到 Digit1  =>  层 1 的 (0,1) 被按下

由此既能把握不住位置的高亮画到正确的物理位置，也能顺带推断出当前层。

局限（方案 A 的固有边界，不是 bug）
----
* MO() / TD() 长按这类层键本身不产生任何 HID 报告，所以"按住层键的瞬间"
  无法感知，要等该层第一个按键按下才切换；
* 上层用 KC_NO 定义的死键同样不产生事件，按了不会有反馈；
* 多个层内容完全相同时（本键位表里层 2/3、层 5~9）无法区分，
  但内容一致，显示上没有区别。

方案 B（混合模式）
----
上面这些局限来自"只能看主机侧"。若同时读到固件矩阵（见 vialmatrix.py），
层号就可以由**物理按压位置**直接算出：MatrixLayers 会按 .vil 里的
MO() / LT() / TD() 长按动作，把当前按住的层键映射成层号，
不需要等下一个按键，也没有空闲回退的延迟。
"""

import io
import json
import re
import time


# ---------------------------------------------------------------------------
# KC 键码 -> 浏览器 KeyboardEvent.code
# 只覆盖本键位表实际用到的键；表外的键码一律视为"无 OS 事件"。
# ---------------------------------------------------------------------------

KC_TO_CODE = {}

for _ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    KC_TO_CODE["KC_" + _ch] = "Key" + _ch
for _dg in "0123456789":
    KC_TO_CODE["KC_" + _dg] = "Digit" + _dg
for _fn in range(1, 25):
    KC_TO_CODE["KC_F%d" % _fn] = "F%d" % _fn

KC_TO_CODE.update({
    "KC_ESCAPE": "Escape",
    "KC_TAB": "Tab",
    "KC_BSPACE": "Backspace",
    "KC_ENTER": "Enter",
    "KC_SPACE": "Space",
    "KC_DELETE": "Delete",
    "KC_INSERT": "Insert",
    "KC_HOME": "Home",
    "KC_END": "End",
    "KC_PGUP": "PageUp",
    "KC_PGDOWN": "PageDown",
    "KC_MINUS": "Minus",
    "KC_EQUAL": "Equal",
    "KC_SCOLON": "Semicolon",
    "KC_QUOTE": "Quote",
    "KC_COMMA": "Comma",
    "KC_DOT": "Period",
    "KC_SLASH": "Slash",
    "KC_BSLASH": "Backslash",
    "KC_LEFT": "ArrowLeft",
    "KC_RIGHT": "ArrowRight",
    "KC_UP": "ArrowUp",
    "KC_DOWN": "ArrowDown",
    "KC_LSHIFT": "ShiftLeft",
    "KC_RSHIFT": "ShiftRight",
    "KC_LCTRL": "ControlLeft",
    "KC_RCTRL": "ControlRight",
    "KC_LALT": "AltLeft",
    "KC_RALT": "AltRight",
    "KC_LGUI": "MetaLeft",
    "KC_RGUI": "MetaRight",
    "KC_MUTE": "AudioVolumeMute",
    "KC_VOLD": "AudioVolumeDown",
    "KC_VOLU": "AudioVolumeUp",
    # 鼠标键走 HID 鼠标通道，不会有键盘 code
    "KC_BTN1": None,
    "KC_BTN2": None,
    "KC_BTN3": None,
})


# ---------------------------------------------------------------------------
# KC 键码 -> 键帽上的短标签
# ---------------------------------------------------------------------------

KC_TO_LABEL = {}

for _ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    KC_TO_LABEL["KC_" + _ch] = _ch
for _dg in "0123456789":
    KC_TO_LABEL["KC_" + _dg] = _dg
for _fn in range(1, 25):
    KC_TO_LABEL["KC_F%d" % _fn] = "F%d" % _fn

KC_TO_LABEL.update({
    "KC_ESCAPE": "Esc",
    "KC_TAB": "Tab",
    "KC_BSPACE": "Bksp",
    "KC_ENTER": "Enter",
    "KC_SPACE": "Space",
    "KC_DELETE": "Del",
    "KC_INSERT": "Ins",
    "KC_HOME": "Home",
    "KC_END": "End",
    "KC_PGUP": "PgUp",
    "KC_PGDOWN": "PgDn",
    "KC_MINUS": "-",
    "KC_EQUAL": "=",
    "KC_SCOLON": ";",
    "KC_QUOTE": "'",
    "KC_COMMA": "<",
    "KC_DOT": ">",
    "KC_SLASH": "?",
    "KC_BSLASH": "\\",
    "KC_LEFT": "Left",
    "KC_RIGHT": "Right",
    "KC_UP": "Up",
    "KC_DOWN": "Down",
    "KC_LSHIFT": "LShift",
    "KC_RSHIFT": "RShift",
    "KC_LCTRL": "LCtrl",
    "KC_RCTRL": "RCtrl",
    "KC_LALT": "LAlt",
    "KC_RALT": "RAlt",
    "KC_LGUI": "LGui",
    "KC_RGUI": "RGui",
    "KC_MUTE": "Mute",
    "KC_VOLD": "Vol-",
    "KC_VOLU": "Vol+",
    "KC_BTN1": "Mouse1",
    "KC_BTN2": "Mouse2",
    "KC_BTN3": "Mouse3",
    # 空位：不画标签
    "KC_NO": "",
    "KC_TRNS": "",
})


# TD(1) 轻按输出空格，而 TD(1) 在本键位表里占了 3 个物理位置，
# 光看输出码分不出是哪一颗 —— 固定点亮左侧那颗正放的（(3,3)）。
CODE_ALIAS = {
    "Space": (3, 3),
}

# 层键模式：按住生效 / 切换 / 轻触动作表
_HOLD_RE = re.compile(r"^(?:MO|LT|LM|OSL|TT)\((\d+)")
_TD_RE = re.compile(r"^TD\((\d+)\)")
_SW_RE = re.compile(r"^(TG|TO|DF)\((\d+)")


# ---------------------------------------------------------------------------
# 键位表
# ---------------------------------------------------------------------------

class Keymap(object):
    """.vil 键位表：按层保存矩阵，并提供 输出码 -> 物理位置 的反查。"""

    def __init__(self, path=None):
        self.path = path
        self.layers = []          # [[[kc, ...], ...], ...]  层 -> 行 -> 列
        self.rows = 0
        self.cols = 0
        self.loaded = False
        self.error = ""
        self.by_code = {}         # code -> [(layer, r, c), ...]
        self.labels = []          # [{("r,c"): 标签}, ...]
        self.layer_codes = []     # [set(code), ...]
        self.tap_dance = []       # Vial tap_dance 定义（取长按动作认层键）
        if path:
            self.load(path)

    # ---- 载入 ----

    def load(self, path):
        try:
            with io.open(path, "r", encoding="utf-8") as fp:
                doc = json.load(fp)
        except Exception as exc:
            self.error = "读取键位表失败: %s" % exc
            return False

        layout = doc.get("layout") or []
        if not layout:
            self.error = "键位表里没有 layout 字段"
            return False

        # TD(n) 的长按动作在 tap_dance 里，方案 B 靠它把 TD 键认成层键
        self.tap_dance = doc.get("tap_dance") or []

        # 非字符串条目（Vial 用数字存原始键码，-1 表示空位）统一当作空位
        self.layers = []
        for layer in layout:
            rows = []
            for row in layer:
                rows.append([kc if isinstance(kc, str) else "" for kc in row])
            self.layers.append(rows)

        self.rows = max(len(l) for l in self.layers)
        self.cols = max((len(r) for l in self.layers for r in l), default=0)

        self._index()
        self.loaded = True
        self.error = ""
        return True

    def _index(self):
        self.by_code = {}
        self.labels = []
        self.layer_codes = []

        for li, layer in enumerate(self.layers):
            codes = set()
            lab = {}
            for r, row in enumerate(layer):
                for c, kc in enumerate(row):
                    if not kc:
                        continue
                    code = KC_TO_CODE.get(kc)
                    if code:
                        codes.add(code)
                        self.by_code.setdefault(code, []).append((li, r, c))
                    text = KC_TO_LABEL.get(kc, kc)
                    if text:
                        lab["%d,%d" % (r, c)] = text
            self.labels.append(lab)
            self.layer_codes.append(codes)

    # ---- 查询 ----

    def layer_count(self):
        return len(self.layers)

    def layers_of_code(self, code):
        """该输出码可能来自哪些层"""
        return set(p[0] for p in self.by_code.get(code, ()))

    def labels_of(self, layer):
        if 0 <= layer < len(self.labels):
            return self.labels[layer]
        return {}

    def layer0_position(self, code):
        """输出码在层 0 的物理位置（用于前端建 位置 -> 元素 映射）"""
        for (l, r, c) in self.by_code.get(code, ()):
            if l == 0:
                return (r, c)
        return None

    def dump_positions(self):
        """code -> (r, c)，取层 0 优先，给前端初始化用"""
        out = {}
        for code, lst in self.by_code.items():
            pos = self.layer0_position(code) or (lst[0][1], lst[0][2])
            out[code] = pos
        for code, pos in CODE_ALIAS.items():
            out.setdefault(code, pos)
        return out

    # ---- 层键解析（方案 B：靠物理按压判断层） ----

    def keycode_at(self, layer, r, c):
        """(层, 行, 列) 的键码；KC_TRNS 向下穿透，取不到返回空串。"""
        li = min(layer, len(self.layers) - 1)
        while li >= 0:
            row = self.layers[li]
            if r < len(row) and c < len(row[r]):
                kc = row[r][c]
                if kc and kc != "KC_TRNS":
                    return kc
            li -= 1
        return ""

    def hold_layer(self, kc):
        """按住该键时激活的层号；不是层键返回 None。

        MO/LT/LM/OSL/TT 直接取参数；TD(n) 追溯到 tap_dance 的长按动作
        —— 本键位表里 TD(1) 长按就是 MO(1)，这正是主机侧看不见的那颗。
        """
        if not kc:
            return None
        m = _HOLD_RE.match(kc)
        if m:
            return int(m.group(1))
        m = _TD_RE.match(kc)
        if m:
            idx = int(m.group(1))
            if 0 <= idx < len(self.tap_dance):
                act = self.tap_dance[idx]
                if isinstance(act, list) and len(act) > 1:
                    return self.hold_layer(act[1])
        return None

    def switch_layer(self, kc):
        """TG(n) / TO(n) / DF(n) 的切换动作，返回 (动作, 层号)；没有返回 None。"""
        m = _SW_RE.match(kc or "")
        if m:
            return (m.group(1), int(m.group(2)))
        return None


# ---------------------------------------------------------------------------
# 层状态推断
# ---------------------------------------------------------------------------

class LayerTracker(object):
    """
    候选集推断：维护"当前可能是哪几层"的集合，每来一个输出键码就与
    "该键码可能来自的层"求交。

    交集为空说明层变了（典型场景：松开 MO(1) 后按了一个只在层 0 存在的键），
    这时直接重置为该键码的层集合。

    取层时优先保持当前层（这样 Mute / Mouse3 这类各层都有的键不会把层号拽回 0）。
    """

    def __init__(self, keymap, idle_revert=0.6):
        self.keymap = keymap
        self.idle_revert = float(idle_revert)
        self.all_layers = set(range(keymap.layer_count())) or set([0])
        self.candidates = set(self.all_layers)
        self.layer = 0
        self.last_event = time.time()

    def on_down(self, code, now=None):
        """
        返回 (layer, changed)。code 不在键位表里时返回 (None, False)，
        表示这条事件对层推断没有信息量。
        """
        now = time.time() if now is None else now
        self.last_event = now

        cands = self.keymap.layers_of_code(code)
        if not cands:
            return (None, False)

        nxt = self.candidates & cands
        if not nxt:
            nxt = cands
        self.candidates = nxt

        layer = self.layer if self.layer in nxt else min(nxt)
        changed = (layer != self.layer)
        self.layer = layer
        return (layer, changed)

    def on_up(self, now=None):
        self.last_event = time.time() if now is None else now

    def position(self, code):
        """输出码 -> 物理位置，优先当前层"""
        lst = self.keymap.by_code.get(code) or ()
        for (l, r, c) in lst:
            if l == self.layer:
                return (r, c)
        if lst:
            return (lst[0][1], lst[0][2])
        return CODE_ALIAS.get(code)

    def maybe_revert(self, now=None):
        """
        空闲一段时间后回到层 0。

        层键是按住生效的，一旦所有按键都松开就必然回到层 0；但主机看不到
        MO() 的松手，只能靠"安静了这么久，应该是放开了"来近似。
        """
        now = time.time() if now is None else now
        if self.layer == 0:
            return False
        if (now - self.last_event) < self.idle_revert:
            return False
        self.layer = 0
        self.candidates = set(self.all_layers)
        return True


# ---------------------------------------------------------------------------
# 层状态推断（方案 B：直接读矩阵）
# ---------------------------------------------------------------------------

class MatrixLayers(object):
    """
    用固件矩阵的按压位置解析当前层。

    与 LayerTracker 的分工：
      * LayerTracker 只能靠"输出码落在哪一层"反推，层键按下瞬间看不到、
        松手也看不到，只能用空闲超时近似 —— 方案 A 的妥协；
      * 这里直接知道"哪些物理位置正被按住"，MO(n) / TD(n) 长按一按下就有层号，
        松手立刻回 0，没有那 0.6 秒。

    取层规则与 QMK 一致：所有激活层里最高的那层生效（layer_state 最高位）。

    解析键码用的是**基础层**（TG/TO 留下的常驻层 + 层 0），而不是"当前层"。
    原因：QMK 在按下的那一瞬间按当时的层状态解析键码，而层键本身都定义在
    基础层；若改用当前层反复求解，会出现"按下 MO(1) → 层 1 → 该位置在层 1
    是数字键 → 层号又退回 0"的震荡。
    """

    def __init__(self, keymap):
        self.keymap = keymap
        self.sticky = set([0])      # TG / TO 留下的常驻层
        self.momentary = set()      # 当前按住生效的层
        self.held = set()           # 上一次的按压位置
        self.layer = 0

    def on_matrix(self, pressed):
        """pressed: set((r, c))。返回 (layer, changed)。"""
        pressed = set(pressed)
        self._handle_switches(pressed - self.held)
        self.held = pressed
        self.momentary = self._solve_momentary(pressed)

        layer = max(self.sticky | self.momentary | set([0]))
        if layer >= self.keymap.layer_count():
            layer = 0
        changed = (layer != self.layer)
        self.layer = layer
        return layer, changed

    def _base(self):
        return max(self.sticky | set([0]))

    def _handle_switches(self, added):
        """TG / TO / DF 只在按下的那一下改变常驻层。"""
        if not added or self.keymap is None:
            return
        base = self._base()
        for (r, c) in added:
            act = self.keymap.switch_layer(
                self.keymap.keycode_at(base, r, c))
            if not act:
                continue
            mode, n = act
            if mode in ("TO", "DF"):
                self.sticky = set([n])
            elif mode == "TG":
                if n in self.sticky:
                    self.sticky.discard(n)
                else:
                    self.sticky.add(n)

    def _solve_momentary(self, pressed):
        """按住的层键 -> 层号集合。"""
        found = set()
        if self.keymap is None:
            return found
        base = self._base()
        count = self.keymap.layer_count()
        for (r, c) in pressed:
            n = self.keymap.hold_layer(self.keymap.keycode_at(base, r, c))
            if n is not None and 0 <= n < count:
                found.add(n)
        return found

    def reset(self):
        self.sticky = set([0])
        self.momentary = set()
        self.held = set()
        self.layer = 0
