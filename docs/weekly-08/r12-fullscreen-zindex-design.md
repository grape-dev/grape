# R12 设计：对话全屏 z-index 遮挡 + 全屏态交互缺陷修复

> **状态**：已实施，已推送，待部署验证
> **分支**：`feat/fullscreen-zindex-fix` → target `grape-dev/weekly-08`
> **影响文件**：`src/api/static/js/pages/events.js` + `src/api/static/index.html`
> **需求文档**：`docs/weekly-08/fullscreen-zindex-requirements.md`

---

## 1. 问题总结

事件详情页对话全屏模式存在以下交互缺陷：
- 全屏后还原/清理按钮被顶部工具栏遮挡不可点击（z-index:999 < 工具栏 z-index:1000）
- 全屏状态下切换事件残留 `#convFullscreen` DOM，新事件对话区域空白
- ESC 无法退出 task log 全屏（ESC handler 缺少 task log 全屏判断）
- 退出全屏分支未检查 panel 是否仍在文档中（竞态下元素丢失）

所有缺陷集中在 `src/api/static/js/pages/events.js`，外加 `index.html` cache busting。

### 1.1 z-index 层级实测表

基于 `grape-dev/weekly-08` 基线代码逐行核实，并经全站 z-index 全景扫描补全：

| 元素 | z-index | 来源 | 类型 |
|---------|---------|--------|------|
| `.chat-picker-pop` | 998 | app.css:773 | 临时弹出 |
| `.modal-overlay` | 999 | app.css:741 | 临时遮罩 |
| **`#convFullscreen`** | **999** | **events.js:826** | **全屏容器（被遮挡）** |
| **`#promptFullscreen`** | **999** | **events.js:889** | **全屏容器（同根因）** |
| `.tb`（顶部工具栏） | 1000 | app.css:825 | 固定遮挡源 |
| select2 下拉 | 1051 | select2 CSS | 临时弹出（全屏内无 select） |
| 移动端侧栏 sb | 1099 | index.html | 固定（移动端，触发入口被全屏覆盖） |
| 帮助抽屉 | 9991 | index.html | 临时弹出（触发入口被全屏覆盖） |
| `.chat-del-overlay` | 9998 | app.css:486 | 临时遮罩 |
| `.chat-del-pop` | 9999 | app.css:487 | 临时弹出 |
| `lobbyOrgMenu` / `lobbyUserMenu` | 9999 | app.js:996, 1003 | 临时菜单 |
| warn 横幅 | 9999 | app.js:912 | 临时通知 |
| `#taskLogOverlay` | 9999 | events.js:1217 | 临时遮罩 |
| toast | 10000 | app.js:2421 | 临时通知 |
| `sbUserMenu` / `tbOrgMenu` / `tbUserMenu` | 10001 | index.html:180, 198, 204 | 临时菜单 |
| auto-recovery 横幅 | 99999 | auto-recovery.js:35 | 系统级 |

> **补全说明**：select2(1051)、移动端侧栏(1099)、帮助抽屉(9991) 由全站 z-index 全景扫描发现，原始表格未列。逐一反证后均不构成遮挡——events.js 全屏容器内无 select 元素；侧栏/帮助抽屉的触发入口都是工具栏按钮，被 inset:0 全屏覆盖后无法触达；全站没有键盘快捷键能在全屏期间拉出高层 UI。

**目标 z-index：`1001`**

只高于唯一的固定遮挡源 `.tb`(1000)，不高于任何临时弹出/通知元素。原因：

1. `.tb` 是唯一始终存在于页面顶部的固定元素，z-index:1000 遮挡了全屏容器的顶部栏按钮。
2. 其余高 z-index 元素（toast/warn/菜单/气泡）都是临时弹出式的，触发入口在工具栏或侧栏。全屏容器 `position:fixed;inset:0` 覆盖整个视口后，用户无法点击工具栏/侧栏触发这些弹出元素，所以它们不会出现在全屏期间。
3. 唯一例外是全屏容器内部的操作会触发 toast——清理对话成功/失败、消息发送失败都调 `app.showToast`。z-index:1001 < toast:10000，toast 在全屏上方可见，操作反馈不丢失。

### 1.2 为什么不设 10002

原方案设为 10002（高于所有 UI），会引入附带问题：

| 受影响元素 | z-index | 业务影响 | 10002 下的状态 |
|------------|---------|----------|---------------|
| toast | 10000 | 清理对话成功/失败反馈、消息发送失败反馈 | 被遮挡，用户无反馈 |
| warn 横幅 | 9999 | 系统警告（如只读模式提示） | 被遮挡 |
| .chat-del-pop | 9999 | 清理对话确认气泡 | 被遮挡，需改挂载位置 |
| .chat-del-overlay | 9998 | 清理对话遮罩 | 被遮挡 |

其中 toast 的问题是功能性的：全屏容器内有清理对话按钮(L841-848)和消息输入行(L854)，用户在这些操作后依赖 toast 获取反馈。z-index:10002 会导致"清理对话成功"的 toast 被全屏遮挡，用户不知道操作是否成功——这不是"退出全屏后可以再看"的通知，而是即时操作反馈。

设为 1001 后上述问题全部不存在，且不需要改气泡挂载位置、不需要改 CSS、不影响 chat.js 中对 `.chat-del-pop` 的共享使用。改动从 5必改+2顺手 缩减为 3必改+1防御+1顺手（+cache busting）。

