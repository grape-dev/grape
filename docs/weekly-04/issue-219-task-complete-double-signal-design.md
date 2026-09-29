# Issue #219 关联：任务完成双重标记设计（Double-Signal Task Completion）

> 状态：设计方案（v1，2026-09-22）｜分支 weekly-04-lyz
> **本体（双标记 + 单点 finalize + 按技能开关）已实施（2026-09-22），见 §10；watchdog 与注册表判定为二期。**
> 关联：#219（tasks complete 收尾二选一）、#45（task-complete endpoint）、issue-45-task-complete-endpoint-design.md
> 背景实证：生产事件 `Seq #2174`（gitcode:Jason7602/hello-calculator-14，issue #3，agent=vod）

---

## 1. 背景与问题

### 1.1 现状：两个 complete 通道竞争，先到先得

`grape tasks complete` 的收尾存在**两条独立通道**，最终都汇入 `service.complete_task`：

| 通道 | 触发方 | 端点 | 携带 |
|------|--------|------|------|
| ① worker 隐式收尾 | 引擎流一结束，worker 自动汇报 | `POST /api/runtime/tasks/{id}/complete`（`runtime.py` `runtime_task_complete`） | `result_text`（引擎最终输出，常为空/叙述）、exit_code |
| ② agent 显式收尾 | agent 按 SKILL 收尾流程主动调 | `POST /api/tasks/complete`（`tasks.py` `self_complete_task`） | 结构化 actions/conclusion/artifacts/… |

当前语义 = **先到先得**：
- `self_complete_task` 在任务已 `completed/failed/cancelled` 时直接返回 `already:true`（`tasks.py:330-331`），**连 `complete_task` 都不调** → agent 显式提交被丢弃。
- 历史注释：worker 隐式收尾先到 → 事件落 `completed`，`events.result = final_text[:200]`（无结构化时 200 字截断），`event.json archive.seg.output` 空模板。

### 1.2 生产实证：Seq #2174

| 项 | 值 |
|----|-----|
| 收尾时间线 | 09:22:01.060 worker 隐式 `Task completed exit=0` → 09:22:01.597 agent `POST /api/tasks/complete 200`（`already`） |
| `events.result` | 恰 200 字截断叙述（`result_text[:200]`），全文被丢 |
| `archive seg.output` | `{"actions":[],"conclusion":"",...}` 全空 |
| `agent_tasks.result_text` | `''`（agent 显式提交未落库） |
| `agent_tasks.status` | 停在 `running`（收尾后被并发运维/重claim 复置，另论） |

结论：**worker 隐式收尾抢先，agent 显式 complete 被终态幂等丢弃** → 详情页「处理记录与答复结论」为空。

### 1.3 问题本质

终态判定是"谁先调用谁赢"，而不是"谁的内容可信"。

---

## 2. 目标

- **以 agent 主动调用 complete 为准**：任务的完成时点与内容由 agent 显式决定（#219 方案C 情形一语义）。
- **根治竞态**：worker 不再抢终态，只作为"引擎流已结束"的证言。
- **result_text 不能丢**：agent 显式 complete 提交的完整结果（叙述或结构化）必须持久化——`events.result` 存完整 conclusion（非 200 字截断）、`agent_tasks.result_text` 存结构化全文，不得被 `already` 与截断路径丢弃。
- **行为不退化**：非 CLI 技能（情形二，只输出 JSON 块、不调 complete）收尾不受影响；failed/cancelled/用户 terminate 立即终态不受影响。
- **不产生二次副作用**：评论回帖 / event archive / pending_review / A2A 下游派发只执行一次。

---

## 3. 方案总览：双重标记 → 单点终态

引入两个**相互独立**的完成标记，二者都满足才最终完成：

```
agent_tasks:
  stream_ended   INTEGER 0/1   ← worker 隐式回调置 1（引擎流结束，只标记，不写终态）
  self_reported  INTEGER 0/1   ← agent 显式 grape tasks complete 置 1（携带结构化权威内容）

终态判定：stream_ended=1 且 self_reported=1  → 触发唯一 finalize() → status=completed
两个标记由「后到者」触发 finalize；先到者只落标记。
```

| 时序 | stream_ended | self_reported | 结果 |
|------|:---:|:---:|------|
| 流先结束（如 #2174 方向） | 1（worker 置） | 1（agent 置，触发 finalize） | finalize 用**结构化完整结果** |
| 自报先到 | 1（worker 置，触发 finalize） | 1（agent 先置） | 同上 |
| 非自报技能（情形二） | 1 → 直接完成 | 不需要 | 维持现状 |
| 用户 terminate / 失败 / 取消 | — | — | 立即终态，不等双标记 |

---

## 4. 细节设计

### 4.1 标记落库

`agent_tasks` 新增两列（`db_setup` 迁移 `_ensure_column`）：
```sql
ALTER TABLE agent_tasks ADD COLUMN stream_ended  INTEGER DEFAULT 0;
ALTER TABLE agent_tasks ADD COLUMN self_reported INTEGER DEFAULT 0;
```

