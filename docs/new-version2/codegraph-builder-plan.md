# 服务端 Project-Info 生成器实现方案（打通 codegraph 基线）

> 版本：v1.0
> 日期：2026-08-11
> 状态：待评审
> 前置：`multi-issue-isolation-design.md` §2.4（project-info 服务端权威）、§2.6（生成职责上收）；`skills/vod/references/codegraph-project-analysis.md`（生成指南）
> 触发背景：agent 日志持续出现「该基线为降级生成（codegraph 不可用）」——codegraph CLI 已安装（v1.5.0），但服务端**无任何调用 codegraph 生成 project-info 的实现**，现有基线是旧的手工降级产物（`.metadata.json.degraded=true` 从未刷新）。

---

## 〇、现状与问题

### 现状

| 项 | 状态 |
|---|---|
| codegraph CLI | 已安装 `/usr/local/bin/codegraph` v1.5.0（服务器与 worker 均有） |
| project-info 文件 | 存在，但 `.metadata.json` 标记 `degraded: true`（Aug 4 手工生成，注明 "codegraph unavailable, generated from README + GitCode API"） |
| prompt 注入 | `prompt_builder._build_project_info_section`（79-109）**只读取**已生成文件；读到 `degraded` → 注入降级警告 |
| **服务端生成器** | **不存在**：全代码库无调用 codegraph CLI 的逻辑（`codegraph init`/`explore`/`files`） |
| 触发机制 | 无：issue_opened/updated 时不会重新生成 project-info |

### 根因

`codegraph-project-analysis.md` 声明"职责上收为服务端统一维护（builder 执行）"，但**服务端没有实现 builder**，Agent 按指南不再自行生成 → 只能使用旧降级基线 → 降级标注永久保留。

---

## 一、目标

1. 服务端实现 **project-info 生成器**：调用已安装的 codegraph CLI + GitCode API，按 `codegraph-project-analysis.md` 流程生成 `project-graph.md` / `project-capabilities.md` / `.metadata.json`；
2. 建立**触发机制**：issue_opened / issue_updated 时检查更新条件（commit 变化 / 超 7 天 / 文件缺失），需要则重新生成；
3. 生成成功写回 `degraded=false`；失败按降级方案标注 `degraded=true` 并记录原因；
4. 生成的基线仍走现有 `load_project_info` / `prompt_builder` 注入链路（**不改变下游**）。

---

## 二、方案设计

### 2.1 新增模块 `src/project_info/builder.py`

服务端专用生成器，独立模块（不依赖 agent 执行上下文）。

```python
"""服务端 project-info 生成器（调用 codegraph CLI + GitCode API）。

见 docs/new-version2/codegraph-builder-plan.md：职责上收的实现落地。
"""
def build_project_info(repo_key: str) -> dict:
    """生成/更新某 repo 的 project-info，返回 {ok, degraded, files_written, reason}。"""
```

**内部步骤**（对齐 `codegraph-project-analysis.md` Step 3-7）：

| 步骤 | 实现 |
|---|---|
| 检查 codegraph 可用性 | `shutil.which("codegraph")` + `codegraph --version` |
| 准备分析副本 | 确保 `project-info/codegraph-repo/` 非 bare 工作副本（从 `.../codebase/` bare mirror checkout 最新 main/master） |
| 获取分支列表 | `GET /repos/{owner}/{repo}/branches?per_page=50`（GitCode API） |
| 分析分支 | 最多 3 个：main/master + 最近活跃 2 个 |
| 项目结构探索 | `codegraph_files --format tree`（在分析副本上执行） |
| 核心架构探索 | `codegraph_explore --query ... --maxFiles 10`（多次，见指南 §4.2） |
| 调用链追踪 | `codegraph_callees --symbol ... --depth 3` |
| 生成 project-graph.md | 按指南 §Step5 模板，≤3000 字 |
| 生成 project-capabilities.md | 按指南 §Step6 模板，≤3000 字 |
| 写 `.metadata.json` | `{last_updated, branches_analyzed, last_commit_sha, graph_version, degraded, note}` |

**执行前提**：
- **目录约定（三目录分离，互不干扰）**：
  - bare mirror：`WORKSPACE_DIR/{owner}/{repo}/codebase/`（已有，`repogit.repo_base_dir`，供 per-Issue worktree 派生）；
  - **codegraph 分析副本**：`WORKSPACE_DIR/{owner}/{repo}/project-info/codegraph-repo/`（生成器专用的非 bare clone/checkout，codegraph 在此建 `.codegraph/` 索引；生成器不触碰 bare mirror 与 per-Issue worktree）；
  - project-info 产物：`WORKSPACE_DIR/{owner}/{repo}/project-info/{project-graph,project-capabilities}.md + .metadata.json`（生成结果写回此目录，供 `load_project_info` 读取）。
- 分析副本从 bare mirror checkout（`git --work-tree` 或 clone），用仓库级 bot token（`BOT_GITCODE_TOKEN`）调 GitCode API；
- codegraph init 可能需要 1-3 分钟，索引持久化于分析副本 `.codegraph/`（首次后无需重复）。

### 2.2 触发机制

**方案 A（推荐）：webhook 事件触发** —— 在 `event_pipeline.run_pipeline` 的 issue_opened / issue_updated 分支调用：

```python
# event_pipeline.py run_pipeline 中，issue_opened / issue_updated 时
from src.project_info.builder import maybe_refresh_project_info
maybe_refresh_project_info(repo_key)
```

`maybe_refresh_project_info` 内部判断更新条件（`load_project_info` 的 meta）：
- 文件缺失 → 生成；
- `last_commit_sha` ≠ 当前 main HEAD → 重新生成；
- 距 `last_updated` > 7 天 → 重新生成；
- 满足任一条件才执行（避免每次事件都跑 codegraph）。

