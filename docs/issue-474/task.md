# Task: Issue #474

## TDD 顺序

- [ ] T1 编写 `tests/test_issue474_credentials_no_plaintext.py`（4 例：有值/无值/清除/codearts 不回退）
- [ ] T2 运行 → 有值 & 清除 & codearts 断言 RED（当前响应含明文 ak/sk 键）
- [ ] T3 修改 `src/api/routes/settings.py`：删除 `huawei_cloud.ak/sk` 2 键 + 更新 description
- [ ] T4 运行 → 4/4 GREEN + `TestSettings` 回归通过
- [ ] T5 修改 `src/api/static/js/pages/account.js`：掩码占位 + 移除 👁 + 删除 `_accountToggleSecret` + 注释更新
- [ ] T6 修改 `src/api/static/js/pages/scene-market.js`：去掉明文自动填入 + 提示语更新
- [ ] T7 自审查（5 轴：正确性/安全/性能/可维护/测试）+ 开源前置增量检查（黑名单拦截 + 敏感增量扫描）
- [ ] T8 原子提交（test 红 → feat 后端绿 → feat 前端）→ push → 建 PR（base=local-liufei）
- [ ] T9 Issue 评论告知 + A2A 通知 QA → `grape tasks complete`

## 提交拆分（默认红/绿）

1. `test(#474): 凭据接口不应回显明文 AK/SK（先红）` — tests/test_issue474_credentials_no_plaintext.py
2. `feat(#474): 凭据视图删除 huawei_cloud 明文 ak/sk，仅回存在性` — src/api/routes/settings.py
3. `feat(#474): 凭据页改掩码占位，华为云场景不再自动填入明文` — account.js + scene-market.js
4. `docs(#474): spec/plan/task` — docs/issue-474/*.md
## 方案A 追加（TDD 顺序）

- [ ] T10 `test(#474): reveal 接口先写失败测试`（二次确认/审计/当次返回/高频告警）✅
- [ ] T11 `feat(#474): 后端 reveal 接口`（audit EVENT_TYPES 增加 credential_reveal +
       credential_reveal_log 表 + POST /credentials/reveal 实现；含高频告警与 no-store）
- [ ] T12 `feat(#474): 前端 👁 保留并走 reveal 接口`（account.js + locales）
- [ ] T13 自审查（5 轴）+ 开源前置增量检查（黑名单拦截 + 敏感增量扫描）
- [ ] T14 push feat/issue-474 → PR #673 评论告知 → A2A 通知 QA → tasks complete
