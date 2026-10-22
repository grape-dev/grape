# i18n 缺失 key 修复 + 中文硬编码国际化

> **分支**：`feat/fullscreen-zindex-fix`
> **基线**：`grape-dev/weekly-08` (06ad2e106)
> **状态**：待实施
> **类型**：Bug 修复（i18n）

## 一、总览

### 1.1 背景

全站扫描发现 i18n.t() 对缺失 key 返回 key 字符串本身（i18n.js:421 `return key`），导致 `I18N.t(k) || fallback` 模式的 fallback 永不触发（key 字符串是 truthy）。全站 513 处此模式，其中 14 处 key 确实缺失，用户直接看到原始 key 字符串。另有 R13 改造中新增的 6 处中文硬编码和 settings.js 既有 18 处 isZh 三元表达式。

### 1.2 需求范围

| 编号 | 需求 | 类型 | 数量 |
|------|------|------|------|
| R15-1 | 补缺失 locale key（14 处） | Bug 修复 | 14 key × 2 语言 = 28 条 |
| R15-2 | events.js 中文硬编码改为 I18N.t | Bug 修复 | 10 处，新增 6 key |
| R15-3 | issues.js 中文硬编码改为 I18N.t | Bug 修复 | 6 处，新增 5 key |
| R15-4 | settings.js isZh 三元改为 I18N.t | 重构 | 18 处，复用已有 key |

### 1.3 不在范围

- 513 处 `||` fallback 模式不改（key 存在时无害；根治项属编码规范，单独处理）
- 后端 grape_auth.py 503 message 是英文，不改
- 注释中的中文不改

---

## 二、问题分析

### 2.1 || fallback 失效根因

i18n.js:421 实现：
```js
// Last resort: return the key itself
if (val === undefined || val === null) return key;
```

缺失 key 时返回 key 字符串本身（如 `'app.account.confirm_create'`），是 truthy。`||` 运算符只对 falsy 值触发 fallback，所以 `I18N.t('app.account.confirm_create') || 'fallback'` 永远返回 key 字符串。

### 2.2 缺失 key 清单（14 处）

| # | 文件 | 行号 | 引用的 key | 缺失原因 | 场景 |
|---|------|------|-----------|----------|------|
| 1 | account.js | 1245 | `app.account.confirm_create` | account 命名空间无此 key | API Key 创建确认按钮 |
| 2 | templates.js | 718 | `templates.progress_creating` | templates.json 无此 key | 模板创建进度提示 |
| 3 | templates.js | 757 | `templates.fork_not_git_template` | 同上 | 非云端模板 Fork 提示 |
| 4 | templates.js | 781 | `templates.forking` | 同上 | Fork 进行中提示 |
| 5 | templates.js | 794/814 | `templates.progress_forking` | 同上 | Fork 进度提示 |
| 6 | templates.js | 824 | `templates.fork_success_default` | 同上 | Fork 成功默认消息 |
| 7 | templates.js | 836 | `templates.fork_exists` | 同上 | 仓库已存在提示 |
| 8 | templates.js | 838 | `templates.fork_no_permission` | 同上 | 无 Fork 权限提示 |
| 9 | settings.js | 24 | `errors.REPO_TOKEN_EXPIRED` | app.json 有 `error.REPO_TOKEN_EXPIRED`（单数 error），代码用 `errors`（复数） | Token 过期提示 |
| 10 | app.js | 1469 | `app.error.forbidden` | app.json `error` 无 `forbidden` key | 403 无仓库权限提示 |
| 11 | dashboard.js | 426 | `pipeline.no_issues` | pipeline.json 无 `no_issues` | 流水线空态 |
| 12 | events.js | 177 | `events.no_fail_reason` | events.json 无 `no_fail_reason` | 失败原因空态 |
| 13 | issues.js | 373 | `dashboard.scene_detail` | dashboard.json 无 `scene_detail` | 场景详情标题 |

> 注：#9 的根因是单复数不匹配——app.json 有 `error.REPO_TOKEN_EXPIRED`（单数 error），代码引用 `errors.REPO_TOKEN_EXPIRED`（复数 errors）。修复方式：改代码用 `app.error.REPO_TOKEN_EXPIRED`（对齐已有 key），不新增 key。

### 2.3 中文硬编码清单

#### events.js（10 处）

