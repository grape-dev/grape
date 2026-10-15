# grape CLI 命令参考（openspec-pr-reviewer 专用子集）

> 事实基线：grape CLI 契约（≥0.3.2）。只列本 skill 实际用到的命令。

## 1. 安装与就绪检查

- 通常预装；未装/版本过低自行安装：`python -m pip install git+https://gitcode.com/grape-dev/grape-cli.git`，验证 `grape --version`。
- 不 curl 直连 API 安装/调用。

## 2. 认证与配置

- `$GRAPE_SESSION_TOKEN` 服务端自动注入，`grape` 自动读取，不需手动 export。
- 会话级 `tk_` 对账户/平台管理类端点（auth/skills 等）返回 403 属正常（本 skill 一般用不到）。

## 3. 常用命令（qa 审查子集）

```bash
# 任务上下文
grape tasks current --json-out
grape issues detail --owner {owner} --repo {repo} --number {n} --json-out
grape events detail --seq {seq} --json-out
grape git-repo issues comments list --owner {owner} --repo {repo} --number {n} --json-out
grape scenes detail --scene_id {scene_id} --json-out          # A2A 前场景节点核验

# PR 审查材料（全只读）
grape git-repo pulls list --owner {owner} --repo {repo} --state open --json-out
grape git-repo pulls get --owner {owner} --repo {repo} --number {pr} --json-out
grape git-repo pulls files --owner {owner} --repo {repo} --number {pr} --json-out      # 变更清单 + patch diff
grape git-repo pulls comments list --owner {owner} --repo {repo} --number {pr} --json-out
grape git-repo commits --owner {owner} --repo {repo} --json-out                        # commit message
grape git-repo contents --owner {owner} --repo {repo} --path {path} --ref {sha} --raw  # 新增文件全文

# 报告发布（唯一写操作：PR 评论）
grape git-repo pulls comments create --owner {owner} --repo {repo} --number {pr} \
  --json '{"body":"[QA Agent] ..."}'

# 收尾 / 交接
grape tasks complete --json '{"result_text":"...","next_step":"...","actions":[],"conclusion":"","issue_summary":""}'
grape a2a handoff --json '{"agent":"coding","payload":{"repository":"...","account":"...","issue_number":N,"pr_number":P,"context":"..."}}'
grape tasks wait --task-id {id}                       # 异步任务轮询
```

## 4. 命令限制与不存在的能力

- **命令遮蔽**：`tasks status get` 不可执行，查询任务状态用 `tasks wait --task-id`。
- **无 `grape api` 逃生舱**：v0.2.2 移除；契约未覆盖端点无法 CLI 调用，不 curl 直连。
- **不存在能力**：仓库创建/删除、任务删除、知识上传、Issue/PR 删除、分支创建/删除、Token 管理、`tasks create`（已移除）、流式输出、交互式提示。
- 本 skill **不使用**：`pulls merge`（禁合并）、`git-repo issues update`（不改 Issue body）、任何 `grape git` 写子命令。

## 5. 退出码

`0` 成功 · `1` 通用错误 · `2` 用法错误 · `≥400` HTTP 透传 · `130` Ctrl+C

## 6. 安全与边界

- token 不写文件/日志/评论；403 → 停止并说明需维护者权限；401 → 凭据失效说明，不换 token。
- 禁止读取 Grape 内部 data/、configs/、sessions.db。
- 报告中敏感值掩码（`ghp_****`），只留文件+行号/commit sha 定位。
