# 方案 C：单信号「流结束即终态」+ 会话 token 延迟失效窗口（取消技能分类与双信号）

> 状态：技术方案（2026-10-02）｜目标分支：weekly-05-lyz
> 关联：`awaits` 现有 #219 双信号（`stream_ended ∧ self_reported`）基于「服务端区分自报/普通技能」
> （`skill_self_reports` 查 SKILL.md 是否写 "grape tasks complete"）收敛终态；本方案论证并落地方向 C：
> **任务收尾统一为「引擎流结束 = 终态」，结构化 self_report 只是可选的产物补挂，不再参与“是否结束”的判定**；
> 会话 token 借由既有「延迟失效」宽限窗承接流结束后的短尾 CLI 调用。

---

## 1. 现状与两组事实

### 1.1 现状：终态强耦合会话 token 生命周期

- 任务落终态 → `update_task_status` 调用 `revoke_session_token(task_id)`（`service.py:1370-1377`）。
- 但 `revoke_session_token` 是**延迟失效而非删除**：把 `session_api_tokens.expires_at` 缩短到
  `now + SESSION_TOKEN_REVOKE_GRACE`（`auth_token.py:105-122`），值为 **180s**（原因为 seq#5613「agent
  complete 后仍需 A2A」）；过期行由 `purge_expired_session_tokens`（`auth_token.py:125-135`）兜底清理。

→ 即：**终态并不立即杀死 token，仍留有 180s 尾部窗口**，宽限期内任务自己的 `tk_` 依旧可调 A2A/其他 CLI。

### 1.2 现状：双信号 + 技能分类的四类代价

| # | 代价 | 出处 |
|---|------|------|
| 1 | 服务端要按 SKILL.md 文本区分「自报/普通」（内容即契约，脆弱） | `service.py:1812-1829` |
| 2 | 自报技能等 `self_reported`：Agent 忘记/中断调 `grape tasks complete` → 任务挂起（#210 类） | `tasks.py:448-450` / `runtime.py:643-645` / `event_pipeline.py:2898-2901` |
| 3 | 双信号齐但 finalize 未收敛 → status 卡 running（#214 类） | sweeper 双信号兜底 `cleanup.py` |
| 4 | `tasks complete` 的终态幂等「已 completed 直接 already」会把迟到的结构化补挂拒掉 | `tasks.py:332-335` |

### 1.3 时序核心命题（方案 C 的成立前提）

引擎为 `opencode run` / worker 时：一次进程 = 一次任务。Agent 的 `grape tasks complete` 是**工具调用**
（bash/命令行），模型需**等该调用 HTTP 返回**才会继续/退出；`tk_` 被 401 只是后续副作用。
因此**正常路径**下：

```
self_report(HTTP 已 commit)  →  进程退出（= 流结束）   →   终态 + 延迟失效(180s)
```

但存在**不许保证的尾部**（引擎不 await 最后一个工具返回即 EOF / 流结束判定与 self_report 提交并发），
所以「流结束 finalize + 立即吊销」会杀死在途的 self_report——这是必须做「终态与吊销解耦」的原因。

---

## 2. 目标

1. **取消技能分类**：删除 `skill_self_reports` 及其所有判定点；所有技能同一条收尾路径。
2. **单信号终态**：`引擎流结束 = 任务终态`（direct：进程 EOF；worker：进程退出后的 completed 回调）。
3. **结构化产出为“产物”而非“闸门”**：已有 self_report/结构化 → 作为权威结论落库；没有 → 回落流文本；
   无文本也不硬判失败（给宽限窗前补挂留机会）。
4. **会话 token 延迟失效承接短尾**：复用 `SESSION_TOKEN_REVOKE_GRACE`，使流结束后 <180s 内到达的
   `grape tasks complete`/A2A 依旧有效；并由 `purge_expired_session_tokens` 兜底清理。
