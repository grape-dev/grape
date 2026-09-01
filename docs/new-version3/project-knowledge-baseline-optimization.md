# 项目知识基线（Project-info）现状梳理与优化方向

> 版本：v1.0 ｜ 日期：2026-09-01 ｜ 状态：**梳理 + 方案（未实施）**
> 范围：grape 服务端 Project Knowledge 基线（`project-info`）的**生成、存储、注入、消费**全链路，结合本地 codegraph（`.codegraph`）能力给出优化方向。
> 参考：`docs/new-version1/codegraph-builder-plan.md`、`docs/new-version2/knowledge-board-design.md`、`docs/new-version2/project-knowledge-credentials-v8.md`（已废弃仅留痕）。

---

## 一、现状全景

### 1.1 生成链路（服务端权威）

| 环节 | 实现 | 关键点 |
|------|------|--------|
| 生成器 | `src/project_info/builder.py` `build_project_info(repo_key, token)` | 调 **codegraph CLI**（`explore / files / callees`）+ GitCode API（HEAD sha） |
| 分析副本 | `{ws}/{owner}/{repo}/project-info/codegraph-repo/` | 从 bare mirror `codebase/` `git worktree add` 出非 bare 副本，首次 `codegraph init` 建索引（持久化于副本 `.codegraph/`） |
| 触发 | `maybe_refresh_project_info()` ← `event_pipeline` webhook 事件后台线程 | 每 repo 进程内锁（`_building` set）防并发 |
| 刷新条件 | `should_refresh()` | 缺文件 / `degraded` 且 codegraph 可用 / HEAD sha 变化 / **超 7 天** |
| 产物 | `project-graph.md` / `project-capabilities.md` / `.metadata.json` | 各节 ≤600 字截断；降级时写 `degraded=true` 并保留旧文件 |
| 存储 | 本地磁盘 + **OBS**（`save_bytes` + DB `agent_shared_knowledge` scope=`baseline`，带 `content_hash`） | OBS 写失败只 warn，不阻断 |

### 1.2 消费链路

| 环节 | 实现 | 说明 |
|------|------|------|
| 读取 | `runtime/service.py: load_project_info` | OBS 分支（DB scope=baseline 行 + OBS 读）或磁盘回退；返回 `{graph, caps, meta}` |
| 流水线 prompt | `prompt_builder._build_project_info_section` | 内联截断（`_PROJECT_INFO_MAX_CHARS`）+ `{{PROJECT_INFO_DIR}}` 全文占位符 + degraded 警告 + member/agent 定向目录指引 + VOD 场景注入 |
| 对话 prompt | `_build_knowledge_section` | project+issue+issue_agent 三级合并 + member 摘要 |
| 任务打包 | `enqueue_task` → `project_info_files` | graph/caps/.metadata + `member_files_pi`（全 Agent）+ `agent_files(agent_type)`（定向），key 相对 project-info 目录 |
| 看板 | `knowledge.js` + `/api/knowledge` | 项目级 / Agent 维度双页签、详情、上传、删除、context 预览 |

### 1.3 codegraph 现状（本地工具能力清单，CLI 1.5.0）

`codegraph --help` 子命令 + 本仓 `.codegraph`（572 files / 12,535 nodes / 32,553 edges / FTS5）：

| 命令 | 用途 | 生成器当前是否使用 |
|------|------|------------------|
| `init / index / sync / status / unlock / daemon` | 索引生命周期/增量/排障 | ✅ 只用 `init`（**未用 `sync` 增量**） |
| `explore <query>` | 自然语言区探索（返回符号源码+调用路径） | ✅ 6 个查询 |
| `files --format tree` | 文件树 | ✅ Key Files |
| `callees <symbol>` | 符号被调/调用 | ✅ （有 wrapper，但例子中未实际调用） |
| `callers <symbol>` | 谁调用该符号 | ❌ |
| `impact <symbol>` | 改动影响面（blast radius） | ❌ |
| `query <search>` | 符号 FTS 检索 | ❌ |
| `node <name>` | 单符号定位/文件映射 | ❌ |
| `affected [files]` | 变更影响到的测试文件 | ❌ |
| `install`（含 **Hermes Agent**）| 给 agent 安装 codegraph MCP | ❌ |

> 结论：**生成器只用到 codegraph 1.5.0 能力的 ~1/3**——`explore+files` 就能生成现有基线，说明当前体系是"LLM 文本摘录"路线，未使用 codegraph 的结构化信号（调用链/影响面/符号检索/增量索引/agent 直连）。

