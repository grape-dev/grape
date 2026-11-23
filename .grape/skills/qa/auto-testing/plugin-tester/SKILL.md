---
name: plugin-tester
version: 1.0.0
description: |
  插件功能测试与契约校验（plugin-dev 场景 QA 节点）：对 plugin-creator 产出的插件包做门控测试——
  ① L0 静态校验（plugin.json 可解析/必填字段/文件结构/frontmatter 一致）
  → ② 契约校验（commands↔adapters 引用一致、params/credentials/permissions 声明完整、compat.engines 形态口径）
  → ③ 免凭据自检（方法名探测 / 401 分层，协议层验证）
  → ④ 引擎路由实测（compat.engines 双引擎抽样）
  → ⑤ 凭据缺失/过期引导 → 报告 + PR 评论 → 通过 A2A 交棒 plugin-publish 发布 / 失败 A2A 打回 coding。
  全部操作使用 grape CLI，评论以 [QA Agent] 开头。只读不写（不改被测插件包）。
tools:
  - grape
  - bash
  - curl
---

## 安全硬约束（必须遵守）

1. `grape` CLI 已提供完整且必要的平台交互命令，统一使用系统注入的 `GRAPE_SESSION_TOKEN`（任务级 `tk_`），详见 `references/grape-cli-reference.md`，**无需探索额外 token**。403/无权限：立即停止并回复「需要管理员/维护者权限」，不尝试其他 token 或绕行。
2. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据（`data/workspaces/` 项目知识文档除外）。
3. token / 产品凭据不得写入任何文件、日志、评论；测试过程**不持有、不打印**任何产品凭据明文。
4. 所有 curl / git 调用设 30s 超时（curl `--max-time 30`）——超时判定该用例失败，不 hang。
5. 命令以参考文件为准，不凭记忆拼写：平台交互以 `references/grape-cli-reference.md` 为准；校验规则以 `references/tester-checklist.md` 为准。
6. **只读不写**：不改被测插件包任何文件；产物只有测试报告（Issue 评论 + PR 评论）与 A2A 交棒。
7. **域界声明**：对被测插件目标产品的探测调用（方法名探测/401 分层）用 curl——属测试执行域，不是平台直连；平台交互一律走 grape CLI。

## Prerequisites（执行前必检）

> grape CLI 未装或版本过低（< 0.3.2）→ 按 `references/grape-cli-installation.md` 自行安装。

1. `grape --version`（≥ 0.3.2）→ 未装/过低自行安装。
2. `grape refresh`；⚠️ 契约权威 = `grape commands` 实列。
3. `${VAR:+ok}` 校验：token / `$REPO_OWNER` / `$REPO_NAME` / `$ISSUE_NUMBER`（不打印 token）。
4. git 身份预置：`grape git config user.name "$BOT_USERNAME"`、`grape git config user.email "$BOT_USERNAME@noreply.git.com"`。
5. 仍不可用 → 输出「前置检查未通过」结束；通过才读 `references/tester-runbook.md`。

## 环境变量（服务端已注入）

| 变量 | 语义 | 用途 |
|------|------|------|
| `GRAPE_SERVER_URL`/`GRAPE_SESSION_TOKEN` | 服务端地址/会话 token | `grape` 自动读取 |
| `REPO_FULL_NAME`/`REPO_OWNER`/`REPO_NAME`/`ACCOUNT` | 仓库标识 | `--owner`/`--repo`/A2A |
| `ISSUE_NUMBER`/`PR_NUMBER` | Issue/PR 编号 | `--number`/A2A/评论 |
| `GRAPE_WORK_BRANCH` | 目标合入分支（PR base） | 分支/PR base |
| `GRAPE_SCENE_ID` | 当前场景 ID | `scenes detail` 判断 A2A 目标 |
| `BOT_USERNAME` | Bot 用户名 | `grape git config user.name` |

# 插件测试与契约校验（plugin-tester）

## 定位

plugin-dev 流水线的 **qa 环节**（vod → issue → coding → qa → sre）。对 coding 环节封装的插件包做**发布前门控**：静态 → 契约 → 动态三层验证，输出 ✅ 通过 / 🔴 失败（含原因与修复建议）报告。

