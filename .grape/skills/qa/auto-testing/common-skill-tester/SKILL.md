---
name: common-skill-tester
description: >
  Use when the user asks to lint, test, run functional tests, audit, security-scan, or
  vulnerability-check a Skill directory. A unified tester integrating three toolkits in one:
  Linter (structure & spec compliance), Tester (runtime functional verification), and Auditor
  (security audit, malicious code, CWE vulnerabilities). Triggers: "检查 skill", "扫描 skill",
  "lint skill", "测试 skill", "功能测试", "运行测试", "功能验证", "安全审计", "安全测试",
  "漏洞检测", "恶意代码检测", "安全评分", "skill 规范", "skill-linter", "skill-tester",
  "skill-auditor", "common-skill-tester", "检查报告", "validate skill", "test skill",
  "audit skill", "check skill", "lint my skill", "帮我检查skill", "帮我测试skill",
  "帮我审计skill", "检测恶意代码".
version: 2.4.1
---

# Common Skill Tester

> 本技能遵循统一技能框架 v2.1.0（与 common-skill-creator 同构）。

## 一、安全硬约束（必须遵守）

1. **流水线 QA 节点定位**：本技能对 Skill 目录做**只读三合一检查**（Lint 规范 / Test 功能 / Audit 安全），并在**流水线场景**承接 QA 门控——测试 → 双评论报告（Issue + PR）→ **✅/⚠️ 通过 → 自动合并 PR → A2A 交棒 sre；❌ 失败 → A2A 交棒 coding**。平台命令面 = 评论（`issues comments list/create` + `pulls comments create`）+ 合并（走 `scripts/merge_skill_pr.sh`——**不手敲 pulls merge**）+ `a2a handoff` + 4.8 `tasks complete`；**不修改被测 Skill、不发布（sre 职责）、不建 PR（creator 职责）**。**平台 API 返回 403/401 → 立即停止操作**，回复「该操作需要管理员/维护者权限，请在仓库设置中配置后重试」——**不尝试其他 token、不绕行**。
2. **动态规范拉取**：三份规范 JSON（lint `common_skillslinter.json` / test `common_functions.json` / audit `common_auditor.json`）**不硬编码、不凭记忆、不用缓存**——每次执行从在线地址拉取最新版（`scripts/fetch_specs.js`，raw → gitcode API v5 自动回退）；拉取失败按 4.5 降级（该套件标记「未执行(原因)」+ 本地文件指定恢复），**不得**用旧快照/记忆假装执行。
3. **外部域界**：fetch_specs.js 访问的在线规范源（gitcode.com 生态规范文件仓库）属**外部规范源域**——脚本内 https 请求合法；此域与 grape 平台管理 API 严格区分（平台管理一律走 CLI 纪律，此处无 CLI 通道也不制造）。
4. **客观证据判定**：每项 pass/fail/warn/skip 必须基于**客观证据**（文件存在、grep 命中、行列号、统计数字）——不主观臆断；audit 每条 fail 必须关联 CWE 编号并引用文件/行号/代码片段。
5. **不写死凭据**：检查/扫描中不向被测文件写入任何内容；被测 skill 的凭据（`{PREFIX}_*`）只读不写；本技能自身不硬编码任何 AK/SK/密码/token。
6. **不跳过未知项**：在线规范新增/未知条目按其 `rule`/`input`/`detection` 描述自行设计检查方法执行——**绝不跳过**；网络不可用时才允许标记「未执行(原因)」。
7. **评论标记/命令纪律**：所有评论 body 必须以 `[QA Agent]` 开头，测试结果固定标记放 body 开头**独立行**（`[QA Agent] ✅ 测试通过` / `[QA Agent] ⚠️ 测试通过（有条件）` / `[QA Agent] 🔴 测试失败`——可检索，前置判断依赖）；平台命令一律以 `references/grape-cli-reference.md` 为准——不凭记忆/猜测拼写，curl 仅用于测试执行域（跑被测 skill 所需的网络探测——若其目标系统需验证时）不用于平台交互。

## 二、环境变量表

