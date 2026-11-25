# Spec: Issue #3444 — 评论→Issue 事件内容去重与幂等派发

## Objective
- 构建什么：在「评论 → Issue 处理事件」派发路径上按「同 Issue + 评论内容哈希 + 同秒/参数化时间窗」判重。仅首个事件进入 processing 并派发任务；后到事件标记 `skipped`（reason=`duplicate_comment_content`）并留痕，事件详情可查到去重依据（源评论 id / 原事件 seq）。
- 为什么：用户反馈（编号已脱敏）——同一评论重复落库后各自触发 Issue 处理事件（两个事件序号并行，编号已脱敏），产生重复 Spec 与重复 A2A 交办风险。现有幂等键仅为 `comment:<评论 id>`，不同评论 id 的同内容评论无法判重。
- 成功标准：见 Acceptance Criteria（含内容不同、跨时间窗的负面用例）。

## Scope
### In Scope
1. 评论→Issue 处理事件派发层判重：同 repo + 同 Issue + 同评论内容 + 邻近时间窗（默认 1 秒，环境变量可配）→ 后到事件不派发任务。
2. 去重留痕：后到事件以 `events.status='skipped'` 落一条留痕事件，summary 带「源评论 id / 原事件 seq」，并写 `event_skipped`（reason=`duplicate_comment_content`）。
3. 同评论 id 的重复投递保持既有 `comment:<id>` 幂等语义（reason=`duplicate_comment_delivery`）。
4. 留痕事件与节点状态聚合隔离：`skipped`/`deduped` 事件不参与「每 agent 最新事件」判定（展示层、批量状态查询、auto-finalize / 审核点检测同一口径），避免重复评论覆盖真实 running/completed。
5. 新增正向与负面测试（内容不同、跨时间窗不误去重、留痕不阻塞 auto-finalize）。

### Out of Scope
- runbook 文档侧幂等判定示例：属 grape-skills 技能资产，由 #3058 跟踪，本单不重复实现。
- A2A 派发幂等（#1770 `a2a_dispatch_guard` 已覆盖）、vod runbook A2A 去重规则（#3171）、issue-cli 重复 A2A 判定增强（#3001）。

## Technical Approach
### 方案概述
事件指纹 = repo + issue_id + event_type + 评论内容哈希；判重窗口 = 首条事件 payload 内记录的时间戳与当前时间之差，默认 1 秒（`GRAPE_COMMENT_DEDUP_WINDOW_SECONDS`）。命中即短路派发，写一条 `skipped` 留痕事件并记 `event_skipped`。

### 实现路径
1. `src/domains/runtime/event_store.py`
   - `comment_dedup_window_seconds()`：读取窗口配置，默认 1 秒；`0` 关闭。
   - `find_recent_duplicate_comment()`：按 repo/issue/event_type 查近期评论事件，解析 `payload._dedup` 中的内容哈希与时间戳，返回命中的原事件 seq 与源评论 id；跳过 `skipped/deduped` 留痕行。
   - `append_comment_dedup_event()`：插入 `status='skipped'` 留痕事件（payload 仅存 `_dedup` 依据，summary 含源评论 id / 原事件 seq），并写 `event_skipped`。
2. `src/domains/runtime/pipeline/event_pipeline.py`
   - 评论族（`issue_comment` / `note_hook_pr`）落库前，在同一 `db_lock` 临界区内执行「判重 + 落库」，消除同进程并发 TOCTOU。
   - 同评论 id 命中优先走既有 `comment:<id>` 幂等（保持 `duplicate_comment_delivery`），不同评论 id 的同内容同窗才走内容去重。
   - 新建事件 payload 写入 `_dedup {content_hash, comment_id, ts}` 作为后续判重依据。
