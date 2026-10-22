# 对话全屏 z-index 遮挡 + 全屏态交互缺陷修复

> **分支**：`feat/fullscreen-zindex-fix`
> **基线**：`grape-dev/weekly-08`
> **状态**：已实施，已推送，待部署验证

## 一、总览

### 1.1 背景

用户在事件详情页使用对话全屏功能时，遇到多个交互缺陷：全屏后还原/清理按钮被顶部工具栏遮挡无法点击、全屏状态下切换事件导致页面卡死、ESC 无法退出任务日志全屏。这些问题影响用户在全屏模式下的基本操作流程，需要修复。

### 1.2 需求范围

| 编号 | 需求 | 类型 |
|------|------|------|
| R12 | 对话全屏 z-index 遮挡 + 全屏态交互缺陷修复（3 必改 + 1 防御 + 1 顺手） | Bug 修复 |

### 1.3 不在范围

- 登录错误提示优化（独立分支 `feat/login-error-prompt-optimization`）
- skill/scene 同步合并（独立分支 `feat/skill-scene-sync-merge`）
- 一键部署脚本（独立分支 `feat/one-click-deploy-script`）

---

## 二、问题分析

### 2.1 业务场景

事件详情页（events 页）是对话窗口的核心交互场所。用户在此查看事件上下文、阅读 agent 回复、与 agent 进行对话。全屏功能让用户能聚焦对话内容，是高频使用的交互。

当前全屏功能存在以下问题，直接影响用户操作：

1. **全屏后按钮不可点击**：用户进入对话全屏后，还原按钮和清理对话按钮被顶部工具栏遮挡，无法点击退出全屏或清理对话。用户被困在全屏中，只能靠 ESC 退出（如果记得的话），清理对话功能完全不可用。

2. **全屏切换事件导致页面卡死**：用户在对话全屏状态下切换到另一个事件，全屏容器残留在页面上覆盖所有内容，新事件的对话区域空白。用户必须靠 ESC 退出残留的全屏容器才能正常使用页面。

3. **任务日志全屏无法 ESC 退出**：用户打开任务日志并切换到全屏后，按 ESC 无反应，只能手动点击还原按钮。与对话全屏和接收上下文全屏的行为不一致，用户预期 ESC 能退出所有全屏状态。

### 2.2 z-index 层级全量实测表

以下数据基于 `grape-dev/weekly-08` 基线代码逐行核实，并经全站 z-index 全景扫描补全：

| 元素 | z-index | 来源 | 类型 | 说明 |
|------|---------|------|------|------|
| .chat-picker-pop | 998 | app.css:773 | 临时弹出 | 对话选择器弹出 |
| .modal-overlay | 999 | app.css:741 | 临时遮罩 | 模态遮罩 |
| **#convFullscreen（对话全屏）** | **999** | **events.js:826** | **全屏容器** | **被工具栏遮挡** |
| **#promptFullscreen（接收上下文全屏）** | **999** | **events.js:889** | **全屏容器** | **同根因** |
| .tb（顶部工具栏） | 1000 | app.css:825 | 固定遮挡源 | 唯一始终存在的固定遮挡 |
| select2 下拉 | 1051 | select2 CSS | 临时弹出 | events 全屏容器内无 select 元素，不构成遮挡 |
| 移动端侧栏 sb | 1099 | index.html | 固定（移动端） | 触发入口在工具栏，全屏后无法触达 |
| .chat-del-overlay（清理确认遮罩） | 9998 | app.css:486 | 临时遮罩 | 清理对话确认 |
| .chat-del-pop（清理确认气泡） | 9999 | app.css:487 | 临时弹出 | 清理对话气泡 |
| lobbyOrgMenu / lobbyUserMenu | 9999 | app.js:996, 1003 | 临时菜单 | 大厅菜单 |
| warn 横幅 | 9999 | app.js:912 | 临时通知 | 系统警告 |
| #taskLogOverlay（任务日志全屏） | 9999 | events.js:1217 | 临时遮罩 | 任务日志遮罩 |
| 帮助抽屉 | 9991 | index.html | 临时弹出 | 触发入口在工具栏，全屏后无法触达 |
| toast | 10000 | app.js:2421 | 临时通知 | 操作反馈通知 |
| sbUserMenu / tbOrgMenu / tbUserMenu | 10001 | index.html:180, 198, 204 | 临时菜单 | 顶栏/侧栏菜单 |
| auto-recovery 横幅 | 99999 | auto-recovery.js:35 | 系统级 | 系统级恢复 |