---

## 二、问题与短板（分级）

### P0 正确性 / 一级体验

| # | 问题 | 位置/依据 | 影响 |
|---|------|----------|------|
| 1 | **首跑时序竞态**：首次 issue_opened 后台刷新与同步构建 prompt 并发，第一版 prompt 拿不到基线（第二次起才有） | `knowledge-board-design.md §5.4`；`event_pipeline` 后台线程 | 新仓库首个任务"盲跑" |
| 2 | 分析副本强依赖 bare mirror `codebase/`（原依赖别的功能副产物创建） | `builder._ensure_analysis_worktree`（已修复 ensure_repo_base 主动建，但属关键路径） | mirror 缺失 → 反复 degraded |
| 3 | 每次刷新都 `codegraph init` 语义（无增量），大仓库 1–3 min 成本 | `builder`；CLI 未用 `sync` | 刷新慢、占用高 |
| 4 | 并发控制仅进程内锁，多实例部署会重复 init | `_building` set | 水平扩展时索引竞争 |

### P1 覆盖度 / 时效

| # | 问题 | 位置 | 影响 |
|---|------|------|------|
| 5 | `project-capabilities.md` 四节（External Integrations / Skills & Agents / Data Model / Configuration）**硬编码 "N/A"** | `builder._build_capabilities_md` | 能力清单残缺 |
| 6 | 分析分支写死 `main/master`，feature 分支不分析 | builder | 新建分支信息缺失 |
| 7 | 无手动刷新端点（设计方案 B 一直未实现） | 无 `POST .../project-info/refresh` | 运维/管理员无法手动重建 |
| 8 | `knowledge.md` 只追加不清理，长期膨胀 | `knowledge.py: write_knowledge` | 文件/latency 劣化、重复 |
| 9 | 刷新判定依赖"超 7 天 / commit 变化"外部 API（GitCode），弱网/限流下不准 | `should_refresh` | 该刷的新仓库可能长期不刷 |

### P2 消费效率 / 工程化

| # | 问题 | 位置 | 影响 |
|---|------|------|------|
| 10 | 生成器只用 LLM `explore`，未用 codegraph 结构化信号（callers/impact/query/node/affected） | builder | 调用链/依赖/符号定位弱，token 消耗大 |
| 11 | 注入是一次性全段截断（600/`_PROJECT_INFO_MAX_CHARS`），无分层（概览 vs 按需） | prompt_builder | 大仓库 token 预算不可控 |
| 12 | worker 上的 agent 只能"读打包文件"，无法在任务内精确检索符号（无 codegraph MCP） | runtime agent | 认知成本、回溯能力弱 |
| 13 | 无生成观测指标（成功率/耗时/降级率/索引大小） | builder | 无从度量治理 |
| 14 | codegraph 版本 1.5.0（1.6.0 已发布）；索引/daemon 排障无 runbook | `.codegraph` | 滞后/被动排障 |

---

## 三、优化方向（结合 codegraph 能力）

### A. 生成质量：从"LLM 文本摘录"升级为"结构信号 + 文本摘要"混合

1. **调用链/依赖用真实图数据**：`project-graph.md` 的 Core Call Chains、Module Dependency 改为由 `codegraph callees/callers/impact` 得出（入口符号 → 二三层调用 + 影响面），再让 LLM 只做"归纳摘要"，避免幻觉式依赖关系。
2. **Key Files / 符号定位用 `node/query`**：用 `codegraph query "main entry|api|route|config"` 与 `node` 定位热点符号（高 fan-in），替代纯 `files` 树猜测。
3. **补齐 capabilities 四节**：`query('integration|sdk|OBS|webhook')`、`configs/` + `query('config')`、`agent_type/skill` 表、数据表 `tables-overview` 目录 → 生成 External Integrations / Config / Data Model / Skills-Agents。
4. **新增"符号索引"产物（可选）**：`codegraph query` 导出符号清单（名称+文件+FTS），生成 `project-symbols.md`（或 ndjson）随 `project_info_files` 下发；Agent 检索符号名时精确命中，替代整图阅读。

### B. 时效与成本：索引增量 + 触发完善

