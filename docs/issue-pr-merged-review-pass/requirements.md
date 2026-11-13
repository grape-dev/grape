# 需求：PR 合并应视同审核通过，自动退出需求「待审核」状态

- 基线：`origin/weekly-10`（`972a88af4`）
- 缺陷类型：状态机断链（用户提前合并 PR 后，需求卡片永久停在待审核列）

## 背景

### 现象

需求卡片停在「待审核」列。用户在待审核阶段**直接合并 PR**，触发 SRE 部署；SRE 部署完成后卡片仍留在待审核列，右上角标签显示 SRE，状态不再变化，也不会进入「已完成」。

### 已核实根因（基线 `origin/weekly-10` 逐函数核对，行号仅参考、一律以函数名锚定）

1. **看板列与卡片标签天然解耦**：看板列按 `issue.status` 分组，卡片标签按 `issue.stage` 显示（前端 `issues.js`）。
2. **PR merged 推进 stage 但不动 status**：`src/api/event_pipeline.py` 的 `pull_request_merged` + `agent_type='sre'` 分支里，仅当 `stage == 'qa'` 时执行 `set_issue_stage(repo_key, issue, "sre", "sre")`。
3. **`set_issue_stage` 同步 status 时排除 pending_review**：`src/api/session_store.py::set_issue_stage` 的
   `UPDATE issue SET status=? WHERE ... AND status NOT IN ('completed','cancelled','awaiting_input','pending_review')`
   —— 于是 `stage` 变成 `sre`，`status` 仍是 `pending_review`。
4. **能把 pending_review 翻回 processing 的只有用户评论**：`_resume_issue_waiting`（`src/api/issues_center.py`）只被 `event_pipeline` 的 `issue_comment` 路径调用；合并 PR 不触发，SRE 技能侧也没有 review-pass 命令。
5. **SRE 末节点完成时自动结单被 pending_review 拦下**：`src/api/issues_center.py::auto_finalize_issue_on_last_node` 命中 `status == 'pending_review'` 直接 `return "noop:pending_review"`，既不结单也不推进 `stage=done`。
6. 结果：卡片 `stage=sre`、`status=pending_review` → 永久停在待审核列。

### 关键事实（避免误改）

- `get_issue_stage()` 返回结构里**没有 `status`**（只读 stage/owner/assignee/scene 等），要判 status 必须另读一次。
- `_resume_issue_waiting(repo_key, issue_number) -> bool` 语义：仅当 `status IN ('awaiting_input','pending_review')` 时置 `processing`，返回是否真正翻转（幂等）。
- `set_issue_stage` 的 `NOT IN (...)` 是**全局保护**（防 stage 推进洗掉 awaiting_input / 人工确认终态），不能为了本单把它去掉。
- `auto_finalize_issue_on_last_node` 的 `pending_review → noop` 是 Issue #51 人工确认语义，本单不改。

## 改动范围

严格 3 组共 6 项；多改一处即打回。

### A 组：状态读取（1 项）

- A1 `src/api/session_store.py` 新增只读 helper `get_issue_status(repo_key: str, issue_id: str) -> str`：`SELECT status FROM issue WHERE repo_key=? AND issue_number=?`，无行返回 `""`；异常 `log.warning` 并返回 `""`。**不改 `get_issue_stage` 的返回结构**，不新增列。

### B 组：PR merged 视同审核通过（3 项）

- B1 在 `event_pipeline.py` 的 `pull_request_merged` + `agent_type == 'sre'` 分支内、`set_issue_stage(..., "sre", "sre")` 之前插入：当 `stage == 'qa'` **且** `get_issue_status(...) == 'pending_review'` 时，调用 `_resume_issue_waiting(repo_key, int(issue_num))`；成功后 `log.info` 一行说明「PR merged 视同审核通过，pending_review→processing」。
- B2 该调用必须 `try/except` 包裹：异常只 `log.warning`，**不得阻断**后续 `set_issue_stage(..., 'sre', 'sre')` 与 SRE 派发；`_resume_issue_waiting` 返回 False（已被恢复/状态不符）也照常继续推进 stage。
- B3 作用范围仅限该分支：`stage != 'qa'`、`agent_type != 'sre'`、`status != 'pending_review'`（含 `awaiting_input`）一律不触发恢复；不新增「review-pass」命令、不改 SRE 技能。

