# Spec: Issue #3482 — direct 执行路径 worktree 目标目录被占用误降级只读

> 需求权威源 = Issue #3482 评论（[VOD Agent] 补充评审修订，comment id 202239010，覆盖原 Spec 评论 202229615）。
> 本文件为开发落地映射摘录，不改动裁定。

## 1. 目标

- direct 执行路径与 worker 执行路径**顺序一致**：先建 worktree 再物化技能/知识/project-info；
- `ensure_issue_worktree` 在 `git worktree add` **之前**处置「目标目录已存在且无 `.git`」（平台产物清空 / 未知内容隔离留证）；
- 建树失败不再 fail-open：任务置 failed 并写明 workspace 不可用，`GRAPE_GIT_READONLY` 只在「gitdir 存在但不可写」时出现；
- 只读判定改判定源（stat + errno），stderr 文案仅作日志线索。

## 2. 根因（四层，逐条在 `arch-weekly-11` 代码事实核对成立）

1. **第一根因：direct 端物化早于建树（顺序缺陷）**。`direct_executor.py` 旧顺序：
   `os.makedirs(ws)` → `_materialize_direct_workdir(...)`（写 `.skills`/`.project-info`/`.knowledge`）
   → `ensure_issue_worktree(...)`。worker 端 `agent.py` 被写死为「必须先建 worktree 再落地技能」
   （#711 方案 A），direct 端没有照搬。
2. **第二层：`ensure_issue_worktree` 缺「目标目录已存在且无 `.git`」的处置分支**。只有 `_has_git`
   为真才进复用闸门；无 `.git` 时不清理、不隔离，直接 `git worktree add`（上次残留目录也无任何一层覆盖）。
3. **第三层：建树失败被静默吞掉**。direct 端建树异常/返回 None 只 `log.warning` 后照常执行；
   `inject_git_write_context` 把 `git dir not found` 也降级成 `GRAPE_GIT_READONLY=true` →
   「工作区没建起来」被标成「只读会话」，任务 completed 却缺交付物。
4. **第四层（放大面）：只读判定依赖 stderr 文案白名单**（`read-only file system` /
   `operation not permitted` / `permission denied`）。`already exists` 正是被漏掉的那条；
   在白名单里再加词等于在同一脆弱点上继续叠加。

## 3. 方案（A→B→C→D）

- **A 物化整体下移**：direct 端拿到 `_wt` 之后再物化 `.skills` / `.project-info` / `.knowledge`，
  `_skill_consistency` 校验依赖物化结果，一并下移（不能只移一半）。落到
  `_materialize_workdir_and_skill_gate()`（`direct_executor.py`）。
- **B 建树前置处置**：`dispose_occupied_workdir_target()` + `ensure_issue_worktree` 前置调用。
  判据 = stat（存在/非空/无 `.git`），不看 stderr：
  - 仅平台白名单产物（`.skills` / `.project-info` / `.knowledge` / `_grape_stage2.py` /
    `.git-askpass*` / `.grape-*`）→ 清空后重建；
  - 含未知内容 → 整体 mv 为 `<dir>.stale-<ts>` 隔离留证后重建（不删除）；
  - 处置失败 → 返回 None（workspace 不可用，上游 failed）。
- **C 关闭 fail-open**：direct / worker 两端建树失败（None 或异常）→ 任务 failed，
  `completed_reason`（`result_text`/`errors`）写明 workspace 不可用及原因；
  `inject_git_write_context` 中 `git dir not found` 标 `mode=missing` + `GRAPE_GIT_WORKSPACE_MISSING=true`，
  不再置 `GRAPE_GIT_READONLY`。
- **D 判据改判定源**：`readonly_errno_reason()` 先 stat 目标目录（不存在则父目录），
  再按 errno（EROFS / EACCES / EPERM）判只读；其它 errno 一律不降级。
  `_is_readonly_git_failure` 保留但仅作日志线索。
- 已完成的 `already exists` 恢复分支作为**最后一道防线**保留（前置处置之后的竞态），不再作为主修。

## 4. 验收（5 条，来自 comment 202239010）

- [ ] 预置 `issues/<n>/<agent>/.skills/`（无 `.git`）→ 建树成功、commit/push 可用、日志不出现 `GRAPE_GIT_READONLY`；
- [ ] 预置含未知文件的目录 → 被隔离为 `.stale-*` 后建树成功；
- [ ] 建树确实不可恢复（如分支被占用 / 隔离失败）→ 任务 failed，不得以只读 completed 收尾；
- [ ] direct 与 worker 两条路径跑同一用例，结果一致；
- [ ] 仅「gitdir 存在但不可写」时出现 `GRAPE_GIT_READONLY=true`。

## 5. Out of Scope

- 只读文件系统/权限类（read-only fs、permission denied）的**迁移**语义不变（迁移到会话可写 gitdir）；
- 非 git 类 agent 执行路径不动。

> 原 Out of Scope 中「`.skills` 技能物化机制本身的写入时机调整（仅在 worktree 创建侧做恢复）」已撤销——
> 物化时机调整属 In Scope（A），原写法把第一根因排除在修复范围外。