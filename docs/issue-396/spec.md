# Spec: Issue #396 — 社区点赞/评论与反馈状态流转 404 修复

> 承载 #384（AT-COMM-04）与 #385（AT-FBA-03）两项修复，详细 Spec 见 Issue #384  与 #385 评论。

## 目标
1. **#384**：社区接口对不存在帖子（`GET /posts/{pid}`、`POST /posts/{pid}/like`、`POST /posts/{pid}/comments`）从 `200 + {"error": ...}` 改为标准 `404`（`HTTPException`）。
2. **#385**：`PUT /api/feedback/{fid}/status` 对不存在记录从 `200 + {"error": ...}` 改为 `404`，detail 与同文件 DELETE 分支对齐（`{"code": 404, "message": "反馈记录不存在"}`）。

## 范围
- `src/api/routes/community.py`：3 处错误字典改抛 `HTTPException(404)`；`toggle_like` 原先无存在性校验，补齐帖子存在性检查；各 `except Exception` 兜底前加 `except HTTPException: raise` 防吞 404。
- `src/api/routes/feedback.py`：PUT `/status` 不存在分支改抛 404；兜底链同上处理。
- 前端：本仓共享 `API` 请求层（`src/api/static/js/api.js` `handleResp`）已对非 2xx 统一 throw（携带 status/detail），调用方均带 `.catch` 提示，无需逐页适配。
- 测试：新增 `tests/test_community_404.py`、`tests/test_feedback_status.py` 覆盖 404 分支与合法记录不回归。

## 不在范围
- 其他社区/反馈接口错误码统一（后续迭代）。
- 错误体结构重构（不调整 detail schema）。

## 验收标准
- `POST /api/community/posts/999999/like` 与 `.../comments` 返回 404
- `PUT /api/feedback/999999/status` 返回 404；合法记录行为不回归
- 全部相关测试通过，无回归

## 目标分支
`功能分支`（服务端注入 `$GRAPE_WORK_BRANCH`，权威）
