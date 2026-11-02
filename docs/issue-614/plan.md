# Plan: Issue #614 — showLogin 统一敏感弹窗清理钩子

## 方案概述
`app.showLogin()` 是所有登出/会话失效路径（`app.logout()` 手动登出、`api.js doRefresh()` 失败、`api.js handle401()` 无 retryFn）的最终汇聚点（app.js 既有注释已确认此收口语义，UC16e/#535 同点停轮询）。在其入口新增 `_clearSensitiveModals()` 钩子集中清理，避免逐点补丁。

## 实现路径（src/api/static/js/app.js）
1. 新增 `_clearSensitiveModals()`：
   - `closeModal()` 关闭统一弹层（showModal/showPrompt，`grapeModalOverlay`）。
   - `closeConfirm()` 关闭确认弹窗（`grapeConfirmOverlay`）。
   - 清空已知明文凭据面板并隐藏：`addDeviceCredArea`、`addDeviceDevArea`、`aiDevspacePanel`（runtimes.js 设备凭据/AI Devspace 区，token 面板一次性渲染、innerHTML 直含明文）。
   - 移除已知敏感弹窗：`sreAddDeviceOverlay`、`addDeviceOverlay`、`[id^="credModal_"]`（devspace 环境凭据弹窗，z-index:1001）。
   - 兜底：移除 `body` 直系子节点中 `position:fixed` 且 z-index ∈ [1000, 9000) 的遮罩层；上限取 9000 以避开 toast(9999/10000)、warn(9999)、帮助抽屉(9990/9991) 等非清理目标。
2. `showLogin()` 函数体首行调用 `_clearSensitiveModals()`；`app` 导出对象新增 `_clearSensitiveModals`。
3. runtimes.js 无需修改（面板 DOM 由钩子统一清理，与 Spec 一致）。

## 测试 seam
公开 seam = `app.showLogin()`（用户可见行为：会话失效回到登录视图时敏感弹窗关闭/秘钥清空）。测试以「从 app.js 源码提取函数体 + mock DOM 沙箱」方式（仓库既有先例：tests/js/test_issue_393_admin_confirm_entry.js）驱动 `showLogin` 真实函数体，断言全部敏感面清理。

## 测试设计（tests/js/test_issue_614_session_logout_cred_clear.js）
- mock DOM 含：`grapeModalOverlay`（含明文 token）、`grapeConfirmOverlay`、`credModal_env42`（含明文 token）、`sreAddDeviceOverlay`、z=1001 的未知 fixed 遮罩、`addDeviceCredArea`/`addDeviceDevArea`/`aiDevspacePanel`（innerHTML 含明文 token）；保留项：toast(9999)、顶栏菜单(10001)、帮助抽屉(9990)、非 fixed 元素。
- 断言：调 `showLogin()` 后上述敏感面全部关闭/清空，保留项不受影响；二次调用幂等；`app` 导出含 `_clearSensitiveModals`。

## 提交策略（TDD 红/绿）
1. `test(#614)`：先提交失败测试（当前 app.js 无钩子 → 红）。
2. `feat(#614)`：最小实现（钩子 + showLogin 调用 + 导出）→ 绿。