| 行号 | 当前代码 | 场景用例 |
|------|----------|----------|
| 1100 | `app.showToast('请先选择目标 Agent')` | 用户未选 Agent 点补发 → toast 应显示翻译 |
| 1104 | `app.showConfirm('该 Agent 已在此前流程中执行完成。\n补发将...')` | 补发确认弹层 → 应显示翻译 |
| 1109/1127 | `app.showToast('A2A 补发未执行：' + ...)` | 补发跳过 → 应显示翻译 |
| 1111/1129 | `app.showToast('A2A 补发成功，新事件 #' + ...)` | 补发成功 → 应显示翻译 |
| 1114/1132 | `app.showToast('A2A 补发失败：' + ...)` | 补发失败 → 应显示翻译 |
| 1118/1136 | `app.showToast('A2A 补发失败：' + msg)` | 补发异常 → 应显示翻译 |

需要新增 6 个 events locale key。

#### issues.js（6 处）

| 行号 | 当前代码 | 场景用例 |
|------|----------|----------|
| 216 | `app.showToast('部署 URL 已保存')` | 保存部署 URL → toast 应显示翻译 |
| 217 | `app.showToast('保存失败: ' + ...)` | 保存失败 → 应显示翻译 |
| 1176 | `app.showConfirm('确认删除附件？', ...)` | 删除附件确认 → 应显示翻译 |
| 1204 | `app.showToast('上传失败: ' + d.error, ...)` | 上传失败 → 应显示翻译 |
| 1210 | `app.showToast('上传失败: ' + e.message, ...)` | 上传异常 → 应显示翻译 |
| 1459 | `app.showToast('该 PR 无关联场景，无法介入解决')` | PR 无场景 → 应显示翻译 |

需要新增 5 个 issues locale key。

### 2.4 settings.js isZh 三元（18 处）

全部有现成 account locale key 可复用，不需要新增 key。

---

## 三、修复方案

### 3.1 R15-1：补缺失 locale key（14 处）

#### 新增 key（12 个，zh/en 各 12 条）

**zh/en app.json** account 命名空间：
```json
"confirm_create": "确认创建" / "Create"
```

**zh/en app.json** error 命名空间：
```json
"forbidden": "无可访问的仓库" / "No accessible repository"
```

**zh/en events.json**：
```json
"no_fail_reason": "暂无失败原因" / "No failure reason"
```

**zh/en templates.json**（7 个）：
```json
"progress_creating": "正在创建仓库..." / "Creating repository..."
"fork_not_git_template": "仅云端模板支持 Fork" / "Only cloud templates support Fork"
"forking": "Fork 中..." / "Forking..."
"progress_forking": "正在 Fork 仓库..." / "Forking repository..."
"fork_success_default": "Fork 成功" / "Fork succeeded"
"fork_exists": "⚠️ 仓库已存在（可能已 Fork 过该模板），可直接在仓库管理中托管" / "⚠️ Repository already exists (may have been forked), manage it in repo settings"
"fork_no_permission": "⚠️ 无 Fork 权限，请检查 Token 权限设置" / "⚠️ No fork permission, please check token settings"
```

**zh/en pipeline.json**：
```json
"no_issues": "暂无 Issue" / "No issues"
```

**zh/en dashboard.json**：
```json
"scene_detail": "场景详情" / "Scene Detail"
```

#### 改代码对齐已有 key（2 处，不新增 key）

| 文件 | 行号 | 当前 | 改为 | 原因 |
|------|------|------|------|------|
| settings.js | 24 | `I18N.t('errors.REPO_TOKEN_EXPIRED')` | `I18N.t('app.error.REPO_TOKEN_EXPIRED')` | app.json 有 `error.REPO_TOKEN_EXPIRED`（单数），代码用 `errors`（复数） |
| issues.js | 373 | `I18N.t('dashboard.scene_detail')` | `I18N.t('dashboard.scene_detail')` | 补 key 到 dashboard.json（见上） |

> settings.js:24 的修复方式是改代码前缀 `errors` → `app.error`，对齐 app.json 已有的 `error.REPO_TOKEN_EXPIRED`，不新增 key。

### 3.2 R15-2：events.js 中文硬编码改为 I18N.t

新增 6 个 events locale key，替换 10 处硬编码。

**zh/en events.json** 新增：
```json
"a2a_select_agent": "请先选择目标 Agent" / "Please select a target Agent first"
"a2a_redo_confirm": "该 Agent 已在此前流程中执行完成。\n补发将重新执行整个阶段（消耗资源与时间），且不会影响已有结果。\n确定继续吗？" / "This Agent has already completed.\nRedo will re-execute the entire stage.\nContinue?"
"a2a_redo_skipped": "A2A 补发未执行：{reason}" / "A2A redo skipped: {reason}"
"a2a_redo_success": "A2A 补发成功，新事件 #{seq}" / "A2A redo succeeded, new event #{seq}"
"a2a_redo_failed": "A2A 补发失败：{msg}" / "A2A redo failed: {msg}"
"a2a_redo_unknown": "未知原因" / "Unknown error"
```

