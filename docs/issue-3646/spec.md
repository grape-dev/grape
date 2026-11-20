# Issue #3646 需求文档：流水线「当前责任人」展示错误

> 目标合入分支：`arch-weekly-11-ljt` | 来源：Issue #3646（Spec comment 202636068）| 2026-11-21

## Objective

- 修复流水线信息「当前责任人」把内部 Agent 执行账号当成人展示的问题：
  Issue #3644 流水线显示 `当前责任人: gcw_3FZApeLT空闲`，实际责任人应为 `songspace`。
- 用户价值：责任人可信 → 运维/交接能直接定位到人，不再对着机器账号找人。

## 现象与根因

- 流水线「当前责任人」取值链（`src/domains/runtime/routes/pipeline.py`）：
  1. `issue_state.assignee`（GitCode 真实指派）；
  2. 事件 payload 里的 `assignee`；
  3. 最近一个有状态阶段（`status>0`）的最后一条事件 `actor` —— 仅判断是否在
     `bot_usernames` 名单内，命中则换 Agent 显示名，否则**原样输出 actor**。
- #3644 实测事件链：`vod(songspace)` → `issue(songspace)` → `qa(gcw_3FZApeLT)`；
  qa 阶段的 A2A 事件 actor 是 Agent runtime 身份（GitCode OAuth 自动登录名
  `gcw_*`），第 3 步没有过滤这类身份 → 直接展示 `gcw_3FZApeLT`。
- 另一处数据缺口：`set_issue_stage()` 把 `assignee or ""` 无条件写回卡片行，
  而 A2A 推进路径（`engine/service.py`、`a2a_dispatch.py`）只传角色不传 assignee，
  于是**一次推进就把责任人抹成空串**；且 assignee 只在 `issue_opened` 初始化。

## 范围

### In Scope

- 责任人解析优先级与内部账号过滤（人类优先，内部身份不进展示位）。
- Issue 指派/更新路径把 VCS 当前 assignee 回填 `issue_state.assignee`；
  推进路径不再清空已有 assignee。
- 单测：责任人解析组合用例 + assignee 不清空/回填用例。

### Out of Scope

- 前端 `Pipeline.vue` 的「空闲/执行中」文案与布局（本次确认它只是 `current_owner`
  的后缀，不改动）。
- 历史流水线数据批量订正（存量 `gcw_*` 值不回溯改写，靠解析侧过滤）。
- Issue 指派权限模型变更。

## 责任人口径（验收口径）

1. 人类 assignee 优先（`issue_state.assignee` 非内部账号）。
2. assignee 为空/为内部账号 → 事件 payload 的 assignee（同样过滤）。
3. 仍无 → 倒序扫描阶段事件 actor，跳过 `bot_usernames`、Agent 显示名/worker
   运行时身份、`gcw_*` 自动登录名，取第一个人类操作者（#3644 → `songspace`）。
4. 全为内部账号 → 回退当前阶段 Agent 显示名（`get_agent_display_name`），
   绝不输出原始内部账号。

## 验收标准

- [ ] 重新查看 Issue #3644 流水线，「当前责任人」显示 `songspace`
- [ ] 不再出现 `gcw_3FZApeLT` 等内部账号作为责任人
- [ ] 人类 assignee 优先；assignee 为 bot/内部账号时回退最近人类操作者
- [ ] 全部为内部账号时显示 Agent 显示名而非原始账号
- [ ] 现有测试通过（含本节新增用例）；前端「空闲/执行中」展示不受影响