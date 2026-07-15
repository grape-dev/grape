# Skill Review and Quality

Skill（技能）是 Agent 的能力定义文件，位于仓库 `skills/` 目录下的 `SKILL.md`。它为 Agent 提供特定领域的指令、行为原则、执行流程和约束规则。Skill 可以是 DevOps Bot 自身的技能文件，也可以是 DevOps Bot 所管理仓库（如华为云 Skills 仓库）中的技能定义。

## 通用审查维度（适用于所有 Skill）

### 1. 格式规范
- 文件以 `---` frontmatter 开头
- frontmatter 包含 `name`、`description`、`tags`、`version` 字段
- `name` 与目录名一致
- `description` 包含以下 5 要素：
  1. 功能概要（一句话说明核心功能）
  2. 技术基础（基于什么 CLI/API）
  3. 主要能力（列举关键功能点）
  4. 适用场景（什么情况下使用）
  5. 触发词（用户可能使用的关键词，中英文）

### 2. 完整性
- 正文章节必需包含：概述 → 前置条件 → 工作流 → 核心命令 → 参数说明 → 参考文档
- 包含 `核心原则`（守门不修 bug / 外部 claim 验证 / 读 session 历史 / 禁止探索 Bot 内部）
- 包含强制流程步骤
- 包含 `硬约束` 表
- 包含 `参考` 章节

### 3. 引用路径
- 引用的参考文件（`references/*.md`）路径存在且正确
- API 端点、占位符（`{owner}`、`{repo}`、`{number}`）格式正确
- 所有命令示例可执行，包含所有必需参数

### 4. 向后兼容
- 已有事件类型的处理流程未被破坏
- `version` 已递增（破坏性变更升大版本）
- 无循环引用（不读取自身 SKILL.md）
- 跨会话 Skill 无隐式状态共享（禁止共享内存/全局变量/DB 中间表）

---

## 华为云 Skill 专项审查维度（适用于 `huawei-cloud-*` 前缀的 Skill）

### 5. 命名规范

| 检查项 | 标准 | 示例 |
|--------|------|------|
| 技能包目录 | `huawei-cloud-{product}-{function}` | `huawei-cloud-ecs-diagnosis-workflow` |
| 产品域目录 | 小写英文，对应华为云产品大类 | `computing`, `storage`, `network` |
| 产品子目录 | 小写英文/缩写 | `ecs`, `vpc`, `obs`, `rds` |
| 参考文档 | 小写 kebab-case | `cli-installation-guide.md` |
| 脚本文件 | 小写 kebab-case | `analyze-ingress.sh` |

### 6. 包结构

| 目录/文件 | 必需 | 说明 |
|-----------|------|------|
| `SKILL.md` | ✅ 必需，唯一入口 | YAML Frontmatter + Markdown 正文 |
| `references/` | 推荐 | 参考文档目录 |
| `references/cli-installation-guide.md` | CLI 操作时必需 | CLI 安装、配置、验证 |
| `references/iam-policies.md` | 推荐 | 所需权限列表和策略配置 |
| `references/verification-method.md` | 推荐 | 安装/配置/功能三级验证 |
| `references/acceptance-criteria.md` | 推荐 | 通过/失败标准 |
| `scripts/` | 可选 | 数据预处理、批量操作、结果解析脚本 |
| `templates/` | 可选 | 配置模板、报告模板 |
| `demo/` | 可选 | 演示样例 |

### 7. CLI 命令规范

KooCLI 命令必须遵循的格式：

| 规则 | 标准 | 示例 |
|------|------|------|
| 基本格式 | `hcloud <SERVICE> <Operation> --param=value` | `hcloud ECS ListServersDetails --cli-region=cn-north-4` |
| 服务名 | 大写 | `ECS`, `VPC`, `CES` |
| 操作名 | PascalCase | `ListServersDetails`, `ShowServer` |
| 区域参数 | `--cli-region=<value>` | `--cli-region=cn-north-4` |
| 参数格式 | `--key=value` | `--server_id=xxx` |
| 数组参数 | `--key.1=value1` | `--servers.1.id=xxx` |
| 嵌套对象 | `--key.sub_key=value` | `--remote_console.protocol=vnc` |
| VPC v3 操作 | 操作名带 `/v3` 后缀 | `ListSecurityGroups/v3` |
| IAM 操作 | Keystone 前缀 | `IAM KeystoneListUsers` |

### 8. 安全规范

**凭证安全（MUST）：**
- ❌ 绝不在对话或命令中暴露 AK/SK 值
- ❌ 绝不让用户在对话中直接输入 AK/SK
- ❌ 绝不使用 `hcloud configure set` 传入明文凭据
- ✅ 仅使用 `hcloud configure list` 检查凭据状态
- ✅ 云上环境推荐 IAM 用户而非主账号
- ✅ 敏感操作启用 MFA

**操作安全（MUST）：**
- ✅ 修改操作（安全组规则、重启、删除）前必须用户确认
- ✅ 优先使用只读 API，避免修改资源状态
- ✅ 生产环境操作需风险提示
- ✅ 敏感信息（AK/SK、密码）绝不出现在报告输出中

### 9. 认证与权限

| 检查项 | 标准 |
|--------|------|
| IAM 策略声明 | 在 `references/iam-policies.md` 中列出所有必需 API Action |
| 最小权限原则 | 策略仅包含必需的操作，使用通配符需说明理由 |
| 认证方式 | 明确支持哪种认证方式（AK/SK / KooCLI 配置 / IAM 临时凭证 / ECS IAM 角色） |
| 权限失败处理 | 命令因权限错误失败时，读取 IAM 策略文档并指导用户授权 |

### 10. 发布检查 Checklist

| 序号 | 检查项 |
|------|--------|
| 1 | □ 目录命名符合规范 `huawei-cloud-{product}-{function}` |
| 2 | □ SKILL.md Frontmatter 完整（`name`, `description`, `tags`, `version`） |
| 3 | □ SKILL.md 正文章节齐全（概述→前置条件→工作流→核心命令→参数→参考） |
| 4 | □ `references/cli-installation-guide.md`（如涉及 CLI 操作） |
| 5 | □ `references/iam-policies.md` 声明所需权限 |
| 6 | □ `references/verification-method.md` 定义验证步骤 |
| 7 | □ 命令格式正确（大写服务名 + PascalCase 操作） |
| 8 | □ 无 AK/SK 硬编码 |
| 9 | □ 敏感操作需用户确认 |
| 10 | □ 中文文档，代码块使用英文 |
| 11 | □ 版本号已设置（初始 `1.0.0`） |
| 12 | □ PR 关联至少一个 Issue |
