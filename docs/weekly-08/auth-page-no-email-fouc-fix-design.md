# 认证页面无邮箱场景 FOUC 修复 — 补充设计

> **根因**：`auth-page-email-config-flicker-design.md` 第 2.2/2.3 节让 `stepIndicator`/`emailField`/`forgotStep1` 删除 `display:none`（初始可见），JS 在 `</body>` 前同步切换为隐藏。但浏览器渲染管线先渲染 HTML 初始状态再执行 JS——无邮箱场景用户会短暂看到表单再切换为"未配置"提示。这是 FOUC（Flash of Unstyled Content）。

## 一、问题定位

### 1.1 当前执行时序

```
1. 服务端返回 HTML（含 __EMAIL_CONFIGURED__=false 在 </head> 前）
2. 浏览器解析 HTML，从上到下渲染：
   a. 标题/副标题 — 可见
   b. stepIndicator — 可见（HTML 无 display:none）
   c. forgotStep1 表单 — 可见（HTML 无 display:none）
   d. forgotNotConfigured — 隐藏（HTML 有 display:none）
3. JS 在 </body> 前执行 applyEmailConfig()：
   - 隐藏 forgotStep1 + stepIndicator
   - 显示 forgotNotConfigured
4. 用户看到：表单闪一下 → 切换为"未配置"提示
```

### 1.2 受影响元素

| 文件 | 元素 | 当前初始状态 | 无邮箱时应为 |
|------|------|-------------|-------------|
| register.html | `#stepIndicator` | 可见 | 隐藏 |
| register.html | `#emailField` | 可见 | 隐藏 |
| register.html | `#regCodeArea` | 隐藏 (display:none) | 隐藏 ✅ |
| forgot.html | `#stepIndicator` | 可见 | 隐藏 |
| forgot.html | `#forgotStep1` | 可见 | 隐藏 |
| forgot.html | `#forgotNotConfigured` | 隐藏 (display:none) | 可见 |

### 1.3 为什么"同步 JS"不够

`__EMAIL_CONFIGURED__` 在 `</head>` 前注入，JS `applyEmailConfig()` 在 `</body>` 前执行——两者都是同步的。但浏览器渲染管线是：

1. 解析 HTML → 构建 DOM 树
2. 解析 CSS → 构建 CSSOM
3. 合并成 Render Tree
4. **Layout → Paint → Composite**（首次绘制）
5. 执行 `<script>`（除非有 `defer`/`async`）

`</body>` 前的内联 `<script>` 在步骤 5 执行，但首次绘制（步骤 4）已经完成——用户已经看到了 HTML 初始状态的渲染结果。

### 1.4 为什么不能简单加回 display:none

如果给 `#forgotStep1` 和 `#stepIndicator` 加回 `display:none`，有邮箱场景也会初始隐藏，然后 JS 切换为可见——闪动方向反了（从"隐藏→显示"），有邮箱场景的用户会看到闪动。

## 二、修复方案：CSS class 驱动显隐

### 2.1 核心思路

在 `</head>` 前注入的 `<script>` 中，除了设置 `window.__EMAIL_CONFIGURED__`，同时给 `<html>` 元素添加 class `email-configured` 或 `email-not-configured`。CSS 根据该 class 控制元素显隐——在 HTML 解析阶段（步骤 1-3）就确定显隐状态，首次绘制时就是正确状态。

### 2.2 main.py 改动

```python
def _inject_email_configured(html: str) -> str:
    val = 'true' if _is_email_configured() else 'false'
    cls = 'email-configured' if _is_email_configured() else 'email-not-configured'
    inject = f'<script>window.__EMAIL_CONFIGURED__={val};document.documentElement.className+=\" {cls}\";</script>'
    return html.replace('</head>', inject + '</head>', 1)
```

注入结果（无邮箱）：
```html
<script>window.__EMAIL_CONFIGURED__=false;document.documentElement.className+=" email-not-configured";</script>
```

