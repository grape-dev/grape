# REST API Skill 测试执行走查表（rest-api-skill-tester runbook）

> 目的：把 rest-api-skill-tester 每一步**要做什么 + 用哪条确切命令**固化下来，Agent **照着执行、不要查 `--help` / `grape commands`**。
> **CLI 纪律**：平台交互（读 Issue/PR/评论/合并/A2A/任务）一律走 grape CLI——**禁止 curl 直连平台 API**；curl 仅用于**被测 skill 目标系统探测**（测试执行域——合法）。
> **只读纪律**：本技能是 QA 门控——**只读验证，绝不修改 skill 内容**；发现需修复 → A2A 回 Coding Agent，不自己修。
> 环境变量直接使用：`$ACCOUNT`、`$REPO_NAME`（= `${REPO_FULL_NAME#*/}`）、`$ISSUE_NUMBER`、`$PR_NUMBER`；被测凭据 `{PREFIX}_BASE_URL` 等（PREFIX 从被测 skill 的 test-vars.json skill_name 推导）。
> **评论前缀硬规**：所有评论 body 必须以 `[QA Agent]` 开头；测试结果固定标记放 body 开头独立行（✅ 测试通过 / 🔴 测试失败——保证可检索性）。

---

## Step 0 · 任务上下文与就绪（必须最先，任何事件都做）

