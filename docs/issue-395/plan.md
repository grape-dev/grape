# Plan: Issue #395 — 四项缺陷修复方案

> 按 P0 → P1 → P2 顺序：#381 → #380 → #382 → #383。每项一个原子提交（TDD：先测试后修复）。

## 1. #381 org 鉴权绑定路径 org_id（P0）

**改动点：**
- `src/api/deps.py` `require_org_member`：
  - 新增可选路径参数校验——FastAPI 依赖无法直接读路径参数，采用**路由层显式传参**方案：`require_org_member` 增加 `path_org_id` 支持。
  - 具体实现：为路径型 org 接口新增 `require_path_org_member(org_id)` / `require_path_org_admin(org_id)` 工厂依赖（FastAPI 子依赖模式），路由签名改为 `user: dict = Depends(require_org_member_for_path())` 不行——路径参数在依赖中不可见，改为直接在路由函数内先调用 `require_org_member` 逻辑。
  - **最终方案**：`require_org_member` 保持非路径接口行为不变；新增 `get_path_org_auth(org_id, user)` 纯函数 + 在 `update_organization`/`delete_organization` 路由内首行调用（404 不存在 / 403 非成员 / 403 角色不足）。
- `src/api/routes/orgs.py`：
  - `update_organization`/`delete_organization` 改用 `Depends(require_org_admin)` → 移除，替换为路径 org_id 鉴权 + 保留角色校验。
  - `update_org` 返回 bool（rowcount>0）已有——路由层判断 False → 404。
  - `delete_org` 返回 False → 404（不存在）或 400（个人组织不可删，区分：先 get_org 判断）。

**行为矩阵：**
| 场景 | 修复前 | 修复后 |
|------|--------|--------|
| PUT /api/orgs/{不存在} | 200 | 404 |
| PUT /api/orgs/{他人 org} | 200（越权） | 403 |
| PUT /api/orgs/{自己非 admin 的 org} | 200 | 403 |
| PUT /api/orgs/{自己是 admin 的 org} | 200 | 200 |
| DELETE 同上 | 400/200 | 403/200（存在性前置） |
| 非路径接口（X-Org-ID） | 不变 | 不变 |

## 2. #380 knowledge 分页校验（P1）

**改动点：**
- `src/api/routes/knowledge.py` list_knowledge：
  - `page: int = Query(1, ge=1, le=100000, ...)`、`page_size: int = Query(50, ge=1, le=200, ...)`
- `src/api/main.py` `global_exception_handler`：DB/未捕获异常返回通用 message「服务器内部错误」，原文 `str(exc)` 仅落日志（保留 request_id）。

## 3. #382 a2a repository 类型校验（P1）

**改动点：**
- `src/api/routes/webhook.py` a2a_endpoint：
  - 前置校验处（`repository_early = str(...)` 之前）加 `isinstance(payload.get("repository"), str)` 校验，非 str → 400「repository must be a string in 'owner/repo' format」。
  - `repository = payload.get("repository", "")` → `repository = repository_early`（统一使用已验证的字符串）。

## 4. #383 host_repo 上游异常分流（P2）

**改动点：**
- `src/api/routes/repos.py` host_repo 权限验证块（`except Exception as e: raise HTTPException(502, f"权限验证失败: {e}")`）：
  - 新增 `except RuntimeError` 分支：正则 `failed \((\d{3})\)` 提取上游状态码；404 → 404「仓库不存在或无权访问」；401/403 → 403「无权限托管，请联系管理员」；其余 → 落兜底。
  - 兜底 502 文案改固定「托管服务暂时不可用，请稍后重试」，`log.warning` 记原文。
  - 既有 GitHub 分支 `except Exception` 的 `f"托管失败: {e}"` 同步脱敏为固定文案。

## 测试计划（tests/test_issue_395_bugfixes.py，沿用 issue-386 模式）

| 用例 | 覆盖 |
|------|------|
| test_knowledge_page_size_negative_422 | #380 |
| test_knowledge_page_zero_422 / test_knowledge_page_size_over_200_422 | #380 |
| test_knowledge_valid_pagination_unchanged | #380 |
| test_org_update_nonexistent_404 | #381 |
| test_org_update_other_org_403 | #381 |
| test_org_update_member_not_admin_403 | #381 |
| test_org_update_admin_ok / test_org_delete_owner_ok | #381 |
| test_org_delete_other_org_403 | #381 |
| test_a2a_repository_dict_400 / int / list / None | #382 |
| test_a2a_repository_str_ok | #382 |
| test_host_repo_upstream_404_returns_404 | #383 |
| test_host_repo_upstream_403_returns_403 | #383 |
| test_host_repo_upstream_500_keeps_5xx_no_leak | #383 |
| test_global_exception_handler_sanitized | #380 全局 |

## 提交计划
1. `test(#395): add regression tests for #380-#383 (RED baseline)` — 测试先行
2. `fix(#381): bind org write endpoints auth to path org_id (P0)` 
3. `fix(#380): validate knowledge pagination params + sanitize global error handler`
4. `fix(#382): reject non-string repository in /api/a2a with 400`
5. `fix(#383): map upstream 404/403 properly in host_repo without leaking internal URL`
6. `docs(#395): add spec/plan/task docs`
