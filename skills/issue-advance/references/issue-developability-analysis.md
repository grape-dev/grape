# 深度可开发性分析指南

## 执行前提

- 项目知识基线已建立（`data/workspaces/{owner}/{repo}/project-info/` 下文档已存在）
- 当前事件为 `issue_opened` 或 `issue_updated`
- Issue 尚未被处理过（无标签且无负责人），或用户补充了信息需要重新分析

## 前置步骤：读取项目知识

```bash
cat data/workspaces/{owner}/{repo}/project-info/project-graph.md
cat data/workspaces/{owner}/{repo}/project-info/project-capabilities.md
```

如果文档不存在，先执行 `references/codegraph-project-analysis.md` 生成。

## 四维分析（按 1→2→3→4 顺序执行，每步必须输出结论）

### 维度 1: 需求完整性

**目标**：判断 Issue 描述的需求是否足够清晰、完整，能否作为开发输入。

1. **提取功能点** — 从 Issue 标题和正文中提取所有明确的功能需求
2. **识别需求类型**：

| 类型 | 判定线索 |
|------|---------|
| Bug | 标题含"bug/故障/报错/错误/异常/不生效/白屏/500"或正文含堆栈 |
| Feature | 标题含"新增/支持/实现/希望/能不能/需求" |
| Enhancement | 标题含"优化/改进/提升/增强/重构" |
| Refactor | 标题含"重构/重写/迁移/替换" |
| Documentation | 标题含"文档/README/指南/说明" |

3. **按类型检查完整性**：

**Bug 类型**：

| 检查项 | 权重 | 通过条件 |
|--------|------|---------|
| 复现步骤 | 高 | 提供了完整的复现步骤 |
| 错误信息 | 高 | 有错误日志、截图或堆栈信息 |
| 环境信息 | 中 | 说明了操作系统、版本等 |
| 预期 vs 实际 | 高 | 说明了预期行为和实际行为 |

**Feature 类型**：

| 检查项 | 权重 | 通过条件 |
|--------|------|---------|
| 需求描述 | 高 | 用户想要什么功能？描述清晰度 |
| 使用场景 | 高 | 说明了使用场景和用户故事 |
| 验收标准 | 高 | 有可衡量的完成标准 |
| 边界条件 | 中 | 无模糊表述（如"优化"、"更好"） |

**Enhancement/Refactor 类型**：

| 检查项 | 权重 | 通过条件 |
|--------|------|---------|
| 当前问题 | 高 | 说明了当前方案的不足 |
| 改进方向 | 高 | 明确了改进的目标和方向 |
| 影响范围 | 中 | 说明了涉及的模块和影响 |

4. **输出完整性评级**：

| 评级 | 条件 |
|------|------|
| `complete` | 所有权重为"高"的检查项全部通过 |
| `partial` | 部分高权重检查项通过，但有足够信息推断缺失项 |
| `vague` | 高权重检查项大部分未通过，无法推断 |

### 维度 2: 技术可行性

**目标**：基于项目架构，判断该需求在技术上是否可实现。

1. **定位涉及模块** — 对照 `project-graph.md` 的 Module Dependency Graph，确定需求涉及的模块
2. **检查模块间依赖** — 涉及的模块之间是否存在循环依赖或强耦合？
3. **识别技术阻碍**：

| 阻碍类型 | 说明 | 判定方式 |
|---------|------|---------|
| 架构冲突 | 需求与现有架构不兼容 | 对照 Architecture Overview |
| 技术限制 | 使用的技术栈不支持该特性 | 对照 Tech Stack |
| 权限不足 | 需要的 API/服务当前无权限 | 对照 External Integrations |
| 性能瓶颈 | 实现后可能导致性能问题 | 基于模块复杂度评估 |

4. **输出可行性评级**：

| 评级 | 条件 |
|------|------|
| `feasible` | 无技术阻碍，涉及模块清晰 |
| `conditional` | 有可解决的技术条件（需额外配置/依赖） |
| `infeasible` | 存在不可逾越的技术阻碍 |

### 维度 3: 依赖就绪性

**目标**：判断实现该需求所需的外部依赖是否已就绪。

1. **识别所需依赖** — 从需求描述中提取涉及的外部服务/API/库
2. **对照已有集成** — 与 `project-capabilities.md` 的 External Integrations 对比
3. **分类依赖状态**：

| 状态 | 说明 |
|------|------|
| `available` | 已集成，可直接使用 |
| `integrable` | 未集成但 API 文档可获取，集成路径清晰 |
| `blocked` | 未集成且 API 文档不可获取，或需要额外授权 |

4. **输出就绪性评级**：

| 评级 | 条件 |
|------|------|
| `ready` | 所有依赖 available |
| `partial` | 部分依赖 integrable，无 blocked |
| `missing` | 存在 blocked 依赖 |

### 维度 4: 范围确定性

**目标**：判断改动范围是否可预估，是否涉及架构变更。

1. **预估涉及文件** — 对照 `project-graph.md` 的 Key Files，列出可能需要改动的文件
2. **判断架构影响**：

| 影响级别 | 条件 |
|---------|------|
| `none` | 仅修改现有模块内部逻辑 |
| `moderate` | 需要修改模块间接口或新增模块 |
| `significant` | 涉及核心架构变更或新增外部服务集成 |

3. **预估复杂度**：

| 级别 | 条件 |
|------|------|
| `simple` | 单文件改动，不涉及外部依赖 |
| `medium` | 多文件改动，涉及已有模块接口 |
| `large` | 跨模块改动、架构调整、新增外部服务集成 |

4. **输出范围评级**：

