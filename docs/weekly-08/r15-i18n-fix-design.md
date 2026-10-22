# R15 设计：i18n 缺失 key 修复 + 中文硬编码国际化

> **状态**：待实施
> **分支**：`feat/fullscreen-zindex-fix`
> **需求文档**：`docs/weekly-08/i18n-fix-requirements.md`

---

## 1. 问题总结

### 1.1 || fallback 失效根因

i18n.js:421：
```js
if (val === undefined || val === null) return key;
```

缺失 key 返回 key 字符串本身（truthy），`I18N.t(k) || fallback` 永不触发。全站 513 处此模式，14 处 key 确实缺失。

### 1.2 缺失 key 清单（14 处）

| # | 文件 | key | 修复方式 |
|---|------|-----|----------|
| 1 | account.js:1245 | `app.account.confirm_create` | 补 key |
| 2 | templates.js:718 | `templates.progress_creating` | 补 key |
| 3 | templates.js:757 | `templates.fork_not_git_template` | 补 key |
| 4 | templates.js:781 | `templates.forking` | 补 key |
| 5 | templates.js:794/814 | `templates.progress_forking` | 补 key |
| 6 | templates.js:824 | `templates.fork_success_default` | 补 key |
| 7 | templates.js:836 | `templates.fork_exists` | 补 key |
| 8 | templates.js:838 | `templates.fork_no_permission` | 补 key |
| 9 | settings.js:24 | `errors.REPO_TOKEN_EXPIRED` | 改代码 `errors`→`app.error`（对齐已有 key） |
| 10 | app.js:1469 | `app.error.forbidden` | 补 key |
| 11 | dashboard.js:426 | `pipeline.no_issues` | 补 key |
| 12 | events.js:177 | `events.no_fail_reason` | 补 key |
| 13 | issues.js:373 | `dashboard.scene_detail` | 补 key |

---

## 2. R15-1：补缺失 locale key

### 2.1 新增 key（12 个）

#### app.json（2 个）

zh account 命名空间，在 `confirm_change` 后：
```json
"confirm_change": "确认修改",
"confirm_create": "确认创建",
```
en：
```json
"confirm_change": "Confirm",
"confirm_create": "Create",
```

zh error 命名空间：
```json
"forbidden": "无可访问的仓库",
```
en：
```json
"forbidden": "No accessible repository",
```

#### events.json（7 个：1 缺失 + 6 新增 a2a）

```json
"no_fail_reason": "暂无失败原因",
"a2a_select_agent": "请先选择目标 Agent",
"a2a_redo_confirm": "该 Agent 已在此前流程中执行完成。\n补发将重新执行整个阶段（消耗资源与时间），且不会影响已有结果。\n确定继续吗？",
"a2a_redo_skipped": "A2A 补发未执行：{reason}",
"a2a_redo_success": "A2A 补发成功，新事件 #{seq}",
"a2a_redo_failed": "A2A 补发失败：{msg}",
"a2a_redo_unknown": "未知原因"
```

en：
```json
"no_fail_reason": "No failure reason",
"a2a_select_agent": "Please select a target Agent first",
"a2a_redo_confirm": "This Agent has already completed.\nRedo will re-execute the entire stage.\nContinue?",
"a2a_redo_skipped": "A2A redo skipped: {reason}",
"a2a_redo_success": "A2A redo succeeded, new event #{seq}",
"a2a_redo_failed": "A2A redo failed: {msg}",
"a2a_redo_unknown": "Unknown error"
```

#### issues.json（5 个）

```json
"deploy_url_saved": "部署 URL 已保存",
"save_failed": "保存失败: {msg}",
"confirm_delete_attach": "确认删除附件？",
"upload_failed": "上传失败: {msg}",
"no_scene_for_pr": "该 PR 无关联场景，无法介入解决"
```

en：
```json
"deploy_url_saved": "Deploy URL saved",
"save_failed": "Save failed: {msg}",
"confirm_delete_attach": "Delete attachment?",
"upload_failed": "Upload failed: {msg}",
"no_scene_for_pr": "No associated scene for this PR, cannot intervene"
```

#### templates.json（7 个）

```json
"progress_creating": "正在创建仓库...",
"fork_not_git_template": "仅云端模板支持 Fork",
"forking": "Fork 中...",
"progress_forking": "正在 Fork 仓库...",
"fork_success_default": "Fork 成功",
"fork_exists": "⚠️ 仓库已存在（可能已 Fork 过该模板），可直接在仓库管理中托管",
"fork_no_permission": "⚠️ 无 Fork 权限，请检查 Token 权限设置"
```

en：
```json
"progress_creating": "Creating repository...",
"fork_not_git_template": "Only cloud templates support Fork",
"forking": "Forking...",
"progress_forking": "Forking repository...",
"fork_success_default": "Fork succeeded",
"fork_exists": "⚠️ Repository already exists (may have been forked), manage it in repo settings",
"fork_no_permission": "⚠️ No fork permission, please check token settings"
```

#### pipeline.json（1 个）

```json
"no_issues": "暂无 Issue"
```
en：
```json
"no_issues": "No issues"
```

#### dashboard.json（1 个）

```json
"scene_detail": "场景详情"
```
en：
```json
"scene_detail": "Scene Detail"
```

### 2.2 改代码对齐已有 key（1 处）

