# Plan: Issue #655 — 场景动态 agent 允许集

## 方案（前序 Spec 方案 1）

在 `src/api/routes/issues.py` 新增辅助函数 `_scene_agent_allowlist(repo_key, scene_id)`：

- 读取 issue 绑定场景（`devops_pipeline_scene`，复用 `get_pipeline_config`，repo 作用域优先）；
- 返回该场景 `nodes[].id` 集合；
- 兜底（返回空集，仅原白名单生效）：scene_id 为空 / `"default"`、场景解析失败（异常、DB 未命中 fallback 标记）。

## 改动点

| 位置 | 改动 |
|------|------|
| `issues.py` `_scene_agent_allowlist()` | 新增：场景节点集合解析 + fallback |
| `issues.py` awaiting-input（~L1213） | 校验移到 issue 行加载后：`session_agent in (场景节点集 ∪ {vod, issue})`；错误文案动态化 |
| `issues.py` `_session_devops_issue()`（~L1281） | `_agent in _DEV_OPS_AGENTS ∪ 场景节点集`；错误文案动态化 |
| 路由 description | 同步说明「场景动态允许集 + 原白名单兜底」 |

## 实施顺序（TDD）

1. RED：新增 `tests/test_issue_655_scene_dynamic_agents.py`（自定义 agent 200 / fallback 403 / 原角色兼容）
2. GREEN：实现 helper + 两处校验改造
3. REFACTOR：文案与 description 收敛
4. 回归：`test_issue_pending_review.py`、`test_issues_awaiting.py` 全量通过

## 风险

- 场景查询失败 → 捕获异常回退白名单并 log.warning（不误放行）
- 动态集合过宽 → 仅限当前 Issue 绑定场景的节点集合（不跨场景）
