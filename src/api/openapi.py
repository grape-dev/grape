"""OpenAPI 定制渲染（Issue-45，M1）。

- 注入三通道 securitySchemes（Bearer / PRIVATE-TOKEN / access_token）。
- **只呈现对外安全路由**（依赖链含 `get_current_user_or_api_key`）：
  管理类/会话认证（`get_current_user`）与无认证路由不进契约，
  避免内部端点经 `/docs` / `/openapi.json` 暴露（issue-45-openapi-api-design.md §3.3/§六）。
- 对外 operation 的 `operation_id` 由 routes 侧显式声明（契约友好，CLI 按 tag+动词命名）。
- 版本号由 FastAPI `app.version` 承载（2.1.0），bump 触发 CLI 自动拉新。
"""
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})

_SECURITY_SCHEMES = {
    "apiKey":      {"type": "http", "scheme": "bearer"},
    "privateKey":  {"type": "apiKey", "in": "header", "name": "PRIVATE-TOKEN"},
    "accessToken": {"type": "apiKey", "in": "query", "name": "access_token"},
}


def _iter_deps(dependant):
    for dep in getattr(dependant, "dependencies", []) or []:
        yield dep
        yield from _iter_deps(dep)


def route_is_public(route) -> bool:
    """路由是否对外：依赖链中含 `get_current_user_or_api_key` 即是对外安全路由。"""
    if not isinstance(route, APIRoute):
        return False
    names = {getattr(d.call, "__name__", "") for d in _iter_deps(route.dependant)}
    return "get_current_user_or_api_key" in names


def custom_openapi(app):
    """定制 /openapi.json 渲染：注入 securitySchemes + 只保留对外安全路由 + 逐操作 security。"""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    schema.setdefault("components", {})["securitySchemes"] = _SECURITY_SCHEMES

    public_ops = {
        (r.path, m.lower())
        for r in app.routes if route_is_public(r)
        for m in (getattr(r, "methods", None) or set())
        if m.lower() in _METHODS
    }
    paths = {}
    for path, methods in (schema.get("paths") or {}).items():
        ops = {
            m: op for m, op in methods.items()
            if m in _METHODS and isinstance(op, dict) and (path, m) in public_ops
        }
        if not ops:
            continue
        for op in ops.values():
            op["security"] = [
                {"apiKey": []},
                {"privateKey": []},
                {"accessToken": []},
            ]
        paths[path] = ops
    schema["paths"] = paths
    app.openapi_schema = schema
    return schema