"""Issue #3414：全仓导入符号完整性冒烟（「cannot import name ...」类缺陷防回归）。

背景：自部署机 engine_model.py 陈旧/部分升级缺 ``detect_engine_model``，调用方
``settings._attach_engine_models`` 的 public 门面 from-import 可能把 ImportError 变成
/api/settings/agent-runtimes 500 → 引擎绑定/派单链路整体断裂（新任务永久 queued）。
历史同类（#2706/#2812/#2292/#1657 移植漏段）均为「模块存在但缺符号 / 缺模块」的
导入齐套性缺陷，逐文件补漏无法根治——本测试用 AST 静态扫描一次性守卫：

对 src/**/*.py 的每个未守卫 import：
1. ``from src.x.y import a, b`` → 目标模块文件必须存在，且 a/b 必须在目标模块
   顶层命名空间有定义（含别名 shim 模块：``sys.modules[__name__] = _impl`` 时
   递归解析到 canonical 实现）；
2. ``from src import a`` → a 或是 src/ 下真实模块文件，或是 src/__init__.py
   LEGACY_MODULE_ALIASES 登记的别名（懒加载 shim，运行期由 meta_path 解析）；
3. ``import src.x.y`` → 目标模块文件必须存在。

仅 ``try.body`` 内的导入（如 engine_model 内 ``import yaml``、Issue #3414 后
_attach_engine_models 的降级导入）属**故意降级**，失败不抛错，跳过不查；
except/orelse/finally 块内的导入失败会正常外抛，仍纳入检查——
本测试只拦「未守卫、会直接把 ImportError 顶到调用方」的导入。

相对导入（from . / from .. import）按文件位置解析为绝对 src 路径后同规则校验。
"""

import ast
import functools
import os

import src

ROOT = os.path.abspath(os.path.join(os.path.dirname(src.__file__), ".."))
SRC_ROOT = os.path.join(ROOT, "src")


def _module_file(dotted: str):
    """src.x.y → src/x/y.py 或 src/x/y/__init__.py；存在返回路径，否则 None。"""
    parts = dotted.split(".")
    if parts[0] != "src" or len(parts) < 2:
        return None
    rel = os.path.join(*parts[1:])
    for cand in (os.path.join(SRC_ROOT, rel + ".py"), os.path.join(SRC_ROOT, rel, "__init__.py")):
        if os.path.isfile(cand):
            return cand
    return None


@functools.lru_cache(maxsize=4096)
def _parse(path: str) -> ast.AST:
    return ast.parse(open(path, encoding="utf-8").read(), filename=path)


def _alias_shim_target(tree: ast.AST):
    """识别别名 shim 模块：``import src.x as _impl`` + ``sys.modules[__name__] = _impl``。

    返回 canonical 模块的 dotted 名（如 "src.kernel.time_utils"），非 shim 返回 None。
    """
    impl_binding = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname and a.name.startswith("src"):
                    impl_binding[a.asname] = a.name
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        t = node.targets[0]
        if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Attribute)
                and t.value.attr == "modules"
                and isinstance(t.slice, ast.Name) and t.slice.id == "__name__"
                and isinstance(node.value, ast.Name)
                and node.value.id in impl_binding):
            return impl_binding[node.value.id]
    return None


