"""OpenAPI 定制渲染（Issue-45，M1）。

- 注入三通道 securitySchemes（Bearer / PRIVATE-TOKEN / access_token）。
- **只呈现对外安全路由**（依赖链含 `get_current_user_or_api_key`）：
  管理类/会话认证（`get_current_user`）与无认证路由不进契约，
  避免内部端点经 `/docs` / `/openapi.json` 暴露（issue-45-openapi-api-design.md §3.3/§六）。
- 对外 operation 的 `operation_id` 由 routes 侧显式声明（契约友好，CLI 按 tag+动词命名）。
- 版本号由 FastAPI `app.version` 承载（2.1.0），bump 触发 CLI 自动拉新。
"""
import re

from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})

# FastAPI 路由模板 `/contents/{path:path}` → OpenAPI 路径 `/contents/{path}`
_PATH_PARAM_CONV = re.compile(r"\{(\w+):\w+\}")


def _openapi_path(path: str) -> str:
    return _PATH_PARAM_CONV.sub(lambda m: "{" + m.group(1) + "}", path)

_SECURITY_SCHEMES = {
    "apiKey":      {"type": "http", "scheme": "bearer"},
    "privateKey":  {"type": "apiKey", "in": "header", "name": "PRIVATE-TOKEN"},
    "accessToken": {"type": "apiKey", "in": "query", "name": "access_token"},
}


def _iter_deps(dependant):
    for dep in getattr(dependant, "dependencies", []) or []:
        yield dep
        yield from _iter_deps(dep)


def _iter_routes(route):
    """递归产出 APIRoute（兼容 fastapi>=0.140 的惰性 `_IncludedRouter` 包装：

    新版 fastapi `include_router` 以 `original_router.routes` 惰性挂载，
    `app.routes` 里是 `_IncludedRouter` 而非 APIRoute，需展开后做对外判定。
    """
    if isinstance(route, APIRoute):
        yield route
        return
    orig = getattr(route, "original_router", None)
    if orig is not None:
        for r in getattr(orig, "routes", []) or []:
            yield from _iter_routes(r)


# 视为「对外安全路由」的认证依赖名（契约收录范围）
_PUBLIC_AUTH_DEPS = frozenset({
    "get_current_user_or_api_key",
    "get_user_or_none",  # a2a 软认证（JWT/session/api key 任一；None 交给内部 bot 鉴权）
})

# session 强制但依赖链不可见（体内 `_repo_token`/`_repo_platform_token` 判 auth_type）的 operation_id。
# 新增此类端点须在此登记（或纳入顺带路径规则），`x-token-scope` 才不会被误标为 dual（见
# docs/weekly-03/openapi-x-token-scope-design.md §3.2 强制机制）。
_SESSION_ONLY_OPERATIONS = frozenset({
    # git-repo 传输层（git_repo_api.py，体内 _repo_token 强制 session）
    "gitrepo_repo", "gitrepo_branches", "gitrepo_commits", "gitrepo_tree",
    "gitrepo_contents", "gitrepo_contents_root", "gitrepo_labels_list", "gitrepo_labels_create",
    "gitrepo_milestones", "gitrepo_issues_list", "gitrepo_issues_create",
    "gitrepo_issues_get", "gitrepo_issues_update", "gitrepo_issues_comments_list",
    "gitrepo_issues_comments_create", "gitrepo_pulls_list", "gitrepo_pulls_create",
    "gitrepo_pulls_get", "gitrepo_pulls_update", "gitrepo_pulls_merge",
    "gitrepo_pulls_comments_list", "gitrepo_pulls_comments_create", "gitrepo_pulls_files",
    "gitrepo_pulls_comments_update", "gitrepo_pulls_comments_delete",
    # CLI 本地 git 凭据/工作区（repos.py，体内 _repo_platform_token 强制 session）
    "repos_git_credential", "repos_workspace",
    # Agent 执行期动作（tasks.py / issues.py，体内 _require_session_repo_token / auth_type 强制 session）
    "tasks_current", "tasks_notify",
    # awaiting-input / pending-review 归属 issues（等用户双态），session_only 显式登记
    "issues_awaiting-input", "issues_pending-review", "issues_review-pass", "issues_review-reject",
})

# session_only 的路径前缀规则（覆盖表补充；新增 git-repo 端点只要进此命名空间即自动标注）
_SESSION_ONLY_PATH_PREFIXES = ("/api/git-repo/",)


def route_is_public(route) -> bool:
    """路由是否对外：依赖链中含对外认证依赖即收录进契约。"""
    if not isinstance(route, APIRoute):
        return False
    names = {getattr(d.call, "__name__", "") for d in _iter_deps(route.dependant)}
    return bool(names & _PUBLIC_AUTH_DEPS)


