# Spec: Issue #614 — 会话超时自动登出后凭据弹窗明文秘钥未清理

> 本文件为 Issue 侧已发布 Spec（[issue agent] Spec Document，评级 ready）的仓库内落地副本，权威版本见 Issue #614 评论。

## Objective
会话失效（超时自动登出 / 手动登出 / refresh_token 失败跳登录）时，前端统一逻辑中关闭或脱敏所有当前打开的凭据/秘钥展示弹窗（含 Agent 运行时设备凭据弹窗及同类明文敏感界面），消除明文秘钥在无人值守屏幕上的残留暴露。

## Scope
- In：`src/api/static/js/app.js` 新增 `_clearSensitiveModals()` 统一清理钩子；`showLogin()` 汇聚点调用；清空 runtime 凭据面板 DOM（`addDeviceCredArea` / `addDeviceDevArea` / `aiDevspacePanel`）；移除敏感弹窗（`sreAddDeviceOverlay` / `addDeviceOverlay` / `credModal_*`）+ z-index 兜底；新增 `tests/js/test_issue_614_session_logout_cred_clear.js`。
- Out：后端 session/token 清理、凭据存储脱敏（#474 范围）、settings 页掩码凭据（已 ******** 掩码）。

## Acceptance Criteria
- 会话超时自动登出（refresh 失败 → showLogin）后不刷新页面：`grapeModalOverlay` 不存在、`addDeviceCredArea` innerHTML 为空。
- 手动登出（logout → showLogin）、401 无 retryFn（handle401 → showLogin）路径同样覆盖。
- 新增前端测试通过；现有 `tests/js/test_issue_*.js` 相关用例不回归。
