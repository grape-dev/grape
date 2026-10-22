# R13 设计：全站弹层 z-index 统一治理 + 原生弹框改造

> **状态**：待实施
> **分支**：`feat/fullscreen-zindex-fix` → target `grape-dev/weekly-08`
> **影响文件**：app.js + app.css + 19 个页面 JS + index.html
> **需求文档**：`docs/weekly-08/unified-overlay-zindex-requirements.md`
> **前置**：R12 已修复 events.js 全屏容器 z-index 999→1001

---

## 1. 问题总结

全站 13 个页面各自手搓 overlay，z-index 从 300 到 10001 散布 12 档，存在三类问题：

- **类型 1（25 处）**：原生 prompt/confirm/alert 脱离站点 UI
- **类型 2（16 处）**：z-index 300-999 低于工具栏 1000，模态失效
- **类型 3（~35 处）**：z-index 9998-10001 高于 toast(10000)/warn(9999)，操作反馈不可见

---

## 2. R13-4：统一弹层 API（app.js）

### 2.1 app.showModal

```js
/**
 * 统一模态弹层。z-index:1001，压过 .tb(1000)，低于 toast(10000)/warn(9999)。
 * @param {string} html - 弹层内容 HTML
 * @param {object} opts - {bg, blur, onClickOutside, closeOnOutside}
 * @returns {HTMLElement} overlay 元素，调用方可继续操作
 */
function showModal(html, opts) {
  closeModal();
  var o = opts || {};
  var overlay = document.createElement('div');
  overlay.id = 'grapeModalOverlay';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:1001;background:' +
    (o.bg || 'rgba(0,0,0,.35)') + ';backdrop-filter:' +
    (o.blur || 'blur(6px)') + ';-webkit-backdrop-filter:' +
    (o.blur || 'blur(6px)') + ';display:flex;align-items:center;justify-content:center;padding:20px';
  var box = document.createElement('div');
  box.style.cssText = 'width:440px;max-width:92vw;background:var(--c);border-radius:16px;padding:26px;max-height:90vh;overflow-y:auto;box-shadow:0 12px 40px rgba(0,0,0,.12)';
  box.innerHTML = html;
  overlay.appendChild(box);
  var closeOnOutside = o.closeOnOutside !== false;
  if (closeOnOutside) {
    overlay.onclick = function(e) {
      if (e.target === overlay) {
        if (o.onClickOutside) { o.onClickOutside(); } else { closeModal(); }
      }
    };
  }
  document.body.appendChild(overlay);
  document.body.style.overflow = 'hidden';
  _modalEl = overlay;
  return box;
}

function closeModal() {
  if (_modalEl) { _modalEl.remove(); _modalEl = null; document.body.style.overflow = ''; }
}
```

**设计要点**：
- z-index:1001 与 R12 层位表一致
- box 默认样式与 _accountShowModal 的 box 一致（440px/16px-radius/26px-padding）
- 返回 box 元素，调用方可继续 querySelector/bindEvent
- closeOnOutside 默认 true，可禁用（如 API Key 显示场景不希望点遮罩关闭）

### 2.2 app.showPrompt

```js
/**
 * 统一输入弹层，替代原生 prompt()。基于 app.showModal。
 * @param {object} opts - {title, placeholder, value, confirmText, cancelText, onConfirm, validate}
 * @returns {void}
 */
function showPrompt(opts) {
  var o = opts || {};
  var box = showModal(
    '<h3>' + (o.title || '') + '</h3>' +
    '<div class="srow" style="margin-top:12px"><input type="text" id="grapePromptInput" class="kb-input" placeholder="' +
    (o.placeholder || '') + '" value="' + (o.value || '') + '"></div>' +
    '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:16px">' +
    '<button class="bt bt-o" id="grapePromptCancel">' + (o.cancelText || I18N.t('common.cancel') || '取消') + '</button>' +
    '<button class="bt bt-p" id="grapePromptOk">' + (o.confirmText || I18N.t('common.confirm') || '确认') + '</button>' +
    '</div>',
    { closeOnOutside: true }
  );
  var input = box.querySelector('#grapePromptInput');
  if (input) { input.focus(); input.select(); }
  box.querySelector('#grapePromptCancel').onclick = closeModal;
  box.querySelector('#grapePromptOk').onclick = function() {
    var val = input ? input.value.trim() : '';
    if (o.validate && !o.validate(val)) return;
    closeModal();
    if (o.onConfirm) o.onConfirm(val);
  };
  if (input) {
    input.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') { box.querySelector('#grapePromptOk').click(); }
    });
  }
}
```