5. **增量刷新**：`should_refresh` 判定 HEAD 变化后优先 `codegraph sync`（增量，秒级）而非 `init`；仅首次/索引损坏才 `init`。为活跃仓库维护常驻索引目录，跨事件复用。
6. **首跑竞态修复**：issue_opened 且基线缺失时，事件执行前做**有界等待**（如等待刷新就绪 ≤60s，超时则按"无基线降级"继续并回填），保证首任务也有基线可用。
7. **多实例锁**：`should_refresh`/`build` 前加 meta 表租约键（`project_info_building` + TTL），解决多实例重复 init。
8. **手动刷新端点**：新增 `POST /api/repos/{owner}/{repo}/project-info/refresh`（成员/管理员），配合 `?force=1` 与队列。
9. **分支覆盖**：分析最近活跃分支（GitCode branches API）而非写死 main/master；对新分支增量 sync。

### C. 消费效率：分层注入 + 可检索

10. **分层注入**：prompt 只内联"概览（≤~1.5k 字）+ 符号索引摘要 + commit 版本"，`{{PROJECT_INFO_DIR}}` 全文按需 read tool；catalog 更新后 prompt 用 `baseline sha` 版本号标注，任务日志记录"使用基线版本"便于溯源。
11. **knowledge.md 治理**：按 `source_seq` 去重；超 N 条滚动归档（如按季度文件）。
12. **worker/agent 直连 codegraph**（重点）：`codegraph install` 已内置 **Hermes Agent** 目标——worker 上为 hermes 安装 codegraph MCP，任务内直接 `explore/query/impact` 当前 `{{PROJECT_INFO_DIR}}`/仓库 worktree，做到"按需精确检索"，减少全量打包与 LLM 通读。

### D. 与本地 `.codegraph` 协同 / 工程化

13. **自举验证**：grape 自身 `.codegraph`（572 文件/12.5k 节点/FTS）就是生成器验证样例；builder 内置"预置问题清单"（main entry / module imports / API endpoints / tech stack / features），并支持按 repo 追加 query（`configs/project-info-queries.json`）。
14. **版本与排障**：`codegraph upgrade` 到 1.6.0；`status / unlock / daemon` 纳入运维 runbook（`.codegraph` 索引不入 git；daemon 卡死用 `unlock`）。
15. **QA 联动**：`codegraph affected <PR改动文件>` 产出"受影响测试文件" → QA Agent 审查范围提示（与事件注入联动）。
16. **观测指标**：builder 上报生成耗时 / 成功·降级率 / 索引大小 / `_PROJECT_INFO_MAX_CHARS` 命中率；meta 增加 `graph_version + engine_version`。

---

## 四、落地路线（建议顺序）

| 优先级 | 内容 | 对应优化 | 目的 |
|--------|------|---------|------|
| **P0** | 首跑竞态有界等待；`codegraph sync` 增量替代 init；meta 表跨实例锁 | B5/B6/B7 | 基线"首任务可用 + 快且不重复生成" |
| **P0** | 补齐 capabilities 四节 + 手动 refresh 端点 | A3/B8 | 能力清单完整、可运维 |
| **P1** | 结构化调用链/依赖（callees/callers/impact）+ 符号索引产物 | A1/A2/A4 | 基线更可信、可检索 |
| **P1** | 分层注入 + baseline sha 版本标注 + knowledge.md 归档 | C10/C11 | 控制 token、可审计 |
| **P2** | worker/hermes 安装 codegraph MCP；affected→QA | C12/D15 | Agent 内精确检索 |
| **P2** | 观测指标 + 自举问题清单 + codegraph 升级/排障 runbook | D13/D14/D16 | 治理与落地配套 |

> 各优化项落地前需配套测试（`tests/test_project_info_builder.py` 增加 codegraph CLI 契约 + sync 增量 + 有界等待用例），并全量回归 prompt/打包链路。

---

## 五、结论

- 生成链路（服务端权威 + codegraph CLI + OBS 双写 + 事件触发）已成型，**核心缺陷集中在"首跑时序、无增量、并发锁、能力覆盖"四类**；
- codegraph 能力利用不足是当前最大的结构性优化空间：从"LLM 文本摘录"转向"**图数据+全文检索+结构化产物+Agent 直连**"，可同时提升基线质量、生成成本与 Agent 使用效率；
- 优先落地 P0（首跑等待、增量 sync、跨实例锁、capabilities 补全、手动刷新），再按 P1/P2 推进结构与工程化增强。

*本文为梳理与方向建议，具体落地按 P0→P2 分期评审执行。*