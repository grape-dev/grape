# 通用 Skill 三合一测试执行走查表（common-skill-tester runbook）

> 目的：把 common-skill-tester 每一步**要做什么 + 用哪条确切命令**固化下来，Agent **照着执行、不要查 `--help` / `grape commands`**。
> **CLI 纪律**：平台交互（评论/合并/分支查询/A2A/任务）一律走 grape CLI——**禁止 curl 直连平台 API**；本技能测试执行域（三合一检查）不涉及目标系统直连，如被测 skill 声明需要运行时验证（凭据 `{PREFIX}_*` 注入）则按其文档执行，与平台交互严格区分。
> **只读纪律**：本技能是 QA 门控——**只读验证，绝不修改 skill 内容**；发现需修复 → A2A 回 Coding Agent，不自己修。
> **评论前缀硬规**：所有评论 body 必须以 `[QA Agent]` 开头；测试结果固定标记放 body 开头独立行（✅ 测试通过 / ⚠️ 测试通过（有条件）/ 🔴 测试失败——保证可检索性，前置判断依赖）。
> 环境变量直接使用：`$REPO_FULL_NAME`、`$ISSUE_NUMBER`、`$PR_NUMBER`、`$ACCOUNT`（`$GRAPE_SESSION_TOKEN` 由 grape CLI 自动读取，不需手动使用）。

---

## Step 0 · 任务上下文与就绪（必须最先，任何事件都做）

```bash
# ⚠️ CLI 前置自愈（流水线场景最先）：`grape --version` ≥0.3.2；未装/版本过低 → 按 references/grape-cli-installation.md
#    自动安装/升级（timeout 300 pip install git+https://gitcode.com/grape-dev/grape-cli.git）→ 装好再验证一次
#    → 仍不可用 → 输出「前置检查未通过：<原因>」并结束任务（本地 standalone 不用 grape，跳过）
grape --version
grape refresh 2>&1 && echo REFRESH_OK        # 契约保鲜；失败重试 ≤2 次后回退内置快照继续
node --version                                # ≥12（拉规范用；缺失 → 尝试安装 → 仍失败 Agent 直接 curl 拉规范继续）
bash --version 2>/dev/null | head -1 || echo "NO_BASH"    # 可选依赖①——缺失时 Step 3/5 语法类检查 skip（不阻塞）
python --version 2>&1 | head -1 || echo "NO_PYTHON"       # 可选依赖②——缺失时 py_compile 类检查 skip（不阻塞）
test -d "$skill_path" && echo SKILL_DIR_OK || { echo "skill_path 不存在/非目录——结束任务"; exit 1; }
[ -n "$PR_NUMBER" ] && echo PR_OK || echo "PR_NUMBER 缺失——流水线场景停下说明（测试必须知道 PR）；standalone 忽略"
grape tasks current --json-out               # A2A payload 在这里（skill 名称/PR 编号/context——creator 交棒信息）
```

确认 mode（`all` 默认；非法值回退 all 并在报告注明）。

## 前置判断（仅 pull_request_merged 事件时执行）

```bash
# 读 Issue 评论历史，找自己之前的「测试通过」标记（body 开头独立行含 [QA Agent] ✅ 测试通过 或 ⚠️ 测试通过（有条件））：
grape git-repo issues comments list --owner "${REPO_FULL_NAME%/*}" --repo "${REPO_FULL_NAME#*/}" \
  --number "$ISSUE_NUMBER" --json-out | head -120
# 找到 → PR 之前已测试通过 → 直接 A2A sre Agent（跳 Step 1-8——见「前置判断命中」节）
# 找不到 → 不处理（非本技能负责的合并事件——回复说明并结束）
```

## Step 1 · 拉取三份规范

```bash
node common-skill-tester/scripts/fetch_specs.js                              # 全部三份，输出合并 JSON
node common-skill-tester/scripts/fetch_specs.js --type lint                  # 只拉 lint 检查点
node common-skill-tester/scripts/fetch_specs.js --type test                  # 只拉功能测试点
node common-skill-tester/scripts/fetch_specs.js --type audit                 # 只拉安全审计规则
node common-skill-tester/scripts/fetch_specs.js "https://...json" --type lint  # 自定义 URL
node common-skill-tester/scripts/fetch_specs.js -o /tmp/specs.json           # 保存到文件（断点续跑/复查）
```

