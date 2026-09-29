# 方案：去掉 event.json，事件数据统一由 events 表承载（含按 Issue 查询接口）

> 状态：✅ **阶段 A、B 已实施并部署**（阶段 A `083be623`；阶段 B 待部署提交）
> 目标分支：weekly-05（实际在 weekly-04-lyz 部署）
> 关联：#206（前端 fetch 恢复，无直接关系）、集群事件 archive 空模板问题
> （VOD 完成信号未达 master / event.json 跨 worker 不共享 → 详情页「处理记录/答复结论」空）

---

## 1. 背景与目标

### 1.1 问题

当前 `event.json`（`{WORKSPACE_DIR}/{repo_key}/issues/{n}/event.json`）是「事件档案」的**文件系统平面**：

- 由 `src/api/event_archive.py` 读写（init 建段 / append log / finalize 结构化 output / a2a_to / summary）
- 详情页、Issue pipeline 路线图、VOD agent 富上下文同时读它
- **集群多 worker 下暴露硬伤**：archive 文件跨节点不共享 / `_locate_by_seq` 定位失败 →
  `finalize_event_archive` 无法写入段 → 详情页「答复结论」落空（VOD seq=8250 实测：events 表 completed、event.json 段却恒为 running 空模板）。

### 1.2 目标

1. **去掉 event.json 作为「事件档案唯一权威」**——事件数据统一由中心化 `events` 表承载（MySQL/SQLite 皆可，天然跨节点一致）。
2. 提供 **按 Issue 获取事件列表/结构化摘要的 API**，平替 event.json 读取（供前端 + Agent）。
3. 保留对存量 event.json 的**读取兼容**（历史 issue 详情不回退）。
4. 修复伴随项：`events.agent_logs`「处理记录」落地（当前偶发恒空）。

### 1.3 阶段划分（已定）

- **阶段 A ✅**：结构化产出 + 日志落 events 表；读侧优先 DB（回退文件）；新增 Issue 事件 API；详情/路线图/VOD 改读 DB
- **阶段 B ⏳**：写侧停写 event.json（双写→单写 DB）；清理 worker_package/cleanup 依赖；存量文件补全（可选）

---

## 2. 现状梳理：event.json 消费方（写完评估）

| 消费方 | 位置 | 用途 | 去文件后平替 |
|--------|------|------|-------------|
| 详情页「答复结论」 | `src/api/routes/events.py` L85-102 | 读取 `archive.pipeline[].output` 展示结构化产出 | 查 events 新列 `structured_output` |
| Issue pipeline 路线图 | `src/api/routes/issues.py` `_pipeline_from_archive` L281-315 | 组装跨 agent 路线图 | 升级 `_pipeline_from_events`（L318 已有雏形）为主路径 |
| VOD agent 富上下文 | `src/im/vod_handler_async.py` L209-216 `build_issue_context` | 注入「Issue 档案摘要」给 VOD | 新 API 查询 events 表组摘要 |
| 写入侧 | `event_pipeline.py` init / `service.py` finalize / `report_messages` append_log / `record_a2a_to` | 6 处写 | 全部改写 events 表 |
| prompt 指引 | `prompt_builder.py` L435 | 仅提示 agent「JSON 结尾」（**不注入文件路径**）✅ 假设成立 | 无影响 |
| 运维打包/清理 | `worker_package.py` L43 / `runtime/cleanup.py` | 打包/保留模块 | 随移除调整 |

---

## 3. 关键前提（先补洞，再移文件）

### 3.1 events 表缺「结构化产出」列 —— 必须新增 ✅ 已实施

现状 `complete_task` 把结构化 output 写给 `event.json.seg.output`，但 `events.result` 只存 conclusion 文本
（`update_event_complete` 传 `_conclusion or result[:200]`）。**去掉文件前必须先落库**：

- **新增列** `events.structured_output TEXT DEFAULT NULL`（JSON：`{actions, conclusion, artifacts, next_step, issue_summary}`）✅
- **写**：`update_event_complete` 增加 `structured_output` 参数，`complete_task`（service.py）传入完整 `output` ✅
- **读**：详情页 / 路线图 / VOD 摘要改读该列 ✅
- **迁移**：`db_setup._ensure_column(events, "structured_output", "MEDIUMTEXT DEFAULT NULL")`（幂等，SQLite/MySQL）✅

### 3.2 `events.agent_logs` 落地 ——「处理记录」伴随修复 ✅ 已实施

现状 worker `report_messages`（service.py:1413）→ `append_event_archive_log` 写 event.json；`events.agent_logs`
恒空是因为该列只由 `update_event_complete(agent_logs=...)` 写入，而执行中消息未回填。改为：

- `report_messages` 路径把消息 JSON 追加写入 `events.agent_logs` ✅

### 3.3 存量 event.json 读取兼容 ✅ 已实施

- 读侧**双通道**：先查 events 表（新列），缺失/为空时回退读 event.json（`load_event_json` 保留）——历史 issue 详情不回退 ✅
- **迁移/数据补全（阶段 B 可选）**：后台任务把存量 event.json 的 `output`/`logs` 写入 events 新列（低优先级，读兼容已兜底）。

