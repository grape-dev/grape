# Implementation Plan: Issue #510 — analyze-image 缺 url 参数返回 400

## Overview
将 `src/api/routes/chat.py::analyze_image` 缺参分支的裸 `return {"error": ...}` 改为 `raise HTTPException(400, {"code": 400, "message": "missing url parameter"})`，并新增原子回归测试锁定行为。

## Architecture Decisions
- 状态码选 400 而非 422：端点用 `Request` 手动解析原始 query string（OBS 未编码 `&`），无 Pydantic 校验层，422 无从触发。
- 包络选 `HTTPException(400, {"code": 400, "message": ...})`：与同文件 L173/L201、auth.py 既有风格一致，FastAPI 自动渲染 `{"detail": {...}}`。
- 测试 seam = HTTP 接口（TestClient 直发），复用已有 seam，不 mock Request。

## Task List
- [ ] Task 1: 新增原子回归用例 `analyze_image_missing_url`（红）——断言 400 + missing url 语义 + 正常路径含 OBS `&` 场景不回归
  - Verify: `pytest tests/test_chat_analyze_image_510.py -k analyze_image_missing_url`（修复前红）
- [ ] Task 2: 最小实现——缺参分支 raise HTTPException(400)（绿）
  - Verify: 上述用例转绿

## Checkpoint: Complete
- [ ] 本仓新增用例全绿；现有测试全绿
- [ ] pre-pr-release-check 增量检查通过

## Risks and Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| 调用方假设 200 解析 body | Low | 端点本就供按状态码判成败的调用方使用；无已合入依赖 200+error 的证据 |
| 与 #408/#409 同根因重复修复冲突 | Med | 最小原子改动；后续在 #408/#409 引用同一 commit |

## Open Questions
无。