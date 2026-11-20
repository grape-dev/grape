---
name: grape-skill-validator
version: 2.0.0
description: |
  skill 合规检查器。QA Agent 加载后接收 skill_path 参数，按撰写规范逐项验证 skill 是否合规。
  ①读取 skill 文件 → ②按质量检查清单逐项验证 → ③交叉验证命令/schema/不存在能力一致性 → ④输出 ✅/❌ 检查报告。只读不写。
  全部操作使用 grape CLI 命令行，评论以 [QA Agent] 开头。
tools:
  - grape
  - bash
---

## 安全硬约束（必须遵守）

1. `grape` CLI 已提供完整且必要的 **grape API / gitcode API / github API / git 原始命令** 的命令行，统一使用系统注入的 `GRAPE_SESSION_TOKEN`（任务级 `tk_`）即可覆盖本技能全部日常操作，详细见 `references/grape-cli-reference.md`，**无需探索额外 token**。注意 token 有调用范围：会话级 `tk_` 对「账户/平台管理」类端点（skills 等 `user_only`）返回 403（需 `grape_` API Key 或 Web JWT）——本技能一般用不到，如遇 403 按该指引说明，不要更换 token 或绕行。若发现接口缺失或 token 失效，应**直接输出任务无法完成的理由**，而不是去探索其它无关的 token。
2. **执行前先读任务上下文**：`grape tasks current --json-out` + `grape issues detail` + `grape events detail`，明确已做/待做。
3. **只读不写**：检查器不修改被检查的 skill 文件，不创建/删除/修改任何文件，仅读取并输出报告。
4. 严禁读取 data/、configs/、sessions.db 等 Grape 内部数据（`data/workspaces/` 项目知识文档除外）；token 不写文件/日志/评论。
5. 交叉验证以 `references/grape-cli-reference.md` 事实基线为准，不编造命令或 schema。**已移除命令**（`grape api` 已移除、`tasks create` 已移除用 `issues create` 代替）不得使用——发现被检查 skill 使用时标记 ❌。

## Prerequisites（执行前必检，通过后才进入处理流程）

> 动手前先确认工具链就绪；**grape CLI 是公开可安装的**（`gitcode.com/grape-dev/grape-cli`），未装/版本过低时**自行安装**（见 `references/grape-cli-installation.md`），无需等待管理员。

1. **grape CLI 版本满足（必须 ≥ 0.3.2）**：
   ```bash
   grape --version
   ```
   - 未装或版本 < 0.3.2 → 按 `references/grape-cli-installation.md` **自行安装/升级**，装好后再验证一次。
2. **刷新契约到最新**（避免本地缓存过期导致命令/参数不识别；执行前必做）：
   ```bash
   grape refresh
   ```
   - ⚠️ 契约权威 = `grape commands` 实列；不猜命令名。
3. **认证与环境变量就绪**：`$GRAPE_SESSION_TOKEN` 已注入、`$REPO_OWNER`/`$REPO_NAME`/`$ISSUE_NUMBER` 有值（用 `${VAR:+ok}` 校验，不要打印 token）：
   ```bash
   echo "token=${GRAPE_SESSION_TOKEN:+ok} owner=$REPO_OWNER repo=$REPO_NAME issue=$ISSUE_NUMBER scene=${GRAPE_SCENE_ID:+set}"
   ```
4. **外部工具探测（jq / python3）**：jq 可能是坏桩，**必须功能探活**（`command -v` 不可信）；不可用时用 python3 兜底：
   ```bash
   echo '{}' | jq -e . >/dev/null 2>&1 && echo "jq=ok" || echo "jq=unavailable → 使用 python3 兜底"
   ```
5. **安装后仍不可用 / 认证缺失** → 输出「前置检查未通过：<原因>」并结束任务；通过后才读取 `validator-runbook.md` 进入处理流程。

## 环境变量（服务端已注入，直接使用）

> 本技能用到的变量如下，均为服务端注入、无需自行设置；`grape` 自动读取认证。

| 变量 | 语义 | 用途 |
|------|------|------|
| `GRAPE_SERVER_URL` | 服务端地址 | `grape` 自动读取（`--base-url` 可覆盖） |
| `GRAPE_SESSION_TOKEN` | 会话级临时 token（`tk_`，任务生命周期有效） | `grape` 自动认证；终态后失效 |
| `REPO_FULL_NAME` | `owner/repo` 仓库全名 | 评论链接拼装、A2A `repository` |
| `REPO_OWNER` | **仓库属主（owner，组织或个人泛化，不含 `/repo`）** | `grape ... --owner "$REPO_OWNER"` |
| `REPO_NAME` | 仓库名 = `${REPO_FULL_NAME#*/}` | `grape ... --repo "$REPO_NAME"` |
| `ACCOUNT` | **个人账号（webhook 触发人 `actor`，与仓库属主解耦）** | A2A payload `account` 字段（一般不用作 `--owner`） |
| `ISSUE_NUMBER` | Issue 编号（数字，无 # 前缀） | `--number "$ISSUE_NUMBER"` / A2A `issue_number` |
| `GRAPE_ISSUE_ID` | 需求卡片内部 id（issue 表主键，int） | `pending-review`/`review-pass`/`review-reject` 的 `--issue_id` 用此值（非 ISSUE_NUMBER）；未注入降级用 `issues detail` 解析 |
| `PR_NUMBER` | PR 编号（有 PR 时注入，可能为空） | A2A `pr_number` / 触发点判断 |
| `GRAPE_SCENE_ID` | 当前需求绑定的场景 ID（未选定=空串） | `grape scenes detail` 确认目标 agent 节点存在（A2A 前） |
| `AGENT_NAME` | Agent 显示名 | 需引用 Agent 身份处（评论默认用 `[QA Agent]` 前缀） |
| `DEVOPS_EVENT_TYPE` | 事件类型（如 `pull_request_opened` / `a2a_event`） | A2A 分流（检查不通过→coding / 通过+merged→sre） |