---

## 4. 新 API：按 Issue 获取事件（平替 event.json）✅ 已实施

接口（满足 `help/api/08-events.md` 评审要求，已落地）：

```
GET /api/issues/{owner}/{repo}/{number}/events    # 已实施；number 支持 5 / #5（%23）
```

返回（结构化摘要，等价于原 `event.json` 的一维视图）：

```json
{
  "events": [
    {
      "seq": 8250, "event_type": "issue_opened", "agent_type": "vod",
      "status": "completed", "runtime": "opencode-worker-...", "skill": "vod-cli",
      "summary": "...", "created_at": "...",
      "output": {"actions": [...], "conclusion": "...", "artifacts": [...],
                 "next_step": "...", "issue_summary": "..."},
      "logs": [...]
    }
  ],
  "segments": [...同 events...],
  "total": 2
}
```

- 数据源：`events` 表（`WHERE repo_key IN (双 provider) AND issue_id` 归一化后匹配，seq 升序）+ 新列 `structured_output` + `agent_logs` ✅
- **鉴权 dual**：`get_current_user_or_api_key`——`grape_`/JWT/`tk_` 均可；**tk_ 限定绑定仓库**（跨仓库 403）✅
- `help/api/08-events.md` 已补该端点文档 ✅
- 仓库事件列表 `GET /api/events/{owner}/{repo}` 同步 dual + tk_ repo 隔离 ✅

---

## 5. 改动清单

| 模块 | 改动 | 说明 | 状态 |
|------|------|------|------|
| `src/api/db_setup.py` | 加列 | `events.structured_output MEDIUMTEXT DEFAULT NULL`（`_ensure_column` 幂等） | ✅ |
| `src/api/event_store.py` | 修改 | `update_event_complete` 增 `structured_output` 参数 | ✅ |
| `src/runtime/service.py` | 修改 | `complete_task` 传完整 output→structured_output；`report_messages` 落 `events.agent_logs` | ✅ |
| `src/api/routes/issues.py` | 修改 | `_pipeline_from_events` 升为主路径（读新列 output）；`_pipeline_from_archive` 降为回退；`get_issue_events` 返回 segments；tk_ repo 作用域校验 | ✅ |
| `src/api/routes/events.py` | 修改 | 详情页优先读 `structured_output`（回退文件）；仓库事件列表 dual + tk_ 隔离 | ✅ |
| `src/api/event_archive.py` | 修改 | `build_issue_context` 优先查 events（回退文件）；`_locate_by_seq` 保持 | ✅ |
| `src/api/routes/issues.py` | 新增 | `GET .../{number}/events`（Issue 事件摘要） | ✅ |
| `help/api/08-events.md` | 修改 | 补 Issue 事件端点 + 仓库事件列表 dual | ✅ |
| `src/runtime/service.py` / `event_pipeline.py` / `agent.py` | 修改 | **停写 event.json**（init/finalize/append/record_a2a 移除写文件，仅写 DB） | ⏳ 阶段 B |
| `src/worker_package.py` / `runtime/cleanup.py` | 修改 | 移除 event_archive 打包/清理依赖（或标注仅读兼容） | ⏳ 阶段 B |
| 存量补全脚本 | 新增（可选） | 存量 event.json → events 新列后台补全 | ⏳ 阶段 B 可选 |

---

## 6. 测试用例设计（✅ 阶段 A 已实施 12 用例全过）

> 阶段 A 测试已落地：`tests/test_event_structured.py`（5）+ `tests/test_issue_events_api.py`（7）。阶段 B 需增补「停写」用例。

### 6.1 测试文件规划

| 文件 | 覆盖范围 |
|------|---------|
| `tests/test_event_structured.py`（新增） | 结构化产出落库 / 详情页读新列 / 回退 |
| `tests/test_issue_events_api.py`（新增） | 新 API 按 Issue 查询（`#5`/`5`/无数据/鉴权） |
| `tests/test_pipeline_from_events.py`（新增或并入 issues） | 路线图改查库后结构化字段 |
| `tests/test_vod_context_db.py`（新增或并入） | VOD 富上下文改查库等价性 |
| `tests/test_scene_env.py` 等既有 | 回归不破坏 |

### 6.2 用例设计（P0 核心）

| # | 用例 | 前置 | 断言 |
|---|------|------|------|
| ST-01 | `update_event_complete` 写结构化产出 | events 表已有行（seq=8250,vod,completed 开放态） | `structured_output` 列 = JSON，含 `actions/conclusion/artifacts/next_step/issue_summary`；`result` 仍为结论文本 |
| ST-02 | 详情页优先读新列 | 行持有 `structured_output` | `GET /api/events/detail/{seq}` 返回 `archive`（或新结构）来自 events 列，含结构化 output |
| ST-03 | 详情页新列为空 → 回退 event.json | `structured_output` NULL，且存在存量 event.json（含该 seq 段） | 详情仍返回结构化 output（回退路径生效） |
| ST-04 | 新列为空且无文件 | 均无 | 详情不崩，`archive` 不含（或空） |

