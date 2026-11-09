# Spec: Issue #728 — PR body 提及无关 Issue 号被 e2e 自动关联，需手动 update 解除

> Coding Agent | 基于 Issue Agent Spec v2 执行 | 目标分支: weekly-08-mys | 开发分支: feat/issue-728

## Objective
- PR body 中 Issue 号自动关联的**上下文过滤**：代码块（```）、行内反引号、引用块（>）内的 `#N` 不再被关联提取捕获；`Closes #` / `Related #` / `关联 #` 显式语义不回归。
- PR↔Issue 关联解除：新增服务端 unlink 端点（幂等、双参数显式校验），契约操作 `gitrepo_pulls_issues_unlink` 对应 CLI 封装 `grape git-repo pulls issues unlink`。
- git-repo CLI runbook（help/api/10-git-repos.md）增补「规避写法」说明段与 unlink 示例。

## Scope

### In Scope
- 纯函数过滤模块 `src/vcs/issue_ref_filter.py`：strip fenced code / inline code / blockquote 后提取 `#N`；strict 模式仅识别显式关键字前缀。
- `src/api/git_repo_api.py` `create_pull`：调用方未显式传 `related_issue_nums` 时，从 body 经过滤函数自动提取（自动关联统一走过滤层）。
- 新增端点 `DELETE /{owner}/{repo}/pulls/{number}/issues/{issue_number}`（operation_id `gitrepo_pulls_issues_unlink`）：GitCode 通道先 GET PR 校验关联存在、再 PATCH 剩余 `inner_issue_nums` 删除；未关联 → 幂等成功返回 `unlinked=false`；GitHub 通道无原生 PR↔Issue 关联 → 幂等 no-op。
- `help/api/10-git-repos.md`：pulls 表新增 unlink 行 + CLI 示例 + 「规避写法」说明段。

### Out of Scope
- #604 auto-comment 标记追加、#680 合并事件取错关联 issue（另行处理）。
- 关联存储结构重构、GitCode 平台自身 body 扫描策略。
- grape CLI 仓库的命令实现（契约驱动自动生成，本仓提供端点与契约 operation_id）。

## Acceptance Criteria
- [ ] 代码块 / 反引号 / 引用块内 `#N` 不被关联提取捕获（单测覆盖三种语境）
- [ ] `Closes #` / `Related #` / `关联 #` 显式关联行为不回归（strict 模式单测）
- [ ] unlink 端点可解除已建立关联且幂等（重复调用返回 unlinked=false、不再发 PATCH）
- [ ] help/api/10-git-repos.md 含「规避写法」说明段与 unlink CLI 示例
- [ ] 现有测试通过 + 新增测试（过滤函数三种语境 + unlink 命令）

## 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 过滤策略 | 纯函数默认忽略代码/引用语境 + 可选 strict 显式关键字模式 | 兼容自然语言写法；strict 供严格场景 |
| 解除命令形态 | 新增专用 DELETE 端点（PR 号 + Issue 号双路径参数） | update 为整体覆盖语义；专用端点幂等清晰、参数显式 |
| unlink 上游实现 | GET 校验 → PATCH 剩余 inner_issue_nums | 复用既有 provider 通道与翻译规则，无新上游依赖 |
