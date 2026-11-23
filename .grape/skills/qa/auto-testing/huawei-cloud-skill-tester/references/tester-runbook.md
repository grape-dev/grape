# Huawei Cloud Skill 测试执行走查表（huawei-cloud-skill-tester runbook）

> 目的：把 huawei-cloud-skill-tester 每一步**要做什么 + 用哪条确切命令**固化下来，Agent **照着执行、不要查 `--help` / `grape commands`**。
> **CLI 纪律**：平台交互（读 Issue/PR/评论/合并/A2A/任务）一律走 grape CLI——**禁止 curl 直连平台 API**；curl 仅用于被测云服务 API 探测（测试执行域——合法）。
> **只读纪律**：本技能是 QA 门控——**只读验证，绝不修改被测 skill 内容**；发现需修复 → A2A 回 Coding Agent，不自己修。
> **凭据纪律**：华为云 AK/SK 仅从环境变量 / hcloud CLI profile 读取，**绝不要求用户在对话中输入 AK/SK**（三选项协议见 SKILL「Agent Protocol」章节）。
> 环境变量直接使用：`$REPO_OWNER`（仓库属主）、`$REPO_NAME`（= `${REPO_FULL_NAME#*/}`）、`$ISSUE_NUMBER`、`$PR_NUMBER`、`$SKILL_INSTALL_DIR`；`$ACCOUNT` 为触发者 actor（一般不用作 `--owner`）。
> **评论前缀硬规**：所有评论 body 必须以 `[QA Agent]` 开头；测试结果固定标记放 body 开头独立行（✅ 测试通过 / 🔴 测试失败——保证可检索性）。

---

## Step 0 · 任务上下文与就绪（必须最先，任何事件都做）

```bash
# ⚠️ CLI 前置自愈（最先）：`grape --version` ≥0.3.2；未装/版本过低 → 按 references/grape-cli-installation.md
#    自动安装/升级（timeout 300 pip install git+https://gitcode.com/grape-dev/grape-cli.git）→ 装好再验证一次
#    → 仍不可用 → 输出「前置检查未通过：<原因>」并结束任务（不降级直连）
grape --version
grape refresh 2>&1 && echo REFRESH_OK        # 契约保鲜；失败重试 ≤2 次后回退内置快照继续

# 质量上报 CLI 就绪（幂等，不阻塞）——本技能每次运行需上报一条 quality 记录
bash {{SKILL_DIR}}/huawei-cloud-skill-tester/scripts/ensure_cli.sh

# 测试环境
command -v gitleaks && echo GITLEAKS_OK || echo "gitleaks 未装——降级 grep 人工检查（非阻断）"
[ -n "$PR_NUMBER" ] && echo PR_OK || echo "PR_NUMBER 缺失——停下说明（测试必须知道 PR）"
grape tasks current --json-out               # A2A payload 在这里（skill 名称/PR 编号——creator 交棒信息）
grape git-repo pulls get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out 2>&1 | head -80   # 读 PR 确认被测对象
```

## 前置判断（仅 pull_request_merged 事件时执行）

```bash
# 读 Issue 评论历史，找自己之前的「[QA Agent] ✅ 测试通过」标记：
grape git-repo issues comments list --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out | head -120
# 找到标记 → PR 之前已测试通过 → 直接 A2A SRE Agent（跳流程 1-5——见下）
# 找不到 → 正常测试流程
```

## 流程 1 · 功能验证（三轨八阶段流水线）

```bash
# ① 定位被测 skill：skill 名从 A2A context / Issue / PR diff 拿（huawei-cloud-* 规范命名）
#    目录：当前 worktree skills/{path}/，或 $SKILL_INSTALL_DIR/huawei-cloud/{skill-name}/
# ② 凭据前置（框架退出码 77 时触发三选项协议）：
#    框架 stderr 输出三选项：① 机器 owner 带外 export HUAWEI_ACCESS_KEY/HUAWEI_SECRET_KEY 等
#   ② grape 环境在平台「通用设置」菜单配置凭据；③ DRYRUN_MODE=1 以 dryrun/mock 继续（用例标 dryrun，绝不 pass）
#    ⚠️ Agent 必须把三选项原样发 Issue 评论并**等待用户选择**；绝不跳过需要凭据的步骤；
#      NEVER 让用户在对话中输入或粘贴 AK/SK
# ③ 跑全流水线（可写用例默认跳过——写用例需 ALLOW_WRITES=1 才真实执行）：
bash scripts/run-test-pipeline.sh --skills "<skill-name>" [--skill-path <dir>] [--fresh]
# ④ 多技能联动 / 编排场景（Phase 5-6，按 PR 范围或场景需要）：
bash scripts/tier2/phase-5-orchestration.sh --skills "skill-a, skill-b"
bash scripts/tier2/phase-6-full-flow.sh --skill "<skill-name>"
# ⑤ 产物：<skill-name>-test-files/reports/report-<timestamp>/test-report.{json,md}
# 失败 → 记录原因（退出码/报错/API 错误码）；合规步骤用 grep 佐证（见流程 2）
```