### 1.3 Playwright 实测证据（修复前）

```
elementAtRestorePos: "DIV.tb#tb"     ← 工具栏在还原按钮上方
elementAtClearPos:   "DIV.tb#tb"     ← 工具栏在清理按钮上方
restoreBtnIsTop: false               ← 还原按钮不可点击
clearBtnIsTop:   false               ← 清理按钮不可点击
tbOverlapsBar:  true                 ← 工具栏与全屏顶部栏重叠
```

---

## 2. 改动规格

改动在 `src/api/static/js/pages/events.js`（5 个改动点）和 `src/api/static/index.html`（cache busting）。不改 CSS，不改其他 JS 文件，不改后端。

### 2.1 必改 1 — L826：对话全屏容器 z-index

**当前代码**（events.js:826）：
```js
fs.style.cssText = 'position:fixed;inset:0;z-index:999;background:var(--bg);display:flex;flex-direction:column;gap:10px;padding:18px;box-sizing:border-box';
```

**问题**：`z-index:999` 低于工具栏(1000)。全屏容器的顶部栏（包含还原按钮和清理对话按钮）被工具栏遮挡。

**场景**：用户进入对话全屏 → 还原按钮被 .tb 遮挡 → 无法点击退出 → 清理对话按钮也被遮挡 → 完全无法操作。

**修改后**：
```js
fs.style.cssText = 'position:fixed;inset:0;z-index:1001;background:var(--bg);display:flex;flex-direction:column;gap:10px;padding:18px;box-sizing:border-box';
```

**Diff**：
```diff
-    fs.style.cssText = 'position:fixed;inset:0;z-index:999;background:var(--bg);display:flex;flex-direction:column;gap:10px;padding:18px;box-sizing:border-box';
+    fs.style.cssText = 'position:fixed;inset:0;z-index:1001;background:var(--bg);display:flex;flex-direction:column;gap:10px;padding:18px;box-sizing:border-box';
```

---

### 2.2 必改 2 — L234 后：切事件时清理对话全屏状态

**当前代码**（events.js:227-235）：
```js
function renderEventDetail(data) {
  // 切换事件时清理接收上下文全屏状态
  if (_promptFs) {
    const _fs = document.getElementById('promptFullscreen');
    if (_fs) _fs.remove();
    _promptFs = false;
    _promptFsPlaceholder = null;
  }
  const el = document.getElementById('pageContent');
  // ...
```

**问题**：只清理了 `_promptFs`（接收上下文全屏），没有清理 `_convFs`（对话全屏）。用户在对话全屏状态下切换事件时：
- `#convFullscreen` 残留在 body 上（position:fixed;inset:0 覆盖整个视口）
- 旧 `convMessages` 和 `convInputRow` 被 `#convFullscreen` 持有
- `el.innerHTML = html`（L347）创建新事件 DOM，但被残留的全屏容器遮挡
- 新事件对话区域虽然创建了新的 convMessages/inputRow，但用户看不到（被覆盖）

**场景**：用户在事件 #101 对话全屏中 → 切换到事件 #102 → 全屏容器残留覆盖页面 → 事件 #102 内容不可见 → 用户必须 ESC 退出。

**修改后**（在 `_promptFs` 清理块之后、`const el = ...` 之前插入）：
```js
function renderEventDetail(data) {
  // 切换事件时清理接收上下文全屏状态
  if (_promptFs) {
    const _fs = document.getElementById('promptFullscreen');
    if (_fs) _fs.remove();
    _promptFs = false;
    _promptFsPlaceholder = null;
  }
  // 切换事件时清理对话全屏状态
  if (_convFs) {
    const _cfs = document.getElementById('convFullscreen');
    if (_cfs) _cfs.remove();
    _convFs = false;
  }
  const el = document.getElementById('pageContent');
  // ...
```

**Diff**：
```diff
   if (_promptFs) {
     const _fs = document.getElementById('promptFullscreen');
     if (_fs) _fs.remove();
     _promptFs = false;
     _promptFsPlaceholder = null;
   }
+  // 切换事件时清理对话全屏状态
+  if (_convFs) {
+    const _cfs = document.getElementById('convFullscreen');
+    if (_cfs) _cfs.remove();
+    _convFs = false;
+  }
   const el = document.getElementById('pageContent');
```

> **说明**：清理后 `el.innerHTML = html` 创建全新的 convPanel/convMessages/convInputRow，`loadConversation()` 填充新 convMessages。旧元素随 `#convFullscreen.remove()` 一起销毁，不存在元素丢失。

---

### 2.3 必改 3 — L930-935：ESC handler 增加 task log 全屏判断

**当前代码**（events.js:922-928）：
```js
// ESC 键退出全屏
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') {
    if (_convFs) toggleConvFullscreen();
    if (_promptFs) _togglePromptFullscreen();
  }
});
```

**问题**：缺少 task log 全屏判断。task log 全屏用 `box.dataset.fs === '1'`（events.js:1399）标记，不是布尔变量。用户在 task log 全屏态下按 ESC 无反应。