> **补全说明**：select2(1051)、移动端侧栏(1099)、帮助抽屉(9991) 三个元素由全站 z-index 全景扫描发现，原始表格未列。逐一反证后均不构成问题——events.js 全屏容器内无 select 元素（无 select2）；侧栏/帮助抽屉的触发入口都是工具栏按钮，被 inset:0 全屏覆盖后无法触达；全站所有 keydown 处理器只做 ESC 关闭，没有键盘快捷键能在全屏期间拉出高层 UI。

### 2.3 z-index 目标值分析

**核心问题**：`#convFullscreen` z-index:999 低于 `.tb` 工具栏 z-index:1000，工具栏是唯一的固定遮挡源（始终存在于页面顶部）。

**目标值选择：1001**

设为 1001（仅高于工具栏 1000）而非 10002（高于所有 UI）的原因：

| z-index 值 | .tb(1000) | toast(10000) | warn(9999) | .chat-del-pop(9999) | 菜单(9999/10001) | select2(1051) | 侧栏(1099) | 副作用 |
|------------|-----------|-------------|------------|---------------------|------------------|--------------|-----------|--------|
| 10002 | 不遮挡 ✓ | 被遮挡 ✗ | 被遮挡 ✗ | 被遮挡 ✗ | 不遮挡 ✓ | 不遮挡 ✓ | 不遮挡 ✓ | toast/warn/气泡不可见，需额外改挂载位置 |
| 1001 | 不遮挡 ✓ | 不遮挡 ✓ | 不遮挡 ✓ | 不遮挡 ✓ | 不遮挡 ✓ | 不遮挡 ✓ | 不遮挡 ✓ | 零副作用 |

**关键业务场景**：全屏容器内有清理对话按钮和消息输入行。用户点"清理对话"后 `_doClearConv` 调用 `app.showToast('清理完成')`，发送消息失败也走 `app.showToast`。如果 z-index 设为 10002，toast(10000) 被全屏遮挡，用户在全屏内执行操作的反馈完全丢失——这不是"退出全屏后可以再看"的通知，而是即时操作反馈。

设为 1001 的效果：
- .tb(1000) < 1001 → 工具栏不再遮挡按钮，核心问题解决
- select2(1051) < 1001 → 即使全屏容器内有 select2 也不会被遮挡（实测全屏内无 select，双重保险）
- 移动端侧栏(1099) < 1001 → 侧栏不遮挡全屏（且触发入口已被全屏覆盖）
- toast(10000) > 1001 → 操作反馈可见
- warn(9999) > 1001 → 系统警告可见
- .chat-del-pop(9999) > 1001 → 清理气泡自然可见，无需改挂载位置
- 临时弹出式菜单(9999/10001)在全屏态下不会被触发（入口在工具栏/侧栏，已被全屏遮挡）
- 帮助抽屉(9991) > 1001 → 不遮挡（且触发入口已被全屏覆盖）

---

## 三、R12：改动点详细说明

### 3.1 必改点 1：L826 — 对话全屏容器 z-index

**当前**：`fs.style.cssText = 'position:fixed;inset:0;z-index:999;...'`

**问题**：z-index:999 低于工具栏 1000，全屏后还原按钮和清理按钮被工具栏遮挡不可点击。

**Playwright 实测验证**：
```
elementAtRestorePos: "DIV.tb#tb"     ← 还原按钮上方是工具栏
elementAtClearPos:   "DIV.tb#tb"     ← 清理按钮上方是工具栏
restoreBtnIsTop: false               ← 还原按钮不可点击
clearBtnIsTop:   false               ← 清理按钮不可点击
tbOverlapsBar:  true                 ← 工具栏与全屏顶部栏重叠
```

**改为**：`z-index:1001`

**场景用例**：
```
用户在事件 #101 详情页点击对话全屏按钮。
→ #convFullscreen 创建为 position:fixed;inset:0;z-index:999
→ 顶部工具栏 .tb(z-index:1000) 覆盖全屏容器的顶部栏
→ 还原按钮（在全屏顶部栏右侧）被工具栏遮挡，点击无反应
→ 清理对话按钮同样被遮挡，无法操作
→ 用户被困在全屏中，只能靠 ESC 退出（如果记得快捷键）
修复后：z-index:1001 > 1000，工具栏不再遮挡，按钮可点击
```

