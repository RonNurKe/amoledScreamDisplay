# -*- coding: utf-8 -*-
"""
opencode_status.py - [已弃用] 兼容层

本模块已改名为 agents_status.py（支持多来源：opencode / workbuddy / 其它）。
这里保留薄薄的转发，避免旧脚本或外部调用方 import 时报错。
新代码请直接 `import agents_status`。
"""

from agents_status import (          # noqa: F401
    AgentsStatusStore as OpencodeStatusStore,
    DEFAULT_SOURCE,
    DEFAULT_STALE_TIMEOUT,
    HISTORY_SIZE,
    MAX_SESSIONS,
    VALID_STATES,
    get_store,
    record,
)
