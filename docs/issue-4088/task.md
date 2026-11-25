# Issue #4088 任务拆解

1. `test(#4088)`：先写失败测试——helper 窗口/并发、pipeline 短路回显、
   redispatch 跨 seq 拒绝（红）。
2. `feat(#4088)`：数据层——`comment_dispatch_guard` DDL（db_setup + kernel/db
   注册）+ event_store helper（claim/hit/release-by-seq/redispatch 判定/TTL
   清理含运行中 task 保护）。
3. `feat(#4088)`：派发接入——run_pipeline 评论分支接管守门之后先查后派 + 出参
   回显；接管跳过释放守卫；redispatch 跨 seq 守卫（共用 `_COMMENT_EVENT_TYPES`
   常量）；周期清理挂载。
4. `docs(#4088)`：spec/plan/task 文档。

验证：`python3 -m pytest tests/runtime/test_issue_4088_comment_dispatch_idempotency.py -q`
