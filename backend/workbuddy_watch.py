# -*- coding: utf-8 -*-
"""
workbuddy_watch.py - 观察 WorkBuddy 会话文件，推断 AI 代理运行状态

WorkBuddy 没有对外的事件推送接口，但它会把每一轮对话的推理、工具调用、
工具结果逐条追加到会话 .jsonl 里（~/.workbuddy/projects/<项目 slug>/<会话>.jsonl）。
本模块用一个后台线程定期读取这些文件的**尾部**，按最后一条记录推断状态，
再写入 agents_status 存储（source="workbuddy"）。

为什么用「读文件」而不是「事件推送」：
    零侵入——不需要改 WorkBuddy 的配置、不占用它的网络端口，后端重启也不会
    影响它。代价是状态有最多一个轮询周期的延迟，且属于推断而非事实。

记录类型 -> 状态（依据实测的 JSONL 结构）：
    reasoning              -> thinking
    function_call 读类工具 -> reading   (Read / Glob / Grep / WebFetch / WebSearch)
    function_call 写类工具 -> writing   (Edit / Write)
    function_call 其它     -> working   (Bash / PowerShell / Task ...)
    function_call_result   -> working   (工具正在跑或刚跑完)
    message role=user      -> thinking  (用户刚发问，等待模型)
    message role=assistant -> writing   (正在输出回答)
    未知类型               -> working   (降级，绝不报错)
    文件安静超过 idle_quiet -> idle

线程安全：所有共享字典都在 self._lock 内读写；任何异常都在 run() 里被吞掉，
不会影响主数据流。
"""

import json
import os
import threading
import time

# 只读文件尾部这么多字节（一条记录通常 < 8KB，16KB 足够回退若干条）
TAIL_BYTES = 16384
# 读文件头部这么多字节找 ai-title（标题记录在会话早期写入）
HEAD_BYTES = 65536
# 超过该时长没写入的会话不再上报（避免历史会话刷满面板）
ACTIVE_WINDOW = 1800.0

# 工具名 -> 状态（小写匹配）
READ_TOOLS = ("read", "glob", "grep", "webfetch", "websearch", "ls",
              "notebookread", "knowledge_search", "toolsearch", "codesearch")
WRITE_TOOLS = ("edit", "write", "multiedit", "notebookedit", "applypatch")

# 这些记录类型不含状态信息，遇到时继续往前翻
SKIP_TYPES = ("session-meta", "file-history-snapshot", "ai-title",
              "summary", "compact", "checkpoint")