### C 组：测试（2 项）

- C1 新增 `tests/test_pr_merged_review_resume.py`（sqlite 内存库 + webhook 载荷构造，不调远端）：
  1. 构造 `stage=qa, status=pending_review` → 投递 `pull_request_merged`（sre）→ 断言 `status=processing`、`stage=sre`、`_resume_issue_waiting` 被调用 1 次；
  2. 幂等：同一事件重复投递 → 第二次 `_resume_issue_waiting` 返回 False，`status/stage` 不再变化、无异常日志升级；
  3. 回归：`stage=qa, status=awaiting_input` → **不**恢复（status 仍 awaiting_input），stage 仍推进 sre；
  4. 回归：`stage=coding, status=pending_review` → 既不恢复也不推进（该分支不命中）；
  5. 失败降级：stub `_resume_issue_waiting` 抛异常 → 仍完成 `stage=sre`，仅一条 warning。
- C2 回归：`auto_finalize_issue_on_last_node` 在 `status=pending_review` 时仍返回 `noop:pending_review`（语义不变）；`status=processing` + 全节点 completed → `auto_done`（原有路径不受影响）。

## 明确禁止

1. **不得**从 `set_issue_stage` 的 `status NOT IN (...)` 里删掉 `pending_review`（全局放开会误伤其它阶段的人工确认语义）。
2. **不得**改 `auto_finalize_issue_on_last_node` 的 `pending_review → noop` 分支。
3. 不得改前端 `issues.js` 的列分组/标签逻辑、不改看板排序。
4. 不得改 SRE 技能/runbook、不得新增 CLI 命令、不改 `_resume_issue_waiting` 既有语义（awaiting_input 同样可恢复这条保持）。
5. 不得改 `get_issue_stage` 返回结构、不新增表/列、不改 HTTP 接口响应。
6. 不新增依赖、不顺手重构、不改无关日志级别。
7. **不 push、不建 MR、不部署、不重启服务**；只提交到本地工作分支并标注「待人工审批」。

## 验收标准

1. 静态核对：`git diff` 只含 `src/api/session_store.py`、`src/api/event_pipeline.py`、新增测试文件。
2. 构造 `status=pending_review, stage=qa` 的需求，投递 `pull_request_merged`（sre）→ 结束后 `status=processing`、`stage=sre`（C1-1）。
3. SRE 任务完成且流水线各节点均 `completed` → `auto_finalize` 自动结单：`status=completed`、`stage=done`，卡片进入已完成列（C2 + 手动一次端到端）。
4. 幂等：同事件重复投递两次 → 状态不变更、无重复恢复日志（C1-2）。
5. 回归：`awaiting_input` / `stage` 非 `qa` / `agent_type` 非 `sre` 三种情况行为与改动前逐字节一致（C1-3/4）。
6. PR merged 的 SRE 派发不因恢复失败而丢失（C1-5 断言事件仍落到 sre）。
7. 无表结构变更、无接口契约变更（列清单对拍）。

## 参考资料

- 分支锚点：`event_pipeline.py` 的「PR merged 跳过 QA 直接路由 SRE」块（`_resolve_event_issue_id` → `get_issue_stage` → `stage == 'qa'` → `set_issue_stage('sre','sre')`）。
- 恢复锚点：`issues_center.py::_resume_issue_waiting`（`UPDATE ... WHERE status IN ('awaiting_input','pending_review')`），现仅被 `event_comment` 路径调用。
- 结单锚点：`issues_center.py::auto_finalize_issue_on_last_node` 的 `noop:pending_review`。
- 规模预估：生产代码约 35–55 行（helper ~15、分支内判定 ~25、日志与异常 ~10），测试约 150–200 行。
- 测试口径：sqlite 内存库 + webhook payload 构造 + 打桩；不连生产、不触发真实 SRE。
- 影响面：仅 `pull_request_merged` → sre 的 `qa→sre` 推进点；表结构、接口、前端、SRE 技能均不变。