5. **迟到 self_report 幂等补挂**：宽限期内对「已终态但缺结构化产出」的任务补挂 `structured_output`，
   覆盖「process 未 await 工具返回」「并发竞态」等尾部场景；安全由「同任务 token + 宽限窗」约束。

---

## 3. 信号与语义（术语表）

| 信号 | 含义 | 在 C 中的作用 |
|------|------|----------------|
| `stream_ended` | 引擎流已结束（direct EOF / worker completed 回调） | **终态触发点（保留，作为仪器）** |
| `self_reported` | Agent 显式 `grape tasks complete` 已提交 | 不再当闸门；仅表示「已有结构化结论可取」 |
| `result_text` / `structured_output` | 结构化产出 | 产物，落库优先级最高 |

终态规则（唯一）：**`stream_ended` 生效即开始 finalize；不等 `self_reported`**。

---

## 4. 方案设计

### 4.1 收尾统一：stream-end finalize

三处收尾入口统一收敛为「流结束 → `finalize_by_stream_end`」：

| 入口 | 触发 | 动作 |
|------|------|------|
| direct：`_execute_agent_inner`（`event_pipeline.py:2890-2931`） | 引擎进程 EOF | 置 `stream_ended=1` → `finalize_by_stream_end` |
| worker：`/api/runtime/tasks/{id}/complete`（`runtime.py:643`） | worker 进程退出回调 | 置 `stream_ended=1` → `finalize_by_stream_end` |
| Agent 显式 `grape tasks complete`（`tasks.py`） | Agent 自报 | 仅置 `self_reported=1` + 存 `result_text`；结束仍由 flow 顶上的 stream-end 触发 |

`finalize_by_stream_end(task_id, exit_code, stream_text)`：

```pseudo
选中 conclusion：
  if 任务已 self_reported 且 result_text 为结构化 JSON:
      conclusion = result_text（自报为权威）
  elif has_output(stream_text):
      conclusion = stream_text（回落）
  else:
      conclusion = ""（不硬判 failed，见 4.2）
status = completed（保持非 failed 占位，供宽限期补挂提升语义一致）
落终态：update_task_status('completed', result_text=conclusion, exit_code)
         → 触发 180s 延迟失效（既有 revoke）
```

- 删除 `skill_self_reports` 分支：direct 不再有「仅自报技能补 stream_ended」的特殊逻辑，
  也不再等 self_reported；worker 不再区分自报/普通。
- `finalize_task`（双信号收敛）一并退役，删除其调用点。

### 4.2 无输出不再硬判 failed

现状 `event_pipeline.py:2918-2931` 在 `has_output=False` 时若 `self_reported==1` 跳过、否则标 failed。

C 改为：
- stream-end 时若既无 stream 文本、又无 self_report → **置 `completed` 但 result/structured_output 留空**
  （理由：流结束即结束；该任务可能马上在宽限窗内补自报，不应在“无输出”这点上定死 failed）。
- 宽限窗前未收到补挂 → 维持 completed 空结论（任务确实结束了，只是没产出）。

> trade-off 说明：真正的“静默死”会被记成 completed 空结论而非 failed。缓解：补挂窗口内若 agent 通过
> `tasks complete` 补报则按自报提升（4.4）；真正需要 failed 语义的场景由「引擎非 0 exit_code」决定——
> `exit_code != 0` 且无自报 → 仍 failed（失败以退出码为准，而不是以“文本为空”为准）。

### 4.3 会话 token 生命周期（复用既有延迟失效）

- 保持 `update_task_status` 落终态时调用 `revoke_session_token`（**不改 revocation 语义**——
  它已经是延迟 180s，正好承接流结束后的短尾 CLI）。
- 建议把 `SESSION_TOKEN_REVOKE_GRACE` 提为一个可配常量（当前 180s），文档说明用途扩展为
  「流结束 → 迟到 self_report / A2A 尾部窗口」；仍由 `purge_expired_session_tokens` 兜底清理过期行。
- 校验链无需改动：`verify_session_token`（`auth_token.py:59-88`）本就“行存在且未过期即有效”。

