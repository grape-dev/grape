---
name: rest-api-skill-tester
version: 2.1.0
description: |
  REST API skill 功能验证（创建后 QA 门控）：凭据/地址检测（项目知识优先）→
  跑 skill 验证功能正确性 + 规范脚本化检查（frontmatter/必填段落/无 mock）→
  输出 Issue 报告 + PR 评论（✅ 通过自动合并 / 🔴 失败+原因）→ A2A 交棒 SRE Agent 发布。
  子命令动态识别：从 templates/test-vars.json 读取子命令列表，不写死。
tools:
  - curl
  - git
---

# REST API Skill Tester（通用REST API skill功能验证）v2.1

## 一、安全硬约束（必须遵守）

1. **平台交互一律走 grape CLI**（自动读取 `$GRAPE_SESSION_TOKEN`），严禁从数据库/configs/日志/历史中寻找其他 token。
2. **严禁读取 Grape 内部数据**（data/、configs/、sessions.db、user_accounts 等）。
3. **403/无权限**：立即停止该操作并回复用户，严禁尝试其他 token 或绕行手段。
4. **token 不得写入任何文件/日志/评论**。
5. **所有 curl / git 调用设 30s 超时**（curl `--max-time 30`）——超时判定失败，不 hang。
6. **命令以参考文件为准**：平台交互一律以 `references/grape-cli-reference.md` 为准——严禁根据经验/记忆/猜测自行拼写命令或 API 路径。
7. **域界声明**：被测 skill 的目标系统调用（跑测试命令、curl 探测目标 API）**属测试执行域，不是平台直连**——平台交互（评论/合并/A2A/任务）仍走 CLI。

## 二、环境变量表

| 变量 | 来源 | 用在哪 |
|------|------|--------|
| `GRAPE_SESSION_TOKEN` | 平台注入（grape 自动读取）| 平台交互（读 Issue/PR/评论/合并/A2A/任务）|
| `REPO_FULL_NAME` | 平台注入 | 仓库全名（命令参数用）|
| `ACCOUNT` | 平台注入/派生 | owner 名（命令参数用）|
| `REPO_NAME` | 派生 | `${REPO_FULL_NAME#*/}`——repo 名（命令参数用）|
| `ISSUE_NUMBER` | 平台注入 | Issue 编号（报告评论）|
| `PR_NUMBER` | 平台注入 | PR 编号（评论/合并/交棒）——前置条件③ 必填 |
| `{PREFIX}_BASE_URL` | 被测 skill 目标系统 | 测试调用——PREFIX 从被测 skill 的 test-vars.json skill_name 推导（如 blog-manager → BLOG_MANAGER）|
| `{PREFIX}_TOKEN` / `{PREFIX}_USERNAME` / `{PREFIX}_PASSWORD` / `{PREFIX}_API_KEY` | 被测 skill 目标系统 | 测试调用凭据（auth_type=none 时不需要）|

被测 skill 凭据获取：项目知识（`.project-info/` 下 JSON 的 secrets.{PREFIX}_*）优先 → 环境变量回退——**从 test-vars.json 读不到凭据**（它只含变量名说明不含值）；变量/JSON 值存在但为空算缺失——必须检查非空。

## 三、前置检查门（自愈 → 重试 → STOP）

| 检查项 | 判定 | 通过标准 | 失败动作 |
|--------|------|---------|---------|
| CLI 可用 | `grape --version` | ≥0.3.2 | 按 `references/grape-cli-installation.md` 自愈；仍不可用 → 「前置检查未通过：<原因>」结束任务 |
| 契约刷新 | `grape refresh` | 成功 | 重试 ≤2 次；仍失败 → 继续（回退内置快照）|
| Python 环境 | `python3 -c "import requests"` | ≥3.8 + requests 可用 | 缺 requests → pip install requests |
| PR_NUMBER 注入 | `[ -n "$PR_NUMBER" ]` | 有值 | 无 → 停止并说明（测试必须知道 PR）|
| 被测 skill 在 worktree | `ls skills/{name}/` | 目录存在（feat/issue-{N} 分支含 creator 产物）| 不存在 → 说明并结束 |
| 凭据/地址就绪 | 项目知识/环境变量扫描 | base_url 非空（auth_type≠none 时凭据也非空）| 缺失 → ⛔ 终止测试（报告缺凭据，不测）|
| gitleaks（安全扫描）| `command -v gitleaks` | 已装 | 未装 → 降级人工检查（非阻断）|

## 四、技能定义与执行

### 4.1 定位与职责边界

