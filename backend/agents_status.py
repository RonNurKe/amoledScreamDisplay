# -*- coding: utf-8 -*-
"""
agents_status.py - AI 代理（Agent）状态扩展（多来源 / 多会话版）

统一管理各路 AI 编码代理的运行状态，供前端 Agents 面板展示。当前接入两类来源：

1. source="opencode"：由 opencode-status-sync 插件在状态变化时推送
   （GET /thinking 等 6 个端点，见 web_server.py），附带 sid/title/project；
2. source="workbuddy"：由 workbuddy_watch.py 观察 WorkBuddy 的会话 .jsonl
   推断得出（本进程内直接调用 record()，不经过 HTTP）。

也支持任意第三方通过通用入口推送：
    GET|POST /api/agents/<state>?source=<来源>&sid=<会话>&title=&project=

    /thinking  思考中    /reading  读取中    /writing  写入中
    /working   执行中    /idle     空闲      /error    出错

设计要点：
1. 按（source, sid）独立存储状态：每个会话有自己的 state/state_since/
   last_event，互不覆盖；无 sid 的推送落入该来源的 "" 全局键；
2. HTTP 处理线程写、采集线程读，全部状态用 threading.Lock 保护；
3. 只有状态真正变化时才更新该会话的 state_since 并追加事件历史；
   同会话同状态重复推送只刷新 last_event；
4. stale 在读取时实时计算：某会话超过 stale_timeout 秒没有任何事件即过期
   （代理意外退出时不会再发 idle/error，靠该机制避免面板卡死旧状态）；
5. 本模块绝不抛出异常影响主数据流（调用处也有兜底）；
6. parent（父会话 id，用于子任务嵌套）来自 opencode.db 的只读查询：
   结果做 TTL 缓存（最多每 PARENT_REFRESH_SECONDS 秒刷新一次），打开/查询
   失败时降级为空表（parent 全为 None），绝不让采集线程抛异常。
"""

import os
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

# 合法状态集合（与推送端点一一对应）
VALID_STATES = ("thinking", "reading", "writing", "working", "idle", "error")

# 来源标识长度上限
MAX_SOURCE_LEN = 24

# 默认来源（旧版 opencode 插件不带 source 参数时使用，保持向后兼容）
DEFAULT_SOURCE = "opencode"

# 默认过期阈值（秒）：超过该时间没有任何事件推送则标记 stale
DEFAULT_STALE_TIMEOUT = 300.0

# 会话快照 / 事件历史上限
MAX_SESSIONS = 20
HISTORY_SIZE = 30

# 死会话清理：超过该时长（秒）无活动的会话从快照中移除
# （后端重启/插件热重载会清空运行时跟踪，靠此机制清掉残留行）
SESSION_PRUNE_SECONDS = 86_400

# 请求参数长度上限（防异常超长输入）
MAX_ID_LEN = 128
MAX_TEXT_LEN = 200

# 父会话映射：从 opencode.db 只读查询 session_v2/session 表的 parent_id
# （与 opencode_usage.py 同一默认路径；采集线程每轮调用，靠 TTL 避免频繁开库）
DEFAULT_PARENT_DB = os.path.join(os.path.expanduser("~"), ".local", "share",
                                 "opencode", "opencode.db")
PARENT_REFRESH_SECONDS = 10.0


def _clip(value, limit):
    """字符串截断；非字符串返回 None。"""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    return value[:limit]


def _now_iso():
    """当前本地时间，ISO 格式字符串（与 collector._now_iso 保持一致）。"""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _epoch_to_iso(epoch):
    """epoch 秒 -> ISO 格式字符串；无效输入返回 None。"""
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 父会话映射（sid -> parent_id）：TTL 缓存 + 只读直读 opencode.db
# ---------------------------------------------------------------------------

_PARENT_LOCK = threading.Lock()
_PARENT_MAP = {}            # sid -> parent_id（非空字符串）
_PARENT_AT = 0.0             # 上次刷新的 epoch 秒
_PARENT_FAIL_LOGGED = False  # 失败提示只打印一次，避免刷屏


def _parent_db_path():
    """数据库路径：复用 opencode_usage 的配置解析（未就绪时退回默认路径）。"""
    try:
        import opencode_usage
        getter = getattr(opencode_usage, "_db_path", None)
        if callable(getter):
            path = getter()
            if path:
                return path
    except Exception:
        pass
    return DEFAULT_PARENT_DB


def _parent_table_exists(cur, name):
    row = cur.execute(
        "select 1 from sqlite_master where type='table' and name=? limit 1",
        (name,)).fetchone()
    return row is not None


