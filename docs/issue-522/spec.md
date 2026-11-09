# Spec: Issue #522 — 无 TTY Worker 取消 hwcloud ACP 任务后进程残留修复

> 目标合入分支: weekly-08-mys（Spec 原文标注 weekly-09 已更正为当前流水线权威基线）

## Objective

修复 `src/runtime/agent.py` `_kill_stale_engine` 中
`os.environ.get("USER", os.getlogin() if hasattr(os, "getlogin") else "")`
默认参数提前求值问题：无控制终端的 Linux Worker 上 `os.getlogin()` 抛
`OSError errno 6 (ENXIO)`，导致 pgrep 终止分支被跳过、任务进程树残留。

## Scope

- In: `_kill_stale_engine` Linux 分支用户名解析（USER → pwd.getpwuid → UID 逐级降级，不依赖 TTY）；
  新增 `_current_username()` 辅助；setsid 无 TTY 取消回归测试（USER 已设置/未设置两场景）。
- Out: Windows wmic 分支、控制器迟到 run 请求拒绝逻辑、文件系统隔离适配器改造。

## Acceptance Criteria

- 代码中不再存在 `os.environ.get("USER", <可抛异常表达式>)` 提前求值模式。
- setsid 无 TTY + USER 已设置/未设置：`_kill_stale_engine` 正常执行 pgrep，无 ENXIO，模拟进程树被终止。
- 所有现有测试通过。
