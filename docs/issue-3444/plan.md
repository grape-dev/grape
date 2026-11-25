# Plan: Issue #3444

1. `src/domains/runtime/event_store.py`
   - 新增常量 `COMMENT_DEDUP_DEFAULT_WINDOW_SECONDS = 1.0`。
   - `comment_dedup_window_seconds()`：读 `GRAPE_COMMENT_DEDUP_WINDOW_SECONDS`，非法值回落默认；`<=0` 视为关闭。
   - `_comment_dedup_now()`：判重时钟（可测试替换）。
   - `comment_content_fingerprint(note)`：评论内容 SHA1 摘要（16 hex）。
   - `find_recent_duplicate_comment(repo_key, issue_id, event_type, content_hash, window_seconds=None)`：按 (repo, issue, event_type) 取近 200 条非 `skipped/deduped` 事件，解析 `payload._dedup`，比较内容哈希与时间戳差，返回 `{seq, comment_id, ts}`。
   - `append_comment_dedup_event(...)`：插入 `status='skipped'`、`result='duplicate_comment_content'` 的留痕事件；payload 仅存 `_dedup` 依据；summary 写「重复评论已去重（源评论 id=…, 原事件 seq=…）」；写 `event_skipped`。
2. `src/domains/runtime/pipeline/event_pipeline.py`
   - `_comment_note_text(payload)` / `_comment_content_id(...)` 辅助提取评论正文与评论 id。
   - 评论族落库分支：在同一 `db_lock` 内先 `find_recent_duplicate_comment`，命中且评论 id 不同 → `append_comment_dedup_event` 并 `return`；否则 `append_event`，payload 注入 `_dedup{content_hash, comment_id, ts}`。
   - 同评论 id 命中仍交由 `append_event` 幂等键返回负 seq → `duplicate_comment_delivery`。
3. `src/api/pipeline_status.py`
   - `EVENT_STATUS_MAP` 增加 `skipped`、`deduped` → `STATUS_SKIPPED`（保留事件状态映射）。
   - `DEDUP_LEFTOVER_STATUSES` / `is_dedup_leftover_event` 作为留痕判定单一来源；`compute_node_statuses` 聚合前排除留痕（节点无真实事件时维持 pending）。
4. `src/domains/runtime/issues_center.py`
   - `_batch_fetch_agent_statuses`：排除 `DEDUP_LEFTOVER_STATUSES`（复用常量，去掉硬编码）。
   - `_issue_latest_event_status_by_agent`：SQL 内外层 `MAX(seq)` 均加 `status NOT IN (留痕)`，使 auto-finalize 与 `_review_checkpoint_invalidated` 不被新到留痕覆盖；覆盖 `issue_id` 带/不带 `#` 两种形态。
5. `tests/runtime/test_issue_3444_comment_content_dedup.py` + `tests/runtime/test_issue_656_auto_finalize.py`
   - 正向：同 Issue 同内容不同评论 id 同秒 → 仅一条正常事件 + 一条 skipped 留痕，summary 含源评论 id/原事件 seq，`event_skipped` 可查。
   - 负面：内容不同不误去重；跨时间窗同内容不误去重（替换判重时钟）。
   - 回归：同评论 id 重复投递仍为 `duplicate_comment_delivery`；同评论 id 不同内容、不同评论 id 不同内容均正常落库。
   - 关闭窗口（`GRAPE_COMMENT_DEDUP_WINDOW_SECONDS=0`）时同内容不同 id 均落库。
   - auto-finalize 回归：末节点真实 completed + 后到 skipped 留痕（seq 更大，带/不带 `#`）→ 仍推进 `pending_review`；仅有留痕时维持 `noop:node_pending`。
6. 文档 `docs/issue-3444/{spec,plan,task}.md`。