# 技能/资源向远端 RuntimeAgent 交付设计（Skill Delivery to Remote Runtime）

> 版本：v1.0
> 日期：2026-08-08
> 状态：待评审
> 前置：`agent-runtime-event-tech-impl.md`（M2/M3 设计）
> 说明：解决「远端 RuntimeAgent 执行时，服务端技能、A2A 地址、历史文件、凭据等 server-local 资源如何随任务交付」的问题。纯设计，不改变现有 skills 体系。

---

## 一、问题背景

### 1.1 本地模式下技能如何注入执行

技能是**服务端本地文件**，三种 scope 落盘位置不同：

| scope | 目录 | 是否进 git |
|---|---|---|
| official | `skills/{name}/SKILL.md` + `references/*.md` | 是 |
| user | `skills/custom/{user_id}/{name}/` | **否**（`.gitignore`） |
| repo | `skills/repo/{repo_key}/{name}/` | **否**（`.gitignore`） |

注入有两条通道，都依赖"文件就在执行机本地"：

1. **Prompt 绝对路径**（`src/prompt_builder.py:301-308`）：构建时写入 `Skill 文件：{SKILLS_DIR}/{skill}/SKILL.md`（服务端绝对路径），指示 Agent 用 read tool 读取。
2. **环境变量**（`src/agent/opencode.py:20`）：`DEVOPS_SKILLS_DIR = SKILLS_DIR`，Agent 据此遍历发现技能（`skills/SKILL-SPEC.md`）。
3. 技能内 `references/*.md` 由 Agent 按相对路径条件读取（如 `issue/SKILL.md` 引 6 个 reference 文件）。

`build_prompt` 在**服务端 enqueue 时**执行，生成的 prompt 是固定字符串，已含服务端绝对路径。

### 1.2 远端失效点清单

| # | 失效点 | 位置 | 后果 |
|---|---|---|---|
| 1 | prompt 内 `Skill 文件：/server/.../SKILL.md` 绝对路径 | `prompt_builder.py:301` | worker 上路径不存在 → read tool 失败 |
| 2 | `DEVOPS_SKILLS_DIR` 指向执行机自身 `skills/` | `opencode.py:20` | worker 若 clone 同仓库有 official；**custom/repo 技能被 gitignore，worker 没有** |
| 3 | **A2A URL 硬编码** `http://localhost:8080/api/a2a` | `prompt_builder.py:336` | worker 上 localhost 指向自身 → 调不通服务端 |
| 4 | **历史文件指引** `{WORKSPACE_DIR}/history/{account}/{repo}/` | `prompt_builder.py:48` | worker 无此目录 |
| 5 | **DB 系凭据注入失效**：`stream_agent` 在服务端读 `get_credentials(user_id)` 注入 env（`executor.py:136-144`）；CodeArts AK/SK 来自服务端 DB 账户（`codearts.py:21-24`） | `executor.py` | worker 的 `data/sessions.db` 为空 → 远端任务拿不到账号/用户凭据 |

> #1/#2 是本问题核心（技能传输）；#3/#4/#5 是**同源的 server-local 依赖**，一并处理，否则远端执行整体不可用。

---

## 二、方案选择

| 方案 | 机制 | 优点 | 缺点 |
|---|---|---|---|
| A 任务内技能包 | enqueue 时服务端打包技能目录进 task；worker 落地本地 + 设 env + 替换 prompt 占位符 | 自包含、每次最新、覆盖三 scope、按权限快照 | payload 变大；每次传输开销 |
| B 全量同步 | worker clone 同仓库 + 服务端 skills 打包端点按注册同步 | 同步一次后本地快 | 陈旧；repo/user 技能泄露给所有 worker；需版本/checksum |
| C Prompt 内联 | 服务端把 SKILL.md+references 全文读入 prompt | 零文件依赖 | prompt 膨胀；多技能+多 reference 场景过大；部分按需条件读文件无法内联 |
| **D 混合（推荐）** | **A 为主** + prompt 路径占位符化 + worker 落地替换 + worker 侧 `(skill, content_hash)` 缓存 | 三 scope 统一、每任务授权快照、一次传输多任务复用、无跨机同步 | 需新列 + 占位符约定 |

---

## 三、推荐方案 D 设计

### 3.1 数据模型变更（`agent_tasks` 新增列）

```sql
ALTER TABLE agent_tasks ADD COLUMN skill_files TEXT DEFAULT '{}';  -- JSON: {skill_name: {rel_path: content}}
ALTER TABLE agent_tasks ADD COLUMN server_url TEXT DEFAULT '';      -- 服务端外呼地址（A2A 用），worker 替换 {{SERVER_URL}}
```

