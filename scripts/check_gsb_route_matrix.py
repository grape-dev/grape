#!/usr/bin/env python3
"""Issue #2479 禁止矩阵 CI 静态扫描：runtime/insight 代码不得引用 mgmt remote 通道.

**为什么需要本门禁**

禁止矩阵（运行时守卫见 src/kernel/gsb/provider.py::matrix_forces_lib）：
``runtime|insight → mgmt`` 一律 lib——这两个域进程调用 mgmt 的任何接口
（7 个契约方法 + mgmt public 全部原语）都必须走本地实现，任何 remote 通道都非法；
``mgmt → runtime|insight`` 方向允许 remote（按配置表）。

运行时守卫已保证 ``get_service("mgmt")`` 在 runtime/insight 进程内恒 Local，
但绕过 provider 工厂的写法（直接 new MgmtServiceRemote / GsbClient("mgmt") /
直接摸 _PROVIDERS）运行时守卫看不见——本脚本用 AST 静态比对把这类绕过在 CI 拦下
（验收矩阵：「CI 静态扫描可红，含 public 原语清单逐个标注归属」）。

**判定口径（AST，天然跳过 docstring/注释）**

| 形态 | 判定 |
|---|---|
| import / 引用 ``MgmtServiceRemote`` | FAIL（mgmt remote 契约实现） |
| ``GsbClient("mgmt", ...)`` / ``ServiceHttpClient("mgmt", ...)`` | FAIL（直连 mgmt remote 传输） |
| ``from … import GsbClient as GC`` 后 ``GC("mgmt", ...)`` | FAIL（客户端类别名导入后直连） |
| 引用 ``_PROVIDERS``（绕过工厂的注册表裸访问） | FAIL |

``get_service("mgmt")`` / ``get_local_service("mgmt")`` 合法（矩阵守卫内）。

**原语归属标注**

--annotate 时输出 public 原语清单逐个归属（来源
``src/kernel/gsb/route_config.py::PUBLIC_PRIMITIVES`` 静态镜像）：
mgmt 原语对 runtime|insight 调用方 lib-only；runtime 原语本期无 remote 通道、
基线 lib（mgmt 方向 remote 化待后续传输通道）。

**用法**

    python3 scripts/check_gsb_route_matrix.py                # 扫同域代码树与 apps 入口
    python3 scripts/check_gsb_route_matrix.py --annotate    # 附原语归属标注
    python3 scripts/check_gsb_route_matrix.py --json         # 机器可读（CI 归档）

退出码：0 = 通过；1 = 命中违规（CI 可红）；2 = fail-closed（没扫到任何文件）。
扫描根缺失按违规处理（fail-closed），避免配置漂移/路径写错时静默跳过。
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 禁止矩阵的调用方域代码树与同域进程入口（docs/lib-3db-3host-design.md §3.1）；
#: 三个 apps 入口自身可能直接组装客户端，必须与 src 域代码一起扫描。
SCAN_ROOTS = (
    "src/domains/runtime",
    "src/domains/insight",
    "apps/runtime/worker/main.py",
    "apps/runtime/webhook/main.py",
    "apps/insight/main.py",
)

#: mgmt remote 通道符号（import/引用/实例化都算）
_BANNED_SYMBOLS = {"MgmtServiceRemote"}

#: 直连 mgmt remote 传输的客户端类（首参字面量 "mgmt" 时违规）
_REMOTE_CLIENT_NAMES = {"GsbClient", "ServiceHttpClient"}

#: provider 注册表裸访问（绕过工厂即绕过矩阵守卫）
_BANNED_REGISTRY_NAME = "_PROVIDERS"

#: 扫描的文件后缀
_SUFFIXES = {".py"}


@dataclass
class Violation:
    root: str
    path: Path
    line: int
    kind: str
    detail: str

    def render(self) -> str:
        try:
            rel = self.path.relative_to(REPO_ROOT)
        except ValueError:
            rel = self.path
        return f"[{self.root}] {rel}:{self.line} {self.kind}: {self.detail}"


def _call_target_name(node: ast.Call) -> str:
    """Call 节点的目标短名（GsbClient / x.GsbClient → GsbClient）。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _collect_client_aliases(tree: ast.AST) -> dict[str, str]:
    """收集客户端类的本地别名（``from … import GsbClient as GC`` → ``GC → GsbClient``）。"""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _REMOTE_CLIENT_NAMES and alias.asname:
                    aliases[alias.asname] = alias.name
    return aliases


