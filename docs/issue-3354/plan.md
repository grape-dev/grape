# Issue #3354 方案设计

## 设计原则

把「VCS 刚建仓未就绪」视为可退避消化的时序错误，与真实权限缺失区分：

| 决策 | 选择 | 理由 |
|------|------|------|
| 重试位置 | 建仓接口内部同步重试 | 用户无感知，CLI/前端均受益 |
| 重试策略 | 3 次重试，5s/15s/45s 退避 | 覆盖 VCS 就绪窗口，与需求建议一致 |
| 可重试错误范围 | 仅 `repo_not_found` | 权限/限流/参数类盲重试无意义，避免误伤 |
| 终态失败落库 | `host_status=webhook_missing` | webhook 未建立，dashboard 语义一致 |
| 失败原因 | `_host_error_text(code)` + 「已重试 N 次」 | 可读、可观测、不含 token/内部 URL |
| 幂等 | find 复用已有 webhook，setup 建缺失 webhook | 与 `grape repos host` 同一语义 |

## 实现路径

1. `src/domains/mgmt/routes/repos.py`
   - 新增 `_HOST_AFTER_CREATE_RETRY_DELAYS=(5,15,45)`、
     `_HOST_AFTER_CREATE_RETRYABLE=("repo_not_found",)` 与
     `_host_after_create_sleep()`（独立函数便于测试注入，避免真实等待）；
   - `create_new_repo` 托管分支包在 `for` 重试循环内：单次失败按
     `_classify_host_error` 归类，命中可重试码且仍有退避额度才 sleep 后重试；
   - 终态失败：`host_error=f"{原因}（已重试 N 次）"`，`host_status=webhook_missing`，
     `host_step=create_webhook`，返回值携带 `host_error` / `retry_count`；
   - 重试后成功：清空历史 `host_status` 诊断，返回 `webhook_id`。
2. `src/domains/insight/routes/dashboard.py`
   - 响应增加 `host_step` / `host_error`，供前端展示失败原因。
3. `src/api/frontend/app/src/views/DashboardView.vue`
   - `hostBanner` 失败态附带 `host_error` 文案，按钮改为「托管失败，点击重试」；
   - 一键重试复用幂等 `POST /repos/{owner}/{repo}/repair-host`。

## 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 同步重试拉长建仓响应（最长约 65s） | 中 | 仅对时序错误重试；非时序错误立即失败 |
| 误伤真实权限缺失 | 中 | 仅 `repo_not_found` 重试，403 快速失败 |
| 前端重试与后端重试并发 | 低 | repair-host/托管均为幂等实现 |