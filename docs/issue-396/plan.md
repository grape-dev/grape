# Plan: Issue #396 — 404 修复方案设计

## 方案
以 FastAPI 标准 `raise HTTPException(status_code=404, detail=...)` 替换 `return {"error": ...}`；在 `try/except Exception` 兜底前插入 `except HTTPException: raise`，保证 404 不被兜底吞掉降级为 200。

## 改动点
| 文件 | 改动 |
|------|------|
| `src/api/routes/community.py` | `get_post`/`add_comment` 两处 `Post not found` 改抛 404；`toggle_like` 补帖子存在性校验后抛 404；三处兜底链加 `except HTTPException: raise` |
| `src/api/routes/feedback.py` | `update_feedback_status` `rowcount==0` 分支抛 404（detail 与 DELETE 一致：`{"code":404,"message":"反馈记录不存在"}`）；兜底链加 `except HTTPException: raise` |
| `tests/test_community_404.py` | 新增：不存在帖 GET/like/comment → 404；合法帖点赞/评论 → 200 不回归 |
| `tests/test_feedback_status.py` | 新增：不存在/已删除记录 → 404；合法记录 → 200；无效状态值行为不变 |

## 关键决策
- 错误方式选 `raise HTTPException`（FastAPI 惯例，自动生成 OpenAPI 404 schema）。
- feedback detail 用 dict `{"code","message"}` 与同文件 DELETE/403 分支格式对齐。
- 前端：共享 `API` 层 `handleResp` 已统一对非 2xx throw（api.js），调用方 `.catch` 展示错误，无需改页面代码。

## 测试策略
- 单测（TestClient + 独立测试库）先行：404 分支 + 合法路径不回归。
- 回归：`test_feedback_delete.py`（同文件 DELETE 行为）与相关路由测试全通过；基线既有失败（Knowledge/OpenAPI 等，环境相关）不受影响。

## 目标分支
`功能分支`（PR base）
