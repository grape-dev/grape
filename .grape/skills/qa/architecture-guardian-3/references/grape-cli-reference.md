# grape CLI 命令参考（architecture-guardian-3 专用子集）

> 事实基线：grape CLI 契约（≥0.3.2）。**只列本 skill 实际用到的命令**，不列全量；契约权威 = `grape commands` 实列。

## 1. 安装与就绪检查

- 通常预装；未装或版本过低自行安装：`python -m pip install git+https://gitcode.com/grape-dev/grape-cli.git`，装完 `grape --version` 复验（要求 ≥ 0.3.2）。
- 不 curl 直连 API 安装或调用。

## 2. 认证与配置

- `$GRAPE_SESSION_TOKEN`（`tk_`）由服务端注入，`grape` 自动读取，**不需要手动 export**。
- 会话级 `tk_` 对账户/平台管理类端点（auth/skills 等）返回 403 属正常；本技能一般用不到，不换 token、不绕行。

## 3. 常用命令（qa 架构审查子集）

```bash
# 任务上下文
grape tasks current --json-out
grape issues detail --owner {owner} --repo {repo} --number {n} --json-out
grape issues pipeline --owner {owner} --repo {repo} --number {n} --json-out      # 防重复 A2A
grape events detail --seq {seq} --json-out                                       # A2A payload

# 项目知识基线 / 仓库结构（只读）
grape repos project info --owner {owner} --repo {repo} --json-out
grape git-repo repo --owner {owner} --repo {repo} --json-out
grape git-repo tree --owner {owner} --repo {repo} --depth single --json-out
grape git-repo contents --owner {owner} --repo {repo} --path {path} --ref {sha} --raw

# 审查材料（只读）
grape git-repo pulls list --owner {owner} --repo {repo} --state open --json-out
grape git-repo pulls get --owner {owner} --repo {repo} --number {pr} --json-out
grape git-repo pulls files --owner {owner} --repo {repo} --number {pr} --json-out
grape git-repo pulls comments list --owner {owner} --repo {repo} --number {pr} --json-out
grape git-repo commits --owner {owner} --repo {repo} --json-out

# 报告发布（唯一写操作）
grape git-repo pulls comments create --owner {owner} --repo {repo} --number {pr} \
  --json '{"body":"[QA Agent] ..."}' --json-out
grape git-repo issues comments create --owner {owner} --repo {repo} --number {n} \
  --json '{"body":"[QA Agent] ..."}' --json-out

# 收尾 / 交办
grape scenes detail --scene_id {scene_id} --json-out                              # A2A 前节点核验
grape a2a handoff --json '{"agent":"coding","payload":{"repository":"...","account":"...","issue_number":N,"pr_number":P,"context":"..."}}' --json-out
grape tasks complete --json '{"result_text":"...","next_step":"...","actions":[],"conclusion":"","artifacts":[],"issue_summary":""}'
grape tasks wait --task-id {id}                                                   # 异步任务轮询

# 卫生与发现
grape refresh                                                                     # 刷新契约（执行前必做）
grape commands {关键词}                                                            # 命令发现
grape feedback submit --json '{"category":"suggestion","feedback_kind":"skill_quality","title":"...","description":"..."}' --json-out   # 只接受 --json，无独立 flag
```

## 4. 限制声明

- **命令遮蔽**：`tasks status get` 不可执行，查任务状态用 `tasks wait --task-id`。
- **无 `grape api` 逃生舱**：v0.2.2 已移除；契约未覆盖的端点无法 CLI 调用，**不 curl 直连**，输出理由。
- **不存在能力**：仓库创建/删除、任务删除、知识上传、Issue/PR 删除、分支创建/删除、Token 管理、`tasks create`（已移除）、流式输出、交互式提示。`skills upload` / `skills delete` **已存在**，不在此清单。
- **平台限制**：`git-repo tree` 的 `depth=recursive` 仅 GitHub 支持，GitCode 返回 HTTP 400「暂不支持递归树」；需要全树时用 `--depth single` 按目录逐层拉取，或只读 `grape git ls-tree -r` 兜底。
- 本技能**不使用**：`pulls merge`（禁合并）、`git-repo issues update`（不改 Issue body）、任何 `grape git` 写子命令、`issues pending-review` / `awaiting-input`（本技能 `flow_nodes=无`）。

## 5. 退出码

`0` 成功 · `1` 通用错误 · `2` 用法错误 · `≥400` HTTP 透传 · `130` Ctrl+C

## 6. 安全与边界

- token 不写文件/日志/评论；403 → 停止并说明需维护者权限；401 → 说明凭据已失效，不换 token。
- 禁止读取 Grape 内部 `data/`、`configs/`、`sessions.db`。
- 报告中敏感值一律掩码（`ghp_****`、`192.168.****`），只留 `file:line` / commit sha 定位。