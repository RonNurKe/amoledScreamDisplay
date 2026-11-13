# -*- coding: utf-8 -*-
"""
token_merge.py - 把多个本地用量来源合并成一份同结构的汇总

各来源（opencode_usage / workbuddy_usage）输出的是同一套 payload 结构，
本模块只做加法与去重合并，不重新统计：

    total / today / week / month / last_month  逐字段相加
    cost / credit / sessions                   相加
    models                                     按模型名（忽略大小写）合并
    by_session                                 按 (source, sid) 合并，绝不跨来源撞键
    updated_at                                 取最新

为什么不在前端做：合并规则要跟统计口径一起维护，放在后端可被任何客户端复用
（手机面板、curl、以后可能的其他前端），也不用把两坨明细都传到前端再算。
"""

TOKEN_KEYS = ("input", "output", "reasoning", "cache_read", "cache_write", "all")
BUCKET_KEYS = ("input", "output", "all", "calls")


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value):
    return int(_num(value))


def _sum_bucket(target, source):
    for key in BUCKET_KEYS:
        target[key] = target.get(key, 0) + _int((source or {}).get(key))
    return target


def _keep_source_diversity(rows, limit, key="source"):
    """截断成 limit 条，但保证每个来源至少有一条露出来。

    否则 opencode 210 个会话会把 7 个 WorkBuddy 会话整个挤出榜单，
    ALL 视图里就完全看不到 WB 的贡献了。
    """
    if limit <= 0:
        return []
    top = list(rows[:limit])
    present = set(str(r.get(key) or "") for r in top)
    extra = []
    for row in rows[limit:]:
        name = str(row.get(key) or "")
        if not name or name in present:
            continue
        # 合并行的 source 是 "opencode+workbuddy"，按单来源拆开判断
        if any(part in present for part in name.split("+")):
            continue
        present.add(name)
        extra.append(row)
    if extra:
        top = top[:max(0, limit - len(extra))] + extra
    return top


def combine(payloads, list_limit=6):
    """合并若干 usage payload。

    :param payloads:   payload 列表（None / 非 dict 会被忽略）
    :param list_limit: models / by_session 保留条数
    :return:           与单一来源结构一致的汇总 payload
    """
    items = [p for p in payloads if isinstance(p, dict)]
    if not items:
        return {}

    total = dict((k, 0) for k in TOKEN_KEYS)
    total["calls"] = 0
    buckets = dict((name, {"input": 0, "output": 0, "all": 0, "calls": 0})
                   for name in ("today", "week", "month", "last_month"))

    cost = 0.0
    credit = 0.0
    sessions = 0
    updated = None
    reasons = []
    sources = []
    models = {}
    by_session = {}

    for payload in items:
        source = payload.get("source") or "opencode"
        available = bool(payload.get("available"))
        if not available and payload.get("reason"):
            reasons.append("%s: %s" % (source, payload.get("reason")))

        sources.append({
            "source": source,
            "available": available,
            "sessions": _int(payload.get("sessions")),
            "total": _int((payload.get("total") or {}).get("all")),
        })

        for key in TOKEN_KEYS:
            total[key] += _int((payload.get("total") or {}).get(key))
        total["calls"] += _int((payload.get("total") or {}).get("calls"))

        for name in buckets:
            _sum_bucket(buckets[name], payload.get(name))

        cost += _num(payload.get("cost"))
        credit += _num(payload.get("credit"))
        sessions += _int(payload.get("sessions"))

        stamp = payload.get("updated_at")
        if stamp and (updated is None or stamp > updated):
            updated = stamp

        for row in payload.get("models") or []:
            name = str(row.get("model") or "--")
            key = name.strip().lower()
            item = models.get(key)
            if item is None:
                item = {"model": name, "input": 0, "output": 0, "reasoning": 0,
                        "cache_read": 0, "cache_write": 0, "cost": 0.0,
                        "credit": 0.0, "all": 0, "_sources": []}
                models[key] = item
            for field in TOKEN_KEYS:
                item[field] = item.get(field, 0) + _int(row.get(field))
            item["cost"] = item.get("cost", 0.0) + _num(row.get("cost"))
            item["credit"] = item.get("credit", 0.0) + _num(row.get("credit"))
            if source not in item["_sources"]:
                item["_sources"].append(source)

        for row in payload.get("by_session") or []:
            sid = str(row.get("sid") or "")
            key = (source, sid)
            item = by_session.get(key)
            if item is None:
                item = {
                    "sid": sid,
                    "title": row.get("title") or "",
                    "project": row.get("project") or "",
                    "source": source,
                    "input": 0, "output": 0, "all": 0,
                    "cost": 0.0, "credit": 0.0,
                    "updated": row.get("updated"),
                }
                by_session[key] = item
            for field in ("input", "output", "all"):
                item[field] = item.get(field, 0) + _int(row.get(field))
            item["cost"] = item.get("cost", 0.0) + _num(row.get("cost"))
            item["credit"] = item.get("credit", 0.0) + _num(row.get("credit"))
            stamp = row.get("updated")
            if stamp and (not item.get("updated") or stamp > item["updated"]):
                item["updated"] = stamp

    model_rows = list(models.values())
    for item in model_rows:
        # all 用各维度重算，避免来源之间 all 口径不一致时被放大
        item["all"] = (item.get("input", 0) + item.get("output", 0)
                       + item.get("reasoning", 0) + item.get("cache_read", 0)
                       + item.get("cache_write", 0))
        item["source"] = "+".join(item.pop("_sources", [])) or None
    model_rows.sort(key=lambda x: x["input"] + x["output"], reverse=True)

    session_rows = sorted(by_session.values(),
                          key=lambda x: x["input"] + x["output"], reverse=True)

    return {
        "available": any(s["available"] for s in sources),
        "reason": "; ".join(reasons),
        "source": "all",
        "sources": sources,
        "sessions": sessions,
        "total": total,
        "today": buckets["today"],
        "week": buckets["week"],
        "month": buckets["month"],
        "last_month": buckets["last_month"],
        "cost": round(cost, 4),
        "credit": round(credit, 2),
        "models": _keep_source_diversity(model_rows, list_limit),
        "by_session": _keep_source_diversity(session_rows, list_limit),
        "updated_at": updated,
    }
