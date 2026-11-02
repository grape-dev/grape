# Plan: Issue #623

## 方案（Spec 方案 A + 文档兜底）
服务端 update 端点兼容两种 id 形态，映射在服务端完成，消除所有客户端试错成本。

## 改动点
1. `src/api/git_repo_api.py`
   - 新增 `_resolve_pull_comment_note_id(p, owner, repo, number, comment_id, token)`：
     - 纯数字 → 直传（存量行为不变）
     - 严格十六进制（`[0-9a-fA-F]+` 且含字母）→ ① `GET /pulls/comments/{hex_id}` 读响应数字 `id`；② 失败回退 `GET /pulls/{number}/comments` 按 `discussion_id` 匹配；401/403 透传不吞
     - 其余 → 422 明确信息
   - `update_pull_comment`：`comment_id: int` → `str`，解析出 note_id 后走既有 PATCH 透传；description 补契约说明。
2. `help/api/10-git-repos.md`：PATCH 小节补 Issue #623 契约说明。
3. `tests/test_git_repo_api.py`：新增 5 个测试（hex 单评论映射 / hex 列表回退 / 非法 422 且不触达上游 / 不可解析 422 / 数字直传向后兼容）。

## 关键决策
- 严格十六进制识别：仅全 hex 且含 a-f 字母才走映射分支，纯数字串按 note_id 直传，避免误判。
- delete 端点不改动（Spec 范围仅 update；delete 保持数字 note_id，文档注明）。

## 验证
- `python3 -m pytest tests/test_git_repo_api.py`：新增 5 测试全绿；存量 test_issues_crud_mapping 失败为基线已有（stash 复核），与本改动无关。