**职责**：REST API skill 创建后的 QA 门控——功能验证（Real-Environment First）+ 规范脚本化检查 + 安全扫描 + 测试报告（Issue/PR 评论）+ 通过 → 自动合并 + A2A SRE Agent。**只读验证**——不创建、不修改 skill 内容；测试发现需要修复 → A2A 回 Coding Agent，不自己修。

**依赖前序**：A2A 被调用时 prompt 含 creator 处理摘要（skill 名称/PR 编号）——通过 grape CLI 获取 PR 和 Issue 完整信息。

**动态识别子命令**：从 `templates/test-vars.json` 读 `test_cases` 数组（id/name/command/expected/method/category/description/cleanup）+ `entry_file`（固定 `scripts/{skill_name}.py`）——不预设子命令，测试范围由 test-vars.json 决定。

| 行为 | 态度 |
|------|------|
| 测试通过后发 Issue + PR 评论告知 | ✅ **必须做，不可跳过** |
| 测试失败后 A2A 回 Coding Agent | ✅ **必须做，不可跳过** |
| 测试通过后调合并脚本 + A2A SRE Agent | ✅ **必须做，不可跳过** |
| A2A 携带 issue_number 和 pr_number | ✅ **必须做** |
| 修改 skill 内容 | ❌ **禁止**（只读验证）|
| 直接合并 PR（不经合并脚本）| ❌ **禁止** |
| 读取 Grape 内部数据 | ❌ **禁止**（硬约束②）|

### 4.2 场景路由（入口 → 路径）

| 触发场景 | 处理路径 |
|---------|---------|
| 收到 A2A 交棒（creator → QA）| 4.3 流程 1 → 2 → 3 → 4 → 5 |
| 收到 pull_request_merged 事件 | 前置判断（见 4.3 前置）——已有自己「✅ 测试通过」标记 → 直接 A2A SRE；无 → 正常测试流程 |
| 非 skill 测试类请求 | 不处理，回复说明范围（4.8 收尾）|

Don't use for: 创建 skill（creator 职责）/ 修改 skill 内容（只读验证）/ 发布 skill（publish 职责）。

**入参契约**（A2A payload）：repository / account / issue_number / pr_number（必填）/ context。

### 4.3 处理流程（统一骨架）

**前置判断（仅 pull_request_merged 事件）**：读 Issue 评论历史（`grape git-repo issues comments list`），查找自己之前的「[QA Agent] ✅ 测试通过」标记：
- 找到 → PR 之前已测试通过 → 直接 A2A SRE Agent（跳 1-5）
- 找不到 → 正常测试流程

**流程 1：功能验证（Real-Environment First）**
- 定位 skill：当前 worktree 的 `skills/{name}/`（从 A2A context / Issue 拿 name）
- **执行命令前先 `cd skills/{name}/`**（test-vars.json 的 command 用相对路径如 `python3 scripts/{name}.py`——需在 skill 目录下执行）
- 读子命令列表：`templates/test-vars.json` 的 test_cases + entry_file
- API 地址获取：从 test-vars.json 的 skill_name 推导前缀 → 项目知识（.project-info/ JSON config.{PREFIX}_BASE_URL）优先 → 环境变量 `{PREFIX}_BASE_URL` 回退 → 都空 → ⛔ 终止
- **获取到 base_url 必须 export 成环境变量**（`export {PREFIX}_BASE_URL=...`）——否则脚本读不到地址卡在交互输入
- 凭据检测：前缀推导 → 项目知识（secrets.{PREFIX}_*）→ 环境变量扫描（{PREFIX} 开头 + USERNAME/PASSWORD/TOKEN/API_KEY）→ 读到后 export；auth_type=none 跳过凭据检测；凭据缺失 → ⛔ 终止
- 测试策略（按 HTTP 方法分类动态适配）：
  - **只读查询类**（GET：list-* / get-*）→ 自动真实调用 API，验证返回数据（非空/字段完整/退出码 0）
  - **变更类**（POST/PUT/DELETE：create-* / update-* / delete-*）→ 自动执行 + 自动清理（不留垃圾数据）——**同一资源按生命周期顺序**：先 create 拿真实 ID（test-vars.json 里 `--id 1` 是示例值——用 create 返回的真实 ID 替换），再用真实 ID 执行 get/update/delete；文件上传测试前自动建临时测试文件（`echo "test" > /tmp/test-upload.txt`），测完清理
  - **cleanup 执行**：test-vars.json 的 cleanup 是描述性文本（如「执行 delete-article 删除刚创建的测试资源」）——agent 执行 create 后从返回 JSON **递归找 ID**（顶层 id/article_id/aid → 嵌套 data.id/data.article_id），自行拼接 cleanup 命令执行；找不到 ID → 记录警告「⚠️ cleanup 未执行：返回结果无 ID 字段」（不算测试失败）