### 4.4 迟到 self_report 幂等补挂（宽限窗内的 enrich）

`self_complete_task`（`tasks.py:293-357`）把「终态幂等只读返回」改为「终态且可补挂则补充」：

```
case task.status:
  queued/dispatched/running       → 正常双分支（直接 complete / 置 self_reported）
  completed:
      if 尚未落 structured_output(空) 且 now < expires_at(宽限窗内):
          # 幂等补挂：附加结构化结论到 agent_tasks + events
          UPDATE agent_tasks SET result_text=结构化, structured 已含 …
          UPDATE events SET structured_output=…, result=conclusion
          返回 {ok:true, status:'completed', enriched:true}
      else:
          返回 already（不覆盖）
  failed:
      if 由「no-output/exit_code=0 占位」置 failed 且 宽限窗内 且 self_report 未消费:
          提升 failed → completed + 补挂结构化（仅此受控路径允许翻转）
      else:
          返回 already
  cancelled  → 返回 already（不提升）
```

守卫（安全）：
- **只看本任务自己的会话 token**（`verify_session_token_for_repo` 同仓库 + `agent_type` 匹配）；
- **仅在宽限窗内**（对照 `session_api_tokens.expires_at`，过期即拒绝）；
- **仅当目标字段为空**（idempotent，不覆盖已有结论）；
- failed→completed 提升只允许 `exit_code` 为「无输出占位」路径产生的 failed，不接受人为已 failed 的被翻转。

### 4.5 退役项

| 项 | 说明 |
|----|------|
| `skill_self_reports`（`service.py:1812`） | 删除，不再读 SKILL.md 判技能类型 |
| `finalize_task` / `task_double_signal_ready`（`service.py:1856-1880`） | 退役，收敛逻辑并入 `finalize_by_stream_end` |
| direct 特判「`if skill_self_reports` 才补 stream_ended」（`event_pipeline.py:2898`） | 删除；stream_ended 无条件置 |
| worker 特判（`runtime.py:643`） | 删除；统一走 `finalize_by_stream_end` |
| `stream_ended / self_reported` 两列 | **保留**作为仪器（日志/audit），不再作为闸门 |

### 4.6 sweeper 规则调整

`sweep_stuck_agent_tasks_once`（`cleanup.py`）现状含「非终态 + 双信号齐 → 回收为 failed」（针对 #214）。
C 落地后：
- 「流结束 = 终态」，理论不再产生“双信号齐但非终态”的残留；该规则**保留**作为兜底（无害，语义变为
  “双信号齐但未收敛 → 置 failed”）；
- 新增「终态已落但 `self_reported` 在宽限窗内仍未补挂且已过期」→ 由既有 `purge` 清理会话行即可，无需新规则。

---

## 5. 时序示意

**direct（正常自报）**
```
[agent] 工具调 grape tasks complete ──► server: self_reported=1, result_text=结构化(commit)
[engine] 进程退出 ──► server: stream_ended=1 → finalize_by_stream_end
        → 用 self_report 结构化结论 → completed；revoke(180s 宽限)
```

**direct（引擎未 await 最后工具 / 竞态）**
```
[engine] EOF(stream end) ──► finalize_by_stream_end（无 self_report，无流文本）→ completed(空)
[agent] grape tasks complete 迟到达（<180s）→ self_complete_task 命中「completed 且未 enrich」→ 补挂结构化 ✅
>180s：verify 过期 → 401 拒绝（结论保不保得住取决于宽限窗内是否到达）
```

**worker**
```
[worker] 进程退出 → POST /api/runtime/tasks/{id}/complete → stream_ended→finalize_by_stream_end
（任务的 agent 在进程内已先行 self_reported，同上正常路径；无则回落流文本）
```

---

## 6. 各模块改动