| 变量 | 来源 | 说明 |
|------|------|------|
| `GRAPE_SESSION_TOKEN` | 平台注入 | 会话令牌——**平台交互（评论/合并/A2A/任务收尾）使用**（CLI 自动读取，不手动传入）|
| `REPO_FULL_NAME` | 平台注入 | 仓库全名 `owner/repo`（评论/合并/交棒命令参数用——`OWNER=${REPO_FULL_NAME%/*}`、`REPO=${REPO_FULL_NAME#*/}`）|
| `ISSUE_NUMBER` | 平台注入 | Issue 编号（评论必填）|
| `PR_NUMBER` | 平台注入 | PR 编号（评论/合并/交棒必填——A2A payload 的 `pr_number` 提取）|
| `ACCOUNT` | 平台注入 | 触发者账号（actor——A2A payload 的 `account` 字段用）|
| `NODE`（可选）| 环境探测 | node 可执行文件路径——预检时探测；缺失时按三、前置检查门自愈（或 Agent 直接 curl 拉规范）|
| `{PREFIX}_*`（可选）| 被测 skill 上下文 | 被测 skill 的目标系统凭据——**只读**（若被测 skill 需要运行时验证且其文档声明该读取方式）|

## 三、前置检查门（自愈 → 重试 → STOP）

1. **node ≥ 12**（运行 `scripts/fetch_specs.js` 拉取三份规范）：`node --version` 检查——缺失 → 尝试安装 → 重试 → 仍失败则 **Agent 直接用 curl/网络工具拉取规范 JSON** 继续（不阻塞主流程）。
2. **目标 Skill 目录存在**：`<skill_path>` 是存在的目录——不存在/非目录 → 返回错误提示，**不执行任何检查**。
3. **可选依赖**：bash（目标脚本 `bash -n` 语法检查）/ python（目标脚本 `py_compile` 检查）——缺失则该类检查项标记 skip 并注明原因，不阻塞。
4. **grape CLI（流水线场景必须——评论/合并/交棒/收尾依赖）**：`grape --version` 检查 ≥0.3.2——缺失/过低 → 按 `references/grape-cli-installation.md` 自愈（`timeout 300 pip install git+https://gitcode.com/grape-dev/grape-cli.git`）→ 重试 → 仍不可用 STOP；本地 standalone 测试跳过此项。
5. **契约刷新（流水线场景）**：`grape refresh`——失败重试 ≤2 次后回退内置快照继续，执行中命令不识别时再 refresh。
6. **PR_NUMBER/被测 skill 就绪（流水线场景）**：`[ -n "$PR_NUMBER" ]` 有值 + worktree 内 `skills/{name}/` 目录存在（A2A payload 的 pr_number 已注入、creator 产物在 feat/issue-{N} 分支）——缺失 → 停止并说明（测试必须知道 PR 与被测对象）；本地 standalone 测试跳过此项（无 PR 门控流程）。
7. 任一前置门失败 → 输出「前置检查未通过：<原因>」并结束任务，不无限重试。

## 四、技能定义与执行

### 4.1 定位与职责边界

- **做什么**：对 Skill 目录做一站式质量验证，整合三个套件：
  | 套件 | 对应原工具 | 职责 | 规范来源（在线动态拉取）|
  |------|-----------|------|------------------------|
  | **Lint 规范检查** | skill-linter | 结构合规、安全基线、资源限制、提交隔离 | `common_skillslinter.json` |
  | **Test 功能测试** | skill-tester | 运行时功能验证：正确性/性能/兼容/可靠/编排 | `common_functions.json` |
  | **Audit 安全审计** | skill-auditor | 恶意代码、后门、注入、凭据窃取、CWE 漏洞 | `common_auditor.json` |
- **不做什么**：
  - 不修改/修复被测 Skill（检测出问题只报告，修复交还用户/creator——发现需修复 → A2A 交棒 coding，不自己修）。
  - 不生成 skill（那是 `common-skill-creator` 的职责）。
  - 不判定产物是否可上线发布（那是质量门禁消费方的职责——本技能只出报告与综合判定）。
  - 不发布/上架产物（那是 `skill-publish` 的职责——本技能交付到 **测试通过自动合并 + A2A 交棒 sre** 为止）。
  - 不创建 PR（那是 `common-skill-creator` 的职责——本技能只测 creator 交棒来的 PR）。
  - 合并 PR 必须走 `scripts/merge_skill_pr.sh`（自动合并），**不手敲 pulls merge**、不外呼其他合并途径。
