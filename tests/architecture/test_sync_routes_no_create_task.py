"""回归护栏：同步路由（`def`）里不得出现 `asyncio.create_task` / `ensure_future`。

背景（PR !1929 架构守护 F1/F2）：把 async 路由改成同步 `def` 后，路由会被
FastAPI 丢进线程池执行，线程内没有运行中的事件循环，
`asyncio.create_task(...)` 直接抛 `RuntimeError: no running event loop`
→ 端点从 200 退化为 500，后台副作用（飞书通知 / 扫码协程）静默丢失。

本测试纯 AST，不建库、不起应用，CI 里毫秒级完成。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ROUTES_DIRS = [ROOT / "src" / "domains"]

#: 线程池里调用必然失败的「起任务」写法
_LOOP_ONLY_CALLS = {"create_task", "ensure_future"}


def _is_route_handler(node: ast.AST) -> bool:
    for deco in getattr(node, "decorator_list", []) or []:
        tgt = deco.func if isinstance(deco, ast.Call) else deco
        if isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name):
            if tgt.value.id in ("router", "app") and tgt.attr.lower() in (
                "get", "post", "put", "patch", "delete", "api_route",
            ):
                return True
    return False


def _loop_only_hits(node: ast.AST) -> list[int]:
    lines = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute) and f.attr in _LOOP_ONLY_CALLS:
                if isinstance(f.value, ast.Name) and f.value.id in ("asyncio", "loop", "event_loop"):
                    lines.append(sub.lineno)
                elif isinstance(f.value, ast.Attribute) and f.value.attr in ("loop", "event_loop"):
                    lines.append(sub.lineno)
    return lines


def _iter_route_files():
    for base in ROUTES_DIRS:
        for p in sorted(base.rglob("*.py")):
            if "routes" in p.parts or p.name in ("server.py", "app.py"):
                yield p


@pytest.mark.parametrize("path", list(_iter_route_files()), ids=lambda p: str(p.relative_to(ROOT)))
def test_sync_route_must_not_start_asyncio_tasks(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_route_handler(node):
            if isinstance(node, ast.AsyncFunctionDef):
                continue
            hits = _loop_only_hits(node)
            if hits:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} def {node.name}() "
                                 f"→ create_task@行 {hits}")
    assert not offenders, (
        "同步路由（def）里不能 asyncio.create_task（线程池无事件循环 → 500）：\n  "
        + "\n  ".join(offenders)
    )


def test_notify_and_onboard_routes_are_async():
    """两条具体回归点：端点必须保持 async def（它们要 create_task 起后台协程）。"""
    import inspect

    from src.domains.runtime.routes.tasks import notify_task
    from src.domains.mgmt.routes.feishu_onboard import start_onboard

    assert inspect.iscoroutinefunction(notify_task), "tasks.notify_task 必须是 async def"
    assert inspect.iscoroutinefunction(start_onboard), "feishu_onboard.start_onboard 必须是 async def"