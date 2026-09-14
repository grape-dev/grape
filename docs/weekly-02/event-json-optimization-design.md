# event.json 生成流程梳理与优化方案

> 目标：梳理 event.json（Issue 级结构化产出档案）的完整生成流程，并给出可落地的优化点。
> 文件：`src/api/event_archive.py`（核心）/ `event_store.py` / `event_pipeline.py` / `service.py` / `runtime.py` / `routes/events.py`
> 日期：2026-09-11
> 状态：**梳理分析，未改代码**

---

## 一、生命周期流程

### 1.1 事件触发 → 落库 → 建档

```
触发（webhook / IM / A2A）
  → append_event（events 表落库 = DB 底账，返回 seq）
  → event_pipeline._enqueue（L1594）：init_event_json 建/追加 pipeline 段
      ├── 首次：建 {issue, status, pipeline:[], summary} 骨架
      └── 追加：pipeline.append({
             seq, agent, runtime, skill, status:running,
             trigger{actor,branch,raw_type}, input{issue_id,summary},
             output{actions,conclusion,artifacts,next_step,issue_summary},
             a2a_to, logs[], started_at, completed_at })
  → enqueue_task（agent_tasks 落库，env_extra/prompt 持久化）
  → Worker / RuntimeAgent claim
  → 执行中 append_event_archive_log（L1321）：增量写 logs（全量、脱敏）
  → 完成 complete_task（L1511）/ runtime.py 完成（L584）：
      finalize_event_archive：写 output + 段 status + 整链 status + 覆盖 summary
  → record_a2a_to：A2A 交办记录 next 段 a2a_to
```

### 1.2 写点汇总

| 写点 | 函数 | 频次 | 方式 |
|------|------|------|------|
| 建档/追加段 | `init_event_json` | 每 agent 启动 1 次 | 原子全量替换（临时文件+rename） |
| 增量日志 | `append_event_archive_log` | 每执行事件 N 次 | 同上（全量重写） |
| 完成 | `finalize_event_archive` | 每 agent 完成 1 次 | 同上 |
| A2A 交办 | `record_a2a_to` | A2A 时 1 次 | 同上 |

> 所有写点都走 `_atomic_write`：写临时文件 → os.replace（Windows 重试 3 次兜底）。写失败仅 log.warning，不阻断（events 表为可靠底账，见文件头注释）。

### 1.3 消费端

| 消费方 | 读取 | 用途 |
|--------|------|------|
| `routes/events.py` 详情（M4） | `load_event_json` 按 seq 取 pipeline 段 | 结构化产出展示（actions/conclusion/artifacts） |
| `build_issue_context` | 档案摘要 + 各段结论 | 后续 agent prompt 富上下文注入 |
| 前端事件详情 | `/api/events/{seq}` `archive` 字段 | 展示 |
| `runtime/cleanup.py` | 保留 event.json | 审计档案 |

### 1.4 写与不写的边界（重要澄清）

> **并非每个 task 结束都写 event.json。** 只有满足以下全部条件的任务才会写入结构化产出：

| 条件 | 判定位置 | 说明 |
|------|---------|------|
| ① task 有 `event_seq` | `complete_task` L1483 `if task.get("event_seq")` | Chat/Chat/A2A 类任务 `event_seq=None` → 不走 finalize |
| ② 关联真实 Issue（非 `#0`/空） | `finalize_event_archive` → `_extract_num`；`#0`/空 → 直接 return | 无关联 Issue 的事件不建档案 |
| ③ 已建档（pipeline 事件链） | `init_event_json`（`#0` 跳过） | 只有 pipeline 事件由 `_enqueue` 建档 |

**会写 event.json（pipeline 事件链）**：
```
issue_opened → VOD → Issue → Coding → QA → SRE
  （各 agent 有 event_seq + 真实 issue_id）
每个 agent：init（建档/追加段）→ 执行中 append log → 完成 finalize（结构化 output 落档）
```

**不写 event.json**：

| 类型 | 位置 | 原因 |
|------|------|------|
| Chat 对话 | `chat.py` L771（`event_seq=None, issue_id=""`） | 条件①不满足 |
| A2A 内部 task | `handler_async.py` L1199 | 条件①不满足 |
| 无关联 Issue 事件（`#0`） | `_extract_num` 对 `#0` 返回空 | 条件②不满足 |
| PR-only 事件（无对应 issue） | `_extract_num` 无匹配 number | 条件②不满足 |

> **影响**：优化的热点（logs 膨胀、`_locate_by_seq` 目录扫）实际影响面**仅限 pipeline 事件链**（VOD/Issue/Coding/QA/SRE），
> 而非全部 task——Chat/A2A/无 Issue 泛事件不涉及，优化时无需考虑。

---

## 二、问题与优化点（按价值排序）

### [高优] 1. `_locate_by_seq` 目录全量扫描（性能热点）

**现状**（`event_archive.py:108-137`）：
- append/finalize/record 每次都先 `_locate_by_seq`：
  1. 显式 `issue_num_hint`（task 自带 issue_id）→ 有则直接用，快
  2. 无 hint → `get_events(repo_key, limit=200)` 轮询比对 seq → 找到 issue_id
  3. 仍无 → **扫描 issues 目录下所有 event.json**（`sorted(os.listdir)` + 逐段比对 seq），O(仓库数 × 段数)

**问题**：
- `finalize_event_archive` 由 `complete_task` 高频调用；`append_event_archive_log` 每条 log 定位一次。
- 长任务日志多时反复触目录全扫，事件多/仓库多时放大明显。