- **默认三合一执行（mode=all）**，也可 `mode=lint|test|audit` 单套件——输出一份组合报告（综合判定 + 各套件子报告）。

### 4.2 场景路由

| 入口 | 路径 |
|------|------|
| 用户要检查/测试/审计一个 skill 目录（默认）| 4.3 全流程（mode=all）|
| 用户只要规范检查 | 4.3 流程（mode=lint——只执行 Lint 套件章节）|
| 用户只要功能测试 | 4.3 流程（mode=test）|
| 用户只要安全审计 | 4.3 流程（mode=audit）|
| 用户在平台任务上下文中收到「测试某 skill」交办 | 4.3 全流程（含 Step 7 报告 + Step 8 门控）+ 4.8 任务收尾 |
| 收到 A2A 交棒（creator → qa——测试创建的 skill PR）| 4.3 全流程：三合一测试（产物已在 worktree——前置门 6）→ Step 7 双评论 → Step 8 门控（✅/⚠️ 合并 + A2A sre；❌ A2A coding）|
| 收到 pull_request_merged 事件（合并完成通知）| 前置判断（见 4.3）：查 Issue 评论历史——有自己「测试通过」标记（✅ 或 ⚠️ 字样）→ 直接 A2A sre（跳测试）；无 → 不处理（非本技能场景）|
| 其他场景 | 不做处理——告知本技能边界并结束（不硬套流程）|

### 4.3 处理流程（三合一 → 流水线骨架）

```
Step 0 就绪（前置门+grape 自愈）→ Step 1 拉规范 → Step 2 收集 → Step 3 Lint
→ Step 4 Test → Step 5 Audit → Step 6 汇总判定
→ Step 7 双评论报告（流水线场景）→ Step 8 门控收尾（✅/⚠️ 合并+A2A sre；❌ A2A coding）
→ 4.8 任务收尾
```

**启动即读 `references/tester-runbook.md`**（⭐ 最先——Step 0-8 全程命令逐条照抄，不查 --help；pull_request_merged 前置判断 + 收尾 + 错误排查速查也在其中）。

执行规则：

1. **Step 0 必先行**：三、前置检查门（node/目标目录/可选依赖/grape CLI 自愈/契约刷新/PR_NUMBER 与被测目录就绪）+ 确认 mode（默认 all；非法值回退 all 并注明）。
2. **前置判断（仅 pull_request_merged 事件）**：先查 Issue 评论历史（`issues comments list`）——找到自己之前的「测试通过」标记（body 首行 `[QA Agent] ✅ 测试通过` 或 `⚠️ 测试通过（有条件）`）→ **直接 A2A sre**（跳测试，不重测）；找不到 → 不处理（非本技能负责的合并事件）。
3. **测试五阶段链式不可跳**：Step 1 拉三份在线规范（raw → API v5 回退；失败按 4.5 降级）→ Step 2 一次收集三套复用 → Step 3/4/5 按 mode 执行对应套件——每项客观证据、audit fail 必关联 CWE 编号、不跳过未知条目；**按方法映射补测（N 类反例/引用完整性/声明实现一致性——references 标注 `[补测]`）**；**可实测必实测、实测结果优先于静态推断**（references/test-methods.md「实测优先规则」）；Step 4 先做**被测产物预检**（bash -n/py_compile/入口冒烟——runbook Step 4）；**同一 PR 复测 = 全量重测、以最新轮为准**（runbook Step 6 复测规则）。
4. **Step 6 判定 + 结论标注必做**：按 4.6 判定表出 ✅/⚠️/❌——**三套评分明细表 + 判定原因 + 建议/必改项清单分别标注**，不得只写笼统判定。
5. **Step 7 双评论必做（流水线）**：Issue（完整报告）+ PR（简短结论），body 首行固定标记（硬约束⑦）；standalone 跳过（对话+文件输出即可）。
6. **Step 8 门控**：✅/⚠️ 通过族 → `scripts/merge_skill_pr.sh` → MERGED=1 → A2A sre；❌ → 不合并 → A2A coding（修复后重 push 复测）；MERGED=0 → 评论「请手动合并」不交棒；agent 用节点名 `sre`/`coding`（不传技能名）——**先交棒再 4.8 收尾**。
### 4.4 命令标准（本技能命令子集）

