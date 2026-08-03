# 工作流审查：Agent Work Loop 五维评估模型

评估**仓库的 AI 编码工作流（Harness）**，而不是只审 diff。本模型基于 Agent Work Loop（前馈指引 + 反馈传感器）设计，回答"编码这件事是如何被组织、被验证、被沉淀的"。

## 证据纪律（先读）

- 每个结论必须绑定证据，标注 **已观察（observed）** 或 **未观察（unobserved）**。
- **未观察 ≠ 没有**。只标注"未获得证据"，不用推断补齐、不虚构、不评分。
- **没有证据就不评分**。维度评分只在证据充分时给出；证据不足的维度输出 `unobserved` + 原因。
- 只通过 GitCode API 获取仓库文件；会话历史 `data/workspaces/history/{account}/{repository}/session_history.json` 用 read 读取。

## 证据来源

| 来源 | 获取方式 | 支撑维度 |
|------|---------|---------|
| `AGENTS.md` / `CLAUDE.md` / `rules` | GitCode API `GET /repos/{owner}/{repo}/contents/AGENTS.md` | 任务理解、受控执行 |
| `spec` / `DESIGN.md` / 需求文档 | GitCode API | 任务理解 |
| `skills/` 目录 | GitCode API 遍历目录 | 受控执行、经验沉淀 |
| `tests/`、测试配置 | GitCode API | 变更验证 |
| CI 配置（`.github/workflows`、`.gitlab-ci.yml`、`Jenkinsfile`） | GitCode API | 变更验证、可靠交付 |
| Hook / 分支保护 / 审查要求 | GitCode API（PR 审查记录、合并记录） | 可靠交付 |
| 构建脚本、`package.json` / `requirements.txt` | GitCode API | 受控执行、变更验证 |
| 会话历史 `session_history.json` | read 工具 | 任务理解、受控执行、经验沉淀 |

## 五个维度

### 1. 任务理解（Task Understanding）

> 是否知道目标，以及"完成"的定义？

| 检查问题 | 证据 |
|---------|------|
| 仓库是否有 `AGENTS.md` 或等效的 Agent 指引？ | AGENTS.md 是否存在 |
| 任务是否有 spec / 需求文档 / 验收标准？ | spec、DESIGN.md、Issue 描述 |
| "完成"是否有可验证的标准（验收条件、测试要求）？ | PR 描述、任务定义 |
| Agent 执行是否基于明确目标而非猜测？ | session_history 中的任务定义 |

**发现样例**：无 AGENTS.md / 任务无验收标准 → Agent 容易"信心十足地解决错误的问题"。

### 2. 受控执行（Controlled Execution）

> 工作是否沿受支持、可复现的路径进行？

| 检查问题 | 证据 |
|---------|------|
| 是否有可复用的 Skill / 命令 / 工作流？ | `skills/` 目录、CI 标准命令 |
| 执行步骤是否可复现（而非一次性手工步骤）？ | session_history、构建脚本 |
| 是否有沙箱 / 权限边界控制 Agent 行为？ | 环境配置、工具配置 |
| 是否使用标准工具链？ | 依赖清单、构建脚本 |

**发现样例**：重复手工步骤 / 无标准化命令 / 一次性脚本 → 工作沿他人无法复现的路径推进。

### 3. 变更验证（Change Validation）

> 是否有证据证明变更真的有效？

| 检查问题 | 证据 |
|---------|------|
| 变更是否附带测试？ | `tests/`、PR 变更文件 |
| 测试是否真正覆盖变更行为（而非只为通过）？ | 测试用例与变更对照 |
| CI 是否运行并提供结果？ | CI 状态、流水线记录 |
| lint / 构建 / 静态检查是否通过？ | CI 日志、PR 检查 |
| PR 中"测试通过"的声明是否有证据？ | **claim 需核对原对象** |

**发现样例**：PR 无测试且 CI 未运行 / "tests pass" 声明无证据 → 只有"能运行"，没有验证。

### 4. 可靠交付（Reliable Delivery）

> AI 的速度是否绕过了质量检查或验收？

| 检查问题 | 证据 |
|---------|------|
| 是否有合并前审查要求？ | 分支保护、审查记录 |
| 是否有 CI 门禁（不通过不能合并）？ | CI 配置 |
| 是否有人工审批环节？ | 审批记录、PR 状态 |
| 是否有回滚 / 恢复路径？ | 部署文档、回滚脚本 |
| 是否存在绕过审查直接合并的记录？ | 合并历史 |

**发现样例**：无审查要求 / 无 CI 门禁 / 跳过验收 → 速度压过保障措施。

### 5. 经验沉淀（Learning Capture）

> 下一个任务能否从本次任务受益？

| 检查问题 | 证据 |
|---------|------|
| 是否有可复用的 Skill / 模板 / 文档沉淀？ | `skills/` 新增记录、docs |
| 本次修复是否沉淀为可复用资产？ | session_history、CHANGELOG、skill |
| 同样的问题是否反复出现？ | session_history 中重复模式 |
| 是否有跨会话的记忆 / 上下文复用？ | 历史文件、会话延续 |

**发现样例**：同样问题反复出现 / 修复无沉淀 → 经验不沉淀，下个任务重蹈覆辙。

## 评分边界

- 仅当该维度证据充分时给出 **0-100** 区间标签：

| 区间 | 标签 | 含义 |
|------|------|------|
| 0–1 | Bootstrap | 关键基础机制缺失（无 AGENTS.md、无验证路径） |
| 1–60 | Operationalize | 机制存在但未接入日常工作 |
| 60–100 | Optimize | 有可比较的 Task Episodes 证据，闭环已运转 |

- 证据不足 → `unobserved`，不评分，不把"未观察到"当成"差"。
- 分数仅用于跨报告趋势对比，不单独作为审查结论。

## 输出

按 `references/findings-format.md` 的契约输出发现。每条发现归属一个主要维度，包含影响、预期输出、最小修复范围与验收检查。