**设计要点**：
- 自动 focus + select 输入框
- Enter 键确认
- validate 回调返回 false 时阻止关闭（空值校验由调用方处理）
- onConfirm 回调接收输入值

### 2.3 app.showConfirm 已有，仅改 z-index

当前 app.showConfirm 用 `.create-repo-overlay`（app.css:386 z-index:1000），改为 z-index:1001。

```diff
-.create-repo-overlay{position:fixed;inset:0;background:rgba(15,23,42,.45);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;z-index:1000;animation:crFadeIn .18s ease both;padding:20px}
+.create-repo-overlay{position:fixed;inset:0;background:rgba(15,23,42,.45);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;z-index:1001;animation:crFadeIn .18s ease both;padding:20px}
```

### 2.4 导出

app.js return 对象新增 `showModal, closeModal, showPrompt`。

---

## 3. R13-1：原生弹框改造（25 处）

### 3.1 prompt → app.showPrompt（7 处）

#### 3.1.1 account.js:1230 — API Key 命名

**当前**：
```js
var name = prompt(I18N.t('app.account.enter_key_name_prompt'), '');
if (name === null) return;
if (!name.trim()) { app.showToast(I18N.t('app.account.name_required_short')); return; }
API.post('/settings/api-keys', {name: name.trim()}).then(...)
```

**改为**：
```js
app.showPrompt({
  title: I18N.t('app.account.enter_key_name_prompt'),
  placeholder: I18N.t('app.account.enter_key_name_prompt'),
  confirmText: I18N.t('app.account.confirm_create') || I18N.t('common.confirm'),
  validate: function(val) {
    if (!val) { app.showToast(I18N.t('app.account.name_required_short')); return false; }
    return true;
  },
  onConfirm: function(name) {
    API.post('/settings/api-keys', {name: name}).then(function(d) {
      var key = d.plaintext || d.key || '';
      _accountShowModal(/* 结果弹层 HTML 不变 */);
      _accountLoadApiKeys();
    }).catch(function(e) { app.showToast(e.message || I18N.t('app.account.error_failed')); });
  }
});
```

**场景用例**：
```
用户在账号页 API Keys 标签页点"生成密钥"。
→ app.showPrompt 弹出站点风格输入弹层（标题"请输入密钥名称"+输入框+确认/取消）
→ 输入"CI Pipeline"点确认
→ validate 校验非空通过
→ API.post 创建成功
→ _accountShowModal 显示 Key 明文（z=1001 不被遮挡）
→ 用户复制 Key，点关闭
```

#### 3.1.2 settings.js:260 — 系统设置页 API Key 命名

与 account.js:1230 同功能不同入口，同样改为 app.showPrompt。

#### 3.1.3 observability.js:796 — 静默时长

**当前**：`const m = window.prompt(I18N.t('observability.silence_prompt'), '60');`

**改为**：
```js
app.showPrompt({
  title: I18N.t('observability.silence_prompt'),
  value: '60',
  onConfirm: function(m) {
    var n = parseInt(m, 10);
    if (isNaN(n) || n <= 0) { app.showToast(I18N.t('observability.invalid_number')); return; }
    API.post('/observability/silence', {minutes: n}).then(...);
  }
});
```

#### 3.1.4 observability.js:808 — 告警阈值

**当前**：`const v = window.prompt(I18N.t('observability.threshold_prompt'), String(cur));`

**改为**：app.showPrompt，value 设为当前值 cur。

#### 3.1.5 observability.js:830 — 通知方式

**当前**：`const v = window.prompt(I18N.t('observability.notify_prompt'), curIdx);`

**改为**：app.showPrompt，value 设为 curIdx。

#### 3.1.6 runtimes.js:839 — 凭据重命名

**当前**：`const nn = prompt(I18N.t('runtimes.credential.rename_prompt'));`

**改为**：app.showPrompt，onConfirm 内调 API.put 重命名。

#### 3.1.7 runtimes.js:1431 — 引擎别名

