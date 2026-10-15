# Grape 引导页面体验优化 — 技术设计文档

> 需求文档：docs/weekly-06/onboarding-experience-requirements.md（需求 31）
> 关联技术设计：docs/weekly-06/grape-account-system-tech-design-v2.md（第 4.5/6.4/17 节）
> 分支：feat/grape-account-system

## 1. 架构总览

### 1.1 本次改动范围

将引导页从多屏滚动产品概览简化为单屏绑定页面。

**删除：**
- 工作流演示区域（`_obWorkflowHtml`）
- 差异化能力卡片（`_obCapabilitiesHtml`）
- Agent 架构区域（`_obAgentsHtml`）
- 基础设施特性区域（`_obFeaturesHtml`）
- 底部行动引导 CTA（`_obCtaHtml`）
- 底部引流入口（`_obFooterHtml`）
- 连续阻尼滚动引擎（`_obInitScrollEngine`、`_mapRange`）
- IO 揭示动画（`_obInitRevealAnim`）
- 滚动进度条（`.ob-progress-bar`）
- 浮动绑定按钮（`.ob-float-bind`）

**保留：**
- 顶栏（`lobby-head`：Logo + 组织下拉 + 用户菜单）— 已有，不动
- 欢迎区域（`_obHeroHtml`）— 简化
- 平台绑定卡片（`ob-bind-card`）— 保留

### 1.2 设计原则

- **单屏布局**：`.lobby-main` 内容垂直居中，不需要滚动
- **复用 lobby 壳框架**：继续使用 `.lobby` + `.lobby-head` + `.lobby-main` 结构
- **CSS 命名空间 `.ob-*`**：不覆盖现有 `.lobby-*` 样式

---

## 2. 页面结构

### 2.1 DOM 结构

```
#pageContent.ob-mode
  └─ .lobby
       ├─ .lobby-head（Logo + 组织下拉 + 用户菜单）— 已有
       └─ .lobby-main
            └─ .ob-content（垂直居中）
                 ├─ .ob-hero
                 │    ├─ .ob-hero-logo img
                 │    ├─ h1.ob-hero-title（欢迎语）
                 │    ├─ p.ob-hero-sub（产品定位语）
                 │    └─ .ob-hero-bind（平台绑定卡片容器）
                 │         └─ .ob-bind-card × N
                 │              ├─ .ob-bind-icon（平台图标）
                 │              ├─ .ob-bind-name（平台名称）
                 │              └─ button.bt.bt-p（绑定按钮）
                 └─ .ob-hero-hint（底部小提示）
```

### 2.2 `_renderOnboardingShell` 重构

```javascript
function _renderOnboardingShell(providers, isZh, name, avatar) {
  var d = document.getElementById('pageContent');
  if (!d) return;

  // 平台绑定卡片 — 品牌色通过 CSS 变量 --brand 传递，按钮统一用系统蓝色
  var platformCards = '';
  providers.forEach(function(p) {
    var icon = platformIcon(p.provider, 28);
    var brandVar = p.brand_color ? '--brand:' + p.brand_color : '--brand:var(--a)';
    platformCards += '<div class="ob-bind-card" style="' + brandVar + '" onclick="app.bindProvider(\'' + p.provider + '\')">'
      + '<div class="ob-bind-icon">' + icon + '</div>'
      + '<div class="ob-bind-name">' + _esc(p.name) + '</div>'
      + '<button class="bt bt-p" style="background:var(--a);font-size:12px;padding:6px 16px">'
      + I18N.t('app.login.ob_bind_btn') + '</button>'
      + '</div>';
  });

  var html = '<div class="lobby">'
    + '<header class="lobby-head">'
    +   '<div class="lobby-brand">'
    +     '<img class="lobby-logo" src="/static/img/grape-logo.jpg?v=1" alt="Grape">'
    +     '<span class="lobby-brand-name">Grape</span>'
    +     '<span class="lobby-brand-sub">' + I18N.t('repos.brand_sub') + '</span>'
    +   '</div>'
    +   '<div style="flex:1"></div>'
    +   '<div style="display:flex;align-items:center;gap:10px">'
    +     '<div id="lobbyOrgDropdown" style="position:relative">'
    +       '<button onclick="app._toggleLobbyOrgDropdown()" id="lobbyOrgBtn" ...>'
    +         '<span style="font-size:14px">🏢</span>'
    +         '<span class="lobby-org-name" id="lobbyOrgName">' + I18N.t('organizations.my_orgs') + '</span>'
    +       '</button>'
    +       '<div id="lobbyOrgMenu" style="display:none;..."></div>'
    +     '</div>'
    +     '<div id="lobbyUserDropdown" style="position:relative">'
    +       '<button onclick="app._toggleLobbyUserMenu()" ...>'
    +         '<span class="lobby-avatar">' + avatar + '</span>'
    +         '<span class="lobby-user-name">' + _esc(name) + '</span>'
    +       '</button>'
    +       '<div id="lobbyUserMenu" style="display:none;..."></div>'
    +     '</div>'
    +   '</div>'
    + '</header>'
    + '<main class="lobby-main">'
    +   '<div class="lobby-content ob-content">'
    +     _obHeroHtml(name, isZh, platformCards)
    +   '</div>'
    + '</main>'
    + '</div>';

  d.innerHTML = html;
  d.classList.add('ob-mode');
  _updateTbOrgName();
  _updateLobbyOrgName();
}
```

