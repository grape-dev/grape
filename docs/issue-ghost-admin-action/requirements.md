# 需求：修复管理端审计事件伪造 issue_id 导致流水线出现幽灵行

- 单号：待平台分配
- 基线：`origin/weekly-10`（`40be0820a`），本地 `weekly-mys` 已对齐该提交
- 关联会话结论：IM 分支工作之外单列的核心缺陷（非 IM 范围）

## 背景

### 现象

管理端对任一事件执行「重派 / 终止」后，流水线页面（`src/api/static/js/pages/pipeline.js`）会凭空多出一行：
Issue 显示为 `#<目标事件 seq>`，标题 `Admin redispatch: event #N`，五个 Agent 节点全灰，责任人为空，阶段停在 `vod`，时长为空。

以 `gitcode:chenjingjie/community-skill` 的 Issue #30 为例：该 Issue 自身事件（54888 / 54898 / 54903 / 55098 / 55199 / 55304 / 55388 / 55453）全部正确归并到同一行；页面额外出现一行 `#55388 / Admin redispatch: event #55388`，实际对应审计事件 `events.seq=55451`（actor=chenjingjie，2026-11-06 14:04:51，对事件 55388 执行 redispatch），并非任何真实 Issue。

### 已核实根因链（三段缺省值叠加，缺一条都不产生幽灵行）

1. **审计事件刻意不带业务归属**：`src/api/routes/events.py` 的 `_audit_admin_action` 调用 `append_event` 时传 `issue_id=""`，注释写明「不参与业务流转，仅留痕」。
2. **写入层强制补号**：`src/api/event_store.py` 的 `append_event` 要求 `issue_id` 非空，空值时按 `payload → result → summary → title` 顺序兜底，最终调用 `_extract_issue_id_from_text`（正则 `#(\d+)`）；审计事件标题为 `Admin {action}: event #{target_seq}`，抓到的正是目标事件序号，于是落库 `issue_id='#55388'`；完全解析不出时兜底写 `'#0'`。
3. **聚合层只看非空**：`src/api/routes/pipeline.py` 的 `_pipeline_window` 用 `issue_id != ''` 过滤并 `GROUP BY e.issue_id`；`get_pipeline` 装载事件同样只过滤非空；`_get_issue_pipeline` 对 `#55388` 在 `issue` 表查不到记录，标题回退为事件自身 title、`agent_type` 为空导致五节点匹配失败、状态全 0，前端即渲染该空行。

以上三处已在 `origin/weekly-10`（`40be0820a`）逐函数核实，与会话结论一致；调用点共三处：`events.py` 的 `terminate` / `redispatch`，以及 `src/api/routes/runtime.py` 中的 `_audit_admin_action` 调用。行号仅作参考，一律以函数名锚定。

### 现状数据（生产库实测，以预发复核为准，不得直接引用本单数字）

- `event_type='admin_action'` 共 1407 条，`issue_id` 全部指向不存在的 Issue，涉及 750 个不同「幽灵编号」。
- `issue_id='#0'` 共 737 条：`pull_request_merged` 421、`tag_created` 230、`release_created` 44、`skill_resolve_failed` 41、`a2a_event` 1。
- 幽灵行参与 `MAX(seq)` 排序，会挤占 `history_limit`（默认 50）名额，并污染 `today_count`、`history_total`、`failed_count` 口径。

## 改动范围

严格 4 组共 10 项；多改一处即打回。

### A 组：审计事件继承真实业务归属（3 项）

- A1 `src/api/routes/events.py::_audit_admin_action`：新增按 `(repo_key, target_seq)` 查询 `events` 表，取目标事件的真实 `repo_key` 与 `issue_id`，显式传给 `append_event`；查询不到（目标事件已不存在）时 `issue_id=""`，并在 `payload` 内写 `"related": false` 标记，不得回退成 `#target_seq`。
- A2 查询失败（DB 异常）必须 `try/except` 降级为无归属并 `log.warning`，不得影响 terminate / redispatch 主流程（现有「失败不影响主操作」约束保持）。
- A3 `terminate` 与 `redispatch` 两个调用点：`repo_key` 取目标事件行的 `repo_key`（现有 `row[1]` / `evt["repo_key"]` 逻辑保留），仅补齐传入，不改动重派业务逻辑本身。

### B 组：append_event 收紧兜底（2 项）

- B1 `src/api/event_store.py::append_event`：仅信任结构化来源——`payload.issue_number`、`payload.issue / pull_request / merge_request` 对象的 `number`（或 `iid`）、以及 `/(issues|pulls|merge_requests)/(\d+)` 链接；**裸 `#N` 不再作为编号来源**（`result` / `summary` / `title` 的 `#N` 一律不采信）。
- B2 无法解析时写 `issue_id=''`（不再写 `'#0'`）；`append_event` 返回值、`idempotency_key` 撞键返回负 seq 的既有语义保持不变；不新增列、不改表结构。

