"""grape CLI 版本线单一事实源（Issue #1934 部署门 + Issue #2695 功能门）。

为什么单独一层：worker 侧（`src/runtime/cli_version_guard.py`）、打包侧
（`src/worker_package.py`）与安装脚本（`scripts/install-worker.sh` / `install-worker.ps1` §4.5）
都要判 grape CLI 版本。worker 侧不能 import `worker_package`（服务端专用），服务端也不该
把整条运行时依赖拖进打包流程，因此把「门禁常量 + 版本比较」收敛到这个只用标准库的模块，
双方各自 import 同一对象；脚本无法 import Python，其字面量由
`tests/test_issue_2695_worker_cli_version_guard.py` 双向对齐校验。

两道门语义不同，不要混用：

- `GRAPE_CLI_MIN_VERSION`：**部署门**。契约驱动命令树（`tasks complete` / `a2a handoff` 等）
  依赖该版本线，低于即视为部署漂移（陈旧轮子/人工落轮/历史包），打包/安装/启动三处共用（#1934）。
- `GRAPE_CLI_ISSUE_ID_INTERCEPT_VERSION`：**功能门**。含 grape-cli #1634（非数字 `--issue_id`
  本地拦截）的首个**已发布**版本线，即 `(0, 3, 4)`（2026-11-14 发版，PR !16）。未发布时才置
  `None`——「未知 ≠ 陈旧」，不判定、不告警。
"""
import os
import re

# 部署门（#1934）：打包（worker_package）、安装（install-worker.sh/ps1）、启动核对共用。
GRAPE_CLI_MIN_VERSION = (0, 3, 3)

# 功能门（#2695 / grape-cli #1634）：含该修复的首个已发布版本线。
# 核对依据（2026-11-16）：grape-cli 已发布 v0.3.4（tag=`98b5288` 即 main HEAD；CHANGELOG [0.3.4]
# 2026-11-14 明载 #1634 本地拦截随 PR !16 合入）→ 功能门取 (0,3,4)。
# 部署机也可用同名环境变量 `GRAPE_CLI_ISSUE_ID_INTERCEPT_VERSION` 覆盖本值（见 intercept_floor）。
GRAPE_CLI_ISSUE_ID_INTERCEPT_VERSION = (0, 3, 4)

_VERSION_RE = re.compile(r"\d+(?:\.\d+)*")


def parse_version(text) -> tuple:
    """从版本串/探活输出（`grape 0.3.3`）取点分数字元组；无法解析返回 ()。"""
    m = _VERSION_RE.search(str(text or ""))
    if not m:
        return ()
    return tuple(int(p) for p in m.group(0).split("."))


def version_below(version, floor) -> bool:
    """点分数字比较：version < floor。任一侧无法解析 → False（未知不判定，避免误报）。

    按段数值比较（0.3.10 > 0.3.9），段数不足补 0。
    """
    cur = parse_version(version) if isinstance(version, str) else tuple(version or ())
    base = parse_version(floor) if isinstance(floor, str) else tuple(floor or ())
    if not cur or not base:
        return False
    width = max(len(cur), len(base))
    cur = cur + (0,) * (width - len(cur))
    base = base + (0,) * (width - len(base))
    return cur < base


def format_version(version) -> str:
    """`(0,3,4)` → `0.3.4`（提示文案/日志用）。"""
    return ".".join(map(str, tuple(version or ())))


def intercept_floor():
    """生效的 #1634 功能门：环境变量 > 模块常量。

    - 环境变量 `GRAPE_CLI_ISSUE_ID_INTERCEPT_VERSION`：部署机可在不改代码的情况下覆盖门禁；
    - 模块常量 `None`（仅当修复尚未发版）→ 返回 None，调用方据此跳过判定（不告警）。
    """
    raw = (os.environ.get("GRAPE_CLI_ISSUE_ID_INTERCEPT_VERSION") or "").strip()
    if raw:
        parsed = parse_version(raw)
        return parsed or None
    return GRAPE_CLI_ISSUE_ID_INTERCEPT_VERSION
