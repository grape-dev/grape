# Grape CLI 参考（QA-CLI 版）

> 本技能**只使用 `grape` CLI**，统一使用系统注入的 **`$GRAPE_SESSION_TOKEN`**（任务级临时，随任务/会话失效）即可操作。
> 详细指南见 `grape-cli-guidance` 技能（命令分组 / token 作用域 / 错误排查 / 反模式），本文件是 QA 审查实际用到的命令与要点。

## 核心原则（先读）

1. **命令是契约驱动的**：`grape <group> <verb> [--参数]` 由服务端 `/openapi.json`（或内置快照）动态生成。**不确定命令 → 先发现，别猜名字。**
2. **token 决定你能做什么**：`GRAPE_SESSION_TOKEN`（`tk_`）= 会话级，任务环境默认注入，覆盖本技能全部日常操作（workflows / Agent Context / git-repo）。账户/平台管理类（skills/auth/repos-managed…）对 `tk_` 返回 **403**，需 `GRAPE_API_TOKEN`（`grape_`）或 Web JWT——**本技能一般用不到**。
3. **读仓库用 `git-repo`，动工作流用 `tasks`/`a2a`，查 Issue/事件/上下文用 `issues`/`events`/`knowledge`。**
4. **结构化内容放评论，不硬改 Issue/PR 状态**：审查结论以 PR 评论 + 汇总评论落地；Issue 阶段/状态属 user 层，agent 不改。
5. **机器可读输出加 `--json-out`**；长 JSON 用 `--json @file.json`（不手拼内联）。

## 命令发现（拿不准命令名时必做）

```bash
grape --help                       # 全部命令 + 分类 + token 作用域提示
grape commands [pattern]           # 列出可调用命令；过滤：grape commands pulls
grape <group> --help               # 该组动词（含方法/body/Token scope/示例）
grape <group> <verb> --help        # 完整：参数 + Body fields + Examples
grape refresh                      # 重拉最新契约（或回退内置快照）
```

> ⚠️ **命令名以契约为准，不要猜测**：不许 `grape commands | grep pulls`（用 `grape commands pulls`）；不许凭印象拼写，契约里不存在会报 `No such command`。

## 常用命令（QA 审查实际用到）

