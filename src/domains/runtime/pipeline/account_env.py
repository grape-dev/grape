"""Issue #1819: $ACCOUNT 个人账号形态校验与结构化告警公共实现。

集中维护账号形态契约（非空、不含 ``/`` / ``\\`` / 空白字符）与 ACCOUNT_* 告警
字段，供事件管道、任务 env 构建与 autopilot 调度复用，防止多处内联实现漂移。
"""
import json

from src.kernel.config import log


def normalize_account(value) -> str:
    """账号形态校验：合法个人账号为非空、不含 ``/`` / ``\\`` 及空白字符的字符串。"""
    acc = str(value or "").strip()
    if not acc:
        return ""
    if "/" in acc or "\\" in acc or any(ch.isspace() for ch in acc):
        return ""
    return acc


def task_creator_account(user_id) -> str:
    """Issue #1819：定时任务（autopilot）的任务创建者账号，作为其 $ACCOUNT 来源。

    autopilot 没有事件触发人，旧实现直接注入仓库全名。改为按 autopilot_tasks.created_by
    反查 user_accounts.username：查不到（created_by=0 的历史任务 / 用户已删）返回空串，
    由调用方留空并告警。
    语义边界：这是「任务创建者」，不是「本次触发人」，调用方须在 detail 里标明来源。
    """
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        uid = 0
    if not uid:
        return ""
    try:
        # user_accounts 归 mgmt：语句只留 mgmt public 一份（`user_accounts_*` 这个
        # 本地兜底句已下线）。失败保留原 fail-open 口径 —— 派生不出创建者就留空并告警，
        # 不让一次跨域读失败打断 autopilot 任务本身。
        from src.domains.mgmt import public as _mgmt_public_account
        return normalize_account(_mgmt_public_account.user_account_username_by_id_any(uid))
    except Exception as e:
        log.warning(f"ACCOUNT_TASK_CREATOR_LOOKUP_FAILED user_id={uid}: {e}")
        return ""


def owner_account_for_repo(repo_key: str) -> str:
    """Issue #3905：无 actor 事件（autopilot）的仓库属主兜底账号，单一实现。

    仅在明确放行 owner 回退的路径调用（autopilot 定时任务 / 无 actor 的
    ``autopilot_event``）。人工/事件驱动的 ``resolve_actor_account`` 默认**不启用**
    （见 Issue #1819：禁止把仓库属主当成触发人），保持既有安全语义。

    取值 = ``repo_key`` 的 owner 段（gitcode:owner/repo → owner），过账号形态校验；
    派生失败返回空串，由调用方留空并告警。
    """
    if not repo_key:
        return ""
    try:
        from src.kernel.repo_key import derive_repo_identity, from_repo_key
        _full, _owner, _name = derive_repo_identity(from_repo_key(repo_key), repo_key)
        return normalize_account(_owner)
    except Exception as e:  # noqa: BLE001 - 兜底派生失败不得打断派发
        log.warning(f"ACCOUNT_OWNER_FALLBACK_LOOKUP_FAILED repo_key={repo_key}: {e}")
        return ""


def log_account_alert(kind: str, level: str = "warning", **fields) -> None:
    """输出 ACCOUNT_* 结构化告警（kind 如 ACCOUNT_INVALID / ACCOUNT_MISSING）。

    ``level`` 允许调用方按场景选择日志级别（如 autopilot 常态缺失降为 info），
    字段集合由调用方传入，统一 JSON 序列化口径。
    """
    getattr(log, level)(kind + " " + json.dumps(fields, ensure_ascii=False))
