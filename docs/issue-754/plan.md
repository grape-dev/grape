# Issue #754 Plan

## 改动点

1. `src/api/git_repo_api.py`
   - 新增 `_scan_pull_conflict_markers(p, owner, repo, number, token)`：分页拉取
     `GET /pulls/{n}/files`（复用 `list_pull_files` 的 `_normalize_pull_files_diff` 归一化），
     统计 `.patch.diff` 中行首 `<<<<<<<` / `>>>>>>>` 冲突标记数，返回命中文件列表。
   - `merge_pull`：合并 PUT 之前执行预检；命中 → 台账记 `pr_merge_blocked` +
     best-effort PR 评论留痕 + HTTP 409 拒绝；预检上游失败（如 405/404）→ fail-open
     透传合并（保持 #549/#558 透传语义，不引入新的 502）。
   - `create_pull_comment`：非 github 通道 POST `/pulls/{n}/comments` 405（merged PR
     不可 review）→ 自动降级 POST `/issues/{n}/comments` 等价通道；台账照记
     `issue_comment`（content_hash 对账）。
2. `src/api/event_pipeline.py`
   - `_post_result_to_pr`：GitCode 裸 urllib 路径同样 405 → `/issues/{n}/comments` 兜底。

## 测试

- `tests/test_issue_754_merge_gate.py`：diff 含冲突标记 → 409、merge 未透传、台账/评论留痕被调用；
  无标记 → PUT merge 正常透传；预检抛 UpstreamAPIError → fail-open。
- `tests/test_git_repo_api.py` 新增：GitCode PR 评论 405 → issue 端点兜底；GitHub 通道不变。
- `_post_result_to_pr` 405 兜底单测（mock urlopen）。