**优化**：
- a) **hint 优先**：调用方都带 `issue_num_hint`（任务有 issue_id），确认并补全传参，跳过扫描（大部分场景已走此路）。
- b) **索引缓存**：维护 `(repo_key, seq) → (issue_num, path)` 内存映射（进程内 dict，首次构建、逐次累积），缺失才目录扫描；配合进程重启后惰性重建。
- c) 兜底保留目录扫描（恢复/异常路径），但加结果缓存。

### [高优] 2. 日志无限增长 + 全量重写放大（I/O 膨胀）

**现状**：
- `append_event_archive_log` 每条 log `{time,type,msg}` 追加到 `seg["logs"]`，**条数无上限**；`msg` 截断到 1000 字符。
- 每次写都 `_atomic_write` **全量 event.json**（logs 累积 → 文件持续膨胀 → 每次写放大）。

**问题**：长任务可能几十~上百条 log，event.json 可到数百 KB，每次 append 全量重写，I/O 与序列化成本高。

**优化**：
- a) **logs 上限**：每段 logs 保留最近 N 条（如 200），超限 trim 最旧（`del seg["logs"][0]` 或保留计数字段）。
- b) **分片写入**（可选）：logs 增量写到 `event.<seq>.log` 分片，`finalize` 时合并进 event.json（关闭时归并）。中工作量。
- c) 若 b 复杂，先做 a（上限）+ 惰性：仅当 logs 变更时写，非每次全量。

### [中优] 3. 锁粒度过粗（同仓库多 issue 串行）

**现状**：`_lock_for(repo_key)` —— 同一仓库所有 issue 的写入串行化。

**优化**：粒度精细化到 `(repo_key, issue_num)`。同仓库多 issue 并行任务不再互相阻塞。
权衡：锁字典键做拼接，注意内存/清理；`_locate_by_seq` 兜底扫描不受锁影响。

### [中优] 4. DB/文件一致性保障与恢复

**现状**：event.json 与 events 表双写，文档声明"events 表为可靠底账"——但**无一致性校验、无重建机制**。某次落盘失败后 DB/文件 diverged，无自动修复。

**优化**：
- a) 提供 `rebuild_event_json(repo_key[, issue_num])`：遍历 events 表按 issue_id 重放 init→append→finalize，重建档案（恢复 SOP 用）。
- b) 启动时可选 diff 校验：对 active 的 issue，若 event.json 缺失/损坏 → 提示可重建（仅检测，不自动覆盖）。
- c) `finalize_event_archive` 后 status 与 events 表 `update_event_complete` 保持幂等对齐（现有已尽力，成本低可补断言日志）。

### [中优] 5. `summary` 覆盖逻辑偏弱

**现状**（`finalize_event_archive` L239）：仅当 `output.issue_summary` 非空才覆盖 `arch["summary"]`；无结构化输出的完成，summary 可能是旧值或空。

**优化**：无 `issue_summary` 时用 `output.conclusion` 兜底截断为摘要，保证每段完成后 issue 概要反映最新结论。

### [低优] 6. 消费端 logs 未暴露 / 展示链路

**现状**：`events.py` 详情只取 pipeline 段结构化产出；`logs` 不自然暴露（任务实时日志另有 task_log SSE）。

**优化**：前端事件详情可选展示 `archive.logs`（折叠可展开），或保持 logs 仅审计用（文档说明定位，避免误暴露脱敏信息）。

### [低优] 7. `_trunc_safe` 对非 str 的处理

**现状**：非 str 用 `json.dumps(..., default=str)` 再截断 1000。深层嵌套/超大对象时 dumps 可能慢。

**优化**：dumps 前先对超大对象做递归截断（如工具结果只保留 key 层级），或限定 dumps 深度（`json.dumps` 无 depth 参数，用自定义截断）。

---

## 三、建议实施节奏

| 优先级 | 项 | 工作量 | 说明 |
|--------|-----|--------|------|
| P0 | `_locate_by_seq` hint 补全 + seq→path 索引缓存 | 小 | 直接提升高频 append/finalize 性能 |
| P0 | logs 条数上限（N=200）+ trim | 小 | 遏制 event.json 无限膨胀 |
| P1 | 锁粒度 repo → repo+issue | 小 | 提升同仓库并发任务写性能 |
| P1 | `rebuild_event_json` 重建工具 | 中 | 运维恢复能力 |
| P2 | summary 兜底 conclusion | 小 | 概览可读性 |
| P2 | 前端展示 archive.logs（可选） | 中 | 观测增强 |
| P3 | 日志分片写入 | 中-大 | 根治全量重写，可视需求再启 |

## 四、关键代码位置

| 位置 | 说明 |
|------|------|
| `src/api/event_archive.py:108-137` | `_locate_by_seq`（目录全扫热点） |
| `src/api/event_archive.py:174-192` | `append_event_archive_log`（logs 无限增长） |
| `src/api/event_archive.py:215-246` | `finalize_event_archive`（summary 覆盖） |
| `src/api/event_archive.py:74-94` | `_atomic_write`（原子全量替换） |
| `src/api/event_archive.py:15-19` | `_lock_for(repo_key)`（锁粒度） |
| `src/api/event_pipeline.py:1568-1594` | `init_event_json` 触发 |
| `src/runtime/service.py:1321-1326` | `append_event_archive_log` 调用（每条 log） |
| `src/runtime/service.py:1486/1511` | `finalize_event_archive` 调用（完成） |
| `src/api/routes/events.py:84-102` | 消费：详情附 archive 段 |