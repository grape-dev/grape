# PR 评论去重豁免 + 评论 ID 幂等（requirements）

基线：`origin/weekly-10` @ `972a88af4`（2026-11-08）。以下行号仅作参考，一律以函数名/常量为锚点。

## 背景

1. `note_hook_pr`（PR 评论）当前与普通事件一样进入 `run_pipeline` 的 webhook 去重段（`event_pipeline.py` / `run_pipeline`，去重条件约 2683 行）：
   - ① `event_active_guard` 按 `(repo_key, issue_id, agent_type)` 原子抢占，已有未终态事件时直接 `return`，写 `event_skipped.reason = dedup:active_<agent>_for_#N`；
   - ② `find_recent_same_event(..., 60)` 按同 `(repo,issue,agent,event_type)` 60 秒窗口判重，写 `event_skipped.reason = dedup:recent_note_hook_pr_60s_for_<agent>`。
   两条都会让真人 PR 评论被静默丢弃（前端无 event_skipped 展示路径），且任务结束后也不会补投。
2. `issue_comment`（Issue 评论）已豁免上述整段去重，理由写在同处注释：评论是人操作、频率低，被「同 (issue,agent) 有 running 事件」挡掉会丢评论，改为始终建事件、由 `issue_agent_session` 串行排队。`note_hook_pr` 同为真人评论，口径不一致。
3. 8 小时误判重问题已在 `9b6ccf589` 修复（cutoff 改走 `time_utils.db_now_str`），本单不重复处理时间口径。本单解决剩余的「活跃守卫 / 60 秒窗」丢评论。
4. 若仅把 `note_hook_pr` 加入豁免，webhook 重投（GitCode/GitHub 重试、双通道、`create_task` 同步补发）可能重复建事件、重复派 agent。现 `events.idempotency_key` + 唯一索引 `(repo_key, idempotency_key)` 已存在（`db_setup.py` `_migrate_a2a_idempotency_key`），`append_event` 支持撞键返回负 seq（`event_store.py` `append_event`），但评论事件插入时未使用。

结论：评论族（`issue_comment` + `note_hook_pr`）统一「豁免时间窗/活跃守卫去重，改用评论内容 ID 幂等」，既不再丢真人评论，也不因 webhook 重投重复派工。

## 改动范围

严格 2 组共 4 项；多改一处即打回。

### A 组：评论族豁免 + 幂等（`src/api/event_pipeline.py`，仅 `run_pipeline` 去重段与事件落库分支）

- A1：新增模块级常量（放在 `run_pipeline` 之前、与 `PR_EVENT_TYPES` 同区）：
  `_COMMENT_EVENT_TYPES = ("issue_comment", "note_hook_pr")`。
- A2：去重段入口条件由 `event_type not in ("a2a_event", "issue_comment")` 改为
  `event_type not in (("a2a_event",) + _COMMENT_EVENT_TYPES)`；
  即 `note_hook_pr` 不再进入 ① `event_active_guard` 抢占、也不再进入 ② 60 秒同类型判重，直接走既有 `append_event` 落库分支。
- A3：评论族落库前计算幂等键并传给 `append_event(idempotency_key=...)`：
  - 取内容 ID：复用 `src/outbound_ledger.extract_event_content_id(provider, payload)`（GitCode Note Hook = `object_attributes.id`；GitHub = `comment.id`），`provider` 由 `parse_repo_key(repo_key)[0]` 取得；
  - 键格式：`comment:<内容ID>`（前缀固定，禁止与 A2A 既有键格式混用）；
  - 内容 ID 为空（平台未给）时 `idempotency_key=""`，退回当前行为，不得自造键（禁止用标题/正文哈希/时间戳兜底）。
- A4：`append_event` 返回负 seq（撞幂等键）时：不 `return` 成功、不再派发，写
  `append_skipped_event(repo_key, event_type, "duplicate_comment_delivery", detail="<内容ID>", issue_id=...)`，
  并令 `result["skipped"]="duplicate_comment_delivery"`；返回结构与既有 skipped 分支一致。

### B 组：回归测试（新增 `tests/test_comment_event_dedup_exemption.py`）