- 失败 → 记录原因（退出码/报错/API 错误码）
- ⚠️ Real-Environment First——**不使用 mock 模式**

**流程 2：规范检查（脚本化）**
```bash
python3 {{SKILL_DIR}}/rest-api-skill-tester/scripts/spec_check.py <skill_dir>
```
- 检查 frontmatter / 文件结构 / version 格式 / mock 模式（严禁）
- 检查 SKILL.md 段落规范：必填（场景/知识/步骤）+ 可选（判断标准/输出规范/参考文档）——对齐 `references/skill-spec.md` 规范（生成物规范，非平台框架）
- 段落名匹配中英文别名（场景/When to Use、知识/Knowledge、步骤/Steps）；必填段落缺失 = spec_check 失败

**流程 3：安全扫描（gitleaks）**
```bash
gitleaks detect --source <skill_dir> --no-git     # 已装时
```
- 未装 → 降级人工检查；发现密钥 → 🔴 失败

**流程 4：报告（Issue 评论 + PR 评论——必做）**——格式见 `references/test-report-format.md`：
```bash
# Issue 评论（固定标记开头——保证可检索性）：
grape git-repo issues comments create \
  --owner "$REPO_OWNER" --repo "$REPO_NAME" \
  --number "$ISSUE_NUMBER" \
  --json '{"body": "[QA Agent] ✅ 测试通过\n\n## Skill 测试报告：{name} v{version}\n\n## 功能验证\n| 检查项 | 状态 | 证据 | 说明 |\n|--------|------|------|------|\n| API 调用 | ✅ | 返回 N 条数据 | 真实调用成功 |\n\n## 结论\n✅ 测试通过，可合并发布"}'
# PR 评论：
grape git-repo pulls comments create \
  --owner "$REPO_OWNER" --repo "$REPO_NAME" \
  --number "$PR_NUMBER" \
  --json '{"body": "[QA Agent] ✅ 测试通过"}'
```
失败时同样两条评论（`[QA Agent] 🔴 测试失败` + 报告含失败原因/证据/修复建议）。

**流程 5：合并 + A2A 交棒（测试通过后）**——统一自动合并（mode=merge）：
```bash
bash {{SKILL_DIR}}/rest-api-skill-tester/scripts/merge_skill_pr.sh $PR_NUMBER
```
- 脚本输出 `MERGED=1/0` + `BRANCH_DELETED=1/0` + `MESSAGE`
- 合并后查源分支删除状态（pulls merge 无 prune_branch 参数——分支可能残留；grape CLI 0.3.3 无 branches delete 接口无法自动删）：
  - `BRANCH_DELETED=1` → 平台已清理 ✅
  - `BRANCH_DELETED=0` → Issue 评论告警「分支未删除，请手动清理」
- **MERGED=1 → A2A SRE Agent**（payload context 注明「测试通过，已自动合并，请发布」）
- **MERGED=0 → 自动合并失败** → 评论 PR + Issue：「✅ 测试通过，自动合并失败，请手动合并」（不 A2A）

**测试失败 → A2A 回 Coding Agent**：payload context 注明「测试失败：{原因摘要}。请修复后重新 push」。

### 4.4 命令标准（子集与坑点）

**grape CLI（平台交互）**——见 `references/grape-cli-reference.md` 完整子集 + ❌ 警示表。核心：
```bash
grape --version / grape refresh                              # 前置门
grape git-repo issues detail --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out   # 读 Issue 全文
grape git-repo pulls get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out            # 读 PR
grape git-repo issues comments list / create                 # 前置判断 / 报告
grape git-repo pulls comments create                         # PR 报告
grape a2a handoff --json '{"agent":"sre/coding","payload":{...}}'   # 交棒
grape tasks complete --json '{...}'                          # 收尾（4.8）
```
（合并全走 `scripts/merge_skill_pr.sh`——内部 pulls get/merge——不手动敲）

**坑点（契约）**：
- `pulls merge` **无 prune_branch 参数**——源分支可能残留，按 4.3 流程 5 告警处理
- **grape CLI 0.3.3 无 branches delete 接口**——无法自动删分支
- 测试命令**必须在 skill 目录下执行**（相对路径依赖 cwd）；base_url/凭据**必须 export**（否则卡交互输入）

**目标系统探测（curl——测试执行域，合法）**：被测 skill 脚本的运行时调用（不走 curl 手测——用被测脚本 `python3 scripts/{name}.py` 真实调用）。

**❌ 不存在的命令（警示）**：见 `references/grape-cli-reference.md` §4（tasks close/mine/start/status/clarify、git 透传、grape api 兜底、knowledge context/content、events detail --seq、curl 直连平台 API）。

