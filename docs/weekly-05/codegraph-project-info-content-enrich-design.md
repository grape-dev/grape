# 方案：提升 codegraph project-info 生成内容量与覆盖度

> 状态：技术方案（2026-09-30）｜分支 weekly-05-lyz
> 现象：生产环境 codegraph 生成的 project-graph.md / project-capabilities.md 内容过少，Agent 对项目信息理解不够。
> 目标：在不重构整体链路的约束下，**立即可提升产出内容量**（Phase 1），并列出可选的结构化信号增强（Phase 2，后置）。
> 参考：`docs/new-version3/project-knowledge-baseline-optimization.md`（现状梳理 + 方向，未实施）、`docs/new-version1/codegraph-builder-plan.md`。

---

## 一、现状与根因（生产代码核对 2026-09-30，`src/project_info/builder.py`）

### 1.1 内容量受限点（按影响从大到小）

| # | 问题 | 位置 | 影响 |
|---|------|------|------|
| 1 | **每节 600 字硬截断**：`arch[:600]`、`modules[:600]`、`chains[:600]`、`key_files[:600]`（graph）；`tech[:600]`、`features[:600]`、`api[:600]`（caps） | builder.py L259-263 / L272-274 | 大仓库每节只剩 600 字摘要；**文件树也被 600 截断** → 几十/上百文件的项目 Key Files 几乎报废（最严重） |
| 2 | 截断**无标记**：`[:600]` 静默切段，无 `...(截断)` 提示 | builder.py L259-274 | Agent 误以为内容完整，不会主动 `grape knowledge detail` 拉全量 |
| 3 | **capabilities 四节硬编码 "N/A"**：`integrations/skills_agents/data_model/config="N/A"` | builder.py L275 | 能力清单残缺一半 |
| 4 | **探索查询少且浅**：仅 6 条 `explore`（max-files 8~15），未用 `query / callers / callees / impact / node` | builder.py L327-354 | 架构/依赖/入口多为 LLM 摘要，无真实调用链与符号定位 |
| 5 | `_codegraph_callees` wrapper 存在但**从未调用**；`codegraph files` 未用 `--max-depth / --filter / --pattern` | builder.py L247-249 / L242-244 | 能力浪费 |
| 6 | **分支写死** `["main", "master"]` | builder.py L358 | feature 分支体系完全缺失 |

> 注：**prompt 不再内联基线是设计使然**（#219 Option B，commit `1e0118d5` 移除注入），非缺陷；Agent 需主动获取 project-info，见 §1.2 消费路径。

### 1.2 消费路径确认（设计对齐，2026-09-30 核对）

| 消费路径 | 状态 | 说明 |
|----------|------|------|
| `grape knowledge list` + `grape knowledge detail <id>` | ✅ **设计主路径** | `/api/knowledge` 列表不过滤 scope，baseline 三条（`project-graph.md`=项目结构图谱 / `project-capabilities.md`=项目能力清单 / `.metadata.json`=项目元数据，`source=system`）均可列出；`detail` 经 `read_entry_content` 返回**全量 content**（不截断） |
| 知识看板 UI | ✅ | 项目级页签展示 graph/caps 全文 |
| `.project-info/` 物化到 worker/direct 工作目录 | ⚠️ 存量保留 | 仍随任务下发（`collect_knowledge_files`），但 prompt 已无 `{{PROJECT_INFO_DIR}}` 占位符 → replace 是空操作，**Agent 无法从 prompt 得知该目录存在**；不属设计主路径 |
| prompt 内联注入（`_build_project_info_section`） | ❌ 已下线 | prompt_builder.py:80 仍有定义，但生产路径无调用点（仅测试与 vod_handler 一条注释引用），随 #219 停用 |

> 结论：**提升基线内容量即提升 Agent 可获取的信息量**——`grape knowledge detail` 拉的是生成文件全量，无二次截断；本方案只需把「生成侧」做厚，消费侧（grape knowledge）无需改动。

### 1.3 次要/结构性问题（本次不处理，记录）

- 每次刷新都是 `codegraph init` 语义（非 `sync` 增量），大仓库 1-3 min；
- 多实例并发仅进程内锁（`_building` set），跨实例会重复 init；
- 首跑时序竞态：首次 issue_opened 后台刷新与同步构建 prompt 并发，第一版 prompt 无基线；
- meta 无生成观测（耗时/成功·降级率/engine 版本/每节字数）。

