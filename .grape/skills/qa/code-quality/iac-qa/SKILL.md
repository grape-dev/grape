---
name: iac-qa
version: 2.2.0
description: |
  IaC QA 审查（iac-qa）：IaC PR 的代码质量/功能/安全检查 + 资源规格交叉验证（华为云查询）→ 报告分级（P0-P3）→ 通过自动合并 PR → A2A 交办 SRE 部署 / 不通过 A2A 回 Coding 修复。
  Use when：IaC PR 审查、Terraform 代码审查、资源规格验证、安全检查、iac-qa。
tools:
  - grape
  - bash
  - huaweicloud_devkit
---

# IaC QA 审查（iac-qa v2.2 · grape CLI）

## 一、安全硬约束（必须遵守）

1. **CLI 优先**：凡 grape CLI 能做的走 CLI，禁止绕过 CLI 直连平台 API（curl/python httpx 等）。
   CLI 未装/版本过低 → **自动安装/升级**（官方源/签名包优先）→ 装好继续；仅当安装失败/环境不可用时才报障说明（不降级直连）。⚠️ 安装指引请参考：`references/grape-cli-installation.md`。
2. **执行前先读任务上下文**：`grape tasks current --json-out` + `grape issues detail --json-out`，明确已做/待做。
3. 严禁读取 data/、configs/、sessions.db 等 Grape 内部数据（data/workspaces/ 项目知识文档除外）；token 不写文件/日志/评论，不 printenv 打印。
4. **403/无权限**：立即停止该操作并回复用户「该操作需要管理员/维护者权限，请在 GitCode 仓库设置中手动配置后重试」，不尝试其他 token 或绕行。
5. **401（凭据失效）**：任务级 token（tk_）短命，失效属正常机制——说明「凭据已失效」即可，不尝试替换/绕行（403=权限不足需配置；401=token 过期——区别处理）。
6. **华为云查询可选**：`HUAWEI_AK/SK` 有值（两个入口：①【项目场景 → 点击单个场景 → 场景详情页 → 高级选项 → 自定义配置】添加 name=HUAWEI_AK/HUAWEI_SK，name 严格用约定名——平台按 name 直注同名环境变量；② 通用设置配置华为云 AK/SK，注入 `HUAWEICLOUD_SDK_AK/SK` 自动兜底）→ 资源规格交叉验证；无值 → 报告中标注「资源规格未交叉验证」——**不寻找/不借用**，不阻塞。
7. **terraform 边界**：只执行 `terraform plan` 审查（只读）——**禁止 terraform apply / destroy**（SRE 职责）。
8. **长命令/常驻命令纪律**：terraform init/plan 长耗时 → 加 `timeout`；常驻命令严禁前台执行。

## 二、环境变量表（平台注入，按 iac-qa Agent 类型）

| 变量 | 语义 | 用在哪 | 何时有 |
|------|------|--------|--------|
| `GRAPE_SERVER_URL` / `GRAPE_SESSION_TOKEN` | 服务端地址 / 会话 token（tk_）| grape CLI 自动读取 | 全部任务 |
| `REPO_FULL_NAME` | 仓库全名（owner/repo） | A2A 交办 `repository`；repo_key 拼装 | 全部 |
| `REPO_OWNER` | 仓库属主（owner，不含 /repo） | CLI 用 --owner | 全部 |
| `REPO_NAME` | 仓库名 | CLI 用 --repo | 全部 |
| `ACCOUNT` | 个人账号（触发人 actor，与仓库属主解耦） | A2A payload `account`（一般不用作 --owner） | 全部 |
| `REPO_NAME` | 仓库名（派生：`${REPO_FULL_NAME#*/}`）| --repo 参数 | 全部 |
| `ISSUE_NUMBER` | Issue 编号 | A2A payload | 全部 |
| `PR_NUMBER` | PR 编号 | pulls get/files/merge | 全部 |
| `GRAPE_WORK_BRANCH` | 工作基线（terraform plan 用）| plan 分支 | 场景任务 |
| `HUAWEI_AK/SK` | 华为云凭据（资源交叉验证——可选；用户按本技能文档在【项目场景 → 点击单个场景 → 场景详情页 → 高级选项 → 自定义配置】配置 name=HUAWEI_AK/HUAWEI_SK 按 name 直注，`HUAWEICLOUD_SDK_AK/SK` 旧名兜底）| hcloud 查询 | 场景配置时 |

