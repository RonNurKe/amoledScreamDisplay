# -*- coding: utf-8 -*-
"""
workbuddy_usage.py - WorkBuddy 会话日志 token 用量统计（JSONL 增量扫描，只读）

数据源：WorkBuddy 会话日志
    ~/.workbuddy/projects/**/*.jsonl        国际版
    ~/.workbuddy-ai/projects/**/*.jsonl     国内版（两个都扫，去重逻辑见下）
每条 assistant 步骤记录里带完整用量：
    providerData.rawUsage
        prompt_tokens / completion_tokens / total_tokens
        prompt_cache_hit_tokens / prompt_cache_miss_tokens
        prompt_tokens_details.cached_tokens
        completion_tokens_details.reasoning_tokens
        credit                       （平台信用点，非美元，单独输出）
    providerData.requestModelName / model
    providerData.conversationRequestId + messageId   （去重键）

与 opencode_usage 的**口径对齐**（重要）：
    opencode 的 all = input + output + reasoning + cache_read + cache_write，
    其中 input 不含缓存命中部分、output 不含推理部分。
    WorkBuddy 的 total_tokens = prompt_tokens + completion_tokens，
    缓存命中已含在 prompt 内、推理已含在 completion 内。
    因此本模块做拆分：
        cache_read = prompt_cache_hit_tokens
        input      = prompt_tokens - cache_read
        reasoning  = completion_tokens_details.reasoning_tokens
        output     = completion_tokens - reasoning
    这样 all 恰好等于 total_tokens，且两路数字可以相加。

设计要点：
1. **增量解析**：按文件记录已读字节偏移，每次只解析新增尾部，
   避免 60s 一次把几十 MB 日志从头解析（日志会随会话变长）；
2. 截断/轮转检测：文件变小就重置该文件状态重头读；
3. 单条记录里 usage 出现在三处（providerData.rawUsage / providerData.usage /
   message.usage），**只取 rawUsage**，否则会三倍计数；
4. 去重键 = conversationRequestId + messageId，重复记录只算一次；
5. 失败容错：任何异常返回 available=False 的结构，不影响主数据流。
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta

# 默认扫描根目录（相对家目录）；config 里可覆盖
DEFAULT_ROOTS = (".workbuddy", ".workbuddy-ai")

DEFAULT_CACHE_SECONDS = 60
DEFAULT_LIST_LIMIT = 6

# 文件头读多少字节找 ai-title（标题记录在会话早期写入）
HEAD_BYTES = 64 * 1024

_LOCK = threading.Lock()
_CFG = {}
_CACHE = None
_CACHE_AT = 0.0
_FAIL_LOGGED = False

# abspath -> {offset,size,tail,recs,seen,sid,project,title,title_read}
_STATE = {}


# ---------------------------------------------------------------------------
# 配置 / 路径
# ---------------------------------------------------------------------------

def configure(config):
    """启动时调用一次：读取 workbuddy_usage 配置段。"""
    global _CFG
    cfg = {}
    if isinstance(config, dict):
        section = config.get("workbuddy_usage")
        if isinstance(section, dict):
            cfg = dict(section)
        cfg["use_mock"] = bool(config.get("use_mock"))
    _CFG = cfg


def _roots():
    """返回要扫描的会话根目录绝对路径列表。"""
    raw = _CFG.get("roots")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)) or not raw:
        raw = list(DEFAULT_ROOTS)
    home = os.path.expanduser("~")
    out = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        path = os.path.expanduser(item.strip())
        if not os.path.isabs(path):
            path = os.path.join(home, path)
        out.append(os.path.normpath(path))
    return out


def _iter_files(roots):
    """遍历会话日志 .jsonl（递归）。
    roots 既可以给 ~/.workbuddy，也可以直接给 ~/.workbuddy/projects，两种写法都认。
    """
    for root in roots:
        base = root
        if os.path.basename(os.path.normpath(base)) != "projects":
            base = os.path.join(base, "projects")
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.endswith(".jsonl"):
                    yield os.path.join(dirpath, name)


def _project_name(dirname):
    """项目 slug 目录名 -> 可读项目名。
    d-project-cronixKeyboard -> cronixKeyboard
    c-Users-RonNurKe-WorkBuddy -> WorkBuddy
    """
    text = (dirname or "").strip()
    if not text:
        return ""
    for sep in ("-", "_"):
        if sep in text:
            tail = text.rsplit(sep, 1)[-1]
            if tail:
                return tail
    return text


def _read_title(path):
    """从文件头找 ai-title 记录的 aiTitle 字段（找不到返回 None）。"""
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(HEAD_BYTES)
    except OSError:
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


# ---------------------------------------------------------------------------
# 增量解析
# ---------------------------------------------------------------------------

def _new_file_state(path):
    directory = os.path.basename(os.path.dirname(path))
    return {
        "offset": 0,
        "size": 0,
        "tail": b"",
        "recs": [],
        "seen": set(),
        "sid": os.path.splitext(os.path.basename(path))[0],
        "project": _project_name(directory),
        "title": None,
        "title_read": False,
    }


def _parse_line(line, state):
    """解析一行 JSON；不是用量记录就静默跳过。"""
    try:
        rec = json.loads(line.decode("utf-8", "ignore"))
    except Exception:
        return
    if not isinstance(rec, dict):
        return
    pdata = rec.get("providerData")
    if not isinstance(pdata, dict):
        return
    usage = pdata.get("rawUsage")
    if not isinstance(usage, dict):
        return

    # 去重：同一 (conversation, message) 只计一次
    key = (str(pdata.get("conversationRequestId") or ""),
           str(pdata.get("messageId") or rec.get("id") or ""))
    if key != ("", "") and key in state["seen"]:
        return
    if key != ("", ""):
        state["seen"].add(key)

    def _int(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    prompt = _int(usage.get("prompt_tokens"))
    completion = _int(usage.get("completion_tokens"))
    cached = _int(usage.get("prompt_cache_hit_tokens"))
    if not cached:
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = _int(details.get("cached_tokens"))
    if cached > prompt:
        cached = prompt
    reasoning = 0
    cdetails = usage.get("completion_tokens_details")
    if isinstance(cdetails, dict):
        reasoning = _int(cdetails.get("reasoning_tokens"))
    if reasoning > completion:
        reasoning = completion

    try:
        credit = float(usage.get("credit") or 0.0)
    except (TypeError, ValueError):
        credit = 0.0

    model = pdata.get("requestModelName") or pdata.get("model") or "--"

    ts = rec.get("timestamp")
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        ts = 0
    if ts and ts < 10000000000:      # 秒级时间戳 -> 毫秒
        ts *= 1000

    project = None
    cwd = rec.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        project = os.path.basename(cwd.replace("\\", "/").rstrip("/"))

    state["recs"].append((
        ts,
        str(model),
        prompt - cached,          # input（不含缓存）
        completion - reasoning,   # output（不含推理）
        reasoning,                # reasoning
        cached,                   # cache_read
        credit,                   # 平台信用点
        project or "",            # 记录级项目名（可选）
    ))


def _scan():
    """把所有文件的增量部分吃掉，写入 _STATE。"""
    paths = list(_iter_files(_roots()))
    alive = set(os.path.normpath(p) for p in paths)
    for known in list(_STATE):
        if known not in alive:          # 文件被删除
            _STATE.pop(known, None)

    for path in paths:
        path = os.path.normpath(path)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        state = _STATE.get(path)
        if state is None:
            state = _new_file_state(path)
            _STATE[path] = state

        if not state["title_read"]:
            state["title_read"] = True
            state["title"] = _read_title(path)

        if stat.st_size < state["size"]:      # 截断/轮转 -> 重头再来
            state["offset"] = 0
            state["tail"] = b""
            state["recs"] = []
            state["seen"] = set()

        if stat.st_size == state["size"]:     # 无变化
            continue

        try:
            with open(path, "rb") as fh:
                fh.seek(state["offset"])
                chunk = fh.read()
        except OSError:
            continue

        state["offset"] += len(chunk)
        state["size"] = stat.st_size
        if not chunk:
            continue

        buf = state["tail"] + chunk
        lines = buf.split(b"\n")
        state["tail"] = lines.pop()      # 最后一段可能不完整，留到下次
        for line in lines:
            if b'"rawUsage"' not in line:
                continue
            _parse_line(line, state)


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def _windows(now):
    """与 opencode_usage 保持一致的窗口起点（毫秒，本地时区）。"""
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today_start.replace(day=1)
    if month_start.month == 1:
        last_month_start = month_start.replace(year=month_start.year - 1, month=12)
    else:
        last_month_start = month_start.replace(month=month_start.month - 1)
    # 本周：最近一次周一 08:00（沿用 opencode_usage 的口径）
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=8, minute=0, second=0, microsecond=0)
    if now < week_start:
        week_start = week_start - timedelta(days=7)

    def ms(dt):
        return int(time.mktime(dt.timetuple()) * 1000)

    return {
        "today": ms(today_start),
        "week": ms(week_start),
        "month": ms(month_start),
        "last_month": ms(last_month_start),
    }


def _empty(reason, available=False):
    return {
        "available": available,
        "reason": reason,
        "source": "workbuddy",
        "roots": _roots(),
        "files": len(_STATE),
        "sessions": 0,
        "total": {"input": 0, "output": 0, "reasoning": 0,
                  "cache_read": 0, "cache_write": 0, "all": 0, "calls": 0},
        "today": {"input": 0, "output": 0, "all": 0, "calls": 0},
        "week": {"input": 0, "output": 0, "all": 0, "calls": 0},
        "month": {"input": 0, "output": 0, "all": 0, "calls": 0},
        "last_month": {"input": 0, "output": 0, "all": 0, "calls": 0},
        "cost": 0.0,
        "credit": 0.0,
        "models": [],
        "by_session": [],
        "updated_at": None,
    }


def _fetch():
    """扫描增量并汇总（仅在缓存过期时调用）。"""
    _scan()

    if not _STATE:
        return _empty("未找到 WorkBuddy 会话日志（%s）" % ", ".join(_roots()))

    windows = _windows(datetime.now())

    total = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0,
             "cache_write": 0, "all": 0, "calls": 0}
    credit_total = 0.0

    def new_bucket():
        return {"input": 0, "output": 0, "all": 0, "calls": 0}

    buckets = {"today": new_bucket(), "week": new_bucket(),
               "month": new_bucket(), "last_month": new_bucket()}

    models = {}
    sessions = {}
    has_data = False

    for path, state in _STATE.items():
        if not state["recs"]:
            continue
        has_data = True
        sid = state["sid"]
        sess = sessions.setdefault(sid, {
            "sid": sid,
            "title": state["title"] or "",
            "project": state["project"],
            "source": "workbuddy",
            "input": 0, "output": 0, "all": 0,
            "cost": 0.0, "credit": 0.0,
            "updated": None,
        })

        for (ts, model, inp, out, reason, cache_r, credit, proj) in state["recs"]:
            total["input"] += inp
            total["output"] += out
            total["reasoning"] += reason
            total["cache_read"] += cache_r
            total["calls"] += 1
            credit_total += credit

            sess["input"] += inp
            sess["output"] += out
            sess["all"] += inp + out + reason + cache_r
            sess["credit"] += credit
            if ts:
                stamp = datetime.fromtimestamp(ts / 1000.0).strftime(
                    "%Y-%m-%dT%H:%M:%S")
                if not sess["updated"] or stamp > sess["updated"]:
                    sess["updated"] = stamp
            if proj and not sess["project"]:
                sess["project"] = _project_name(proj)

            # 时间分桶（累积窗口：today ⊂ week ⊂ month，与 opencode_usage 口径一致）
            for name, low in (("today", windows["today"]),
                              ("week", windows["week"]),
                              ("month", windows["month"])):
                if ts >= low:
                    bucket = buckets[name]
                    bucket["input"] += inp
                    bucket["output"] += out
                    bucket["all"] += inp + out
                    bucket["calls"] += 1
            if ts < windows["month"] and ts >= windows["last_month"]:
                bucket = buckets["last_month"]
                bucket["input"] += inp
                bucket["output"] += out
                bucket["all"] += inp + out
                bucket["calls"] += 1

            # 模型聚合（大小写不同视为同一模型，取出现更多的写法）
            key = (model or "--").strip().lower()
            item = models.get(key)
            if item is None:
                item = {"model": (model or "--").strip(), "input": 0, "output": 0,
                        "reasoning": 0, "cache_read": 0, "cache_write": 0,
                        "cost": 0.0, "credit": 0.0, "all": 0, "_hits": 0,
                        "_alt": {}}
                models[key] = item
            item["_hits"] += 1
            item["_alt"][(model or "--").strip()] = \
                item["_alt"].get((model or "--").strip(), 0) + 1
            item["input"] += inp
            item["output"] += out
            item["reasoning"] += reason
            item["cache_read"] += cache_r
            item["credit"] += credit

    if not has_data:
        return _empty("会话日志里没有用量记录")

    total["all"] = (total["input"] + total["output"] + total["reasoning"]
                    + total["cache_read"] + total["cache_write"])

    try:
        limit = int(_CFG.get("list_limit", DEFAULT_LIST_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIST_LIMIT
    if limit < 1:
        limit = DEFAULT_LIST_LIMIT

    model_rows = []
    for item in models.values():
        # 用出现次数最多的写法做展示名（DeepSeek / Deepseek 这种大小写变体）
        if item["_alt"]:
            item["model"] = max(item["_alt"].items(), key=lambda kv: kv[1])[0]
        item["all"] = (item["input"] + item["output"] + item["reasoning"]
                       + item["cache_read"] + item["cache_write"])
        item.pop("_alt", None)
        item.pop("_hits", None)
        model_rows.append(item)
    model_rows.sort(key=lambda x: x["input"] + x["output"], reverse=True)

    session_rows = sorted(sessions.values(),
                          key=lambda x: x["input"] + x["output"], reverse=True)

    return {
        "available": True,
        "reason": "",
        "source": "workbuddy",
        "roots": _roots(),
        "files": len(_STATE),
        "sessions": len(sessions),
        "total": total,
        "today": buckets["today"],
        "week": buckets["week"],
        "month": buckets["month"],
        "last_month": buckets["last_month"],
        "cost": 0.0,          # WorkBuddy 侧无美元成本，只有 credit
        "credit": round(credit_total, 2),
        "models": model_rows[:limit],
        "by_session": session_rows[:limit],
        "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


def reset_cache():
    """清空增量状态与缓存（配置文件变更后调用）。"""
    global _CACHE, _CACHE_AT, _STATE
    with _LOCK:
        _STATE = {}
        _CACHE = None
        _CACHE_AT = 0.0


def current():
    """扩展采集入口：带缓存的 WorkBuddy token 用量汇总。"""
    global _CACHE, _CACHE_AT, _FAIL_LOGGED

    if _CFG.get("use_mock"):
        return _empty("mock 模式跳过日志统计")

    try:
        cache_seconds = float(_CFG.get("cache_seconds", DEFAULT_CACHE_SECONDS))
    except (TypeError, ValueError):
        cache_seconds = DEFAULT_CACHE_SECONDS
    if cache_seconds < 5:
        cache_seconds = 5.0

    with _LOCK:
        expired = (time.monotonic() - _CACHE_AT) >= cache_seconds
        if _CACHE is None or expired:
            try:
                _CACHE = _fetch()
                _FAIL_LOGGED = False
            except Exception as exc:
                if not _FAIL_LOGGED:
                    print("[workbuddy_usage] 统计失败（沿用旧数据）: %s" % exc)
                    _FAIL_LOGGED = True
                if _CACHE is None:
                    _CACHE = _empty("读取失败: %s" % exc)
            _CACHE_AT = time.monotonic()
        return dict(_CACHE)