### 3.2 必改点 2：L227-234 — 切事件时未清理对话全屏状态

**当前**：`renderEventDetail` 只清理了 `_promptFs`（接收上下文全屏），没有清理 `_convFs`（对话全屏）。

**问题**：用户在对话全屏状态下切换事件，`#convFullscreen` 残留在 body 上，持有 `convMessages` 和 `convInputRow`。新事件 `el.innerHTML = html` 全量重写 pageContent 时创建了新的 convPanel/convMessages/convInputRow，但旧的 `#convFullscreen` 仍覆盖整个页面。

**场景用例**：
```
用户在事件 #101 的对话全屏中查看 agent 回复。
点击侧栏导航切换到事件 #102。
→ renderEventDetail 执行，el.innerHTML = html 创建新事件 DOM
→ 但 #convFullscreen 仍残留在 body 上（position:fixed;inset:0 覆盖整个视口）
→ 旧 convMessages 和 convInputRow 被 #convFullscreen 持有，已从旧 convPanel 移走
→ 新事件的 convPanel 中有新的 convMessages（id 相同但 DOM 不同）
→ #convFullscreen 挡住整个页面，用户看不到事件 #102 的内容
→ 用户必须按 ESC 退出残留的全屏容器
修复后：renderEventDetail 开头清理 _convFs，移除残留容器，新事件正常渲染
```

### 3.3 必改点 3：L930-935 — ESC 未处理 task log 全屏

**当前**：ESC handler 只检查 `_convFs` 和 `_promptFs`，缺少 `_toggleTaskLogFullscreen` 的判断。

**问题**：task log 全屏用 `box.dataset.fs === '1'` 标记（events.js:1399），不是布尔变量。用户在 task log 全屏态下按 ESC 无反应，与对话全屏和接收上下文全屏的行为不一致。

**场景用例**：
```
用户在事件 #101 详情页点击"执行日志"，打开 task log overlay。
点击 task log 的全屏按钮，task log 占满整个视口。
按 ESC 想退出全屏。
→ ESC handler 只检查 _convFs(false) 和 _promptFs(false)
→ 两者都不满足，ESC 无反应
→ 用户只能手动点击还原按钮
→ 与对话全屏/接收上下文全屏的 ESC 行为不一致，用户困惑
修复后：ESC handler 增加 task log 全屏判断，行为统一
```

**边界场景——openInlineLog 的 taskLogOverlay 无全屏按钮**：
```
openInlineLog(L1532) 也创建 #taskLogOverlay，但 HTML 结构中没有 #taskLogFullscreenBtn。
→ inline log 的 box.dataset.fs 永远是 undefined
→ ESC handler 检查 dataset.fs === '1' 为 false
→ 不误触发 _toggleTaskLogFullscreen，ESC 继续走 _convFs/_promptFs 判断
→ 行为正确：inline log 的 ESC 应该走 closeTaskLog（overlay click handler），不走全屏退出
```

### 3.4 防御点：L861-866 — 还原分支检查 panel.isConnected

**当前**：退出全屏分支直接 `panel.appendChild(messages)`，未检查 panel 是否仍在文档中。

**问题**：如果 `convPanel` 已被事件切换移除（`el.innerHTML = html` 重写了 pageContent），`panel` 变量持有的是游离节点。`appendChild` 不报错但元素消失到游离节点中。

**场景用例**：
```
（必改点 2 修复后的防御场景）
用户在事件 #101 对话全屏中。
快速连续点击切换到事件 #102 再切换到事件 #103。
→ 必改点 2 的清理逻辑会移除 #convFullscreen 并重置 _convFs=false
→ 正常情况下 ESC 不会触发退出分支（_convFs 已为 false）
→ 但如果存在竞态（ESC 按键事件已在事件队列中等待处理）
→ 退出分支执行时 panel 可能已脱离文档
→ appendChild(messages) 塞入游离节点，元素从页面消失
修复后：检查 panel.isConnected，游离时直接移除全屏容器并返回
```

**关于 `!panel` 的说明**：toggleConvFullscreen 函数开头 L819 有 `if (!panel) return`，进入退出分支时 panel 必然非 null。`!panel` 一半是死代码但无害——写 `!panel || !panel.isConnected` 是防御性编程惯例，不影响逻辑正确性，`!panel.isConnected` 是实际有效的检查。

### 3.5 顺手点：L889 — 接收上下文全屏 z-index

