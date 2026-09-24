"""场景自定义环境变量（custom_fields）共享校验规则（Issue #236）。

单元供两处复用：
- src/api/routes/scenes.py 保存/读取时校验与掩码；
- src/api/event_pipeline.py 注入 agent env 时兜底校验。

规则：
1. 变量名统一大写（[A-Z][A-Z0-9_]*）；
2. 不与系统保留变量冲突（GRAPE_* 前缀 + _RESERVED_ENV_NAMES 集合）；
3. 密钥判定：命名约定后缀自动识别 + 手动 is_secret 覆盖；
4. 注入范围 scope：agent 名数组，默认 ["all"]，支持多选。
"""
import re

# 系统保留环境变量（显式集合 + GRAPE_ 前缀兜底）
_RESERVED_ENV_NAMES = {
    "PATH", "HOME", "USER", "USERNAME", "SHELL", "LANG", "LANGUAGE",
    "LC_ALL", "LC_CTYPE", "PWD", "OLDPWD", "HOSTNAME", "HOST", "TERM",
    "TMPDIR", "TMP", "TEMP", "EDITOR", "VISUAL", "SHLVL", "PS1",
    "OSTYPE", "MACHTYPE", "LOGNAME", "MAIL", "WINDIR", "SYSTEMROOT",
    "COMSPEC", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "REPO_FULL_NAME", "ACCOUNT", "BOT_USERNAME", "ISSUE_NUMBER",
    "PR_NUMBER", "GIT_ASKPASS", "GRAPE_SERVER_URL", "GRAPE_WORK_BRANCH",
    "GRAPE_SCENE_ID", "GRAPE_SESSION_TOKEN", "GRAPE_CLI_MODE",
    "BOT_GITCODE_TOKEN", "USER_ID", "CONVERSATION_ID",
}

# 密钥命名约定后缀：名称匹配任一后缀（大小写不敏感）→ 自动判定为 secret
_SECRET_SUFFIXES = (
    "_KEY", "_SK", "_SECRET", "_TOKEN", "_PASSWORD", "_PWD", "_CREDENTIALS",
)

_UPPER_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# 注入范围枚举（与场景 pipeline node agent 一致）
_SCOPE_AGENTS = ("all", "vod", "issue", "coding", "qa", "sre")
_SCOPE_SET = set(_SCOPE_AGENTS)


def is_reserved_name(name: str) -> bool:
    """变量名是否与系统保留环境变量冲突（GRAPE_ 前缀 + 显式集合）。"""
    return name.startswith("GRAPE_") or name in _RESERVED_ENV_NAMES


def is_upper_name(name: str) -> bool:
    """变量名是否符合大写规范（[A-Z][A-Z0-9_]*）。"""
    return bool(_UPPER_NAME_RE.match(name or ""))


def is_secret_name(name: str) -> bool:
    """按命名约定自动识别密钥类型：结尾匹配约定后缀（大小写不敏感）。"""
    up = (name or "").upper()
    return any(up.endswith(s) for s in _SECRET_SUFFIXES)


def compute_is_secret(name: str, is_secret: bool | None = None) -> bool:
    """最终密钥判定。

    三态语义：是_secret=None（未显式指定）→ 按命名约定自动识别；
    显式 True/False → 尊重用户选择（True 强制密钥、False 强制取消自动识别）。
    """
    if is_secret is not None:
        return bool(is_secret)
    return is_secret_name(name)


def custom_env_error(name: str) -> str:
    """校验自定义变量名，返回错误描述；合法返回空串。

    校验顺序：空名 → 大写 → GRAPE_ 前缀/保留集合冲突。
    """
    n = (name or "").strip()
    if not n:
        return "自定义环境变量名不能为空"
    if not is_upper_name(n):
        return f"自定义环境变量名需使用大写字母（如 MY_API_KEY），当前：{n}"
    if is_reserved_name(n):
        return f"变量名 '{n}' 与系统保留环境变量冲突，请更换名称"
    return ""


def normalize_scope(scope) -> list:
    """归一化注入范围 scope 为 agent 名数组。

    - 空 / 缺省 / [] / [""] → ["all"]
    - 旧单值字符串 "sre" / "all" → ["sre"] / ["all"]（兼容历史数据）
    - 数组：去重、大小写归一；剔除非法项；含 "all" 时收敛为 ["all"]（all 为全域，无需再列其它）
    - 剔除后为空 → ["all"]；原始含非法值但剔除后仍有合法项 → 返回合法数组
    """
    items: list = []
    if isinstance(scope, str):
        items = [scope]
    elif isinstance(scope, (list, tuple, set)):
        items = list(scope)
    else:
        items = []
    items = [(str(x) or "").strip().lower() for x in items if x not in (None, "")]
    # 去重保序
    seen = []
    for x in items:
        if x not in seen:
            seen.append(x)
    items = seen
    valid = [x for x in items if x in _SCOPE_SET]
    if not valid:
        return ["all"]
    if "all" in valid:
        return ["all"]
    return valid


def normalize_custom_field(f: dict) -> dict:
    """归一化一条 custom_field：{name, value, is_secret, scope}（容忍/清理多余字段）。

    输入可能来自旧数据（{key,...}）或新前端（{name,...}）；name 缺失时回退 key
    （仅作读取容错，写入侧仍以 name 为准）。
    is_secret 语义：dict 中显式存在 → 尊重该值（True/False）；未显式给 → 按命名约定自动识别。
    scope 语义：数组，默认 ["all"]；旧单值字符串自动包装。
    """
    name = ((f.get("name") or f.get("key") or "") if isinstance(f, dict) else "") or ""
    value = (f.get("value") or "") if isinstance(f, dict) else ""
    is_secret = compute_is_secret(name, f.get("is_secret") if isinstance(f, dict) else None)
    scope = normalize_scope(f.get("scope") if isinstance(f, dict) else None)
    name = name.strip()
    return {"name": name, "value": value, "is_secret": is_secret, "scope": scope}