| 评级 | 条件 |
|------|------|
| `clear` | 涉及文件明确，架构影响 none，复杂度 simple/medium |
| `estimated` | 涉及文件可推断，架构影响 moderate |
| `unclear` | 涉及文件不确定，或架构影响 significant |

## 可开发性评级

### 评级规则

```
IF 需求完整性 == "complete"
   AND 技术可行性 == "feasible"
   AND 依赖就绪性 == "ready"
   AND 范围确定性 == "clear"
→ ready

ELSE IF 技术可行性 == "infeasible"
   OR 需求与项目范围完全无关
→ not-feasible

ELSE IF 不通过维度数 >= 3
   OR 任一维度有 major 缺失
→ needs-discussion

ELSE (不通过维度数 <= 2 且均为 minor 缺失)
→ almost-ready
```

**评级保守原则**：宁可多问一句，不贸然标 `ready`。当评级在 `ready` 和 `almost-ready` 之间犹豫时，选择 `almost-ready`。

### 评级与动作

| 评级 | 负责人分配 | 动作 | 后续 |
|------|-----------|------|------|
| `ready` | **分配给机器人** | 生成 Spec → 贴到 Issue → A2A 通知 coding agent | 等待 coding agent 开始开发 |
| `almost-ready` | **分配给项目责任人**（CODEOWNERS） | 针对不明确维度精确追问 1-2 个问题 | 等待责任人补充信息后重新分析 |
| `needs-discussion` | **分配给项目责任人**（CODEOWNERS） | 逐维度列出缺失项 → 逐一追问 → 标记 `need-info` | 等待责任人补充信息后重新分析 |
| `not-feasible` | 不分配 | 评论说明不可行原因 → 标记 `invalid` | 结束处理 |

### 负责人分配规则

| 评级 | 负责人 | 理由 |
|------|--------|------|
| `ready` | 机器人（Bot 账号） | 需求已充分明确，机器人接管后续开发流程（Spec → A2A → Coding Agent） |
| `almost-ready` | 项目责任人（CODEOWNERS） | 需要项目相关负责人补充信息后才能进入开发 |
| `needs-discussion` | 项目责任人（CODEOWNERS） | 需要项目相关负责人深入讨论后才能确定方向 |
| `not-feasible` | 不分配 | Issue 不可行，无需跟踪 |

**CODEOWNERS 匹配**：按 `references/assignee-guide.md` 的现有逻辑，基于 Issue 内容匹配 CODEOWNERS 中的模块负责人。

**机器人分配**：使用 Bot 的 GitCode 用户名（从 `AGENT_NAME` 环境变量或 Bot 配置获取）作为 assignee。

## 追问策略

### almost-ready 追问

针对不明确的维度，生成 1-2 个精确问题：

```
[$AGENT_NAME] 分析了该 Issue，需求基本清晰，但有以下问题需要确认：

1. **验收标准**：Issue 中提到"优化响应速度"，请明确具体的性能指标（如：P99 延迟 < 200ms？）
2. **边界条件**：是否需要考虑并发场景？如果需要，预期的并发量级是多少？

确认后即可生成开发规格文档并启动实现。
```

### needs-discussion 追问

逐维度列出缺失项，每个维度 1-3 个问题：

```
[$AGENT_NAME] 对该 Issue 进行了深度可开发性分析，当前评级：needs-discussion

### 需求完整性: partial
- 功能点基本明确，但缺少验收标准
- ❓ 请补充：该功能完成后，如何验证其正确性？有哪些测试场景？

### 技术可行性: conditional
- 需要调用外部 API，但该 API 的认证方式未说明
- ❓ 该 API 使用哪种认证方式（OAuth / API Key / Token）？是否已有可用凭证？

### 依赖就绪性: partial
- 项目已集成飞书 API，但未集成目标通知渠道
- ❓ 通知目标是什么渠道（飞书群 / 邮件 / Webhook）？

请逐一回复以上问题，确认后将重新评估可开发性。
```

### not-feasible 说明

```
[$AGENT_NAME] 该 Issue 经分析判定为不可行，原因：

**技术阻碍**：该需求要求实现 X 功能，但当前项目使用的 Y 框架不支持此特性。

**建议**：
1. 考虑使用 Z 方案作为替代
2. 或升级 Y 框架至支持该特性的版本

如有疑问请回复讨论。
```

## 分析结论输出格式

```markdown
## 可开发性分析结论

**Issue**: #{number} — {title}
**评级**: {ready|almost-ready|needs-discussion|not-feasible}
**分析时间**: {datetime}

### 需求完整性: {complete|partial|vague}
- 类型: {Bug|Feature|Enhancement|Refactor|Documentation}
- 功能点: {明确|部分明确|模糊}
- 边界条件: {清晰|部分清晰|缺失}
- 验收标准: {存在|部分存在|缺失}

### 技术可行性: {feasible|conditional|infeasible}
- 涉及模块: {模块列表}
- 技术阻碍: {无|具体描述}

### 依赖就绪性: {ready|partial|missing}
- 已有依赖: {列表}
- 缺失依赖: {列表}

### 范围确定性: {clear|estimated|unclear}
- 预估改动: {文件/模块列表}
- 架构影响: {none|moderate|significant}
- 复杂度: {simple|medium|large}

### 追问项
1. {问题}
2. {问题}
```

## 与原有效性检查的关系

| 原检查项 | 对应维度 | 增强点 |
|---------|---------|--------|
| 是否与仓库主题相关 | 技术可行性（前置） | 结合 project-graph 判断，不再仅凭 README |
| 内容质量分析 | 需求完整性 | 按类型细化检查项，增加权重 |
| 是否已实现 | 技术可行性（子步骤） | 结合 codegraph 分析 commit 内容 |
| 是否重复 | 需求完整性（子步骤） | 保留原有逻辑 |