```bash
# ⚠️ CLI 前置自愈（最先）：`grape --version` ≥0.3.2；未装/版本过低 → 按 references/grape-cli-installation.md
#    自动安装/升级（timeout 300 pip install git+https://gitcode.com/grape-dev/grape-cli.git）→ 装好再验证一次
#    → 仍不可用 → 输出「前置检查未通过：<原因>」并结束任务（不降级直连）
grape --version
grape refresh 2>&1 && echo REFRESH_OK        # 契约保鲜；失败重试 ≤2 次后回退内置快照继续
python3 -c "import requests" 2>&1 && echo REQUESTS_OK   # Python 环境（≥3.8 + requests——被测 skill 依赖）
command -v gitleaks && echo GITLEAKS_OK || echo "gitleaks 未装——降级人工检查（非阻断）"
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

## 流程 1 · 功能验证（Real-Environment First）

```bash
# ① 定位 skill + 读子命令列表：
#    （skill 名从 A2A context / Issue 拿；当前 worktree 的 skills/{name}/）
cd skills/{name}/                                  # ⚠️ 必须先 cd——test-vars 的 command 是相对路径
cat templates/test-vars.json                       # test_cases[]（id/name/command/expected/method/category/description/cleanup）+ entry_file
# ② API 地址获取（从项目知识/环境变量——不从 test-vars.json 读）：
#    前缀从 test-vars.json skill_name 推导（blog-manager → BLOG_MANAGER）
#    优先 .project-info/ 下 JSON config.{PREFIX}_BASE_URL → 回退环境变量 {PREFIX}_BASE_URL → 都空 → ⛔ 终止
export {PREFIX}_BASE_URL="<获取到的地址>"          # ⚠️ 必须 export——否则脚本卡交互输入
# ③ 凭据检测（auth_type=none 跳过）：
#    项目知识 .project-info/ JSON secrets.{PREFIX}_* → 回退扫描 {PREFIX} 开头含 USERNAME/PASSWORD/TOKEN/API_KEY 的环境变量
#    ⚠️ 值存在但为空也算缺失——必须检查非空；凭据缺失 → ⛔ 终止测试
export {PREFIX}_TOKEN="<获取到的令牌>"             # （按认证方式对应变量）
# ④ 跑测试用例（按 method 分类动态适配）：
python3 scripts/{name}.py health-check             # 只读：真实调用验证返回数据（非空/字段完整）
python3 scripts/{name}.py list-articles --page 1 --size 10
# 变更类：同一资源按生命周期顺序——先 create 拿真实 ID，用真实 ID 做 get/update/delete：
ID=$(python3 scripts/{name}.py create-article --title test --content hello | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id') or d.get('data',{}).get('id'))")
python3 scripts/{name}.py get-article --id "$ID"        # 验证创建结果
python3 scripts/{name}.py update-article --id "$ID" --title updated
python3 scripts/{name}.py delete-article --id "$ID"     # 清理不留垃圾数据
# ⚠️ test-vars.json 里 update/delete 的 --id 1 是示例值——必须用 create 返回的真实 ID 替换
# ⚠️ 返回 JSON 找不到 ID → 记录警告「⚠️ cleanup 未执行：返回结果无 ID 字段」（不算测试失败）
# 失败 → 记录原因（退出码/报错/API 错误码）
```

## 流程 2 · 规范检查（脚本化）

```bash
python3 {{SKILL_DIR}}/rest-api-skill-tester/scripts/spec_check.py <skill_dir>
# 退出码 0 = 通过（frontmatter/文件结构/version 格式/无 mock/必填段落【场景/知识/步骤】全在——对齐 references/skill-spec.md）
# 必填段落缺失 = spec_check 失败（🔴）
```

## 流程 3 · 安全扫描（gitleaks）

```bash
gitleaks detect --source <skill_dir> --no-git
# 已装才跑；未装 → 降级人工检查（非阻断）；发现密钥 → 🔴 失败
```

## 流程 4 · 报告（Issue 评论 + PR 评论——必做，两次都要发）

```bash
# ⚠️ 测试完成后必须发 Issue 评论 + PR 评论（固定标记开头——格式见 references/test-report-format.md）：
# Issue 评论：报告正文先写 /tmp/tester-issue-body.md（纯 markdown 免转义），再转 JSON @file（jq 自动转义；禁 printf 拼 JSON）：
cat > /tmp/tester-issue-body.md <<'EOF'
[QA Agent] ✅ 测试通过

## Skill 测试报告：{name} v{version}

## 功能验证
| 检查项 | 状态 | 证据 | 说明 |
|--------|------|------|------|
| API 调用 | ✅ | 返回 N 条数据 | 真实调用成功 |

## 结论
✅ 测试通过，可合并发布
EOF
jq -Rs '{body: .}' < /tmp/tester-issue-body.md > /tmp/tester-issue-comment.json
grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" \
  --number "$ISSUE_NUMBER" --json @/tmp/tester-issue-comment.json --json-out
# PR 评论：
grape git-repo pulls comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" \
  --number "$PR_NUMBER" --json '{"body": "[QA Agent] ✅ 测试通过"}' --json-out
# 失败时同样两条评论（[QA Agent] 🔴 测试失败 + 失败原因/证据/修复建议）
```

## 流程 5 · 合并 + A2A 交棒（测试通过后——统一自动合并 mode=merge）

```bash
bash {{SKILL_DIR}}/rest-api-skill-tester/scripts/merge_skill_pr.sh "$PR_NUMBER"
# 脚本输出：MERGED=1/0 + BRANCH_DELETED=1/0 + MESSAGE
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
    "context": "测试通过，已自动合并。Skill：{name}，请发布到 ClawHub/GitHub/GitCode"
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
    "context": "测试失败：{原因摘要}。请修复后重新 push"
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
    "context": "pull_request_merged——此前已测试通过（有 ✅ 标记），请发布 Skill：{name} 到 ClawHub/GitHub/GitCode"
  }
}'
```

## 步骤 收尾 · 任务完成流程（必做——任何分支都必须执行到最后）

```bash
grape tasks complete --json '{
  "result_text": "<处理结论>",
  "actions": ["做了什么，如 tested:rest-api-skill-xxx / merged:PR#N / a2a:sre / a2a:coding"],
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
| base_url 缺失 | ⛔ 终止测试——报告「缺 API 地址配置」 |
| 凭据缺失/为空（auth_type≠none）| ⛔ 终止测试——报告「缺凭据」（不测——不猜凭据）|
| auth_type=none | 跳过凭据检测，直接用 base_url 调 API |
| 只读查询返回空/异常 | 记录失败原因 + 断言数据（非空/字段完整）|
| 变更类 create 返回无 ID | ⚠️ 记录「cleanup 未执行：返回结果无 ID 字段」——不算失败 |
| gitleaks 未装 | 降级人工检查（非阻断）|
| gitleaks 发现密钥 | 🔴 失败 → A2A 回 Coding Agent |
| spec_check 退码非 0 | 🔴 失败（必填段落缺失 = 失败）→ A2A 回 Coding Agent |
| 合并脚本 MERGED=0 | 评论「✅ 测试通过，自动合并失败，请手动合并」——不 A2A SRE |
| 合并后 BRANCH_DELETED=0 | Issue 评论告警「分支未删除，请手动清理」——不影响 A2A |
| 命令 403/无权限 | 立即停止该操作并回复用户（硬约束③——不换 token 不绕行）|
| 401/凭据失效 | 说明凭据已失效（tk_ 短命属正常）——不替换不绕行 |
| 被测脚本卡在交互输入 | base_url/凭据没 export——export 后重跑 |
| 报「契约过期/命令不识别」| 先 `grape refresh` 再重试（前置门②）|