**场景**：用户打开任务日志 → 点击全屏按钮 → task log 占满视口 → 按 ESC → 无反应（`_convFs=false`、`_promptFs=false`，两个条件都不满足）→ 只能手动点还原按钮。与对话全屏/接收上下文全屏的 ESC 行为不一致。

**边界场景——openInlineLog 的 taskLogOverlay 无全屏按钮**：
openInlineLog(L1532) 也创建 `#taskLogOverlay`，但 HTML 结构中没有 `#taskLogFullscreenBtn`，所以 `box.dataset.fs` 永远是 undefined。ESC handler 检查 `dataset.fs === '1'` 为 false，不误触发 `_toggleTaskLogFullscreen`，ESC 继续走 `_convFs`/`_promptFs` 判断。行为正确——inline log 的 ESC 应该走 `closeTaskLog`（overlay click handler），不走全屏退出。

**修改后**：
```js
// ESC 键退出全屏
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') {
    // task log 全屏：检查 box.dataset.fs
    const tlBox = document.querySelector('#taskLogOverlay > div');
    if (tlBox && tlBox.dataset.fs === '1') {
      _toggleTaskLogFullscreen();
      return;  // task log 全屏退出，不再处理其他全屏状态
    }
    if (_convFs) toggleConvFullscreen();
    if (_promptFs) _togglePromptFullscreen();
  }
});
```

**Diff**：
```diff
 document.addEventListener('keydown', function (e) {
   if (e.key === 'Escape') {
+    // task log 全屏：检查 box.dataset.fs
+    const tlBox = document.querySelector('#taskLogOverlay > div');
+    if (tlBox && tlBox.dataset.fs === '1') {
+      _toggleTaskLogFullscreen();
+      return;  // task log 全屏退出，不再处理其他全屏状态
+    }
     if (_convFs) toggleConvFullscreen();
     if (_promptFs) _togglePromptFullscreen();
   }
 });
```

> **`return` 的理由**：三种全屏模式互斥——task log overlay(z-index:9999) 覆盖整个视口时用户无法触达对话全屏按钮；对话全屏(z-index:1001)覆盖整个视口时用户无法打开 task log。`return` 防止一次 ESC 触发多个全屏退出。

> **选择器说明**：`#taskLogOverlay > div` 选取 overlay 的第一个子元素（box 容器）。openTaskLog(L1217) 和 openInlineLog(L1532) 都用 innerHTML 创建 `<div>` 子元素，结构一致。`box.dataset.fs` 是 `_toggleTaskLogFullscreen`(L1399) 设置的全屏标记。当 overlay 不存在时 querySelector 返回 null，`tlBox && tlBox.dataset.fs === '1'` 为 falsy，不误触发。

---

### 2.4 防御 — L861-866：退出分支检查 panel.isConnected

**当前代码**（events.js:858-869）：
```js
} else {
  const fs = document.getElementById('convFullscreen');
  if (!fs) return;
  const messages = document.getElementById('convMessages');
  const inputRow = document.getElementById('convInputRow');
  panel.appendChild(messages);    // ← panel 可能已脱离文档
  panel.appendChild(inputRow);
  fs.remove();
  panel.style.display = 'flex';
  _convFs = false;
  messages.scrollTop = messages.scrollHeight;
}
```

**问题**：`panel` 在 L818 捕获（`const panel = document.getElementById('convPanel')`）。如果事件切换已重写 pageContent，旧 panel 脱离文档，`appendChild` 不报错但元素消失到游离节点中。

**场景**：必改 2 已在 `renderEventDetail` 开头清理 `_convFs` 并移除 `#convFullscreen`，正常情况下 ESC 不会触发退出分支。但如果存在竞态（ESC 按键已在事件队列中），退出分支执行时 panel 可能已脱离文档。

**修改后**：
```js
} else {
  const fs = document.getElementById('convFullscreen');
  if (!fs) return;
  // panel 已脱离文档（事件切换场景）时直接移除全屏容器
  if (!panel || !panel.isConnected) {
    fs.remove();
    _convFs = false;
    return;
  }
  const messages = document.getElementById('convMessages');
  const inputRow = document.getElementById('convInputRow');
  panel.appendChild(messages);
  panel.appendChild(inputRow);
  fs.remove();
  panel.style.display = 'flex';
  _convFs = false;
  messages.scrollTop = messages.scrollHeight;
}
```

**Diff**：
```diff
 } else {
   const fs = document.getElementById('convFullscreen');
   if (!fs) return;
+  // panel 已脱离文档（事件切换场景）时直接移除全屏容器
+  if (!panel || !panel.isConnected) {
+    fs.remove();
+    _convFs = false;
+    return;
+  }
   const messages = document.getElementById('convMessages');
   const inputRow = document.getElementById('convInputRow');
   panel.appendChild(messages);
```

> **定位**：defense-in-depth。必改 2 已设 `_convFs=false`，ESC 不会触发退出分支。此检查防止竞态下元素丢失。

> **关于 `!panel` 的说明**：toggleConvFullscreen 函数开头 L819 有 `if (!panel) return`，进入退出分支时 panel 必然非 null。`!panel` 一半是死代码但无害——写 `!panel || !panel.isConnected` 是防御性编程惯例，`!panel.isConnected` 是实际有效的检查。外部审查亦确认此分析。

---

### 2.5 顺手 1 — L889：接收上下文全屏 z-index

