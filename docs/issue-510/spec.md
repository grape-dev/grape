# Spec: Issue #510 — analyze-image 缺 url 参数应返回 400 统一包络

> 依据 Issue #510 评论中发布的 Spec（[issue agent] 2026-10-29），本文件为 coding 侧落盘副本。

## Objective
- `GET /api/chat/analyze-image` 在缺失必填 `url` 参数（query string 不以 `url=` 开头）时返回 400（FastAPI `HTTPException` 统一包络），不再以 200 + `{"error": "missing url parameter"}` 承载错误。
- 用户：调用内部工具接口的 agent 自动化链路（按 HTTP 状态码判定成败）。

## 现状（已核验）
- `src/api/routes/chat.py` `analyze_image`（L57-76）：因 OBS 签名 URL 含未编码 `&`，端点用 `Request` 手动解析原始 query string；`raw_qs` 不以 `url=` 开头时直接 `return {"error": "missing url parameter"}`（FastAPI 默认 200）。

## 成功标准（Acceptance Criteria）
- [ ] 不带 url → 400（非 200），body 为统一包络 `{"detail": {"code": 400, "message": "missing url parameter"}}`
- [ ] 带 `url=` 的正常路径零回归（含 OBS 签名 URL 未编码 `&` 场景）
- [ ] 原子用例 `analyze_image_missing_url`（外部原子测试集登记口径）→ XPASS；本仓新增回归用例全绿
- [ ] 所有现有测试通过

## Scope
### In
- `src/api/routes/chat.py` analyze_image 缺参分支：`return dict` → `raise HTTPException(status_code=400, detail={"code": 400, "message": "missing url parameter"})`（HTTPException 已导入 L23）
- `tests/test_chat_analyze_image_510.py`（新增）回归用例

### Out of Scope
- 同函数其余 200 错误分支（domain not allowed / download failed）——归 #508 错误码治理簇
- OBS 签名 URL 手动解析逻辑与域名白名单防 SSRF 逻辑——不变
- 前端/调用方适配

## Code Style / 包络风格
对齐同文件 L173/L201 与 auth.py 既有 `HTTPException(status_code=…, detail={"code": …, "message": …})` 用法。

## Testing Strategy
- FastAPI TestClient 直发真实请求（同 tests/test_chat_multi_image_297.py 惯例），不 mock Request。
- 端点无鉴权依赖，无需 dependency_overrides。

## Boundaries
- Always: TDD 红/绿分提交；改动仅限本 Issue 范围
- Never: 修改 domain not allowed / download failed 分支；改动 OBS 解析与 SSRF 白名单逻辑