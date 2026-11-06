# Issue #1245 需求文档：建单前场景可用性预校验

## 目标
在需求建单（API）与 GitCode 直接建单（webhook）两个入口，对所选场景做静态只读可用性
预校验，提前拦截坏引擎、引擎全离线、他人个人机器、技能不可解析、节点结构损坏，避免
产生“死单”。

## 范围
- 新增 `src/api/scene_precheck.py`：`precheck_scene(scene_id, repo_key, user)`。
- API `create_task_via_issue`：查重 + assignee 预校验之后、GitCode 建单之前拦截，
  返回 400 + `detail.problems[]` + `trace_id`，零副作用。
- webhook `issue_opened`：不拒单，不启动流水线，事件置 failed 且含 reason_code，
  并在 Issue 评论原因。
- `configs/scene-precheck.json`：`enforce` 总开关 + 6 个 reason_code 独立开关，
  默认全开；配置/查询异常 fail-open。

## 校验口径
严格口径 B：对场景中「存在且 enabled」节点逐个校验：
1. 绑定引擎存在于 `agent_runtimes`。
2. 至少一台可用（沿用 `engine_select._health_ok` + 健康分 >= 30）。
3. 引擎归属合法（user 档属本人，repo 档属当前仓库）。
4. 节点技能可解析（`skill_id` 有效或 `resolve_skill_id` 命中）。

三类豁免：
- 非 SRE 节点 `engine_runtime_ids` 为空（全量广播）。
- `enabled=false` 节点。
- `skill` 为空的节点（跳过技能校验）。
- SRE 空绑定不豁免，仍拒绝。

结构校验：`next` 指向不存在节点、节点 id 重复。

## reason_code
`engine_missing` / `engine_all_offline` / `foreign_personal_engine` /
`skill_unresolvable` / `scene_node_dangling` / `scene_node_duplicate`。

## 验收
- 单测覆盖 6 个 reason_code、3 类豁免、fail-open、enforce=false。
- API 被拦返回 400 + problems[] + trace_id，GitCode 零新单。
- 双入口生效，现有测试全绿。