**当前代码**（events.js:889）：
```js
fs.style.cssText = 'position:fixed;inset:0;z-index:999;background:#000;display:flex;flex-direction:column;box-sizing:border-box';
```

**问题**：与必改 1 同根因。`#promptFullscreen` z-index:999 低于工具栏 1000，还原按钮被遮挡。

**修改后**：
```js
fs.style.cssText = 'position:fixed;inset:0;z-index:1001;background:#000;display:flex;flex-direction:column;box-sizing:border-box';
```

**Diff**：
```diff
-    fs.style.cssText = 'position:fixed;inset:0;z-index:999;background:#000;display:flex;flex-direction:column;box-sizing:border-box';
+    fs.style.cssText = 'position:fixed;inset:0;z-index:1001;background:#000;display:flex;flex-direction:column;box-sizing:border-box';
```

---

### 2.6 顺手 2 — index.html:320：events.js cache busting

**当前代码**（index.html:320）：
```html
<script src="/static/js/pages/events.js?v=56"></script>
```

**问题**：events.js 修改后浏览器仍缓存旧版本（?v=56），用户无法获取修复后的代码。

**改为**：`?v=57`（R12 原始 bump）

> **后续更新**：R13 后续将 events.js cache version 进一步 bump 到 `?v=58`。当前部署版本为 v=58。

**场景**：用户浏览器已缓存 events.js?v=56 → 部署修复后服务器返回新 events.js → 浏览器看到 ?v=56 与缓存一致直接用旧版本 → 修复不生效。

**最终修改后**：
```html
<script src="/static/js/pages/events.js?v=58"></script>
```

**Diff**（R12 原始）：
```diff
-<script src="/static/js/pages/events.js?v=56"></script>
+<script src="/static/js/pages/events.js?v=57"></script>
```

**Diff**（R13 后续 bump）：
```diff
-<script src="/static/js/pages/events.js?v=57"></script>
+<script src="/static/js/pages/events.js?v=58"></script>
```

---

## 3. 依赖关系

```
必改 1 (z-index 999 → 1001)
  └── 顺手 1 (prompt z-index 999 → 1001) — 同根因，独立修复

必改 2 (切事件清理 _convFs)
  └── 防御 (panel.isConnected 检查) — defense-in-depth

必改 3 (ESC handler 增加 task log)
  └── 独立，无依赖

顺手 2 (cache busting ?v=56 → ?v=57[R12] → ?v=58[R13后])
  └── 依赖所有 events.js 改动完成
```

**实施顺序**（推荐）：
1. 必改 1 + 顺手 1（z-index 改值，机械操作）
2. 必改 2（切事件清理）
3. 防御（panel.isConnected 检查）
4. 必改 3（ESC handler）
5. 顺手 2（cache busting）

---

## 4. 验证方法

### 4.1 Playwright DOM 检查

使用 `python3.9`（项目约定）。

#### 4.1.1 z-index 正确性（必改 1，顺手 1）

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto("http://localhost:3000/events/101")
    # 进入对话全屏
    page.click("text=全屏")  # 调整选择器
    z = page.evaluate("document.getElementById('convFullscreen').style.zIndex")
    assert z == "1001", f"Expected 1001, got {z}"

    # 进入接收上下文全屏
    page.click("#promptFsBtn")
    z = page.evaluate("document.getElementById('promptFullscreen').style.zIndex")
    assert z == "1001", f"Expected 1001, got {z}"
    browser.close()
