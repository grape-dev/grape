# grape CLI 命令参考（iac-qa 专用）

> 来源：cli-code-analysis-spec.md（CLI v0.3.x，216 条契约经 `grape commands` 核对）
> 本文件只列 iac-qa 实际用到的命令子集，不列全量。

---

## 1. 安装与就绪检查

grape CLI 由 worker 包预装。使用前检查：

```bash
which grape          # 验证安装；未装/版本过低 → 按 `references/grape-cli-installation.md` 自动安装/升级
grape --version      # 确认版本（≥0.3.2）
```

> ⚠️ grape 未安装时按 `references/grape-cli-installation.md` 自动安装/升级（不 curl 直连 API）；仍装不上才报障。

---

## 2. 认证与配置

| 项 | 说明 |
|----|------|
| `$GRAPE_SESSION_TOKEN` | 服务端注入，任务级临时 token（`tk_` 前缀），`grape` 自动读取，不需手动 export |
| `$GRAPE_SERVER_URL` | 服务端注入，指向 API 地址 |
| Token 优先级 | `--token` > `GRAPE_SESSION_TOKEN` > `GRAPE_API_TOKEN` > config.json |
| base_url 优先级 | `--base-url` > `GRAPE_BASE_URL` > `GRAPE_SERVER_URL` > config.json > `http://localhost:8000` |

---

## 3. 常用命令（iac-qa 实际用到的子集）

### 3.1 任务上下文读取（共用，执行前必做）

| 命令 | 用途 |
|------|------|
| `grape tasks current --json-out` | 获取分配给自己的任务（执行前必读） |
| `grape issues detail --owner {owner} --repo {repo} --number {n} --json-out` | Issue 全貌（含 stage/事件/pipeline，执行前必读） |
| `grape issues pipeline --owner {owner} --repo {repo} --number {n} --json-out` | Issue pipeline（防重复 A2A 检查） |
| `grape commands [pattern]` | 列出可调用命令（命令发现） |

### 3.2 知识与项目上下文

| 项目 | 说明 |
|------|------|
| 项目知识基线 | **由 prompt 内联注入**（平台服务端维护，Agent 不自取/不自行生成）——需全文读 prompt 标注的 project-info/ 路径 |
| ❌ 不存在的命令 | `knowledge context` / `knowledge content` 契约不存在（`grape knowledge --help` 无此子命令）——不要尝试 |

### 3.3 PR 读取（审查核心）

| 命令 | 用途 |
|------|------|
| `grape git-repo pulls get --owner {owner} --repo {repo} --number {pn} --json-out` | 单 PR 原始信息（title/body/state/head sha） |
| `grape git-repo pulls list --owner {owner} --repo {repo} --state all --json-out` | PR 列表 |
| `grape git-repo pulls files --owner {owner} --repo {repo} --number {pn} --json-out` | PR 变更文件列表与 diff（审查定位） |
| `grape git-repo pulls comments list --owner {owner} --repo {repo} --number {pn} --json-out` | PR 已有行内评论（上次问题修复核对） |
| `grape git-repo commits --owner {owner} --repo {repo} --per_page 20 --json-out` | 提交记录（变更范围核对） |

### 3.4 PR 评论写操作

| 命令 | 用途 |
|------|------|
| `grape git-repo pulls comments create --owner {owner} --repo {repo} --number {pn} --json '{"body":"[qa Agent] ..."}'` | 发 PR 总结评论（**仅支持普通评论 body**） |

> ⚠️ **CLI 契约限制（重要）**：`grape git-repo pulls comments create` 的 body 字段仅支持 `body`，**不支持** `commit_id`/`path`/`position` 行内评论参数；评论 resolve（PUT）命令**不存在**。这两个能力契约未覆盖 → **不 curl 直连补齐**，统一通过最终总结评论输出「已修复/新增问题」清单（问题定位在评论中标注 文件:行号）。

### 3.5 关联 Issue 读写（配置检查）

| 命令 | 用途 |
|------|------|
| `grape git-repo issues get --owner {owner} --repo {repo} --number {n} --json-out` | 关联 Issue 原始信息（确认真实 Issue） |
| `grape git-repo issues comments list --owner {owner} --repo {repo} --number {n} --json-out` | 关联 Issue 评论 |
| `grape git-repo issues comments create --owner {owner} --repo {repo} --number {n} --json '{"body":"[qa Agent] ..."}'` | 发关联 Issue 评论（审查报告——runbook Step 4） |

### 3.6 任务完成与 A2A 交接

| 命令 | 用途 |
|------|------|
| `grape tasks complete --json '{"result_text":"...","next_step":"..."}'` | 自报完成（收尾必做） |
| `grape a2a handoff --json '{"agent":"coding","payload":{...}}'` | A2A 交棒 Coding Agent（发现 Critical/Required 级问题时必做） |

### 3.7 --json body Schema（iac-qa 用到的）