本技能的 grape 命令面**六条**——① 评论读取（`issues comments list`——前置判断）② Issue 评论（`issues comments create`——Step 7 报告）③ PR 评论（`pulls comments create`——Step 7 报告）④ 合并（走 `scripts/merge_skill_pr.sh`——内部封装 pulls merge/get + branches 查询，**不手敲**）⑤ `a2a handoff`（Step 8 交棒 sre/coding）⑥ 4.8 `tasks complete`（收尾）。命令面 = 本地脚本 + 平台门控协议：

| 命令 | 用途 | 前置 |
|------|------|------|
| `node common-skill-tester/scripts/fetch_specs.js [--type lint\|test\|audit] [-o <file>] [<url>]` | 拉取三份在线规范 | node ≥ 12 |
| `grape git-repo issues comments list --owner ... --repo ... --number "$ISSUE_NUMBER" --json-out` | 前置判断（pull_request_merged 事件查通过标记）| 平台任务上下文 |
| `grape git-repo issues comments create --owner ... --repo ... --number "$ISSUE_NUMBER" --json '{...}'` | Step 7 Issue 评论（body 首行固定标记）| 平台任务上下文 |
| `grape git-repo pulls comments create --owner ... --repo ... --number "$PR_NUMBER" --json '{...}'` | Step 7 PR 评论（固定标记）| 平台任务上下文 |
| `bash scripts/merge_skill_pr.sh "$PR_NUMBER"` | Step 8 自动合并（内部 pulls merge/get + branches——MERGED/BRANCH_DELETED 输出）| bash + grape CLI |
| `grape a2a handoff --json '{"agent":"sre\|coding","payload":{...}}'` | Step 8 交棒（sre 发布 / coding 修复——**必带 issue_number + pr_number**；agent 用节点名 sre/coding）| 平台任务上下文 |
| `grape tasks complete --json '{...}'` | 4.8 任务收尾（A2A 交棒之后调用）| 平台任务上下文 |

> ⚠️ **不要尝试的命令 / 禁止操作（防误用）**：
>
> | 命令 | 理由 |
> |------|------|
> | `grape git-repo pulls merge` 手敲 | **不直接手敲**——合并走 `scripts/merge_skill_pr.sh`（MERGED/BRANCH_DELETED 契约输出）；直接 merge 无法拿到分支删除状态 |
> | `grape git-repo pulls create` | 创建 PR 是 creator 的职责——本技能只测已存在的 PR |
> | `grape git-repo branches delete` | 不存在（仅 list；0.3.3 无分支删除接口——残留分支人工清理）|
> | `grape api` | 已移除（0.2.2 起），且平台禁止直连 API |
> | 检查中的写操作（修改被测文件/`sed -i`/覆盖 SKILL.md）| 本技能只读——检测不改动；修复交 coding |

### 4.5 故障决策表

| 场景 | 处理 |
|------|------|
| skill_path 不存在或非目录 | 返回错误提示，不执行任何检查 |
| 某份规范拉取失败（网络不可用）| 该套件标记「未执行(原因)」，其余照常；提示用户可指定本地 JSON 文件（`-o` 保存的或手动指定路径作为 URL 参数）|
| 用户只要某一种检查 | mode 指定 lint/test/audit，只执行对应套件章节 |
| mode 传非法值 | 回退 all，并在报告中注明 |
| 目标 Skill 无脚本/可执行代码 | audit 中代码相关规则 skip；lint 的脚本检查项 skip 或 pass（注明原因）|
| 目标 Skill 无写操作 | test 中读写语义、幂等、资源清理等用例 skip（注明原因）|
| 在线规范新增未知条目 | 按 `rule`/`input`/`detection` 描述自行设计检查/测试/检测方法，不跳过 |
| 报告过长 | 按模板章节结构输出，详细记录表保留全部条目 |
| 三套评分均 ≥ 95 但存在 warn | 判定 ⚠️ 有条件通过，报告列 warn 项及修正建议 |
| CRITICAL/P0 fail 多处出现 | 判定 ❌ 不通过，全部列出；修复后建议重跑全量 |
| 断点续跑 | 已拉取规范用 `-o` 保存；中断后重新指定该文件作为对应 URL 参数（本地文件路径直接读取）恢复执行 |
| node 不可用 | Agent 直接用 curl/网络工具拉取规范 JSON 后继续（不阻塞主流程）|

