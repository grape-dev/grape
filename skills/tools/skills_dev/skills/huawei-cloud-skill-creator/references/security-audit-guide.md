# Security Audit Guide — Phase 6 安全审视

> Phase 6 由 Agent 编排四项安全与质量检查；不得直接调用其他具名 Skill 或其目录下的脚本。

## 四项检查概览

| # | 工具 | 检查内容 | 安装 |
|---|------|----------|------|
| 1 | skillcheck | SKILL.md agentskills.io 规范（frontmatter、description 质量、引用安全） | `pip install skillcheck` |
| 2 | markdownlint-cli2 | Markdown 风格一致性（行长度、标题、代码块） | `npm install -g markdownlint-cli2` |
| 3 | hwcloud-spec | 华为云规范：frontmatter 字段、章节结构、文件大小 | 内置 |
| 4 | gitleaks | 凭证泄露：800+ 种 API 密钥、密码、私钥、token 格式 | [GitHub Releases](https://github.com/gitleaks/gitleaks/releases) |

## 执行方式

由 Agent 逐项执行下列工具，汇总结果并生成 `skill-gate-report-<timestamp>.txt`。如果某项工具不可用，应报告缺失依赖，不得将该项标记为通过。

## 修复策略

### skillcheck

| 规则 | 修复 |
|------|------|
| description.quality-score | 以动词开头（Generates/Analyzes/Validates），添加触发上下文 |
| disclosure.metadata-budget | 将非必要 frontmatter 字段移至正文 |
| disclosure.body-bloat | 将大表格（>20行）移至 references/ 文件 |
| frontmatter.field.unknown | 在 skillcheck.toml 的 extension_fields 中添加，或移除 |
| references.escape | 将 `../other-skill/SKILL.md` 替换为纯文本 `[text (other-skill-name)]` |

### markdownlint-cli2

```bash
# 自动修复（处理 MD022/MD031/MD032/MD047/MD012 等）
markdownlint-cli2 "{skill-path}/**/*.md" --config "{skill-path}/.markdownlint.json" --fix

# 手动修复不可自动修复项
# MD036: 将 **伪标题** 替换为 ### 真标题
# MD040: 将裸 ``` 替换为 ```bash 或 ```text
```

### hwcloud-spec

| 规则 | 修复 |
|------|------|
| frontmatter.missing.{field} | 补充必需字段：name/description/tags |
| frontmatter.version-format / version | 移除 version 字段；华为云 Skill 规范 v1.0 不使用该字段 |
| frontmatter.tags-too-many | 精简标签至 ≤5 个 |
| section.missing-required | 补充必需章节：概述/前置条件/工作流/核心命令/参数确认/参考文档 |
| section.missing-recommended | 补充推荐章节：最佳实践/注意事项/验证方法 |
| size.skill-md-lines | SKILL.md 超 500 行时拆分至 references/ |
| size.dir-too-large | 目录超 40MB 时拆分大文件 |

### gitleaks

| 规则 | 修复 |
|------|------|
| generic-api-key | 替换为 `os.environ.get("VAR")`，误报加入 `.gitleaksignore` |
| private-key | 移除硬编码私钥，运行时从文件或密钥管理器加载 |

## 审视结果处理

| 结果 | 处理 |
|------|------|
| PASS（0 issues） | 安全审视通过 |
| PASS（仅 WARNING） | 提示用户确认是否接受 |
| FAIL（ERROR/CRITICAL） | **必须修复后重新审视，不可跳过** |
