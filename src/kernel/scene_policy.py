"""场景化会话作用域与取券策略（单一判定点）。

为什么要这一层（2026-11-22，Issue #3713 / #1946 现场）：

同一个「会话 token 的 repo_key」被十来个端点各自拿去做 `==` 比对，结果同类问题反复出现：

  · 换券（repos.py::_repo_platform_token）要求同仓，但它自己的取券口径与 TokenResolver 又不同；
  · tasks/notify（tasks.py:257 → issues_center.py:111）要求同仓，可 chat 的会话 token 是
    `chat:<user_id>` 伪作用域（chat.py:1055/1362/2297），永远不可能相等 → 必 403；
  · 跨仓下派（源仓 agent 给子 issue 所在仓派任务）同理会撞 403；
  · project-info / skills / plugins / issues 各写一套 403 文案，调用方分不清「该绑仓」还是「真越权」。

所以把「场景」抽成枚举：**每个调用点只声明自己属于哪个场景**，作用域规则、允许跨仓与否、
错误码、以及取券口径（rc 显式优先 / 策略链）全部由一张策略表派生。新增调用点 = 选一个场景，
而不是再写一遍 if repo_key != target。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class Scene(str, enum.Enum):
    """调用场景枚举（值即日志/契约里的场景名，便于 grep 与审计）。"""

    GIT_CREDENTIAL = "git_credential"     # 换券：给工作区 clone/push 用，必须同仓
    PROJECT_INFO = "project_info"         # 项目知识基线读取，必须同仓
    ISSUE_OP = "issue_op"                 # 卡片/评论类写操作，必须同仓
    SKILL_UPLOAD = "skill_upload"         # 仓库级技能/插件上传，必须同仓
    TASK_NOTIFY = "task_notify"           # 任务分配/换人/接管：允许跨仓（要有用户可见权）
    TASK_ACCESS = "task_access"           # agent 自省自己的任务（按 session_key=task_id）：必须同仓
    EVENT_STREAM = "event_stream"         # 执行期事件流/日志，必须同仓
    DISPATCH = "dispatch"                 # 跨仓下派：允许跨仓（要有 dispatch 关系或可见权）


@dataclass(frozen=True)
class ScenePolicy:
    """一个场景的完整规则（作用域 + 取券口径 + 错误码）。"""

    scope: str                              # repo_bound | task_plane | service_only
    credential_source: str                  # explicit_rc_first | resolver_chain | none
    allow_cross_repo: bool = False
    allow_pseudo_scope: bool = False        # chat:<uid> 伪作用域是否可走用户可见权校验
    legacy_unbound_ok: bool = False         # 空 repo_key（历史 token）是否按旧行为放行
    require_same_provider: bool = False     # 同名不同平台不算同仓（两套凭据）
    code_pseudo: str = "SESSION_NOT_REPO_BOUND"
    code_mismatch: str = "REPO_SCOPE_MISMATCH"
    code_cross: str = "CROSS_REPO_DENIED"
    code_provider: str = "PROVIDER_SCOPE_MISMATCH"


#: 策略表是唯一真源：改行为改这里，不改调用点。
SCENE_POLICIES: dict[Scene, ScenePolicy] = {
    Scene.GIT_CREDENTIAL: ScenePolicy(
        scope="repo_bound", credential_source="explicit_rc_first",
        require_same_provider=True,
        legacy_unbound_ok=True),  # 历史 tk_ 无 repo_key 的行：保持旧行为，待存量清零后关闭
    Scene.PROJECT_INFO: ScenePolicy(scope="repo_bound", credential_source="none",
                                    legacy_unbound_ok=True),
    Scene.ISSUE_OP: ScenePolicy(scope="repo_bound", credential_source="resolver_chain"),
    Scene.SKILL_UPLOAD: ScenePolicy(scope="repo_bound", credential_source="none"),
    Scene.EVENT_STREAM: ScenePolicy(scope="repo_bound", credential_source="none"),
    # 任务平面：chat / 跨仓下派必须能过，靠「用户对目标仓可见」而不是 token 仓绑定
    Scene.TASK_NOTIFY: ScenePolicy(scope="task_plane", credential_source="none",
                                    allow_cross_repo=True, allow_pseudo_scope=True),
    # 自省类（按 session_key 定位自己的任务）不跨仓：agent 只属于它那条任务
    Scene.TASK_ACCESS: ScenePolicy(scope="repo_bound", credential_source="none"),
    Scene.DISPATCH: ScenePolicy(scope="task_plane", credential_source="none",
                                 allow_cross_repo=True, allow_pseudo_scope=True),
}


@dataclass(frozen=True)
class Decision:
    """判定结果：ok=False 时带可判定的 code 与 message（调用方映射成 HTTP 错误）。"""

    ok: bool
    code: str = ""
    message: str = ""
    cross_repo: bool = False
    pseudo_scope: bool = False


def is_pseudo_scope(repo_key: str) -> bool:
    """伪作用域：空，或 chat 的 `chat:<user_id>`（不是真实托管仓库）。"""
    rk = str(repo_key or "").strip()
    return (not rk) or rk.startswith("chat:")


def _split(repo_key: str) -> "tuple[str, str]":
    """`provider:owner/repo` → (provider, owner/repo)；无前缀按 gitcode。"""
    rk = str(repo_key or "").strip()
    if ":" in rk:
        provider, _, rest = rk.partition(":")
        return provider, rest
    return "gitcode", rk


def decide(scene: Scene, session_repo_key: str, target_repo_key: str, *,
           session_provider: str = "", target_provider: str = "",
           access_check=None) -> Decision:
    """场景化作用域判定。

    Args:
        scene: 调用场景（枚举，@see SCENE_POLICIES）
        session_repo_key: 会话 token 携带的 repo_key（可为 `chat:<uid>` 或空）
        target_repo_key: 本次操作的目标仓库 repo_key
        session_provider / target_provider: 平台标识（不传则从 repo_key 前缀取）
        access_check: `(repo_key) -> bool`，任务平面跨仓/伪作用域时用它校验
                      「该会话用户对目标仓是否有可见权」；None 视为无权。
    """
    policy = SCENE_POLICIES.get(scene)
    if policy is None:  # 未登记场景一律拒绝，逼调用方显式声明
        return Decision(False, "SCENE_NOT_REGISTERED",
                        f"未登记的调用场景：{scene}")

    target = str(target_repo_key or "").strip()
    t_provider = (target_provider or _split(target)[0]).strip().lower()
    s_provider = (session_provider or _split(session_repo_key)[0]).strip().lower()

    if policy.scope == "service_only":
        return Decision(False, "SESSION_NOT_ALLOWED_FOR_SERVICE",
                        "该接口仅接受内部服务凭据，不接受会话 token")

    raw = str(session_repo_key or "").strip()
    if not raw and policy.legacy_unbound_ok:
        # 历史 token 没有 repo_key（本仓实测 0 行）：保持旧行为，等存量清零后把
        # legacy_unbound_ok 置 False 即可收口 —— 不需要再改调用点。
        return Decision(True, pseudo_scope=True)

    pseudo = is_pseudo_scope(session_repo_key)
    s_rest = _split(session_repo_key)[1]
    t_rest = _split(target)[1]
    same_repo = (not pseudo) and s_rest == t_rest
    same_provider = (not policy.require_same_provider) or (s_provider == t_provider)

    if same_repo and same_provider:
        return Decision(True)

    # 同 owner/repo、不同平台 = 两套凭据（同名不算同仓）
    if same_repo and not same_provider:
        return Decision(False, policy.code_provider,
                        f"平台不匹配：会话绑定 {s_provider}，目标 {t_provider}")

    if pseudo:
        if not (policy.allow_pseudo_scope and policy.allow_cross_repo):
            return Decision(False, policy.code_pseudo,
                            "会话 token 未绑定仓库作用域（chat/空作用域），"
                            "该场景需要先绑定仓库", pseudo_scope=True)
        if access_check is not None and access_check(target):
            return Decision(True, cross_repo=True, pseudo_scope=True)
        return Decision(False, policy.code_pseudo,
                        "会话 token 未绑定仓库作用域，且当前用户对目标仓无可见权",
                        pseudo_scope=True)

    # 真实作用域但不同仓
    if policy.allow_cross_repo:
        if access_check is not None and access_check(target):
            return Decision(True, cross_repo=True)
        return Decision(False, policy.code_cross,
                        f"跨仓操作被拒：会话绑定 {session_repo_key}，目标 {target}，"
                        f"且当前用户对目标仓无可见权")
    return Decision(False, policy.code_mismatch,
                    "仓库作用域不匹配")


def credential_source(scene: Scene) -> str:
    """该场景的取券口径（Issue #3713：换券与建树必须同源）。"""
    policy = SCENE_POLICIES.get(scene)
    return policy.credential_source if policy else "resolver_chain"