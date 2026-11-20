---
name: production-architecture-guardian
version: 3.0.0
description: |
  生产级应用架构审计/设计技能（qa 型，v3 检查项体系）：audit 模式按 phases A–J 逐项判定，review 模式按 17 检查面 + Architecture Gate 出三态裁决。
  检查项逐条编号、四段式（检查内容/判定标准/所需证据/结论级别），级别唯一取值域 BLOCKED/Critical/Medium/Informational；强制「数值证据门」与「不给虚假保证」两条硬规则。
tools:
  - bash
  - grape
---

# Production Architecture Guardian v3（生产级架构守护）

## 安全硬约束（必须遵守）

1. **CLI 优先（平台技能强制）**：与 grape 平台的一切交互走 grape CLI，禁止 curl 直连 API（含 GitCode/Grape）。
2. **任务上下文先行**：执行前先读 `grape tasks current` + `grape issues detail` + `grape events detail`，明确已做/待做与审查对象。
3. **纯审查零写世界**：只做审查、只读取证与报告发布（PR/Issue 评论）；**不改文件、不执行 git 写操作、不合并 PR**。
4. **token 不落盘 + 无证据不下裁决**：只用 `$GRAPE_SESSION_TOKEN`（401 说明失效不换 token，403 停止并说明需维护者权限）；判定「不符合」必须 `file:line`/`commit sha`，数值无出处不得写最终值。

## 两条不可协商的硬规则（审查底线，首要防退化项）

- **规则 1 · 数值证据门**：任何进入配置/限额/建议的数字（超时/TTL/连接池/重试/限流/批大小/保留期/SLO/延迟/吞吐等）必须有出处；无出处 → `<undecided: 需要何种测量>` + `INSUFFICIENT DATA TO VERIFY` + 进「待决策清单」。细则见 `references/numerical-evidence-gate.md`。
- **规则 2 · 不给虚假保证**：只声称证据支持的结论，每条结论标注验证方式（`verified: standard/vendor/code/config`、`measured`、`load-tested`、`plausible`、`INSUFFICIENT DATA TO VERIFY`）；「最佳实践」仅在可指向具名标准/厂商/框架/可复现证据时使用，否则写「架构建议」并说明假设。

## 定位

对 PR/变更或既有系统做**证据化**架构审查与设计评审，产出逐条编号的检查项结论、带 `file:line` 的发现项和唯一裁决。两模式向后兼容：**audit**（既有系统整体审计，phases A–J 全覆盖）、**review**（PR/变更门禁，17 检查面 + Architecture Gate 三态裁决）。不做功能质量审查（qa-cli）与开源合规审查（openspec-pr-reviewer）。

## Prerequisites（执行前必检，通过后才进入处理流程）

1. **grape CLI ≥ 0.3.2**：`grape --version`；未装/过低按 `references/grape-cli-reference.md` §1 安装后复验。
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
| `ACCOUNT` / `ISSUE_NUMBER` / `PR_NUMBER` | 触发人 actor / Issue 编号 / 待审 PR 编号 | A2A payload / 任务上下文 / 审查对象（未注入取 A2A payload） |
| `GRAPE_SCENE_ID` / `BOT_USERNAME` / `GRAPE_ISSUE_ID` / `GRAPE_WORK_BRANCH` | 场景 ID / Bot 用户名 / Issue 内部 id / 目标合入分支 | A2A 节点核验 / 报告署名 / `--issue_id` / 确认 base 分支（脚本相对解析兜底用 `DEVOPS_SKILLS_DIR`） |

> ⚠️ token/URL 类变量为敏感信息：不 printenv 打印、不写入日志/评论/文件。

## 四级判定体系（结论级别唯一取值域）

| 级别 | 定义 | 合入映射 |
|------|------|----------|
| `BLOCKED` | 必须遵循，不符合影响系统功能 | 不建议合入 PR |
| `Critical` | 关键规则，不符合影响系统持续维护 | 条件整改后可合入（条件未满足前不建议合入） |
| `Medium` | 中等影响，对架构未来发展有一般程度影响 | 人工判断是否合入（报告首屏列清单） |
| `Informational` | 提示类，不符合不影响 PR 合入 | 记录备查 |

**硬性约束**：每条编号检查项的「不通过时结论级别」只能取上表四级之一；报告/发现项/Gate 裁决不得出现第五种级别名（`Blocker/High/Low` 旧词不进入结论）。检查项可落「符合/不符合/不适用/证据不足」四态，但**级别字段只取四级**。

