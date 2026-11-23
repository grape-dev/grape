# 组织下派待确认问题（2026-11-20）

记录当前实现里两个需要产品/架构决策的问题，先不改代码。

## 1. 托管仓库的“默认场景”语义不完整

- 托管成功后会调用 `ensure_default_agent_sessions(repo_key)`（`src/domains/mgmt/routes/repos.py`）。
- 只有该仓库 active 场景数为 0 时，才会从 `scene_market` 的官方 `default`（显示名“通用开发”）
  复制一条仓内场景；托管失败/partial、官方模板缺失、建场景失败时不会创建。
- 复制出来的只是 `devops_pipeline_scene` 的一条场景，**不会写 `managed_repos.dev_scene`**。
- 结果：托管仓库常见状态是“1 条场景 + `dev_scene=''`”。下派能成功只是因为命中了
  `_resolve_target_scene` 的“唯一场景”兜底；一旦仓库再应用第二个场景，就没有默认场景可解析。
- 页面上没有展示/设置 `dev_scene` 的入口；语言包里仍有 `current_default` / `set_default`
  但当前前端未使用。

待确认：

- 是否需要在托管时同时写入 `dev_scene`（真正设置默认场景）？
- 是否需要在“项目场景”页显示“当前默认”，并提供“设为默认”？
- 多场景仓库的默认场景策略：模板指定、托管默认、人工选择，优先级如何定？

## 2. 组织下派子任务的 SRE 节点可能直接失败

- SRE 引擎约束：只允许 `user` / `repo` 两档，**不支持广播**，也没有 global 档
  （`engine_select._sre_no_broadcast` / `get_scene_pool_engine_ids`）。
- 官方 `default`（“通用开发”）场景中的 `sre` 节点当前是 **enabled + 未绑定引擎**；
  市场场景复制时不走 SRE 保存校验，所以这个未绑定状态会原样进入关联仓库。
- 组织下派时，`_create_child_issue` 把解析出的 target scene 作为**显式场景**传给建单；
  `create_task_via_issue` 对显式场景执行严格 `precheck_scene`：
  - SRE 未绑引擎 → `engine_missing` → 400，子 Issue 不创建，target 直接 `failed`；
  - SRE 绑了别人的个人引擎 → `foreign_personal_engine` → 同样拦单；
  - 绑仓库级引擎才可以通过。
- 若预检查被绕过（例如配置降级/内部路径），运行时会返回 `pool_engine_ids=None`，
  事件置 failed 并落一条 failed task 快照；sweeper 在下一轮把 target 标 failed。
- 收口语义：dispatch 进入 `partial_failed` / `failed` 后，组织 Issue 会被归一为
  `pending_review`（有失败聚合评论），不会自动 `completed`；不会永久卡 running，
  但需要人工处理，达不到“自动完成”。

待确认：

- 关联仓库的 SRE 节点是否统一绑定仓库级引擎（org dispatch 不依赖个人机器）？
- 不需要部署的组织需求，是否允许在 target scene 中禁用 SRE 节点？
- 是否允许组织级 SRE 引擎池（安全边界需要单独评审）？
- 子任务代码/QA 已完成但 SRE 未绑定时，产品上应算 `failed` 还是 `needs_review`？