- 拉取策略：raw URL 失败 → 自动回退 gitcode API v5（base64 解码）→ 仍失败该套件标记「未执行(原因)」其余照常
- **核心原则：以拉取到的实际 JSON 内容为准，不硬编码任何检查项/测试项/规则**——在线新增条目自动纳入
- 拉取后按 `references/spec-sources.md` 核对结构

## Step 2 · 收集目标 Skill 信息（一次收集，三套复用）

| # | 收集项 | 方法 |
|---|--------|------|
| 1 | 目录结构（文件/子目录/大小/扩展名——重点标记 .sh/.py/.js/.ts/.go/.rs）| Glob 列文件 |
| 2 | SKILL.md 全文 + Frontmatter（name/description/version）+ Workflow/inputSchema/outputSchema/读写声明 | Read |
| 3 | 脚本文件内容（所有 .sh/.py/.js/.ts——安全审计素材）| Read |
| 4 | 配置文件（.env 存在性；package.json/requirements.txt/pyproject.toml 依赖声明）| Glob + Read |
| 5 | 危险模式预扫描（一次 grep 三套复用）：凭据（AK/SK/密码/token）、内网 IP/域名、危险命令（sudo/rm -rf）、敏感路径（~/.ssh、~/.aws）、网络请求 URL、动态执行调用 | Grep |
| 6 | 文件统计（总大小/文件数/SKILL.md 行数/类型列表）| Bash 统计 |
| 7 | 跨引用与集成声明（引用其他具名 Skill/多 Skill 编排/数据传递）| Read SKILL.md 找声明 |

## Step 3 · Lint 规范检查（mode 含 lint）

1. 对**每一个**检查项（`groups[].items[]`）按 `references/lint-methods.md` 方法映射执行（含 `[补测]` 引用完整性——SKILL.md 正文/参考清单引用的文件存在性对账）
2. 判定 pass/fail/warn/skip——**客观证据**（文件存在/grep 命中/行列号/统计数字）
3. Lint 总分：`100 - (Critical×15 + High×8 + Medium×4 + Low×2 + warn×1)`
4. Critical fail = 必改项（附具体修改建议）；非 Critical fail/warn = 建议修改项
5. 输出「三、Lint 检查结果」章节

## Step 4 · 功能测试（mode 含 test）

0. **被测产物预检（先于全量用例——必做）**：
   ```bash
   find "$skill_path" -name "*.sh" -exec bash -n {} +
   find "$skill_path" -name "*.py" -exec python -m py_compile {} +
   # 入口探测（可执行入口识别——为行为实测做准备）：scripts/*.sh --help/--version 冒烟；失败不阻塞
   ```
   语法失败 → 记 fail（证据：文件 + 报错行）；**不跳过后续用例**（继续跑——确认其余部分是否也坏）
1. 对每一条测试用例（`test_groups[].items[]`）按 `references/test-methods.md` 执行（触发词/工作流/Schema/读写语义/性能/兼容/可靠/编排 + `[补测]` N 类反例/声明实现一致性）
2. 静态分析为基础；**有可执行入口必须行为实测**（冒烟 + 关键用例）——实测结果优先于静态推断（references/test-methods.md「实测优先规则」）；不可运行注明「未实测(原因)」
3. 判定 pass/fail/warn/skip + 证据（引用章节/行号/内容）
4. 通过率：`pass / (总用例数 - skip) × 100%`；P0 必测项任一 fail 标记「建议打回」
5. 输出「四、功能测试结果」章节

## Step 5 · 安全审计（mode 含 audit）

1. 对每一条检测规则（`groups[].items[]` + CWE 模式）按 `references/audit-methods.md` 执行（网络请求/敏感文件/危险命令/注入/后门/混淆/依赖安全）
2. 判定 pass/fail/warn/skip；**fail 必须关联 CWE 编号**并引用文件/行号/代码片段
3. 安全分：`100 - (CRITICAL×20 + WARNING×8 + INFO×2 + warn×1)`；CRITICAL fail 一票否决
4. CWE 覆盖率：`已检测 CWE 数 / 总 CWE 数 × 100%`
5. 输出「五、安全审计结果」章节

