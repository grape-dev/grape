"""D109 启动装配守卫：两个入口都必须跑导入，且 lifespan 装饰器完整。

反证：删掉 grape_server.lifespan 里的调用，或让 `@asynccontextmanager` 被别的函数
劫持（装配函数插在装饰器与 `async def lifespan` 之间），本测试必红。
两种形态都真实出现过：装配只加在 main.py（A 进程不执行）、装饰器绑到插入的
同步函数上（lifespan 失去包装、装配本身也从未执行）。
"""
from __future__ import annotations

import ast
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[2] / "src" / "api"
ENTRY = "d109_platform_object_startup"


def _lifespan(module_file: str) -> ast.AsyncFunctionDef:
    tree = ast.parse((API_DIR / module_file).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            return node
    raise AssertionError(f"{module_file}: 找不到 async def lifespan")


def _calls(node: ast.AsyncFunctionDef) -> set[str]:
    """收集函数体里被调用的裸函数名（含 try/except 内），按精确名判，不做子串匹配。"""
    return {n.func.id for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def _decorators(node: ast.AsyncFunctionDef) -> str:
    return " ".join(ast.unparse(d) for d in node.decorator_list)


def test_grape_server_lifespan_runs_d109_startup():
    assert ENTRY in _calls(_lifespan("grape_server.py"))


def test_main_lifespan_runs_d109_startup():
    assert ENTRY in _calls(_lifespan("main.py"))


def test_main_lifespan_keeps_asynccontextmanager_decorator():
    decos = _decorators(_lifespan("main.py"))
    assert "asynccontextmanager" in decos, (
        f"main.lifespan 丢了装饰器（实际装饰器={decos!r}）—— "
        "启动装配函数插在装饰器与 def 之间会把装饰器抢走"
    )


def test_grape_server_lifespan_keeps_asynccontextmanager_decorator():
    decos = _decorators(_lifespan("grape_server.py"))
    assert "asynccontextmanager" in decos, f"grape_server.lifespan 装饰器异常：{decos!r}"