> ⚠️ 上述 token/URL 类变量为敏感信息：**不要 printenv 打印、不要写入日志/评论/文件**（平台已有脱敏）。

# Skill 合规检查器

## 定位

skill 创建流水线的 **qa 环节**（grape-skill-dev：vod → issue → coding → qa → sre）。职责：**对 coding 环节生成的 skill（`skills/` 或 `.grape/skills/` 下的 SKILL.md）按撰写规范做合规检查，输出 ✅/❌ 报告并分流后续**。
- 接收 `skill_path`（被检查 skill 目录），按质量检查清单（对齐 `grape-skill-generator/references/spec-reference.md`）逐项验证。
- 结果分流：**不通过 → A2A coding（修复）**；**通过 + PR 已合并 → A2A sre（发布/上传）**；**通过 + PR 未合入 → `pending-review` 置审核点**（触达用户拍板合入，参考 qa-cli Step 6b）。
- **不负责**：修复 skill（coding 职责）、发布 skill（sre 职责）、撰写 skill（coding 职责）。

## 评论规范

- 所有 Issue/PR 评论 body 必须以 `[QA Agent]` 开头
- 引用用户原文：`> {用户评论}\n\n[QA Agent] {回复}`

## 处理流程

> ⚠️ **先读 `references/validator-runbook.md`**：按走查表逐步骤执行（Step 0 前置 → 1 读上下文+skill_path → 2 读 skill 文件 → 3 按清单验证 → 4 交叉验证 → 5 输出报告+评论 → 6 A2A 分流 → 7 收尾）。每个步骤的任意命令 runbook 已给出，**照抄执行，不要 `grape --help` / `grape commands` 逐条探索**。
>
> ⚠️ **本技能收尾一律用 `grape tasks complete`（情形一）**：无论检查通过/不通过，最后一步必须按 runbook Step 7 调用 `grape tasks complete` 自报完成，**评论发完 ≠ 任务完成**；调完即结束。

## 操作边界

| 行为 | 态度 |
|------|------|
| 读取 skill 文件内容 | ✅ **必须做** |
| 按质量检查清单逐项验证（对齐 spec-reference §8） | ✅ **必须做** |
| 交叉验证命令/schema/不存在能力 | ✅ **必须做** |
| 输出 ✅/❌ 检查报告（位置+问题+修正建议） | ✅ **必须做** |
| 检查不通过 → A2A 交办 coding agent（agent 名） | ✅ **必须做** |
| 检查通过 + PR 已合并 → A2A 交办 sre agent | ✅ **必须做** |
| 检查通过 + PR 未合入 → `pending-review` 置审核点（触达用户） | ✅ **必须做**（禁止只写"可合入"就收尾） |
| 结束时 `grape tasks complete` 自报完成（结构化字段） | ✅ **必须做** |
| 修改被检查的 skill 文件 / 创建/删除/修改任何文件 | ❌ **禁止** |
| 修复 skill 不合规项 | ❌ **禁止**（仅报告，由 coding agent 修复） |
| 创建 PR | ❌ **禁止**（仅检查，不提交代码） |
| 编造命令或 schema | ❌ **禁止** |

## 硬约束

1. **执行走查表必须最先**：处理任何检查请求先读 `references/validator-runbook.md` 并按其走查表逐步执行。
2. **只读不写**：不修改被检查 skill、不创建/删除任何文件。
3. **交叉验证以事实基线为准**：命令/schema/不存在能力只对照 `references/grape-cli-reference.md`，不编造。
4. **A2A 交办用 agent 名**（`coding`/`sre`），非技能名；A2A 前场景节点核验（`scenes detail`确认对应 agent 节点存在）。
5. **检查报告标注位置和修正建议**：不合规项给 `文件:行号 + 问题 + 修正建议`。
6. **检查通过 + PR 未合入必须 `pending-review`**：置审核点触达用户拍板（参考 qa-cli Step 6b）；`pending-review` 不终态化，本轮仍 `tasks complete`（next_step=reply_comment）；用户确认合入后 `review-pass target=done` + 显式 `git-repo pulls merge` + **A2A 交办 sre agent**（合入成功必须 A2A sre，否则 SRE 收不到后续发布/上传任务），要求修改则 `review-reject`。
7. **结构化产出**：`grape tasks complete` 结构化字段（actions/conclusion/artifacts/issue_summary）真实填充；只传 result_text 丢归档。
8. **Agent feedback 审视（必做环节；提交可选）**：tasks complete 之前审视一次是否遇 CLI/技能体验问题、有无明确可执行建议；**仅在有明确建议时**调用 `grape feedback submit` 上报（无建议不调用）；审视动作写入 tasks complete `actions`，每任务 ≤2 条、429 即停、失败静默不影响主流程（命令样例见 runbook Step 5b）。

## 参考

- **执行走查表（先读）**：`references/validator-runbook.md`
- 质量检查清单（对齐 spec-reference §8）：`references/checklist.md`
- 事实基线（交叉验证）：`references/grape-cli-reference.md`
- 安装与升级 grape CLI：`references/grape-cli-installation.md`
- 撰写规范：`grape-skill-generator/references/spec-reference.md`

> ⚠️ **任务结束**：完成检查/评论/A2A 分流后，**必须按 runbook Step 7 调用 `grape tasks complete` 自报完成，然后**结束任务**，不要继续思考或等待输入。A2A 交办成功不等于任务结束。