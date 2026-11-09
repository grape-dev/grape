# Spec: 智能推荐入口判定换用真实绑定查询（Issue #681）

> 来源：Issue #681（VOD/issue Agent 收敛，评级 ready）｜目标分支 weekly-08-mys｜2026-11-02

## Objective
修复智能推荐入口判定被 `get_issue_scene` 的 dev_scene 回退短路的问题：配了 `managed_repos.dev_scene` 的仓库（grape-dev/grape、grape-dev/grape-skills）所有新 issue 被判定为"已有场景"，整体跳过智能场景推荐。修复后仅真实绑定跳过推荐，dev_scene 仅作展示兜底。

## Scope
- In：
  - `session_store.py` 新增 `get_issue_bound_scene(repo_key, issue_id) -> tuple`：只查 `issue_state.scene_id` → `issue 表 scene_id` 两层真实绑定，不回退 dev_scene；返回 `("", 0)` 表示应走智能推荐；docstring 注明推荐入口专用。
  - `event_pipeline.py` issue_opened 推荐入口判定（原 1890-1891）换用 `get_issue_bound_scene`。
  - `event_pipeline.py` L2184 场景解析保持 `get_issue_scene` 原样（Task Center 建单派发等需 dev_scene 兜底）。
  - worker `_handle_vod_recommend_complete` 核查：无"是否已有场景"同款判断（直接解析推荐结果写回），无需改动。
  - 新增 `tests/test_issue_bound_scene.py` 三态测试 + issue-415 回归。
- Out：不改动 `get_issue_scene` 的 dev_scene 第三层回退（57 处调用方依赖展示/A2A 语义）；不改变 dev_scene 为跳过推荐的强绑定。

## Acceptance Criteria
- [x] 无真实绑定 + 有 dev_scene → 走推荐（#681 核心修复）
- [x] 有真实绑定（issue 表 / issue_state）→ 跳过推荐（Task Center 行为不变）
- [x] 无绑定 + 无 dev_scene → 走推荐（未配 dev_scene 的仓行为不变）
- [x] issue-415 展示回退回归（get_issue_scene 仍回退 dev_scene）
- [x] test_bot_opened_recommend.py（#565）回归通过
