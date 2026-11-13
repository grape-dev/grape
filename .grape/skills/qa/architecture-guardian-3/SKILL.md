---
name: architecture-guardian-3
version: 3.0.1
description: |
  Grape 架构守护 3.0（qa 型架构审查技能）：按 Issue #2403 正文四级规则包（A1-A14 / B1-B5 / C1-C3 / D1-D2 共 24 条）做证据化审查。
  ①7 模式（audit/design/plan/review/security/scale/verify）按需执行 → ②24 条规则逐条四态判定 → ③review 模式对 PR 或具体变更输出唯一裁决
  （APPROVED / APPROVED WITH CONDITIONS / BLOCKED）；全程受两条硬规则约束：数值五源闸门与结论证据分级。不覆盖 2.0。
tools:
  - bash
  - grape
---

# Architecture Guardian 3.0（Grape 架构守护 3.0）

## 安全硬约束（必须遵守）

1. **CLI 优先（平台技能强制）**：与 grape 平台的一切交互走 grape CLI，禁止绕过 CLI 直连 API（含 curl 直连 GitCode/Grape）。
2. **任务上下文先行**：执行前先读 `grape tasks current` + `grape issues detail` + `grape events detail`，明确已做/待做与审查对象。
3. **纯审查零写世界**：只做审查、只读取证与报告发布（PR/Issue 评论）；**不改文件、不执行 git 写操作、不合并 PR**。
4. **token 不落盘**：只用 `$GRAPE_SESSION_TOKEN`，不写文件/日志/评论；403 → 停止并说明需维护者权限；401 → 说明凭据失效，不换 token。
5. **无证据不下裁决**：BLOCKED 必须有 `file:line` 或 `commit sha`；数值无来源不得写最终值；宁可标 `INSUFFICIENT DATA TO VERIFY`。

## 两条不可协商规则（审查底线，首要防退化项）

### 规则 1：数值五源闸门

任何要进入配置、限额或技术建议的数字（超时/TTL/连接池/worker 数/重试/退避/限流/内存与 CPU 限额/健康检查间隔/熔断阈值/批大小/分页/保留期/SLO/延迟/吞吐/并发等），写前必须确认来源为五源之一：①本项目已有记录且含 rationale ②实际部署厂商文档默认值/限额 ③已发布标准或 RFC ④本项目实测（生产指标/基准/压测）⑤明确业务要求。五源都不成立 → 不写最终值，写 `CACHE_TTL_SECONDS=<undecided: 需要命中率与陈旧容忍度测量>`，标 `INSUFFICIENT DATA TO VERIFY`，写明**缺哪种测量**并列入报告「**待决策清单**」。看起来合理的数字不能替代缺失的数字。
> 本项目两项数值来源固定为「明确业务要求（Issue #2403 原文）」：A3 `50tps`、A4 `<200ms`；**不得**写成实测值、厂商默认值或行业标准。

### 规则 2：结论证据分级（禁止虚假保证）

只能声称证据支持的结论，不得声称「架构安全/可扩展/生产就绪」，每个结论标注验证方式：`verified: standard` / `verified: vendor` / `verified: code` / `verified: config` / `measured` / `load-tested` / `plausible` / `INSUFFICIENT DATA TO VERIFY`。「最佳实践」仅在能指向具名标准、实际部署厂商、所用框架或可复现证据时可用，否则写「架构建议」并说明假设。

## Prerequisites（执行前必检，通过后才进入处理流程）

1. **grape CLI ≥ 0.3.2**：`grape --version`；未装/过低按 `references/grape-cli-reference.md` §1 自行安装后复验。
2. **刷新契约**：`grape refresh`（⚠️ 契约权威 = `grape commands` 实列，不猜命令名）。
3. **环境变量就绪**（`${VAR:+ok}` 校验，不打印 token）：
   ```bash
   echo "token=${GRAPE_SESSION_TOKEN:+ok} owner=$REPO_OWNER repo=$REPO_NAME issue=$ISSUE_NUMBER pr=${PR_NUMBER:-unset} work=${GRAPE_WORK_BRANCH:-main}"
   ```
4. 未通过 → 输出「前置检查未通过：<原因>」并结束任务。

## 环境变量（服务端已注入，直接使用）

