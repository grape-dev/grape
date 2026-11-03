# Issue #658 Spec（开发基线摘录）

> 来源：Issue #658 issue agent 发布的 📋 Spec Document（评级 ready）。开发基准分支：`weekly-08-mys`。

## Objective

派发器在引擎候选筛选最前置按「节点角色 agent_type × 引擎角色 role_tags」做硬过滤，
杜绝 vod 等角色被派给不匹配引擎类型（如 codearts）导致的 `rc=1` 执行失败。

## In Scope

1. 引擎注册支持声明 `role_tags`（显式字段；缺省按引擎类型默认角色集兜底：
   opencode→[coding,issue,qa]、hermes→[vod,chat,sre]、codearts→[coding,issue,qa]）。
2. 派发器候选筛选阶段执行硬过滤：`agent_type ∈ engine.role_tags`，过滤后无候选时
   报明确错误（错误信息含 agent_type 与各可用引擎 role_tags），不静默降级。
3. 无 role_tags 声明且引擎类型不在默认映射内的存量引擎：不因缺声明被全量排除（零配置兼容）。
4. 回归测试：#90 vod→codearts 反例（断言 vod 不会被派给 codearts）；角色匹配正向用例。

## Out of Scope

- #653（场景节点 engine_runtime_ids 指定引擎约束缺失）——分单。
- #659（帮助文档「引擎选择与派发规则」章节联动）。
- priority / scope 档位排序逻辑调整。
- 前端引擎展示与兜底逻辑。

## 验收标准

- [ ] 引擎注册支持 `role_tags` 字段声明（或引擎类型→角色集默认映射）。
- [ ] 派发器在选引擎时执行 `agent_type ∈ engine.role_tags` 硬过滤。
- [ ] 无匹配候选引擎时派发器报错（含 agent_type 与可用引擎 role_tags），不静默降级。
- [ ] 回归 #90 场景：vod 节点不再被派给 codearts，匹配到 hermes 类引擎执行成功。
- [ ] 现有 opencode→Coding/Issue/QA、hermes→VOD/Chat/SRE 分工在派发层强制生效。
