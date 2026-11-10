# Spec: Issue #1811 — Autopilot 定时调度注入 BOT_USERNAME / AGENT_NAME

> 来源：Issue Agent Spec（评论 id=199602606，评级 ready）
> 目标基线分支：weekly-mys（服务端注入 `GRAPE_WORK_BRANCH` 权威值）

## 目标

Autopilot 定时任务执行前，在 `env_extra` 中注入 `BOT_USERNAME` 与 `AGENT_NAME`，使
pending-merger-notice 等 bot skill 的「评论作者」幂等查重可精确判定，不再因作者缺失
退化为形态判定（可见标记 / 单行模板）而误判他人同形态评论。

## 根因（代码实勘）

- `src/runtime/scheduler.py::_execute_autopilot_agent`（Spec 文档中的
  `_execute_autopilot_task` 即此函数，本基线实际命名为 `_execute_autopilot_agent`）
  第 6 步组装的 `env_extra` 仅含 `REPO_FULL_NAME` / `REPO_OWNER` / `REPO_NAME` /
  `ACCOUNT` / `resolve_task_server_env` / `custom_fields` / `GRAPE_WORK_BRANCH`，
  全文件无 `BOT_USERNAME` / `AGENT_NAME`。
- #573 的修复落在 `src/api/event_pipeline.py` 三个注入点；Autopilot REMOTE 分支经
  `src/runtime/service.py::enqueue_task` 入队，该函数自身不做 bot 注入，因此定时调度
  DIRECT / REMOTE 两条分支都拿不到变量。
- `BOT_USERNAME` / `AGENT_NAME` 均在 `src/scene_custom_env.py::_RESERVED_ENV_NAMES`
  保留集内，任务 `custom_fields` 无法兜底，只能由平台注入。

## Scope

### In Scope

- `src/runtime/scheduler.py::_execute_autopilot_agent` 第 6 步 env 组装末尾：函数内
  延迟 import 并调用 `event_pipeline._inject_bot_agent_env(env_extra, repo_key, agent_type)`，
  单点覆盖 DIRECT / REMOTE 两分支。
- 注入后 `BOT_USERNAME` 为空时输出显式 WARNING（含 exec id + repo）。
- 新增 `tests/test_issue_1811_autopilot_bot_agent_env.py`：两分支注入、保留集拒绝、
  多 bot 取值、空值告警；回归 `tests/test_issue_573_bot_agent_env_injection.py`。
- 落档 `docs/issue-1811/{spec,plan,task}.md`。

### Out of Scope

- 不改 `_resolve_bot_username` 三级兜底逻辑（#573 已定）。
- 不改 `scene_custom_env._RESERVED_ENV_NAMES` 保留集。
- 不改 pending-merger-notice 查重算法（技能资产在外部仓库）。
- 不改 event_pipeline 三个既有注入点。

## Technical Approach

1. 在第 6 步 `GRAPE_WORK_BRANCH` 注入之后追加：

   ```python
   from src.api.event_pipeline import _inject_bot_agent_env
   _inject_bot_agent_env(env_extra, repo_key, agent_type)
   if not env_extra.get("BOT_USERNAME"):
       log.warning(
           f"Autopilot exec {exec_id}: BOT_USERNAME unresolved for repo={repository}; "
           "skill author filtering will degrade"
       )
   ```

2. `_inject_bot_agent_env` 使用 `setdefault`，幂等且不覆盖调用方显式设置；
   DIRECT 已把 `env_extra` 传给 `stream_devops_agent`，REMOTE 已传给 `enqueue_task`，
   分流函数无需改签名。
3. 函数内延迟 import 延续 scheduler 既有做法，规避 scheduler ↔ event_pipeline
   模块级循环依赖。

## Acceptance Criteria

- [x] DIRECT、REMOTE 两分支 `env_extra` 均含非空 `BOT_USERNAME`（该 repo 的 bot login）
      与 `AGENT_NAME`
- [x] `custom_fields` 写 `BOT_USERNAME` / `AGENT_NAME` 仍被保留集拒绝，最终值来自平台
- [x] 多 bot 场景：repo 级 `bot_username` 优先，缺失回落全局 bot，与 event_pipeline 一致
- [x] `BOT_USERNAME` 解析为空时输出可辨识 WARNING（exec id + repo）
- [x] 回归 `tests/test_issue_573_bot_agent_env_injection.py` 全过；新增用例覆盖以上 4 点
- [x] 相关 pytest 子集通过（无新增失败）

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| scheduler 顶层 import event_pipeline 引发循环依赖 | Med | 函数内延迟 import |
| 注入改变 autopilot 任务 env 快照 | Low | 仅新增 2 个字符串键，setdefault 幂等 |
| DIRECT 模式 `agent_name` 参数与 env 内 `AGENT_NAME` 不一致 | Low | 两者同源 `get_agent_display_name(agent_type)`，测试断言相等 |