### 2.3 欢迎区域 `_obHeroHtml`

```javascript
function _obHeroHtml(name, isZh, platformCards) {
  return '<section class="ob-hero">'
    + '<div class="ob-hero-logo"><img src="/static/img/grape-logo.jpg?v=1" alt="Grape"></div>'
    + '<h1 class="ob-hero-title">' + I18N.t('app.login.ob_cta_title') + '</h1>'
    + '<p class="ob-hero-sub">' + I18N.t('app.login.ob_cta_sub') + '</p>'
    + '<div class="ob-hero-bind">' + platformCards + '</div>'
    + '<p class="ob-hero-hint">' + I18N.t('app.login.ob_hero_hint') + '</p>'
    + '</section>';
}
```

### 2.4 离开引导页清理

```javascript
function _obExitMode() {
  var d = document.getElementById('pageContent');
  if (d) d.classList.remove('ob-mode');
}
```

不再需要 `cancelAnimationFrame` 清理（无 rAF 循环）。

---

## 3. CSS 设计

### 3.1 引导页模式

```css
/* 引导页模式：.ct.ob-mode 禁用自身滚动和 padding */
.ct.ob-mode {
  overflow: hidden !important;
  padding: 0 !important;
  display: flex !important;
}
.ct.ob-mode > .lobby {
  margin: 0 !important;
  height: 100% !important;
}
```

### 3.2 单屏居中布局

```css
.ob-content {
  max-width: 720px;
  margin: 0 auto;
  padding: 0 24px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  min-height: 100%;
}
```

### 3.3 欢迎区域

```css
.ob-hero {
  text-align: center;
  padding: 0;
}
.ob-hero-logo img {
  width: 72px;
  height: 72px;
  border-radius: 18px;
  margin-bottom: 24px;
}
.ob-hero-title {
  font-size: 32px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--t1);
  margin: 0 0 12px;
}
.ob-hero-sub {
  font-size: 17px;
  color: var(--t2);
  margin: 0 0 40px;
}
.ob-hero-bind {
  display: flex;
  gap: 20px;
  justify-content: center;
  flex-wrap: wrap;
  margin-bottom: 32px;
}
.ob-hero-hint {
  font-size: 13px;
  color: var(--t3);
  margin: 0;
}
```

### 3.4 绑定卡片

品牌色通过 CSS 变量 `--brand` 传递到卡片，按钮统一用系统蓝色。

```css
.ob-bind-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 24px 28px;
  background: var(--c);
  border: 1px solid var(--b);
  border-radius: 18px;
  cursor: pointer;
  transition: transform 0.3s var(--apple-spring-ease, ease), border-color 0.3s ease;
  animation: obBindIn 0.6s var(--apple-spring-ease, ease) both;
}
.ob-bind-card:hover {
  transform: translateY(-4px);
  border-color: color-mix(in srgb, var(--brand, var(--a)) 30%, var(--b));
}
/* 平台图标：品牌色 tint 背景 + 品牌色图标 */
.ob-bind-icon {
  width: 48px;
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 12px;
  color: var(--brand, var(--a));
  background: color-mix(in srgb, var(--brand, var(--a)) 8%, transparent);
}
.ob-bind-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--t1);
}

@keyframes obBindIn {
  from { opacity: 0; transform: translateY(20px); }
  to { opacity: 1; transform: translateY(0); }
}
.ob-bind-card:nth-child(1) { animation-delay: 0.15s; }
.ob-bind-card:nth-child(2) { animation-delay: 0.25s; }
.ob-bind-card:nth-child(3) { animation-delay: 0.35s; }
```