### 4.2 并发：两个回调谁触发 finalize

两个回调在独立 HTTP 请求（各自带凭据）：
- **条件更新**：`UPDATE agent_tasks SET stream_ended=1 WHERE id=? AND stream_ended=0`，`rowcount==0` 表示自己不是第一个 → 需触发 finalize。
- 触发前在锁内再读一次 `(stream_ended, self_reported)`，双满才 finalize；`finalize` 本身幂等（终态事件改写保护）。

### 4.3 finalize（单点，副作用去重）

抽 `service.finalize_task(task_id, result_text_structured, exit_code)`（可视为 `complete_task` 终态段的收敛）：
- 解析结构化 output → `finalize_event_archive`（archive 重写为完整结构化 output）→ `update_event_complete(completed, result=conclusion)`。
- **完整持久化**：`events.result` 存完整 conclusion（结构化始终存在时不再走 `[:200]` 截断）；`agent_tasks.result_text` 存结构化全文，供后续查看/重放。
- **副作用单点**：评论回帖 / 会话同步 / 子任务登记 / `pending_review` 只在 finalize 内部唯一执行；基于「标记从未双满」的判据天然只跑一次（配合 update_event_complete 同态覆盖保护）。

### 4.4 worker 侧改动（`runtime.py` `runtime_task_complete`）

- 断言任务 `self_reported` 能力：**非自报技能**→走原 `complete_task`（现状不变）。
- **自报技能**→ 只置 `stream_ended=1`；若此时 `self_reported=1`（自报先到）→ 触发 finalize。

### 4.5 agent 侧改动（`tasks.py` `self_complete_task`）

- 置 `self_reported=1`（幂等：`completed+self_reported=1` → `already`）。
- 结构化结果始终组装（沿用 #219 已实施的 result_text 回填）。
- 若此时 `stream_ended=1` → 触发 finalize。
- `failed/cancelled` → 仍 `already`，不覆盖真实失败/取消。

### 4.6 按技能开关（最大前提）

双标记**只对声明「会自报 complete」的技能启用**。判定方式（选一，未决）：
- a) `agent_tasks.skill` → SKILL.md 内容检测（含 `grape tasks complete`）；
- b) 技能能力注册表 `{skill: self_report: true}`（configs/skill-description.json 或独立表）；
- 建议二期 b，首期用 a 的轻量实现或按 `GRAPE_CLI_MODE=WORKER` 任务全量视为自报 + watchdog 兜底。

### 4.7 超时兜底（防永久 running）

- 自报技能下 `stream_ended=1` 后等待 `self_reported` 有超时阈值（复用任务 TTL/心跳；建议 `SELF_REPORT_GRACE`，如 10-30 min）。
- 超时且 `self_reported=0` → 回退用 `agent_tasks.result_text`（引擎流结果）终态化（或标记 failed），避免事件永远 running。
- 反向（`self_reported=1` 但流迟迟不结束）→ 同样阈值内以自报结果完成。

---

## 5. 改动点清单

| 文件 | 改动 |
|------|------|
| `src/api/db_setup.py` | `agent_tasks` 迁移加 `stream_ended` / `self_reported` 列 |
| `src/api/routes/runtime.py` | `runtime_task_complete`：自报技能只置 `stream_ended=1`（触发条件 finalize） |
| `src/api/routes/tasks.py` | `self_complete_task`：置 `self_reported=1` + 双满触发 finalize |
| `src/runtime/service.py` | 收敛 `finalize_task()`（原 `complete_task` 终态段），副作用单点；watchdog 兜底 |
| `tests/test_task_complete.py` / 新增 | 两条并发时序（流先/自报先）、非自报技能不阻塞、超时回退、副作用仅一次 |

---

## 6. 验收标准

| # | 验收项 |
|---|--------|
| 1 | 自报技能：流先结束 + agent 后 complete → 事件 completed，`events.result` = **完整结构化 conclusion**（不再 200 截断），`archive.seg.output` 非空 |
| 2 | 同 1 反向时序（自报先、流后到）→ 结果一致 |
| 3 | 两标记未齐 → 事件不提前 completed（不回退截断/空结果） |
| 4 | **result_text 完整保留**：`agent_tasks.result_text` 存结构化全文，`events.result` 为完整 conclusion，无截断 |
| 5 | 非自报技能：`stream_ended` 即完成（现状不回归） |
| 6 | failed / cancelled / 用户 terminate 立即终态 |
| 7 | 副作用（评论/归档/pending_review/A2A 派发）仅执行一次 |
| 8 | 超时兜底触发，无永久 running |

### 6.1 测试用例（对应验收项）

> 载体：`tests/test_task_complete.py` + 新增时序/状态机用例（`tests/test_task_double_signal.py`）。端点级走 TestClient；状态机级单测 `finalize`/标记判定。

