# Spec: Issue #290 — 需求中心「查看PR」入口统一改为「查看Issue」

## 目标
需求中心（Issue 卡片详情页）的操作入口按钮统一改为「查看Issue」：
- 任何流水线阶段（coding/qa/sre）下，入口均指向 `/issues/{N}`；
- 不再按 `t.is_pr` 切换 `/pull/` 链接（进入 QA/SRE 阶段关联 PR 事件后 `is_pr=true` 导致跳转错误）。

## 改动范围
- `src/api/static/js/pages/issues.js` 两处按钮：
  - L363（`_openBoardDetail` 看板详情页头部按钮）
  - L976（`_render` 列表视图右侧详情面板按钮）
  - URL：`_vcsUrl(repo_key, (is_pr?'/pull/':'/issues/')+N)` → `_vcsUrl(repo_key, '/issues/'+N)`
  - 文案：`${is_pr?'查看 PR':I18N.t('issues.detail.view_issue')}` → `${I18N.t('issues.detail.view_issue')}`
- `src/api/static/locales/{zh,en}/issues.json`：验证 `issues.detail.view_issue` 词条（已存在：zh「查看 Issue」/ en「View Issue」，预期零改动）。
- `tests/js/test_issue_290_view_issue.js`：新增回归测试。

## 明确不改（Out of Scope）
- `_openPRDetail`（PR 事件专属详情页，含「查看 PR ↗」按钮）保持不变。
- L1065 摘要行 `#N ↗` 链接的 is_pr 分支不改（保留跨 provider 语义）。
- 复制 modal 内 PR 链接预填（`openTaskCopyModal` L1381）不改。
- 后端 `src/api/issues_center.py` 的 `is_pr` 字段计算不改。

## 设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 入口指向 | 始终指向 Issue | Issue 页侧栏仍可查到关联 PR；入口语义稳定 |
| is_pr 字段 | 仅改前端消费点，不动后端 | `is_pr` 仍被 L1065 摘要行使用，最小改动降低回归风险 |
| 文案 | 统一 i18n key `issues.detail.view_issue` | 消除硬编码中文「查看 PR」，跨语言一致 |
| 测试 | Node 源码级断言（tests/js 现行模式） | 纯前端模板字符串改动，沿用仓库已有单测范式 |

## 依赖
- `_vcsUrl`（app.js L1746，支持 `gitcode:`/`github:` 前缀跨 provider）与 `I18N.t`（i18n.js）：均为现成依赖，无新增。

## 验收标准
- [ ] 详情页按钮显示「查看 Issue」(en: View Issue)，onclick 打开 `https://{gitcode|github}.com/{owner}/{repo}/issues/{N}`。
- [ ] coding/qa/sre 任一阶段（含 `is_pr=true` 卡片）入口均指向 Issue。
- [ ] `_openPRDetail` 的「查看 PR」按钮不受影响。
- [ ] 跨 provider（`github:` 前缀 repo_key）链接正确。
- [ ] zh/en 词条无残缺 key，两处无硬编码中文残留。
- [ ] 新增回归测试与现有测试全部通过。

## 风险与缓解
| 风险 | 影响 | 缓解 |
|------|------|------|
| 用户习惯从详情页直达 PR | 中 | Issue 页侧栏可达关联 PR；PR 事件卡片有专属详情页入口 |
| 误改其他 is_pr 消费点 | 低 | 改动严格限定 L363/L976 两处 |
| 跨仓库 A2A 场景 issue_number 实为 PR 编号 | 中 | Spec 已注明 Out of Scope，可后续在详情区补充 PR 链接行 |
