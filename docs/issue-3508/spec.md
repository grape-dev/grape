# Spec: Issue #3508 — #tasks 流程配置页引擎健康双指标展示与报错修复

来源：Spec 评论（VOD Agent，评论 id 已脱敏）+ Issue Agent 深度分析（评级 ready）。

## 目标
流程配置页（#tasks → 场景流程编辑，scene-detail-local）能正常查看并修改绑定引擎健康状态：
- 不再出现系统报错（前端兜底 + 后端 500 根因修复）；
- 健康展示区分「健康分」与「物理可达性」双指标，异常原因可读；
- 绑定引擎修改校验候选范围、保存后实际生效；
- 接口异常时前端可读错误、可重试、不白屏。

## 现状（根因）
1. IssuesView（#tasks 创建弹窗）读 `/scenes/with-pipeline` 的 `all_offline` 字段，
   该字段在 Issue #501 已迁出（`/scenes/{scene_id}/engine-status`），前端从未调用新端点
   → 健康告警永远不显示（「不好查看」）。
2. 场景流程编辑页（DashboardView）引擎徽章只读 `agent_engines[*].status`（在线/离线），
   无 health_score / reachability / reason 任何字段 → 无法区分「健康分不足」与「物理不可达」。
3. `GET /scenes/{scene_id}/engine-status` 中 `get_pipeline_config` 调用不在异常保护内，
   DB/JSON 异常直接 500（「系统报错」）。
4. `PUT /scenes/{scene_id}` QA 节点提取 `getattr(n,'tag', n.get('tag')...)` 对非 dict/非模型
   节点抛 AttributeError → 500；引擎 ID 校验存在行解包无长度防护；非法/悬空 engine id
   静默落库导致「保存后不生效」。

## 期望行为
1. `GET /scenes/{scene_id}/engine-status` 扩展结构化返回：
   `engines: [{id, name, health_score, reachability, reason, node}]`，
   `low_score_nodes`（存在可达但不健康引擎）与 `offline_nodes`（全部物理不可达）分列，
   多引擎混合节点分类完整；
   保留旧字段（offline_nodes/sre_unbound/all_offline）兼容存量调用方。
2. 端点内部异常降级为 2xx + `{degraded: true, error: 可读文案}`，不裸 500。
3. `PUT /scenes/{scene_id}`：修复 QA 节点提取 500；校验候选范围（scope/能力，已有）+
   落库前清洗悬空/非法 engine id，保存配置真实生效；清洗后绑定由非空变空（全部悬空/
   非法）时显式 400（SRE 与非 SRE 一致），不允许静默清空为空绑定=广播。
4. 前端 IssuesView 创建弹窗：选中场景后按需调用 engine-status，双指标告警
   （健康分不足 / 物理不可达）+ 原因可读 + 「去换绑」入口；失败可重试不白屏。
5. 前端 DashboardView 流程配置引擎区域：双指标健康徽章（分值 + 可达性 + 原因）、
   接口失败展示可读错误 + 重试按钮；引擎选择弹窗展示健康分与可达性（含未绑定候选引擎）。

## 验收标准
- [ ] #tasks 页打开引擎健康区域不再报错，相关接口返回 2xx 并正常渲染。
- [ ] 健康分不足与物理不可达在页面可明确区分并展示原因。
- [ ] 修改/更换绑定引擎保存成功，刷新后状态一致且后续派发使用新配置。
- [ ] 接口失败时页面展示可读错误、可重试、不白屏。
- [ ] 覆盖「健康分不足」与「物理不可达」各至少一次回归验证。

## Out of Scope
引擎探活调度策略改造、告警大盘（#404 等关联单）。