## Step 6 · 汇总报告 + 综合判定

按 `references/report-template.md` 生成组合报告：

```bash
综合分 = round((Lint总分 + Test通过率 + Audit安全分) / 3)
```

| 判定 | 条件 |
|------|------|
| ✅ 通过 | 三套评分均 ≥ 95，且无 lint Critical fail、无 test P0 fail、无 audit CRITICAL 发现 |
| ⚠️ 有条件通过 | 无上述三类 fail，但存在 warn 或任一评分在 75-94（条件项——建议随报告转交下游，不阻断合并）|
| ❌ 不通过 | 存在任一上述 fail 类型，或任一评分 < 75 |

**结论标注（必做）**——三套检查结果分别标注，不写笼统判定：
- **三套评分明细表**：套件 / 评分或通过率 / 星级 / 状态（无问题 / N 项 warn / N 项 fail）/ 单项证据（条目号 + 文件 + 行号）
- **综合判定行**：✅/⚠️/❌ + **判成该档的原因**（如「Audit 安全分 88 未达 95」「Lint 1 项 warn」）
- **建议项清单**（warn——⚠️ 时即「条件」，随报告转交下游）/ **必改项清单**（fail——附修复建议）

输出两处：
- **对话输出**：Markdown 报告直接给用户
- **文件输出**：`{被测 Skill 父目录}/{skill-name}-tester-report-{YYYYMMDD-HHmmss}.md`（`{skill-name}` 从 skill_path 目录名提取；例 `skill_path=/repos/skills/dev/my-skill` → `/repos/skills/dev/my-skill-tester-report-20260916-120000.md`）

**覆盖矩阵（必做）**：报告含「六、覆盖矩阵」节——被测 skill 每个脚本/入口 × 三套检查覆盖状态（每脚本至少被 Audit 覆盖；无任何覆盖 → 记 warn 并在结论标注）——确保无死角。

**复测规则（同一 PR 第二轮及以后——必做）**：
- 报告头部标注：`第 N 轮复测——初测判定：{❌/⚠️} / 修复项：{清单} / 本轮为最终判定依据`
- **全量重测**（完整三套 + 覆盖矩阵，不只测修复点——检测修复引入的退化）
- **评分与判定一律以最新轮为准**；初测评论保留于评论区（证据链），复测通过后新发 ✅ 评论（三分标记契约不受影响——前置判断只查最新「✅ 测试通过」标记）

判定 ❌ → 对话明确列未达标项（fail 清单 + 未达 95 的套件 + 原因）+ 修复建议。报告使用中文输出。

流水线场景 → Step 7 双评论；standalone → 对话 + 文件输出即可（无平台交互）。

## Step 7 · 双评论报告（流水线场景必做——不可跳过；standalone 跳过）

```bash
# ⚠️ 必须发 Issue 评论 + PR 评论（body 首行固定标记；长 body 先写文件再 --json @file 防 shell 转义）：
# Issue 评论（完整报告）：正文先写 /tmp/tester-issue-body.md（纯 markdown 免转义），再转 JSON @file（jq 自动转义；禁 printf 拼 JSON）：
cat > /tmp/tester-issue-body.md <<'EOF'
[QA Agent] ✅ 测试通过

## Skill 测试报告：{name} v{version}

## 三套评分明细
| 套件 | 评分/通过率 | 星级 | 状态 | 证据 |
|------|------------|------|------|------|
| Lint | 96 | ⭐⭐⭐⭐⭐ | 1 项 warn | GS-003:a.md L12 |
| Test | 98% | ⭐⭐⭐⭐⭐ | 无问题 | TC-02 通过 |
| Audit | 100 | ⭐⭐⭐⭐⭐ | 无问题 | — |

## 综合判定
✅ 测试通过（无 fail、无 warn、三套均 ≥95）

## 建议项
- （warn 时列；无则写「无」）
EOF
jq -Rs '{body: .}' < /tmp/tester-issue-body.md > /tmp/tester-issue-comment.json
grape git-repo issues comments create --owner "${REPO_FULL_NAME%/*}" --repo "${REPO_FULL_NAME#*/}" \
  --number "$ISSUE_NUMBER" --json @/tmp/tester-issue-comment.json --json-out
# PR 评论（简短结论）：
grape git-repo pulls comments create --owner "${REPO_FULL_NAME%/*}" --repo "${REPO_FULL_NAME#*/}" \
  --number "$PR_NUMBER" --json '{"body": "[QA Agent] ✅ 测试通过"}' --json-out
```

