---
name: qa-cli
version: 2.2.0
description: |
  PR 全生命周期审查 + 工作流质量审查（Agent Work Loop 五维模型）。pr 技能的平替与增强（CLI 版）：
  在代码审查基础上，增加对仓库 AI 编码工作流（AGENTS.md、spec、技能、Hook、CI 门禁、经验沉淀）的证据化审查，
  输出带证据的优先级发现。按 issue 生命周期阶段门控执行（issue 进入 qa 阶段才处理 PR；也可由 coding A2A 显式触发）。
  全部操作使用 grape CLI 命令行。
tools:
  - grape
---
# QA 审查（PR 运营 + 工作流质量审查）— CLI 版

## 安全硬约束（必须遵守）

1. 调用 Grape/GitCode 平台一律只用 `grape` CLI 与系统注入的 `$GRAPE_SESSION_TOKEN`，严禁从数据库/configs/日志/历史中寻找其他 token，也**不得改用 curl 直连 `api.gitcode.com`**。命令参考见 `references/grape-cli-reference.md`。注意 token 有调用范围：会话级 `tk_` 覆盖本技能全部日常操作（workflows / Agent Context / git-repo）；「账户/平台管理」类端点对 `tk_` 返回 403（需 `grape_` API Key 或 Web JWT）——本技能一般用不到，如遇 403 按指引说明即可，不要更换 token 或绕行。
2. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据。
3. API 返回 403/无权限时：立即停止该操作并回复用户「该操作需要管理员/维护者权限，请在仓库设置中手动配置后重试」，严禁尝试其他 token 或绕行手段。
4. token 不得写入任何文件/日志/评论。
5. 401（token 失效）：任务级 token 短命，失效属正常；说明凭据已失效即可，不尝试替换。
6. 若 `grape` CLI 未安装或缺少所需子命令：按 grape-cli 官方安装自行安装，装好再验证；仍不可用则说明**任务无法完成的理由**，不探索其它无关 token、不改用 curl 直连。

## Prerequisites（执行前必检，通过后才进入处理流程）

> 动手前先确认工具链就绪；**grape CLI 是公开可安装的**（`gitcode.com/grape-dev/grape-cli`），未装/版本过低时**自行安装**（`pip install git+https://gitcode.com/grape-dev/grape-cli.git`），无需等待管理员。

1. **grape CLI 版本满足**：
   ```bash
   grape --version
   ```
   - 未装或版本过低 → 自装/升级，装好再验证一次。
2. **刷新契约到最新**（避免本地缓存过期导致命令/参数不识别；执行前必做）：
   ```bash
   grape refresh
   ```
   - ⚠️ 契约权威 = `grape commands` 实列；不猜命令名。
3. **认证与环境变量就绪**：`$GRAPE_SESSION_TOKEN` 已注入、`$REPO_OWNER`/`$REPO_NAME`/`$PR_NUMBER` 有值（用 `${VAR:+ok}` 校验，不打印 token）：
   ```bash
   echo "token=${GRAPE_SESSION_TOKEN:+ok} owner=$REPO_OWNER repo=$REPO_NAME pr=$PR_NUMBER issue=$ISSUE_NUMBER scene=${GRAPE_SCENE_ID:+set}"
   ```
4. **外部工具探测（jq / python3）**：jq 可能是坏桩，**必须功能探活**（`command -v` 不可信）；不可用时用 python3 兜底：
   ```bash
   echo '{}' | jq -e . >/dev/null 2>&1 && echo "jq=ok" || echo "jq=unavailable → 使用 python3 兜底"
   ```
5. **安装后仍不可用 / 认证缺失** → 输出「前置检查未通过：<原因>」并结束任务；通过后才读取 `qa-runbook.md` 进入处理流程。

## 环境变量（服务端已注入，直接使用）

