# Spec: Issue #1745 跨场景转交后 GRAPE_SCENE_ID 仍为源场景，阻断 A2A 场景校验

> 评级：ready（Issue #1745 Spec 评论）| 开发基线：weekly-mys（服务端注入的 `GRAPE_WORK_BRANCH`）
> 来源：用户反馈后台 id 3186；VOD 结构化收敛见 Issue 评论。

## 1. 目标
跨场景转交（Step 4b）后重入 issue 节点时，会话注入的场景上下文与 Issue 真实所属场景保持一致；
场景校验不再因读到源场景 ID 而误判「无 coding 节点」，A2A 交办可正常完成；不一致时给出可操作诊断，
仅在真实场景确实不含目标节点时才阻断。

## 2. 现状与根因
- 转交链路：issue 节点把整单移交目标场景（`target_scene_id` + `target_node=vod`）；目标场景 vod → issue
  重新进入时，任务 env 快照可能仍带源场景。
- 注入侧：`_inject_scene_env` 以 `scene_id_override or _resolve_scene_id(issue_id)` 取值。跨场景转交后
  `issue` 表 `scene_id` 已指向目标场景，但**普通事件重派**（`src/api/routes/events.py`）沿用事件行旧
  `scene_id`（源场景）作为 override，覆盖了 Issue 当前归属场景；worker 执行前拉取的「最新 env」此前也只
  同步 AK/SK/session token，不校正 `GRAPE_SCENE_ID`。
- 消费侧：runbook Step 5 直接用 `$GRAPE_SCENE_ID` 调 `scenes detail`，取到源场景节点（通常只有
  vod/issue），coding 校验未命中 → 误 BLOCK。runbook 属 `grape-dev/grape-skills`，本仓仅落口径引用。
- 与 #2745（Step 5 `SCENE_NODES` 变量跨调用失效）、#1681/#1597（`scene_agents` jq 提取失效）区分：
  那两条是命令/表达式缺陷；本条是转交后场景归属快照未刷新，命令本身正确。

## 3. 修复方案（三层）
1. **注入侧（主修）**：
   - 普通事件重派不再用事件行 `scene_id` 覆盖 Issue 归属场景；仅 PR 订阅事件（伪 id `#pr_...` / PR 事件类型 /
     `PR 订阅` 标题）保留订阅场景 override（Issue #427 语义）。
   - worker 执行前 `GET /runtimes/{id}/tasks/{task_id}/env` 增加场景校正：按 `agent_tasks → issue` 当前归属
     场景重算 `GRAPE_SCENE_ID`，覆盖陈旧快照；PR 订阅任务不校正。
   - 值以 Issue 归属场景（与 `issues detail.scene_id` 同源）为唯一真值，不沿用源场景快照。
2. **校验口径（兜底，跨仓协同）**：runbook Step 5 场景校验以 `issues detail` 的 `scene_id` 为权威，
   `GRAPE_SCENE_ID` 仅作兜底；技能资产归 `grape-dev/grape-skills`，本仓在 `help/usage/agent-environment-variables.md`
   固化口径引用，两侧不得分叉。
3. **可操作诊断**：注入值与真实值不一致时输出双方取值 + 「按真实场景 `scenes detail` 重取节点后重试」指引；
   仅在真实场景确无目标节点时才进入 `a2a_need_clarify`。本仓提供 `scene_env_mismatch_diagnostic` 文案与日志。

## 4. 验收标准
- [x] 跨场景转交后重入 issue 节点，会话 `GRAPE_SCENE_ID` 等于 `issues detail.scene_id`（目标场景），
      注入链路有回归测试覆盖（普通事件重派不沿用源场景）。
- [x] 人为把任务快照 `GRAPE_SCENE_ID` 置为陈旧值，执行前校正后仍能取到目标场景节点并成功 A2A，不再误 BLOCK。
- [x] 场景不一致时输出含双方取值与恢复指引的诊断（日志/文案），不向最终用户泄露内部 scene id。
- [x] 回归：单场景（无转交）流程校验与交办行为不变；PR 订阅场景（#427）override 语义不变；
      `scenes detail` 404/跨流水线既有分支不退化。

## 5. 影响面
- 仅会话环境注入/执行前校正与重派 override 选择，不改 A2A 协议字段（`target_scene_id`/`target_node` 语义不变）。
- 风险：执行前校正多一次 `agent_tasks`/`issue` DB 读取（非 scenes 查询，无网络/缓存开销）；PR 订阅任务显式跳过，
  避免把订阅场景误校正为关联 Issue 场景。

## 6. 测试
- `tests/test_issue_1745_scene_env_truth.py`：重派 override 选择、注入真值、PR 订阅兼容、执行前校正、诊断文案。
- 既有回归：`tests/test_scene_env.py`（#427 override 四路径）、`tests/test_issue_568_scene_visibility_unification.py`
  （scenes detail / handoff 可见性）、`tests/test_issue_990_session_token_snapshot.py`（task env 快照同步）。

## 7. 协同项
- runbook/技能侧正文改动归 `grape-dev/grape-skills`；本仓修复注入侧并在 help 文档固化口径，需同步提交技能侧修正。
- 场景归口：运行域-任务编排专用开发流水线（A2A 交办与跨场景转交、场景参数注入、场景与流水线定义）。