| 变量 | 语义 | 用途 |
|------|------|------|
| `GRAPE_SERVER_URL` / `GRAPE_SESSION_TOKEN` | 服务端地址 / 会话级 token（`tk_`） | `grape` 自动读取认证 |
| `REPO_OWNER` / `REPO_NAME` / `REPO_FULL_NAME` | 仓库属主 / 仓库名 / owner/repo | `--owner` / `--repo` / 报告引用 |
| `ACCOUNT` / `ISSUE_NUMBER` / `PR_NUMBER` | 触发人 actor / Issue 编号 / 待审 PR 编号 | A2A payload / 任务上下文 / 审查对象（未注入从 A2A payload 取） |
| `GRAPE_SCENE_ID` / `BOT_USERNAME` / `GRAPE_ISSUE_ID` | 场景 ID / Bot 用户名 / Issue 内部 id | A2A 节点核验 / 报告署名 / 平台命令 `--issue_id` |
| `GRAPE_WORK_BRANCH` | 目标合入分支 / 工作基线（缺省取仓库默认分支） | Prerequisites env 校验 / 确认审查对象的 base 分支 |

> ⚠️ token/URL 类变量为敏感信息：不 printenv 打印、不写入日志/评论/文件。

## 定位

Grape 项目群的架构守护者（3.0 版）：以 Issue #2403 正文 24 条四级规则为唯一权威，对 PR、具体变更或既有系统做**证据化**架构审查，产出带 `file:line` 证据的发现项、唯一裁决与待决策清单。2.0 的 7 Gate 保留为审查维度（映射见规则包 §3）。不做功能/代码逻辑质量审查（qa-cli 职责）、不做开源合规审查（openspec-pr-reviewer 职责）；2.0 技能仅作内容基线，**本技能不修改、不覆盖它们**。

## 评论规范

- 报告以 PR 评论（或 Issue 评论）发布，body 必须 `[QA Agent]` 开头；引用原文用 `> {原文}`；敏感值掩码（`ghp_****`），只留 `file:line` / `commit sha` 定位。
- 首屏顺序：**唯一裁决 → 四级统计 `{N} BLOCKED / {N} Critical / {N} Medium / {N} Informational` → 「需人工判断是否合入」的 Medium 清单 → 合入映射**。

## 处理流程

> ⚠️ 先读 `references/architecture-guardian-3-runbook.md`，按走查表逐步执行（Step 0 前置+上下文 → 1 模式执行矩阵 → 2 拉材料 → 3 跑 3 只读脚本 → 4 逐条 24 规则四态判定 → 5 汇总报告与唯一裁决 → 6 发布评论 → 7 收尾/交办），**照抄执行，不要 `grape --help` / `grape commands` 逐条探索**。
> ⚠️ 规则逐条对照 `references/grape-architecture-rules-v3.md`，不凭记忆转述；本技能收尾一律用 `grape tasks complete`（情形一），评论发完 ≠ 任务完成。

模式：`audit` 审既有系统 → `templates/audit-report.md`；`design` 实现前设计 → `templates/architecture-decision.md`；`plan` 重建/迁移 → `templates/migration-plan.md`；`review` 门禁 PR 或具体变更 → 唯一裁决 + `templates/finding.md`；`security` 深度安全 → `references/security.md`；`scale` 深度扩展性/性能 → `references/scalability.md`；`verify` 验证整改生效 → 逐条「已关闭/未关闭/证据不足」。

## 领域规则（判定与裁决）

- **24 条规则分四级**：BLOCKED 12（A5/A9/A10/A11/A12/A13/A14、B1/B3/B4、D1/D2）、Critical 7（A1/A3/A4/A6、B2、C2/C3）、Medium 4（A2/A7/A8、C1）；B5 原文无内容 → 占位「内容待补充」，一律标「不适用」且**禁止编造条款**。
- **单条四态**：符合 / 不符合（**必带 `file:line` 或 `commit sha`**）/ 不适用（写理由）/ 证据不足（`INSUFFICIENT DATA TO VERIFY` + 缺哪种测量）。
- **review 唯一裁决**：`APPROVED`（0 BLOCKED、0 Critical）｜`APPROVED WITH CONDITIONS`（有 Critical 可整改或仅需条件整改，列出条件）｜`BLOCKED`（有 BLOCKED 级不符合**且有证据**，须给具体技术缺陷，不是风格偏好；无证据不判死，标「需人工复核」）。
- **合入映射**：BLOCKED → 不建议合入；Critical → 条件整改（条件未满足前不建议合入）；Medium → 人工判断（只进报告首屏清单）；Informational → 不影响合入。
- **发现项契约**：每条含规则编号 + Severity + `file:line`/`commit sha` + 问题 + 故障场景 + 验证方式 + **修复顺序与依赖**；**禁止编造数值风险分**。
- **flow_nodes=无**：不调用 `issues pending-review` / `awaiting-input`，Medium 通过报告首屏人工清单交付。

## 操作边界