```bash
# 取 PR 详情（配置检查：title/body/head.sha/labels/assignee/user.login）
grape git-repo pulls get --owner {owner} --repo {repo} --number {number} --json-out

# 取 PR 变更文件清单 + diff（行内评论定位用；返回中可提取每个文件在 diff 中的行号映射）
grape git-repo pulls files --owner {owner} --repo {repo} --number {number} --json-out

# PR 行内评论列表（审查前核对既有问题是否已修复；discussion_id 为对象 discussion_id 字段）
grape git-repo pulls comments list --owner {owner} --repo {repo} --number {number} --json-out

# 发布问题评论（⚠️ 契约仅支持 body 字段，不支持 commit_id/path/position 行内定位字段，传入报 unknown request field(s)；
# 问题定位以 文件路径:行号 写进 body；body 以 [$AGENT_NAME] 开头）
grape git-repo pulls comments create --owner {owner} --repo {repo} --number {number} \
  --json '{"body":"[$AGENT_NAME] 🔴 Critical(src/config.py:42)：..."}'

# 发布汇总评论（仅 body，无定位字段）
# ⚠️ 405 风险：pulls comments create 可能返回 405 Method Not Allowed——GitCode v5 API 端点方法级拒绝
#   （open/merged PR 均可触发，与 PR 是否合并无关，不要归因为「PR 已合并」）→ 降级
#   issues comments create 发关联 Issue 评论（body 注明 PR 编号 + 降级原因）；
#   无关联 Issue → 结论写入 tasks complete result_text（见 qa-runbook Step 6 降级块）
grape git-repo pulls comments create --owner {owner} --repo {repo} --number {number} \
  --json '{"body":"[$AGENT_NAME] 审查报告 ..."}'

# ⚠️ 标记既有讨论已解决（问题已修复）——契约当前无 pulls comments update 命令，先 `grape commands pulls` 确认；
#    存在则用：grape git-repo pulls comments update --owner {owner} --repo {repo} --number {n} \
#      --discussion-id {discussion_id} --json '{"resolved": true}'
#    不存在则改在汇总评论中说明"已修复"，不要拼不存在命令。

# Issue/PR 上下文（关联 Issue 判断 bug / 读评论 / 阶段与路线图）
grape issues detail --owner {owner} --repo {repo} --number {n} --json-out   # 聚合视图（含阶段/状态）
grape issues pipeline --owner {owner} --repo {repo} --number {n} --json-out # 路线图
grape git-repo issues get --owner {owner} --repo {repo} --number {n} --json-out
grape git-repo issues comments list --owner {owner} --repo {repo} --number {n} --json-out

# Issue 评论（body 以 [$AGENT_NAME] 开头，如 📌 关联 PR #{pr_number}）
grape git-repo issues comments create --owner {owner} --repo {repo} --number {n} \
  --json '{"body":"[$AGENT_NAME] 这是关联说明。"}'

# Issue 打标签（对应 Issue 是 bug 时 → bug,triaged；仅允许字段 labels/assignee/milestone_id/state）
grape git-repo issues update --owner {owner} --repo {repo} --number {n} \
  --json '{"labels":"bug,triaged"}'

# 仓库文件/目录读取（工作流审查证据采集；--raw 自动 base64 解码，长文可组合 --path）
grape git-repo tree --owner {owner} --repo {repo} --ref {branch} --json-out   # 目录树（先定位文件是否存在）
grape git-repo contents --owner {owner} --repo {repo} --path AGENTS.md --ref {branch} --raw
grape git-repo contents --owner {owner} --repo {repo} --path spec --raw
grape git-repo contents --owner {owner} --repo {repo} --path tests --raw
# ⚠️ contents --raw 会补末尾换行，不能据此判断文件结尾格式；需字节级校验用 git 对象（grape git cat-file -p）

# 仓库信息 / 分支 / 提交（合并历史、审查门禁判断）
grape git-repo repo --owner {owner} --repo {repo} --json-out
grape git-repo branches --owner {owner} --repo {repo} --per_page 50 --json-out
grape git-repo commits --owner {owner} --repo {repo} --per_page 20 --json-out

# A2A 交办 coding Agent（审查发现问题需修改时，专属命令）
grape a2a handoff --json '{"agent":"coding","payload":{...}}'

# 审核点（pending_review）：审查通过需合入确认/用户拍板时；session_only；--issue_id=需求卡片内部 id（优先 $GRAPE_ISSUE_ID，未注入降级 issues detail 的 id）；changed:true=成功
grape issues pending-review --issue_id ${GRAPE_ISSUE_ID:-{id}} \
  --json '{"review_points":"PR #N 审查通过，请确认是否合入","references":"PR #N"}'
# 审核通过（用户确认后）：target=done（最终合入）/ processing（继续）
grape issues review-pass --issue_id ${GRAPE_ISSUE_ID:-{id}} --json '{"target":"done","evidence":"用户确认合入（评论 id）"}'
# 审核打回（用户要求修改）：target=processing / awaiting_input
grape issues review-reject --issue_id ${GRAPE_ISSUE_ID:-{id}} --json '{"target":"processing","reason":"用户要求修改：{要点}"}'

# 任务自报完成（收尾必做，见 runbook Step 7；会话 token 自动定位任务，无需 --task_id；⚠️ 结构化字段必填，服务端据此组装 archive）
grape tasks complete --json '{
  "result_text": "结论：PR #N 审查完成。",
  "next_step": "done",
  "actions": ["代码审查 + 工作流审查", "发布问题评论 + 汇总评论", "A2A 交办 coding Agent 修复"],
  "conclusion": "PR #N 审查结论：{通过 / 需修改}；发现问题 {n} 项；A2A 交办 coding",
  "artifacts": ["PR #N 审查评论（comment id=XXXXXX）", "A2A event seq=XXXX"],
  "issue_summary": "PR #N 审查完成 | 结论 {通过/需修改} | 发现 {n} 项 | A2A {已/未}交办"
}'

# 轮询任务至终态
grape tasks wait --task-id t_xxx --timeout 120 --interval 3 --json-out
```

- 所有命令支持 `--json-out`（机器可读单行 JSON）；分页 `--per_page 20`（max 100）；出参结构 = GitCode 平台实际结构。
- 契约不可达时 CLI 自动回退**内置契约快照**（`grape refresh` 显示来源）。

