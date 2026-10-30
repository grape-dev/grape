# Spec: Issue #565 — bot 创建的 Issue 触发智能推荐流水线（开发版）

> 复用 Issue #565 既有 Spec（Issue Agent 发布，评级 ready）。目标分支：weekly-08-mys。

## Objective
- bot 创建的 Issue 的 `issue_opened` 事件不再被自循环检测拦截，放行进入事件流水线触发
  智能推荐（VOD Agent 判定场景写 scene_id），与人工建单一致。
- 加仓级滑动窗口限流：同 repo_key 60s 内最多 5 条 bot 开单推荐，超限记
  `event_skipped(reason=bot_open_recommend_rate_limited)`（进程内 dict，不进 DB）。

## Scope
- In：
  - `src/api/event_pipeline.py` 自循环检测放行 ×2（主路径 / fallback 路径）：bot actor 的
    issue_opened 一律放行，与「## 父任务」豁免叠加；放行时打 `evt["_bot_opened"]=True`。
  - 仓级滑动窗口限流：放行后（issue_opened 台账检查之后）、路由判定前；「## 父任务」
    子任务开单不占配额。
  - 执行棒短路：核实推荐完成回调确会派发执行棒（`_redispatch_vod_after_recommend`
    重派 VOD 执行流；direct 路径 event_pipeline + worker 路径 service 双入口）——
    bot 建单以 `evt["_bot_opened"]` 短路，只做场景判定不进执行棒。worker 侧 evt 为重建
    对象，按 issue 首事件 actor（建单人）是否 bot 还原标记。
  - 新增 `tests/test_bot_opened_recommend.py`（5 条用例）。
- Out：issue_comment / note_hook_pr 评论族 bot 拦截（防死循环最后闸门，不动）；
  pull_request_* 预匹配、子任务豁免、get_issue_scene / dev_scene 兜底链均不动。

## Acceptance Criteria
1. bot actor 的 issue_opened → 不进 event_skipped(self_loop)、派发 vod 推荐任务、
   不派 issue/coding 执行棒、issue 表 scene_id 非空
2. issue 卡已有场景的 bot 开单 → 跳过推荐（不重复判定）
3. 60s 内第 6 条 bot 开单 → event_skipped 记 bot_open_recommend_rate_limited
4. bot 的 issue_comment → 仍被 self_loop 拦截（回归保护）
5. 人建 issue_opened → 行为与现状完全一致（回归）
6. 所有现有测试通过（新增测试覆盖新功能）；真机验证另行安排
