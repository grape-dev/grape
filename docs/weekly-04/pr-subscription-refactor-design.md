# PR 订阅场景迭代需求文档

## 背景

当前 PR 订阅作为独立场景类型（scene_type='pr_subscription'）存在，问题：
1. 用户学习成本高——需要单独创建一个 PR 订阅场景
2. 不区分工作分支——无法按不同分支匹配不同场景的 QA
3. 与标准场景割裂——高级选项、技能配置需要重复设置

## 目标

将 PR 订阅从独立场景类型改为标准场景的附加能力：用户在创建/编辑标准场景时开启 PR 订阅开关即可。PR 提交时按 target_branch 匹配场景的 work_branch，命中则唤醒该场景的 QA agent 审查，不进一步 A2A。

## 设计

### 1. DB 改动

**新增字段：**
- `devops_pipeline_scene.pr_subscription_enabled`（INTEGER DEFAULT 0）

**数据迁移：**
- 已有 `scene_type='pr_subscription'` 的场景 → `scene_type='standard', pr_subscription_enabled=1`
- 迁移后 `scene_type` 只剩 `standard`，`pr_subscription` 值不再使用

**字段废弃：**
- `scene_type` 字段保留但不再使用 `pr_subscription` 值（向后兼容不删列）

### 2. 触发逻辑

**入口：** event_pipeline.py 第973行，PR 事件无关联 Issue 时

**新逻辑 `_match_pr_subscription`（替换原 `_try_pr_subscription_scene`）：**
1. 提取 PR 的 target_branch（已有 `resolve_pr_target_branch`）
2. 查仓库内所有 `pr_subscription_enabled=1 AND is_active=1` 的场景
3. 匹配场景的 work_branch（从 advanced_options 读）== PR target_branch
4. 命中 → 从场景 nodes_json 取 QA 节点的 skill，唤醒 QA agent
5. 多个场景命中同一 PR → 多个 QA 同时审查

**去重调整：**
- 伪 issue_id 从 `#pr_{pr_num}` 改为 `#pr_{pr_num}_{scene_id}`，区分不同场景的 QA 任务
- 同场景同 PR 已有 running QA → 跳过（保持原有去重）
- 不同场景同 PR → 允许并行

### 3. 不 A2A 控制（方案 A）

**实现位置：** webhook.py A2A 端点（第222行 a2a_endpoint）

**逻辑：**
- PR 订阅触发的 QA 任务带标记 `_pr_sub_scene_id`
- A2A 端点检查发起方是否带此标记
- 带标记 → 拒绝，返回 `{"ok": false, "skipped": "pr_subscription_a2a_disabled"}`
- 不带标记 → 正常 A2A 流程

### 4. 代码清理

**删除：**
- `_try_pr_subscription_scene` 函数（event_pipeline.py 第24-54行）→ 替换为新函数 `_match_pr_subscription`
- `scene_type='pr_subscription'` 相关的所有查询分支
- `_PR_SUB_ENABLED` feature flag（GRAPE_PR_SUBSCRIPTION_ENABLED）→ 改为直接查场景 `pr_subscription_enabled` 字段

**保留：**
- `_pr_sub_scene_id` / `_pr_sub_skill` / `_pr_sub_pr_number` 等 evt 标记 → 新函数继续写入
- PR 订阅回帖逻辑（第2506行）→ 不变
- SRE 节点可选逻辑 → 保留，如果场景有 SRE 节点且 pr_subscription_enabled，QA 完成后可手动触发 SRE

### 5. API 改动

**scenes.py：**
- 创建/更新场景 API 支持传入 `pr_subscription_enabled` 字段
- 查询场景列表时返回 `pr_subscription_enabled` 状态

### 6. 前端改动

**场景编辑页：**
- 新增 PR 订阅开关（Toggle）
- 开关下方提示："开启后，当有 PR 的目标分支匹配本场景的工作分支时，自动触发 QA 审查"

## 影响评估

改动范围小且集中：后端 3 文件 + 前端 1 文件。
现有 `scene_type='pr_subscription'` 的查询分支约 10 处，全部替换为 `pr_subscription_enabled` 字段判断。
不影响已有标准场景的 Issue 驱动流水线，PR 订阅是旁路触发。