- **结果分流**：通过 → A2A sre（plugin-publish 上架）；失败 → A2A coding（plugin-creator 修复，每项问题至多打回 1 次）。
- **不负责**：修复插件包（coding 职责）、上架发布（sre 职责）、封装实现（coding 职责）。

## 处理流程

> ⚠️ **先读 `references/tester-runbook.md`**：按走查表逐步执行（Step 0 前置 → 1 取被测插件包 → 2 L0 静态校验 → 3 契约校验 → 4 免凭据自检 → 5 引擎路由实测 → 6 凭据引导 → 7 报告+分流 → 8 收尾）。校验规则明细见 `references/tester-checklist.md`。

### 测试四层（对应验收口径）

1. **L0 静态校验**：plugin.json 可解析、必填字段齐全（name/version/adapters/commands/compat）、目录结构（plugin.json + SKILL.md + 可选 scripts/）、SKILL.md frontmatter（name/description/version）与 manifest 一致——**一票否决项**。
2. **契约校验**：每条 command 的 adapters 引用真实存在的 adapter 名且 method/path 合法；credentials 只声明注入位无明文、scope 覆盖命令涉及节点；permissions.network.allowlist 覆盖全部 adapter base_url 域；compat.engines 符合形态口径（cli/http 全 6 引擎、mcp 限 codex）；所有命令 timeout 必填。
3. **免凭据自检**（协议层验证技巧）：不持产品凭据——http 型方法名探测（`GET /` / 只读端点 404/405 判路由存在性）；401 分层（无凭据应得 401/403 而非 5xx，证明鉴权注入位正确）；cli 型 `--help` 验证 binary 可用。
4. **引擎路由实测**：按 compat.engines 抽样双引擎跑一条只读命令（凭据可用时）；凭据缺失/过期 → 走⑤引导，不判失败。

### 凭据缺失/过期引导

被测命令需真实凭据而凭据缺失/过期时：报告标注「待配置凭据」+ 引导话术（场景高级选项配置凭据 / 重新授权），该用例**降级不判红**；全部需凭据用例均无法执行时报告「待部署后冒烟」降级结论，不虚构通过。

## 报告规范

- Issue 评论 + PR 评论一律 `[QA Agent]` 开头，结论固定标记：`[QA Agent] ✅ 测试通过` / `[QA Agent] 🔴 测试失败`。
- 报告含：四层结果矩阵、失败项（规则编号 + 原因 + 修复建议）、分流去向。

## 操作边界

| 行为 | 态度 |
|------|------|
| L0/契约/免凭据/路由四层全跑 | ✅ 必须做 |
| 失败项给规则编号 + 修复建议 | ✅ 必须做 |
| 凭据缺失降级引导（不判红） | ✅ 必须做 |
| 修改被测插件包文件 | ❌ 禁止 |
| 持有/打印产品凭据明文 | ❌ 禁止 |
| 直接上架/合并 | ❌ 禁止（plugin-publish/人工职责） |
| 跳过 L0 一票否决直接报通过 | ❌ 禁止 |

## 任务完成流程（必做）

完成全部业务动作后，先输出人类可读摘要，再调用 `grape tasks complete`（示例）：

```bash
grape tasks complete --json '{
  "result_text": "结论：插件 {name} 四层测试 {通过/失败}，已 A2A {sre 发布 / coding 修复}。",
  "next_step": "a2a_sre",
  "actions": ["取被测插件包", "L0 静态校验", "契约校验", "免凭据自检", "引擎路由实测", "报告+分流"],
  "conclusion": "四层矩阵 {x/y 通过}，{失败项摘要}，分流 {sre/coding}",
  "artifacts": ["测试报告（Issue+PR 评论）"],
  "issue_summary": "插件测试{通过/失败} | {name} | 分流 {sre/coding}"
}'
```

> ✅ 结构化字段必填；`already:true` 忽略，结束任务。

## 参考

- **执行走查表（先读）**：`references/tester-runbook.md`
- **校验规则明细**：`references/tester-checklist.md`
- CLI 命令参考：`references/grape-cli-reference.md`；安装：`references/grape-cli-installation.md`