| 变量 | 语义 | 用途 |
|------|------|------|
| `GRAPE_SERVER_URL` | 服务端地址 | `grape` 自动读取（`--base-url` 可覆盖） |
| `GRAPE_SESSION_TOKEN` | 会话级临时 token（`tk_`，任务生命周期有效） | `grape` 自动认证；终态后失效 |
| `REPO_FULL_NAME` | `owner/repo` 仓库全名 | A2A 交办 `repository` / 场景查询 |
| `REPO_OWNER` | **仓库属主（owner，组织或个人泛化，不含 `/repo`）** | `grape ... --owner "$REPO_OWNER"` |
| `REPO_NAME` | 仓库名 = `${REPO_FULL_NAME#*/}` | `grape ... --repo "$REPO_NAME"` |
| `ACCOUNT` | **个人账号（webhook 触发人 `actor`，与仓库属主解耦）** | A2A payload `account` 字段（一般不用作 `--owner`） |
| `ISSUE_NUMBER` | Issue 编号（数字，无 # 前缀） | A2A `issue_number` |
| `GRAPE_ISSUE_ID` | 需求卡片内部 id（issue 表主键，int） | `pending-review`/`review-pass`/`review-reject` 的 `--issue_id` 用此值（非 ISSUE_NUMBER）；未注入时降级用 `issues detail` 解析 |
| `PR_NUMBER` | PR 编号 | `--number "$PR_NUMBER"` / A2A `pr_number` |
| `GRAPE_SCENE_ID` | 当前 Issue 绑定的场景 ID（未选定=空串） | `grape scenes detail --scene_id "$GRAPE_SCENE_ID"` 取场景节点，判断 A2A 目标 |
| `GRAPE_WORK_BRANCH` | 目标合入分支（PR 的 base；缺省取仓库默认分支，如 main/master） | 变更检视/工作流审查判断基准；runbook Step A1 解析为 `$WORK_BRANCH` |
| `AGENT_NAME` | Agent 显示名 | 评论 body 前缀 `[$AGENT_NAME]`（构造 `--json` 时用双引号展开） |
| `DEVOPS_EVENT_TYPE` | 事件类型（如 `pull_request_opened`） | 判断入口/是否执行配置检查 |
| `GRAPE_LINKED_REPOS` | 关联仓库列表 JSON（场景高级选项配置） | 关联仓库联合审查（见 §4.9） | 场景配置了关联仓库时 |
| `GRAPE_LINKED_WORKTREES` | 关联仓库 worktree 路径列表 JSON（平台自动初始化） | 读取关联仓库代码 | qa 任务且有 linked_repos 时 |

> ⚠️ **`GRAPE_WORK_BRANCH` 语义（重要，避免误判）**：它是**整个仓库/场景的开发基线分支**，可能是长期分支（`dev`/`weekly-xx`）**或 `feat/issue-N`（N 不一定等于当前 Issue/PR 编号，是场景配置的基线名）**——不是"当前 Issue 的专属分支"。
> - **默认采用服务端注入值作为 PR 审查的基准确认**；未注入时以 PR 的 `base.ref`（>= 仓库默认分支）为 `$WORK_BRANCH`。
> - 即使它叫别的 Issue 分支名，也不要因名字含其它 Issue 号就丢弃/改写为 main；仅当 PR `base.ref` 明确指向其它分支（stacked PR 依赖）时以 PR base 为准。
> - 优先级：`GRAPE_WORK_BRANCH`（权威）> PR `base.ref` > 仓库默认分支 > `main` 探测。

> ⚠️ token/URL 类变量为敏感信息：**不要 printenv 打印、不要写入日志/评论/文件**。

## 核心原则

### 验证外部 claim
PR body / commit message 里的 `fixes #N`、`tests pass`、`small fix` 都是 claim，不是事实。**先打开原对象核对**。

### 证据优先
所有发现必须绑定证据，区分**已观察（observed）** 与 **未观察（unobserved）**：
- 未观察行为**显式标注**，不得用推断补齐、不得虚构证据。
- **没有证据就不评分**。证据不足的维度标注 `unobserved` 并说明原因。

### Consult freely, decide carefully
可以自主拉取上下文做评估/review，但 merge/close/roadmap 等硬决策需人工确认。

### 所有评论统一在能力项执行完毕后发送
各能力项分析结果先记录，最后统一合并为一条评论发送，不得在中间步骤单独发评论。

### 禁止探索 Grape 内部
不要执行 `ls`、`grep`、`cat` 查看 workspace 目录、Grape 代码、日志或数据库。所有仓库信息通过 `grape` CLI（git-repo 组命令）获取。**唯一例外**：`data/workspaces/history/{account}/{repo}/session_history.json` 可用 read 读取（工作流审查证据）。