- 三态标记（body 首行独立行）：`[QA Agent] ✅ 测试通过`（✅）/ `[QA Agent] ⚠️ 测试通过（有条件）`（⚠️——合并照走，条件 = 建议项随报告转交下游）/ `[QA Agent] 🔴 测试失败`（❌——body 列 fail 清单 + 修复建议）
- 两条评论都要发（Issue 报告 + PR 结论）；评论失败 → 重试 1 次 → 仍失败记录并说明（不阻塞后续门控）

## Step 8 · 门控收尾（流水线场景必做——通过族合并 + 交棒 / 失败交棒）

> ⚠️ **发现 fail 时的动作链（❌ 路径——显式顺序，不可颠倒）**：
> 1. **记录证据**：复现命令 + 实际输出 + 文件/行号（写进报告 fail 条目）
> 2. **不修改/不修复被测 Skill**（硬约束①——修复是 coding 职责；tester 只测不修）
> 3. 判定 ❌ → Step 7 发「🔴 测试失败」评论（列失败原因 + 复现步骤）
> 4. A2A coding：context = `测试失败：{原因} {复现步骤}。请修复后重新 push 复测`（见下 ❌ 分支）
> 5. tasks complete 结束任务——等待 `pull_request_updated` 事件触发复测（复测按 Step 6「复测规则」全量执行）

```bash
# ✅ / ⚠️（通过族）→ 自动合并：
bash scripts/merge_skill_pr.sh "$PR_NUMBER"
# 脚本输出：MERGED=1/0 + BRANCH_DELETED=1/0 + MESSAGE
# - MERGED=1 且 BRANCH_DELETED=1 → A2A sre（发布）
# - MERGED=1 且 BRANCH_DELETED=0 → Issue 评论告警「分支未删除，请手动清理」→ 仍 A2A sre
# - MERGED=0 → 评论 PR + Issue：「✅ 测试通过，自动合并失败，请手动合并」→ 不 A2A sre
```

```bash
# A2A sre（通过族——合并成功后）：
grape a2a handoff --json '{
  "agent": "sre",
  "payload": {
    "repository": "'"$REPO_FULL_NAME"'",
    "account": "'"$ACCOUNT"'",
    "issue_number": '"$ISSUE_NUMBER"',
    "pr_number": '"$PR_NUMBER"',
    "context": "测试通过（判定：<✅ 通过 / ⚠️ 有条件通过——建议项见报告>），已自动合并。Skill：<name>，请发布"
  }
}'

# ❌（失败）→ 不合并 → A2A coding（修复）：
grape a2a handoff --json '{
  "agent": "coding",
  "payload": {
    "repository": "'"$REPO_FULL_NAME"'",
    "account": "'"$ACCOUNT"'",
    "issue_number": '"$ISSUE_NUMBER"',
    "pr_number": '"$PR_NUMBER"',
    "context": "测试失败：{失败原因摘要}。请修复后重新 push 复测"
  }
}'
```

- A2A 失败 → 重试 ≤3 次 → 仍失败：评论说明「A2A 服务不可达」+ next_step 人工介入
- 交棒成功后 → 平台任务收尾（complete 在 A2A 之后——先交棒再关任务）

## 前置判断命中「已测试通过」→ 直接 A2A sre Agent

```bash
grape a2a handoff --json '{
  "agent": "sre",
  "payload": {
    "repository": "'"$REPO_FULL_NAME"'",
    "account": "'"$ACCOUNT"'",
    "issue_number": '"$ISSUE_NUMBER"',
    "pr_number": '"$PR_NUMBER"',
    "context": "pull_request_merged——此前已测试通过（有 ✅/⚠️ 标记），请发布 Skill：<name>"
  }
}'
```

## 平台任务收尾（必做——任何分支都必须执行到最后）

