# IaC QA 执行走查表（runbook）

> 目的：把 iac-qa 每一步**要做什么 + 用哪条确切命令**固化下来，Agent **照着执行、不要查 `--help` / `grape commands`**。
> **CLI 纪律**：本表已覆盖本技能全部平台交互命令（issues / git-repo pulls/issues / a2a / tasks）——**禁止 curl 直连平台 API（读操作也一样）**——每个节点需要的命令已写在该节点——按节点照抄；命令失败才看「错误排查速查」。
> 环境变量直接使用：`$ACCOUNT`、`$REPO_NAME`（=`${REPO_FULL_NAME#*/}`）、`$ISSUE_NUMBER`、`$PR_NUMBER`、`$GRAPE_WORK_BRANCH`、`$GRAPE_SESSION_TOKEN`/`$GRAPE_SERVER_URL`。
> **评论前缀硬规**：所有评论 body 一律以 `[qa Agent]` 开头。

---

## Step 0 · 任务上下文与就绪（必须最先，任何事件都做）

```bash
# ⚠️ CLI 前置自愈（最先）：`grape --version` ≥0.3.2；未装/版本过低 → 按 `references/grape-cli-installation.md` 自动安装/升级 → 装好再验；仍不可用 → 结束任务并说明
grape --version
grape refresh 2>&1 && echo REFRESH_OK
grape tasks current --json-out
grape issues detail --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out 2>&1 | head -80
printenv GRAPE_WORK_BRANCH 2>/dev/null
cd /root/grape-workspaces/$REPO_FULL_NAME/issues/$ISSUE_NUMBER/qa 2>/dev/null && git status 2>&1 | head -3 || true
```

## Step 1 · 获取代码（必做）

```bash
# PR 信息 + 变更文件 + diff：
grape git-repo pulls get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out 2>&1 | head -60
grape git-repo pulls files --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out 2>&1 | head -60
# 变更新鲜度断点（.sha——与上次审查对比，SHA 未变更 → 沿用上次结论）：
cat .last_pr_review/${PR_NUMBER}.sha 2>/dev/null && git log -1 --format=%H 2>/dev/null || true
```

## Step 2 · 自动化审查（必做）

```bash
# 代码质量 + 功能验证（按 references/code-review-checklist.md）——本地读 diff/源码
# 资源规格交叉验证（IaC 专项——⚠️ 前置校验 ${HUAWEI_AK:+ok}：有值才查；缺失 → 报告标注「未交叉验证」，不阻塞不借用）：
#   ECS flavor / 镜像在目标区域可用性、VPC CIDR/安全组不冲突、OBS 桶名/EVS 类型可用（hcloud 查询——命令族同 iac-coding A3）：
#   hcloud VPC ListVpcs 2>&1 | grep -i "名称" || echo 无同名
#   hcloud ECS ListFlavors --cli-region=cn-north-4 --cli-output=json --cli-query="flavors[?contains(name, '{flavor}')].{name:name}" 2>&1 | head -10
# terraform plan 审查（验证 state 读写正常——到 plan 为止；worktree 已由 Step 0 cd 就位）：
cd /root/grape-workspaces/$REPO_FULL_NAME/issues/$ISSUE_NUMBER/qa 2>/dev/null && timeout 180 terraform init 2>&1 | tail -5
timeout 300 terraform plan -out=plan-review.tfplan 2>&1 | tail -40
# 逐资源分析 plan 输出（create/update/destroy/replace）——镜像存在性/RDS flavor/provider 下载/OBS backend 连通
```

## Step 3 · 安全检查（必做）

```bash
# 敏感信息泄漏（AK/SK/密码/token 硬编码）、依赖安全、权限控制（references/security-checklist.md）
# 华为云 IaC 专项（references/huaweicloud-security-checklist.md）：凭据落盘检查（plan/state 无明文密码）
# tfsec 扫描 .tf（如可用）：
tfsec iac/ 2>&1 | tail -30 || echo "tfsec 不可用——按 checklist 人工核对"
```

## Step 4 · 报告输出（必做）

```bash
# 完整报告写文件（结论 + P0-P3 问题表 + 资源交叉验证结果 + 安全扫描 + plan 摘要）→ Issue 评论 @file：
cat > /tmp/qa-full-report.json << 'JSONEOF'
{"body":"[qa Agent] 审查报告\n\n{结论 ✅ LGTM / ❌ Request Changes}\n\n## 问题清单（P0-P3）\n| 级别 | 文件 | 行号 | 问题 | 建议 |\n|--|--|--|--|--|\n{逐条}\n\n## 资源规格交叉验证\n{已查结果 / 未交叉验证（AK/SK 无值）}\n\n## 安全扫描\n{结果}\n\n## terraform plan 摘要\n{create/update/destroy 资源数 + 验证项}"}
JSONEOF
grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json @/tmp/qa-full-report.json --json-out
# MR 评论（结论摘要——@file 或内联）：
grape git-repo pulls comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" \
  --json '{"body":"[qa Agent] {结论}——{摘要}"}' --json-out
```