settings.js:24：
```diff
- (I18N.t('errors.REPO_TOKEN_EXPIRED') || 'Repo bot token expired')
+ I18N.t('app.error.REPO_TOKEN_EXPIRED')
```

app.json 已有 `error.REPO_TOKEN_EXPIRED`（单数 error），代码用 `errors`（复数）导致查不到。

**场景用例**：
```
用户仓库的 Bot Token 过期。
→ 当前：I18N.t('errors.REPO_TOKEN_EXPIRED') → pack.errors 不存在 → 返回 'errors.REPO_TOKEN_EXPIRED'
→ || 'Repo bot token expired' 不触发 → 用户看到 key 字符串
修复后：I18N.t('app.error.REPO_TOKEN_EXPIRED') → pack.app.error.REPO_TOKEN_EXPIRED → "仓库 Bot Token 已过期，请联系有权限的成员重新配置。"
```

---

## 3. R15-2：events.js 中文硬编码改为 I18N.t

### 3.1 替换清单

| 行号 | 当前 | 改为 |
|------|------|------|
| 1100 | `app.showToast('请先选择目标 Agent')` | `app.showToast(I18N.t('events.a2a_select_agent'))` |
| 1104 | `app.showConfirm('该 Agent 已在此前流程中执行完成。\n补发将...')` | `app.showConfirm(I18N.t('events.a2a_redo_confirm'), ...)` |
| 1109/1127 | `app.showToast('A2A 补发未执行：' + (_reasons[...] || ...))` | `app.showToast(I18N.t('events.a2a_redo_skipped', {reason: d.skipped}))` |
| 1111/1129 | `app.showToast('A2A 补发成功，新事件 #' + d.seq)` | `app.showToast(I18N.t('events.a2a_redo_success', {seq: d.seq}))` |
| 1114/1132 | `app.showToast('A2A 补发失败：' + (... \|\| '未知原因'))` | `app.showToast(I18N.t('events.a2a_redo_failed', {msg: (d&&d.error)\|\|(d&&d.message)\|\|I18N.t('events.a2a_redo_unknown')}))` |
| 1118/1136 | `app.showToast('A2A 补发失败：' + msg)` | `app.showToast(I18N.t('events.a2a_redo_failed', {msg: msg}))` |

### 3.2 _reasons 映射简化

当前代码用 _reasons 对象做中文映射（not_in_scene → '目标 Agent 不在当前场景内'）。改为直接用 d.skipped 原始枚举值，不做本地化映射——skipped reason 是技术枚举值，不翻译可接受。

---

## 4. R15-3：issues.js 中文硬编码改为 I18N.t

| 行号 | 当前 | 改为 |
|------|------|------|
| 216 | `app.showToast('部署 URL 已保存')` | `app.showToast(I18N.t('issues.deploy_url_saved'))` |
| 217 | `app.showToast('保存失败: ' + ...)` | `app.showToast(I18N.t('issues.save_failed', {msg: ...}))` |
| 1176 | `app.showConfirm('确认删除附件？', ...)` | `app.showConfirm(I18N.t('issues.confirm_delete_attach'), ...)` |
| 1204 | `app.showToast('上传失败: ' + d.error, ...)` | `app.showToast(I18N.t('issues.upload_failed', {msg: d.error}), ...)` |
| 1210 | `app.showToast('上传失败: ' + e.message, ...)` | `app.showToast(I18N.t('issues.upload_failed', {msg: e.message}), ...)` |
| 1459 | `app.showToast('该 PR 无关联场景，无法介入解决')` | `app.showToast(I18N.t('issues.no_scene_for_pr'))` |

---

## 5. R15-4：settings.js isZh 改为 I18N.t

18 处全部复用已有 account locale key。替换映射见需求文档 §3.4。

revoke_confirm 需传 {name} 参数，revoke_failed 需传 {msg} 参数。

---

## 6. 风险分析

### 6.1 || fallback 模式不改

513 处 `||` fallback 中 499 处 key 存在（暂时不踩雷）。本次只补 14 处缺失 key。根治 `||` 模式属编码规范问题，单独处理。

### 6.2 settings.js isZh 变量清理

替换完 18 处后 `var isZh = I18N.getLang() === 'zh'` 可能不再被引用。如果确认无其他引用则删除。

### 6.3 events.js _reasons 简化

去掉 _reasons 中文映射后，d.skipped 原始值（如 not_in_scene）直接显示在 toast 中。这是技术枚举值，对开发者可理解，对普通用户可接受（比看到 key 字符串好）。

---

## 7. 改动汇总表

| # | 文件 | 改动 | 新增 key |
|---|------|------|----------|
| 1 | zh/en app.json | 补 confirm_create + forbidden | 2 |
| 2 | zh/en events.json | 补 no_fail_reason + 6 个 a2a | 7 |
| 3 | zh/en issues.json | 补 5 个 | 5 |
| 4 | zh/en templates.json | 补 7 个 | 7 |
| 5 | zh/en pipeline.json | 补 no_issues | 1 |
| 6 | zh/en dashboard.json | 补 scene_detail | 1 |
| 7 | events.js | 10 处硬编码→I18N.t | — |
| 8 | issues.js | 6 处硬编码→I18N.t | — |
| 9 | settings.js | 18 处 isZh→I18N.t + L24 errors→app.error | — |
| 10 | index.html | cache busting | — |

新增 locale key 共 23 个（zh 23 + en 23 = 46 条）。

---

## 8. 测试用例代码

见需求文档 §六。
