# Plan: Issue #2492 — rebase 合入失败的中文结构化提示

> 对应 spec.md | 开发基线：`arch-weekly-11-ljt` | 分支：`feat/issue-2492`

## 方案

在 `merge_pull` 的 **合入 PUT 调用**外围加一层归类，仅在 `merge_method=rebase` 时生效；
其余方法（merge/squash/缺省）保持原样 re-raise，行为完全不变。

```
merge_pull
  ├─ 门禁（冲突标记门禁 / QA gate）           ← 不变
  ├─ try: _vcs_req(PUT .../merge)
  └─ except HTTPException as exc:
        merge_method == "rebase" ? → _rebase_merge_failure(exc) → 中文结构化 detail
        else                     ? → raise（原样透传，回归安全）
```

新增模块级私有件（`src/api/git_repo_api.py`）：

```
_REBASE_CONFLICT_SHA_RE      # re.compile(r'commit\s+"([0-9a-f]{7,40})"')
_REBASE_MERGE_SUGGESTION     # 处理建议文案（单一来源，message/suggestion 共用）
_merge_upstream_message()    # HTTPException.detail → 上游原始 message 文本
_classify_rebase_merge_failure()  # → (是否冲突, 冲突提交 sha 列表)
_rebase_merge_failure()      # → 新的 HTTPException(中文结构化 detail)
```

## 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 状态码 | 包装 502 / 保持上游语义 | **保持上游语义** | 延续 #549/#558 透传设计，不新增 502 包装 |
| 原文保留 | 丢弃 / detail 子字段 + 日志 | **detail.upstream_message + 日志** | 维护者溯源需要，仅新增字段、向后兼容 |
| 归类位置 | `_vcs_req` 内 / `merge_pull` 内 | **`merge_pull` 内** | 归类只对 rebase 合入语义成立，不能污染通用请求层 |
| 冲突特征 | 单一 `conflict` / `conflict`+`conflicting` | **两者都认** | 上游文案存在 `conflict` 与 `conflicting` 两种形态 |
| 未命中冲突 | 抛原文 / 兜底中文 | **兜底中文 + 原文** | 不吞信息，同时保证用户可见提示是中文归类后的形态 |

## detail 结构（新增子字段，既有 `code`/`message` 语义不变）

```json
{
  "code": 400,
  "message": "变基冲突：rebase 合入失败，PR 与目标分支存在冲突（冲突提交: abc1234）。建议：…",
  "reason": "rebase_conflict",          // 或 "rebase_failed"
  "conflict_commits": ["abc1234"],
  "suggestion": "同步目标分支并解决冲突后重试，或改用常规合并（merge_method=merge）",
  "upstream_message": "Something went wrong during merge: ... "
}
```

## 实现步骤

1. 先写失败单测（RED）：三分支 + 日志断言。
2. `src/api/git_repo_api.py` 加归类私有件与 `merge_pull` 的 try/except 归类分支。
3. 端点 description 补一句 rebase 失败语义说明（契约文档同步）。
4. 跑绿单测与既有 merge/门禁相关回归。

## 测试策略

- `tests/runtime/test_issue_2492_rebase_merge_hint.py`：mock `_vcs_req` / `_repo_token` /
  `_provider` / `_pre_merge_qa_gate` 及 `merge_gate` 三件，三分支断言。
- 回归：`tests/runtime/test_issue_453_outbound_ledger.py -k merge`、
  `tests/runtime/test_issue_993_merge_gate_structure.py`、
  `tests/runtime/test_issue_1739_qa_close_gate.py`。