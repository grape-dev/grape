# Plan: Issue #513 — devstage 删除不存在环境 404 统一包络

## 方案设计

### 现状缺陷链
1. `src/api/routes/devstage.py` `delete_environment()`：
   - 前置 `_list_envs()` 查环境名，`DevStageError` 被吞（`env_name=""`）——「不存在」信号丢失点之一；
   - `_delete_env()` 抛 `DevStageError` 一律映射 `HTTPException(502, {"code": e.code, "message": str(e)})`——上游 `HD.97420111 parameter invalid` 原样透传前端。
2. `src/im/devstage_client.py` `_request()`：`error_code != "0000"` 直接 `raise DevStageError(err, error_msg)`，无「资源不存在」语义。

### 改动设计（两层判别）
**客户端层 `devstage_client.py`**：
- `DevStageError` 增加 `not_found: bool = False` 属性（构造签名向后兼容：`DevStageError(code, msg)`）；
- `_request()` 中 `err != "0000"` 时调用新增的 `_is_not_found_error(err, error_msg)` 白名单判别，命中则 `raise DevStageError(err, error_msg, not_found=True)`；
- 白名单（最小集，仅明确的资源不存在特征）：错误码 `HD.97420111`（删除/操作不存在环境实测码）。`NETWORK`/`PARSE`/HTTP 5xx 等一律 not_found=False（真实故障→502）。

**路由层 `devstage.py` `delete_environment()`**：
- 前置 `_list_envs` 成功但 `devstage_id` 不在列表 → 直接抛 404 统一包络（`api_error(404, "DevStage environment not found", internal=...)` 风格，与 `src/api/errors.py` 约定一致），不发起删除调用、不做凭据清理；
- `_list_envs` 本身失败（列表不可用）→ 不再吞掉语义：继续走删除调用，由删除结果分类；
- `_delete_env` 抛 `DevStageError`：`e.not_found=True` → 404 统一包络（message 不含上游码/文案）；否则维持现有 502 透传。

### 测试 seam（公开接口/用户可见行为）
- 路由层 seam：`ds_route.delete_environment(devstage_id, user)` 直接调用（沿用 tests/test_devstage_filter.py 的 `unittest.mock.patch` 模式，mock `_get_ak_sk`/`_list_envs`/`_delete_env`）——断言 HTTPException.status_code == 404/502 与 detail 包络；
- 客户端层 seam：`ds_client._request`（mock urlopen）——断言 `DevStageError.not_found` 分类正确。

## 任务拆解（TDD 红/绿分提交）
1. **红**：`tests/test_devstage_delete_404_513.py` 新增原子用例 `delete_missing_environment_is_404`（列表无该 id → 404 统一包络）+ `delete_upstream_not_found_code_maps_404`（上游 HD.97420111 → 404）+ `delete_upstream_real_failure_keeps_502`（真实故障维持 502）+ 客户端 `_request` 分类用例；`pytest.ini` 注册 `atomic` marker。提交 `test(#513)`。
2. **绿**：`devstage_client.py`（not_found 分类）+ `devstage.py`（路由 404 映射 + 前置判别）最小实现。提交 `feat(#513)`。
3. 回归：devstage 相关测试 + 全量受影响面；`docs/issue-513/` 文档提交（随绿提交或单独 docs 提交）。

## 风险与对策
- 上游码不稳定误判 → 白名单最小集 + 502 兜底保守；
- 前置列表成功但环境刚被并发删除 → 删除调用会抛 not_found 类错误 → 同样落 404，闭环一致；
- 现有调用 `DevStageError(code, msg)` 的代码不受影响（新参数带默认值）。
