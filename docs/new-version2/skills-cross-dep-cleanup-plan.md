# Skills 跨技能依赖与悬空引用清理方案

> 版本：v0.2（方案稿，未改代码）
> 日期：2026-08-10（v0.2 按评审确认 A2：`skills/agent/*` 转为 `implementation/references/*.md`，agent 不作为技能存在）
> 目的：清理 `skills/` 下所有技能的跨技能依赖与悬空引用，使每个技能自持、消除"引用其它技能文件"的情况。

---

## 一、扫描结论

### A. 真实跨技能依赖（2 处）

| # | 来源 | 引用的其它技能文件 | 类型 |
|---|------|--------------------|------|
| A1 | `issue-advance/references/spec-generation.md:222` | `references/new-dev-flow.md`（该文件只在 `implementation/references/`） | 相对路径在 issue-advance 下悬空（实际指向 implementation） |
| A2 | `implementation/references/new-dev-flow.md` | `skills/agent/*` 5 个子技能：`spec-driven-development` / `planning-and-task-breakdown` / `test-driven-development` / `incremental-implementation` / `code-review-and-quality` | implementation 依赖其它技能目录 |

### B. 悬空"See Also"引用

- `pr` / `qa` / `skill-pr` 的 `references/code-review-and-quality.md` "See Also"（:351-352）引用 `references/security-checklist.md`、`references/performance-checklist.md` —— **全仓不存在**（3 文件 × 2 行 = 6 行）。

### C. 悬空示例

- `skill-pr/SKILL.md:74` 与 `:82` 示例 `skills/tokauthai/skillscan` → `tokauthai/skillscan`（技能 `tokauthai` 不存在）。

### 非问题项（确认不改）

| 项 | 说明 |
|----|------|
| `pr/qa/skill-pr` 的 `skill-review-and-quality.md` 中 `references/cli-installation-guide.md` / `iam-policies.md` / `verification-method.md` / `acceptance-criteria.md` | 这是"**被审查技能**应包含的推荐文件"的**模板清单**（KooCLI/Huawei 云技能结构），非本技能自身的文件依赖 → 不改 |
| `vod/references/requirement-elicit-guide.md:3` → `skills/vod/SKILL.md` | 自引用（同技能）→ 不改 |
| `pr/qa/skill-pr` 的 `../.last_pr_review/` | workspace 相对路径（非技能依赖）→ 不改 |

---

## 二、处理方案

### A1：issue-advance 的 new-dev-flow 引用
- 复制 `skills/implementation/references/new-dev-flow.md` → `skills/issue-advance/references/new-dev-flow.md`。
- 引用本身已写为 `references/new-dev-flow.md`（相对），复制后即可解析 —— 与之前 vod 修复同模式。
- 保留 implementation 的原始文件（issue-advance 自持副本）。

### A2：implementation → skills/agent/*（关键决策）

**方案（按评审确认）**：`skills/agent/` 下的 5 个技能**不作为技能存在**，转为 `implementation` 的 **references 参考文档**：

- 转换：`skills/agent/<name>/SKILL.md` → `skills/implementation/references/<name>.md`（**去掉 YAML frontmatter**，保留正文；`agent/` 本身无 SKILL.md，不构成技能）。
  - 5 个文件：`spec-driven-development` / `planning-and-task-breakdown` / `test-driven-development` / `incremental-implementation` / `code-review-and-quality`
  - 各文件 207~397 行、frontmatter 仅 4 行、无自身 references → 直接剥离 frontmatter 转参考文档。
- 更新 `new-dev-flow.md` 引用：`skills/agent/<name>/SKILL.md` → `references/<name>.md`（implementation 内相对路径）。
- **删除 `skills/agent/` 目录**（agent 不作为技能存在）。

**理由**：
- implementation 是 `skills/agent/*` 的**唯一使用者**（全仓 src/configs/tests 已扫描，无其它引用）。
- `skills/agent/` 非顶层技能（无自身 SKILL.md，Agent 一级遍历不发现它）→ 其内容本属 implementation 的开发方法论，转 references 后彻底自持，消除跨技能依赖与"伪技能"目录。

**影响面**：仅 `skills/` 内部；已确认无代码/配置/测试引用 `skills/agent`。执行后全仓复核。

### B：悬空 See Also
- 删除 `pr` / `qa` / `skill-pr` 的 `references/code-review-and-quality.md` "See Also" 两行（`:351-352`，`security-checklist.md` / `performance-checklist.md`）—— 共 6 行。

### C：悬空示例
- `skill-pr/SKILL.md:74` 与 `:82` 的 `tokauthai/skillscan` 示例，改为本仓库真实技能示例（如 `skills/qa/SKILL.md` → `qa`），标题示例同步改为 `add skill: qa`。

---

## 三、验证

1. 复跑扫描脚本：`skills/` 内无 `skills/<其它技能>/` 路径引用、无跨技能 `references/xxx` 悬空。
2. skill 相关测试：`test_qa_skill` / `test_issue_advance_skill` / `test_vod_skill` / `test_issue_advance_skill` 通过。
3. 全量回归（全新 test DB）。
4. 部署 server + worker（边端执行读 skills），重启后确认 skills 目录结构。

## 四、改动文件清单

| 操作 | 文件 |
|------|------|
| 复制 | `skills/issue-advance/references/new-dev-flow.md`（新增，A1） |
| 转换 | `skills/agent/{5 技能}/SKILL.md` → `skills/implementation/references/{name}.md`（去 frontmatter，A2） |
| 删除 | `skills/agent/` 目录（A2） |
| 修改 | `skills/implementation/references/new-dev-flow.md`（引用改 `references/{name}.md`，A2） |
| 修改 | `skills/pr/qa/skill-pr/references/code-review-and-quality.md`（删 See Also 2 行，B） |
| 修改 | `skills/skill-pr/SKILL.md`（示例改真实技能，C） |

> 方案已按评审确认（A2 采纳"agent/* → implementation/references"）。待你确认整体方案后实施。
