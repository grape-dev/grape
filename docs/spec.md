# Spec: help 目录独立化（Issue #97 → #96）

## 背景

Grape 帮助文档当前以单一 `src/api/static/help.html`（429 行）承载全部使用说明，而 API 文档散落在 `docs/openapi/`（13 个 markdown）。Issue #96 Spec v2 要求将系统帮助文档独立到 `help/` 目录，按章节拆分 markdown，便于维护与复用。

## 目标

1. `help/usage/`：将 `help.html` 的 9 个章节拆分为 9 个 markdown 文件，内容与原 HTML 章节一致，无信息丢失、无残留 HTML 标签。
2. `help/api/`：从 `docs/openapi/` 原样复制 13 个文件（`00-auth.md` ~ `12-a2a.md` + `README.md`）。
3. 原 `help.html` 保留，在顶部 `<head>` 后添加迁移说明注释。

## 文件清单

### help/usage/（9 个）

| 文件 | 对应 HTML 章节 id |
|------|------------------|
| overview.md | `#overview` |
| quickstart.md | `#quickstart` |
| agents.md | `#agents` |
| pipeline.md | `#pipeline` |
| skills.md | `#skills` |
| runtimes.md | `#runtimes` |
| monitoring.md | `#observability` |
| knowledge-base.md | `#kb` |
| faq.md | `#faq` |

### help/api/（13 个，原样复制）

`00-auth.md`, `01-tasks.md`, `02-repos.md`, `03-agents.md`, `04-chat.md`, `05-skills.md`, `06-scenes.md`, `07-knowledge.md`, `08-events.md`, `09-task-logs.md`, `10-git-repos.md`, `12-a2a.md`, `README.md`

## 转换规则

- HTML → Markdown：标签语义映射（h1→#, h2→##, h3→###, ul→-, ol→1., pre/code→```, table→markdown table, b/strong→**，card.tip→> 💡, card.warn→> ⚠️, card.danger→> 🚨）。
- 去除所有 HTML 标签与内联样式（`onclick`, `style`, `class` 等）。
- FAQ 的 `faq-q`（点击展开）转为 `### Q0: ...`，`faq-a` 转为正文。
- 保留全部文本内容与代码示例，不增删信息。

## 验收标准（#96）

- 项目根目录存在 `help/` 目录，含 `usage/` 与 `api/` 两个二级子目录。
- `help/usage/` 下 9 个 markdown 与 `help.html` 对应章节一致，无信息丢失、无残留 HTML 标签。
- `help/api/` 下 13 个文件与 `docs/openapi/` 源文件一致。
- 原 `help.html` 保留并含迁移注释。
- 文件命名英文小写+连字符。
- 产出 PR（关联 Issue #97），目标分支 `weekly-02`。

## 影响范围

- 新增 `help/` 目录（纯文档，无代码逻辑）。
- 修改 `src/api/static/help.html`（仅添加注释，不改内容）。
- 无外部依赖变更，无架构影响。