#### 场景用例（L1100）

```
用户在事件详情页未选择 Agent 就点"补发"。
→ 当前：toast 显示"请先选择目标 Agent"（中文硬编码，英文环境也是中文）
修复后：toast 显示 I18N.t('events.a2a_select_agent')
  中文环境："请先选择目标 Agent"
  英文环境："Please select a target Agent first"
```

### 3.3 R15-3：issues.js 中文硬编码改为 I18N.t

新增 5 个 issues locale key，替换 6 处硬编码。

**zh/en issues.json** 新增：
```json
"deploy_url_saved": "部署 URL 已保存" / "Deploy URL saved"
"save_failed": "保存失败: {msg}" / "Save failed: {msg}"
"confirm_delete_attach": "确认删除附件？" / "Delete attachment?"
"upload_failed": "上传失败: {msg}" / "Upload failed: {msg}"
"no_scene_for_pr": "该 PR 无关联场景，无法介入解决" / "No associated scene for this PR, cannot intervene"
```

#### 场景用例（L1176）

```
用户在 Issue 详情页点"删除附件"。
→ 当前：app.showConfirm('确认删除附件？')（中文硬编码）
修复后：app.showConfirm(I18N.t('issues.confirm_delete_attach'))
  中文环境："确认删除附件？"
  英文环境："Delete attachment?"
```

### 3.4 R15-4：settings.js isZh 改为 I18N.t

18 处 `isZh ? '中文' : 'English'` 改为 `I18N.t('app.account.xxx')`，全部复用已有 key：

| 行号 | 当前 | 改为 |
|------|------|------|
| 136 | `isZh ? '重新加载中…' : 'Reloading…'` | `I18N.t('app.account.reloading')` |
| 137 | `isZh ? '切换中…' : 'Switching…'` | `I18N.t('app.account.switching')` |
| 215 | `isZh ? '暂无 API Key...' : 'No API Keys...'` | `I18N.t('app.account.no_api_keys')` |
| 223 | `isZh ? '创建' : 'Created'` | `I18N.t('app.account.created')` |
| 224 | `isZh ? '最近使用' : 'Last used'` | `I18N.t('app.account.last_used')` |
| 228 | `isZh ? '吊销' : 'Revoke'` | `I18N.t('app.account.revoke')` |
| 236 | `isZh ? 'API Keys' : 'API Keys'` | `I18N.t('app.account.api_keys_title')` |
| 237 | `isZh ? '+ 生成新 Key' : '+ Generate Key'` | `I18N.t('app.account.generate_key')` |
| 250 | `isZh ? '加载失败: ' : 'Load failed: '` | `I18N.t('app.account.load_failed')` |
| 261 | `isZh ? '请输入...' : 'Enter...'` | `I18N.t('app.account.enter_key_name')` |
| 262 | `isZh ? 'API Key 名称' : 'API Key name'` | `I18N.t('app.account.enter_key_name')` |
| 265 | `isZh ? '名称不能为空' : 'Name cannot be empty'` | `I18N.t('app.account.name_required')` |
| 274 | `isZh ? 'API Key 已生成' : 'API Key Created'` | `I18N.t('app.account.api_key_created')` |
| 275 | `isZh ? '⚠ 明文...' : '⚠ Plaintext...'` | `I18N.t('app.account.api_key_warning')` |
| 279 | `isZh ? '复制' : 'Copy'` | `I18N.t('app.account.copy')` |
| 282 | `isZh ? '我已保存' : 'Saved'` | `I18N.t('app.account.saved')` |
| 291 | `isZh ? '生成失败: ' : 'Failed: '` | `I18N.t('app.account.generate_failed')` |
| 302 | `isZh ? '已复制到剪贴板' : 'Copied to clipboard'` | `I18N.t('app.account.copied')` |
| 307 | `isZh ? '确定吊销...' : 'Revoke...'` | `I18N.t('app.account.revoke_confirm', {name: name})` |
| 311 | `isZh ? '吊销失败: ' : 'Revoke failed: '` | `I18N.t('app.account.revoke_failed', {msg: e.message || e})` |

#### 场景用例（L237）