### 4.6 输出格式（固定产物）

**三套评分公式与门禁**：

| 套件 | 公式 | 一票否决项 |
|------|------|-----------|
| Lint | `总分 = 100 - (Critical×15 + High×8 + Medium×4 + Low×2 + warn×1)` | Critical fail |
| Test | `通过率 = pass / (总用例 - skip) × 100%` | P0 必测项 fail |
| Audit | `安全分 = 100 - (CRITICAL×20 + WARNING×8 + INFO×2 + warn×1)` | CRITICAL 严重发现 |

**星级标准**（三套统一 + 综合分）：90-100 ⭐⭐⭐⭐⭐ / 75-89 ⭐⭐⭐⭐ / 60-74 ⭐⭐⭐ / 40-59 ⭐⭐ / 0-39 ⭐

**综合判定**：

| 判定 | 条件 |
|------|------|
| ✅ 通过 | 三套评分均 ≥ 95，且无 lint Critical fail、无 test P0 fail、无 audit CRITICAL 发现 |
| ⚠️ 有条件通过 | 无上述三类 fail，但存在 warn 或任一评分在 75-94（条件项——建议随报告转交下游，不阻断合并）|
| ❌ 不通过 | 存在任一上述 fail 类型，或任一评分 < 75 |

**报告结构**（`references/report-template.md`）：综合概览 → 综合判定与评分 → Lint 结果 → Test 结果 → Audit 结果 → **覆盖矩阵（脚本 × 三套检查）**；报告为纯 Markdown，含 `{OVERALL_SCORE}`/`{LINT_SCORE}`/`{TEST_PASS_RATE}`/`{AUDIT_SCORE}`/`{VERDICT}` 等占位符数值供 CI/CD 门禁解析。报告使用中文输出。

**结论标注要求（❌ 必须逐项标注清楚）**：测试结论必须把**各套检查结果分别标注**，不允许只写一个综合判定——Issue 评论与报告文件都要有：
1. **三套评分明细表**（每套一行）：套件名 / 评分或通过率 / 星级 / 状态（无问题 / N 项 warn / N 项 fail）+ 单项证据（条目号、文件、行号）
2. **综合判定行**：`✅ 通过` / `⚠️ 有条件通过` / `❌ 不通过` + **判成这个档的原因**（如「Audit 安全分 88 未达 95」或「Lint 1 项 warn」）
3. **建议项/必改项清单**：warn → 建议项（分开列）；fail → 必改项（附修复建议）——⚠️ 有条件通过时此清单即「条件」——随报告转交下游（sre 发布参考 / coding 修复）

**文件输出**：`{被测 Skill 父目录}/{skill-name}-tester-report-{YYYYMMDD-HHmmss}.md`（从 skill_path 提取目录名；例：`skill_path=/repos/skills/dev/my-skill` → `/repos/skills/dev/my-skill-tester-report-20260916-120000.md`）

### 4.7 写操作协议

- **本地写操作**：报告文件写入被测 Skill **父目录**（文件名含 skill 名 + 时间戳——重复执行覆盖写入，幂等无副作用）；报告保存后在对话中提示报告路径。
- **绝不修改被测 Skill 目录内任何文件**（检测只读）；不创建临时文件（拉取规范用 `-o` 保存的文件由用户决定清理）。
- **平台写操作（流水线场景——流程内声明，直接执行不需用户确认）**：
  | 动作 | 要求 |
  |------|------|
  | Issue + PR 双评论（测试报告）| **必做**（Step 7——body 首行固定标记）|
  | 合并 PR | 测试通过族（✅/⚠️）自动执行——**必须走 `scripts/merge_skill_pr.sh`**，不手敲 pulls merge |
  | A2A 交棒（sre / coding）| 流程内（Step 8——必带 issue_number + pr_number）|
  | 删除评论 / 其他写操作 | 流程外——用户确认后执行 |

### 4.8 任务收尾（必做）

业务动作全部完成后：