### 4.5 故障决策表

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

### 4.6 输出格式（固定产物）

**评论前缀硬规**：所有 Issue/PR 评论 body 必须以 `[QA Agent]` 开头；测试结果固定标记放 body 开头独立行：✅ 测试通过 / 🔴 测试失败（保证可检索性——前置判断依赖此标记）。

**报告格式**：见 `references/test-report-format.md`（检查项/状态/证据/影响/修复建议表格）。

**分析结论示例**：
```
成功：
分析结论：
- 场景：功能验证（QA Agent 被 Coding Agent A2A 触发）
- Skill：{skill_name} v{version}
- 测试结果：✅ 通过（API 调用成功，返回 N 条数据）
- 必须执行：报告 Issue+PR 评论 → 调合并脚本 → A2A SRE Agent

失败：
分析结论：
- 场景：功能验证
- Skill：{skill_name} v{version}
- 测试结果：🔴 失败（API 调用超时 / spec_check 失败）
- 必须执行：报告 Issue+PR 评论 → A2A 回 Coding Agent
```

**收尾 JSON 块**（最终答复末尾输出——平台读取，只输出一个）：
```json
{
  "actions": ["做了什么，如 tested:rest-api-skill-xxx / merged:PR#N / a2a:sre / a2a:coding"],
  "conclusion": "处理结论（测试通过已自动合并 / 测试失败 / 自动合并失败）",
  "artifacts": ["产生的产物：测试报告评论 / PR 评论 / 合并状态"],
  "next_step": "下一步建议",
  "issue_summary": "Issue 最新聚合结论（紧凑文本，用 | 分隔）"
}
```
不得用长流程叙述替代 JSON 块；无法确定动作/结论 → 填空数组/空字符串，不伪造。

### 4.7 写操作协议

| 动作 | 确认要求 |
|------|---------|
| Issue/PR 评论（测试报告）| **必做**——测试完成后两条都要发（4.6 固定标记）|
| 合并 PR（脚本封装 pulls merge）| 流程内（测试通过后直接执行——不经脚本禁止）|
| A2A 交棒（sre 发布 / coding 修复）| 流程内（必带 issue_number/pr_number）|
| 修改 skill 内容 | ❌ **禁止**（只读验证——修复由 coding Agent 做）|

### 4.8 任务收尾（必做）

1. **先调 `grape tasks complete` 关闭当前任务**（任务中心闭环）：
```bash
grape tasks complete --json '{"result_text": "<处理结论>", "actions": ["做了什么，如 tested:rest-api-skill-xxx / merged:PR#N / a2a:sre / a2a:coding"], "conclusion": "处理结论（测试通过已自动合并 / 测试失败 / 自动合并失败）", "artifacts": ["产生的产物：测试报告评论 / PR 评论 / 合并状态"], "next_step": "下一步建议"}'
```
2. **再在最终答复末尾输出结构化 JSON 块**（4.6 格式——actions/conclusion/artifacts/issue_summary must 真实填充）。
3. 结束信号：complete 后立即结束任务，不要继续思考或等待输入。

## 五、参考文件清单

| 文件 | 内容 | 何时用 |
|------|------|--------|
| `references/skill-spec.md` | 被测 skill 的段落规范（生成物规范） | 流程 2 规范检查 |
| `references/test-report-format.md` | 测试报告格式模板 | 流程 4 报告 |
| `references/grape-cli-reference.md` | grape 命令全表 + ❌ 警示表 | 平台交互 |
| `references/grape-cli-installation.md` | CLI 安装/升级指引 | 前置门 CLI 自愈 |
| `references/tester-runbook.md` | 执行走查表（命令固化——照着执行不查 help）| 全流程 |
| `scripts/spec_check.py` | 规范检查脚本（frontmatter/段落/无 mock）| 流程 2 |
| `scripts/merge_skill_pr.sh` | 合并脚本（pulls get/merge——MERGED/BRANCH_DELETED）| 流程 5 |

## 六、Best Practices（实践积累区）

- **Real-Environment First**：全部真实 API 调用——不使用 mock 模式。
- **生命周期测试**：变更类同一资源按 create → get → update → delete 顺序跑——create 真实 ID 复用——测完清理不留垃圾数据。
- **动态识别**：子命令从 test-vars.json 读——不预设——测试范围与 creator 生成物自动对齐。
- **固定标记可检索**：✅/🔴 标记放 body 开头独立行——前置判断（pull_request_merged）靠它识别。
- **只读不动手**：发现问题 A2A 回 Coding Agent——本技能绝不自己修。
- **凭据纪律**：base_url/凭据从项目知识/环境变量拿——test-vars.json 不含真实值——缺失则 ⛔ 终止不猜。