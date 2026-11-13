# -*- coding: utf-8 -*-
"""
opencode_usage.py - 本地 OpenCode token 用量统计（直读 opencode.db，只读）

数据源：opencode 2.x 的 SQLite 数据库（默认 ~/.local/share/opencode/opencode.db）。
会话表（session_v2，旧版为 session）逐会话记录：
    cost / tokens_input / tokens_output / tokens_reasoning
    / tokens_cache_read / tokens_cache_write
    + time_created / time_updated / model(JSON) 等

设计要点：
1. 只读模式打开（file:...?mode=ro），不干扰正在运行的 opencode（WAL 安全）；
2. 结果缓存（默认 60s），采集线程每 2s 调 current() 只读缓存；
3. 表/列做存在性探测：优先 session_v2，退回 session；列缺失时按 0 处理，
   避免 opencode 升级改动 schema 时直接报错；
4. 失败容错：任何异常都返回 available=False 的结构，不影响主数据流；
5. 仅用标准库 sqlite3（Python 3.7 兼容，零第三方依赖）。
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

# 默认数据库路径（Windows: C:\Users\<user>\.local\share\opencode\opencode.db）
DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".local", "share",
                          "opencode", "opencode.db")

DEFAULT_CACHE_SECONDS = 60

# 统计的 token 维度：输出键 -> 列名
TOKEN_COLUMNS = (
    ("input", "tokens_input"),
    ("output", "tokens_output"),
    ("reasoning", "tokens_reasoning"),
    ("cache_read", "tokens_cache_read"),
    ("cache_write", "tokens_cache_write"),
)

_LOCK = threading.Lock()
_CFG = {}
_CACHE = None
_CACHE_AT = 0.0
_FAIL_LOGGED = False


def configure(config):
    """启动时调用一次：读取 opencode_usage 配置段与 use_mock 开关。
    兼容完整 config（含 "opencode_usage" 段）或该段本身。"""
    global _CFG
    cfg = {}
    if isinstance(config, dict):
        section = config.get("opencode_usage")
        if isinstance(section, dict):
            cfg = dict(section)
        else:
            cfg = dict(config)
        cfg["use_mock"] = bool(config.get("use_mock"))
    _CFG = cfg


def _db_path():
    path = _CFG.get("db_path")
    if not path:
        path = DEFAULT_DB
    return os.path.expanduser(path)


def _connect_ro(path):
    """只读打开（URI 模式），防止误写与锁冲突。"""
    uri = "file:%s?mode=ro" % path.replace("\\", "/")
    con = sqlite3.connect(uri, uri=True, timeout=3.0)
    con.row_factory = sqlite3.Row
    return con


def _table_exists(cur, name):
    row = cur.execute(
        "select 1 from sqlite_master where type='table' and name=? limit 1",
        (name,)).fetchone()
    return row is not None


def _columns(cur, table):
    return set(c[1] for c in cur.execute("pragma table_info(%s)" % table))


def _empty(reason, available=False):
    return {
        "available": available,
        "reason": reason,
        "db": _db_path(),
        "sessions": 0,
        "total": {"input": 0, "output": 0, "reasoning": 0,
                  "cache_read": 0, "cache_write": 0, "all": 0, "calls": 0},
        "today": {"input": 0, "output": 0, "all": 0, "calls": 0},
        "week": {"input": 0, "output": 0, "all": 0, "calls": 0},
        "month": {"input": 0, "output": 0, "all": 0, "calls": 0},
        "last_month": {"input": 0, "output": 0, "all": 0, "calls": 0},
        "cost": 0.0,
        "models": [],
        "by_session": [],
        "updated_at": None,
    }


def _basename(path):
    if not path:
        return ""
    text = str(path).replace("\\", "/").rstrip("/")
    return text.rsplit("/", 1)[-1] if text else ""


def _ms_to_iso(ms):
    try:
        value = float(ms)
        if value <= 0:
            return None
        return datetime.fromtimestamp(value / 1000.0).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


def _row_value(row, key, default=0):
    """sqlite3.Row 安全取值（列不存在时返回默认值）。"""
    try:
        value = row[key]
        return default if value is None else value
    except (IndexError, KeyError):
        return default


def _fetch():
    """直读数据库并汇总（仅在缓存过期时调用）。"""
    path = _db_path()
    if not os.path.isfile(path):
        return _empty("数据库不存在: %s" % path)

    con = _connect_ro(path)
    try:
        cur = con.cursor()
        # 优先 2.x 的 session_v2；旧库退回 session
        table = None
        for candidate in ("session_v2", "session"):
            if _table_exists(cur, candidate):
                table = candidate
                break
        if table is None:
            return _empty("未找到会话表（session_v2/session）")

        cols = _columns(cur, table)
        token_exprs = []
        for key, col in TOKEN_COLUMNS:
            if col in cols:
                token_exprs.append((key, "coalesce(sum(%s), 0)" % col))
            else:
                token_exprs.append((key, "0"))
        cost_expr = "coalesce(sum(cost), 0)" if "cost" in cols else "0"

        select = ", ".join("%s as %s" % (expr, key) for key, expr in token_exprs)
        agg = cur.execute(
            "select count(*) as sessions, %s, %s as cost from %s"
            % (select, cost_expr, table)).fetchone()

        total = {}
        all_sum = 0
        for key, _expr in token_exprs:
            value = int(agg[key] or 0)
            total[key] = value
            all_sum += value
        total["all"] = all_sum

        # 明细条数上限
        try:
            list_limit = int(_CFG.get("list_limit", 6))
        except (TypeError, ValueError):
            list_limit = 6
        if list_limit < 1:
            list_limit = 6

        # 今日/本月/上月 + 按模型：优先逐消息一遍解析（精确分桶）；
        # 旧库无 session_message 时回退会话级近似分桶
        today = {"input": 0, "output": 0, "all": 0, "calls": 0}
        week = {"input": 0, "output": 0, "all": 0, "calls": 0}
        month = {"input": 0, "output": 0, "all": 0, "calls": 0}
        last_month = {"input": 0, "output": 0, "all": 0, "calls": 0}

        total_calls = 0
        models = []
        if _table_exists(cur, "session_message"):
            msg_agg = _aggregate_messages(cur, list_limit)
            models = msg_agg["models"]
            today = msg_agg["today"]
            week = msg_agg["week"]
            month = msg_agg["month"]
            last_month = msg_agg["last_month"]
            total_calls = msg_agg.get("calls", 0)
        if not models:
            models = _models_from_sessions(cur, table, cols, list_limit)
            buckets = _buckets_from_sessions(cur, table, cols)
            today = buckets["today"]
            week = buckets["week"]
            month = buckets["month"]
            last_month = buckets["last_month"]

        # 累计模型调用次数（全历史：每条带 tokens 的 assistant 消息算一次）
        total["calls"] = total_calls

        # 按会话明细（Top N，按 输入+输出 排序）
        by_session = []
        if "id" in cols:
            wanted = [c for c in ("id", "title", "directory", "tokens_input",
                                  "tokens_output", "tokens_reasoning",
                                  "tokens_cache_read", "tokens_cache_write",
                                  "cost", "time_updated") if c in cols]
            rows = cur.execute(
                "select %s from %s" % (", ".join(wanted), table)).fetchall()
            items = []
            for r in rows:
                inp = int(_row_value(r, "tokens_input", 0))
                out = int(_row_value(r, "tokens_output", 0))
                reason = int(_row_value(r, "tokens_reasoning", 0))
                cache_r = int(_row_value(r, "tokens_cache_read", 0))
                cache_w = int(_row_value(r, "tokens_cache_write", 0))
                items.append({
                    "sid": str(_row_value(r, "id", "")),
                    "title": str(_row_value(r, "title", "") or ""),
                    "project": _basename(_row_value(r, "directory", "")),
                    "input": inp,
                    "output": out,
                    "all": inp + out + reason + cache_r + cache_w,
                    "cost": float(_row_value(r, "cost", 0.0)),
                    "updated": _ms_to_iso(_row_value(r, "time_updated", 0)),
                })
            items.sort(key=lambda x: x["input"] + x["output"], reverse=True)
            by_session = items[:list_limit]

        return {
            "available": True,
            "reason": "",
            "db": path,
            "sessions": int(agg["sessions"] or 0),
            "total": total,
            "today": today,
            "week": week,
            "month": month,
            "last_month": last_month,
            "cost": float(agg["cost"] or 0.0),
            "models": models,
            "by_session": by_session,
            "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
    finally:
        con.close()


def _model_name(raw):
    """model 列可能是 JSON 字符串（{"id","providerID"}）或普通字符串。"""
    if not raw:
        return "--"
    text = raw if isinstance(raw, str) else str(raw)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            mid = obj.get("id") or obj.get("modelID") or ""
            pid = obj.get("providerID") or obj.get("provider") or ""
            if mid and pid:
                return "%s/%s" % (pid, mid)
            return mid or pid or text
    except Exception:
        pass
    return text


def _model_key(model):
    """从消息 data 的 model 字段解析出 provider/model 名称；无法解析返回 None。"""
    if isinstance(model, dict):
        mid = model.get("id") or model.get("modelID") or ""
        pid = model.get("providerID") or model.get("provider") or ""
        if mid and pid:
            return "%s/%s" % (pid, mid)
        return mid or pid or None
    if isinstance(model, str) and model:
        return _model_name(model)
    return None


def _models_from_messages(cur, limit):
    """
    按模型聚合：逐条解析 session_message 的消息 data JSON
    （每条 assistant 消息自带 model / tokens / cost），
    精确反映"同一会话中途换模型"的真实用量分布。
    """
    return _aggregate_messages(cur, limit)["models"]


def _message_time_ms(data):
    """消息 data 里的时间戳（毫秒）：优先 completed，其次 created。"""
    t = data.get("time")
    if isinstance(t, dict):
        for key in ("completed", "created"):
            try:
                value = int(t.get(key) or 0)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                continue
        return 0
    if isinstance(t, (int, float)):
        return int(t)
    return 0


def _add_tokens(bucket, inp, out):
    bucket["input"] += inp
    bucket["output"] += out
    bucket["all"] += inp + out


def _aggregate_messages(cur, limit):
    """
    一遍解析 session_message 全部消息，同时产出：
      - models: 按实际使用模型聚合（精确）
      - today / week / month / last_month: 按消息时间戳分桶（本地时区）
      - calls: 带 tokens 的 assistant 消息数（每条 = 一次模型调用，按窗口分桶 + 全历史合计）
    返回 {"models": [...], "today": {...}, "week": {...}, "month": {...},
          "last_month": {...}, "calls": N}
    """
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today_start.replace(day=1)
    if month_start.month == 1:
        last_month_start = month_start.replace(year=month_start.year - 1, month=12)
    else:
        last_month_start = month_start.replace(month=month_start.month - 1)
    today_ms = int(time.mktime(today_start.timetuple()) * 1000)
    month_ms = int(time.mktime(month_start.timetuple()) * 1000)
    last_month_ms = int(time.mktime(last_month_start.timetuple()) * 1000)
    # 本周：最近一次周一 08:00（本地时区）
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=8, minute=0, second=0, microsecond=0)
    if now < week_start:
        week_start = week_start - timedelta(days=7)
    week_ms = int(time.mktime(week_start.timetuple()) * 1000)

    def new_bucket():
        return {"input": 0, "output": 0, "all": 0, "calls": 0}

    today = new_bucket()
    week = new_bucket()
    month = new_bucket()
    last_month = new_bucket()
    merged = {}
    total_calls = 0

    for r in cur.execute(
            "select data from session_message where data like '%\"tokens\"%'"):
        try:
            d = json.loads(r["data"])
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        tokens = d.get("tokens")
        if not isinstance(tokens, dict):
            continue
        try:
            inp = int(tokens.get("input") or 0)
            out = int(tokens.get("output") or 0)
        except (TypeError, ValueError):
            continue

        # 时间分桶（消息时间戳）。窗口是**累积**的：today ⊂ week ⊂ month，
        # 与 _buckets_from_sessions 的 SQL 口径一致（那里也是只设下界）。
        ts = _message_time_ms(d)
        if ts >= today_ms:
            _add_tokens(today, inp, out)
            today["calls"] += 1
        if ts >= week_ms:
            _add_tokens(week, inp, out)
            week["calls"] += 1
        if ts >= month_ms:
            _add_tokens(month, inp, out)
            month["calls"] += 1
        elif ts >= last_month_ms:
            _add_tokens(last_month, inp, out)
            last_month["calls"] += 1

        # 全历史调用次数（每条带 tokens 的消息算一次，与窗口无关）
        total_calls += 1

        # 按模型聚合
        name = _model_key(d.get("model"))
        if not name:
            continue
        item = merged.setdefault(name, {
            "model": name, "input": 0, "output": 0, "reasoning": 0,
            "cache_read": 0, "cache_write": 0, "cost": 0.0, "all": 0,
        })
        item["input"] += inp
        item["output"] += out
        item["reasoning"] += int(tokens.get("reasoning") or 0)
        cache = tokens.get("cache")
        if isinstance(cache, dict):
            item["cache_read"] += int(cache.get("read") or 0)
            item["cache_write"] += int(cache.get("write") or 0)
        item["cost"] += float(d.get("cost") or 0.0)

    for item in merged.values():
        item["all"] = (item["input"] + item["output"] + item["reasoning"]
                       + item["cache_read"] + item["cache_write"])

    return {
        "models": sorted(merged.values(),
                         key=lambda x: x["input"] + x["output"],
                         reverse=True)[:limit],
        "today": today,
        "week": week,
        "month": month,
        "last_month": last_month,
        "calls": total_calls,
    }


def _buckets_from_sessions(cur, table, cols):
    """回退：无 session_message 时按会话行 time_updated 粗分桶（近似）。
    会话行无法给出模型调用次数，故各桶 calls 恒为 0。"""
    def new_bucket():
        return {"input": 0, "output": 0, "all": 0, "calls": 0}

    result = {"today": new_bucket(), "week": new_bucket(),
              "month": new_bucket(), "last_month": new_bucket()}
    if "time_updated" not in cols:
        return result

    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today_start.replace(day=1)
    if month_start.month == 1:
        last_month_start = month_start.replace(year=month_start.year - 1, month=12)
    else:
        last_month_start = month_start.replace(month=month_start.month - 1)
    # 本周：最近一次周一 08:00（本地时区）
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=8, minute=0, second=0, microsecond=0)
    if now < week_start:
        week_start = week_start - timedelta(days=7)

    in_col = "tokens_input" if "tokens_input" in cols else None
    out_col = "tokens_output" if "tokens_output" in cols else None
    if not in_col and not out_col:
        return result

    def sum_expr():
        parts = []
        parts.append("coalesce(sum(%s), 0)" % in_col if in_col else "0")
        parts.append("coalesce(sum(%s), 0)" % out_col if out_col else "0")
        return ", ".join(parts)

    def query(low_ms, high_ms=None):
        sql = "select %s from %s where time_updated >= ?" % (sum_expr(), table)
        params = [low_ms]
        if high_ms is not None:
            sql += " and time_updated < ?"
            params.append(high_ms)
        row = cur.execute(sql, params).fetchone()
        inp = int(row[0] or 0)
        out = int(row[1] or 0)
        return {"input": inp, "output": out, "all": inp + out, "calls": 0}

    today_ms = int(time.mktime(today_start.timetuple()) * 1000)
    week_ms = int(time.mktime(week_start.timetuple()) * 1000)
    month_ms = int(time.mktime(month_start.timetuple()) * 1000)
    last_month_ms = int(time.mktime(last_month_start.timetuple()) * 1000)
    result["today"] = query(today_ms)
    result["week"] = query(week_ms)
    result["month"] = query(month_ms)
    result["last_month"] = query(last_month_ms, month_ms)
    return result


def _models_from_sessions(cur, table, cols, limit):
    """回退路径：按会话行的 model 单值聚合（旧库无 session_message 时使用）。"""
    if "model" not in cols:
        return []
    wanted = [c for c in ("model", "tokens_input", "tokens_output",
                          "tokens_reasoning", "tokens_cache_read",
                          "tokens_cache_write", "cost") if c in cols]
    rows = cur.execute(
        "select %s from %s" % (", ".join(wanted), table)).fetchall()
    merged = {}
    for r in rows:
        name = _model_name(_row_value(r, "model", ""))
        item = merged.setdefault(name, {
            "model": name, "input": 0, "output": 0, "reasoning": 0,
            "cache_read": 0, "cache_write": 0, "cost": 0.0, "all": 0,
        })
        for key, col in TOKEN_COLUMNS:
            item[key] += int(_row_value(r, col, 0))
        item["cost"] += float(_row_value(r, "cost", 0.0))
    for item in merged.values():
        item["all"] = (item["input"] + item["output"] + item["reasoning"]
                       + item["cache_read"] + item["cache_write"])
    return sorted(merged.values(), key=lambda x: x["input"] + x["output"],
                  reverse=True)[:limit]


def current():
    """扩展采集入口：带缓存的本地 token 用量汇总。"""
    global _CACHE, _CACHE_AT, _FAIL_LOGGED

    if _CFG.get("use_mock"):
        return _empty("mock 模式跳过数据库统计")

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
                    print("[opencode_usage] 统计失败（沿用旧数据）: %s" % exc)
                    _FAIL_LOGGED = True
                if _CACHE is None:
                    _CACHE = _empty("读取失败: %s" % exc)
            _CACHE_AT = time.monotonic()
        return dict(_CACHE)