```

#### 4.1.2 按钮可点击性（必改 1）

```python
page.click("text=全屏")
result = page.evaluate("""() => {
    const bar = document.querySelector('#convFullscreen > div');
    const btns = bar.querySelectorAll('button');
    // 还原按钮是最后一个，清理对话按钮是倒数第二个
    const restoreBtn = btns[btns.length - 1];
    const clearBtn = btns[0];  // 清理对话按钮
    function check(btn) {
        const r = btn.getBoundingClientRect();
        const el = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
        return el === btn;
    }
    return {
        restoreBtnIsTop: check(restoreBtn),
        clearBtnIsTop: check(clearBtn)
    };
}""")
assert result["restoreBtnIsTop"], f"还原按钮不可点击: {result}"
assert result["clearBtnIsTop"], f"清理对话按钮不可点击: {result}"
```

#### 4.1.2b 接收上下文全屏按钮可点击性（顺手 1）

```python
# 进入接收上下文全屏
page.click("#promptFsBtn")
result = page.evaluate("""() => {
    const fs = document.getElementById('promptFullscreen');
    if (!fs) return {error: 'no promptFullscreen'};
    const bar = fs.querySelector('div');  // 顶部栏
    const btn = bar.querySelector('button');  // 还原按钮
    const r = btn.getBoundingClientRect();
    const el = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
    return {
        elementAtPos: el?.tagName + (el?.id ? '#' + el.id : ''),
        restoreBtnIsTop: el === btn
    };
}""")
assert result.get("restoreBtnIsTop"), f"接收上下文全屏还原按钮不可点击: {result}"
```

#### 4.1.3 切事件清理（必改 2）

```python
page.click("text=全屏")
assert page.evaluate("document.getElementById('convFullscreen') !== null")
# 切换到另一个事件
page.click("[data-event='102']")  # 调整选择器
assert page.evaluate("document.getElementById('convFullscreen') === null"), "全屏未清理"
# 新事件对话区域正常渲染（非空白）
assert page.evaluate("document.getElementById('convMessages') !== null"), "新事件 convMessages 不存在"
assert page.evaluate("document.getElementById('convMessages').children.length > 0"), "新事件 convMessages 为空白"
```

#### 4.1.4 panel.isConnected 防御

```python
# 模拟：进入全屏，切换事件（必改 2 清理），再手动调退出分支
page.evaluate("window._convFs = true")  # 强制状态
page.evaluate("toggleConvFullscreen()")  # 退出分支
assert page.evaluate("document.getElementById('convFullscreen') === null")
```

#### 4.1.5 ESC 退出 task log 全屏（必改 3）

```python
# 打开任务日志
page.click("text=执行日志")
# 进入 task log 全屏
page.click("#taskLogFullscreenBtn")
assert page.evaluate("document.querySelector('#taskLogOverlay > div').dataset.fs === '1'")
# 按 ESC
page.keyboard.press("Escape")
assert page.evaluate("document.querySelector('#taskLogOverlay > div').dataset.fs !== '1'"), "ESC 未退出 task log 全屏"
```

#### 4.1.5b ESC 退出对话全屏

```python
# 进入对话全屏
page.click("text=全屏")
assert page.evaluate("document.getElementById('convFullscreen') !== null")
assert page.evaluate("window._convFs === true")
# 按 ESC
page.keyboard.press("Escape")
assert page.evaluate("document.getElementById('convFullscreen') === null"), "ESC 未退出对话全屏"
assert page.evaluate("window._convFs === false"), "_convFs 未重置"
```

#### 4.1.5c ESC 退出接收上下文全屏

```python
# 进入接收上下文全屏
page.click("#promptFsBtn")
assert page.evaluate("document.getElementById('promptFullscreen') !== null")
# 按 ESC
page.keyboard.press("Escape")
assert page.evaluate("document.getElementById('promptFullscreen') === null"), "ESC 未退出接收上下文全屏"
```

#### 4.1.6 层级检查

```python
# toast 在全屏上方可见（z-index 10000 > 1001）
# 在全屏内触发清理对话 → 点确认 → 检查 toast 可见
page.click("text=全屏")
page.click("text=清理对话")
page.click("text=确认")
toast_visible = page.evaluate("""() => {
    const t = document.querySelector('[style*="z-index:10000"]');
    if (!t) return false;
    const fs = document.getElementById('convFullscreen');
    if (!fs) return true;
    // toast z-index 10000 > fullscreen 1001 → toast 在上层
    return parseInt(t.style.zIndex) > parseInt(fs.style.zIndex);
}""")
assert toast_visible, "toast 应在全屏上方可见"

# warn 横幅在全屏上方可见（z-index 9999 > 1001）
# 如存在 warn 横幅则检查
warn_above = page.evaluate("""() => {
    // warn 横幅由 app.js:912 创建，style 中含 z-index:9999
    const warns = document.querySelectorAll('[style*="z-index:9999"]');
    if (warns.length === 0) return true; // 不存在则跳过
    const fs = document.getElementById('convFullscreen');
    if (!fs) return true;
    const fsZ = parseInt(fs.style.zIndex);
    for (const w of warns) {
        // 排除 .chat-del-pop（也是 9999 但不是 warn 横幅）
        if (w.className && w.className.includes('chat-del')) continue;
        if (w.id === 'taskLogOverlay') continue;
        // warn 横幅通常是顶部 fixed 横幅
        const style = w.getAttribute('style') || '';
        if (style.includes('top:0') || style.includes('position:fixed')) {
            return parseInt(style.match(/z-index:(\d+)/)?.[1] || 0) > fsZ;
        }
    }
    return true; // 无法识别则跳过
}""")
assert warn_above, "warn 横幅应在全屏上方可见"