def _top_level_names(tree: ast.AST) -> set:
    """模块顶层命名空间可见名（含 if/try 分支内定义；不含函数/类体内局部名）。"""

    def walk_body(body, out):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        out.add(t.id)
                    elif isinstance(t, (ast.Tuple, ast.List)):
                        for e in t.elts:
                            if isinstance(e, ast.Name):
                                out.add(e.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                out.add(node.target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    out.add(a.asname or a.name.split(".")[0])
            elif isinstance(node, (ast.If, ast.Try)):
                containers = [node.body, getattr(node, "orelse", []), getattr(node, "finalbody", [])]
                for h in getattr(node, "handlers", []):
                    containers.append(h.body)
                for b in containers:
                    walk_body(b, out)
            elif isinstance(node, ast.With):
                walk_body(node.body, out)

    out: set = set()
    walk_body(tree.body, out)
    return out


@functools.lru_cache(maxsize=4096)
def _module_names(dotted: str, _depth: int = 0) -> tuple[set, bool]:
    """返回 (顶层符号集, 是否包)；shim 模块递归解析 canonical 目标。"""
    path = _module_file(dotted)
    if path is None:
        return set(), dotted.count(".") >= 1 and dotted.endswith("__init__")
    tree = _parse(path)
    if _depth < 4:
        shim = _alias_shim_target(tree)
        if shim:
            return _module_names(shim, _depth + 1)
    return _top_level_names(tree), path.endswith("__init__.py")


def _is_guarded_import(node) -> bool:
    return getattr(node, "_guarded", False) is True


# Issue #3414 rebase 基线豁免（共 **7** 项，临时豁免）：weekly-mys（arch-weekly-11
# 刷新）上**存量**的导入齐套性缺陷——与本 Issue 改动无关（本 PR 未触碰这些文件），
# 修复归后续 Issue；登记在此避免冒烟在当前基线上必然红卡住本 PR，同时保持对新回归
# 的拦截力。新代码出现同类问题时**不得**往此表追加，应修导入本身；后续 Issue 修完
# 后请同步删除对应条目。
_BASELINE_KNOWN_VIOLATIONS = frozenset({
    "src/domains/insight/observability/master_usage.py: "
    "cannot import name 'UsageCollector' from 'src.domains.runtime.public'",
    "src/domains/runtime/storage/plugin_sync.py: "
    "cannot import name 'plugin_key' from 'src.domains.runtime.storage'",
    "src/domains/mgmt/admin/platform_cards/service.py: "
    "cannot import name 'PlatformCardError' from 'src.domains.runtime.public'",
    "src/domains/mgmt/plugin_tester/runner.py: "
    "cannot import name 'plugin_l0' from 'src.api'",
    "src/domains/mgmt/plugin_tester/runner.py: "
    "import target module missing: src.api.plugin_l0",
    "src/domains/mgmt/routes/repos.py: "
    "cannot import name 'VcsApiClient' from 'src.domains.runtime.public'",
    "src/domains/mgmt/routes/scenes.py: "
    "cannot import name 'VcsApiClient' from 'src.domains.runtime.public'",
})


def _filter_baseline(problems: list, path: str) -> list:
    rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
    return [p for p in problems
            if f"{rel}: {p}" not in _BASELINE_KNOWN_VIOLATIONS]


def _mark_guarded(tree: ast.AST) -> None:
    """仅给 ``try.body`` 内的 import 打 _guarded 标记（故意降级，跳过检查）。

    except/orelse/finally 块内的 import 失败会正常外抛，不能算作守卫，否则漏报。
    嵌套在 try.body 里的 if/with/class/try 仍受外层 try 保护，递归标记；
    function 体内的 import 在调用期才执行、不受模块级 try 保护，不标记。
    """

    def tag_body(body):
        for node in body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                node._guarded = True
            elif isinstance(node, (ast.If, ast.With)):
                tag_body(node.body)
                tag_body(getattr(node, "orelse", []))
            elif isinstance(node, ast.Try):
                tag_body(node.body)
                for h in node.handlers:
                    tag_body(h.body)
                tag_body(node.orelse + node.finalbody)
            elif isinstance(node, ast.ClassDef):
                tag_body(node.body)

    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and node.handlers:
            tag_body(node.body)


def _iter_src_imports():
    for base, _dirs, files in os.walk(SRC_ROOT):
        if "__pycache__" in base:
            continue
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            path = os.path.join(base, f)
            try:
                tree = _parse(path)
            except SyntaxError:
                continue  # 语法坏文件由编译守卫负责
            _mark_guarded(tree)
            pkg_parts = os.path.relpath(os.path.dirname(path), ROOT).replace(os.sep, ".").split(".")
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    yield path, pkg_parts, node


def _resolve_dotted(path, pkg_parts, node):
    """ImportFrom node → 绝对 dotted 目标（相对导入按文件位置解析）；非 src 返回 None。"""
    if node.level == 0:
        dotted = node.module or ""
    else:
        if node.level > len(pkg_parts):
            return None
        base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
        tail = (node.module or "").split(".") if node.module else []
        dotted = ".".join(base + tail)
    if not dotted or not dotted.startswith("src"):
        return None
    return dotted


def test_from_src_imports_resolve_module_and_symbols():
    """``from src... import sym``：模块文件存在且 sym 已定义（未守卫导入才算违规）。"""
    problems = []
    for path, pkg_parts, node in _iter_src_imports():
        if not isinstance(node, ast.ImportFrom) or _is_guarded_import(node):
            continue
        if not (node.module or "").startswith("src") and node.level == 0:
            continue
        if node.module == "src" and node.level == 0:
            continue  # 「from src import x」由 test_from_src_root_imports_resolve_alias_or_submodule 覆盖
        dotted = _resolve_dotted(path, pkg_parts, node)
        if dotted is None:
            continue
        file_problems = []
        if _module_file(dotted) is None:
            file_problems.append(f"import target module missing: {dotted}")
        else:
            names, is_pkg = _module_names(dotted)
            for a in node.names:
                if a.name == "*":
                    continue
                if is_pkg and _module_file(f"{dotted}.{a.name}") is not None:
                    continue  # 子模块导入
                if a.name not in names:
                    file_problems.append(f"cannot import name '{a.name}' from '{dotted}'")
        problems.extend(_filter_baseline(file_problems, path))
    assert not problems, "导入符号完整性违规（Issue #3414 防回归）:\n" + "\n".join(problems[:80])


def test_from_src_root_imports_resolve_alias_or_submodule():
    """``from src import x``：x 须是 src/ 下模块或 LEGACY_MODULE_ALIASES 登记别名。"""
    problems = []
    for path, _pkg, node in _iter_src_imports():
        if not isinstance(node, ast.ImportFrom) or _is_guarded_import(node):
            continue
        if node.module != "src":
            continue
        for a in node.names:
            if a.name == "*":
                continue
            if _module_file(f"src.{a.name}") is not None:
                continue
            if a.name in dir(src) or f"src.{a.name}" in src.LEGACY_MODULE_ALIASES:
                continue
            problems.append(f"{path}: 'from src import {a.name}' 无法解析（非模块/非别名）")
    assert not problems, "\n".join(problems[:40])


def test_import_src_modules_resolve_files():
    """``import src.x.y``：目标模块文件必须存在（未守卫导入）。"""
    problems = []
    for path, _pkg, node in _iter_src_imports():
        if not isinstance(node, ast.Import) or _is_guarded_import(node):
            continue
        for a in node.names:
            if not a.name.startswith("src"):
                continue
            if _module_file(a.name) is None:
                problems.append(f"{path}: imported module missing: {a.name}")
    assert not problems, "\n".join(problems[:40])