| Body schema | 使用命令 | 必填 | 可选 |
|-------------|---------|------|------|
| `CommentBody` | `pulls comments create` / `issues comments create` | `body` | — |
| `IssueUpdateBody` | `git-repo issues update` | — | `assignee`, `body`, `labels`, `milestone_id`, `state` |
| `A2AHandoffBody` | `a2a handoff` | — | `agent`, `payload` |
| `CompleteBody` | `tasks complete` | — | `actions`, `artifacts`, `conclusion`, `issue_summary`, `next_step`, `result_text`, `task_id` |

### 3.8 A2A payload 标准结构

```json
{
  "agent": "coding",
  "payload": {
    "repository": "$ACCOUNT/$REPO_NAME",
    "account": "$ACCOUNT",
    "issue_number": {真实编号},
    "pr_number": {被审查 PR 编号},
    "context": "{审查问题汇总}"
  }
}
```

- `issue_number` 必填（本 PR 关联的真实 Issue 编号，无法确定则不调用 A2A）；`pr_number` 必填（被审查的 PR 编号）；两者严禁混用；`repository` 与 `account` 必填。
- 分支 A（Request Changes）`agent="coding"`；分支 B（LGTM 已合并）`agent="sre"`，context 改「PR #n 已合并，QA 审查通过，交办部署验证」。

---

## 4. 命令限制与不存在的能力

### 命令存在性核验

iac-qa 引用的命令子集（§3.1-§3.8）经 `grape commands` + `grape <cmd> --help` 动态验证：

| 命令 | 类型 | 验证结果 |
|------|------|---------|
| `grape tasks current` | GET | ✅ 可执行（`--json-out`） |
| `grape tasks complete` | POST | ✅ 可执行 |
| `grape issues detail` | GET | ✅ 可执行（`--owner/--repo/--number/--json-out`） |
| `grape issues pipeline` | GET | ✅ 可执行（`--owner/--repo/--number/--json-out`） |
| `grape git-repo pulls get` | GET | ✅ 可执行 |
| `grape git-repo pulls list` | GET | ✅ 可执行（`--state`） |
| `grape git-repo pulls files` | GET | ✅ 可执行（`--owner/--repo/--number/--json-out`） |
| `grape git-repo pulls comments list` | GET | ✅ 可执行 |
| `grape git-repo pulls comments create` | POST | ✅ 可执行（body: `body`；行内 position 参数不在契约内） |
| `grape git-repo commits` | GET | ✅ 可执行（`--owner/--repo/--per_page/--json-out`） |
| `grape git-repo issues get` | GET | ✅ 可执行 |
| `grape git-repo issues comments list` | GET | ✅ 可执行 |
| `grape git-repo issues comments create` | POST | ✅ 可执行（body: `body`） |
| `grape git-repo issues update` | PATCH | ✅ 可执行（body: `assignee/body/labels/milestone_id/state`） |
| `grape a2a handoff` | POST | ✅ 可执行（body: `agent/payload`） |

> ⚠️ **不存在的命令/能力**（不要引用，不要 curl 直连补齐）：
> - `grape git-repo pulls comments update` / resolve — 不存在；评论已修复状态通过总结评论说明
> - 行内评论（`commit_id`/`path`/`position` 参数）— `pulls comments create` 契约不支持
> - `grape git-repo pulls merge` **存在且本技能使用**（自动合并——LGTM 后幂等合并，见 runbook Step 5 分支 B；冲突/403 → 分支 C 评论等待人工）

### 无 grape api 逃生舱

v0.2.2 移除 `grape api` 通用兜底命令。契约未覆盖的端点无法通过 CLI 调用，不要 curl 直连。

### 不存在的能力（不要尝试）

任务删除（tasks 组仅 complete/current/notify/wait——无删除命令）· 知识上传 · Issue/PR 删除 · 分支创建/删除 · Token 管理 · `tasks create`（已移除，用 `issues create`）· 流式输出 · 交互式提示 · 配置初始化

---

## 5. 退出码

| 码 | 含义 |
|----|------|
| 0 | 成功 |
| 1 | 通用/配置错误 |
| 2 | click 用法错误 |
| ≥400 | HTTP 透传 |
| 130 | Ctrl+C |

---

## 6. 安全与边界

- **token 使用范围**：只用 `$GRAPE_SESSION_TOKEN`（grape CLI 自动读取）。不得从数据库、配置文件、日志、历史中寻找其他 token。
- **禁止访问的内部数据路径**：`data/sessions.db`、`user_accounts` 表、`managed_repos` 表、`configs/` 目录（含 `global-bot-token.json` 等）。`data/workspaces/` 项目知识文档除外。
- **403/401 处理**：命令返回 403 或权限不足时，停止操作并回复「该操作需要管理员/维护者权限，请在 GitCode 仓库设置中手动配置后重试」，不尝试其他 token 或绕行手段。
- **token 不写入文件**：不将 token 写进日志、脚本、Issue 评论或工具调用以外的地方。