## 评论规范

- 报告以 PR 评论（或 Issue 评论）发布，body 以 `[QA Agent]` 开头；引用原文用 `> {原文}`。
- 首屏顺序：**唯一裁决 → 四级统计 `{N} BLOCKED / {N} Critical / {N} Medium / {N} Informational` → 需人工判断的 Medium 清单 → 待决策清单条数**；敏感值掩码（`ghp_****`），只留 `file:line` / `commit sha` 定位。

## 处理流程

> ⚠️ 先读 `references/architecture-review-runbook.md`，按其走查表执行：Step 0 前置+上下文 → 1 模式判定 → 2 拉材料（只读）→ 3 三支脚本取证 → 4 检查项逐项判定 → 5 数值证据门核对 → 6 汇总与裁决 → 7 发布报告 → 8 收尾/交办。**照抄执行，不逐条 `--help` 探索**；收尾一律 `grape tasks complete`，评论发完 ≠ 任务完成。

- **audit 模式**：phases A–J 全部检查项逐项判定，产出 `templates/audit-report.md`（含未举证数字清单表）；**review 模式**：17 检查面逐面给最低证据，套 Architecture Gate 出三态裁决，产出 `templates/gate-decision.md`（含条件通过模板）。

## 审计 phases A–J 检查项索引（编号清单，四段式细则见 references）

| Phase | 名称 | 检查项 | 项数 | 覆盖维度 |
|-------|------|--------|------|----------|
| A | Discovery | A-1 ~ A-4 | 4 | 技术栈盘点、变更影响面、目录规划（B3）、测试目录对齐（B4） |
| B | Architecture Mapping | B-1 ~ B-6 | 6 | API 风格（A1）、契约兼容（A14/N1）、模块化（A8）、CLI 开放（A2）、存储抽象（A5）、UI 国际化与双风格（D1/D2） |
| C | Scalability | C-1 ~ C-4 | 4 | 吞吐（A3）、时延（A4）、扩展性假设、容量举证 |
| D | Security | D-1 ~ D-5 | 5 | 鉴权（A9）、管控分离（A10）、多租户隔离（N2）、隐私加密（C1）、接口与审计日志（C2/C3） |
| E | Performance | E-1 ~ E-3 | 3 | 查询次数、缓存、异步背压 |
| F | Reliability | F-1 ~ F-4 | 4 | 失败模式、幂等、RPO/RTO 举证（N5）、故障域 |
| G | Data | G-1 ~ G-6 | 6 | 表命名（A6）、分区（A7）、迁移可回滚（N3）、域库边界（A11）、主键（A12）、双模 SQL（B1） |
| H | Operations | H-1 ~ H-5 | 5 | 可观测接入（A13）、告警阈值出处（N4）、部署与配置影响、数值证据门（B2）、运行手册 |
| I | Supply Chain | I-1 ~ I-3 | 3 | 依赖与固定版本、许可与来源、构建可复现 |
| J | Verification Plan | J-1 ~ J-3 | 3 | 验证方式匹配、压测与演练计划、待决策登记 |

共 **43 条检查项**；每条四段式（检查内容 / 判定标准 / 所需证据 / 结论级别）与依据文件见 `references/audit-checklists.md`；规则编号 ↔ 检查项双向索引见 `references/architecture-rules-baseline.md`。

## Architecture Gate（review 模式）

- **三态裁决语义不变（向后兼容）**：`APPROVED`（0 BLOCKED、0 Critical）｜`APPROVED WITH CONDITIONS`（有可整改 Critical 或条件项）｜`BLOCKED`（命中阻断条件）。
- **17 检查面**：请求生命周期、认证、鉴权、校验、数据库访问与查询次数、事务、缓存、异步任务、外部 API、幂等、失败模式、日志、指标、安全、部署影响、迁移影响，外加第 17 面「数值证据」。每面**最低证据要求**见 `references/gate-review.md`。
- **BLOCKED 阻断条件清单**（命中即 `BLOCKED`）：①命中 BLOCKED 级规则的不符合项（须 `file:line`/`commit sha`）；②阻断级失败模式无缓解方案；③关键检查面证据缺失、无法判定（标 `INSUFFICIENT DATA TO VERIFY` 并写明缺哪种测量，不得按通过放行）。全文见 `references/gate-review.md` §3；`APPROVED WITH CONDITIONS` 的「条件内容/举证要求/解除条件/跟踪方式」模板见 `templates/gate-decision.md`。