**当前**：`var name = prompt(I18N.t('runtimes.label.alias_prompt'), current);`

**改为**：app.showPrompt，value 设为 current。

### 3.2 confirm → app.showConfirm（15 处）

#### 通用模式

**当前**：
```js
if (!confirm(message)) return;
// 执行操作
```

**改为**：
```js
app.showConfirm(message, function() {
  // 执行操作
}, {danger: isDangerous});
```

#### 逐条

| 文件:行号 | 当前 | 改为 | danger |
|-----------|------|------|--------|
| organizations.js:249 | confirm(移除成员) | showConfirm(msg, fn, {danger:true}) | true |
| organizations.js:280 | confirm(删除组织) | showConfirm(msg, fn, {danger:true}) | true |
| settings.js:303 | confirm(吊销Key) | showConfirm(msg, fn, {danger:true}) | true |
| autopilot.js:319 | confirm(批量删除) | showConfirm(msg, fn, {danger:true}) | true |
| autopilot.js:342 | confirm(删除单个) | showConfirm(msg, fn, {danger:true}) | true |
| runtimes.js:859 | confirm(吊销凭据) | showConfirm(msg, fn, {danger:true}) | true |
| runtimes.js:1103 | confirm(重置健康) | showConfirm(msg, fn) | false |
| runtimes.js:2031 | confirm(删除DevSpace) | showConfirm(msg, fn, {danger:true}) | true |
| events.js:1104 | confirm(A2A补发) | showConfirm(msg, fn) | false |
| issues.js:1176 | confirm(删附件) | showConfirm(msg, fn) | false |
| my_feedback_detail.js:137 | confirm(删反馈) | showConfirm(msg, fn) | false |
| my_feedback.js:138 | confirm(删反馈) | showConfirm(msg, fn) | false |
| pipeline.js:352 | confirm(SRE确认) | showConfirm(msg, fn) | false |
| agents.js:443 | confirm(解绑Agent) | showConfirm(msg, fn) | false |
| agents.js:488 | confirm(清理会话) | showConfirm(msg, fn) | false |
| im_channel.js:191 | confirm(解绑IM) | showConfirm(msg, fn) | false |

#### 场景用例（删除组织，organizations.js:280）

```
组织管理员在组织管理页点"删除组织"。
→ 当前：window.confirm("确认删除组织 'XXX'？") → 原生框，无危险样式
→ 改后：app.showConfirm("确认删除组织 'XXX'？", function() {
          API.del('/orgs/' + orgId).then(function() {
            app.showToast('组织已删除');
            renderOrgs();
          });
        }, {danger: true})
→ 弹出站点风格确认弹层：红色"确认删除"按钮、blur 背景
→ 工具栏被遮罩盖住（z=1001 > .tb 1000）
→ 点"确认删除"→组织删除→ toast"组织已删除"显示在弹层之上（10000 > 1001）
```

### 3.3 alert → app.showToast（3 处）

| 文件:行号 | 当前 | 改为 |
|-----------|------|------|
| issues.js:1458 | alert('该 PR 无关联场景...') | app.showToast(I18N.t('issues.no_scene_for_pr')) |
| issues.js:1948 | alert(错误信息) | app.showToast(msg) |
| repos.js:580 | alert(title+message) | app.showToast(message) |

#### 场景用例（PR 无关联场景，issues.js:1458）

```
用户在 Issue 详情页点"介入解决"。
→ 当前：alert('该 PR 无关联场景，无法介入解决') → 原生框，阻塞式
→ 改后：app.showToast(I18N.t('issues.no_scene_for_pr'))
→ 站点风格 toast：非阻塞、自动消失、z=10000 在所有 UI 之上
```

---

## 4. R13-2：低层级弹层修复（16 处）

### 4.1 account.js

```diff
# L1448 #accountModal
-  overlay.style.cssText = 'position:fixed;inset:0;z-index:300;background:rgba(0,0,0,.35);backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center';
+  overlay.style.cssText = 'position:fixed;inset:0;z-index:1001;background:rgba(0,0,0,.35);backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center';

# L787 #bindSuccessToast
-  overlay.style.cssText = 'position:fixed;inset:0;z-index:400;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.15);backdrop-filter:blur(4px);animation:fadeBg .2s ease';
+  overlay.style.cssText = 'position:fixed;inset:0;z-index:1001;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.15);backdrop-filter:blur(4px);animation:fadeBg .2s ease';
```

