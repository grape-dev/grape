# PR 审查执行走查表（runbook）

> 目的：把 pr 每一步**要做什么 + 用哪条确切命令**固化下来，Agent **照着执行、不要查 `--help` / `grape commands`**。
> 环境变量直接使用：`$ACCOUNT`、`$REPO_NAME`、`$PR_NUMBER`、`$ISSUE_NUMBER`（能确定时）、`$DEVOPS_EVENT_TYPE`。
> **评论前缀硬规**：总结评论一律 `[QA Agent]` 开头（不以它开头 = 非法输出）。
> ⚠️ **行内评论契约限制**：pulls comments create 仅支持普通评论 body——不带 commit_id/path/position、不 resolve——统一总结评论标注文件:行号。

---

## Step 0 · 任务上下文与就绪（必须最先）

```bash
grape refresh                        # 契约刷新（前置门②）
grape tasks current --json-out
grape git-repo pulls get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out
grape issues detail --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out   # 能确定关联 Issue 时
```

> CLI 自愈安装（仅未装/版本低时）：`timeout 300 pip install git+https://gitcode.com/grape-dev/grape-cli.git`。

---

## Step 1 · PR 配置检查（仅首次 pull_request_opened 事件执行；updated/push/note_hook_pr 跳过）

```bash
# 按 references/pr-config-check.md 检查 Title/Description/Labels/Assignee 等
# 不通过项记录到 review_comments（暂不评论——统一到最后）
# 有关联 Issue → Issue 评论「📌 关联 PR #{pr_number}」+ bug 则打标：
grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" \
  --json '{"body":"📌 关联 PR #'"$PR_NUMBER"'"}'
grape git-repo issues update --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" \
  --json '{"labels":"bug,triaged"}'    # 仅当对应 Issue 是 bug
```

## Step 2 · 变更检视（断点续跑）

```bash
cat ../.last_pr_review/$PR_NUMBER.sha 2>/dev/null
# 文件不存在或 SHA 与当前 head 不同 → 有新变更，继续
# SHA 相同 → 无新变更，跳过以下所有能力项（配置检查评论仍发送）——然后收尾
# 审查完成后把当前 head SHA 写入 ../.last_pr_review/$PR_NUMBER.sha（断点续跑状态）
```

## Step 3 · 任务类型判断

| 判断条件 | 任务类型（按顺序执行）|
|---------|-------------------|
| PR 修改 `skills/` 目录 | 常规审查 → Skill 审查 → A2A → 回复 |
| PR 包含 IaC 文件（K8s/Terraform/Dockerfile/Helm）| 常规审查 → IaC 审查 → A2A → 回复 |
| 普通代码变更 | 常规审查 → A2A → 回复 |
| 无新变更 | 不做处理，结束 |

---

## 能力一 · 常规审查（5 轴：正确性/可读性/架构/安全/性能）

```bash
# ① 先核对上次问题是否已修复（获取已有行内评论）：
grape git-repo pulls comments list --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out
# ② 核对修复情况（pulls get + pulls files）：
grape git-repo pulls get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out
grape git-repo pulls files --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out
#   已修复 → 记入 review_comments「已修复」清单；未修复 → 保留问题
# ③ 审查新变更（diff 从 pulls files 返回）：按 5 轴找问题 → 记录 review_comments（级别/文件/行号/问题/建议）
# ④ 发现 Critical/Required 级别问题 → 记录摘要（供能力四 A2A）
```

> 读取 `references/code-review-and-quality.md` 五轴标准执行审查。

## 能力二 · Skill 审查（仅 PR 修改 skills/ 目录时）

```bash
# 读取 references/skill-review-and-quality.md——审查技能文件格式规范/完整性/引用路径/向后兼容
# 发现的问题参照能力一格式记录 review_comments
```

## 能力三 · IaC 审查（仅 PR 包含 IaC 文件时）

| 文件类型 | 关注点 |
|---------|--------|
| Kubernetes Manifest | 特权容器、hostPath、securityContext 缺失、runAsRoot、资源限制未设 |
| Terraform / OpenTofu | 公开 S3 bucket、ingress 全开放（0.0.0.0/0）、硬编码凭据、未启用加密 |
| Dockerfile | FROM latest、ADD 替代 COPY、root 运行、多余端口暴露 |
| Helm Chart | values 敏感默认值、RBAC 过度、镜像拉取策略不当 |

## 能力四 · A2A 交办（按审查结果分流——必做判断）

> **决策规则**（根据前序审查结论 + 当前事件判断，三分支选一；不得跳过——合并验证的 sre 交办是固定收尾）：
> - **分支 A（有问题）**：发现 Critical/Required 级问题 → A2A 交办 `coding` 修复
> - **分支 B（通过 + 已合并）**：无 Critical/Required 且当前事件为 `pull_request_merged`（PR 合并验证）→ **A2A 交办 `sre` 部署**——不得以「无问题」为理由省略
> - **分支 C（通过 + 未合并）**：无问题但 PR 尚未合并（`a2a_event` 首查场景）→ 不交办（下一轮 `pull_request_merged` 事件再交 sre）

