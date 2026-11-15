#!/usr/bin/env python3
"""异步阻塞静态检查（棘轮）。

背景：这套代码里同一个故障反复出现四次 —— 同步 DB / git / 网络调用跑在 asyncio 事件循环里，
把整个进程卡死，表现却不是「变慢」而是**远端引擎失联**（worker 不响应 → 心跳落不了库 →
引擎被判 offline）。已修过的位置：event sweeper、task sweeper、local claimer、
autopilot scheduler、grape_server 模块级迁移。

每次都是等故障出现、抓 py-spy 才定位。这个脚本让它在**提交前**就暴露。

检查项（高精度，只报确定会造成阻塞/丢弃的形态）：
  A1  async def 内直接 time.sleep(...)
  A2  async def 内直接 subprocess.run / check_output / check_call / Popen(...)
  A3  async def 内直接 requests.get/post/put/delete(...)
  A4  async def 内直接 urllib.request.urlopen(...)
  A5  非 async 上下文调用 X.get_event_loop() 后 .create_task(...) —— 无运行中 loop 时
      task 会被静默丢弃（事件写库却无人派发，2026-11-15 seq=9755 实测）

判定「在 async def 内」时只看该函数体自身，不跨嵌套 def/async def/lambda（那些各自判定）。

棘轮：现有违规数记在 configs/async_blocking_baseline.json。新增即失败；
清零或下降需要 --update-baseline 收紧（参考 foreign_table_refs.py 的做法）。

用法：
  python3 scripts/check_async_blocking.py                 # 检查（棘轮）
  python3 scripts/check_async_blocking.py --update-baseline
  python3 scripts/check_async_blocking.py --list          # 打印全部命中明细
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
BASELINE = REPO / "configs" / "async_blocking_baseline.json"

SYNC_SLEEP = {"sleep"}
SUBPROCESS_FUNCS = {"run", "check_output", "check_call", "call", "Popen"}
HTTP_MODULES = {"requests"}
URLOPEN_FUNCS = {"urlopen"}


def _call_name(node: ast.Call) -> tuple[str, str]:
    """(dotted owner, func name)。owner 取到倒数第二段，兼容 a.b.c()。"""
    f = node.func
    if isinstance(f, ast.Attribute):
        name = f.attr
        owner_parts: list[str] = []
        cur = f.value
        while isinstance(cur, ast.Attribute):
            owner_parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            owner_parts.append(cur.id)
        return ".".join(reversed(owner_parts)), name
    if isinstance(f, ast.Name):
        return "", f.id
    return "", ""


def _walk_body(node: ast.AST):
    """遍历函数体，跳过嵌套的函数/类定义（它们各自单独判定）。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield child
        yield from _walk_body(child)


def _scan_function(fn: ast.AST, rel: str, out: list[dict], *, in_async: bool) -> None:
    for node in _walk_body(fn):
        if not isinstance(node, ast.Call):
            continue
        owner, name = _call_name(node)
        kind = None
        if in_async and name in SYNC_SLEEP and owner in ("time", ""):
            kind = "A1 time.sleep in async"
        elif in_async and name in SUBPROCESS_FUNCS and ("subprocess" in owner or owner == ""):
            kind = "A2 subprocess in async"
        elif in_async and owner.split(".")[0] in HTTP_MODULES:
            kind = "A3 requests in async"
        elif in_async and name in URLOPEN_FUNCS:
            kind = "A4 urlopen in async"
        if kind:
            out.append({"file": rel, "line": node.lineno, "kind": kind,
                        "detail": f"{owner}.{name}" if owner else name})


def _scan_dropped_task(tree: ast.AST, rel: str, out: list[dict]) -> None:
    """A5：非 async 上下文里 get_event_loop().create_task(...)。

    等价形态：loop = asyncio.get_event_loop(); loop.create_task(...)
    只报「赋值来自 get_event_loop」或「直接链式调用」两种，避免误报。
    """
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef):
            continue  # async 内 get_event_loop == running loop，安全
        if not isinstance(fn, (ast.FunctionDef, ast.Module)):
            continue
        loops: set[str] = set()
        for node in _walk_body(fn):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                owner, name = _call_name(node.value)
                if name == "get_event_loop":
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            loops.add(t.id)
            if isinstance(node, ast.Call):
                owner, name = _call_name(node)
                if name == "create_task":
                    base = owner.split(".")[0] if owner else ""
                    is_chain = owner.endswith("get_event_loop")
                    if base in loops or is_chain:
                        out.append({"file": rel, "line": node.lineno,
                                    "kind": "A5 create_task on non-running loop",
                                    "detail": f"{owner}.create_task"})


def collect() -> list[dict]:
    out: list[dict] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if "__pycache__" in rel:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                _scan_function(node, rel, out, in_async=True)
            elif isinstance(node, ast.FunctionDef):
                _scan_function(node, rel, out, in_async=False)
        _scan_dropped_task(tree, rel, out)
    return out


def load_baseline() -> dict:
    if not BASELINE.exists():
        return {"total": 0, "by_kind": {}}
    try:
        return json.loads(BASELINE.read_text(encoding="utf-8"))
    except Exception:
        return {"total": 0, "by_kind": {}}


def write_baseline(hits: list[dict]) -> None:
    by_kind: dict[str, int] = {}
    for h in hits:
        by_kind[h["kind"]] = by_kind.get(h["kind"], 0) + 1
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps(
        {"total": len(hits), "by_kind": dict(sorted(by_kind.items()))},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--list", action="store_true", dest="show_all")
    args = ap.parse_args()

    hits = collect()
    if args.update_baseline:
        write_baseline(hits)
        print(f"baseline updated: {len(hits)} 处（{BASELINE.relative_to(REPO)}）")

    if args.show_all or args.update_baseline:
        for h in hits:
            print(f"  {h['file']}:{h['line']}  [{h['kind']}] {h['detail']}")

    base = load_baseline()
    base_total = int(base.get("total") or 0)
    print(f"async-blocking: 当前 {len(hits)} / 基线 {base_total}")
    if len(hits) > base_total:
        print(f"FAIL: 新增 {len(hits) - base_total} 处事件循环阻塞/丢弃形态"
              f"（同步调用必须包 asyncio.to_thread；派发必须用 get_running_loop）",
              file=sys.stderr)
        new = [h for h in hits if args.show_all] if args.show_all else hits
        for h in new[:20]:
            print(f"  ✗ {h['file']}:{h['line']} [{h['kind']}] {h['detail']}", file=sys.stderr)
        return 1
    if len(hits) < base_total:
        print(f"  ↓ 比基线少 {base_total - len(hits)} 处，可 --update-baseline 收紧", file=sys.stderr)
    print("OK: 未新增事件循环阻塞形态")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())