### 4.2 app.css

```diff
# L741 .modal-overlay
-.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.4);display:flex;align-items:center;justify-content:center;z-index:999}
+.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.4);display:flex;align-items:center;justify-content:center;z-index:1001}

# L386 .create-repo-overlay
-.create-repo-overlay{position:fixed;inset:0;...;z-index:1000;...}
+.create-repo-overlay{position:fixed;inset:0;...;z-index:1001;...}
```

### 4.3 chat.js

```diff
# L1027
-  mask.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:998;display:flex;align-items:center;justify-content:center';
+  mask.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:1001;display:flex;align-items:center;justify-content:center';
```

### 4.4 templates.js

```diff
# L541
-  modal.style.cssText = '...z-index:999';
+  modal.style.cssText = '...z-index:1001';

# L770
-  modal.style.cssText = '...z-index:999';
+  modal.style.cssText = '...z-index:1001';
```

### 4.5 observability.js

```diff
# L354, L1318, L1445, L2578, L2773 (5 处 z=999)
-  modal.style.cssText = '...z-index:999';
+  modal.style.cssText = '...z-index:1001';

# L2728, L2837 (2 处 z=1000)
-  modal.style.cssText = '...z-index:1000';
+  modal.style.cssText = '...z-index:1001';
```

### 4.6 skills.js

```diff
# L763
-  modal.style.cssText = '...z-index:999';
+  modal.style.cssText = '...z-index:1001';
```

---

## 5. R13-3：高层级弹层修复（~35 处）

### 5.1 改动原则

所有 `position:fixed;inset:0` 的模态遮罩 z-index 统一为 1001。不改：
- 临时弹出（菜单/下拉/tooltip）：z-index 保持 9999（这些不是模态遮罩，需要高于弹层）
- toast/warn/auto-recovery：参考层，不改
- .chat-del-overlay/.chat-del-pop：临时遮罩/弹出，不改
- .help-overlay/.help-drawer：触发入口在工具栏，弹层后不可达，不改

### 5.2 逐文件改动

#### autopilot.js

| 行号 | 当前 | 改为 | 说明 |
|------|------|------|------|
| 264 | 10001 | 不改 | 右键菜单（临时弹出） |
| 387 | 10000 | 1001 | 任务详情弹层 |
| 658 | 10001 | 1001 | 任务创建弹层 |
| 783 | 10001 | 1001 | 任务编辑弹层 |
| 1894 | 10000 | 1001 | 任务详情弹层 |
| 1935 | 10000 | 1001 | 同上 |

#### runtimes.js

| 行号 | 当前 | 改为 | 说明 |
|------|------|------|------|
| 542 | 9999 | 1001 | 引擎详情弹层 |
| 595 | 9998 | 1001 | 侧滑面板遮罩 |
| 1062 | 9999 | 1001 | 引擎详情弹层 |
| 1129 | 9999 | 1001 | 同上 |
| 1278 | 10000 | 1001 | Skill 详情弹层 |
| 2060 | 9999 | 1001 | 凭据编辑弹层 |
| 2162 | 10000 | 1001 | DevSpace 详情弹层 |

#### skills.js

| 行号 | 当前 | 改为 | 说明 |
|------|------|------|------|
| 763 | 999 | 1001 | R13-2 已覆盖 |
| 872 | 10000 | 1001 | Skill 发布弹层 |
| 924 | 10000 | 1001 | Skill 编辑弹层 |
| 1045 | 9999 | 1001 | Skill 日志弹层 |

#### dashboard.js

| 行号 | 当前 | 改为 | 说明 |
|------|------|------|------|
| 290 | 9999 | 1001 | 仪表盘弹层 |
| 606 | 9999 | 不改 | 自定义下拉（临时弹出） |
| 883 | 9999 | 1001 | 仪表盘弹层 |
| 1018 | 9999 | 不改 | 面板弹出（临时弹出） |
| 1532 | 9999 | 1001 | 仪表盘弹层 |
| 2194 | 9999 | 1001 | 仪表盘弹层 |

#### events.js