def token_scope(route) -> str | None:
    """推导对外路由的 token 作用域（`x-token-scope`）：

    - 依赖链命中 `require_no_session_token`                → user_only（156 处 D 类管理端点）
    - operation_id 在 `_SESSION_ONLY_OPERATIONS` 或路径前缀命中
      `_SESSION_ONLY_PATH_PREFIXES`                          → session_only（git-repo 传输/凭据）
    - 依赖链命中 `get_current_user_or_api_key` / `get_user_or_none` → dual
    - 其余（依赖链无对外认证）                               → None（不进契约或未知）
    """
    if not isinstance(route, APIRoute):
        return None
    names = {getattr(d.call, "__name__", "") for d in _iter_deps(route.dependant)}
    op_id = getattr(route, "operation_id", "") or ""
    path = getattr(route, "path", "") or ""
    if op_id in _SESSION_ONLY_OPERATIONS or path.startswith(_SESSION_ONLY_PATH_PREFIXES):
        return "session_only"
    if "require_no_session_token" in names:
        return "user_only"
    if names & _PUBLIC_AUTH_DEPS:
        return "dual"
    return None


def _event_only_openapi_paths(prefixes=("/api/a2a",)) -> dict:
    """把只注册在 Event 进程的路由并入契约（仅 schema，不注册真实路由）。

    Issue #9427（flask-blog 流水线停在 issue 节点）根因：/api/a2a 在 #699 Phase 4
    四进程拆分后只挂在 Event 进程（:38082，nginx 也把 /api/a2a 路由到 Event），
    而 CLI 是按 Mgmt 的 /openapi.json 生成命令的 —— 契约里没有 /api/a2a，
    `grape a2a handoff` 命令就不存在，agent 无法交办下一个节点，流水线直接停住
    （agent 结构化产出里写着「契约 2.2.0 无 a2a/tasks 命令」）。

    这里用同一个 router 在一个临时 app 上生成 schema，按前缀挑出 a2a 条目补进契约。
    只影响 /openapi.json 内容，Mgmt 进程不会因此新增真实端点。
    """
    try:
        from fastapi import FastAPI

        from src.domains.runtime.routes import webhook
    except Exception:  # pragma: no cover - 契约合并失败不应影响 /openapi.json
        return {}

    sub = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    sub.include_router(webhook.router)
    sub_schema = get_openapi(title=sub.title, version=sub.version, routes=sub.routes)

    merged: dict = {}
    for path, methods in (sub_schema.get("paths") or {}).items():
        if not any(path.startswith(p) for p in prefixes):
            continue
        ops = {m: op for m, op in methods.items() if m in _METHODS and isinstance(op, dict)}
        if not ops:
            continue
        for op in ops.values():
            op["security"] = [
                {"apiKey": []},
                {"privateKey": []},
                {"accessToken": []},
            ]
            # a2a 走 get_user_or_none 软认证 → dual（JWT/session/api key 任一）
            op.setdefault("x-token-scope", "dual")
        merged[path] = ops
    return merged


def custom_openapi(app):
    """定制 /openapi.json 渲染：注入 securitySchemes + 只保留对外安全路由 + 逐操作 security。"""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    schema.setdefault("components", {})["securitySchemes"] = _SECURITY_SCHEMES

    public_ops = {
        (_openapi_path(r.path), m.lower(), token_scope(r))
        for route in app.routes for r in _iter_routes(route)
        if route_is_public(r)
        for m in (getattr(r, "methods", None) or set())
        if m.lower() in _METHODS
    }
    public_paths = {(path, m) for path, m, _ in public_ops}
    scopes = {(path, m): scope for path, m, scope in public_ops}
    paths = {}
    for path, methods in (schema.get("paths") or {}).items():
        ops = {
            m: op for m, op in methods.items()
            if m in _METHODS and isinstance(op, dict) and (path, m) in public_paths
        }
        if not ops:
            continue
        for m, op in ops.items():
            op["security"] = [
                {"apiKey": []},
                {"privateKey": []},
                {"accessToken": []},
            ]
            scope = scopes.get((path, m))
            if scope:
                op["x-token-scope"] = scope
        paths[path] = ops

    # 契约完整性：/api/a2a 只注册在 Event 进程（nginx 同样路由到 Event），
    # 但 CLI 按本进程 /openapi.json 生成命令 —— 不补进来 agent 就没有 a2a 命令可用。
    for _p, _ops in _event_only_openapi_paths().items():
        paths.setdefault(_p, _ops)

    schema["paths"] = paths
    app.openapi_schema = schema
    return schema