1. **流水线场景：先 A2A 交棒（Step 8 完成——sre 或 coding），再调 `grape tasks complete` 关闭当前任务**（任务中心闭环——先交棒再关任务）；standalone 场景直接收尾：
```bash
grape tasks complete --json '{"result_text": "三合一测试完成：<skill 名称> 综合判定 <✅/⚠️/❌>", "actions": ["拉取规范/lint/test/audit/汇总报告", "comments:issue+pr", "merged:PR#<n>（通过族）", "a2a:sre（通过族）/ a2a:coding（失败）"], "conclusion": "<综合判定与关键发现>", "artifacts": ["<报告文件路径>", "PR 评论/合并状态"], "next_step": "<按判定给用户的下一步（修复后重测/已合并待发布) 建议>"}'
```
2. **再在最终答复末尾输出结构化 JSON 块**（平台读取）：
```json
{
  "actions": ["拉取规范/lint/test/audit/汇总报告", "comments:issue+pr", "merged:PR#<n>（通过族）", "a2a:sre（通过族）/ a2a:coding（失败）"],
  "conclusion": "综合判定与关键发现",
  "artifacts": ["<报告文件路径>", "PR 评论/合并状态"],
  "next_step": "按判定给用户的下一步建议",
  "issue_summary": "本次任务最新聚合结论（紧凑文本，用 | 分隔）"
}
```

## 五、参考文件清单

| 文件 | 职责 | 何时加载 |
|------|------|---------|
| `references/spec-sources.md` | 三份规范的在线地址、合并输出结构、各自预期 JSON 结构、最新规范概要 | Step 1 拉取后核对结构时 |
| `references/lint-methods.md` | Lint 检查方法映射表 + 判定逻辑 + 评分公式 | Step 3 |
| `references/test-methods.md` | 功能测试方法映射表 + 判定逻辑 + 通过率公式 + 证据要求 | Step 4 |
| `references/audit-methods.md` | 安全审计方法映射表 + 判定逻辑 + 安全评分 + CWE 映射要求 | Step 5 |
| `references/report-template.md` | 组合报告模板（综合判定 + 三套子报告）| Step 6 |
| `scripts/fetch_specs.js` | 三份规范动态拉取器（raw → API v5 回退；--type/-o/--help）| Step 1 |
| `references/tester-runbook.md` | **执行走查表：Step 0-8 全程命令逐步照抄 + 前置判断 + 错误排查速查** | ⭐ **最先**——启动即读，全流程照抄不查 --help |
| `references/grape-cli-installation.md` | grape CLI 安装/升级/验证（前置门 4 自愈）| 前置检查发现缺失/版本过低时 |
| `references/grape-cli-reference.md` | grape CLI 命令契约（子集 + ❌ 警示 + token scope）| Step 7/8 平台交互前 |
| `scripts/merge_skill_pr.sh` | 合并脚本（pulls merge/get + branches——MERGED/BRANCH_DELETED 输出）| Step 8 |

## 六、Best Practices

- **动态规范为本**：每次执行都必须从在线地址拉取最新规范——不使用缓存或硬编码条目；三份 JSON 结构/字段/条目变化时以实际内容为准
- **客观证据**：每项 pass/fail 基于文件内容/grep 命中/行列号/统计数字——不主观臆断
- **证据与 CWE**：test 每条结果附证据；audit 每条 fail 必须关联 CWE 编号
- **门禁语义**：lint Critical / test P0 / audit CRITICAL 一票否决；三套评分全 ≥ 95 才判定通过
- **不跳过未知项**：在线新增条目按其字段描述执行
- **只读纪律**：检测不改动被测文件——发现问题列清单与修复建议，修复交还用户
- **复用收集**：Step 2 一次收集信息三套复用，避免重复读目标文件；规范 JSON 解析后仅保留必要字段（丢弃 summary/legend 冗余）控制上下文
- **门控映射**：✅/⚠️ 通过族 → 合并 + A2A sre（⚠️ 有条件通过 = 建议项随报告转交下游，不阻断合并）；❌ 失败 → 不合并 + A2A coding——**判定以技能自身 ⚠️ 判定为准，门控不做二次标准**
- **固定标记可检索**：✅/⚠️/🔴 标记放评论 body 首行独立行——pull_request_merged 前置判断靠「测试通过」字样（✅/⚠️）识别
- **只读不乱动**：发现问题 A2A 回 coding——本技能绝不自己修、不手敲合并