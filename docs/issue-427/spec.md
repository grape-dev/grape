# Spec: Issue #427 — PR 关联 Issue 时 PR 订阅 QA 任务 env 走 Issue 场景，订阅场景 custom_fields 丢失

> 基于 Issue Agent 已发布 Spec（developability: ready）落地的实现需求文档。

## 1. 背景与根因

PR 订阅派发的 QA 任务，环境变量场景解析走 issue_id 链路：`_inject_scene_env` → `_resolve_scene_id` → `get_issue_scene(issue_id)`。

- PR 无关联 Issue：issue_id 为伪 id `#pr_<PR号>_<场景ID>`，正则解析出订阅场景 → custom_fields 正常注入（2026-10-21 已修，Out of Scope）
- PR 有关联 Issue（PR #586 关联 Issue #419 实测）：issue_id 解析为真实 Issue 编号 → env 按 Issue 场景（如代码合规检查，custom_fields 为空）解析 → 订阅场景自定义变量（`BUILD0007_PASSWORD` / `TEST_INTERNAL_IP`）全部丢失

同一任务里 skill 从 `evt._pr_sub_scene_id` 取订阅场景、env 从 issue_id 推导出 Issue 场景，两套口径不一致。QA 前置检查（printenv 无口令）失败，审查空转。

语义佐证：`_filter_pr_sub_matches` 本就排除「PR 关联 Issue 所在场景」防重复 QA——订阅场景与 Issue 场景是有意区分的，env 与 skill 同用订阅场景才是正确口径。

## 2. 修复方案

`_inject_scene_env` 增加可选参数 `scene_id_override`，PR 订阅分支显式传入订阅场景：

1. `src/api/event_pipeline.py` `_inject_scene_env` 签名追加 `scene_id_override: str = ""`；函数开头场景解析改为 `scene_id = scene_id_override or _resolve_scene_id(repo_key, issue_id)`——GRAPE_SCENE_ID 与 custom_fields 注入逻辑共用该 scene_id，后续代码不动
2. 入队主路径 `_enqueue_pipeline_task` 的 env 组装处调用改为 `_inject_scene_env(env_extra, repo_key, issue_id, agent_type, scene_id_override=evt.get("_pr_sub_scene_id", ""))`
3. 重派路径 `build_task_env` 同样追加 `scene_id_override` 参数，由调用方从旧任务场景传入（events.py 两处 redispatch：`evt.scene_id` 即订阅场景，evt 为 `_load_event` 返回的事件行）
4. direct 执行路径（`_execute_agent`，无 evt 上的订阅语义透传需求，`_pr_sub_*` 只在主路由/PR 订阅派发路径的 evt 上携带）保持现状，仅在 PR 订阅 direct 派发点同样传 override

## 3. 影响路径盘点（evt.\_pr_sub_scene_id 的生命周期）

| 路径 | evt 是否携带 `_pr_sub_scene_id` | 覆盖动作 |
|------|------|------|
| 主路由入队（`_enqueue_pipeline_task`，L2578） | 是（L1590 设置） | 传 override |
| PR 订阅多场景补派（L958/L976） | 是（`_extra_evt` L913） | 自动经 1 覆盖 |
| PR 订阅专用重派（`_dispatch_pr_subscription_redispatch` L1060/L1078） | 是（L1047） | 自动经 1 覆盖 |
| 通用重派（events.py L499/L866 `build_task_env`） | 否（`_load_event` 无 `_pr_sub_*`，但事件行 `scene_id` = 订阅场景） | `build_task_env` 新参从 `evt.scene_id` 传入 |
| direct 执行（`_execute_agent` L3409） | PR 订阅 direct 派发点携带 | PR 订阅 direct 派发点传 override |
| 对话入队（events.py L1448 `build_task_env`） | 否 | 不传（默认空串，行为不变） |

## 4. 验收标准

- [ ] PR 关联 Issue + 订阅命中：任务 printenv 含 `GRAPE_SCENE_ID=<订阅场景>` 且 `BUILD0007_PASSWORD` 非空
- [ ] PR 无关联 Issue（伪 id 路径）：行为不变（override 与正则解析结果相同）
- [ ] 普通 Issue 任务（override 为空串）：行为不变
- [ ] 重派 PR 订阅任务：env 同样带订阅场景变量
- [ ] 所有现有测试通过 + 新增测试覆盖四条路径

## 5. 测试设计（tests/test_scene_env.py 扩展）

- 路径一：PR 关联 Issue + 订阅命中——issue #5 绑定 Issue 场景（custom_fields 空），订阅场景带 `BUILD0007_PASSWORD`；`_inject_scene_env(..., scene_id_override=<订阅场景>)` 断言 `GRAPE_SCENE_ID` = 订阅场景、`BUILD0007_PASSWORD` 非空
- 路径二：PR 无关联 Issue（伪 id）——`issue_id="#pr_88_<订阅场景>"` + override 同值：两口径结果一致
- 路径三：普通 Issue 任务——override 不传/空串：按 Issue 场景解析（现有行为断言不回归）
- 路径四：重派——`build_task_env(..., scene_id_override=<订阅场景>)` 断言 env 带订阅场景变量；同时覆盖 events.py redispatch 传参链