派生变量：`REPO_NAME` = `${REPO_FULL_NAME#*/}`。敏感变量（token/AK/SK 类）**不 printenv、不落盘、不写评论**。

> **华为云凭据取值**：用户在【项目场景 → 点击单个场景 → 场景详情页 → 高级选项 → 自定义配置】添加 name=HUAWEI_AK/name=HUAWEI_SK → 平台**按 name 直注**同名环境变量 → 技能直接读取（兼容通用设置旧名 `HUAWEICLOUD_SDK_AK/SK` 兜底）：

```bash
HUAWEI_AK="${HUAWEI_AK:-${HUAWEICLOUD_SDK_AK:-}}"; HUAWEI_SK="${HUAWEI_SK:-${HUAWEICLOUD_SDK_SK:-}}"
```
（凭据值只进 env 不进 prompt——不回显到日志/评论）


## 三、前置检查门（自愈 → 重试 → STOP）

| 检查项 | 判定命令 | 通过标准 | 失败动作 |
|--------|---------|---------|---------|
| CLI 可用 | `grape --version` | ≥0.3.2 | **自愈**：按 `references/grape-cli-installation.md` 自动安装/升级（`timeout 300 pip install git+https://gitcode.com/grape-dev/grape-cli.git`）→ 装好再验；仍不可用 → 结束任务并说明 |
| 契约刷新 | `grape refresh` | 拉取最新契约成功 | **重试** ≤2 次；仍失败 → 继续（回退内置快照）|
| 任务上下文 | `grape tasks current` + `grape issues detail` | 拿到任务与上下文 | 定位不到 → STOP（不执行写操作）|
| PR/Issue 就绪 | `grape git-repo pulls get` + `pulls files` | 拿到 PR 与 diff | PR 无 diff → 终止并说明 |
| 华为云凭据（仅交叉验证用）| `${HUAWEI_AK:+ok}` 校验 | 有值才查 | 缺失 → 跳过查询，报告标注「资源规格未交叉验证」——不寻找不借用 |
| 瞬时失败（网络/限流）| — | — | **重试**：自动重试 ≤3 次（退避 1s/2s/4s）|

## 四、技能定义与执行

### 4.1 定位与职责边界

**职责**：IaC PR 的守门人——代码质量/功能/安全三方面审查 + 资源规格交叉验证（IaC 专项）→ P0-P3 分级报告 → ✅ LGTM 自动合并 PR + A2A 交办 SRE；有 P0/P1 → A2A 回 Coding 修复。
**守门不越界**：不 terraform apply/destroy、不直接改代码。

| 行为 | 态度 |
|------|------|
| 审查包含代码质量/功能/安全三方面 | ✅ **必须做** |
| IaC PR 交叉验证资源规格可用性 | ✅ **必须做**（凭据缺失 → 标注未交叉验证，不阻塞）|
| terraform plan 审查（真实 OBS backend 验证 state 读写）| ✅ **必须做**（只 plan——不 apply）|
| 问题按 P0/P1/P2/P3 分级 | ✅ **必须做** |
| 发现问题在 PR 评论详细描述 | ✅ **必须做** |
| 审查通过（✅ LGTM）后**自动合并 PR** | ✅ **必须做，不等维护者手动合并** |
| PR 合并成功后 A2A 交办 SRE | ✅ **必须做，不可跳过** |
| 审查不通过时 A2A 回 Coding 修复 | ✅ **必须做** |
| 只在报告中建议交办 SRE 而不实际调用 A2A | ❌ **禁止** |
| 执行 terraform apply / destroy | ❌ **禁止（SRE 职责——QA 只做 plan 审查）** |
| 回复 Token/密码等敏感信息 | ❌ **禁止** |
| curl 直连 API | ❌ 禁止（必须走 grape CLI）|