---

## 二、优化方案 Phase 1（立即可落地，改动集中在 builder 一个文件；消费侧 grape knowledge 无需改动）

### 2.1 截断上限改为常量 + 环境可调（对症 #1/#2）

模块顶部新增：

```python
import os as _os
# 各节内容上限（env 可调，便于按库/部署调节 token 预算）
_SECTION_CAP = int(_os.environ.get("PROJECT_INFO_SECTION_CAP", "3000"))
_KEY_FILES_CAP = int(_os.environ.get("PROJECT_INFO_KEY_FILES_CAP", "8000"))
```

- 结构/摘要节（architecture/modules/chains/tech/features/api）用 `_SECTION_CAP`（600 → 3000）；
- **Key Files 文件树**单独用 `_KEY_FILES_CAP`（600 → 8000），它是结构化信号，最值钱；
- 抽出截断 helper，统一带 `...(已截断，完整见同名知识 `grape knowledge detail`)` 标记，Agent 可感知并有明确获取路径（消费主路径 §1.2）：

```python
def _cap(text: str, limit: int) -> str:
    text = (text or "").strip()
    if not text:
        return "N/A"
    if len(text) > limit:
        return text[:limit] + "\n...(已截断，完整内容用 `grape knowledge detail` 获取)"
    return text
```

### 2.2 补齐 capabilities 四节（对症 #3）

在 `build_project_info` 探索序列新增 3 条 `explore` + 1 次 `files` 目录探测，替换 L275 的硬编码：

| 模板节 | 数据来源 | 建议查询 / 手段 |
|--------|----------|------------------|
| External Integrations | explore | `"external integrations third-party sdk api webhook oauth sso"`（max-files 8） |
| Data Model | explore | `"data model entities database tables orm schema migration models"`（max-files 8） |
| Configuration | explore | `"configuration settings env vars feature flags config"`（max-files 8） |
| Skills & Agents | files 目录探测 | `codegraph files --filter skills/`、`--filter agents/`、`--filter .github/` 等，命中才填内容；无命中保留 "N/A"（不硬编码假数据） |

> 说明："Skills & Agents" 是 Grape 特有概念，通用仓库大概率无此内容——**探测命中才填，未命中保留 N/A 是合理行为**，与当前"永远 N/A"的差异在于命中时能产出真实信息。探测用 `_codegraph_files_section(work, dir)` 封装 `files --filter <dir> --max-depth 3`。

### 2.3 扩展 graph 探索查询（对症 #4）

在现有 6 条基础上新增 4 条，锁续期间隔保持"每两步 renew"不变（Issue #103 机制已覆盖）：

| 新增节/内容 | 建议查询 | max-files |
|-------------|----------|-----------|
| 后台任务/定时任务 | `"background jobs scheduled tasks cron workers queue"` | 8 |
| 错误处理/日志/观测 | `"error handling logging observability middleware"` | 8 |
| 认证/权限/安全 | `"authentication authorization security guard middleware"` | 8 |
| 部署/CI-CD | `"deployment build ci cd dockerfile container k8s"` | 6 |

> 查询列表收敛为模块常量 `_EXPLORE_QUERIES: list[tuple[str, int]]`，后续可平滑迁移到 `configs/project-info-queries.json`（new-version3 §D13 预留）。

### 2.4 Key Files 改用限深文件树（对症 #4/#5）

`_codegraph_files` 增加 `--max-depth 5 --filter 排除测试/构建产物`（复用 `files --filter` 能力），避免整树溢出 + 信息密度更高：

```python
def _codegraph_files(work: str, max_depth: int = 5) -> str:
    code, out, err = _run(
        ["codegraph", "files", "--format", "tree", "--max-depth", str(max_depth)],
        cwd=work, timeout=120)
    return out or err or ""
```

### 2.5 分支真实化（对症 #6）

- 用 GitCode/GitHub branches API 拉取真实分支列表写入 `meta["branches_analyzed"]`（复用 L293-308 已有 `urllib` + Repo token 模式），失败回落 `["main", "master"]`；
- `_ensure_analysis_worktree` 的 checkout 保持 `origin/HEAD → origin/main → origin/master`（已适用默认分支）；
- 若 API 返回默认分支名，顺带写入 meta（如 `default_branch`），供 prompt 标注。

