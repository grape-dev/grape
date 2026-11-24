"""顶层同名定义门禁（Issue #4158）：同一 Python 模块顶层重复定义同名函数/类。

事故背景：`issues_center.py` 里整段复制粘贴出一份「飞书专用」通知实现，排在 A4 全渠道
扇出版之后；Python 后定义覆盖前定义，pending_review / awaiting_input / 任务分配通知
静默退回旧链路并以 `reason=no_ctx` 丢弃。静态门禁此前对此零可见性。

规则：单个模块顶层（module body）同名 FunctionDef/AsyncFunctionDef/ClassDef 计数 > 1 即 FAIL。
基线固定为 0——重名没有合法场景（`@overload` 桩本仓未使用；条件定义属于设计缺陷）。

判定与取舍依据见 `docs/issue-4158/plan.md`（逐符号「择一保留」表 + no_ctx 指标口径）；
import 别名/局部重定义的姊妹门禁是 `scripts/check_f811.py`。

用法：
    python3 scripts/check_duplicate_defs.py          # 校验（CI/make lint）
    python3 scripts/check_duplicate_defs.py src      # 指定扫描根（默认 src）
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = 0


def _scan(root: Path) -> tuple[list[str], list[str]]:
    """返回 (重名明细, 解析失败明细)。"""
    dup_hits: list[str] = []
    parse_errors: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except Exception as exc:  # noqa: BLE001 - 语法错误必须显式暴露，不能静默跳过
            parse_errors.append(f"{rel}: {type(exc).__name__}: {exc}")
            continue
        seen: dict[str, list[int]] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                seen.setdefault(node.name, []).append(node.lineno)
        for name, lines in seen.items():
            if len(lines) > 1:
                dup_hits.append(f"{rel}: def/class `{name}` 顶层重复定义于行 {lines}")
    return dup_hits, parse_errors


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "src"
    root = (ROOT / target) if not Path(target).is_absolute() else Path(target)
    if not root.is_dir():
        # 缺目标（工具/目录缺位）显式 FAIL，不伪装成通过
        print(f"✗ duplicate-defs gate: 扫描根不存在 {root}（显式失败，不静默跳过）")
        return 1

    dup_hits, parse_errors = _scan(root)
    if parse_errors:
        print(f"✗ duplicate-defs gate: {len(parse_errors)} 个文件解析失败（显式失败）：")
        for err in parse_errors[:20]:
            print("  " + err)
        return 1

    current = len(dup_hits)
    print(f"duplicate-defs gate: 顶层重名 {current} / 基线 {BASELINE}（{root}）")
    if current > BASELINE:
        print(f"\n✗ 顶层同名定义 {current} 处（基线 {BASELINE}，重名无合法场景）：")
        for hit in dup_hits:
            print("  " + hit)
        print("\n修复：每个符号只保留唯一实现（择一删另一份），禁止整段复制粘贴。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())