> 探索性查询一律 `--limit=1`；hcloud 命令执行一律经被测技能自身的包装入口（合规技能已内置 `hcloud-run.sh` / `skill-quality-cli run` 包裹——禁止裸调 hcloud）。

## 流程 2 · 规范检查（华为云检查规范，脚本化 + grep 佐证）

```bash
# 被测技能目录校验（逐项）：
# ① Frontmatter 可解析 + name 与目录一致 + 命名 huawei-cloud-{产品}-{功能}
python3 -c "import yaml,io; d=yaml.safe_load(io.open('<skill_dir>/SKILL.md',encoding='utf-8').read().split('---')[1]); assert d['name']==d['name']" 2>/dev/null && echo NAME_OK
# ② SKILL.md ≤ 500 行；技能包文件数 ≤ 40、单文件 ≤5MB；扩展名 46 种白名单
find <skill_dir> -type f | wc -l
# ③ references 四件套（涉及 CLI/IAM 时）：cli-installation-guide / iam-policies / verification-method / acceptance-criteria
ls <skill_dir>/references/
# ④ 无硬编码 AK/SK（复用流程 3 gitleaks + grep 兜底）
```
- 任一 Critical 项不过 → 🔴 失败 → A2A 回 Coding Agent

## 流程 3 · 安全扫描（gitleaks）

```bash
gitleaks detect --source <skill_dir> --no-git
# 已装才跑；未装 → 降级 grep 人工检查（非阻断）：
grep -rn -i -E "access[_-]?key|secret[_-]?key" <skill_dir> --include="*.md" --include="*.py" --include="*.sh" | grep -v "references/iam-policies" | head
# 发现密钥 → 🔴 失败
```

## 流程 4 · 报告（Issue 评论 + PR 评论——必做，两次都要发）

```bash
# ⚠️ 测试完成后必须发 Issue 评论 + PR 评论（固定标记开头；详细格式见 references/acceptance-criteria.md 报告段）：
# Issue 评论（长 body 先写文件再 --json @file）：
cat > /tmp/tester-issue-comment.json <<'EOF'
{"body":"[QA Agent] ✅ 测试通过\n\n## Skill 测试报告：<skill-name> v<version>\n\n| 检查项 | 状态 | 证据 | 说明 |\n|--------|------|------|------|\n| 三轨八阶段流水线 | ✅ | PASS: N FAIL: M | 真实/DRYRUN 模式 |\n| 规范检查 | ✅ | yaml/命名/四件套 | 对表逐项 |\n| 安全扫描 | ✅ | gitleaks 0 处 | 无密钥 |\n\n## 结论\n✅ 测试通过，可合并发布"}
EOF
grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" \
  --number "$ISSUE_NUMBER" --json @/tmp/tester-issue-comment.json --json-out
# PR 评论：
grape git-repo pulls comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" \
  --number "$PR_NUMBER" --json '{"body": "[QA Agent] ✅ 测试通过"}' --json-out
# 失败时同样两条评论（[QA Agent] 🔴 测试失败 + 失败原因/证据/修复建议）
# 三选项凭据等待期间：Issue 评论发三选项文本并注明「等待用户选择」
```

## 流程 5 · 合并 + A2A 交棒（测试通过后——统一自动合并 mode=merge）

```bash
bash {{SKILL_DIR}}/huawei-cloud-skill-tester/scripts/merge_skill_pr.sh "$PR_NUMBER"
# 脚本输出：MERGED=1/0 + BRANCH_DELETED=1/0 + MESSAGE（grape pulls merge，无分支删除接口时降级告警）
```
- **MERGED=1 且 BRANCH_DELETED=1** → A2A SRE Agent（发布）
- **MERGED=1 且 BRANCH_DELETED=0** → Issue 评论告警「分支未删除，请手动清理」→ 仍 A2A SRE Agent
- **MERGED=0** → 评论 PR + Issue：「✅ 测试通过，自动合并失败，请手动合并」——**不 A2A SRE**
- **测试失败** → 不合并 → A2A 回 Coding Agent（修复）