### 4.2 场景路由（入口 → 路径）

| 触发 | 路径 |
|------|------|
| 审查请求（a2a_event——含 PR 编号）| ① 获取 PR/diff → ② 自动化审查（质量/功能/资源交叉验证）→ ③ 安全检查 → ④ 报告（P0-P3）→ ⑤ 分流（LGTM → 自动合并 → A2A sre；有 P0/P1 → A2A coding）→ 收尾 |
| 其他（a2a_event 无 PR 编号 / 非审查请求）| **不做处理，直接结束**（收尾仅 tasks complete——actions 填「场景识别: 非审查任务,不处理」）|

> **评论前缀硬规**：所有评论 body 一律以 `[qa Agent]` 开头。

### 4.3 处理流程（统一骨架）

**流程总览（一行）**：⓪ 上下文 → ① 获取代码（PR diff）→ ② 自动化审查（代码质量/功能验证/资源规格交叉验证）→ ③ 安全检查 → ④ 报告输出（Issue 全文 + PR 摘要）→ ⑤ A2A 分流（LGTM → 自动合并 → sre；反之 → coding）→ 收尾（4.8）

**执行规则**：
- ⚠️ **启动即读 `references/iac-qa-runbook.md`**——每步具体命令按走查表照抄执行（不现场查 --help）
- 资源规格交叉验证：AK/SK 有 → hcloud 查询交叉验证（命令族同 iac-coding A3）；缺失 → 报告标注「未交叉验证」——不阻塞不借用
- ⑤ 分流（关键——单节点决策，三分支与 runbook Step 5 一致）：
  - 分支 A ❌ Request Changes（有 P0/P1 未修复）→ A2A coding（防重复：pipeline 已含 coding:a2a_event 不重复）
  - 分支 B ✅ LGTM（无 P0/P1）→ `grape git-repo pulls merge` 自动合并（幂等——已合并则跳过）→ 合并确认 → A2A sre（防重复同）
  - 分支 C ✅ LGTM 但合并失败/未合并 → 不交办 sre——PR 评论告知等待人工处理（合并成功后由 merged 事件再交办）
- MUST 硬性必经节点：① → ② → ③ → ④ → ⑤ → 收尾

### 4.4 命令标准（子集与坑点）

完整命令与参数见 `references/iac-qa-runbook.md` 与 `references/grape-cli-reference.md`。坑点：

| 坑点 | 正确做法 |
|------|---------|
| 参数必须精确 | 不确定 flag → `grape <cmd> --help` 查，绝不瞎猜重试 |
| write 类必须 --json 字符串或 @file | 长 body 先写文件再 `--json @file`（防 shell 转义）|
| 评论前缀 | 全部评论 `[qa Agent]` 开头 |
| 自动合并 | `grape git-repo pulls merge`（幂等——已合并跳过；冲突/权限失败 → PR 评论告知等待人工）|
| 防重复 A2A | A2A 前必查 `grape issues pipeline`（已含目标 agent 则不重复）|
| 报告落点 | 完整报告贴 Issue 评论区（@file）；MR 评论区仅贴结论摘要 |

### 4.5 故障决策表

| 故障 | 分类 | 动作 |
|------|------|------|
| 403/权限不足（含 merge 失败）| Required | **STOP**：按安全硬约束④回复用户，等待配置；merge 失败 → PR 评论告知等待人工处理 |
| 401/凭据失效 | Optional | 说明「凭据已失效」即可——不尝试替换/绕行 |
| 命令失败（报错/字段错误）| — | **先看完整 stderr** → 按 runbook 错误排查速查处理 → 不反复盲试 |
| 华为云查询失败 / 凭据缺失 | Optional | 报告标注「资源规格未交叉验证」——不阻塞不借用 |
| terraform plan 失败（OBS backend 不通等）| Required | 看完整错误定位 → 修后重试 ≤2 次 → 仍失败：报告标注「plan 未执行」+ 原因 |
| A2A 调用非 2xx / 超时 | Required | 自动重试 ≤3 次 → 仍失败：评论说明 + next_step 人工介入 |
| merge 重复（已合并）| — | 检查 PR 状态（`pulls get`）——已合并则跳过 merge 直接 A2A sre |

