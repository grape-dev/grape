"""PR 维度标识与任务 env_extra 解析（Issue #3375）。

入队/执行/接管/清理多处需要从事件或任务行解析 PR 号、派生 PR 目录键。
此前各调用点各写一份（routes/events.py、routes/runtime.py、takeover.py、
agent.py），且 agent.py 漏了 env_extra 为 JSON 字符串的分支。这里统一口径：
- 归一：去 # 前缀、纯数字、排除 "0"；
- env_extra：既接受 dict 也接受 JSON 字符串（加密信封等无法解析时按空处理）；
- 目录键：`pr-<n>`，与 repogit.issue_dir_for 的 PR 子键同源。
"""

import json


def normalize_pr_number(value) -> str:
    """PR 号归一：`#12` / `12` → `12`；非数字 / `0` / 空 → 空串。"""
    text = str(value or "").strip().lstrip("#")
    return text if text.isdigit() and text != "0" else ""


def pr_dir_key(pr_number) -> str:
    """PR 维度目录键：`pr-<n>`；非数字 / 0 / 空返回空串。"""
    _pr = normalize_pr_number(pr_number)
    return f"pr-{_pr}" if _pr else ""


def parse_env_extra(value) -> dict:
    """env_extra 归一为 dict：dict 原样返回；JSON 字符串解析；其余返回空 dict。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def pr_number_from_env_extra(value) -> str:
    """从 env_extra（dict 或 JSON 字符串）解析 PR_NUMBER 并归一。"""
    return normalize_pr_number(parse_env_extra(value).get("PR_NUMBER") or "")
