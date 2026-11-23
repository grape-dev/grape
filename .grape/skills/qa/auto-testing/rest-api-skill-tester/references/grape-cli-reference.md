# grape CLI 参考（rest-api-skill-tester 子集）

> 本文件是 **grape CLI 命令契约**（v0.3.x，经 `grape commands` 核对）。平台交互（评论/合并/分支查询/A2A/任务）一律走 grape CLI。被测 skill 的目标系统调用（跑测试命令）不属平台交互。

## 1. 安装与自愈

前置检查发现 `grape` 未安装或版本过低（< 0.3.2）→ 按 `references/grape-cli-installation.md` 自动安装/升级，不直接报障。

## 2. 认证（自动，无需配置）

`grape` 自动读取服务端注入的 `$GRAPE_SESSION_TOKEN`——**严禁**寻找/读取其他 token 用于 grape 命令。

> ⚠️ 两套认证严格区分：
> - **平台交互（评论/合并/A2A/任务）** → grape CLI 自动读 `$GRAPE_SESSION_TOKEN`，什么都不用配
> - **被测 skill 的目标系统** → 凭据从 `.project-info/` 或 `{PREFIX}_*` 环境变量读取（见 SKILL 4.3 流程 1 凭据检测），export 给被测脚本用，与 grape 认证无关

## 3. 命令子集（本技能真实使用）

### 3.1 评论（步骤 4 硬规则）

```bash
# Issue 评论（测试报告）
grape git-repo issues comments create \
  --owner "${REPO_FULL_NAME%/*}" --repo "${REPO_FULL_NAME#*/}" \
  --number "$ISSUE_NUMBER" \
  --json '{"body": "[QA Agent] ✅ 测试通过\n\n{报告内容}"}'

# PR 评论（固定标记）
grape git-repo pulls comments create \
  --owner "${REPO_FULL_NAME%/*}" --repo "${REPO_FULL_NAME#*/}" \
  --number "$PR_NUMBER" \
  --json '{"body": "[QA Agent] ✅ 测试通过"}'
```

- 评论 body 固定标记开头（可检索）：`[QA Agent] ✅ 测试通过` / `[QA Agent] 🔴 测试失败`
- 读评论历史（pull_request_merged 前置判断）：`grape git-repo issues comments list --owner ... --repo ... --number "$ISSUE_NUMBER" --json-out`

### 3.2 合并（步骤 5，脚本封装）

`scripts/merge_skill_pr.sh` 内部使用（不手动敲）：

```bash
grape git-repo pulls merge --owner "$OWNER" --repo "$REPO" --number "$PR_NUMBER" --json-out
grape git-repo pulls get  --owner "$OWNER" --repo "$REPO" --number "$PR_NUMBER" --json-out   # 源分支名
grape git-repo branches   --owner "$OWNER" --repo "$REPO" --per_page 100 --json-out           # 分支是否还在
```

> **pulls merge 无 merge_method/prune_branch 参数**（0.3.3）——合并方式走平台默认；prune_branch 缺失导致源分支可能残留，脚本查分支仍在时报「请手动清理」（grape 无 branches delete 接口，原 curl 直连已按纪律移除）。

### 3.3 A2A 交棒（步骤 5/失败路径）

```bash
grape a2a handoff --json '{"agent":"sre","payload":{"repository":"'$REPO_FULL_NAME'","account":"'$ACCOUNT'","issue_number":'$ISSUE_NUMBER',"pr_number":'$PR_NUMBER',"context":"测试通过，已自动合并。Skill：{name}，请发布到 ClawHub/GitHub/GitCode"}}'
grape a2a handoff --json '{"agent":"coding","payload":{"repository":"'$REPO_FULL_NAME'","account":"'$ACCOUNT'","issue_number":'$ISSUE_NUMBER',"pr_number":'$PR_NUMBER',"context":"测试失败：{原因摘要}。请修复后重新 push"}}'
```

### 3.4 任务管理

```bash
grape tasks current          # 可选，确认当前任务
grape tasks complete --json '{"result_text":"<处理结论>","actions":[...],"conclusion":"...","artifacts":[...],"next_step":"..."}'
# 收尾协议：业务动作完成后先调 tasks complete，再输出 JSON 块（见 SKILL 4.8 任务收尾）
```

## 4. ❌ 不存在的命令（防误用，禁止尝试）

| 命令 | 说明 |
|------|------|
| `grape tasks close / mine / start / status / clarify` | tasks 组仅有 complete/current/notify/wait |
| `grape git xxx`（透传） | git 操作不存在透传，worktree 操作用原生 git |
| `grape api` | 已移除（0.2.2 起），且平台禁止直连 API |
| `grape knowledge context/content` | 不存在，知识基线由 prompt 内联注入 |
| `grape git-repo branches delete` | 不存在（仅 list），分支删除需人工 |
| `grape events detail --seq` | --seq 平台不注入，勿用 |

## 5. 退出码 与 安全边界

- 命令失败：检查输出错误信息（401=token 无效 / 403=无权限），按 SKILL 一 安全硬约束 ③区分处理——**不重试、不用其他 token 绕行**
- 安全边界：不用 grape commands 之外的方式拼平台 API；token 不落盘/不进日志/不进评论/不进脚本文件