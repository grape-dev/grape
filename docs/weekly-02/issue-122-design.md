# Issue #122 — 技能详情页返回后列表页空白

## 1. 问题描述

从技能详情页点击「返回」按钮后，技能列表页变为空白页。

### 现象
- 仓库技能 → 详情页 → 返回 → 列表空白
- 个人技能 → 详情页 → 返回 → 列表空白
- 场景中心详情 → 技能详情 → 返回 → 场景详情正常（不受影响）

### 触发条件
仅页面内「返回」按钮触发。浏览器原生后退按钮不走此路径，不受影响。

## 2. 根因分析

### 调用链

```
点击技能卡片:  app.nav('skill', id) → _currentView='skill' → renderSkillDetail()
点击返回按钮:  _backFromSkillDetail() → _backToSkillList() → renderSkills()
                                                  ↑ 绕过 app.nav()，未重置 _currentView
```

### 拦截点

`renderSkills()` → `_reloadSkills()` 的 API 回调中有 stale guard（skills.js 第141行）：

```js
if (window._currentView !== 'skills' && window._currentView !== 'skills:' + _skillTab) return;
```

此时 `_currentView` 仍为 `'skill'`（进入详情页时设的值），guard 命中直接 return，`_renderSkillPage()` 永不被调用 → 页面空白。

### 为什么浏览器后退正常

浏览器后退走 `popstate` → `nav('skills')` → 第369行 `window._currentView = 'skills'` 正确设置 → guard 通过 → 正常渲染。

## 3. 修复方案

### 改动文件

`src/api/static/js/pages/skills.js`，函数 `_backToSkillList()`（第69-71行）

### 改动内容

```diff
 function _backToSkillList() {
+  window._currentView = 'skills:' + _skillTab;
   renderSkills();
 }
```

### 设计依据

1. stale guard 检查 `window._currentView !== 'skills' && window._currentView !== 'skills:' + _skillTab`，设为 `'skills:' + _skillTab` 精确匹配

2. 不用 `app.nav('skills')` 的原因：
   - app.nav() 会清空 pageContent 显示 loading → 视觉闪烁
   - app.nav() 调 _pushHistory → 不必要的 history 入栈
   - _backFromSkillDetail 注释明确写了"不 push history，避免历史栈膨胀"
   - 直接设 _currentView + 调 renderSkills() 保持原有设计意图，最小改动

3. 场景详情返回路径无问题：`_backFromSkillDetail()` 中 `from.view === 'scene-detail'` 分支调 `renderSceneDetail(from.param)`，该函数 API 回调（scene-market.js 第801-802行）没有 stale guard，直接调 `_renderSceneDetailPage(d)`

### 影响范围

- `_backToSkillList()` 仅在 `_backFromSkillDetail()` 的 else 分支调用（非场景详情来源时）
- 改的是函数内部逻辑，不影响任何其他调用方

## 4. 验收标准

- [ ] 仓库技能 → 详情 → 返回，列表正常渲染
- [ ] 个人技能 → 详情 → 返回，列表正常渲染
- [ ] 场景中心详情 → 技能详情 → 返回，场景详情正常渲染
- [ ] 页面控制台无阻断性报错
