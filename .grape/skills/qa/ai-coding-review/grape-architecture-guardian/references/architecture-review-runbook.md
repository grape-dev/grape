# grape-architecture-guardian 执行走查表（runbook）

> 目的：把 PR 架构守护门禁审查的每一步 + 确切命令固化，Agent 照着执行、不查 `--help` 探索。
> 环境变量直接使用：`$REPO_OWNER` / `$REPO_NAME` / `$REPO_FULL_NAME` / `$ISSUE_NUMBER` / `$PR_NUMBER` / `$GRAPE_ISSUE_ID` / `$GRAPE_SCENE_ID` / `$BOT_USERNAME`。
> **评论前缀硬规**：审查报告 body 一律以 `[QA Agent]` 开头。
> ⚠️ **纯审查**：全程只读（拉取材料 + 3 脚本）+ PR 评论发布；禁 git 写操作 / 合并 / 改文件。

### Step 0 · 前置 + 读任务上下文（必须最先）

```bash
grape tasks current --json-out
grape issues detail --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out
grape events detail --seq {seq} --json-out        # 读前序 A2A payload，取 pr_number/context
```

- `$PR_NUMBER` 未注入时，从 A2A payload 的 `pr_number` 取；仍缺 → `grape git-repo pulls list --owner "$REPO_OWNER" --repo "$REPO_NAME" --state open --json-out` 找待审 PR，无法确定 → 评论说明并收尾（next_step=reply_comment）。
- **先读 `references/grape-architecture-rules.md`**（7 Gate 检查项唯一依据）+ `references/known-config-values.md`（已知数值注册表）。

### Step 1 · 拉取 PR 审查材料（必做，全只读）

```bash
grape git-repo pulls get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out       # 元信息 + head/base/sha
grape git-repo pulls files --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out     # 变更清单 + patch diff
grape git-repo commits --owner "$REPO_OWNER" --repo "$REPO_NAME" --json-out                                # commit message（按 head sha 对照）
grape git-repo pulls comments list --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out  # 已有评论（防重复报告）
```

- 解析 PR 元数据：PR 编号、head sha 前 8 位、base 分支（判定模式：main=A 生产基线 / dev=B 开发基线 / weekly-xx=C 迭代基线 / feat-xx=D 功能分支）、变更量（文件数/±行数）。
- 新增文件需全文核查时：`grape git-repo contents --owner "$REPO_OWNER" --repo "$REPO_NAME" --path "{path}" --ref "{head_sha}" --raw`。
- 变更 >100 文件或 >1000 行 → 按 `agents/grape-architecture-auditor.md` 委派子审计（跑 3 脚本 + 逐条对照规则），主上下文只汇总结论。
- 已有同轮报告评论（`[QA Agent]` 前缀且对应最新 head sha）→ 不重复发布，直接收尾。

### Step 2 · 运行 3 只读脚本取证（必做）

```bash
SKILL_DIR=".grape/skills/qa/ai-coding-review/grape-architecture-guardian"   # 本技能在仓库中的目录
REPO_PATH="."                                                              # 审查对象仓库（当前 worktree 或克隆路径）

python3 "$SKILL_DIR/scripts/repo_inventory.py" "$REPO_PATH" --json            # 技术栈盘点（确定适用 Gate）
python3 "$SKILL_DIR/scripts/magic_number_audit.py" "$REPO_PATH" --min-severity medium   # 无来源配置数值扫描
python3 "$SKILL_DIR/scripts/grape_constraint_check.py" "$REPO_PATH" --json    # Grape 特有约束检查
```

- 脚本零依赖（标准库）、只读，可独立运行；`grape_constraint_check.py` 支持 `--changed-files file1,file2` 聚焦 PR 变更。
- 脚本不在本仓库时（如审查其他 7 个项目）：从 grape 主仓 `git-repo contents --path {skill_scripts} --ref {sha} --raw` 拉取后本地执行。
- 脚本输出是**候选与问题，不是结论**：每条 finding 需按 Finding 证据契约复核（证据是否成立、是否需人工复核）。

### Step 3 · 逐 Gate 三态判定（必做）

按 `references/grape-architecture-rules.md` §8 适用矩阵选择 Gate（先跑 repo_inventory 确认技术栈）：

| Gate | 名称 | 适用仓库 |
|-----|------|---------|
| 1 | Grape 核心架构约束 | 所有仓库 |
| 2 | 组织隔离与安全 | grape 主仓必查，其他按需 |
| 3 | 前端规范 | grape-admin 前端、grape-website 必查 |
| 4 | Git 与提交规范 | 所有仓库 |
| 5 | 测试规范 | grape / grape-cli / grape-admin / grape-test 必查 |
| 6 | 数值证据 | 所有仓库（对照 known-config-values.md） |
| 7 | 架构适配性 | grape 主仓必查，其他按需 |