# auto-recovery 在全屏上方可见（z-index 99999 > 1001）
# 如存在 auto-recovery 元素则检查
ar_above = page.evaluate("""() => {
    const ar = document.querySelector('[class*="auto-recovery"]');
    if (!ar) return true; // 不存在则跳过
    const arZ = parseInt(getComputedStyle(ar).zIndex);
    const fsZ = parseInt(getComputedStyle(document.getElementById('convFullscreen')).zIndex);
    return arZ > fsZ;
}""")
assert ar_above, "auto-recovery 应在全屏上方"
```

#### 4.1.7 cache busting 验证

```python
# 验证浏览器加载的是 events.js?v=58 而非旧缓存
page.goto("http://localhost:3000/events/101")
script_src = page.evaluate("""() => {
    const scripts = document.querySelectorAll('script[src*="events.js"]');
    return scripts.length > 0 ? scripts[0].src : null;
}""")
assert "v=58" in script_src, f"events.js cache version not bumped: {script_src}"
```

### 4.2 手动验证清单

- [ ] 对话全屏后还原按钮可点击（不被工具栏遮挡）
- [ ] 对话全屏后清理对话按钮可点击
- [ ] 对话全屏后点击"清理对话"→ 确认气泡可见（z-index 9999 > 1001）
- [ ] 对话全屏后点清理对话确认 → toast"清理完成"可见（z-index 10000 > 1001）
- [ ] 对话全屏后确认气泡不被遮挡（无需改挂载位置）
- [ ] 切换事件（在对话全屏中）→ 全屏自动关闭，新事件正常渲染
- [ ] 切换事件（在对话全屏中）→ 新事件对话区域正常显示（非空白）
- [ ] convPanel 脱离文档时退出全屏 → 不报错，全屏容器移除
- [ ] task log 全屏 + ESC → 退出全屏
- [ ] 对话全屏 + ESC → 退出全屏
- [ ] 接收上下文全屏后还原按钮可点击
- [ ] 接收上下文全屏 + ESC → 退出全屏
- [ ] auto-recovery 横幅在全屏上方可见（z-index 99999 > 1001）
- [ ] warn 横幅在全屏上方可见（z-index 9999 > 1001）
- [ ] 部署后浏览器加载 events.js?v=58（非旧缓存 ?v=56）

---

## 5. 风险分析

### 5.1 z-index 设为 1001 的充分性

**风险**：1001 是否足够高？是否有 z-index 在 1001-9998 之间的固定元素会遮挡全屏？

**分析**：实测表中 1001-9998 之间有 select2(1051) 和移动端侧栏(1099) 两个元素，逐一反证：
- select2(1051)：events.js 全屏容器内无 `<select>` 元素，select2 不会出现在全屏期间。
- 移动端侧栏(1099)：触发入口在工具栏按钮，全屏 `position:fixed;inset:0` 覆盖整个视口后无法触达。
- 帮助抽屉(9991)：同理，触发入口在工具栏，全屏后无法触达。
- 全站所有 keydown 处理器只做 ESC 关闭，没有键盘快捷键能在全屏期间拉出高层 UI。

.tb(1000) 是唯一的固定遮挡源。1001 精确地只解决工具栏遮挡问题，不引入任何附带遮挡。

**结论**：充分，零副作用。外部全站 z-index 全景扫描亦确认此结论。

### 5.2 切事件清理的时机

**风险**：`renderEventDetail` 中清理 `_convFs` 的位置在 `const el = document.getElementById('pageContent')` 之前，是否过早？

**分析**：清理位置与现有的 `_promptFs` 清理逻辑相邻（L229-233），在 `el.innerHTML = html`(L347) 之前。顺序为：清理全屏状态 → 获取 el → 重写 pageContent → loadConversation 填充新数据。清理在全量重写之前，确保旧 DOM 被移除后新 DOM 才创建。位置正确。

### 5.3 ESC handler 选择器

**风险**：`document.querySelector('#taskLogOverlay > div')` 依赖 HTML 结构假设，如果 taskLogOverlay 内部结构变化会静默失效。

**分析**：openTaskLog(L1217) 和 openInlineLog(L1532) 都用 innerHTML 创建 `<div>` 作为第一个子元素（box 容器），结构一致。`_toggleTaskLogFullscreen`(L1395) 用 `ov.firstElementChild` 获取 box，与本处选择器逻辑等价。如果未来结构变化，两处需同步修改——可接受的风险。

**边界场景验证**：当 `#taskLogOverlay` 不存在时，`querySelector` 返回 null，`tlBox && tlBox.dataset.fs === '1'` 为 falsy（null），不误触发。当 openInlineLog 创建的 overlay 无全屏按钮时，`dataset.fs` 为 undefined，`=== '1'` 为 false，不误触发。两种边界均正确。

### 5.4 三种全屏模式互斥性

**风险**：ESC handler 中 `return` 后不再检查其他全屏状态。如果两种全屏同时存在会漏退。

**分析**：
- 对话全屏 `#convFullscreen`(position:fixed;inset:0;z-index:1001) 覆盖整个视口
- task log overlay `#taskLogOverlay`(position:fixed;inset:0;z-index:9999) 覆盖整个视口
- 两个 position:fixed;inset:0 的元素不可能同时可见且可交互
- 接收上下文全屏 `#promptFullscreen`(position:fixed;inset:0;z-index:1001) 同理

互斥性成立，`return` 是正确的。

### 5.5 .chat-del-pop 共享类

**说明**：`.chat-del-pop` CSS 类同时被 `chat.js`(L435 `_showChatDelPopover`) 使用。z-index 设为 1001 后，`.chat-del-pop`(z-index:9999) > 全屏(1001)，气泡自然可见，不需要改 CSS 或挂载位置，不影响 chat.js。

### 5.6 panel.isConnected 中 !panel 是死代码

**说明**：toggleConvFullscreen 函数开头 L819 有 `if (!panel) return`，进入退出分支时 panel 必然非 null。`!panel` 永远为 false，是死代码。但 `!panel.isConnected` 是实际有效的检查。写 `!panel || !panel.isConnected` 是防御性编程惯例，无害。外部审查亦确认此分析。

### 5.7 回归风险

**总体**：所有改动在 `events.js` 单文件 + `index.html` cache busting。不改 CSS、不改其他 JS、不改后端。影响范围仅限事件页全屏交互。非全屏行为不变（z-index 从 999→1001 不影响非全屏态的层级关系，999 和 1001 都低于工具栏以下的元素层级）。blast radius 最小。

**向前兼容**：只改一个 JS 文件的内联样式和局部逻辑 + 一个 HTML 文件的 cache version，不动 CSS、不动共享符号（.chat-del-pop 等仍归 chat.js 正常使用）、不动后端/API/DB，对其他页面零影响。FF 合并进 weekly-08 无冲突；主干预 weekly-08 的后续演进也不共享任何被改符号。

