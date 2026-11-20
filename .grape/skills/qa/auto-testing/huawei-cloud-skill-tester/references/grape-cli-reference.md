# grape CLI 命令参考（huawei-cloud-skill-tester 用到的子集）

> 命令契约以 `grape commands` / `grape <cmd> --help` 为准；本文只列 tester runbook 实际用到且已验证的命令子集。
> ⚠️ **禁止 curl 直连平台 API**（grape v0.2.2 起已移除 `grape api` 逃生舱，契约未覆盖的端点不可调用）。

---

## 1. 安装与就绪

```bash
which grape          # 验证安装；未装/版本过低 → 按 references/grape-cli-installation.md 自动安装/升级
grape --version      # 确认版本（≥0.3.2）
grape refresh        # 契约保鲜（失败重试 ≤2 次后回退内置快照继续）
```

## 2. 认证与配置

| 项 | 说明 |
|----|------|
| `$GRAPE_SESSION_TOKEN` | 服务端注入，任务级临时 token（`tk_` 前缀），`grape` 自动读取，不需手动 export |
| `$GRAPE_SERVER_URL` | 服务端注入，指向 API 地址 |
| Token 优先级 | `--token` > `GRAPE_SESSION_TOKEN` > `GRAPE_API_TOKEN` > config.json |
| base_url 优先级 | `--base-url` > `GRAPE_BASE_URL` > `GRAPE_SERVER_URL` > config.json > `http://localhost:8000` |
| 环境变量 | `$REPO_OWNER`（仓库属主 owner）、`$REPO_NAME`（= `${REPO_FULL_NAME#*/}`）、`$ISSUE_NUMBER`、`$PR_NUMBER`；`$ACCOUNT` 为触发者 actor（一般不用作 `--owner`）|

## 3. 常用命令

### 3.1 任务与上下文

| 命令 | 用途 |
|------|------|
| `grape tasks current --json-out` | 获取分配给自己的任务（A2A payload 在此） |
| `grape tasks complete --json '{"result_text":"...","actions":[...],"conclusion":"...","artifacts":[...],"next_step":"..."}'` | 自报完成（收尾必做） |
| `grape refresh` | 契约保鲜 |

### 3.2 PR 操作

| 命令 | 用途 |
|------|------|
| `grape git-repo pulls get --owner {owner} --repo {repo} --number {pr} --json-out` | 读 PR 确认被测对象 |
| `grape git-repo pulls merge --owner {owner} --repo {repo} --number {pr} --json-out` | 测试通过后自动合并（merge_skill_pr.sh 封装） |
| `grape git-repo pulls comments create --owner {owner} --repo {repo} --number {pr} --json '{"body":"[QA Agent] ..."}'` | PR 评论（结果标记） |
| `grape git-repo branches --owner {owner} --repo {repo} --per_page 100 --json-out` | 查询源分支是否已删（合并后检查） |

> grape 0.3.3 无分支删除接口——源分支残留降级为告警，请手动清理。

### 3.3 Issue 评论

| 命令 | 用途 |
|------|------|
| `grape git-repo issues comments list --owner {owner} --repo {repo} --number {n} --json-out` | 读历史评论（查历史 ✅ 标记 / 三选项等待结果） |
| `grape git-repo issues comments create --owner {owner} --repo {repo} --number {n} --json '{"body":"[QA Agent] ..."}'` | 发评论（测试报告 / 三选项 / 告警）；长 body 用 `--json @file` |

### 3.4 A2A 交棒

```bash
# 测试通过+合并成功 → SRE：grape a2a handoff --json '{"agent":"sre","payload":{"repository":"owner/repo","account":"{user}","issue_number":N,"pr_number":M,"context":"..."}}'
# 测试失败 → 回 Coding：grape a2a handoff --json '{"agent":"coding","payload":{"repository":"owner/repo","account":"{user}","issue_number":N,"pr_number":M,"context":"测试失败：<原因>。请修复后重新 push"}}'
```

- `issue_number` 必填；`pr_number` 有则必填；`repository` 与 `account` 必填。

## 4. 命令限制与不存在的能力

- **无 `grape api` 逃生舱**（v0.2.2 移除）——契约未覆盖的端点不可调用，不要 curl 直连。
- 不存在的命令（不要尝试）：`grape tasks status`（tasks 组仅 complete/current/notify/wait）、`grape issues status get`（被父 leaf 命令遮蔽）等——以 `grape <cmd> --help` 返回 `No such command` 为准。
- 任务删除 / 分支创建 / 分支删除 / Token 管理 / 交互式提示 / 配置初始化——均不存在。

## 5. 退出码

| 码 | 含义 |
|----|------|
| 0 | 成功 |
| 1 | 通用/配置错误 |
| 2 | click 用法错误 |
| ≥400 | HTTP 透传 |
| 130 | Ctrl+C |

## 6. 安全与边界

- **token 使用范围**：只用 `$GRAPE_SESSION_TOKEN`（grape CLI 自动读取）。不得从数据库、配置文件、日志、历史中寻找其他 token。
- **403/401 处理**：API 返回 403 或权限不足时，停止操作并回复「该操作需要管理员/维护者权限，请在 GitCode 仓库设置中手动配置后重试」，不尝试其他 token 或绕行手段。
- **token 不写入文件**：不将 token 写进日志、脚本、Issue 评论或工具调用以外的地方。
- 华为云 AK/SK 只经环境变量 / hcloud CLI profile 读取，绝不要求用户在对话中输入（三选项协议见 SKILL「Agent Protocol」章节）。