- B1：新增该测试文件，使用 `DEVOPS_DB_BACKEND=sqlite` + 临时库路径（沿用 `tests/test_dedup_window_time_basis.py` 的初始化方式），不连 MySQL、不压生产。
- B2：只允许新增该测试文件，不改既有测试的断言与数据。

## 明确禁止

1. 不得改动非评论事件（`issue_opened` / `issue_updated` / `pull_request_*` / `push` / a2a 等）的去重语义与 60 秒窗口。
2. 不得修改 `event_active_guard` 表结构、TTL（`purge_stale_event_guards` 的 1800s）、释放逻辑。
3. 不得修改 `events.idempotency_key` 列、唯一索引、`append_event` 的 A2A 键格式与撞键返回负 seq 的契约。
4. 不得改动上游过滤：bot actor `self_loop`、`<!-- grape-auto-comment -->` marker、outbound ledger（`is_our_outbound`）。
5. 不得改 `time_utils.db_now_str` / `find_recent_same_event` / dashboard 时间口径（已由 `9b6ccf589` 收敛）。
6. 不得改前端、接口响应结构、日志级别；不得顺手重构或抽公共函数。
7. 不得改 `issue_comment` 恢复/指令解析、`note_hook_pr` 阶段矩阵映射（`_STAGE_EVENT_AGENT_MAP`）。
8. 范围外发现只写报告，不顺手修。

## 验收标准

1. 静态：`event_pipeline.py` 中 `_COMMENT_EVENT_TYPES` 恰出现（定义 1 次 + 引用 1 次）；去重入口条件不再出现字面量 `("a2a_event", "issue_comment")`；`comment:` 前缀键在评论族落库路径出现且仅此一处。
2. T1（核心）：预置 `(repo, issue, agent=coding)` 一条未终态事件（模拟 agent 正在干活），再投 `note_hook_pr` 同类事件 → 断言新建事件成功、`status=pending`、`skip_reason` 为空、`event_active_guard` 中该 `(repo,issue,coding)` 无新增行。
3. T2：同一 `(repo,issue,agent)` 投两条**不同评论 ID** 的 `note_hook_pr`（间隔 0s 与 30s 各一）→ 断言两条事件都落库（seq 不同）。
4. T3（重投）：同一 payload（同 `object_attributes.id`）连续投两次 → 断言仅 1 条事件，第二次返回 `skipped=duplicate_comment_delivery` 且 `event_skipped` 中该 reason 恰 1 条。
5. T4：`issue_comment` 同评论 ID 重投两次 → 同样仅 1 条事件 + `duplicate_comment_delivery` 留痕；不同评论 ID 两条都落库（不回归 #453 评论恢复路径）。
6. T5（不回归）：`issue_opened` 同 60 秒内两次 → 仍只 1 条事件（`duplicate_recent_window` 或 `duplicate_active_task`），证明豁免未外溢。
7. T6（不回归）：bot actor 的 `note_hook_pr` → 仍在上游被拦为 `self_loop`，不因本单进入派发。
8. T7：`pytest tests/test_comment_event_dedup_exemption.py tests/test_dedup_window_time_basis.py tests/test_issue_426_pr_sub_binding.py -q` 全绿（最后一组若环境缺依赖可注明 skipped，不得删用例）。
9. 交付物：只提交本地分支，diff 文件清单必须恰为 `src/api/event_pipeline.py` + `tests/test_comment_event_dedup_exemption.py`；多一个源文件即打回。

## 参考资料

- 现存保护：`extract_event_content_id`（`src/outbound_ledger.py:284`，GitCode=object_attributes.id）；`append_event` 撞键契约（`src/api/event_store.py:61-145`）；唯一索引（`src/api/db_setup.py` `_migrate_a2a_idempotency_key`）。
- 事件类型：`note_hook_pr` 属于 `ALL_PR_EVENTS`（`src/vcs/event_types.py:65`），进入 `PR_EVENT_TYPES` 订阅匹配（`src/api/routing.py:17`）。
- 规模预估：`event_pipeline.py` +8~15 行，测试文件新增约 120~180 行。
- 测试口径：sqlite 临时库；网络与 runtime 全部打桩，不触发真实 agent、不压 MySQL。