## git（本地执行，token 零暴露）

`grape git` 是 **git 原生命令透传**：**自动识别仓库**（clone 从 URL 解析，其余从当前目录 `remote.origin.url` 解析），
再用会话 token 换券成**短期平台凭据**经 `GIT_CONFIG` env 注入 git 子进程——**不进命令行、不落磁盘、agent 感知不到**。
仅 gitcode.com 宿主注入；换券失败仅告警、按本机原生行为执行。`grape git --help` 与 `git --help` 一致。

```bash
grape git status                     # 本地命令（无需凭据）
grape git log --oneline -20 --all    # 合并历史核对（工作流审查证据）
```

> QA 只审查不写代码：git 仅用于只读核对（合并历史、提交），**不得**修改代码/提交/推送。

## 错误排查

| 现象 | 含义 | 处理 |
|------|------|------|
| `HTTP 401: ... re-inject` | token 无效/过期/缺失 | 任务级 token 短命，失效属正常：说明「凭据已失效」，不尝试换 token |
| `HTTP 403: ... 不支持会话 token / use GRAPE_API_TOKEN` | 账户/平台类（D 类）拒绝会话凭据 | 本技能一般不涉及；确需时改用 `GRAPE_API_TOKEN`（`grape_`）或 Web JWT |
| `No such command 'X'. Did you mean: A, B?` | 命令名写错 | 按建议改；或 `grape commands <关键词>` 查 |
| `unknown request field(s): ... allowed for ...` | `--json` 字段不在请求体允许集 | 看 `grape <group> <verb> --help` 的 Body fields |
| `(path not found; try --path '' to list root)` | `contents` 路径不存在 | 先 `grape git-repo tree` / `--path ""` 看根列表定位 |
| `pulls comments create` 405 | GitCode v5 API 端点方法级拒绝（open/merged PR 均可触发，非 PR 状态问题，不要归因为「PR 已合并」） | 降级 `issues comments create` 发关联 Issue 评论（body 注明 PR 编号 + 降级原因）；无关联 Issue → 结论写入 tasks complete result_text |
| 403（无权限） | 写操作无权限 | 停止并回复「该操作需要管理员/维护者权限，请在 仓库设置中手动配置后重试」 |

## 退出码

`0` 成功 · `1` 通用/配置错误（缺 token/网络/超时） · `2` 用法错误 ·
`>=400` HTTP 状态透传（如 `401`） · `130` Ctrl+C。

## 反模式（禁止）

- ❌ 凭印象/经验拼命令名或 `--参数` —— 先 `--help` 或 `grape commands <pattern>`。
- ❌ `grape commands | grep ...` —— 用 `grape commands <pattern>`。
- ❌ `grape api ...` —— 该命令**已移除**（曾是逃生舱），一律走契约命令。
- ❌ 大 JSON 手拼内联 —— 用 `--json @file.json`。
- ❌ 用 `tk_` 会话 token 打账户/平台管理接口（会 403）—— 需要时切换 `grape_`/JWT。
- ❌ 用 `cat`/`ls`/`grep` 直接翻 workspace / Grape 系统文件采集证据 —— 一律走 `grape git-repo`（仅 `session_history.json` 例外可读）。
- ❌ 中间步骤单独发评论 —— 所有评论统一在最后合并发送。

## 安全与边界（必须遵守）

1. **只用系统注入的 `$GRAPE_SESSION_TOKEN`**：不得从数据库、配置文件、日志、历史、环境变量
   中寻找/猜测其他 token。
2. **禁止访问 Grape 内部数据**：不得读取 `data/sessions.db`、`user_accounts`、`managed_repos`、
   `configs/`（含 `global-bot-token.json` 等）等 Grape 内部基础设施（`data/workspaces/` 项目知识文档除外）。
3. **403/无权限**：立即停止并回复用户「该操作需要管理员/维护者权限，请在 仓库设置中手动配置后重试」，
   不要尝试其他 token 或绕行手段。
4. **401（token 失效）**：任务级 token 短命，失效属正常；说明凭据已失效即可，不尝试替换。
5. **接口缺失 / token 失效**：直接输出**任务无法完成的理由**，不要探索其它无关的 token。
6. **不写 token 到任何文件/日志/评论/工具调用之外的地方**。