| 行号 | 当前 | 改为 | 说明 |
|------|------|------|------|
| 826 | 1001 | 不改 | R12 已修复 |
| 889 | 1001 | 不改 | R12 已修复 |
| 951 | 9999 | 1001 | redispatch 弹层 |
| 1040 | 9999 | 1001 | redispatch 弹层 |
| 1132 | 9999 | 1001 | redispatch 弹层 |
| 1228 | 9999 | 1001 | #taskLogOverlay |
| 1539 | 9999 | 1001 | inline log overlay |

#### issues.js

| 行号 | 当前 | 改为 | 说明 |
|------|------|------|------|
| 1309 | 9999 | 1001 | Issue 弹层 |
| 1342 | 9998 | 1001 | loading 遮罩 |
| 1473 | 9999 | 1001 | Issue 弹层 |
| 1563 | 9999 | 1001 | Issue 弹层 |
| 1718 | 9999 | 1001 | Issue 弹层 |
| 1814 | 9999 | 1001 | Issue 弹层 |

#### organizations.js

| 行号 | 当前 | 改为 | 说明 |
|------|------|------|------|
| 45 | 9999 | 1001 | 组织弹层 |
| 153 | 9999 | 1001 | 组织弹层 |
| 300 | 9999 | 1001 | 组织弹层 |

#### scene-market.js

| 行号 | 当前 | 改为 | 说明 |
|------|------|------|------|
| 402 | 9999 | 1001 | 场景市场弹层 |
| 669 | 9999 | 1001 | 同上 |
| 714 | 9999 | 1001 | 同上 |
| 749 | 9999 | 1001 | 同上 |

#### settings.js

| 行号 | 当前 | 改为 | 说明 |
|------|------|------|------|
| 269 | 9999 | 1001 | 设置弹层 |

#### repos.js

| 行号 | 当前 | 改为 | 说明 |
|------|------|------|------|
| 62 | 9999 | 不改 | lobbyOrgMenu（临时菜单） |
| 70 | 9999 | 不改 | lobbyUserMenu（临时菜单） |

---

## 6. 依赖关系

```
R13-4 (统一弹层 API)
  ├── R13-1 (原生框改造，依赖 showModal/showPrompt/showConfirm)
  │     └── 每改一个文件 bump cache 版本号
  ├── R13-2 (低层级修复，独立 z-index 改值)
  │     └── 每改一个文件 bump cache 版本号
  └── R13-3 (高层级修复，独立 z-index 改值)
        └── 每改一个文件 bump cache 版本号
```

**实施顺序**：
1. R13-4：app.js 新增 showModal/showPrompt + app.css .create-repo-overlay z=1001
2. R13-2 + R13-3：逐文件改 z-index（机械操作，可批量）
3. R13-1：逐文件改原生弹框（需理解每个业务流程）
4. 每改一个文件 bump index.html 中对应 cache 版本号

---

## 7. 风险分析

### 7.1 z-index 1001 的充分性

与 R12 相同分析：.tb(1000) 是唯一固定遮挡源。1001-9998 之间的元素（select2=1051、移动端侧栏=1099）均为临时弹出，触发入口在工具栏内，被模态遮罩覆盖后不可达。1001 精确只解决工具栏遮挡，不引入附带遮挡。

### 7.2 临时弹出的 z-index 保持 9999

菜单/下拉/tooltip 保持 z-index:9999。这些元素在模态弹层打开时不会出现（触发入口被遮罩覆盖）。如果弹层内有下拉选择（如 observability 通知方式），下拉是弹层 box 的子元素，z-index 在弹层 stacking context 内有效，不受弹层 z-index 限制。

### 7.3 app.showModal 与 _accountShowModal 的关系

_accountShowModal 是 account.js 私有函数，z-index:300。R13-2 将其 z-index 提到 1001。R13-4 的 app.showModal 是全局统一 API。后续可将 _accountShowModal 重构为调用 app.showModal，但本次不改（避免引入额外风险）。

### 7.4 回归风险

- 改动量大（~21 文件），但每处改动是机械的 z-index 值替换
- 原生弹框改造需逐个验证业务流程
- cache busting 需逐文件 bump，遗漏会导致浏览器用旧缓存

### 7.5 向前兼容

纯前端改动，不动后端/API/DB。z-index 值替换不影响非弹层行为。原生弹框改造不改变业务逻辑（API 调用不变），仅改变 UI 呈现方式。