```bash
# ⚠️ 防重复（分支 A/B 都要）：先查 pipeline，已含目标 agent（coding:a2a_event /
#   sre:a2a_event status≠failed）则不重复 A2A，只发评论
grape issues pipeline --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out

# ── 分支 A：交办 coding（有问题时；issue_number 必填——无法确定则不调用，
#    最终评论说明；pr_number 有则填，严禁混用）──
grape a2a handoff --json '{"agent":"coding","payload":{"repository":"'"$ACCOUNT/$REPO_NAME"'","account":"{user}","issue_number":'"$ISSUE_NUMBER"',"pr_number":'"$PR_NUMBER"',"context":"PR #'"$PR_NUMBER"' 审查发现代码需要修改：{REVIEW_SUMMARY}"}}'
# 返回后：将 coding 优化结论合并进最终审查评论

# ── 分支 B：交办 sre（通过 + pull_request_merged；构造 payload 用 @file 避免转义）──
cat > /tmp/a2a-sre-payload.json << 'EOF'
{"agent":"sre","payload":{"repository":"'"$ACCOUNT/$REPO_NAME"'","account":"{user}","issue_number":'"$ISSUE_NUMBER"',"pr_number":'"$PR_NUMBER"',"context":"PR #'"$PR_NUMBER"' 已合并，QA 审查通过，交办部署验证"}}
EOF
grape a2a handoff --json @/tmp/a2a-sre-payload.json --json-out

# A2A 失败 → 重试 ≤3 次 → 仍失败：最终评论说明 + next_step 人工介入
```

## 能力五 · 统一总结评论（必做——所有能力项执行完毕后最后执行；不得中间单独发评论）

```bash
# 所有 review_comments 合并为一条评论（长 body 先写文件再 --json @file）
# body 模板见 SKILL.md 4.6 输出格式：
# [QA Agent] 审查报告 + 配置检查 + 代码审查（结论 ✅/⚠️/❌ + 问题表）+ 安全扫描 + 合并检查
# 全部通过 → "[QA Agent] 审查通过，所有检查项均正常。"
# ⚠️ 首查通过且 PR 未合并（无 pull_request_merged 事件）→ body 末尾【必须】追加醒目提醒：
#   "## ⚠️ 请合并本 PR
#    > 审查已通过，但流水线需要 PR 合并才能继续（合并后将自动触发 SRE 部署验证）。
#    > 请维护者操作：在 GitCode 页面点击【Merge】合并本 PR。"
grape git-repo pulls comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" \
  --json @/tmp/review-body.json
# ⚠️ 405（pulls comments create 返回 405 Method Not Allowed——GitCode v5 API 端点方法级拒绝，
#   open/merged PR 均可触发，与 PR 是否合并无关；不要归因为「PR 已合并」）→ 降级：
#   grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" \
#     --number "$ISSUE_NUMBER" --json @/tmp/review-body.json（发到关联 Issue，body 注明「PR #$PR_NUMBER 审查评论（PR 评论端点 405，降级发至此）」）
#   PR 无关联 Issue 且 405 → 将结论写入 tasks complete result_text（不丢结论）
```

> 跳过此步 = 事件未完成处理。例外：变更检视判定 SHA 未变更而跳过时，不发评论。

---

## Step 收尾 · 任务完成流程（必做）

```bash
grape tasks complete --json '{
  "result_text":  "结论：PR #'"$PR_NUMBER"' 审查完毕（{结论}）",
  "actions":      ["配置检查: {通过/问题项}", "变更检视: {新变更/SHA 未变跳过}", "常规审查: {结论}", "Skill/IaC 审查: {如执行}", "评论: [QA Agent] 总结评论", "A2A: coding/sre/无"],
    "conclusion":   "{pass / needs_changes / needs_major_changes}，{已 A2A coding / 已 A2A sre / 无需修改}",
    "artifacts":    ["PR #'"$PR_NUMBER"' 总结评论", "A2A handoff（如有）", ".sha 断点更新"],
    "issue_summary":"{审查结论摘要——结论/问题级别/去向 一句话}",
    "next_step":    "a2a_sre（合并验证通过时）/ a2a_coding（有问题时）/ 等待人工决策"
}'
```

> ⚠️ **结束信号**：tasks complete 后立即结束，不要继续思考或等待输入。A2A 交办成功 ≠ 任务结束。结构化字段 must 真实填充。

---

## 错误排查速查（命令返回异常时）

> ⚠️ 出错时**先看完整错误**（命令去掉 `| tail -N` 截断，`2>&1` 保留 stderr）；runbook 命令照抄仍失败，才允许 `grape commands <关键词>` / `grape <group> --help` 按 Body fields 修正。**不反复盲试同一命令**。

| 现象 | 处理 |
|------|------|
| `No such command 'X'` | 命令名抄错，核对 runbook/reference；仍不确定再 `grape commands <关键词>` |
| `unknown request field(s): ...` | `--json` 有契约不允许字段 → `--help` 看 Body fields 删多余 |
| `pulls files` / `pulls get` 返回空 | 属正常（无变更/信息缺失）——按已获取信息出结论并标注 |
| `pulls comments create` 报行内字段不支持 | 契约限制：只发普通 body——删掉 commit_id/path/position 字段（见 SKILL.md 安全硬约束⑦）|
| `pulls comments create` 405（端点方法级拒绝）| **GitCode v5 API 端点方法级拒绝——open/merged PR 均可触发，与 PR 是否合并无关，不要归因为「PR 已合并」** → 降级 `issues comments create` 发关联 Issue（body 注明 PR 编号 + 降级原因）；无关联 Issue → 结论写入 tasks complete result_text |
| `issues update` 打标失败 | 先 `labels list` 确认 `bug`/`triaged` 标签存在（不存在先 create）|
| `.sha` 文件不存在 | 属正常（首次审查）——按「有新变更」处理 |
| 401 凭据失效 | 说明「凭据已失效」即可，不换 token（安全硬约束⑤）|
| 403 账户/平台管理类 | 说明需管理员配置/`grape_` 级凭据，本技能不涉及——不绕行 |
| 报「契约过期/命令不识别」| 先 `grape refresh` 再重试（前置门②）|