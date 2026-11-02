# Plan: Issue #205 — 内测申请弹窗防误关闭 + 表单数据持久化

## Task List
- [x] Task 1: app.js — 移除遮罩点击关闭逻辑
- [x] Task 2: app.js — 新增 sessionStorage 收集/保存/回填/清空辅助函数
- [x] Task 3: app.js — openBetaApply 创建后回填 + 事件委托保存 + baSelectIdentity 保存
- [x] Task 4: app.js — submitBetaApply 成功后清空 sessionStorage
- [x] Task 5: beta-apply.html — 页面加载回填 + 事件委托保存 + selectIdentity 保存 + 提交成功清空
- [x] Task 6: 测试 tests/js/test_issue_205_beta_apply_persist.js

## 风险
- 语言切换后 sessionStorage 中的中文选项值与英文 DOM 不匹配 → 回填静默跳过（不崩溃），简单字段仍回填。
- sessionStorage 不可用（隐私模式）→ try/catch 包裹，降级为无持久化，不影响表单正常使用。