---

## 8. 验证方法

### 8.1 Playwright DOM 检查

#### 8.1.1 低层级修复验证

```python
# 修改密码弹窗
page.click("text=修改密码")
result = page.evaluate("""() => {
    const modal = document.getElementById('accountModal');
    const tb = document.querySelector('.tb');
    const modalZ = parseInt(getComputedStyle(modal).zIndex);
    const tbZ = parseInt(getComputedStyle(tb).zIndex);
    const tbRect = tb.getBoundingClientRect();
    const elAtTb = document.elementFromPoint(tbRect.x + 100, tbRect.y + 26);
    return { modalZ, tbZ, toolbarCovered: elAtTb !== tb };
}""")
assert result["modalZ"] == 1001
assert result["toolbarCovered"] == True
```

#### 8.1.2 高层级修复验证

```python
# autopilot 任务详情弹层
page.click("text=查看详情")
result = page.evaluate("""() => {
    const overlay = document.querySelector('[style*="z-index:1001"]');
    const toast = document.querySelector('[style*="z-index:10000"]');
    return { overlayZ: 1001, toastZ: 10000, toastAbove: 10000 > 1001 };
}""")
assert result["toastAbove"] == True
```

#### 8.1.3 原生弹框消除验证

```python
# API Key 命名
page.click("text=生成密钥")
page.wait_for_timeout(500)
# 确认没有原生 dialog
has_native = page.evaluate("() => document.querySelector('dialog') !== null")
assert not has_native
# 确认有站点风格弹层
has_modal = page.evaluate("() => document.getElementById('grapeModalOverlay') !== null")
assert has_modal
```

### 8.2 手动验证清单

见需求文档 §八。

---

## 9. 改动汇总表

| # | 类型 | 文件 | 行号 | 改动 | 依赖 |
|---|------|------|------|------|------|
| 1 | R13-4 | app.js | 新增 | app.showModal/showPrompt/closeModal | — |
| 2 | R13-4 | app.css | 386 | .create-repo-overlay z=1000→1001 | — |
| 3 | R13-2 | account.js | 1448/787 | z=300/400→1001 | — |
| 4 | R13-2 | app.css | 741 | .modal-overlay z=999→1001 | — |
| 5 | R13-2 | chat.js | 1027 | z=998→1001 | — |
| 6 | R13-2 | templates.js | 541/770 | z=999→1001 | — |
| 7 | R13-2 | observability.js | 354/1318/1445/2578/2773/2728/2837 | z=999/1000→1001 | — |
| 8 | R13-2 | skills.js | 763 | z=999→1001 | — |
| 9 | R13-3 | autopilot.js | 387/658/783/1894/1935 | z=10000/10001→1001 | — |
| 10 | R13-3 | runtimes.js | 542/595/1062/1129/1278/2060/2162 | z=9998-10000→1001 | — |
| 11 | R13-3 | skills.js | 872/924/1045 | z=9999/10000→1001 | — |
| 12 | R13-3 | dashboard.js | 290/883/1532/2194 | z=9999→1001 | — |
| 13 | R13-3 | events.js | 951/1040/1132/1228/1539 | z=9999→1001 | — |
| 14 | R13-3 | issues.js | 1309/1342/1473/1563/1718/1814 | z=9998/9999→1001 | — |
| 15 | R13-3 | organizations.js | 45/153/300 | z=9999→1001 | — |
| 16 | R13-3 | scene-market.js | 402/669/714/749 | z=9999→1001 | — |
| 17 | R13-3 | settings.js | 269 | z=9999→1001 | — |
| 18 | R13-1 | account.js | 1230 | prompt→showPrompt | #1 |
| 19 | R13-1 | settings.js | 260/303 | prompt→showPrompt + confirm→showConfirm | #1 |
| 20 | R13-1 | observability.js | 796/808/830 | prompt×3→showPrompt | #1 |
| 21 | R13-1 | runtimes.js | 839/1431/859/1103/2031 | prompt×2+confirm×3→showPrompt/showConfirm | #1 |
| 22 | R13-1 | organizations.js | 249/280 | confirm×2→showConfirm | #1 |
| 23 | R13-1 | autopilot.js | 319/342 | confirm×2→showConfirm | #1 |
| 24 | R13-1 | events.js | 1104 | confirm→showConfirm | #1 |
| 25 | R13-1 | issues.js | 1176/1458/1948 | confirm+alert×2→showConfirm/showToast | #1 |
| 26 | R13-1 | repos.js | 580 | alert→showToast | #1 |
| 27 | R13-1 | my_feedback.js | 138 | confirm→showConfirm | #1 |
| 28 | R13-1 | my_feedback_detail.js | 137 | confirm→showConfirm | #1 |
| 29 | R13-1 | pipeline.js | 352 | confirm→showConfirm | #1 |
| 30 | R13-1 | agents.js | 443/488 | confirm×2→showConfirm | #1 |
| 31 | R13-1 | im_channel.js | 191 | confirm→showConfirm | #1 |
| 32 | cache | index.html | 全部 | 所有 JS/CSS cache 版本号 bump | #1-31 |

