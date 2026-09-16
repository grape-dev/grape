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
    # CLI 本地 git 凭据/工作区（repos.py，体内 _repo_platform_token 强制 session）
    "repos_git_credential", "repos_workspace",
    # Agent 执行期自省（tasks.py，体内要求 auth_type==session_token）
    "tasks_current",
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
    schema["paths"] = paths
    app.openapi_schema = schema
    return schema