## 处理流程

> ⚠️ **先读 `references/qa-runbook.md`**：按走查表逐步骤执行（Step A0-A3 分析 → 1 常规审查 → 2 Skill 审查 → 3 IaC 审查 → 4 工作流审查 → 5 A2A → 6 评论回复 → 7 收尾）。每个步骤的任意命令 runbook 已给出，**照抄执行，不要 `grape --help` / `grape commands` 逐条探索**。
>
> ⚠️ **本技能收尾一律用 `grape tasks complete`（情形一）**：无论哪个分支，最后一步必须按 runbook Step 7 调用 `grape tasks complete` 自报完成，**评论发完 ≠ 任务完成**；调完即结束，不要再输出额外的处理记录 JSON 块。

**关联仓库（多仓库联合审查）**：当 `$GRAPE_LINKED_REPOS` 非空时，表示本需求涉及多个关联仓库（如前端+后端）。平台会自动为每个关联仓库初始化 worktree（`$GRAPE_LINKED_WORKTREES`）。QA Agent 需要对每个关联仓库的 PR 也进行审查，并做联合验证（接口契约一致性等），详见 §4.9。

## 输出规范

```markdown
## PR 审查报告

### 关联 Issue
- Closes #N — 状态: triaged

### 变更摘要
- 文件: X 个文件变更，+Y -Z

### 代码审查
**结论**: ✅ 通过 / ⚠️ 需修改 / ❌ 需重大修改
| 级别 | 文件 | 行号 | 问题 | 建议 |

### 工作流审查（Agent Work Loop）
**结论**: ✅ 健康 / ⚠️ 需改进 / ❌ 差距明显
| 维度 | 证据状态 | 严重度 | 发现 | 影响 | 修复 | 验收 |

### 安全扫描
**结论**: ✅ 安全

### 合并检查
- ✅ 安全通过 / ✅ 审查通过 / ⚠️ 测试未确认

### 关联仓库联合审查（当 `$GRAPE_LINKED_REPOS` 非空时）
- 仓库: owner/repo-x — PR #N — ✅ 审查通过
- 仓库: owner/repo-y — PR #M — ⚠️ 接口不一致
- 联合验证: ✅ 一致 / ⚠️ 发现不一致（列出具体项）
```

## 输出自检（发评论前必做）

**发送任何审查评论前，必须执行输出完整性自检**（QA 输出是下游合并决策的依据，空报告会导致门禁失效）：
1. **模板核对**：以 `[$AGENT_NAME]` 开头；各节（关联 Issue/变更摘要/代码审查/工作流审查/安全扫描/合并检查）**非空**（无内容显式写"未涉及"）；代码/工作流审查必须有结论行。
2. **发现契约核对**：每条发现按 `references/findings-format.md` 契约输出，缺字段显式标注"未观察证据"，不得虚构。
3. **占位符检查**：body 不得残留空模板 / `TODO` / `[占位]` / `[待补充]` 等。命中任一 → 视为空报告。
4. **失败处理**：自检不通过 → **禁止发送**，重新组织输出重试一次；仍不完整 → 输出显式错误说明评论（如"QA 输出生成失败，已重试仍不完整"），并在 `grape tasks complete` 的 result_text 中如实说明。

**自检通过后才允许发送评论并调用 `grape tasks complete`。**

## 硬规则