3. `src/api/pipeline_status.py`：`EVENT_STATUS_MAP` 增加 `skipped`/`deduped → STATUS_SKIPPED`（保留事件状态映射，避免未知状态按 pending 渲染）；`DEDUP_LEFTOVER_STATUSES` / `is_dedup_leftover_event` 作为留痕判定单一来源，`compute_node_statuses` 聚合前排除留痕。
4. `src/domains/runtime/issues_center.py`：`_batch_fetch_agent_statuses` 与 `_issue_latest_event_status_by_agent`（auto-finalize / `_review_checkpoint_invalidated` 共用）按同一状态集排除留痕，SQL 内外层 `MAX(seq)` 均过滤，兼容 `issue_id` 带/不带 `#` 两种历史形态。
5. `tests/runtime/`：去重正向/负面用例 + auto-finalize 回归（末节点真实 completed + 后到 skipped 留痕仍推进 pending_review）。

### 关键设计决策
| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 判重键 | issue+内容哈希+时间窗 / 仅评论 id | 前者 | 重复落库时评论 id 不同（两个评论 id，已脱敏），必须按内容判重 |
| 时间窗 | 严格同秒 / 邻近窗 | 邻近窗默认 1 秒，参数化 | 反馈证据为同秒；参数化并支持关闭，防误伤正常连续评论 |
| 后到事件 | 静默丢弃 / 标记留痕 | 落 `skipped` 事件 + `event_skipped` | 验收要求事件详情可审计去重依据 |
| 并发 | 先查后写 / 同锁内查+写 | 同 `db_lock` 临界区 | 复用仓库既有 dedup+insert 原子模式，避免同进程竞态 |

## Affected Modules
| 模块 | 改动类型 | 说明 |
|------|---------|------|
| `src/domains/runtime/event_store.py` | 修改 | 判重查询、窗口配置、留痕事件写入 |
| `src/domains/runtime/pipeline/event_pipeline.py` | 修改 | 评论落库前判重短路 + 指纹写入 |
| `src/api/pipeline_status.py` | 修改 | skipped/deduped 状态映射 + 留痕判定单一来源 |
| `src/domains/runtime/issues_center.py` | 修改 | 批量状态与 auto-finalize 最新事件查询排除留痕 |
| `tests/runtime/` | 新增 | 正向/负面用例 + auto-finalize 回归 |

依赖关系：事件存储 → 判重 → 派发为仓内既有调用链，仅插入判重环节；无新增外部依赖。

## Acceptance Criteria
- [ ] 同一 Issue 连续提交两条内容完全相同、同秒的评论，仅首个事件进入处理链路；后到事件落 `skipped` 且不派发任务。
- [ ] 已发布 Spec 或已 A2A 的 Issue 再次收到同内容同秒评论时，不重复发布 Spec、不重复 A2A（后到事件被短路）。
- [ ] 去重事件在事件详情可查到去重依据（summary 含源评论 id / 原事件 seq）。
- [ ] 负面用例：内容不同、或跨时间窗的正常连续评论不被误去重。
- [ ] 同评论 id 重复投递保持 `duplicate_comment_delivery` 既有语义。
- [ ] 留痕不参与节点聚合：节点仅有 skipped 留痕时维持 pending；末节点真实 completed 后到达 skipped 留痕（seq 更大）仍能推进 pending_review（issue_id 带/不带 `#` 两种形态）。
- [ ] 所有现有测试通过。

## Risks & Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| 判重窗过宽误吞正常连续评论 | Med | 默认 1 秒并参数化；负面用例守门；`0` 可关闭 |
| 同秒并发落库竞态 | Med | 判重与落库在同一 `db_lock` 临界区完成 |
| 留痕事件污染流水线/自动结单状态 | High | 聚合前排除 `skipped`/`deduped`（展示层、批量查询、auto-finalize 最新事件查询同口径）；仅有留痕的节点回落 pending，不渲染成 skipped |
| 派发关键路径新增查询开销 | Low | 按 (repo, issue) 索引取近期评论事件，窗口外直接放行 |

## Open Questions
- 无