def scan_tree(tree: ast.AST, path: Path, root: str) -> list[Violation]:
    """AST 扫描单个文件（docstring/注释天然不参与）。"""
    found: list[Violation] = []
    client_aliases = _collect_client_aliases(tree)
    for node in ast.walk(tree):
        # import MgmtServiceRemote / from ... import MgmtServiceRemote
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] in _BANNED_SYMBOLS:
                    found.append(Violation(root, path, node.lineno,
                                           "banned-import", f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _BANNED_SYMBOLS:
                    found.append(Violation(root, path, node.lineno,
                                           "banned-import",
                                           f"from {node.module or '.'} import {alias.name}"))
        elif isinstance(node, ast.Name):
            if node.id in _BANNED_SYMBOLS:
                found.append(Violation(root, path, node.lineno,
                                       "banned-symbol", f"reference {node.id}"))
            elif node.id == _BANNED_REGISTRY_NAME:
                found.append(Violation(root, path, node.lineno,
                                       "registry-bypass", f"access {node.id}"))
        elif isinstance(node, ast.Attribute):
            if node.attr in _BANNED_SYMBOLS:
                found.append(Violation(root, path, node.lineno,
                                       "banned-symbol", f"reference {node.attr}"))
        elif isinstance(node, ast.Call):
            target = _call_target_name(node)
            target = client_aliases.get(target, target)
            if target in _REMOTE_CLIENT_NAMES and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str) \
                        and first.value.strip().lower() == "mgmt":
                    found.append(Violation(root, path, node.lineno,
                                           "mgmt-remote-client",
                                           f'{target}("mgmt", ...)'))
    return found


def scan_roots(roots: list[Path]) -> tuple[list[Violation], int]:
    """扫描全部根目录，返回（违规列表, 扫描文件数）。"""
    violations: list[Violation] = []
    files = 0
    for root in roots:
        if not root.exists():
            violations.append(Violation(str(root), root, 0,
                                        "missing-root", f"scan root not found: {root}"))
            continue
        if root.is_file():
            candidates = [root]
        else:
            candidates = sorted(
                p for p in root.rglob("*") if p.suffix in _SUFFIXES and p.is_file()
            )
        for path in candidates:
            files += 1
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as exc:  # 解析失败按违规处理（fail-loud）
                violations.append(Violation(str(root), path, exc.lineno or 0,
                                            "syntax-error", str(exc)))
                continue
            violations.extend(scan_tree(tree, path, str(root)))
    return violations, files


def annotate_primitives() -> list[dict]:
    """public 原语清单逐个归属标注（路由 key 空间，含矩阵口径）。"""
    sys.path.insert(0, str(REPO_ROOT))
    from src.kernel.gsb.route_config import PUBLIC_PRIMITIVES

    rows: list[dict] = []
    for domain, prims in PUBLIC_PRIMITIVES.items():
        for name in prims:
            if domain == "mgmt":
                policy = ("lib-only for runtime|insight callers (forbidden matrix); "
                          "no remote channel")
            else:
                policy = "baseline lib; no remote channel this phase"
            rows.append({"route_key": f"{domain}.{name}", "policy": policy})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GSB forbidden-matrix static gate")
    parser.add_argument("--annotate", action="store_true",
                        help="输出 public 原语清单逐个归属标注")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="机器可读输出（CI 归档）")
    args = parser.parse_args(argv)

    roots = [REPO_ROOT / r for r in SCAN_ROOTS]
    violations, files = scan_roots(roots)

    primitives = annotate_primitives() if args.annotate or args.as_json else []

    if args.as_json:
        print(json.dumps({
            "files_scanned": files,
            "violations": [
                {"path": str(v.path.relative_to(REPO_ROOT)), "line": v.line,
                 "kind": v.kind, "detail": v.detail, "root": v.root}
                for v in violations
            ],
            "primitives": primitives,
        }, ensure_ascii=False, indent=2))
    else:
        if args.annotate:
            print(f"== public 原语归属标注（{len(primitives)} 条，禁止矩阵口径） ==")
            for row in primitives:
                print(f"  {row['route_key']:<40} {row['policy']}")
            print()
        for v in violations:
            print(f"FAIL {v.render()}")
        print(f"scanned {files} files under {[str(r) for r in roots]}: "
              f"{'PASS' if not violations else 'FAIL'}")

    if files == 0:
        print("[FATAL] fail-closed: no source file scanned", file=sys.stderr)
        return 2
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