| 模块 | 改动 | 说明 |
|------|------|------|
| `src/runtime/service.py` | 新增 `finalize_by_stream_end`；删除 `skill_self_reports` / `finalize_task` / `task_double_signal_ready`；`update_task_status` 保持既有 revoke | 核心收敛 |
| `src/api/event_pipeline.py` | `_execute_agent_inner` 收尾改为无条件置 `stream_ended` + `finalize_by_stream_end`；删除自报特判与 no-output 硬 fail 分支 | direct |
| `src/api/routes/runtime.py` | `runtime_task_complete` 删除技能分支，统一 finalize | worker |
| `src/api/routes/tasks.py` | `self_complete_task`：新增「completed 缺产出的宽限补挂」与「受控 failed→completed 提升」；不再调 `skill_self_reports` | 自报/补挂 |
| `src/api/auth_token.py` | （可选）`SESSION_TOKEN_REVOKE_GRACE` 常量文档化/可配；机制复用不动 | 宽限窗 |
| `src/runtime/cleanup.py` | 双信号规则保留为兜底；无新增规则 | sweeper |
| 测试 | 新增 single-signal 收敛 + 补挂 + 期满拒绝用例 | 见 §9 |

---

## 7. 安全分析（宽限窗暴露面）

- 宽限窗内该 `tk_` 仍可调**本仓库、本 agent_type** 的会话端点（A2A、git-repo、tasks 收尾）——这与现状
  （`revoke_session_token` 延迟 180s）暴露面一致，**不新增风险**；
- 补挂/提升端点强校验：同任务 token + 时间窗 + 目标字段为空，未放大权限；
- 过期即由 `verify_session_token` 拒绝 + `purge` 清理，无永久 token。

---

## 8. 兼容性 / 上线

1. **存量任务**：线上可能残留“双信号已置但未收敛”的 running 任务 → sweeper 双信号规则兜底回收，无需迁移数据。
2. **技能仓库**：无需再维护 SKILL.md 中的 `grape tasks complete` 作为契约（普通/自报一视同仁），技能文档可逐步删除该描述。
3. **前端/状态机**：`awaiting_input`/`pending_review` 等状态流不变；本次只改任务完成语义。
4. **CLI**：`grape tasks complete` 语义不变（自报 + 结构化），只是不再作为“结束闸门”。

---

## 9. 测试

| 用例 | 断言 |
|------|------|
| UT-01 direct 正常自报：流结束后用 self_report 结构化收敛 completed + structured_output | 终态 + 产物 |
| UT-02 direct 无文本无自报：置 completed 空结论，不判 failed | 无硬 fail |
| UT-03 引擎非 0 exit_code 且无自报：failed | 退出码主导失败 |
| UT-04 宽限窗内迟到 self_report 补挂 completed 空结论任务 | enriched=True + structured_output 落库 |
| UT-05 超过宽限窗迟到：401/403 拒绝 | 时间窗守卫 |
| UT-06 failed(no-output 占位) 宽限窗内提升 completed + 补挂 | 受控翻转 |
| UT-07 已有结构化产出的 completed 重复补挂 → already，不覆盖 | 幂等 |
| UT-08 移除 `skill_self_reports` 后普通/自报行为一致（同一收敛路径） | 无分类 |
| 回归 | `tasks complete` 正常自报、worker complete、A2A 尾部、awaiting-input、sweeper 双信号兜底 |

---

## 10. 验收对照

| 项 | 结果 |
|----|------|
| 取消技能分类（普通/自报同路径） | ✅ 4.1/4.5 |
| 流结束即终态，不再等 self_reported | ✅ 4.1 |
| 结构化产出为产物而非闸门 | ✅ 4.2 |
| 流结束后的短尾 CLI（含迟来自报）由既有 180s 宽限承接 | ✅ 4.3 |
| 迟到 self_report 幂等补挂，安全受同任务 token+时间窗约束 | ✅ 4.4 |
| 消除 #210 挂起、#214 残留两类故障面 | ✅ 4.5/4.6 |
| token 生命周期不新增暴露面 | ✅ 7 |