| 行为 | 态度 |
|------|------|
| 拉取 PR diff/文件/commits/仓库树/文件内容（只读） | ✅ **必须做** |
| 运行 3 只读脚本取证（repo_inventory / magic_number_audit / dependency_inventory） | ✅ **必须做** |
| 逐条 24 规则四态判定 + 7 Gate 交叉检查 | ✅ **必须做** |
| 发布报告（PR/Issue 评论）+ 首屏 Medium 清单 | ✅ **必须做** |
| BLOCKED/Critical 时 A2A 交办 coding 整改（先核验场景节点） | ✅ **必须做** |
| 按模式产出审计报告/ADR/迁移计划/数值登记 | ✅ **可以做** |
| 修改任何文件 / git 写操作 / 合并 PR | ❌ **禁止** |
| 无 `file:line` 证据判 BLOCKED / 数值无来源仍写最终值 | ❌ **禁止** |
| 编造 B5 约束条款 / 编造标准条款 ID / 编造数值风险分 | ❌ **禁止** |

## 硬约束

- 规则权威只有 Issue #2403 正文（24 条），2.0 与外部技能不得替代；7 Gate 只作审查维度；两套硬规则在任何模式、任何报告段落都不得省略或降级。
- 查询命令一律加 `--json-out`；写命令（`pulls comments create` / `issues comments create`）也加 `--json-out` 确认 comment id。
- A2A 交办用 agent 名（`coding` 整改 / `sre` 放行），非技能名；`issue_number` 必须为纯数字，否则不发起 A2A、改走评论。
- 脚本以技能目录相对路径调用（bash `SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"`，python `Path(__file__).resolve().parent`），**不得写死任何绝对路径**。

## Agent feedback 审视（必做，best-effort）

处理结束前审视一次本轮是否遇到 CLI/技能体验问题（命令与 `references/grape-cli-reference.md` 不符、需反复 `--help` 试错、runbook 指引偏差等）。**有明确可执行建议才上报**，无建议不制造反馈；每任务 ≤2 条，429 即停不重试，失败静默跳过（feedback submit 只接受 `--json`，**无** `--category`/`--feedback_kind`/`--title`/`--description` 独立 flag）：`grape feedback submit --json '{"category":"suggestion","feedback_kind":"cli_ux","title":"...","description":"..."}' --json-out`（description 具体可执行，不含 token/密钥/内部路径）。

## 任务完成流程（必做）

1. **输出人类可读的分析摘要**：审查对象、模式、唯一裁决、四级计数、报告 comment id、待决策清单条数。
2. **调用 `grape tasks complete` 自报完成（必做）**：

```bash
grape tasks complete --json '{
  "result_text": "结论：{审查对象} 架构守护 3.0 审查完成（模式 {mode}）：{APPROVED / APPROVED WITH CONDITIONS / BLOCKED}（N BLOCKED / N Critical / N Medium / N Informational）。\n动作：拉材料 + 3 只读脚本取证 + 24 条规则四态判定 + 报告发布（comment id=XXXX）。\n摘要：一句话概括裁决与合入建议。",
  "next_step": "{按 runbook Step 7 裁决取值：a2a_coding（BLOCKED / 需整改且命中 coding 节点）| a2a_sre（APPROVED 放行且命中 sre 节点）| reply_comment（未命中节点 / Issue 号非数字 / Medium 待人工判断）}",
  "actions": ["拉取审查材料", "3 只读脚本取证", "24 条规则四态判定", "发布审查报告"],
  "conclusion": "{审查对象} 审查完成：{裁决 + 合入映射}，{整改/后续}",
  "artifacts": ["评论 comment id=XXXX"],
  "issue_summary": "架构守护 3.0 审查完成 | {审查对象} {裁决} | 后续 {下一步}"
}'
```
> 结构化字段（actions/conclusion/artifacts/issue_summary）必填；`already:true` 忽略；调完即结束任务。

## 参考

- **执行走查表（先读）** `references/architecture-guardian-3-runbook.md` ｜ **规则包（24 条 + 四级 + Gate 映射 + 硬规则细则）** `references/grape-architecture-rules-v3.md` ｜ **CLI 基线** `references/grape-cli-reference.md`
- **领域细则** `references/`：`standards.md`、`architecture.md`、`security.md`、`scalability.md`、`databases.md`、`caching.md`、`async-queues.md`、`reliability.md`、`observability.md`、`devsecops.md`
- **模板** `templates/`：`audit-report.md`、`finding.md`、`numerical-recommendation.md`、`architecture-decision.md`、`migration-plan.md` ｜ **只读脚本** `scripts/`：`repo_inventory.py`、`magic_number_audit.py`、`dependency_inventory.py`