```
用户在系统设置页看到"生成新 Key"按钮。
→ 当前：isZh ? '+ 生成新 Key' : '+ Generate Key'（游离在 i18n 体系外，词条不可管理）
修复后：I18N.t('app.account.generate_key')
  中文环境："+ 生成新 Key"
  英文环境："+ Generate Key"
  词条纳入 locale JSON，可统一管理
```

### 3.5 cache busting

- events.js v=58→v=59
- issues.js v=116→v=117
- settings.js v=32→v=33
- account.js v=42→v=43
- templates.js v=37→v=38
- dashboard.js v=98→v=99
- app.js v=183→v=184
- i18n cache version 35→36

---

## 四、改动汇总

| # | 改动点 | 文件 | 新增 key |
|---|--------|------|----------|
| 1 | 补 account.confirm_create | zh/en app.json | 1 |
| 2 | 补 error.forbidden | zh/en app.json | 1 |
| 3 | 补 events.no_fail_reason | zh/en events.json | 1 |
| 4 | 新增 6 个 a2a key | zh/en events.json | 6 |
| 5 | 替换 10 处硬编码 | events.js | — |
| 6 | 新增 5 个 issues key | zh/en issues.json | 5 |
| 7 | 替换 6 处硬编码 | issues.js | — |
| 8 | 补 templates 7 个 key | zh/en templates.json | 7 |
| 9 | 补 pipeline.no_issues | zh/en pipeline.json | 1 |
| 10 | 补 dashboard.scene_detail | zh/en dashboard.json | 1 |
| 11 | 改 errors→app.error | settings.js L24 | 0（对齐已有） |
| 12 | 替换 18 处 isZh | settings.js | 0 |
| 13 | cache busting | index.html | — |

新增 locale key 共 23 个（zh 23 + en 23 = 46 条）。

---

## 五、验证场景

| # | 场景 | 期望行为 |
|---|------|----------|
| 1 | API Key 创建弹层确认按钮 | 显示"确认创建"/"Create"，非 key 字符串 |
| 2 | 模板 Fork 全流程提示 | 全部显示翻译文案，非 key 字符串 |
| 3 | Token 过期提示 | 显示翻译文案 |
| 4 | 403 无仓库权限 | 显示翻译文案 |
| 5 | 流水线空态 | 显示翻译文案 |
| 6 | 事件失败原因空态 | 显示翻译文案 |
| 7 | 场景详情标题 | 显示翻译文案 |
| 8 | A2A 补发全流程 | showToast/showConfirm 显示翻译 |
| 9 | Issue 删除附件/上传失败 | 显示翻译 |
| 10 | settings.js API Key 页 | 全部走 I18N.t，无 isZh 三元 |
| 11 | 英文环境全站 | 无中文硬编码残留 |

---

## 六、测试用例代码

