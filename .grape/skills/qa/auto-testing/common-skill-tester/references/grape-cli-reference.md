# grape CLI 参考（common-skill-tester 子集）

> 本文件是 **grape CLI 命令契约**（v0.3.x，经本机 `grape --help` 实测核对——0.3.3）。平台交互（评论/合并/分支查询/A2A/任务）一律走 grape CLI——**禁止 curl 直连平台 API**。

## 1. 安装与自愈

前置检查（硬约束① + 三 前置检查门第 4 项）发现 `grape` 未安装或版本过低（< 0.3.2）→ 按 `references/grape-cli-installation.md` 自动安装/升级，不直接报障。安装后重试 `grape --version`——仍失败 STOP（本地 standalone 测试不用 grape，跳过）。

## 2. 认证（自动，无需配置）

`grape` 自动读取服务端注入的 `$GRAPE_SESSION_TOKEN`——**严禁**寻找/读取其他 token 用于 grape 命令。服务端在任务执行上下文注入 `GRAPE_CLI_MODE=WORKER`（源码 event_pipeline 实锤），使 git-repo/a2a/tasks 组（**session_only scope**）命令可用——mitigation：USER 模式（GRAPE_API_TOKEN）下这些命令会报 `token scope 'session_only'` 不可用，属预期，不要试图绕行。

> ⚠️ 两套认证严格区分：
> - **平台交互（评论/合并/A2A/任务）** → grape CLI 自动读 `$GRAPE_SESSION_TOKEN`，什么都不用配
> - **被测 skill 的目标系统**（仅当被测 skill 声明需要运行时验证且凭据以 `{PREFIX}_*` 环境变量注入）→ 测试执行域，与 grape 认证无关；无此场景则跳过

## 3. 命令子集（本技能真实使用）

### 3.1 评论读取（pull_request_merged 前置判断）

```bash
grape git-repo issues comments list --owner "${REPO_FULL_NAME%/*}" --repo "${REPO_FULL_NAME#*/}" \
  --number "$ISSUE_NUMBER" --json-out
# 找 body 开头独立行的「[QA Agent] ✅ 测试通过」或「[QA Agent] ⚠️ 测试通过（有条件）」标记
```

### 3.2 评论（Step 7 硬规则——Issue + PR 双评论，body 首行固定标记）

```bash
# Issue 评论（完整测试报告；长 body 先写文件再 --json @file 防 shell 转义）：
grape git-repo issues comments create \
  --owner "${REPO_FULL_NAME%/*}" --repo "${REPO_FULL_NAME#*/}" \
  --number "$ISSUE_NUMBER" \
  --json '{"body":"[QA Agent] ✅ 测试通过\n\n{报告内容——三套评分明细表 + 综合判定 + 建议/必改项清单}"}'
# PR 评论（简短结论）：
grape git-repo pulls comments create \
  --owner "${REPO_FULL_NAME%/*}" --repo "${REPO_FULL_NAME#*/}" \
  --number "$PR_NUMBER" \
  --json '{"body":"[QA Agent] ✅ 测试通过"}'
```

- 固定标记三态（body 首行独立行——可检索，前置判断依赖）：
  - `[QA Agent] ✅ 测试通过`（判定 ✅——三套全 ≥95 无 fail 无 warn）
  - `[QA Agent] ⚠️ 测试通过（有条件）`（判定 ⚠️——存在 warn 或某评分 75-94；**合并照走**，建议项随报告转交下游）
  - `[QA Agent] 🔴 测试失败`（判定 ❌——存在 fail 或评分 <75；不合并回修）

### 3.3 合并（Step 8，脚本封装——不手动敲）

`scripts/merge_skill_pr.sh` 内部使用：

```bash
grape git-repo pulls merge --owner "$OWNER" --repo "$REPO" --number "$PR_NUMBER" --json-out
grape git-repo pulls get  --owner "$OWNER" --repo "$REPO" --number "$PR_NUMBER" --json-out   # 源分支名
grape git-repo branches   --owner "$OWNER" --repo "$REPO" --per_page 100 --json-out           # 分支是否还在
```

> **pulls merge 无 merge_method/prune_branch 参数**（0.3.3 实测）——合并方式走平台默认；prune_branch 缺失导致源分支可能残留，脚本查分支仍在时报「请手动清理」（grape CLI 无 branches delete 接口——原 curl 直连已按纪律移除）。

### 3.4 A2A 交棒（Step 8）

```bash
# 通过族（✅/⚠️——合并后交棒 sre 发布）：
grape a2a handoff --json '{"agent":"sre","payload":{"repository":"'"$REPO_FULL_NAME"'","account":"'"$ACCOUNT"'","issue_number":'"$ISSUE_NUMBER"',"pr_number":'"$PR_NUMBER"',"context":"测试通过（判定：<✅ 通过 / ⚠️ 有条件通过——建议项见报告>），已自动合并。Skill：<name>，请发布"}}'
# 失败（❌——不合并交棒 coding 修复）：
grape a2a handoff --json '{"agent":"coding","payload":{"repository":"'"$REPO_FULL_NAME"'","account":"'"$ACCOUNT"'","issue_number":'"$ISSUE_NUMBER"',"pr_number":'"$PR_NUMBER"',"context":"测试失败：{失败原因摘要}。请修复后重新 push 复测"}}'
```

- **agent 用场景标准节点名 `sre` / `coding`**（不传技能名）；payload **必带 issue_number + pr_number**（sre 发布/coding 修复依赖）
- pull_request_merged 前置判断命中「已测试通过」→ 直接 A2A sre（context 注明「pull_request_merged——此前已测试通过（有 ✅/⚠️ 标记）」

### 3.5 任务管理

```bash
grape tasks current          # 可选，确认当前任务
grape tasks complete --json '{"result_text":"<处理结论>","actions":[...],"conclusion":"...","artifacts":[...],"next_step":"..."}'
# 收尾协议：业务动作全部完成后——先调 tasks complete（4.8：流水线场景先 A2A 交棒再 complete；standalone 直接），再输出 JSON 块（见 SKILL 4.8）
```

## 4. ❌ 不要尝试的命令 / 禁止操作（不存在或禁止使用）

| 命令 | 说明 |
|------|------|
| `grape tasks close / mine / start / status / clarify` | tasks 组仅有 complete/current/notify/wait |
| `grape git xxx`（透传） | **存在但不用**（0.3.3 实测：`grape git` = 本地 git 透传，会尝试注入平台凭据到 git 命令）——本技能 git 操作用平台 worktree 原生 git，透传引入凭据注入风险且无必要 |
| `grape api` | 已移除（0.2.2 起），且平台禁止直连 API |
| `grape knowledge context/content` | 不存在，知识基线由 prompt 内联注入 |
| `grape git-repo branches delete` | 不存在（仅 list）——分支残留人工清理 |
| `grape git-repo pulls create` | 创建 PR 是 creator 职责——本技能只测已存在的 PR |
| `grape git-repo pulls merge`（手敲） | 合并必须走 `scripts/merge_skill_pr.sh`（MERGED/BRANCH_DELETED 契约输出） |
| `grape events detail --seq` | --seq 平台不注入，勿用 |

## 5. 退出码 与 安全边界

- 命令失败：检查输出错误信息（401=token 无效 / 403=无权限），按 SKILL 一 安全硬约束①（403/401 停止）处理——**不重试、不用其他 token 绕行**
- 安全边界：不用 grape commands 之外的方式拼平台 API；token 不落盘/不进日志/不进评论/不进脚本文件