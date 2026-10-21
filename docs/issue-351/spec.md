# Spec: Issue #351 - Windows 上提示词会被 cmd 壳截断成第一行的问题

> 来源：Issue Agent Spec（Developability: ready, 2026-10-16）| 本文档为开发基线落盘

## Objective
- 修复 Windows 上多行提示词经 cmd/bat 壳执行时被截断为第一行的缺陷，保障提示词完整传入 CLI 引擎。
- 成功标准：Windows + npm 全局安装环境下，多行提示词完整到达引擎，探活与真实任务行为一致。

## Scope
### In Scope
1. find_cli 解析优先 exe，清理 os_adapter.py `_ENGINE_WINDOWS_PATHS` 的 cmd 兜底路径
2. 启动前检测 cli_path 以 cmd/bat 结尾时输出 WARN 告警
3. 提示词改为 stdin 方式传参（opencode/codex），从机制上规避 argv 截断；prompt_via_stdin 默认 True
4. CI 增加 Windows 冒烟测试覆盖多行提示词

### Out of Scope
- hermes 引擎（真 exe 且主路径走 ACP，不受影响）
- Linux 平台（无 cmd 壳问题）
- CLI 引擎自身内部逻辑改动

## 关键设计决策
| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 传参方式 | argv/stdin/@file | stdin | stdin 不经过 shell 解析 |
| cmd 兜底策略 | 保留/移除/降级WARN | 保留但降级WARN | 兼容旧环境但告警风险 |
| prompt_via_stdin 默认值 | True/False | True | 机制上默认安全 |

## 实现基线核实（代码走查）
- 真实引擎实现在 `src/runtime/{opencode,codex,codearts,hermes,hwcloud}.py`（`src/agent/*.py` 为兼容转发层）
- 唯一 CLI 执行路径：`src/agent/executor.py` `_run_once`（约 L549 解析 agent_cmd、L595/L670 处理 stdin）
- `os_adapter.which()` Windows 扩展查找顺序为 `.cmd→.exe→.bat`；`_ENGINE_WINDOWS_PATHS` 中 opencode/codex/codearts/cursor 兜底硬编码 `.cmd`
- `executor.py:549` 主动 `shutil.which(binary + ".cmd")`

## 与 Spec 的偏差（开发决策）
- codearts 保留 argv 传 prompt：仓库内已有实证注释（`src/runtime/codearts.py:36` stdin 触发沙箱网络封锁），
  改 stdin 会引入已知的网络回归；Windows 截断风险由 exe 优先解析 + WARN 兜底覆盖。
- codex stdin 采用 `-` 位置参数标记（codex exec 约定：`-` 表示从 stdin 读取 prompt）。