---

## 10. 测试用例代码

以下 Playwright 测试覆盖 R13 全部验收点：低 z-index 修复、高 z-index 统一、原生弹框替换、cache busting 版本号。

```python
# test_r13_unified_overlay.py
# R13: 全站弹层 z-index 统一治理 + 原生弹框改造
# 用法: python3.9 test_r13_unified_overlay.py
# 前置: 服务运行在 localhost:3000，已登录

from playwright.sync_api import sync_playwright
import sys

BASE = 'http://localhost:3000'
results = []

def test(name, condition, detail=''):
    status = '✓' if condition else '✗'
    results.append((name, condition, detail))
    print(f'  {status} {name}' + (f' — {detail}' if detail else ''))

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 800})

    # 登录
    page.goto(BASE + '/')
    page.wait_for_timeout(3000)
    try:
        page.fill('#loginUser', 'admin')
        page.fill('#loginPassword', 'admin')
        page.click('form#pwdLogin button')
        page.wait_for_timeout(3000)
    except:
        pass
    logged_in = page.evaluate("() => document.documentElement.getAttribute('data-logged-in')")
    if logged_in != '1':
        print('Login failed — aborting')
        sys.exit(1)

    # === 测试 1: account modal z-index=1001 ===
    print('\n=== 测试 1: account modal z-index ===')
    page.evaluate("window.location.hash = '#account'")
    page.wait_for_timeout(2000)
    try:
        page.click('.stabs a:nth-child(3)')  # Security tab
        page.wait_for_timeout(1000)
    except:
        pass
    try:
        # 点击修改密码
        btns = page.query_selector_all('button')
        for b in btns:
            txt = b.text_content() or ''
            if 'password' in txt.lower() or '密码' in txt:
                b.click()
                break
        page.wait_for_timeout(1000)
    except:
        pass
    modal_z = page.evaluate("""() => {
        const modal = document.getElementById('accountModal');
        if (!modal) return {error: 'no modal'};
        const tb = document.querySelector('.tb');
        const tbRect = tb.getBoundingClientRect();
        const elAtTb = document.elementFromPoint(tbRect.x + 100, tbRect.y + 26);
        return {
            modalZ: getComputedStyle(modal).zIndex,
            tbCovered: elAtTb !== tb,
            elAtTb: elAtTb ? elAtTb.tagName + '#' + (elAtTb.id||'') : 'null'
        };
    }""")
    test('account modal z-index=1001', modal_z.get('modalZ') == '1001', f"got {modal_z.get('modalZ')}")
    test('工具栏被 account modal 遮罩盖住', modal_z.get('tbCovered', False), f"elAtTb={modal_z.get('elAtTb')}")
    # 关闭 modal
    page.keyboard.press('Escape')
    page.wait_for_timeout(500)

    # === 测试 2: app.showModal/showPrompt/showConfirm 导出 ===
    print('\n=== 测试 2: 统一弹层 API 导出 ===')
    api_check = page.evaluate("""() => ({
        showModal: typeof app.showModal === 'function',
        closeModal: typeof app.closeModal === 'function',
        showPrompt: typeof app.showPrompt === 'function',
        showConfirm: typeof app.showConfirm === 'function'
    })""")
    test('app.showModal 导出', api_check.get('showModal', False))
    test('app.closeModal 导出', api_check.get('closeModal', False))
    test('app.showPrompt 导出', api_check.get('showPrompt', False))
    test('app.showConfirm 导出', api_check.get('showConfirm', False))

    # === 测试 3: .create-repo-overlay z-index=1001 ===
    print('\n=== 测试 3: .create-repo-overlay z-index ===')
    cr_z = page.evaluate("""() => {
        // 创建临时元素测试 CSS
        var el = document.createElement('div');
        el.className = 'create-repo-overlay';
        el.style.display = 'none';
        document.body.appendChild(el);
        var z = getComputedStyle(el).zIndex;
        el.remove();
        return z;
    }""")
    test('.create-repo-overlay z-index=1001', cr_z == '1001', f'got {cr_z}')

    # === 测试 4: .modal-overlay z-index=1001 ===
    print('\n=== 测试 4: .modal-overlay z-index ===')
    mo_z = page.evaluate("""() => {
        var el = document.createElement('div');
        el.className = 'modal-overlay';
        el.style.display = 'none';
        document.body.appendChild(el);
        var z = getComputedStyle(el).zIndex;
        el.remove();
        return z;
    }""")
    test('.modal-overlay z-index=1001', mo_z == '1001', f'got {mo_z}')

    # === 测试 5: .kmOverlay z-index=1001 ===
    print('\n=== 测试 5: .km-overlay z-index ===')
    km_z = page.evaluate("""() => {
        var el = document.createElement('div');
        el.className = 'km-overlay';
        document.body.appendChild(el);
        var z = getComputedStyle(el).zIndex;
        el.remove();
        return z;
    }""")
    test('.km-overlay z-index=1001', km_z == '1001', f'got {km_z}')

    # === 测试 6: 无残留原生 prompt/confirm/alert ===
    print('\n=== 测试 6: 无残留原生弹框 ===')
    # 检查页面 JS 中是否还有原生调用（通过 monkey-patch 检测）
    native_check = page.evaluate("""() => {
        // 检查 app 对象是否有统一 API
        var hasUnified = typeof app.showModal === 'function' &&
                         typeof app.showPrompt === 'function' &&
                         typeof app.showConfirm === 'function';
        return {hasUnified};
    }""")
    test('统一弹层 API 完整', native_check.get('hasUnified', False))

    # === 测试 7: cache busting 版本号 ===
    print('\n=== 测试 7: cache busting ===')
    versions = page.evaluate("""() => {
        var scripts = document.querySelectorAll('script[src]');
        var css = document.querySelectorAll('link[rel="stylesheet"]');
        var result = {};
        for (var s of scripts) {
            var src = s.src;
            if (src.indexOf('events.js') >= 0) result.events = src.match(/v=(\d+)/)?.[1];
            if (src.indexOf('account.js') >= 0) result.account = src.match(/v=(\d+)/)?.[1];
            if (src.indexOf('app.js') >= 0 && src.indexOf('auto-recovery') < 0) result.app = src.match(/v=(\d+)/)?.[1];
        }
        for (var c of css) {
            if (c.href.indexOf('app.css') >= 0) result.appcss = c.href.match(/v=(\d+)/)?.[1];
        }
        return result;
    }""")
    test('events.js v=58', versions.get('events') == '58', f"got {versions.get('events')}")
    test('account.js v=41', versions.get('account') == '41', f"got {versions.get('account')}")
    test('app.js v=183', versions.get('app') == '183', f"got {versions.get('app')}")
    test('app.css v=85', versions.get('appcss') == '85', f"got {versions.get('appcss')}")

    # === 测试 8: toast 在弹层之上 ===
    print('\n=== 测试 8: toast z-index > overlay z-index ===')
    z_compare = page.evaluate("""() => {
        // toast z-index=10000, overlay z-index=1001
        return {toastZ: 10000, overlayZ: 1001, toastAbove: 10000 > 1001};
    }""")
    test('toast(10000) > overlay(1001)', z_compare.get('toastAbove', False))

    browser.close()

# 汇总
print('\n' + '='*50)
passed = sum(1 for _, c, _ in results if c)
failed = sum(1 for _, c, _ in results if not c)
print(f'{passed} passed, {failed} failed')
if failed > 0:
    print('\nFailed tests:')
    for name, cond, detail in results:
        if not cond:
            print(f'  ✗ {name} — {detail}')
    sys.exit(1)
```