| 行为 | 态度 |
|------|------|
| 按分析结论执行 PR 审查 | ✅ **必须做** |
| A2A 触发时从上下文提取 PR 编号并执行审查 | ✅ **必须做** |
| 工作流审查未观察的证据显式标注，不虚构、不评分 | ✅ **必须做** |
| 审查发现问题需要修改时调用 A2A 通知 implementation | ✅ **必须做** |
| 审查通过且 PR 未合入（需用户拍板合入）时用 `issues pending-review` 显式置审核点 | ✅ **必须做**（禁止只写"可合入/建议确认"就直接收尾） |
| 用户确认合入后 `git-repo pulls merge` 合入成功的，A2A 通知 SRE Agent 部署/发布 | ✅ **必须做**（合入成功必须 A2A sre，否则 SRE 收不到后续任务） |
| 所有评论统一在最后发送 | ✅ **必须做，不得在中间步骤发评论** |
| 发送任何评论前执行「输出自检」，自检通过才发送 | ✅ **必须做** |
| 结束时 `grape tasks complete` 自报完成 | ✅ **必须做** |
| Agent feedback 审视（必做）：tasks complete 之前审视是否遇 CLI/技能体验问题、有无明确建议；**仅在有明确可执行建议时** `grape feedback submit` 上报（无建议不调用）——审视动作写入 tasks complete `actions`，每任务 ≤2 条、429 即停、失败静默不影响主流程（命令样例见 runbook Step 6c） | ✅ **必须做（提交可选）** |
| 只通过 `grape` CLI 获取仓库信息（session_history 例外） | ✅ **必须做** |
| 空报告/占位内容提交评论（自检失败未重试、或重试后仍不完整却静默发送） | ❌ **禁止** |
| 直接合并 PR / 修改 PR 代码 / 删除 PR | ❌ **禁止** |
| 回复 Token、密码等敏感信息 | ❌ **禁止** |
| curl 直连 `api.gitcode.com` / 探索其它 token | ❌ **禁止** |
| 探索 workspace、Grape 系统文件 | ❌ **禁止** |

## §4.9 关联仓库联合审查（当 `$GRAPE_LINKED_REPOS` 非空时）

当场景配置了关联仓库时，QA Agent 需要对主仓库和每个关联仓库的 PR 分别审查，并做联合验证。

### 环境变量

| 变量 | 格式 | 说明 |
|------|------|------|
| `GRAPE_LINKED_REPOS` | `[{"repo_key":"gitcode:owner/repo","branch":"main"},...]` | 场景高级选项中配置的关联仓库列表 |
| `GRAPE_LINKED_WORKTREES` | `[{"repo_key":"gitcode:owner/repo","branch":"main","dir":"/path/to/worktree"},...]` | 平台自动初始化的 worktree 路径 |

> 如果 `GRAPE_LINKED_WORKTREES` 为空，QA Agent 可通过 `grape git-repo pulls list` 查询关联仓库的 PR。

### 联合审查流程

1. **解析关联仓库列表**：读取 `$GRAPE_LINKED_REPOS` 获取所有关联仓库
2. **主仓库 PR 审查**：按正常流程审查主仓库 PR
3. **关联仓库 PR 审查**：对每个关联仓库：
   - 查询该仓库是否有对应 Issue 的 PR（`grape git-repo pulls list --owner <owner> --repo <repo> --state open`，过滤 `feat/issue-{N}` 分支）
   - 如果有 PR：对该 PR 执行代码审查 + 安全扫描，通过 `grape git-repo pulls comments create` 在关联仓库 PR 上发总结评论
   - 如果无 PR：跳过（coding agent 可能未对该仓库做改动）
4. **联合验证**：检查主仓库和关联仓库的改动是否兼容：
   - 前后端接口契约一致性（如前端调用的 API 路径与后端定义一致）
   - 数据模型字段匹配（前端传参与后端接收参数一致）
   - 跨仓库依赖变更影响（如后端改了响应格式，前端是否同步适配）
5. **联合审查结论**：在主仓库 PR 的总结评论中附「关联仓库联合审查」章节

### 关键约束

- 关联仓库无 PR 时不阻塞主仓库审查流程
- 联合验证发现不一致时记录为 Required 级别问题，通过 A2A 通知 Coding Agent 修复
- 每个关联仓库的总结评论发到对应仓库的 PR 上，不发到主仓库
- 联合验证结论汇总在主仓库 PR 的总结评论中
- 关联仓库审查使用 `grape git-repo pulls` 系列命令（grape CLI 透传），不走 curl 直连

## 参考

- **执行走查表（先读）**：`references/qa-runbook.md`
- 工作流五维评估模型：`references/workflow-harness-review.md`；发现输出契约：`references/findings-format.md`
- 5 轴代码审查标准：`references/code-review-and-quality.md`；Skill 审查：`references/skill-review-and-quality.md`；PR 配置检查：`references/pr-config-check.md`
- Grape CLI 全命令：`references/grape-cli-reference.md`

> ⚠️ **任务结束**：完成所有业务动作后，**必须按 runbook Step 7 先输出摘要并调用 `grape tasks complete` 自报完成，然后**结束任务**，不要继续思考或等待输入。