### 2.6 消费侧：不改（design 确认）

- **不做**「prompt 内联摘要 400→1000」：prompt 不再注入基线是 #219 的设计决定（Agent 主动获取，避免 prompt 膨胀），本方案不推翻；
- **不做**「恢复 `_build_project_info_section` 注入」：消费主路径是 `grape knowledge list/detail`（§1.2），生成文件变厚即等效 Agent 信息变多；
- 配套（可选，另单走）：`skills/vod/vod/SKILL.md:47` 仍在写「内联注入」——若消费侧统一切到 grape knowledge，技能文档需同步更新描述（skill 仓库单独立项）。

### 2.7 观测 meta 字段（轻量，配合后期治理）

`.metadata.json` 增加：`graph_version`（本方案版本号，如 2）、`engine_version`（`codegraph --version` 输出）、`sections_char_counts`（每节字数，用于判断是否又触顶被截断）、`build_ms`。

---

## 三、Phase 2 候选（后置，本次不做）

从"LLM 文本摘录"升级为"**结构化信号 + 文本摘要**"，对齐 new-version3 优化文档 A1/A2/A4：

| 项 | 做法 | 价值 |
|----|------|------|
| 真实调用链 | `callees <入口symbol> -l` / `callers <热点symbol>` / `impact <symbol>` 产出三层调用链 + 影响面，LLM 只做归纳 | 依赖关系不靠幻觉 |
| 符号定位 | `codegraph query "main|api|route|config"` + `node` 定位高 fan-in 符号 | Key Files 从"猜"变"算" |
| 符号索引产物 | `codegraph query` 导出符号清单 → `project-symbols.md` 随 `project_info_files` 下发 | Agent 精确检索符号名 |
| 增量刷新 | `should_refresh` 判定 HEAD 变化后优先 `codegraph sync` 而非 `init` | 大仓库刷新从分钟级降到秒级 |
| 手动刷新端点 | `POST /api/repos/{owner}/{repo}/project-info/refresh?force=1` | 运维可控 |

> 若上线 Phase 2，需配套 `tests/test_project_info_builder.py` 增加 codegraph CLI 契约用例（sync 增量 / callees 输出映射）。

---

## 四、独立清理项：知识物化链路退役（后置，单独 MR）

> 与本方案 Phase 1（内容量提升）**解耦**，单独立项、单独 MR。收益：任务包变小、worker/direct 物化耗时降低、消除死代码。

### 4.1 现状（2026-09-30 核对：占位符替换已是空操作）

| 位置 | 代码 | 状态 |
|------|------|------|
| worker 物化 | `runtime/agent.py:1517,1527` `prompt.replace("{{...DIR}}", dir)` | 占位符已不存在于任何 prompt → **空操作**，仅剩落地 `.project-info/` / `.knowledge/` 文件 |
| direct 物化 | `event_pipeline.py:2295,2306` `_materialize_direct_workdir` L2278-2306 | 同上，文件落 ws 但无 prompt 告知 Agent |
| VOD direct | `im/vod_handler_async.py:313-316` | 注释仍写"`_build_project_info_section` 生成占位符"（过时，#219 已移除），replace 空操作 |
| 测试锁定新设计 | `test_prompt_builder.py:388` `"{{KNOWLEDGE_DIR}}" not in prompt`；`test_project_info.py:145` `"{{PROJECT_INFO_DIR}}" not in prompt` | 设计即"prompt 不携带知识目录指引" |

### 4.2 物化内容不止 baseline（三源合流）

`collect_knowledge_files`（service.py:513）把三源打包进 `agent_tasks.project_info_files`：

| 源 | 落点 | source |
|----|------|--------|
| baseline（graph/caps/meta） | `.project-info/` | system |
| **member 用户上传知识**（`member_files_pi` 并入） | `.project-info/member/` | manual |
| agent 沉淀知识 | `.knowledge/` | agent |

> **退役必须三件套一起评估**，不能只摘 baseline——否则连带丢失 member/agent 知识。

### 4.3 覆盖性：grape knowledge 主路径可承载