### 6.3 新 API 用例（P0）

| # | 用例 | 请求 | 断言 |
|---|------|------|------|
| API-01 | 按 issue 返回全部事件段 | `GET /api/issues/{o}/{r}/5/events` | 200；`segments` 按 seq 升序；每段含 `output`（结构化）/`logs`/`status`/`skill` |
| API-02 | 兼容 `#5` 写法 | `.../issues/{o}/{r}?issue=#5` 或 query `issue=#5` | 与 `5` 返回一致（`_extract_num` 兼容） |
| API-03 | 无该 issue 事件 | 随机块号 | 200 `segments: []`（不 404） |
| API-04 | 无鉴权 | 不带 token | 401/403 |
| API-06 | dual：tk_ 会话 token 可访问 | 签发 tk_（repo_key=同仓库）+ GET | 200（Agent 执行期可读事件） |
| API-07 | tk_ 跨仓库拒绝 | tk_ 绑定其他 repo | 403（repo 作用域校验） |
| API-05 | 跨 provider | `github:owner/repo` repo_key | 按 repo 过滤，返回该仓库事件 |

### 6.4 伴随修复用例（P1）

| # | 用例 | 前置 | 断言 |
|---|------|------|------|
| LOG-01 | `report_messages` 落 `events.agent_logs` | worker 上报 messages | `events.agent_logs` 含该消息（task 相关事件）|
| LOG-02 | 详情页「处理记录」展示 | agent_logs 有值 | 详情返回 logs 数组（非恒空）|

### 6.5 路由/兼容用例（P1）

| # | 用例 | 前置 | 断言 |
|---|------|------|------|
| ROUTE-01 | 路线图走 DB 主路径 | events 行 + structured_output | `_pipeline_from_events` 返回含 `output` 结构化字段的 segments |
| ROUTE-02 | 路线图回退 | events 无行、event.json 存在 | 回退文件读（旧行为） |
| VOD-01 | VOD 富上下文查 DB 等价 | 同一 issue 双源（DB + 文件） | `build_issue_context`（DB 版）与文件版内容一致（不含时序字段差异） |

### 6.6 回归清单

- 既有：事件详情 / Issue 路线图 / 上传 / worker 消息上报（`test_event_conversation.py` 等）
- 迁移：`_ensure_column(events, structured_output)` 幂等（SQLite + MySQL 各一）
- 端到端（阶段 A/B 后）：集群 VOD 段 archive 不再空模板（events 表承载）；阶段 B 后 event.json 不再新增

---

## 7. 分步实施

### 阶段 0 + A：✅ 已完成（已部署 `083be623`）

| 步骤 | 内容 | 用例 | 状态 |
|------|------|------|------|
| 0 | 补测试（红） | ST-01~04 / API-01~05 / LOG-01 | ✅ |
| A-1 | `events.structured_output` 列 + `update_event_complete` 写 | ST-01 | ✅ |
| A-2 | `report_messages` 落 `events.agent_logs` | LOG-01 | ✅ |
| A-3 | Issue 事件摘要 API + `08-events.md`（含 dual + tk_ 隔离） | API-01~07 | ✅ |
| A-4 | 详情页/路线图/VOD 改读 DB + 回退 | ST-02/03/04 / ROUTE-01/02 / VOD-01 | ✅ |

### 阶段 B：✅ 已实施（停写 event.json，双写→单写 DB）

**目标**：新事件不再写 event.json 文件，仅写 events 表；读侧保留文件回退（历史兼容）。

| 步骤 | 内容 | 状态 |
|------|------|------|
| B-0 | 测试：STOP-01 init 不建文件 / STOP-02 finalize 不建文件 / STOP-03 读侧 `_load` 兼容 | ✅ |
| B-1 | `event_archive.py` init/append/finalize/record_a2a 改为 log.debug（不再写文件）；保留 `_load`/`load_event_json`/`_locate_by_seq`/`_resolve_event_json_path` 只读 | ✅ |
| B-2 | `worker_package.py` MANIFEST 移除 `event_archive.py`（worker 端已不引用） | ✅ |
| B-3 | 新增 `scripts/backfill_event_archive_to_events.py`（一次性补写存量文件→events，`--dry-run` 支持） | ✅ |
| B-4 | 回归：ST/API/LOG/ROUTE/VOD + event_conversation 均通过（既有失败除外） | ✅ |

---

## 8. 风险与兼容

- **历史 issue**：读侧双通道回退（阶段 B 后仍保留文件读），详情不回退 ✅
- **集群跨节点**：events 表是 MySQL 中心化 → archive 空模板问题根治（写入不再依赖文件系统共享）✅
- **agent 依赖**：prompt 不依赖 event.json 路径（已验证）；Agent 读事件走 issue 事件 API（dual / tk_ 隔离）✅
- **停写风险（阶段 B）**：`event_archive` 只读保留，若发现遗漏写路径可回退写文件（读兼容仍在）
- **部署顺序**：阶段 B 上线前必须确认线上已无依赖文件写入的读路径（历史详情靠读兼容）