### 3.5 移动端适配

```css
@media (max-width: 768px) {
  .ob-content { padding: 0 16px; }
  .ob-hero-title { font-size: 26px; }
  .ob-hero-sub { font-size: 15px; }
  .ob-hero-bind { flex-direction: column; align-items: center; }
  .ob-bind-card { min-width: 240px; }
}

@media (prefers-reduced-motion: reduce) {
  .ob-bind-card { animation: none; opacity: 1; transform: none; }
  .ob-bind-card:hover { transform: none; }
}
```

### 3.6 删除的 CSS

以下 CSS 类全部删除（app.css + apple-theme.css）：

| 类名 | 用途 |
|------|------|
| `.ob-progress-bar` / `.ob-progress-fill` | 滚动进度条 |
| `.ob-float-bind` / `.ob-float-show` | 浮动绑定按钮 |
| `.ob-section` / `.ob-section-head` / `.ob-section-num` / `.ob-section-title` / `.ob-section-desc` | Section 容器 |
| `.ob-wf-*` | 工作流演示 |
| `.ob-cap-*` | 差异化能力卡片 |
| `.ob-agent-*` | Agent 架构卡片 |
| `.ob-feat-*` | 基础设施特性卡片 |
| `.ob-cta-*` | 底部 CTA |
| `.ob-footer-*` | 引流入口 |
| `.ob-visible` / IO 相关 | 揭示动画 |

---

## 4. i18n 策略

### 4.1 保留的 key

| key | 用途 |
|-----|------|
| `ob_cta_title` | 欢迎语（"欢迎使用 Grape" / "Welcome to Grape"） |
| `ob_cta_sub` | 产品定位语 |
| `ob_bind_btn` | 绑定按钮文字 |
| `ob_hero_hint` | 底部提示（新增） |
| `ob_sticky_bind` | 保留（用户设置中可能引用） |

### 4.2 删除的 key

所有 `ob_wf_*`（工作流）、`ob_agent_*`（Agent）、`ob_feat_*`（特性）、`ob_footer_*`（引流）相关 key 从 `locales/zh/app.json` 和 `locales/en/app.json` 中删除。

---

## 5. 文件改动清单

| 文件 | 改动 |
|------|------|
| `src/api/static/js/app.js` | 删除 6 个渲染函数 + 2 个动画函数 + `_mapRange`；简化 `_renderOnboardingShell` 和 `_obHeroHtml`；简化 `_obExitMode` |
| `src/api/static/css/app.css` | 删除 ~400 行 `.ob-*` 样式（section/wf/cap/agent/feat/cta/footer/progress/float）；保留 ~60 行（hero/bind-card/move/reduced-motion） |
| `src/api/static/css/apple-theme.css` | `.ct.ob-mode` 规则保留（禁用 overflow/padding） |
| `src/api/static/locales/zh/app.json` | 删除 `ob_wf_*` / `ob_agent_*` / `ob_feat_*` / `ob_footer_*` key；新增 `ob_hero_hint` |
| `src/api/static/locales/en/app.json` | 同上 |

---

## 6. 验证清单

- [ ] JS 语法检查通过（`node --check app.js`）
- [ ] Playwright DOM 验证：引导页渲染绑定卡片
- [ ] 未绑定用户看到单屏页面，不需要滚动
- [ ] 不存在工作流/Agent/特性/CTA/引流元素
- [ ] 不存在进度条/浮动按钮
- [ ] 绑定卡片点击触发 OAuth 绑定流程
- [ ] 已绑定用户不看到引导页
- [ ] 中英文切换正常
- [ ] 移动端布局正常
- [ ] prefers-reduced-motion 降级正常
