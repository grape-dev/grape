# Issue #1245 方案设计

## 核心流程
`precheck_scene` 读取场景配置（`get_pipeline_config`），先做结构校验，再逐节点做
四检；所有问题按 reason_code 汇总为 `problems[]`，任一问题即 `ok=false`。
配置关闭的 reason_code 不参与阻断，探测/查询异常统一 fail-open。

## 复用实现
- 场景配置：`src/api/pipeline_config.get_pipeline_config`。
- 引擎健康：`src/runtime/engine_select._health_ok` + `runtime_health_scores` 只读查询。
- 技能解析：`src/user/db.get_skill_by_id` / `resolve_skill_id`。
- webhook 失败：`event_pipeline._fail_event` / `_create_failed_task`，与
  `_fail_scene_all_offline` 同款终态路径。

## 拦截点
- API：放在 `if issue_num is None:` 内 assignee 预校验之后、附件拼装与
  `_post_gitcode_issue` 之前——此时尚未产生任何 VCS/DB 副作用。
- webhook：事件落库后、`pick_engine` 派发前；失败时置 failed、评论原因并 return。

## 配置
```json
{
  "enforce": true,
  "reasons": {
    "engine_missing": true,
    "engine_all_offline": true,
    "foreign_personal_engine": true,
    "skill_unresolvable": true,
    "scene_node_dangling": true,
    "scene_node_duplicate": true
  }
}
```
文件缺失/解析失败回落默认全开（`enforce=true` + 全部 reason 开启），即缺配置仍执行
校验，不因配置文件缺失静默跳过；查询/探测等内部异常逐一 fail-open 并记录 warning。

## 测试
使用仓库 sqlite 测试库构造 `agent_runtimes` / `runtime_credentials` /
`runtime_health_scores` 行，打桩场景配置；API 用例打桩 GitCode 建单函数验证零副作用。