# Spec: Issue #3790 — 场景未纳入 scene pool 的派发降级可观测化

> 编码实现依据 Issue #3790 评论中的 Spec（Issue Agent 发布）细化到可执行粒度。

## Objective

仓库场景未在 grape-skills scene pool（`devops_pipeline_scene` 仓内副本）登记时，派发降级链路
携带结构化来源标记 `pool_engine_source=scene_missing`，并把每小时 22 条无结构 WARNING 收敛为
事件中心可筛、可按 (repo_key, scene_id, agent_type) 窗口合并的告警。运营据此区分
「场景未注册」与「引擎真离线」。

## Scope

### In
- `engine_select.POOL_SOURCE_*` 扩展第四态 `scene_missing`；`get_scene_pool_engine_ids`
  通过可选 out 参数上抛来源（返回语义 `str | None` 不变）。
- `scene_missing` 告警按 `(repo_key, scene_id, agent_type)` 时间窗收敛（首次输出、窗口内计数抑制、
  窗口滚动输出累计值）。
- `event_pipeline._scene_pool_gate` 回传来源；主派发 / PR 订阅补派与重派显式把来源传给
  `_enqueue_pipeline_task`，避免被「带 scene 上下文 → 推导为 scene」覆盖；入队落
  `agent_tasks.pool_engine_source`；事件 payload 回填 `pool_engine_source / scene_id /
  repo_key / agent_type`。
- 场景未登记时派发失败文案改用「仓内不存在 + 原因码 scene_missing」，不再笼统报「引擎全部离线」。
- `set_repo_scene`（建仓 / 场景绑定的单点写侧）与模板建仓回执给出 scene pool 缺失提示（不阻断）。
- 新增回归测试；`#541` 注释口径统一为「跳过全局模板回退 + scene_missing 可观测」。

### Out
- 不恢复全局模板回退，不改 `strict_scene_engines` 默认严格语义。
- 不做历史 `agent_tasks` / `events` 行回填与数据迁移。
- 不做前端新筛选控件（以事件 payload 字段 + 现有事件详情页为准）。

## 关键设计

| 决策 | 选择 | 理由 |
|------|------|------|
| 字段命名 | 复用 `pool_engine_source`，新增取值 `scene_missing` | 已有 DB 列与事件词汇，避免双字段语义漂移 |
| 来源上抛 | `get_scene_pool_engine_ids(..., source_out=dict)` | 既有调用点零改动，返回类型不变 |
| 告警收敛 | 进程内窗口计数（默认 300s）+ 窗口滚动汇总 | 22 条/时 → 每窗口 1 条 + 计数，不丢总量 |
| 事件落点 | 派发事件 payload 合并四字段 | 门禁在事件 INSERT 之后解析，需回填 |
| 保存侧提示 | 复用 `scene_exists_in_repo`，只告警/回执提示不阻断 | 历史直写路径仍需落库 |

## Acceptance

1. 场景未登记时 `agent_tasks.pool_engine_source='scene_missing'`，事件 payload 含
   `pool_engine_source=scene_missing`（及 scene_id/repo_key/agent_type）。
2. 同一 (repo_key, scene_id, agent_type) 的缺失告警按窗口收敛，日志行带结构化字段。
3. 建仓 / 场景绑定写入对缺失给出提示且不阻断保存。
4. 回归测试覆盖缺失路径、来源不被推导覆盖、告警收敛。
5. 现有测试通过（含 `tests/runtime/test_issue_541_template_scene_id.py` 中本单修复的用例）。