> ⚠️ **异步化**：codegraph 生成耗时（1-3 分钟），不得阻塞 webhook 响应。用 `threading.Thread` 或后台任务触发，webhook 立即返回（与现有 `_enqueue_pipeline_task` 模式一致）。

**方案 B（补充）：手动/管理端点** —— `POST /api/repos/{owner}/{repo}/project-info/refresh`（管理员触发），供手工重建。

### 2.3 降级与失败处理

| 场景 | 处理 |
|---|---|
| codegraph 未安装/`init` 失败 | 按指南降级：README + GitCode API 生成基础信息，`degraded=true`，`note` 记录原因 |
| 单次 explore 超时 | 减小 `maxFiles` 重试或跳过该查询 |
| 分支列表获取失败 | 仅分析当前分支 |
| 生成成功 | `degraded=false`，`note` 清空 |
| 生成失败但已有旧文件 | 保留旧文件，仅更新 `last_updated`（避免降级恶化） |

### 2.4 与现有链路集成（零下游改动）

- 生成结果写回 `WORKSPACE_DIR/{owner}/{repo}/project-info/` → 现有 `load_project_info`（service.py:116）自然读取；
- `prompt_builder._build_project_info_section`（79-109）读到 `degraded=false` → 不再注入降级警告；
- **Agent 侧零改动**（vod/issue-advance 已按"服务端权威"设计）。

---

## 三、改动点清单

| # | 文件 | 改动 | 说明 |
|---|---|---|---|
| 1 | `src/project_info/builder.py`（新增） | `build_project_info` / `maybe_refresh_project_info` / `_should_refresh` | 核心生成器 |
| 2 | `src/project_info/__init__.py`（新增） | 空 init | 模块 |
| 3 | `src/api/event_pipeline.py`（run_pipeline） | issue_opened/updated 分支调用 `maybe_refresh_project_info`（后台线程） | 触发机制 |
| 4 | `src/api/routes/repos.py`（新增端点，可选） | `POST /project-info/refresh` 管理员手动触发 | 方案 B |
| 5 | `tests/test_project_info_builder.py`（新增） | mock codegraph CLI + GitCode API，验证生成/降级/触发 | 测试 |
| 6 | `skills/vod/references/codegraph-project-analysis.md` | 更新为"服务端已实现，Agent 无需自行生成"（标注实现状态） | 文档对齐 |

---

## 四、关键实现注意

1. **工作副本（固定目录）**：codegraph 需在非 bare 工作副本执行。固定使用 `WORKSPACE_DIR/{owner}/{repo}/project-info/codegraph-repo/`（生成器专用），从 bare mirror（`.../codebase/`）checkout，**不触碰** bare mirror 与 per-Issue worktree（`.../{agent}/issues/{n}/`）。三目录分离避免 codegraph init 与 worktree/镜像互相干扰。
2. **token 注入**：GitCode API 调用用仓库级 bot token（`get_gitcode_token`），不得写入文件/日志。
3. **并发控制**：同一 repo 同时只允许一个生成任务（防重复 codegraph init 竞争），可用 meta 表 `project_info_building` 标记或进程内锁。
4. **幂等**：`maybe_refresh_project_info` 满足条件才生成；生成过程可重复执行不破坏已有文件。
5. **worker 一致性**：远端 worker 不生成（职责上收服务端），其 `cwd` 只有 `{{PROJECT_INFO_DIR}}` 落地副本（服务端随任务下发，现有 `service.py:113-155` 已实现）。

---

## 五、验证方案

1. **单元测试**（`test_project_info_builder.py`）：
   - mock `codegraph` 命令输出 → 生成 project-graph/capabilities 文件 + `.metadata.json degraded=false`；
   - mock codegraph 不可用 → 走降级，`degraded=true`；
   - `_should_refresh`：commit 变化 / 超 7 天 / 文件缺失 → True；否则 False；
   - 触发：issue_opened 事件 → 后台触发生成（不阻塞 webhook 返回）。
2. **端到端**：在服务器对 `gitcode-pr-assistant` 手动触发生成 → 确认 `.metadata.json.degraded=false` → 下一个 agent 事件 prompt 不再出现"降级生成"警告。
3. **回归**：既有 `test_prompt_builder.py` / `test_runtime.py` / `test_issue_lifecycle_routing.py` 全量通过（下游注入链路零改动）。

---

## 六、风险与成本

| 项 | 说明 |
|---|---|
| codegraph 生成耗时（1-3 分钟） | 后台线程触发，不阻塞 webhook；首次为每个新仓库代价，后续增量 |
| codegraph init 磁盘/内存 | 仓库大时 init 慢/占资源；可限制分析分支数、设置超时 |
| 生成失败 | 保留旧文件 + degraded 标注，不阻塞事件处理 |
| 与现有 repogit 工作副本协调 | 明确用 bare mirror 或独立 clone，避免与 per-Issue worktree 冲突 |

---

## 七、结论

- **根因**：职责上收为服务端维护，但服务端**无生成器实现**，旧降级基线从未刷新；
- **修复**：实现 `src/project_info/builder.py`（调 codegraph CLI + GitCode API）+ webhook 触发（issue_opened/updated 后台刷新）；
- **收益**：agent 拿到真实 codegraph 基线（架构图/能力/调用链），消除"降级生成"警告，提升需求分析与开发质量；
- **兼容**：生成结果沿用现有 `load_project_info`/`prompt_builder` 注入链路，Agent 侧与下游零改动。

*本文为实现方案，待评审后实施。*