## Step 5 · A2A 分流（必做——单节点决策）

> **决策规则**（按审查结论 + PR 状态，三分支选一；不得跳过——合并验证的 sre 交办是固定收尾）：
> - **分支 A（有问题）**：存在 P0/P1 未修复 → A2A 交办 `coding` 修复
> - **分支 B（通过 + 已合并）**：✅ LGTM 且 PR 已合并（或本次合并成功）→ **A2A 交办 `sre` 部署**——不得以「无问题」为理由省略
> - **分支 C（通过 + 未合并）**：✅ LGTM 但合并失败/未合并 → 不交办 sre（修复合并问题后由 merged 事件再交）

```bash
# ⚠️ 防重复（分支 A/B 都要）：先查 pipeline，已含目标 agent（coding:a2a_event / sre:a2a_event status≠failed）则不重复 A2A，只发评论
grape issues pipeline --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out

# ── 分支 A：交办 coding（有 P0/P1 时；issue_number 必填，pr_number 有则填，严禁混用）──
grape a2a handoff --json '{"agent":"coding","payload":{"repository":"'"$ACCOUNT/$REPO_NAME"'","account":"'"$ACCOUNT"'","issue_number":'"$ISSUE_NUMBER"',"pr_number":'"$PR_NUMBER"',"context":"PR #'"$PR_NUMBER"' 审查发现 {P0/P1 问题清单}，请修复"}}'

# ── 分支 B：先自动合并（幂等——已合并跳过；失败走分支 C）──
grape git-repo pulls merge --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out 2>&1 | head -30
# 合并确认（state=merged 后再 A2A sre——防未合并就交办空转）：
grape git-repo pulls get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out 2>&1 | grep -E '"state"|merged' | head -3
# ── 分支 B 续：交办 sre（通过 + 已合并；payload 用 @file 避免转义）──
cat > /tmp/a2a-sre-payload.json << EOF
{"agent":"sre","payload":{"repository":"$ACCOUNT/$REPO_NAME","account":"$ACCOUNT","issue_number":$ISSUE_NUMBER,"pr_number":$PR_NUMBER,"context":"PR #$PR_NUMBER 已合并，QA 审查通过（含资源规格交叉验证），交办部署验证"}}
EOF
grape a2a handoff --json @/tmp/a2a-sre-payload.json --json-out

# A2A 失败 → 重试 ≤3 次 → 仍失败：最终评论说明 + next_step 人工介入
```

## Step 收尾 · 任务完成流程（必做——任何分支都必须执行到最后）

```bash
grape tasks complete --json '{
  "result_text":  "结论：PR #'$PR_NUMBER' 审查完毕（{结论}）",
  "actions":      ["获取代码: PR #'$PR_NUMBER' diff", "自动化审查: 质量/功能/资源交叉验证", "安全检查: {结果}", "报告: Issue 全文 + PR 摘要", "合并: {已合并/未合并}", "A2A: sre/coding/无"],
  "conclusion":   "{LGTM/Request Changes}，{已合并并交办 sre / 已交办 coding / 待决策}",
  "artifacts":    ["PR #'$PR_NUMBER' 审查报告", "A2A handoff（如有）", "plan 摘要（如执行）"],
  "issue_summary":"{审查结论摘要——结论/问题级别/去向 一句话}",
  "next_step":    "a2a_sre（合并验证通过时）/ a2a_coding（有问题时）/ 等待人工决策"
}'
```

> ⚠️ **结束信号**：tasks complete 后立即结束任务，不要继续思考或等待输入。结构化字段 must 真实填充——结尾是下一节点的输入。

---

## 错误排查速查（命令返回异常时）

> ⚠️ 出错时**先看完整错误**（命令去掉 `| tail -N` 截断，`2>&1` 保留 stderr）；runbook 命令照抄仍失败，才允许 `grape commands <关键词>` / `grape <group> --help` 修正。**不反复盲试同一命令**。

| 现象 | 处理 |
|------|------|
| `No such command 'X'` | 命令名抄错，核对本 runbook；仍不确定再 `grape commands <关键词>` |
| `unknown request field(s): ...` | `--json` 有契约不允许字段 → `grape <group> <verb> --help` 看 Body fields |
| pulls get/files 返回空 | PR 无 diff 或编号错 → 核对 PR 编号（pulls list --state open 查）|
| merge 失败（冲突/403）| 分支 C：PR 评论告知等待人工处理——不提前交办 sre |
| terraform plan 失败（OBS backend 不通/provider 下载）| 看完整错误定位 → 修后重试 ≤2 次 → 仍失败：报告标注「plan 未执行」+ 原因 |
| 华为云查询失败 / 凭据缺失 | 报告标注「资源规格未交叉验证」——不阻塞不借用 |
| `comments create` 失败（长 body 转义）| 改用 `--json @file` |
| 401 凭据失效 | 说明「凭据已失效」即可，不换 token |
| 403 权限不足 | 说明需管理员配置——不绕行 |
| 报「契约过期/命令不识别」| 先 `grape refresh` 再重试 |