- 每项三态：**符合 / 不符合（附证据定位 文件+行号/commit sha）/ 不适用（PR 未涉及）**。
- 不符合项按 `templates/finding.md` 契约记录：标题 / Gate / 严重度 / 证据（文件+行号+最小代码引用）/ 问题（事实描述）/ 故障场景 / 为什么对 Grape 重要 / 修复 / 验证。
- 证据不足或需业务判断 → 标注「需人工复核」，不判死。
- 反幻觉自检：每条技术声明前验证——能被仓库代码、项目文档、标准或实测数据支持吗？不能则不作事实陈述；对数值这是绝对的。

### Step 4 · 汇总报告（必做）

按 `templates/pr-review-report.md` 组装：首行（PR 编号、head sha8、模式 A-D、执行位置、重启状态、被中断他人任务数）→ 总体结论 → 审查范围 → 逐 Gate 明细表 → 发现清单（按严重度排序）→ 独立验证（脚本结果摘要）→ 结论与建议。

- 总体结论三态唯一：`APPROVED`（0 BLOCKED / 0 Critical）/ `APPROVED WITH CONDITIONS`（有 Critical 可修复，列出条件）/ `BLOCKED`（N 项 BLOCKED，给具体技术原因）。
- 敏感值一律掩码（`ghp_****`、`192.168.****`），只留定位。

### Step 5 · 发布 PR 评论（必做）

```bash
grape git-repo pulls comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" \
  --json '{"body":"[QA Agent] PR #'"$PR_NUMBER"' grape架构守护审查报告（head sha: {sha8}｜模式 {X}（{基线描述}）｜执行位置 {worker/build}｜{重启状态}，被中断他人任务 {N} 个）\n\n{报告正文}"}' --json-out
```

- 返回的 comment id 记入 `tasks complete` 的 artifacts。
- 发布失败重试 ≤3 次；仍失败 → Issue 评论说明 + 标注人工介入。

### Step 6 · 收尾 / 交办（必做，任何分支都执行到最后）

1. 输出人类可读摘要：PR 编号、适用 Gate、各严重度计数、报告评论 id。
2. 需 A2A 交办前，**先做场景节点核验**（必须 A2A 前）：
   ```bash
   echo "scene_id=${GRAPE_SCENE_ID:+set}"
   SCENE_NODES="$(grape scenes detail --scene_id "$GRAPE_SCENE_ID" --json-out 2>/dev/null)"
   echo "$SCENE_NODES" | jq -r '[.nodes[]?.tag // .nodes[]?.skill] | unique | join(",")' 2>/dev/null
   ```
   - 命中目标 agent 节点 → 可 A2A；未命中 → **评论说明「当前场景未配置 {agent} 环节」+ 不 A2A**，直接收尾（next_step=reply_comment/done）。
3. `grape tasks complete`（见 SKILL.md 任务完成流程模板；结构化字段必填）。
   - `APPROVED` 且流程需要 → `a2a handoff --json '{"agent":"sre","payload":{"repository":"...","account":"...","issue_number":N,"pr_number":P,"context":"架构守护审查通过，请继续"}}'`（交办 agent 名 sre）。
   - `BLOCKED` / 需整改 → `a2a handoff --json '{"agent":"coding","payload":{"repository":"...","account":"...","issue_number":N,"pr_number":P,"context":"架构守护审查发现 N 项问题，请按报告整改"}}'`（交办 agent 名 coding）。
   - 通过且无需交办 → `next_step=done`。
4. 调完即结束任务，不继续等待。

## 错误排查速查

| 现象 | 处理 |
|------|------|
| `No such command 'X'` | 命令名错，核对 `references/grape-cli-reference.md`；仍不确定 `grape commands <关键词>` |
| `pulls get` / API 5xx | `grape tasks wait --task-id` 或 CLI 内置退避重试；仍失败 `grape git ls-remote origin` 核对远端 ref |
| 脚本报错 `not a directory` | `REPO_PATH` 传仓库根目录（含 src/ 或 tests/ 的层级），非子目录 |
| 脚本输出 finding 洪水 | 加 `--min-severity high` 收敛 + `--changed-files` 聚焦 PR 变更文件 |
| 评论发布失败 | 重试 ≤3 次；仍失败 → Issue 评论说明 + 人工介入标注 |
| 401 凭据失效 | 说明「凭据已失效」，不换 token |

## 明确禁止

- ❌ 修改文件 / git 写操作 / 合并 PR（纯审查）。
- ❌ 无证据判 BLOCKED；数值无来源仍判通过。
- ❌ 报告复述完整密钥/口令原文（一律掩码，只留定位）。
- ❌ A2A 交办写技能名——交办 **agent**（`coding` / `sre`）。
- ❌ 因「等待」跳过最终收尾（`tasks complete`）——报告发布后即收尾。