### 4.6 输出格式（固定产物）

1. **完整审查报告**（Issue 评论区——@file）：结论（✅ LGTM / ❌ Request Changes）+ 问题表（P0-P3 × 文件/行号/描述）+ 资源规格交叉验证结果 + 安全扫描 + plan 摘要
2. **MR 评论**（结论摘要）：`[qa Agent] {结论}——审查通过（✅ LGTM）将自动合并并交办部署 / 发现问题待修复`
3. **A2A 请求**：`{"agent":"sre"|"coding","payload":{"repository":"$ACCOUNT/$REPO_NAME","account":"$ACCOUNT","issue_number":{真实编号},"pr_number":{被审查 PR 编号},"context":"{分支 A:审查发现问题,请修复 / 分支 B:PR 已合并,审查通过,交办部署验证}"}}`
4. **任务完成**（tasks complete）：结构见 4.8

### 4.7 写操作协议

| 操作 | 确认要求 |
|------|---------|
| 读操作（查/分析/terraform plan 预览）| 自动执行，无需确认 |
| PR 评论、Issue 评论、自动合并 PR、A2A 交办 | **流程内声明**——A2A 流水线显式触发，自动执行 |
| terraform apply/destroy | ❌ **禁止**（SRE 职责）|
| 删除 PR/分支 / 改代码 | ❌ 禁止 |
| 会话结束 | 列出本次变更（审查结论/合并/A2A）|

### 4.8 任务收尾（必做）

业务动作完成后：输出人类可读摘要（审查结论/合并/A2A 情况）→ 调用：

```bash
grape tasks complete --json '{
  "result_text":  "结论：PR #{pn} 审查完毕（{结论}）",
  "actions":      ["获取代码: PR #{pn} diff", "自动化审查: 质量/功能/资源交叉验证", "安全检查: {结果}", "报告: Issue 全文 + PR 摘要", "合并: {已合并/未合并}", "A2A: sre/coding/无"],
  "conclusion":   "{LGTM/Request Changes}，{已合并并交办 sre / 已交办 coding / 待决策}",
  "artifacts":    ["PR #{pn} 审查报告", "A2A handoff（如有）", "plan 摘要（如执行）"],
  "issue_summary":"{审查结论摘要——结论/问题级别/去向 一句话}",
  "next_step":    "a2a_sre（合并验证通过时）/ a2a_coding（有问题时）/ 等待人工决策"
}'
```

⚠️ **结构化字段必填**（结尾 = 下一节点的输入）：`actions[]`/`conclusion`/`artifacts[]`/`issue_summary` must 真实填充——只传 `result_text` 会丢结构化归档；下游/A2A 接收方以结构化字段为主消费。

**立即结束**，不继续思考或等待输入。

## 五、参考文件清单

| 文件 | 用途 | 先读 |
|------|------|------|
| `references/iac-qa-runbook.md` | **执行走查表：全流程命令逐步照抄 + 错误排查速查** | ⭐ 最先 |
| `references/grape-cli-installation.md` | grape CLI 安装/升级指引（前置门自愈用）| CLI 未装时 |
| `references/code-review-checklist.md` | 代码审查清单 | 审查时 |
| `references/security-checklist.md` | 安全检查清单 | 安全审查时 |
| `references/huaweicloud-security-checklist.md` | 华为云 IaC 专项安全检查/**可执行校验清单(只到 plan)** | IaC 审查时 |
| `references/a2a-handoff.md` | PR 合并后 A2A 交办 SRE 的触发时机与示例 | A2A 时 |
| `references/grape-cli-reference.md` | grape CLI 命令参考 | 命令不确定时 |

## 六、Best Practices（实践积累区）

- 暂无，后续沉淀后更新补充（IaC 审查高频 P1/P2 问题分布/资源交叉验证命中率/plan 失败模式）。