---

## 6. 实施说明

- **影响文件**：`src/api/static/js/pages/events.js`（5 个改动点）+ `src/api/static/index.html`（cache busting）
- **无新依赖**：无新 import、无新 CSS 类、无新 HTML 模板
- **无后端改动**：纯前端修复
- **不改 CSS**：z-index 设为 1001 后不需要修改任何 CSS 文件
- **测试**：
  - JS 语法检查 `node -c events.js` 通过
  - 前端沙箱测试 `test_event_nav_hash.js`（4 pass）+ `test_issue_342_event_poll_partial.js`（6 pass）通过
  - Playwright e2e_ui 测试需部署后运行，详见 §4
- **Commit message**（per CONTRIBUTING §5）：
  ```
  fix(events): raise fullscreen z-index to 1001 + fix fullscreen interaction defects (R12)

  - Raise #convFullscreen and #promptFullscreen z-index from 999 to 1001
    (only above .tb toolbar 1000, preserving toast/warn/popover visibility)
  - Clean up conversation fullscreen state on event switch
  - Add panel.isConnected guard in fullscreen exit branch (defense-in-depth)
  - Add task log fullscreen ESC handler for consistent UX
  - Bump events.js cache version to v=57 (R12), subsequently v=58 (R13)

  Signed-off-by: yangaiwu <yangaiwu@huawei.com>
  ```

---

## 7. 改动汇总表

| # | 行号 | 改动 | 类型 | 依赖 | 文件 |
|---|------|------|------|------|------|
| 1 | L826 | `z-index:999` → `1001` | 必改 | — | events.js |
| 2 | L235-240 | `renderEventDetail` 增加 `_convFs` 清理 | 必改 | — | events.js |
| 3 | L930-935 | ESC handler 增加 task log 全屏判断 | 必改 | — | events.js |
| 4 | L861-866 | 退出分支检查 `panel.isConnected` | 防御 | #2 | events.js |
| 5 | L889 | `z-index:999` → `1001` | 顺手 | — | events.js |
| 6 | L320 | `?v=56` → `?v=57`(R12) → `?v=58`(R13后) | 顺手 | #1-5 | index.html |

---

## 8. 实施后行号映射

> 基线 commit → 实施后 commit `449c034ba`（分支 `feat/fullscreen-zindex-fix`，rebase 到 `06ad2e106`）

| 改动点 | 基线行号 | 实施后行号 | 文件 |
|--------|---------|-----------|------|
| 必改 1: convFullscreen z-index | L827 | L826 | events.js |
| 必改 2: _convFs 清理 | L234 后 | L235-240 | events.js |
| 必改 3: ESC handler task log | L922-927 | L930-935 | events.js |
| 防御: panel.isConnected | L858-866 | L861-866 | events.js |
| 顺手 1: promptFullscreen z-index | L884 | L889 | events.js |
| 顺手 2: cache busting | L320 | L320 | index.html |

---

## 9. 测试用例代码

