# Issue #2395 实现方案

## 改动
1. `src/domains/runtime/agents/executor.py`
   - 新增 `_apply_runtime_env_contract()`：在 4 条执行路径（通用 CLI、hermes ACP、
     hwcloud ACP、aishell-dsh ACP）的 `env.update(env_extra)` 之后统一调用。
   - `DEVOPS_EVENT_TYPE` 以真实 `event_type` 覆盖；`ACCOUNT` 拒绝仓库标识；
     `GRAPE_ISSUE_ID` 由 `ISSUE_NUMBER` 解析内部卡片 id；`GRAPE_WORK_BRANCH` 回退场景。
   - 缺失输出 WARNING（仅变量名 + 引擎标签），降级不阻断。
2. 各引擎适配层
   - 删除 `get_env()` 中的 `DEVOPS_EVENT_TYPE=""`：codex/opencode/dsh/hermes/
     codearts/atomcode/aishell_acp/hermes_acp/hwcloud_acp。
3. `src/domains/runtime/im/vod_handler_async.py`
   - VOD worker/direct 两条路径补 `ACCOUNT`（触发人）与 `ISSUE_NUMBER`，
     direct 路径透传真实 event_type，供统一装配解析 `GRAPE_ISSUE_ID`/`GRAPE_WORK_BRANCH`。

## 兼容性
- 不新增环境变量；`GRAPE_ISSUE_ID` 语义保持内部卡片 id，避免与 `ISSUE_NUMBER` 混用。
- 缺失变量仅告警，历史任务/旁路调用行为不中断。
