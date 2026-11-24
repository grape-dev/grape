# Issue #4065 — 方案设计（plan）

## 1. 归一单点：`src/kernel/issue_ident.py`

已有的 `normalize_issue_ident` 只回答「是不是平台原生 ident」（数字 / 大写 `IK*`），
对 `autopilot-<字母>` 合成键判非法。本单补两套口径，全部落在内核唯一入口，禁止业务侧再写 `isdigit`：

| 函数 | 口径 | 用途 |
|------|------|------|
| `deliverable_comment_ident(value, provider)` | 可回帖：数字平台只收纯数字；`gitee` 额外收平台原生大写 ident（其 `notes` 路由接受字符串）；其余 → `""` | 评论投递 / 预检 |
| `normalize_card_ident(value)` | 可作卡片主键：数字、平台原生 ident、`autopilot-*` 合成键；`pr_*`/`pr-*` 订阅伪 id、空、含分隔文本、超长 → `""` | 卡片端点 / session_store |
| `card_ident_number(value)` | 卡片主键原生形态：数字 → `int`（#995 契约不变），ident/合成键 → `str`，无法归一 → `None` | `_issue_num_or_ident` |

`autopilot-*` 之所以允许作卡片主键：`issue.issue_number` 是 TEXT 亲和列（`db_setup._normalize_issue_number_text`），
`autopilot-<字母>` 就是该表真实主键；把它们判非法等于把卡片链路一起打死。
`pr_*` 是 PR 订阅伪 id，永不对应卡片，一律拒绝。

## 2. 投递链（`event_pipeline.py`）

### 2.1 归一 + 短路

```
_post_result_to_issue
  ├─ _deliverable_ident(issue_number, repo_key)      # 归一
  │    ├─ 有效 → 继续
  │    └─ 无效 → 尝试任务真实号 _task_real_issue_number(task_id, seq, repo_key)
  │              ├─ 有 → 用真实号继续（双保险）
  │              └─ 无 → RESULT_COMMENT_SKIPPED + RESULT_CARD_FALLBACK → return False
  ├─ _list_issue_comments_card(...)                 # 预检：归一后再请求，非法直接 []
  └─ _post_issue_comment_ledgered(...)              # 投递：归一后再请求，非法直接 skipped
```

`_post_issue_comment_ledgered` / `_list_issue_comments_card` 自身也做归一（纵深防御）：
即便上游忘了前置校验，也绝不把非数字 ident 拼进远程 URL。三处共用同一 helper，口径不分叉。

### 2.2 真实号双保险（scope 4）

`_task_real_issue_number(task_id, seq, repo_key)`：读 `agent_tasks.issue_id/event_seq` → 读 `events.issue_id/payload`
（`issue_number` / `issue.number` / `number`），取第一个**可投递**的候选。
合成键任务（autopilot）自然取不到 → 走卡片/事件流兜底；带真实号的任务（webhook/subtask）能凭真实号回帖。
任务记录与卡片主键分离：`issue_id` 保持统计分组键语义不变，真实号只在投递时按需解析。

### 2.3 结果路由与留痕

- 有效号 → GitCode/Gitee 评论（原链路）；
- 无有效号 → 评论不发，`agent_tasks.errors` 追加 `{"error_code": "RESULT_CARD_FALLBACK", "issue_ident", "conclusion"(截断), "at"}`，
  结果正文已在 `events.result`（`update_event_complete`）里，看板/事件流可读；
- 跳过与失败都写结构化行：`RESULT_COMMENT_SKIPPED reason=<enum> ident=<ident> repo=<rk> task_id=<id>`。

## 3. 可观测（scope 3）

新增 `src/kernel/comment_metrics.py`（与 `org_metrics` 同款「可安装收集器 + 无副作用 observe」）：

- `grape_comment_post_failed_total{reason}` Counter，reason 限枚举 `non_numeric_ident|http_4xx|http_5xx|network|unknown`；
- `grape_comment_post_skipped_total{reason}` Counter（跳过与失败分开，运维含义不同）；
- `grape_comment_post_consecutive_failures{repo_key}` Gauge + 进程内连续计数；
  达阈值（`GRAPE_COMMENT_POST_FAIL_ALERT_THRESHOLD`，默认 5，夹到 1..1000）打一条 ERROR 结构化告警行
  `RESULT_COMMENT_ALERT consecutive=N threshold=N reason=...`，成功后清零；
- 注册点：`src/domains/insight/observability/__init__.py`（`install(registry)`，与 `org_metrics` 同址）；
- 告警规则：`deploy/alerts.yml` 两条（失败增量 > 0 / 连续失败 ≥ 阈值）。

worker 进程不暴露 `/metrics`（既有拓扑不变）：worker 侧指标在进程内聚合，连续失败告警走 ERROR 日志行（Loki 可检索），
主控侧同时有 Counter 与日志，两条通道都不断。

## 4. 卡片端点（scope 5）

`GET /internal/issues/card` 签名 `issue_number: int → str`：

```
raw --normalize_card_ident--> ident
  ├─ ident 非空 → native_issue_number(ident) → 既有 look-up（数字→int 行为不变）
  └─ ident 空   → 400 {"code":400,"error":"invalid_issue_ident","message":"issue_number 需为数字/平台 ident/autopilot-* 合成键（收到 'pr_123'）"}
```

`src/api/session_store._issue_num_or_ident` 改用 `card_ident_number`：`pr_*`/垃圾 → `None`（不再打 HTTP），
`autopilot-*` 保持可查卡片 → 244 条/小时 422 的来源直接消失。

## 5. 测试 seam

| seam | 测试方式 |
|------|---------|
| `deliverable_comment_ident` / `normalize_card_ident` / `card_ident_number` | 纯函数矩阵 |
| `_post_issue_comment_ledgered` / `_list_issue_comments_card` | monkeypatch `create_client` → 断言**未发起** `_request`（零远程 400） |
| `_post_result_to_issue` | monkeypatch 两个底层投递函数 + 读 `agent_tasks.errors` 断言 `RESULT_COMMENT_SKIPPED` / `RESULT_CARD_FALLBACK` |
| `/internal/issues/card` | FastAPI `TestClient` 直接打端点：`autopilot-as`/`IKX1DL` 可读、`pr_123` 400 |
| 指标 | `install(CollectorRegistry())` 后 `observe_failure` 断言 `{reason}` 聚合与连续失败告警行 |

## 6. 提交切分（TDD 红/绿）

1. `test(#4065)` —— 三类 ident 归一 + 投递零远程 + 卡片端点可读的失败测试（红）
2. `feat(#4065)` —— kernel 归一口径 + 投递/预检短路 + 路由兜底（绿）
3. `feat(#4065)` —— 投递指标与连续失败告警 + 卡片端点归一（绿）
4. `docs(#4065)` —— spec/plan/task（可与实现合并提交）