此 script 在 `</head>` 前，浏览器解析到时立即执行——`<html>` 元素已有 class，后续 CSS 解析时根据该 class 确定显隐。

### 2.3 CSS 规则

在 register.html 和 forgot.html 的 `<style>` 块中添加：

```css
/* 无邮箱配置：隐藏邮箱相关元素，显示未配置提示 */
.email-not-configured #stepIndicator,
.email-not-configured #emailField,
.email-not-configured #forgotStep1 { display: none !important; }
.email-not-configured #forgotNotConfigured { display: block !important; }

/* 有邮箱配置：显示邮箱相关元素，隐藏未配置提示 */
.email-configured #forgotNotConfigured { display: none !important; }
/* emailField/stepIndicator/forgotStep1 默认可见，无需显式 display:block */
```

### 2.4 HTML 调整

**forgot.html**：
- `#forgotStep1`：保持无 `display:none`（CSS class 控制）
- `#stepIndicator`：保持无 `display:none`（CSS class 控制）
- `#forgotNotConfigured`：保持 `style="display:none"`（CSS `!important` 覆盖）

**register.html**：
- `#emailField`：保持无 `display:none`（CSS class 控制）
- `#stepIndicator`：保持无 `display:none`（CSS class 控制）
- `#regCodeArea`：保持 `style="display:none"`（不变，验证码区域始终初始隐藏）

### 2.5 JS 调整

`applyEmailConfig()` 中的 `style.display` 操作可以保留（JS 同步执行时 class 已设置，`!important` 优先级高于 inline style，不会冲突），也可以简化为只处理按钮和表单提交方式。

保留的理由：`!important` 确保无邮箱场景 CSS 优先，JS 的 `style.display` 操作在有邮箱场景生效（无 `!important` 冲突）。两层防护，安全。

### 2.6 兜底：__EMAIL_CONFIGURED__ 未定义

当注入失败（极端情况），`<html>` 无 `email-configured`/`email-not-configured` class，CSS 规则不匹配任何选择器：
- `#forgotStep1`/`#stepIndicator`/`#emailField`：默认可见（HTML 无 display:none）
- `#forgotNotConfigured`：`style="display:none"` → 隐藏

效果 = 有邮箱场景。这是安全的兜底——用户看到表单可以尝试提交，后端返回 `EMAIL_NOT_CONFIGURED` 错误。比"隐藏表单让用户无路可走"更好。

### 2.7 index.html（登录页）

`#forgotLink` 和 `#forgotDot` 初始 `style="display:none"`，JS 读 `__EMAIL_CONFIGURED__` 后显示。这里**不需要**改——登录页只有一个小链接，display:none → display:'' 的切换用户感知不到（不像表单那么大）。如果要彻底零闪动，也可以用同样的 CSS class 方案，但投入产出比低。

## 三、行为等价性验证

| 场景 | 修改前 | 修改后 | 等价 |
|------|--------|--------|------|
| 有邮箱+注册页 | JS 同步显示 | CSS class 显示（html 有 email-configured） | ✅ |
| 无邮箱+注册页 | JS 同步隐藏（但闪一下） | CSS class 隐藏（html 有 email-not-configured） | ✅ 修复闪动 |
| 有邮箱+忘记密码 | JS 同步显示 | CSS class 显示 | ✅ |
| 无邮箱+忘记密码 | JS 同步隐藏（但闪一下） | CSS class 隐藏 | ✅ 修复闪动 |
| OAuth 关联注册 | 跳过邮箱配置 | CSS class 仍生效，但 OAuth 模式 JS 直接 return | ✅ |
| 注入失败兜底 | JS 默认 false → 隐藏 | 无 class → 默认可见 | ⚠️ 行为变更 |

**兜底行为变更说明**：修改前注入失败时 JS `__EMAIL_CONFIGURED__` 未定义 → 默认 `false` → 隐藏表单。修改后注入失败时无 CSS class → 表单可见。新行为更安全——用户看到表单可以提交，后端返回错误；旧行为用户看到"未配置"提示但可能实际有邮箱（注入失败但邮箱可用）。这是改善不是回归。