### C 组：流水线口径剔除审计与无归属事件（3 项）

- C1 `src/api/routes/pipeline.py::_pipeline_window` 三条查询（窗口 / `history_total` 总数 / 失败集合）统一追加 `e.event_type <> 'admin_action'` 与 `e.issue_id NOT IN ('', '#0')`；参数占位符与 `_scene_params` 拼接顺序必须保持正确。
- C2 `get_pipeline` 的事件装载 `_where` 同口径追加（含 `_win_ids` 下推分支）。
- C3 `scene_id` 过滤路径（LEFT JOIN `issue` 分支）与 `executing` 派生（`running_issues` 交集）保持同一口径；前端不改代码。

### D 组：存量数据修复（2 项）

- D1 新增 `scripts/backfill_admin_action_issue_id.py`：按 `payload.target_event_seq` 反查目标事件，补齐 `repo_key` / `issue_id`；默认 dry-run 只打印将修正条数与样例；`--apply` 时单事务分批（每批 ≤ 500 行）执行并打印前后计数；幂等（已正确的行跳过）。
- D2 同一脚本增加 `--normalize-unrelated`：把确无关联的 `issue_id='#0'` 历史行改为 `''`（唯一允许触碰历史数据的一条 UPDATE）；执行前把 `(seq, old_issue_id)` 清单落盘备份文件，供人工回滚；`--rollback <备份文件>` 按清单回写。

## 明确禁止

1. 不改 `events` 表结构、不新增列/索引（含「无归属」持久化标记，如评审认为必须，另行申请）。
2. 不修改 `append_event` 返回值语义、`idempotency_key` 唯一约束行为。
3. 不改事件详情页、审计导出、后台管理页对 `admin_action` 的查询与展示。
4. 不调整重派业务逻辑（30 秒幂等、任务快照参数复用等约束不变）。
5. 不碰前端 JS/CSS/响应字段、不碰 skill 包、不加新依赖。
6. 不顺手重构、不改无关日志级别、不做无关格式化；范围外发现只写报告。
7. **不 push、不建 MR、不部署、不重启服务**；只提交到本地 `weekly-mys` 分支并标注「待人工审批」。

## 验收标准

1. 静态核对：`git diff` 只含 A/B/C/D 列出的文件，改动行数与“参考资料”的预估量级相符。
2. 单测（正向）：构造 `title="Admin redispatch: event #<seq>"` 的审计事件，其目标事件 `issue_id='#30'` → 断言落库 `issue_id == '#30'`，且 `payload.target_event_seq` 仍为原 seq。
3. 单测（反向）：构造无任何结构化来源的事件（标题含裸 `#123`）→ 断言 `issue_id == ''`，不是 `'#123'` 也不是 `'#0'`。
4. 单测（聚合）：插入 `admin_action` 与 `issue_id='#0'` 行 → 断言 `_pipeline_window` 返回的 `ordered_ids` / `total` / `failed_ids` 均不含它们；`get_pipeline` 响应 `history_total`、`today_count`、`failed_count` 与剔除后一致；分页多取不空转。
5. 端到端：对同一真实 Issue 事件分别执行一次 redispatch 与一次 terminate → 该 Issue 行事件数增加，列表**不新增任何行**；`#55388` 这类行不再出现。
6. 回填：对存量数据 dry-run 输出修正条数；`--apply` 后 SQL 校验「`admin_action` 中 issue_id 指向不存在 Issue」的记录数 = 0；`--normalize-unrelated` 后 `issue_id='#0'` 行数 = 0。
7. 幂等：回填脚本连续执行两次，第二次变更数为 0；`--rollback` 能按备份文件还原。
8. 回归：真实 Issue #30 的行数、节点状态、责任人、stage 与修复前一致（before/after 对照）；事件详情页仍可按 `target_event_seq` 查到「谁在何时对哪个事件做了什么」。
9. 线上观察（只写步骤不执行）：部署后观察一个自然日内 `admin_action` 新行 `issue_id` 是否均指向真实 Issue，流水线不新增无归属行。

## 参考资料

- 必须保留的既有保护：`_extract_entity_type_from_text` 的 issue/PR 区分语义；`append_event` 的 `idempotency_key` 撞键分支；`_pipeline_window` 的 scene LEFT JOIN 与索引友好写法（SQL 下推是性能优化 #1077-P，不得回退成全表加载）。
- 规模预估：生产代码约 120–160 行（A 组 ~40，B 组 ~25，C 组 ~30，D 组 ~60 脚本+备份），测试约 150–200 行；合计 < 400 行。
- 测试口径：`sqlite` 内存库 + 网络打桩，不压生产/远端；`pytest tests/` 只跑相关用例。
- 数据口径：本单引用数字来自生产库历史快照，执行前以预发复核为准。