# Plan: Issue #351

## 方案
1. `src/runtime/os_adapter.py`
   - `which()`：Windows 查找顺序改为 `.exe → 裸名/PATHEXT → .cmd → .bat`（exe 优先，issue #351）
   - `_ENGINE_WINDOWS_PATHS`：opencode/codex/codearts/cursor 兜底路径增加 `.exe` 候选并排在 `.cmd` 之前
2. `src/agent/executor.py`
   - CLI 解析移除 `shutil.which(binary + ".cmd")` 主动查找，统一走 `os_adapter.find_cli`（exe 优先）
   - 解析结果以 `.cmd/.bat` 结尾时启动前输出 WARN（提示多行提示词截断风险）
3. `src/runtime/base.py`：`prompt_via_stdin = True`（机制默认安全）
4. `src/runtime/opencode.py`：`prompt_via_stdin = True`，build_args 不再 append prompt（opencode run 无消息参数时从 stdin 读取）
5. `src/runtime/codex.py`：`prompt_via_stdin = True`，build_args 以 `-` 占位（codex exec 从 stdin 读 prompt）
6. `src/runtime/hermes.py`：显式 `prompt_via_stdin = False`（Out of Scope，行为保持）
7. `src/runtime/codearts.py`：保留 argv（stdin 触发沙箱网络封锁的实证），注释更新引用 #351
8. 测试：更新受影响断言 + 新增 tests/test_issue_351_windows_prompt.py（exe 优先 / WARN / stdin 传参 / 默认值）
9. CI：`.github/workflows/windows-smoke.yml` Windows runner 冒烟（多行提示词单测）

## 依赖顺序
base.py → opencode/codex/hermes → executor.py → os_adapter.py

## 风险
- 引擎不读 stdin → opencode/codex 均为文档化的 stdin 约定；探活与执行同路径，行为一致
- 旧环境无 exe → cmd 兜底保留 + WARN
