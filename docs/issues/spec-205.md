# Spec: Issue #205 — 内测申请弹窗防误关闭 + 表单数据持久化

> 来源：上游 Issue Agent 已发布 Spec（Issue #205 评论），此处为开发落地说明。

## 目标
修复内测申请弹窗点击遮罩层即关闭导致已填表单数据丢失的问题。移除遮罩点击关闭逻辑，仅通过 × 按钮关闭；增加表单数据 sessionStorage 持久化，重新打开时自动回填。

## 范围
- `src/api/static/js/app.js` 中 `openBetaApply()`：移除 `overlay.addEventListener('click', ... overlay.remove())`；新增 sessionStorage 读写；`submitBetaApply()` 提交成功后清空。
- `src/api/static/beta-apply.html`：新增表单数据 sessionStorage 持久化（刷新后保留）。

## 技术方案
1. 删除 `openBetaApply()` 中 `if (e.target === overlay) overlay.remove()` 逻辑。
2. 新增模块级辅助函数 `_baCollectFormData / _baSaveForm / _baRestoreForm / _baClearForm`，存储键 `beta_apply_form`。
3. `openBetaApply()` 创建弹窗后调用 `_baRestoreForm()` 回填；在 overlay 上委托 `input`/`change` 事件触发 `_baSaveForm()`；`baSelectIdentity()` 末尾调用 `_baSaveForm()`（身份选择不触发 input/change）。
4. `submitBetaApply()` 提交成功回调中调用 `_baClearForm()` 后再移除弹窗。
5. `beta-apply.html` 同步实现：页面加载时回填、表单 `input`/`change` 委托保存、`selectIdentity()` 末尾保存、提交成功后清空。

## 验收标准
- 点击遮罩不关闭弹窗
- × 关闭后重新打开已填信息保留
- 提交成功后关闭并清空数据
- beta-apply.html 刷新后信息保留
- 中英文双语行为一致