**当前**：`fs.style.cssText = '...z-index:999;...'`（events.js:884）

**问题**：与必改点 1 同根因。`#promptFullscreen` z-index:999 低于工具栏 1000，还原按钮被遮挡。

**改为**：`z-index:1001`

### 3.6 顺手点：index.html — events.js cache busting

**当前**：`<script src="/static/js/pages/events.js?v=56"></script>`（index.html:320）

**问题**：events.js 修改后浏览器仍缓存旧版本（?v=56），用户无法获取修复后的代码。

**改为**：`?v=57`（R12 原始 bump）

> **后续更新**：R13 后续将 events.js cache version 进一步 bump 到 `?v=58`。当前部署版本为 v=58。

**场景用例**：
```
用户浏览器已缓存 events.js?v=56（含 z-index:999 的旧代码）。
部署修复后，服务器返回新的 events.js（z-index:1001）。
→ 但浏览器看到 ?v=56 与缓存一致，直接用旧版本
→ 修复不生效，用户仍然遇到按钮不可点击
修复后：?v=58 强制浏览器重新加载 events.js
```

### 3.7 不需要改动的点（z-index 设为 1001 后自动解决）

原方案 z-index 设为 10002 时，会引入以下附带问题需要额外修复：
- 清理对话气泡(.chat-del-pop z-index:9999)被全屏容器(10002)遮挡 → 需改挂载位置
- toast(10000)被全屏遮挡 → 清理对话成功/失败反馈不可见
- warn 横幅(9999)被全屏遮挡 → 系统警告不可见

设为 1001 后，上述元素的 z-index 均高于全屏容器，不需要任何额外改动。`.chat-del-pop` 类同时被 `chat.js`(L435) 使用，不改 CSS 不影响其他页面。

---

## 四、改动汇总

| 改动点 | 行号 | 改动内容 | 类型 | 文件 |
|--------|------|---------|------|------|
| 必改 1 | L827(基线)→L826(实施后) | z-index:999 → 1001 | 必改 | events.js |
| 必改 2 | L234 后(基线)→L235-240(实施后) | 增加 _convFs 清理逻辑 | 必改 | events.js |
| 必改 3 | L922-927(基线)→L930-935(实施后) | ESC handler 增加 task log 全屏判断 | 必改 | events.js |
| 防御 | L858-866(基线)→L861-866(实施后) | 退出分支检查 panel.isConnected | 防御 | events.js |
| 顺手 1 | L884(基线)→L889(实施后) | z-index:999 → 1001 | 顺手 | events.js |
| 顺手 2 | L320(index.html) | ?v=56 → ?v=57(R12)→ ?v=58(R13后) | 顺手 | index.html |

## 五、验证标准

- [ ] 对话全屏后还原按钮可点击（不被工具栏遮挡）
- [ ] 对话全屏后清理对话按钮可点击
- [ ] 对话全屏后点击"清理对话"→ 确认气泡可见（z-index 9999 > 1001）
- [ ] 对话全屏后点清理对话确认 → toast"清理完成"可见（z-index 10000 > 1001）
- [ ] 对话全屏状态下切换事件 → 全屏自动关闭，新事件正常渲染
- [ ] 对话全屏状态下切换事件 → 新事件的对话区域正常显示（非空白）
- [ ] convPanel 已脱离文档时退出全屏 → 不报错，全屏容器移除
- [ ] task log 全屏状态下按 ESC → 退出全屏
- [ ] 对话全屏状态下按 ESC → 退出全屏
- [ ] 接收上下文全屏后还原按钮可点击
- [ ] 接收上下文全屏状态下按 ESC → 退出全屏
- [ ] auto-recovery 横幅在全屏容器之上可见（z-index 99999 > 1001）
- [ ] warn 横幅在全屏容器之上可见（z-index 9999 > 1001）
- [ ] 部署后浏览器加载 events.js?v=58（非旧缓存 ?v=56）

## 六、改动文件

| 文件 | 改动内容 |
|------|---------|
| src/api/static/js/pages/events.js | z-index 999→1001 + 切事件清理 + ESC task log + panel.isConnected 防御 |
| src/api/static/index.html | events.js cache busting ?v=56 → ?v=57(R12) → ?v=58(R13后) |

## 七、设计文档

详细设计见同目录下 `r12-fullscreen-zindex-design.md`。

---

## 八、测试用例代码

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
