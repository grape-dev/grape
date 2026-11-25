# Task — Issue #4343 实现拆解

基线：arch-weekly-11-ljt ｜ 分支：feat/issue-4343

- [x] T0 代码取证：定位扇出点（`routes/issues.py` `_repo_keys_of`/`_vcs_issue_lookup`/
      `_sync_missing_issue_row`）、WARNING 三源（`git_repo_api.py:646`、
      `api_client.py:1115`、`token_resolver.py:632`）；确认 #3891 回落只改响应语义、
      与本改造不重叠
- [x] T1 api_client.py：ref 级 404 负缓存（TTL env 夹取 60–300s）+ error_code
      分类 + GET-404 告警降 DEBUG；TDD 先写用例
- [x] T2 token_resolver.py：GlobalBotToken._fetch 窗口聚合告警（300s 窗口 env 可调）
- [x] T3 event_pipeline.py `_vcs_get_issue`：token 解析前先查负缓存
- [x] T4 routes/issues.py：`_repo_keys_of` 前缀直选 + `_vcs_issue_lookup`
      前缀单探测 + 候选失败聚合 1 条告警
- [x] T5 `_detail_404` 补 Retry-After 头（= 负缓存 TTL）
- [x] T6 QA runbook 轮询指引：404 快速失败 + 指数退避（20s→40s→80s）+ 3 次终止
- [x] T7 回归 5 类用例 + 既有 3891/667/672 测试通过
- [x] T8 自审查（5 轴 + pre-pr 增量检查）→ 提交 → PR → 评论 → A2A QA