```python
"""
R15: i18n 缺失 key 修复 + 中文硬编码国际化验证
用法: python3.9 tests/playwright/test_r15_i18n_fix.py
前置: 服务 localhost:3000，已登录
"""
import requests, sys, re, json

BASE = 'http://localhost:3000'
results = []

def test(name, condition, detail=''):
    results.append((name, condition, detail))
    print(f'  [{"PASS" if condition else "FAIL"}] {name}' + (f' — {detail}' if detail else ''))

# ===== R15-1: 缺失 key 补齐 =====
print('\n=== R15-1: 缺失 key ===')

zh_app = json.load(open('src/api/static/locales/zh/app.json'))
en_app = json.load(open('src/api/static/locales/en/app.json'))
test('zh account.confirm_create', 'confirm_create' in zh_app.get('account', {}))
test('en account.confirm_create', 'confirm_create' in en_app.get('account', {}))
test('zh error.forbidden', 'forbidden' in zh_app.get('error', {}))
test('en error.forbidden', 'forbidden' in en_app.get('error', {}))

zh_ev = json.load(open('src/api/static/locales/zh/events.json'))
en_ev = json.load(open('src/api/static/locales/en/events.json'))
test('zh events.no_fail_reason', 'no_fail_reason' in zh_ev)
test('en events.no_fail_reason', 'no_fail_reason' in en_ev)
for k in ['a2a_select_agent', 'a2a_redo_confirm', 'a2a_redo_skipped', 'a2a_redo_success', 'a2a_redo_failed', 'a2a_redo_unknown']:
    test(f'zh events.{k}', k in zh_ev)
    test(f'en events.{k}', k in en_ev)

zh_iss = json.load(open('src/api/static/locales/zh/issues.json'))
en_iss = json.load(open('src/api/static/locales/en/issues.json'))
for k in ['deploy_url_saved', 'save_failed', 'confirm_delete_attach', 'upload_failed', 'no_scene_for_pr']:
    test(f'zh issues.{k}', k in zh_iss)
    test(f'en issues.{k}', k in en_iss)

zh_tmpl = json.load(open('src/api/static/locales/zh/templates.json'))
en_tmpl = json.load(open('src/api/static/locales/en/templates.json'))
for k in ['progress_creating', 'fork_not_git_template', 'forking', 'progress_forking', 'fork_success_default', 'fork_exists', 'fork_no_permission']:
    test(f'zh templates.{k}', k in zh_tmpl)
    test(f'en templates.{k}', k in en_tmpl)

zh_p = json.load(open('src/api/static/locales/zh/pipeline.json'))
en_p = json.load(open('src/api/static/locales/en/pipeline.json'))
test('zh pipeline.no_issues', 'no_issues' in zh_p)
test('en pipeline.no_issues', 'no_issues' in en_p)

zh_d = json.load(open('src/api/static/locales/zh/dashboard.json'))
en_d = json.load(open('src/api/static/locales/en/dashboard.json'))
test('zh dashboard.scene_detail', 'scene_detail' in zh_d)
test('en dashboard.scene_detail', 'scene_detail' in en_d)

# ===== R15-2: events.js 无中文硬编码 =====
print('\n=== R15-2: events.js 硬编码 ===')
ev_src = requests.get(f'{BASE}/static/js/pages/events.js?v=59', timeout=10).text
# 查 showToast/showConfirm 后直接跟中文字符串（非 I18N.t）
hardcoded = []
for m in re.finditer(r"show(?:Toast|Confirm)\('([^']*[\u4e00-\u9fff][^']*)'", ev_src):
    if 'I18N.t' not in ev_src[max(0,m.start()-20):m.start()]:
        hardcoded.append(m.group(1)[:30])
test('events.js no Chinese hardcoded', len(hardcoded) == 0, f'found {len(hardcoded)}: {hardcoded[:3]}')

# ===== R15-3: issues.js 无中文硬编码 =====
print('\n=== R15-3: issues.js 硬编码 ===')
iss_src = requests.get(f'{BASE}/static/js/pages/issues.js?v=117', timeout=10).text
hardcoded = []
for m in re.finditer(r"show(?:Toast|Confirm)\('([^']*[\u4e00-\u9fff][^']*)'", iss_src):
    if 'I18N.t' not in iss_src[max(0,m.start()-20):m.start()]:
        hardcoded.append(m.group(1)[:30])
test('issues.js no Chinese hardcoded', len(hardcoded) == 0, f'found {len(hardcoded)}: {hardcoded[:3]}')

# ===== R15-4: settings.js 无 isZh 三元 =====
print('\n=== R15-4: settings.js isZh ===')
set_src = requests.get(f'{BASE}/static/js/pages/settings.js?v=33', timeout=10).text
iszh_count = set_src.count('isZh ?')
test('settings.js no isZh ternary', iszh_count == 0, f'found {iszh_count}')

# settings.js L24 errors→app.error 对齐
test('settings.js uses app.error.REPO_TOKEN_EXPIRED', 'app.error.REPO_TOKEN_EXPIRED' in set_src)

# ===== cache busting =====
print('\n=== cache busting ===')
html = requests.get(f'{BASE}/', timeout=10).text
for f, v in [('events.js', '59'), ('issues.js', '117'), ('settings.js', '33'), ('account.js', '43'),
             ('templates.js', '38'), ('dashboard.js', '99'), ('app.js', '184')]:
    test(f'{f} v={v}', f'{f}?v={v}' in html)

# ===== I18N.t() || fallback 不再踩雷 =====
print('\n=== || fallback 验证 ===')
# account.confirm_create 现在存在，|| 不会触发但也不需要触发
# 验证 I18N.t 返回的是翻译值不是 key
import requests as req
# 通过 Playwright 验证太重，改用源码检查
test('account.js confirm_create uses I18N.t', "I18N.t('app.account.confirm_create')" in acc_src if (acc_src := requests.get(f'{BASE}/static/js/pages/account.js?v=43', timeout=10).text) else False)

# 汇总
print('\n' + '=' * 50)
p = sum(1 for _, c, _ in results if c)
f = sum(1 for _, c, _ in results if not c)
print(f'{p}/{len(results)} passed, {f} failed')
if f:
    for n, c, d in results:
        if not c:
            print(f'  [FAIL] {n} — {d}')
    sys.exit(1)
print('\nALL PASSED')
```

## 七、设计文档

详细设计见同目录下 `r15-i18n-fix-design.md`。