baseline/member/agent 三源均落 `agent_shared_knowledge` + OBS，`grape knowledge list/detail` 均可取（§1.2 已验证 detail 返回全量 content）→ 退役物化后 Agent 获取知识的能力不降。

### 4.4 退役前置确认清单（grep 后清零再动手）

1. 全库 skills/脚本确认无 `read .project-info/` / `.knowledge/`（目前仅命中 `skills/vod/vod/SKILL.md:47` 一处过时描述，属技能仓库单独立项）；
2. `agent_tasks.project_info_files / knowledge_files / *_file_urls` 及 OBS `generate_file_urls` 链路，确认除 agent 物化外无其他消费者（知识看板/详情走 API，不依赖物化）；
3. 一并清残留死代码：`_build_project_info_section`（prompt_builder.py:80，仅测试/注释引用）、`knowledge.py:819/871` 已标"[#95 废弃]"的 `member_files_pi`/`agent_files` 入口；
4. 文档同步：`agent-fs-isolation-design.md` 中 `.project-info/` 白名单条目（不物化后自然失效）、`vod_handler_async.py:311` 过时注释。

### 4.5 验收

- worker/direct 执行任务后 cwd 不再出现 `.project-info/` / `.knowledge/`；
- `grape knowledge list/detail` 返回 baseline+member+agent 全量（对照退役前）；
- 全量回归 `pytest tests/`（`test_knowledge_board` / `test_project_info` 需同步调整断言）。

---

## 五、影响面

| 项 | 说明 |
|----|------|
| 代码改动 | `src/project_info/builder.py`（截断 helper、新增查询、files 限深、分支 API、meta 观测）；**不动 prompt_builder.py**；不新增第三方依赖 |
| 生成耗时 | +4 条 explore（每条约 2-10s）与 files 限深查询，总耗时可接受；锁续期机制已覆盖（Issue #103） |
| 产物体积 | project-graph/capabilities 变大（约 600→3000/节 + 文件树 8000），OBS 单对象仍远低于限制；`grape knowledge detail` 拉全量，无额外 token 开销（按需拉取） |
| 既有测试 | `tests/test_project_info_builder.py` 的 mock 签名 `(work, q, max_files=10)` 不变（不加参数、不改签名），可兼容；新增分支/meta 断言用例 |
| 降级路径 | codegraph 不可用 / worktree 失败路径不动，仍走 degraded 兜底 |
| 兼容性 | `_build_capabilities_md` 签名保持不变（仍 5 参），内部 N/A 替换为实数据 |

---

## 六、验证

1. 对 grape 自仓触发 `build_project_info("gitcode:grape-dev/grape")`（或测试仓）：
   - project-graph.md 各节 >600 字、文件树 >8KB、超限时带 `...(已截断)` 标记；
   - capabilities 四节不再全 N/A（skills/agents 未命中时可 N/A）；
   - `meta.branches_analyzed` 为真实分支（非写死 main/master）。
2. 消费侧验证：`grape knowledge list --repo ...` 可列出 baseline 三条，`detail <id>` 返回全量新内容。
3. 单测补充（沿用现有 mock 风格）：
   - `_cap` 截断 >限 → 带标记；≤限 → 原样；
   - capabilities 四节 mock 返回非空 → 非 N/A；
   - `should_refresh` 不受影响（无签名变化）。
4. 全量回归 `pytest tests/`（知识列表/打包链路）。已按此方案实施后，走 grape-dev-workflow 提 MR 到 weekly-05。

---

## 七、落档修订说明

- 本方案聚焦"内容量"，未含 new-version3 文档的架构级重构（sync 增量 / MCP / 手动端点），列为 Phase 2；
- **设计对齐（2026-09-30）**：确认 prompt 不再内联基线为 #219 设计决定，消费以 `grape knowledge list/detail` 为主路径；故取消原"prompt 内联 400→1000"与"恢复注入"两项，生成侧做厚即可；
- **独立清理（2026-09-30）**：`.project-info/` / `.knowledge/` 物化链路退役列为 §四 独立 MR，与内容量提升解耦；
- 行号以合并 weekly-05 后实测为准：builder 截断 L259-274、N/A L275、探索序列 L327-354、分支写死 L358。