def _fetch_parent_map():
    """直读数据库取 sid -> parent_id（仅在缓存过期时调用；失败返回空表）。"""
    global _PARENT_FAIL_LOGGED
    path = _parent_db_path()
    if not os.path.isfile(path):
        if not _PARENT_FAIL_LOGGED:
            print("[agents_status] 父会话映射：数据库不存在，parent 暂为 None "
                  "(%s)" % path)
            _PARENT_FAIL_LOGGED = True
        return {}
    try:
        # 只读模式（URI），不干扰正在运行的 opencode（与 opencode_usage 一致）
        uri = "file:%s?mode=ro" % path.replace("\\", "/")
        con = sqlite3.connect(uri, uri=True, timeout=3.0)
        try:
            cur = con.cursor()
            parent_map = {}
            # 优先 2.x 的 session_v2，再补旧版 session 的 id（仅补缺）
            for table in ("session_v2", "session"):
                if not _parent_table_exists(cur, table):
                    continue
                cols = set(c[1] for c in cur.execute(
                    "pragma table_info(%s)" % table))
                if "id" not in cols or "parent_id" not in cols:
                    continue
                rows = cur.execute(
                    "select id, parent_id from %s "
                    "where parent_id is not null and parent_id != ''" % table)
                for row in rows:
                    sid, parent_id = row[0], row[1]
                    if isinstance(sid, str) and isinstance(parent_id, str):
                        parent_id = parent_id.strip()
                        if sid and parent_id and sid not in parent_map:
                            parent_map[sid] = parent_id
            _PARENT_FAIL_LOGGED = False
            return parent_map
        finally:
            con.close()
    except Exception as exc:
        if not _PARENT_FAIL_LOGGED:
            print("[agents_status] 父会话映射查询失败（parent 沿用空表）: %s"
                  % exc)
            _PARENT_FAIL_LOGGED = True
        return {}


def _parent_map():
    """带 TTL 的父会话映射：最多每 PARENT_REFRESH_SECONDS 秒开一次库。

    刷新用非阻塞锁：别的线程正在刷新时直接沿用旧缓存，绝不卡采集线程；
    任何失败都降级为空表（parent 全为 None），不向上抛异常。
    """
    global _PARENT_MAP, _PARENT_AT
    if time.time() - _PARENT_AT < PARENT_REFRESH_SECONDS:
        return _PARENT_MAP
    if not _PARENT_LOCK.acquire(blocking=False):
        return _PARENT_MAP  # 刷新进行中：沿用旧缓存，不阻塞调用方
    try:
        # 双重检查：拿到锁后可能刚被别人刷新过
        if time.time() - _PARENT_AT < PARENT_REFRESH_SECONDS:
            return _PARENT_MAP
        _PARENT_MAP = _fetch_parent_map()  # 失败时为 {}，同样推进时间戳
        _PARENT_AT = time.time()
        return _PARENT_MAP
    finally:
        _PARENT_LOCK.release()


class _SessionState(object):
    """单个会话的状态条目（仅在被 lock 保护时读写）。"""

    # 实例属性注解（仅静态提示，Python 3.6+ 支持，运行时不生效）
    source: str
    sid: str
    title: Optional[str]
    project: Optional[str]
    state: Optional[str]
    state_since: Optional[str]
    last_epoch: Optional[float]

    def __init__(self, source, sid):
        self.source = source
        self.sid = sid
        self.title = None
        self.project = None
        self.state = None
        self.state_since = None
        self.last_epoch = None

    @property
    def key(self):
        return "%s:%s" % (self.source, self.sid)