- `skill_files`：enqueue 时服务端按 `resolve_skill_id`/`get_skill_path` 解析实际技能目录，读 SKILL.md + 递归 references，序列化为 `{skill_name: {"SKILL.md": "...", "references/xxx.md": "..."}}`。
- `server_url`：服务端从 meta `webhook_url`（或 `RUNTIME_SERVER_URL`）注入，供 prompt 中 `{{SERVER_URL}}` 替换。
- 凭据不落新列：账号/用户级 env 在 enqueue 时合并进既有 `env_extra` 列（见 §3.3）。

### 3.2 Prompt 占位符约定（`prompt_builder.py` 改动）

`build_prompt` 不再写绝对路径，改为占位符，worker 执行前统一替换：

| 原注入 | 改为 | 替换方 |
|---|---|---|
| `Skill 文件：{SKILLS_DIR}/{skill}/SKILL.md` | `Skill 文件：{{SKILL_DIR}}/{skill}/SKILL.md` | worker |
| `curl -X POST http://localhost:8080/api/a2a` | `curl -X POST {{SERVER_URL}}/api/a2a` | worker |
| 历史文件指引绝对路径 | 远端模式下不注入本地路径（见 §3.4） | 服务端构建时 |

占位符统一格式 `{{UPPER_SNAKE}}`，避免与技能文本冲突。

### 3.3 凭据随任务（不再依赖 worker 本地 DB）

- enqueue 时服务端完成**所有** DB 系 env 注入：
  - `get_credentials(user_id)` 各凭据的 config → `env_extra`（对应现 `executor.py:136-144`）。
  - 账户配置（如 `CODEARTS_CLI_AK/SK`）→ `env_extra`（对应现 `codearts.py:21-24`）。
- worker 端 `stream_runtime` 移除"读本地 DB 注入凭据"逻辑（或改为"仅注入 env_extra 中已带值"，双端兼容）。

### 3.4 历史文件与档案上下文

- 远端模式（`queue_remote`）：**不注入** `WORKSPACE_DIR/history/...` 绝对路径指引。
- 会话上下文改用 event.json 档案（M1 的 `build_issue_context` 富上下文）注入 prompt——服务端构建时已内联为文本，无文件依赖。
- 本地模式（`direct`，`queue_local` 已废除）保持现状，渐进迁移。

### 3.5 worker 执行流程（`src/runtime/agent.py` 内 `_run_task` 前置）

```
1. skill_files = json.loads(task.skill_files) or {}
2. for skill_name, files in skill_files.items():
       dest = os.path.join(cwd, ".skills", skill_name)
       if cache miss by (skill_name, content_hash):
           写入 SKILL.md + references/** → dest
       env["DEVOPS_SKILLS_DIR"] = dest  （多技能时指向父目录 .skills）
3. prompt = task.prompt
       .replace("{{SKILL_DIR}}", os.path.join(cwd, ".skills"))
       .replace("{{SERVER_URL}}", task.server_url or local)
4. env 合并 task.env_extra（含已注入凭据）
5. stream_runtime(prompt, cwd, session_id=task.runtime_session_id, ...) 执行
```

**缓存**：worker 本地 `{cwd}/.skills_cache/{skill}/{content_hash}/`，命中则跳过写盘，仅换软链/复制指针（实现期二选一）。

---

## 四、影响面与改动清单

| 文件 | 改动 |
|---|---|
| `src/api/db_setup.py` | `agent_tasks` 建表加 `skill_files` / `server_url` 列 |
| `src/runtime/service.py` | `enqueue_task`：解析技能目录打包 `skill_files`；注入 `server_url`；DB 凭据合并 `env_extra` |
| `src/prompt_builder.py` | 技能路径/A2A URL 占位符化；历史指引远端模式豁免 |
| `src/runtime/agent.py` | `_run_task` 落地技能包 + 占位符替换 + env 合并 |
| `src/agent/executor.py` | 凭据注入改"仅用 env_extra 已带值"（本地兼容） |
| 测试 | `test_runtime.py`：技能包落地/占位符替换/凭据随任务/多技能 |
| 文档 | `agent-runtime-event-tech-impl.md` 补本设计引用 |

---

## 五、验收要点

| ID | 场景 | 预期 |
|---|---|---|
| SD-1 | 远端任务带 official 技能 | worker 落地 SKILL.md+references，`DEVOPS_SKILLS_DIR` 指向本地，prompt 占位符替换成功 |
| SD-2 | 远端任务带 repo/user 技能（gitignore 目录） | 随任务打包下发，worker 可读，无需跨机同步 |
| SD-3 | 多技能任务 | 各自落地，`DEVOPS_SKILLS_DIR` 指向公共父目录 |
| SD-4 | 同技能重复任务 | 命中 content_hash 缓存，不重复传输 |
| SD-5 | A2A 外呼 | prompt 中 `{{SERVER_URL}}` 替换为服务端可达地址，远端可调 `/api/a2a` |
| SD-6 | 凭据 | worker 无本地 DB 时，CodeArts AK/SK 与 user credentials 仍注入成功 |
| SD-7 | 本地模式回归 | `direct`（本机引擎）下行为与现状一致（占位符在本地也被替换或保持绝对路径） |