| 用例 | 前置 | 动作 | 断言 |
|------|------|------|------|
| **TC1** 流先·自报后（复现 #2174） | agent_tasks(status=running) + events(running)；技能声明自报 | ① worker `POST /api/runtime/tasks/{id}/complete`（叙述 result_text）→ ② agent `POST /api/tasks/complete`（结构化） | ①后：events 仍 running、task 不 completed、只置 `stream_ended=1`；②后：completed，`events.result=完整 conclusion`、`archive.seg.output` 非空、`agent_tasks.result_text` 存全文、副作用恰 1 次 |
| **TC2** 自报先·流后到 | 同上 | ① agent complete 先 → ② worker 隐式 complete | ①后：置 `self_reported=1`、不终态；②后：触发 finalize，结果同 TC1 |
| **TC3** 双标记未齐不终态 | 自报技能 | 仅 worker 隐式 complete（流结束） | `stream_ended=1`、`self_reported=0` → events 保持 running、task 不 completed |
| **TC4** 非自报技能不阻塞 | 技能未声明自报 | worker 隐式 complete | 直接 completed（现状不回归），`self_reported` 不参与判定 |
| **TC5** failed/cancelled/terminate 立即终态 | status=running | 置 failed / 用户 terminate | 立即终态，不等待第二个标记，不被后续 complete 覆盖 |
| **TC6** 重复显式 complete 幂等 | task completed + self_reported=1 | 再次 agent complete | 返回 `already:true`，不重复 finalize、不缺副作用 |
| **TC7** 副作用单次 | 双标记双满触发中 | 评论/归档/`pending_review`/A2A 派发埋点 | 每种副作用执行次数 = 1 |
| **TC8** 超时兜底 | 自报技能，stream_ended=1 后超 `SELF_REPORT_GRACE` | 走 watchdog | 回退完成/failed（用已有 `agent_tasks.result_text` 兜底），无永久 running |
| **TC9** result_text 完整性（>200 字） | 长叙述 + 结构化 | 双满 finalize | `events.result` 含 >200 字符完整文本，无 `[:200]` 截断；`agent_tasks.result_text` 为结构化全文 |

---

## 7. 风险与缓解

| 风险 | 等级 | 缓解 |
|------|------|------|
| 非自报技能被误判自报 → 永不完成 | Med | 按技能开关 + watchdog 兜底（§4.6/4.7） |
| 两个回调并发触发 finalize | Low | 条件更新标记 + 锁内判定 + finalize 幂等 |
| 超时窗口内下游 A2A 延迟 | Low | 阈值内正常，仅"拖慢完成"不丢数据 |
| 历史事件（如 #2174）不回补 | Med | 一次性数据修补脚本（另行评估，不回滚代码） |

---

## 8. 回退方案

- 纯状态机 + 标记改动，回退 = revert 对应 commit + 恢复 `complete_task` 直调；`stream_ended/self_reported` 列为可留空（默认 0），不影响既有任务。
- 若某技能回归卡 running：先调 watchdog 阈值，不必回退代码。

---

## 9. 待办/未决

- [ ] 技能自报能力判定方式：SKILL.md 检测 vs 注册表（§4.6）。
- [ ] `SELF_REPORT_GRACE` 超时阈值取值（建议复用任务 TTL，10-30 min）。
- [ ] 与#219 方案C 收尾二选一的提示文本对齐（情形一技能需"声明自报"）。
- [ ] #2174 历史数据修补脚本（可选）。

---

## 10. 实施记录（2026-09-22，本体 v1）

| 文件 | 改动 |
|------|------|
| `src/api/db_setup.py` | `agent_tasks` 迁移加 `stream_ended`/`self_reported`（INTEGER DEFAULT 0）；`service._table_columns` 按 PRAGMA 动态过滤，兼容未迁移库 |
| `src/runtime/service.py` | `_TASK_COLUMNS` 纳入双标记列；`skill_self_reports()`（SKILL.md 内容含 `grape tasks complete`）、`set_task_signal()`（条件置标记 + self_reported 同步存 result_text）、`task_double_signal_ready()`、`finalize_task()`（双满→收敛终态）；`enqueue_task`/`get_task` 按实际列过滤 |
| `src/api/routes/runtime.py` | `runtime_task_complete`：自报技能只置 `stream_ended` + `finalize_task`；非自报走原 `complete_task` |
| `src/api/routes/tasks.py` | `self_complete_task`：failed/cancelled→already；completed→already；running→自报技能置 `self_reported`+`finalize_task`（未齐则等待），非自报走原 `complete_task` |
| `tests/test_task_double_signal.py`（新，7 项） | 双标记未齐不终态 / 两种时序（流先·自报先）/ 重复标记幂等 / result_text 不截断 / 端点显式等待流结束 / 非自报走旧路径 |

**验收自查**：`stream_ended ∧ self_reported` 双满才 `complete_task`；`events.result` 走完整 conclusion（含 >200 字）；`agent_tasks.result_text` 在自报时即存全文（不丢）；新测试 7 项通过；既有 `test_task_complete`（9）/`test_a2a_queue`（7 单跑）/`test_runtime` 无新增失败（12 项组合失败 = 基线存量，含 a2a 顺序污染与 openapi skills scope）。