```bash
# ⚠️ 流水线场景先完成 A2A 交棒（Step 8），再 tasks complete；standalone 无 Step 7/8 直接收尾
grape tasks complete --json '{
  "result_text": "三合一测试完成：<skill 名称> 综合判定 <✅/⚠️/❌>",
  "actions": ["拉取规范/lint/test/audit/汇总报告", "comments:issue+pr", "merged:PR#<n>（通过族）", "a2a:sre（通过族）/ a2a:coding（失败）"],
  "conclusion": "<综合判定与关键发现>",
  "artifacts": ["<报告文件路径>", "PR 评论/合并状态"],
  "next_step": "<按判定给用户的下一步建议>"
}'
```

最终答复末尾输出结构化 JSON 块（actions/conclusion/artifacts/issue_summary must 真实填充——平台读取，只输出一个）：

```json
{
  "actions": ["拉取规范/lint/test/audit/汇总报告", "comments:issue+pr", "merged:PR#<n>（通过族）", "a2a:sre（通过族）/ a2a:coding（失败）"],
  "conclusion": "综合判定与关键发现",
  "artifacts": ["<报告文件路径>", "PR 评论/合并状态"],
  "next_step": "按判定给用户的下一步建议",
  "issue_summary": "本次任务最新聚合结论（紧凑文本，用 | 分隔）"
}
```

> ⚠️ **结束信号**：complete 后立即结束任务，不要继续思考或等待输入。

---

## 错误排查速查（命令返回异常时）

> ⚠️ 出错时**先看完整错误**；runbook 命令照抄仍失败，才允许查 `grape commands` / API 文档。**不反复盲试同一命令**。

| 现象 | 处理 |
|------|------|
| `node: command not found` | Agent 直接用 curl/网络工具拉规范 JSON 继续（不阻塞）|
| skill_path 不存在/非目录 | 返回错误提示，不执行任何检查 |
| 某份规范拉取失败（网络断）| 该套件标记「未执行(原因)」其余照常；提示可指定本地 JSON（`-o` 保存的或手动路径当 URL 参数）|
| mode 非法值 | 回退 all，报告中注明 |
| 目标无脚本 | audit 代码相关规则 skip；lint 脚本检查 skip/pass（注明原因）|
| 目标无写操作 | test 读写语义/幂等/资源清理用例 skip（注明原因）|
| 在线规范新增未知条目 | 按 `rule`/`input`/`detection` 描述自行设计方法——不跳过 |
| 断点续跑 | `-o` 保存的规范文件重新指定为 URL 参数（本地路径直接读取）|
| 合并脚本 MERGED=0 | 评论「✅ 测试通过，自动合并失败，请手动合并」→ **不 A2A sre** |
| 合并后 BRANCH_DELETED=0 | Issue 评论告警「分支未删除（grape CLI 无分支删除接口），请手动清理」→ 不影响 A2A |
| A2A 交棒失败（非 2xx/超时）| 重试 ≤3 次 → 仍失败：评论说明「A2A 服务不可达，待恢复后重试」+ next_step 人工介入 |
| `issues/pulls comments create` 失败 | 看完整 stderr → 核对参数/权限重试 1 次 → 仍失败：记录并评论说明（不阻塞门控）|
| 命令 403/无权限 | **停止操作**——回复「该操作需要管理员/维护者权限，请在仓库设置中配置后重试」——不换 token 不绕行 |
| 401/凭据失效 | 说明凭据已失效（tk_ 短命属正常）——不替换不绕行 |
| 报「契约过期/命令不识别」| 先 `grape refresh` 再重试（Step 0）|
| 报 `base_url unreachable`（localhost:8000 拒连）| 本机默认指本地服务端——任务环境由服务端注入 base-url/GRAPE_CLI_MODE=WORKER 自动可用；**不要自行改 base-url**——确认平台任务上下文正常，异常则按 403 纪律回复 |
| 报 `token scope 'session_only'` 不可用 | 命令需要会话 token（GRAPE_SESSION_TOKEN）——流水线任务环境由服务端注入自动可用 |
| 前置判断（merged 事件）找不到通过标记 | 不处理——回复说明非本技能负责的合并事件并结束 |
| `grape tasks complete` 失败 | 非平台任务上下文（standalone）→ 跳过收尾，照常交付报告 |
| 报告过长 | 按模板章节结构输出，详细记录表保留全部条目 |