class AgentsStatusStore(object):
    """线程安全的多来源多会话状态存储（单实例，见模块底部 get_store()）。"""

    def __init__(self, stale_timeout=DEFAULT_STALE_TIMEOUT):
        self._lock = threading.Lock()
        self._sessions = {}                    # "source:sid" -> _SessionState
        self._events = deque(maxlen=HISTORY_SIZE)  # 全局事件流（新 -> 旧按追加序）
        self.set_stale_timeout(stale_timeout)

    def set_stale_timeout(self, seconds):
        """设置过期阈值（秒）。"""
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            seconds = DEFAULT_STALE_TIMEOUT
        if seconds <= 0:
            seconds = DEFAULT_STALE_TIMEOUT
        with self._lock:
            self._stale_timeout = seconds

    # ------------------------------------------------------------------
    # 写入（HTTP 处理线程 / workbuddy_watch 线程调用）
    # ------------------------------------------------------------------

    def record(self, state, source=None, sid=None, title=None, project=None):
        """
        记录一次状态推送。
        :param state:   thinking/reading/writing/working/idle/error 之一
        :param source:  opencode / workbuddy / 其它自定义来源（缺省 opencode）
        :param sid:     会话 ID（缺省归入该来源的 "" 全局会话）
        :param title:   会话标题（对话名）
        :param project: 项目名
        :return:        是否为合法状态
        """
        if state not in VALID_STATES:
            return False
        source = _clip(source, MAX_SOURCE_LEN) or DEFAULT_SOURCE
        source = source.lower()
        sid = _clip(sid, MAX_ID_LEN) or ""
        title = _clip(title, MAX_TEXT_LEN)
        project = _clip(project, MAX_TEXT_LEN)
        now = time.time()
        stamp = _now_iso()

        with self._lock:
            key = "%s:%s" % (source, sid)
            sess = self._sessions.get(key)
            if sess is None:
                sess = _SessionState(source, sid)
                self._sessions[key] = sess
            if title:
                sess.title = title
            if project:
                sess.project = project
            # 状态变化才更新 since 并记事件；同状态重复推送只刷新时间戳
            if state != sess.state:
                sess.state = state
                sess.state_since = stamp
                self._events.append({
                    "state": state, "at": stamp, "source": source, "sid": sid,
                    "title": sess.title, "project": sess.project,
                })
            sess.last_epoch = now

            # 会话数量保护：超出上限时丢弃最久未活跃的会话
            if len(self._sessions) > MAX_SESSIONS:
                victims = sorted(
                    self._sessions.values(),
                    key=lambda s: s.last_epoch or 0,
                )
                for victim in victims[:len(self._sessions) - MAX_SESSIONS]:
                    if victim.key != key:
                        del self._sessions[victim.key]
        return True

    def forget(self, source=None, sid=None):
        """移除某个会话（workbuddy_watch 检测到会话文件消失时调用）。"""
        source = (_clip(source, MAX_SOURCE_LEN) or DEFAULT_SOURCE).lower()
        sid = _clip(sid, MAX_ID_LEN) or ""
        with self._lock:
            self._sessions.pop("%s:%s" % (source, sid), None)

    # ------------------------------------------------------------------
    # 读取（采集线程调用）
    # ------------------------------------------------------------------

    def collector(self):
        """
        扩展采集函数：输出 extensions.agents_status 的数据字段。
        注意：不包含 "enabled" 键（由 extensions.py 统一合并开关状态）。
        结构：
            {
              "state": ..., "state_since": ..., "last_event_at": ..., "stale": ...,
                # ↑ 最近活跃会话的镜像（兼容旧版前端/脚本）
              "sessions": [ {sid,source,title,project,state,state_since,
                             last_event_at,stale,parent}, ... ],  # 按最近活跃排序
              # ↑ parent = 父会话 sid（来自 opencode.db，无父/查询失败时为 None）
              "events": [ {state,at,source,sid,title,project}, ... ]  # 全局事件流
            }
        """
        with self._lock:
            sessions = list(self._sessions.values())
            events = list(self._events)
            stale_timeout = self._stale_timeout

            # 清理超过 24h 无活动的死会话（后端/插件热重载后的残留）
            now_epoch = time.time()
            for key in [s.key for s in sessions
                        if s.last_epoch is not None
                        and now_epoch - s.last_epoch > SESSION_PRUNE_SECONDS]:
                del self._sessions[key]
            sessions = list(self._sessions.values())

        # 父会话映射（TTL 缓存的只读 DB 查询，失败降级为空表）
        parent_map = _parent_map()

        now = time.time()
        out_sessions = []
        for sess in sessions:
            stale = (sess.state is not None and sess.last_epoch is not None and
                     (now - sess.last_epoch) > stale_timeout)
            out_sessions.append({
                "sid": sess.sid,
                "source": sess.source,
                "title": sess.title,
                "project": sess.project,
                "state": sess.state,
                "state_since": sess.state_since,
                "last_event_at": _epoch_to_iso(sess.last_epoch),
                "stale": stale,
                # parent 只在 opencode 侧有值，其它来源自然为 None
                "parent": parent_map.get(sess.sid) or None,
            })
        # 最近活跃的会话排前
        out_sessions.sort(key=lambda s: s["last_event_at"] or "", reverse=True)

        # 顶层字段 = 最近活跃会话（无任何会话时为空态）
        if out_sessions:
            active = out_sessions[0]
            top = {
                "state": active["state"],
                "state_since": active["state_since"],
                "last_event_at": active["last_event_at"],
                "stale": active["stale"],
            }
        else:
            top = {"state": None, "state_since": None,
                   "last_event_at": None, "stale": False}

        # 各来源的会话数量（前端可据此显示徽标统计）
        by_source = {}
        for sess in out_sessions:
            cur = by_source.get(sess["source"]) or {"sessions": 0, "active": 0}
            cur["sessions"] += 1
            if sess["state"] and sess["state"] != "idle" and not sess["stale"]:
                cur["active"] += 1
            by_source[sess["source"]] = cur

        top["sessions"] = out_sessions
        top["events"] = events
        top["sources"] = by_source
        return top


# ---------------------------------------------------------------------------
# 模块级单例：web_server 的请求处理、extensions 的注册、workbuddy_watch
# 的状态写入共用同一份状态
# ---------------------------------------------------------------------------

_store = None


def get_store(stale_timeout=None):
    """获取（并按需创建）全局状态存储。stale_timeout 仅首次创建时生效。"""
    global _store
    if _store is None:
        timeout = DEFAULT_STALE_TIMEOUT if stale_timeout is None else stale_timeout
        _store = AgentsStatusStore(stale_timeout=timeout)
    return _store


def record(state, source=None, sid=None, title=None, project=None):
    """便捷入口：向全局存储记录一次状态推送。"""
    return get_store().record(state, source=source, sid=sid,
                              title=title, project=project)