```bash
# A2A SRE（测试通过+合并成功）：
grape a2a handoff --json '{
  "agent": "sre",
  "payload": {
    "repository": "'"$REPO_FULL_NAME"'",
    "account": "'"$ACCOUNT"'",
    "issue_number": '"$ISSUE_NUMBER"',
    "pr_number": '"$PR_NUMBER"',
    "context": "测试通过，已自动合并。Skill：<skill-name>，请发布到 ClawHub/GitHub/GitCode"
  }
}'
# A2A Coding（测试失败——回修）：
grape a2a handoff --json '{
  "agent": "coding",
  "payload": {
    "repository": "'"$REPO_FULL_NAME"'",
    "account": "'"$ACCOUNT"'",
    "issue_number": '"$ISSUE_NUMBER"',
    "pr_number": '"$PR_NUMBER"',
    "context": "测试失败：<原因摘要>。请修复后重新 push"
  }
}'
```

## 前置判断命中「已测试通过」→ 直接 A2A SRE Agent（含合并状态追溯）

```bash
grape a2a handoff --json '{
  "agent": "sre",
  "payload": {
    "repository": "'"$REPO_FULL_NAME"'",
    "account": "'"$ACCOUNT"'",
    "issue_number": '"$ISSUE_NUMBER"',
    "pr_number": '"$PR_NUMBER"',
    "context": "pull_request_merged——此前已测试通过（有 ✅ 标记），请发布 Skill：<skill-name> 到 ClawHub/GitHub/GitCode"
  }
}'
```

## 步骤 收尾 · 任务完成流程（必做——任何分支都必须执行到最后）

```bash
grape tasks complete --json '{
  "result_text": "<处理结论>",
  "actions": ["做了什么，如 tested:<skill-name> / merged:PR#N / a2a:sre / a2a:coding"],
  "conclusion": "<处理结论（测试通过已自动合并 / 测试失败 / 自动合并失败）>",
  "artifacts": ["产生的产物：测试报告评论 / PR 评论 / 合并状态"],
  "next_step": "<下一步建议>"
}'
# 最终答复末尾输出结构化 JSON 块（actions/conclusion/artifacts/issue_summary must 真实填充）
```

> ⚠️ **结束信号**：complete 后立即结束任务，不要继续思考或等待输入。

---

## 错误排查速查（命令返回异常时）

> ⚠️ 出错时**先看完整错误**；runbook 命令照抄仍失败，才允许查 `grape commands` / API 文档。**不反复盲试同一命令**。

| 现象 | 处理 |
|------|------|
| 框架退出码 77（AK/SK 缺失）| 把三选项原样发 Issue 评论并等用户选择：① 机器 owner 带外 export 环境变量 ② grape 平台「通用设置」配置凭据 ③ `DRYRUN_MODE=1` dryrun 继续（用例标 `dryrun`，报告显式标注，绝不 pass）——**绝不静默跳过** |
| `DRYRUN_MODE=1` 下的用例 | 一律标记 `dryrun`，不得呈现为 `pass`；报告须显式标注 dryrun 模式 |
| 写用例（Phase 4/6）被跳过 | 正常——`ALLOW_WRITES=1` 才真实执行写用例；如需写验证，告知用户并等其确认后置 1 重跑 |
| 找不到被测 skill | `--skill-path` 指定 worktree 目录；或检查 `$SKILL_INSTALL_DIR` 下是否有该 `huawei-cloud-*` 技能 |
| 被测技能命令涉嫌裸调 hcloud | 记入报告（合规问题）→ 🔴 → A2A 回 Coding Agent |
| gitleaks 未装 | 降级 grep 人工检查（非阻断）|
| gitleaks / grep 发现密钥 | 🔴 失败 → A2A 回 Coding Agent |
| 规范检查 Critical 项不过 | 🔴 失败（必填段落/命名/规模缺失 = 失败）→ A2A 回 Coding Agent |
| 合并脚本 MERGED=0 | 评论「✅ 测试通过，自动合并失败，请手动合并」——不 A2A SRE |
| 合并后 BRANCH_DELETED=0 | Issue 评论告警「分支未删除，请手动清理」——不影响 A2A |
| 命令 403/无权限 | 立即停止该操作并回复用户（不换 token 不绕行）|
| 401/凭据失效 | 说明凭据已失效（tk_ 短命属正常）——不替换不绕行 |
| 质量上报失败（report 不可达）| 非阻塞——技能/流水线照常完成，WARNING 记录即可 |
| 报「契约过期/命令不识别」| 先 `grape refresh` 再重试（前置门②）|