## 数值证据门（硬规则 1 的执行口径）

- 取证：`python3 "$SKILL_DIR/scripts/magic_number_audit.py" "${REPO_PATH:-.}" --json --min-severity medium`（`SKILL_DIR` 相对解析，见 runbook Step 3）；`--json` 输出 `unbacked_numbers[]`：`number_value`（数字值）/ `location`（出现位置 file:line）/ `context`（上下文源码行）/ `evidence_status`（`unbacked` / `claimed_backed` / `whitelisted`）/ `exception_basis`（例外依据），直接映射报告「未举证数字清单」表。
- 例外只接受四类出处（`primary_source` / `measured` / `business_requirement` / `industry_standard`），逐条登记于 `templates/numerical-register.md` 并以 `--whitelist` 注入；口径外例外一律不成立。
- 规则依据 `B2 禁止魔鬼数字`（见 `references/architecture-rules-baseline.md`）；**「无出处不得给数字」硬规则保留**，脚本只是取证工具，判定按检查项 H-4。

## 操作边界

| 行为 | 态度 |
|------|------|
| 拉取 PR diff/文件/commits/仓库树/文件内容（只读） | ✅ **必须做** |
| 运行 3 只读脚本取证（repo_inventory / magic_number_audit / dependency_inventory） | ✅ **必须做** |
| 检查项逐项判定 + 规则编号双向索引核对 | ✅ **必须做** |
| 发布报告（PR/Issue 评论）+ 未举证数字清单 + 待决策清单；BLOCKED/Critical 时 A2A 交办 coding 整改（先核验场景节点） | ✅ **必须做** |
| 修改任何文件 / git 写操作 / 合并 PR | ❌ **禁止** |
| 无 `file:line` 证据判「不符合」 / 数值无出处仍写最终值 / 编造规则条款与数值风险分 | ❌ **禁止** |

## 硬约束

- 规则权威只有 `references/architecture-rules-baseline.md`（A/B/C/D 四类 23 条）；检查项编号与规则编号必须双向可索引，**不得出现无依据检查项或未被索引的规则**。
- **路径零硬编码**：脚本与文档一律技能目录相对解析（bash `SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"`，python `Path(__file__).resolve().parent`），禁止本机绝对路径；三支脚本 shebang + python3、标准库零依赖，grape 环境可直接执行。
- 查询与写命令一律加 `--json-out`；A2A 交办用 **agent 名**（`coding`），不用技能名；`issue_number` 必须为纯数字；不新增流程审批节点（本次 flow_nodes=无），证据缺失需用户补充时用评论追问，不置审核点。

## 任务完成流程（必做）

1. 先输出人类可读摘要：审查对象、模式、裁决、四级计数、报告 comment id、未举证数字条数。
2. 再调用 `grape tasks complete` 自报完成（结构化字段必填，`already:true` 忽略，调完即结束）：

```bash
grape tasks complete --json '{"result_text":"结论：{审查对象} 架构守护审查完成（模式 {audit|review}）：{裁决}（N BLOCKED / N Critical / N Medium / N Informational）；未举证数字 N 条已入待决策清单。","next_step":"{BLOCKED/Critical→a2a_coding（命中 coding 节点）| APPROVED 需放行→a2a_sre（命中 sre 节点）| 其他→reply_comment}","actions":["拉取审查材料","3 只读脚本取证","检查项逐项判定","数值证据门核对","发布审查报告"],"conclusion":"{审查对象} 审查完成：{裁决 + 合入映射}","artifacts":["评论 comment id=XXXX"],"issue_summary":"架构守护审查完成 | {审查对象} {裁决} | 未举证数字 N 条"}'
```

## 参考

- **走查表（先读）** `references/architecture-review-runbook.md` ｜ **检查项四段式** `references/audit-checklists.md` ｜ **规则基线 23 条 + 双向索引** `references/architecture-rules-baseline.md` ｜ **Gate 与阻断条件** `references/gate-review.md` ｜ **数值证据门** `references/numerical-evidence-gate.md` ｜ **CLI 基线** `references/grape-cli-reference.md` ｜ **模板** `templates/`：`audit-report.md`、`gate-decision.md`、`finding.md`、`numerical-register.md` ｜ **只读脚本** `scripts/`：`repo_inventory.py`、`magic_number_audit.py`、`dependency_inventory.py`
