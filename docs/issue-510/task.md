# Task: Issue #510 — analyze-image 缺 url 参数返回 400

## Task 1: 新增原子回归用例（红）
**Description:** 新建 `tests/test_chat_analyze_image_510.py`，TestClient 直发 `GET /api/chat/analyze-image`（不带 url）断言 400 + 包络 missing url 语义；同时锁定 `url=` 正常路径（含 OBS 签名 URL 多 `&`）不回归。用例名 `analyze_image_missing_url`（与外部原子测试集登记口径一致）。

**Acceptance criteria:**
- [ ] 无 url → status_code == 400，body 为 `{"detail": {"code": 400, "message": "missing url parameter"}}`
- [ ] `url=` 正常路径（含 OBS `&` 参数）行为不回归（域名校验/下载逻辑保持原样，测试只覆盖缺参语义与正常带参请求的 200/error 结构）

**Verification:**
- [ ] 修复前：`pytest tests/test_chat_analyze_image_510.py -k analyze_image_missing_url` 红（400 断言失败，现返回 200）

**Files likely touched:**
- `tests/test_chat_analyze_image_510.py`（新增）

**Estimated scope:** Small

## Task 2: 最小实现（绿）
**Description:** `src/api/routes/chat.py` analyze_image 缺参分支 `return {"error": "missing url parameter"}` → `raise HTTPException(status_code=400, detail={"code": 400, "message": "missing url parameter"})`。

**Acceptance criteria:**
- [ ] 缺参 → 400 包络
- [ ] 正常路径零回归

**Verification:**
- [ ] `pytest tests/test_chat_analyze_image_510.py -k analyze_image_missing_url` 绿
- [ ] `pytest tests/test_chat_routes.py tests/test_chat_multi_image_297.py` 绿（既有 chat 测试无回归）

**Files likely touched:**
- `src/api/routes/chat.py`

**Estimated scope:** Small