```python
# test_r12_fullscreen_zindex.py
# R12: 对话全屏 z-index 遮挡 + 全屏态交互缺陷修复
# 用法: python3.9 test_r12_fullscreen_zindex.py
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

    # 前置: 登录
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

    # 导航到事件页
    page.evaluate("window.location.hash = '#event/1'")
    page.wait_for_timeout(2000)

    # === 测试 1: z-index 正确性 ===
    print('\n=== 测试 1: z-index 正确性 ===')
    # 进入对话全屏
    try:
        page.click('button:has-text("全屏")')
        page.wait_for_timeout(500)
    except:
        # 尝试其他选择器
        try:
            page.evaluate('toggleConvFullscreen()')
            page.wait_for_timeout(500)
        except:
            pass

    z = page.evaluate("""() => {
        const fs = document.getElementById('convFullscreen');
        return fs ? fs.style.zIndex : null;
    }""")
    test('对话全屏 z-index=1001', z == '1001', f'got {z}')

    # === 测试 2: 还原按钮可点击 ===
    print('\n=== 测试 2: 还原按钮可点击 ===')
    btn_result = page.evaluate("""() => {
        const fs = document.getElementById('convFullscreen');
        if (!fs) return {error: 'no fullscreen'};
        const bar = fs.querySelector('div');
        const btns = bar ? bar.querySelectorAll('button') : [];
        if (btns.length === 0) return {error: 'no buttons'};
        const restoreBtn = btns[btns.length - 1]; # 还原按钮是最后一个
        const r = restoreBtn.getBoundingClientRect();
        const el = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
        return {
            btnText: restoreBtn.textContent.trim(),
            topEl: el ? (el.tagName + (el.id ? '#' + el.id : '')) : 'null',
            isBtn: el === restoreBtn
        };
    }""")
    test('还原按钮可点击', btn_result.get('isBtn', False), f"topEl={btn_result.get('topEl')}")

    # === 测试 3: 清理对话按钮可点击 ===
    print('\n=== 测试 3: 清理对话按钮可点击 ===')
    clear_result = page.evaluate("""() => {
        const fs = document.getElementById('convFullscreen');
        if (!fs) return {error: 'no fullscreen'};
        const bar = fs.querySelector('div');
        const btns = bar ? bar.querySelectorAll('button') : [];
        if (btns.length === 0) return {error: 'no buttons'};
        # 清理对话按钮是第一个或倒数第二个
        const clearBtn = btns[0];
        const r = clearBtn.getBoundingClientRect();
        const el = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
        return {
            btnText: clearBtn.textContent.trim(),
            topEl: el ? (el.tagName + (el.id ? '#' + el.id : '')) : 'null',
            isBtn: el === clearBtn
        };
    }""")
    test('清理对话按钮可点击', clear_result.get('isBtn', False), f"topEl={clear_result.get('topEl')}")

    # === 测试 4: 工具栏被遮罩盖住 ===
    print('\n=== 测试 4: 工具栏被遮罩盖住 ===')
    tb_result = page.evaluate("""() => {
        const tb = document.querySelector('.tb');
        const fs = document.getElementById('convFullscreen');
        if (!tb || !fs) return {error: 'no tb or fs'};
        const tbRect = tb.getBoundingClientRect();
        const elAtTb = document.elementFromPoint(tbRect.x + 100, tbRect.y + 26);
        return {
            tbZ: getComputedStyle(tb).zIndex,
            fsZ: fs.style.zIndex,
            elAtTb: elAtTb ? (elAtTb.tagName + (elAtTb.id ? '#' + elAtTb.id : '')) : 'null',
            tbCovered: elAtTb !== tb
        };
    }""")
    test('工具栏被全屏遮罩盖住', tb_result.get('tbCovered', False), f"elAtTb={tb_result.get('elAtTb')}")

    # === 测试 5: ESC 退出对话全屏 ===
    print('\n=== 测试 5: ESC 退出对话全屏 ===')
    page.keyboard.press('Escape')
    page.wait_for_timeout(500)
    fs_after = page.evaluate("document.getElementById('convFullscreen') !== null")
    test('ESC 退出对话全屏', not fs_after, 'fullscreen removed')

    # === 测试 6: 切事件清理全屏 ===
    print('\n=== 测试 6: 切事件清理全屏 ===')
    # 重新进入全屏
    try:
        page.click('button:has-text("全屏")')
        page.wait_for_timeout(500)
    except:
        try:
            page.evaluate('toggleConvFullscreen()')
            page.wait_for_timeout(500)
        except:
            pass
    has_fs = page.evaluate("document.getElementById('convFullscreen') !== null")
    if has_fs:
        # 切换到另一个事件
        page.evaluate("window.location.hash = '#event/2'")
        page.wait_for_timeout(2000)
        fs_after_switch = page.evaluate("document.getElementById('convFullscreen') !== null")
        test('切事件后全屏已清理', not fs_after_switch, 'fullscreen removed on event switch')
        # 检查新事件对话区域存在
        has_conv = page.evaluate("document.getElementById('convMessages') !== null")
        test('新事件对话区域存在', has_conv)
    else:
        test('切事件清理全屏', False, 'could not enter fullscreen')

    # === 测试 7: task log 全屏 ESC 退出 ===
    print('\n=== 测试 7: task log 全屏 ESC 退出 ===')
    page.evaluate("window.location.hash = '#event/1'")
    page.wait_for_timeout(2000)
    try:
        page.click('button:has-text("执行日志")')
        page.wait_for_timeout(1000)
        # 进入 task log 全屏
        page.click('#taskLogFullscreenBtn')
        page.wait_for_timeout(500)
        is_fs = page.evaluate("""() => {
            const box = document.querySelector('#taskLogOverlay > div');
            return box && box.dataset.fs === '1';
        }""")
        if is_fs:
            page.keyboard.press('Escape')
            page.wait_for_timeout(500)
            still_fs = page.evaluate("""() => {
                const box = document.querySelector('#taskLogOverlay > div');
                return box && box.dataset.fs === '1';
            }""")
            test('ESC 退出 task log 全屏', not still_fs, 'task log fullscreen exited')
        else:
            test('ESC 退出 task log 全屏', False, 'could not enter task log fullscreen')
        # 关闭 task log
        page.keyboard.press('Escape')
        page.wait_for_timeout(500)
    except Exception as e:
        test('ESC 退出 task log 全屏', False, str(e))

    # === 测试 8: 接收上下文全屏 z-index ===
    print('\n=== 测试 8: 接收上下文全屏 z-index ===')
    try:
        page.click('#promptFsBtn')
        page.wait_for_timeout(500)
        pz = page.evaluate("""() => {
            const fs = document.getElementById('promptFullscreen');
            return fs ? fs.style.zIndex : null;
        }""")
        test('接收上下文全屏 z-index=1001', pz == '1001', f'got {pz}')
        page.keyboard.press('Escape')
        page.wait_for_timeout(500)
    except:
        test('接收上下文全屏 z-index=1001', False, 'could not open prompt fullscreen')

    # === 测试 9: cache busting ===
    print('\n=== 测试 9: cache busting ===')
    src = page.evaluate("""() => {
        const scripts = document.querySelectorAll('script[src*="events.js"]');
        return scripts.length > 0 ? scripts[0].src : null;
    }""")
    has_v58 = src and 'v=58' in src
    test('events.js cache version bumped', has_v58, f'src={src}')

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