## 四、影响文件

| 文件 | 改动 |
|------|------|
| `src/api/main.py` | `_inject_email_configured` 增加 html class 注入 |
| `src/api/static/register.html` | `<style>` 加 CSS 规则 |
| `src/api/static/forgot.html` | `<style>` 加 CSS 规则 |

不需要改动：index.html、grape_auth.py、i18n.js、auth.js、locale JSON、api.js

## 五、测试

### 5.1 新增静态断言（tests/js/test_auth_page_email_config.js）

- T40: main.py `_inject_email_configured` 注入 `email-configured` 或 `email-not-configured` class（F22）
  - 断言 `MAIN_PY` 含 `email-configured` 和 `email-not-configured` 字符串
- T41: register.html CSS 含 `.email-not-configured #emailField` 隐藏规则（F23）
  - 断言 `REGISTER_HTML` 含 `.email-not-configured #emailField` 且含 `display: none !important`
- T42: register.html CSS 含 `.email-not-configured #stepIndicator` 隐藏规则（F23）
  - 断言 `REGISTER_HTML` 含 `.email-not-configured #stepIndicator` 且含 `display: none !important`
- T43: forgot.html CSS 含 `.email-not-configured #forgotStep1` 隐藏规则（F23）
  - 断言 `FORGOT_HTML` 含 `.email-not-configured #forgotStep1` 且含 `display: none !important`
- T44: forgot.html CSS 含 `.email-not-configured #forgotNotConfigured` 显示规则（F23）
  - 断言 `FORGOT_HTML` 含 `.email-not-configured #forgotNotConfigured` 且含 `display: block !important`
- T45: forgot.html CSS 含 `.email-configured #forgotNotConfigured` 隐藏规则（F23）
  - 断言 `FORGOT_HTML` 含 `.email-configured #forgotNotConfigured` 且含 `display: none !important`

### 5.2 场景用例（tests/js/test_auth_page_scenarios.js）

- U10-1: 无邮箱场景 register.html CSS class 驱动 #stepIndicator 隐藏
  - 断言 `.email-not-configured #stepIndicator` 规则含 `display: none !important`
  - 断言 `#stepIndicator` HTML 无 `display:none`（由 CSS class 控制，不是 inline style）
- U10-2: 无邮箱场景 forgot.html CSS class 驱动 #forgotStep1 隐藏
  - 断言 `.email-not-configured #forgotStep1` 规则含 `display: none !important`
  - 断言 `#forgotStep1` HTML 无 `display:none`（由 CSS class 控制）
- U10-3: 无邮箱场景 forgot.html CSS class 驱动 #forgotNotConfigured 显示
  - 断言 `.email-not-configured #forgotNotConfigured` 规则含 `display: block !important`
  - 断言 `#forgotNotConfigured` HTML 有 `style="display:none"`（被 CSS `!important` 覆盖为 block）
- U10-4: 有邮箱场景 register.html CSS 不匹配 → #emailField 默认可见
  - 断言 `.email-configured` 规则不含 `#emailField`（有邮箱时元素默认可见，无需显式规则）
  - 断言 `#emailField` HTML 无 `display:none`

## 六、为什么不用其他方案

| 方案 | 优点 | 缺点 | 结论 |
|------|------|------|------|
| CSS class（本方案） | HTML 解析阶段确定状态，零闪动 | 需改 3 个文件 | ✅ 采用 |
| HTML 初始 display:none + JS 显示 | 简单 | 有邮箱场景从隐藏→显示，可能微闪 | ❌ 闪动方向反转 |
| `<script>` 在 `<head>` 中设置 CSS 变量 | 类似本方案 | CSS 变量浏览器兼容性 | ❌ 过度设计 |
| 服务端渲染不同 HTML | 彻底解决 | 需改后端路由逻辑，破坏静态文件缓存 | ❌ 过度设计 |
