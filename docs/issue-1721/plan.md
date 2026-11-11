# Plan: Issue #1721

## 方案概述
读侧增量字段 + 审计通道授权放宽 + 冲突清单改绑指引，全部复用既有 scope 判定函数，不动派发路径。

## 实现步骤
1. `src/api/routes/scenes.py` 新增节点级 scope 判定辅助：
   - `_scene_node_scope_check(node, repo_key, scene_id, cache)`：先按节点 `skill_id` 查 active 技能（命中即授权，与派发侧 `resolve_skill_dispatch` 一致），未命中则调用 `src.api.event_pipeline._skill_scope_mismatch` 按名字判定；reason 使用 detail 语境措辞。
   - `_attach_scene_node_scope_checks(nodes, repo_key, scene_id)`：复制节点后附加，避免污染 `get_pipeline_config` 缓存；同一响应内按 `skill_id`/技能名缓存判定，降低重复查询。
2. `get_scene_detail` 返回前调用上述辅助，纯增量字段。
3. `audit_scene_skill_scope`：
   - 依赖改为 `get_current_user_or_api_key`；
   - `auth_type=session_token` 时强制 `repo_key` 为会话绑定仓，显式其它仓返回 403；
   - 既有 `_collect_scene_skill_scope_violations(allow_own_user=False)` 逻辑保持不变；
   - 为每个冲突追加 `suggested_skills`（repo/official 级同 agent_type 候选）。
4. 测试：`tests/test_issue_1721_scene_scope_precheck.py` 覆盖 detail 冲突/无冲突、audit tk_ 本仓、越权 403、只读与建议字段。

## 关键设计决策
| 决策 | 选择 | 理由 |
|---|---|---|
| 判定逻辑 | 显式 `skill_id` 优先，否则复用派发侧 `_skill_scope_mismatch` | 与派发侧同口径，避免误报与规则漂移 |
| detail 节点处理 | 复制后附加字段 + 同响应去重缓存 | 缓存对象不可原地修改，降低重复查询 |
| 私有符号依赖 | 直接调用 `event_pipeline._skill_scope_mismatch`（契约 `(skill_name, repo_key) -> (mismatch, info)` 已注释） | 复用派发侧口径；上游签名变更需同步 detail |
| tk_ 鉴权 | 会话仓只读 + 显式越权 403 | 放宽可用性同时不扩大数据范围 |
| 存量修复 | 仅输出 suggestions | 历史配置由管理员显式改绑 |

## 风险与缓解
| 风险 | 缓解 |
|---|---|
| 逐节点解析带来查询开销 | 同一响应内按 `skill_id`/技能名去重缓存；detail 单场景节点数有限；无冲突路径行为不变 |
| tk_ 越权 | 校验显式 `repo` 与绑定 `repo_key` 一致，越权 403 |
| 增量字段影响消费方 | 纯新增字段，既有字段保留 |
