# Task: Issue #496

- [x] T1 日志取证：grape events detail seq 29249/28353/15625/14741/13993/13159/13104 → 根因归类（4/7 派发级引擎离线即时终态、1 业务性部署冲突、1 执行级空输出(#403 已覆盖)、1 历史僵尸）
- [x] T2 失败测试先行：tests/test_issue_496_pr_merged_sre_retry.py（RED：ModuleNotFoundError 确认）
- [x] T3 实现：src/api/pr_merged_sre_retry.py（退避轮询 + 复位重派 + fail-close 收口）+ event_pipeline.py L2298 离线分支接入（仅 pull_request_merged+sre）
- [x] T4 回归：新测试 6/6 通过；#259 / 重派 / transient_retry / 事件守卫 / #426 / #404 等 106 项通过（test_pipeline.py / test_pipeline_issue182.py 各 6 项失败为基线 pre-existing，与本改动无关，已对照验证）
- [x] T5 自审查 + pre-pr 检查 + 建 PR（base weekly-08-mys）+ 评论 + A2A QA