def _tail_records(path, size=TAIL_BYTES):
    """读取文件尾部并按行倒序返回可解析的记录（最新的在前）。

    只返回够用的若干条；解析失败的行（比如正在写入的半行）直接跳过。
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            start = max(0, end - size)
            fh.seek(start)
            chunk = fh.read(end - start)
    except Exception:
        return []

    text = chunk.decode("utf-8", "ignore")
    lines = text.splitlines()
    if start > 0 and lines:
        lines = lines[1:]          # 首行可能被截断，丢掉

    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
        if len(out) >= 12:
            break
    return out


def _map_record(rec):
    """单条记录 -> (状态, 是否工具在飞)，无法判断时返回 (None, False)。"""
    if not isinstance(rec, dict):
        return None, False
    rtype = rec.get("type")

    if rtype == "reasoning":
        return "thinking", False
    if rtype == "function_call":
        name = (rec.get("name") or "").lower()
        if name in READ_TOOLS:
            return "reading", True
        if name in WRITE_TOOLS:
            return "writing", True
        return "working", True
    if rtype == "function_call_result":
        return "working", False
    if rtype == "message":
        role = rec.get("role")
        if role == "user":
            return "thinking", False
        if role == "assistant":
            return "writing", False
        return None, False
    return None, False


def _read_title(path):
    """从文件头找 ai-title 记录的 aiTitle 字段（找不到返回 None）。"""
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(HEAD_BYTES)
    except Exception:
        return None
    for line in chunk.decode("utf-8", "ignore").splitlines():
        if "ai-title" not in line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        title = rec.get("aiTitle")
        if isinstance(title, str) and title.strip():
            return title.strip()[:120]
    return None


def _project_from_record(rec, fallback):
    """优先用记录里的 cwd 末级目录名，其次用项目 slug 目录名。"""
    if isinstance(rec, dict):
        cwd = rec.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            name = os.path.basename(cwd.replace("\\", "/").rstrip("/"))
            if name:
                return name
    return fallback


class WorkBuddyWatcher(object):
    """后台线程：轮询 WorkBuddy 会话文件并写入 agents_status。"""

    def __init__(self, store, roots=None, scan_interval=1.0, idle_quiet=6.0,
                 max_sessions=8, only_projects=None, debug=False):
        self.store = store
        # 统一展开 ~，否则启动时的 isdir 校验会失败
        self.roots = [os.path.expanduser(r) for r in (roots or [])]
        self.scan_interval = max(0.3, float(scan_interval))
        self.idle_quiet = max(1.0, float(idle_quiet))
        self.max_sessions = max(1, int(max_sessions))
        self.only_projects = [p for p in (only_projects or []) if p]
        self.debug = debug

        self._lock = threading.Lock()
        self._titles = {}       # path -> (title, 读取时间)
        self._applied = {}      # sid -> (state, epoch) 已写入的状态
        self._known = set()     # 当前已上报的 sid 集合
        self._stop = threading.Event()
        self._thread = None
        self.reason = ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self):
        if not self.roots:
            self.reason = "未配置扫描目录"
            return False
        alive = [r for r in self.roots if os.path.isdir(r)]
        if not alive:
            self.reason = "扫描目录均不存在: %s" % ", ".join(self.roots)
            return False
        self.reason = ""
        self._thread = threading.Thread(target=self._run,
                                        name="workbuddy-watch",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception as exc:      # 任何异常都不能让线程退出
                if self.debug:
                    print("[workbuddy_watch] 扫描异常（已忽略）: %s" % exc)
            self._stop.wait(self.scan_interval)

    def _iter_files(self):
        """遍历所有会话文件，产出 (path, sid, slug)。"""
        for root in self.roots:
            root = os.path.expanduser(root)
            if not os.path.isdir(root):
                continue
            try:
                slugs = os.listdir(root)
            except Exception:
                continue
            for slug in slugs:
                # slug 形如 d-project-cronixKeyboard，末级就是项目名
                folder = os.path.join(root, slug)
                if not os.path.isdir(folder):
                    continue
                try:
                    names = os.listdir(folder)
                except Exception:
                    continue
                for name in names:
                    if not name.endswith(".jsonl"):
                        continue
                    if name.endswith(".file-rollback.ndjson"):
                        continue
                    sid = name[:-len(".jsonl")]
                    yield os.path.join(folder, name), sid, slug

    def _scan_once(self):
        now = time.time()
        candidates = []

        for path, sid, slug in self._iter_files():
            try:
                mtime = os.path.getmtime(path)
            except Exception:
                continue
            if now - mtime > ACTIVE_WINDOW:
                continue

            records = _tail_records(path)
            if not records:
                continue

            state = None
            pending = False
            anchor = None
            for rec in records:
                if isinstance(rec, dict) and rec.get("type") in SKIP_TYPES:
                    continue
                state, pending = _map_record(rec)
                if state is not None:
                    anchor = rec
                    break
            if state is None:
                continue

            quiet = now - mtime
            # 工具还在飞（有调用没等到结果）时即使安静也不判空闲
            if not pending and quiet > self.idle_quiet:
                state = "idle"

            project = _project_from_record(anchor, slug)
            if self.only_projects:
                hit = any(p.lower() in project.lower() for p in self.only_projects)
                if not hit:
                    continue

            candidates.append({
                "path": path, "sid": sid, "state": state,
                "project": project, "mtime": mtime,
            })

        # 太多旧会话时只保留最近活跃的若干个
        candidates.sort(key=lambda c: c["mtime"], reverse=True)
        candidates = candidates[:self.max_sessions]

        seen = set()
        for cand in candidates:
            sid = cand["sid"]
            seen.add(sid)
            with self._lock:
                prev = self._applied.get(sid, (None, 0.0))[0]
            state = self._coalesce(sid, cand["state"], now)
            title = self._title(cand["path"], now)
            self.store.record(state, source="workbuddy", sid=sid,
                              title=title, project=cand["project"])
            if self.debug and prev != state:
                print("[workbuddy_watch] %s -> %s (%s)"
                      % (sid[:8], state, cand["project"]))

        # 掉出窗口的会话从面板移除，避免残留行
        with self._lock:
            gone = self._known - seen
            self._known = seen
        for sid in gone:
            self.store.forget(source="workbuddy", sid=sid)
            self._applied.pop(sid, None)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _coalesce(self, sid, state, now):
        """抑制 500ms 内的 writing/reading -> thinking 抖动（闪一下又回去）。"""
        with self._lock:
            prev, prev_at = self._applied.get(sid, (None, 0.0))
            if state != prev and state == "thinking" and \
                    (now - prev_at) < 0.5:
                return prev or state
            if state != prev:
                self._applied[sid] = (state, now)
            return state

    def _title(self, path, now):
        """标题缓存：命中且未过期直接返回；未取到则每 60s 重试一次。"""
        with self._lock:
            cached = self._titles.get(path)
        if cached and cached[0] is not None:
            return cached[0]
        if cached and now - cached[1] < 60.0:
            return cached[0]
        title = _read_title(path)
        with self._lock:
            self._titles[path] = (title, now)
        return title


def start(config, store, debug=False):
    """按 config["agents_status"]["workbuddy"] 启动观察器，返回实例或 None。"""
    cfg = {}
    try:
        cfg = ((config or {}).get("agents_status") or {}).get("workbuddy") or {}
    except Exception:
        cfg = {}
    if not cfg.get("enabled", True):
        print("[workbuddy_watch] 已在配置中关闭")
        return None

    home = os.path.expanduser("~")
    # 默认同时扫国内版与国际版：两套 session 是独立目录，
    # 不存在的目录会被 start() 自动过滤，不会报错
    roots = cfg.get("roots") or [
        os.path.join(home, ".workbuddy", "projects"),
        os.path.join(home, ".workbuddy-ai", "projects"),
    ]
    watcher = WorkBuddyWatcher(
        store,
        roots=roots,
        scan_interval=cfg.get("scan_interval", 1.0),
        idle_quiet=cfg.get("idle_quiet", 6.0),
        max_sessions=cfg.get("max_sessions", 8),
        only_projects=cfg.get("only_projects") or [],
        debug=debug,
    )
    if watcher.start():
        print("[workbuddy_watch] 观察已启动: %s" % ", ".join(watcher.roots))
    else:
        print("[workbuddy_watch] 未启动: %s" % watcher.reason)
    return watcher
