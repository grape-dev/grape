# 任务拆解：Issue #378

## T1: src/runtime/agent.py — 新增映射表 + _execute_task 注入
- [x] 模块级新增 `_AGENT_SILENT_TIMEOUTS` / `_DEFAULT_SILENT_WINDOW`
- [x] `_execute_task` 在 env 构建后按 agent_type setdefault 注入三个 env
- 影响文件：src/runtime/agent.py

## T2: src/agent/hermes_acp.py — _SILENT_TIMEOUT 读 env
- [x] L448 写死 300 改为读 HERMES_SILENT_TIMEOUT env（默认 300，与 hwcloud 对齐）
- 影响文件：src/agent/hermes_acp.py

## T3: src/agent/executor.py — _silent_window 读 env dict（Spec 偏差）
- [x] L734 os.environ.get 改为 env.get(..., os.environ.get(...))（优先任务级 env）
- 影响文件：src/agent/executor.py

## T4: 单元测试
- [x] tests/test_agent_silent_timeouts.py：
  - agent_type→env 注入映射（coding/qa=1200，其他不注入）
  - setdefault 不覆盖已有值
  - hermes _SILENT_TIMEOUT 读 env 默认值与覆盖值
  - executor _silent_window 读 env dict
- 影响文件：tests/test_agent_silent_timeouts.py（新增）

## T5: 自审查 + 建 PR
- [x] 5 轴自审查 + 敏感增量扫描
- [x] 建 PR（base=weekly-clean-mys）
- [x] Issue 评论告知 + A2A QA
