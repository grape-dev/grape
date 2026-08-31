import os

_DB_CONF_FILE = os.getenv("EVAL_DB_CONF_FILE", "~/.fetch-pr-skill/eval-db.conf")


def _load_db_config():
    config = {
        "host": os.getenv("EVAL_DB_HOST", ""),
        "user": os.getenv("EVAL_DB_USER", ""),
        "password": os.getenv("EVAL_DB_PASSWORD", ""),
        "database": os.getenv("EVAL_DB_NAME", ""),
    }
    path = os.path.expanduser(_DB_CONF_FILE)
    if not config["host"] and os.path.exists(path):
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip()
                    if key == "EVAL_DB_HOST" and not config["host"]:
                        config["host"] = value
                    elif key == "EVAL_DB_USER" and not config["user"]:
                        config["user"] = value
                    elif key == "EVAL_DB_PASSWORD" and not config["password"]:
                        config["password"] = value
                    elif key == "EVAL_DB_NAME" and not config["database"]:
                        config["database"] = value
    config["charset"] = "utf8mb4"
    return config


DB_CONFIG = _load_db_config()

SESSION_ROOT = os.path.expanduser(
    os.getenv("FETCH_PR_SKILL_SESSION_ROOT", "~/.fetch-pr-skill/sessions")
)

TOLERANCE = 0.10

EVAL_ITEMS = [
    {"id": 1, "category": "稳定性评估", "item_name": "功能可用", "max_score": 2.00, "dimension": "stability"},
    {"id": 2, "category": "稳定性评估", "item_name": "触发词充足度", "max_score": 2.00, "dimension": "stability"},
    {"id": 3, "category": "成本评估", "item_name": "消耗总token", "max_score": 1.00, "dimension": "cost"},
    {"id": 4, "category": "效率评估", "item_name": "完成时间", "max_score": 0.25, "dimension": "efficiency"},
    {"id": 5, "category": "效率评估", "item_name": "工具失败次数", "max_score": 0.25, "dimension": "efficiency"},
    {"id": 6, "category": "效率评估", "item_name": "重试次数", "max_score": 0.25, "dimension": "efficiency"},
    {"id": 7, "category": "效率评估", "item_name": "cli调用次数", "max_score": 0.25, "dimension": "efficiency"},
    {"id": 8, "category": "效率评估", "item_name": "sdk调用次数", "max_score": 0.25, "dimension": "efficiency"},
    {"id": 9, "category": "效率评估", "item_name": "api调用次数", "max_score": 0.25, "dimension": "efficiency"},
    {"id": 10, "category": "效率评估", "item_name": "LLM调用次数", "max_score": 0.25, "dimension": "efficiency"},
    {"id": 11, "category": "效率评估", "item_name": "上下文上限是否压缩", "max_score": 0.25, "dimension": "efficiency"},
    {"id": 12, "category": "安全评估", "item_name": "安全性", "max_score": 3.00, "dimension": "security"},
]

LOG_PATTERNS = {
    "token_input": r"tokens\.input=(\d+)",
    "token_output": r"tokens\.output=(\d+)",
    "token_reasoning": r"tokens\.reasoning=(\d+)",
    "token_cache_read": r"tokens\.cache\.read=(\d+)",
    "token_cache_write": r"tokens\.cache\.write=(\d+)",
    "error": r"level=ERROR|Traceback|BUG",
    "retry": r"[Rr]etry|[Rr]etries",
    "llm_call": r"message=stream\s|message=process\s|\[LLM\]|llm_call|chat_completion|completions\.create",
    "cli_call": r"→\s*(?:\x1b\[[0-9;]*m)?(Read|Write|Edit|Bash|bash|Glob|Grep|WebFetch|Task|Skill)\b|\$\s+(?:\x1b\[[0-9;]*m)?\w+|\[CLI\]|cli_call|exec_command|subprocess",
    "sdk_call": r"\[SDK\]|sdk_call|load_instance|\$\s+(?:\x1b\[[0-9;]*m)?hcloud\s|huaweicloud|hwcloud",
    "api_call": r"\[API\]|api_call|fetch_config|requests\.|https?://[a-z0-9.-]*huaweicloud\.com",
    "context_compress": r"context.*compress|truncat|context.*limit|truncated|context.*exceeded",
    "bug": r"BUG|Traceback|exception|AssertionError|level=ERROR.*BUG",
    "prompt_match": r"\[✓\]|\[✗\]|\[✔\]|\[✘\]|\[•\]|Workflow Step|[Ss]tep \d+(?:-\d+)?:",
    "loop_step": r"message=loop\s+session\.id=\S+\s+step=(\d+)",
    "tool_invoke": r"→\s*(?:\x1b\[[0-9;]*m)?(\w+)|\$\s+(?:\x1b\[[0-9;]*m)?(\w+)",
}

_pattern_file = os.getenv("LOG_PATTERN_FILE")
if _pattern_file and os.path.exists(_pattern_file):
    import json as _json
    with open(_pattern_file, "r") as _f:
        _overrides = _json.load(_f)
    LOG_PATTERNS.update(_overrides)

SECURITY_SKILL_PATH = os.path.expanduser("~/.agents/skills/huawei-cloud-skill-audit")
