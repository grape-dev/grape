"""未定义名棘轮（QA G6）：门禁此前对 undefined name 零可见性。

背景：一次机械重构把 `db.cursor()` 批量替换成 `_rt_db.cursor()`，拼出 `__rt_db`、`_sm__mg_db`
这类从未赋值的名字共 44 处；每处都在 except 里被吞成静默失败（事件派发失能、OAuth 回退到旧路径）。
静态检查当时不在门禁里，所以问题一路带到 PR 复审。

本脚本把 undefined name 计数钉成基线：不高于基线即通过，超出即失败（棘轮只往下走）。
优先用 ruff（快），其次 pyflakes；两者都不可用时**跳过并明确打印**（不让工具缺失伪装成全绿）。

用法：
    python3 scripts/check_undefined_names.py             # 校验（CI/门禁）
    python3 scripts/check_undefined_names.py --update    # 收紧基线到当前值
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "configs" / "undefined_names_baseline.json"
TARGETS = ["src"]


def _run(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
        return out.stdout + out.stderr
    except Exception:
        return None


def _count() -> tuple[int, str, list[str]]:
    """返回 (未定义名处数, 使用的工具名, 命中明细)。"""
    if shutil.which("ruff"):
        raw = _run(["ruff", "check", "--select", "F821", "--no-cache", *TARGETS])
        if raw is not None:
            # ruff 现代输出：`F821 Undefined name `x``；旧输出：`F821 undefined name 'x'`
            hits = [ln for ln in raw.splitlines() if "F821" in ln]
            return len(hits), "ruff", hits
    py = shutil.which("pyflakes") or (str(Path(sys.executable).parent / "pyflakes")
                                     if (Path(sys.executable).parent / "pyflakes").exists() else "")
    if py:
        raw = _run([py, *TARGETS])
        if raw is not None:
            hits = [ln for ln in raw.splitlines() if re.search(r"undefined name", ln)]
            return len(hits), "pyflakes", hits
    return -1, "", []


def main() -> int:
    current, tool, hits = _count()
    if current < 0:
        print("undefined-name ratchet: 跳过（ruff / pyflakes 均不可用）")
        return 0

    baseline = 0
    if BASELINE.exists():
        try:
            baseline = int(json.loads(BASELINE.read_text())["undefined_names"])
        except Exception:
            baseline = 0

    if "--update" in sys.argv:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps({"undefined_names": current,
                                        "tool": tool,
                                        "note": "棘轮基线：只允许下降；收紧请重跑 --update"},
                                       indent=2, ensure_ascii=False) + "\n")
        print(f"undefined-name ratchet: 基线已更新为 {current}（{tool}）")
        return 0

    print(f"undefined-name ratchet: 当前 {current} / 基线 {baseline}（{tool}）")
    if current > baseline:
        print(f"\n✗ 新增 {current - baseline} 处未定义名（棘轮只允许下降）：")
        for ln in hits[:20]:
            print("  " + ln)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())