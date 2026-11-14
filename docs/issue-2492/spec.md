# Spec: Issue #2492 — 修复变基（rebase）合入失败的错误提示

> 开发基线：`arch-weekly-11-ljt` | 分支：`feat/issue-2492` | 优先级：P1

## Objective

- 构建：`merge_method=rebase` 合入失败时，`merge_pull` 返回**中文结构化提示**
  （失败原因=变基冲突、冲突提交 sha、处理建议），替代对上游英文报错的裸透传。
- 价值：用户可直接判断并操作（同步 base 解决冲突后重试 / 改用常规合并），
  解除 PR #1397 的合入阻塞。
- 用户：通过 API/CLI 触发变基合入的开发者。

## 现状（实证）

`PUT /api/git-repo/{owner}/{repo}/pulls/{number}/merge` 在 `merge_method=rebase` 时，
上游返回类似：

```
Something went wrong during merge: rebase failed (conflict) ... commit "abc1234" ...
```

`_vcs_req` 按 Issue #549/#558 设计透传上游状态码与完整 message，于是调用方看到的
`detail.message` 就是上述英文裸串——没有原因归类、没有冲突提交、没有处理建议。

## Scope

### In Scope

- `src/api/git_repo_api.py::merge_pull` 对 rebase 分支失败的错误归类与结构化改写。
- 冲突特征识别：message 同时含 `rebase` 与 `conflict`/`conflicting`。
- 冲突提交 sha 提取：正则 `commit "([0-9a-f]{7,40})"`。
- 上游原文保留在 `detail.upstream_message` 与日志（`log.warning`）。
- rebase 非冲突失败的兜底中文提示（附上游原文）。
- 单测三分支：rebase 冲突命中 / rebase 非冲突兜底 / merge-squash 回归。

### Out of Scope

- 自动解决冲突、自动同步 base。
- CLI/Web 一键「改用常规合并」动作入口（仅在提示文字中给出建议）。
- `merge`/`squash` 提示与既有门禁（冲突标记门禁、QA gate）行为的任何变更。

## Acceptance Criteria

1. rebase 冲突返回中文结构化提示（含「变基冲突」原因、冲突提交 sha、处理建议），
   不再以 `Something went wrong during merge` 裸串作为用户可见提示。
2. `upstream_message` 在 `detail` 子字段与日志可取；返回状态码与上游语义一致。
3. 非冲突类 rebase 失败走兜底中文提示，保留上游原文。
4. `merge`/`squash` 路径提示与既有门禁（冲突标记门禁、QA gate）行为不变。
5. 单测覆盖三分支，现有测试全通过。

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| 上游错误文案变化导致特征识别漏判 | Med | 未命中即走兜底（附原文），不吞信息 |
| detail 新增子字段影响既有消费方 | Low | 仅新增字段，不改既有字段语义 |
| merge/squash 路径回归 | Med | 单测回归分支 + 既有门禁测试保持通过 |