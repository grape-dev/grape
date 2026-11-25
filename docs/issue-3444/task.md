# Tasks: Issue #3444

- [ ] T1 `event_store`：窗口配置 + 判重查询 + skipped 留痕事件写入
- [ ] T2 `event_pipeline`：评论落库同锁判重短路，payload 写入 `_dedup` 指纹
- [ ] T3 `pipeline_status`：`skipped`/`deduped` → `STATUS_SKIPPED`，留痕判定单一来源
- [ ] T3b `issues_center`：批量状态 + auto-finalize 最新事件查询排除留痕（复用常量，兼容带/不带 `#`）
- [ ] T4 测试：正向同内容不同 id 去重 + 留痕依据
- [ ] T5 测试：负面内容不同 / 跨时间窗不误去重
- [ ] T6 测试：同 id 幂等回归、窗口关闭放行
- [ ] T6b 测试：auto-finalize 回归（末节点 completed + 新到 skipped 留痕仍推进 pending_review；仅留痕节点维持 pending）
- [ ] T7 运行 `tests/runtime/test_issue_3444_comment_content_dedup.py` 与评论去重既有回归
- [ ] T8 自审查 + pre-pr 增量扫描 + 建 PR