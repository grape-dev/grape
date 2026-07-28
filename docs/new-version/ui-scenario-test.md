# Agentic DevOps 场景测试方案

基于 `user-workflow-overview.md` 定义的完整用户旅程，使用 Playwright 对**远端 DevOps Bot** (`http://203.0.113.2`) 进行端到端场景测试。

> **测试目标：** 覆盖用户从首次登录到日常使用的全部场景，确保每个场景的页面渲染、交互逻辑、数据加载符合设计文档。

---

## 一、测试环境

```python
from playwright.sync_api import sync_playwright
import time, requests

BASE = "http://203.0.113.2"

@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b

@pytest.fixture
def page(browser):
    p = browser.new_page(viewport={"width": 1440, "height": 900})
    yield p

def login(page, token):
    page.add_init_script(f"""
    localStorage.setItem('token', '{token}');
    localStorage.setItem('app_cache_ver', '2');
    localStorage.setItem('currentRepo', JSON.stringify({{fullName:'Jason7602/devops-bot',owner:'Jason7602',name:'devops-bot'}}));
    """)
    page.goto(BASE)
    page.wait_for_load_state("networkidle")
    time.sleep(2)

def get_token():
    r = requests.post(f"{BASE}/api/auth/login",
        json={"username": "test", "password": "test123"})
    return r.json()["token"]
```

---

## 二、登录场景测试（S1 重构）

将原 S1 拆解为 **5 个子场景**，从页面渲染到 OAuth 回调全链路覆盖，每步含诊断日志捕获用于定位"空页面"类故障。

### S1-A：登录页静态渲染

**目的：** 验证未登录状态下 SPA 正确渲染登录页

| # | 检查点 | 断言 | 诊断意义 |
|---|--------|------|----------|
| A1 | 页面标题 | `<title>` 含 "Agentic DevOps — Loop Engineer" | SEO/书签 |
| A2 | 登录覆盖层可见 | `.ln` 无 `hid` class | 登录覆盖层正确显示 |
| A3 | Logo 可见 | `.lbox .logo` 文本为 "A" | 品牌标识 |
| A4 | 主标题 | `.lbox h1` 文本为 "Agentic DevOps" | — |
| A5 | 副标题 | `.lbox .sub` 含 "Loop Engineer" | — |
| A6 | GitCode OAuth 按钮 | `#gitBtn` 可见，含 SVG + "使用 GitCode 登录" 文本 | — |
| A7 | 密码切换链接可见 | `#loginLinks a` 文本含 "用户名密码登录" | 切换入口 |
| A8 | 密码表单默认隐藏 | `#pwdLogin` 的 `display` 为 `none` | 默认状态 |
| A9 | SPA 已初始化 | `page.evaluate('typeof app')` 返回 `"object"` | JS 无报错 |
| A10 | 页面无 JS 错误 | `page.on("pageerror")` 无事件触发 | 语法/运行时正确 |

```python
class TestLoginA_Render:
    def test_login_page_render(self, page):
        logs, errors = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE)
        page.wait_for_load_state("networkidle")
        assert page.locator(".lbox h1").inner_text() == "Agentic DevOps"
        assert page.locator("#gitBtn").is_visible()
        assert "使用 GitCode 登录" in page.locator("#gitBtn").inner_text()
        assert page.locator("#loginLinks a").is_visible()
        assert page.evaluate('typeof window.app') == 'object'
        assert len(errors) == 0, f"JS Errors: {errors}"
```

---

### S1-B：密码登录切换交互

**目的：** 验证密码/OAuth 登录两种方式切换正确

| # | 操作 | 预期 |
|---|------|------|
| B1 | 点击"用户名密码登录" | `#gitBtn` 变为 `display:none` |
| B2 | — | `#loginLinks` 变为 `display:none` |
| B3 | — | `#pwdLogin` 变为 `display:block` |
| B4 | — | `#loginUser` 可见、`#loginPass` 可见 |
| B5 | — | 登录按钮可见 |
| B6 | — | 底部"返回 GitCode 登录"链接可见 |
| B7 | 点击"返回 GitCode 登录" | 恢复初始状态：`#gitBtn` 可见 |
| B8 | — | `#pwdLogin` 变回 `display:none` |
| B9 | 重复切换 3 次 | 每次状态无漂移，始终正确 |

```python
class TestLoginB_Toggle:
    def test_password_toggle(self, page):
        page.goto(BASE)
        page.wait_for_load_state("networkidle")
        # Click toggle
        page.locator("#loginLinks a").click()
        time.sleep(0.3)
        assert page.locator("#gitBtn").is_hidden()
        assert page.locator("#loginUser").is_visible()
        assert page.locator("#loginPass").is_visible()
        # Toggle back
        page.locator("#pwdLogin a").click()
        time.sleep(0.3)
        assert page.locator("#gitBtn").is_visible()
        assert page.locator("#pwdLogin").evaluate("el => getComputedStyle(el).display") == "none"
```

---

### S1-C：密码登录完整流程（核心诊断）

**目的：** 模拟用户输入凭据登录，验证 token 生成、页面跳转、3 秒内无异常重载

**诊断手段：**
- 捕获 `console.log` / `console.error` — 追踪 `init()` / `loadRepos()` 执行流
- 捕获 `pageerror` — 发现 JS 运行时异常
- 捕获 `request` / `response` — 追踪 API 调用链
- 3 秒后验证页面状态 — 检测是否触发 `location.reload()`

| # | 操作 | 预期 |
|---|------|------|
| C1 | 填写用户名 + 密码 | 输入框值正确 |
| C2 | 点击登录按钮 | 触发 `POST /api/auth/login` |
| C3 | API 返回 200 | `{"token": "...", "user": {...}}` |
| C4 | 登录后 500ms | `#sidebar` 显示（有仓库时）或 `.rc` 显示（首次） |
| C5 | 登录后 1s | `#pageTitle` 为 "Dashboard" 或仓库选择页 |
| C6 | 登录后 3s | 页面 URL 未变化（非 OAuth 时无跳转） |
| C7 | 登录后 3s | 页面未重新加载（无 `GET /` 请求） |
| C8 | 期间无 `pageerror` | JS 无异常 |
| C9 | 期间无 `console.error` | 无未捕获异常日志 |
| C10 | 验证 session token 有效 | `GET /api/auth/profile` 返回 200 |

```python
class TestLoginC_PasswordFlow:
    def test_password_login_flow(self, page):
        logs, errors, requests = [], [], []
        page.on("console", lambda msg: logs.append(f"[{msg.type}] {msg.text}"))
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda req: requests.append(req.url))
        
        page.goto(BASE)
        page.wait_for_load_state("networkidle")
        
        # Login
        page.locator("#loginLinks a").click()
        time.sleep(0.3)
        page.locator("#loginUser").fill("test")
        page.locator("#loginPass").fill("test123")
        page.locator("#pwdLogin .obtn").click()
        
        # Wait for API response + navigation
        time.sleep(3)
        
        # No JS errors
        assert len(errors) == 0, f"JS ERRORS: {errors}"
        
        # No page reload (no second GET /)
        reloads = [u for u in requests if u == BASE + "/" and requests.count(u) > 1]
        assert len(reloads) == 0, f"Page reload detected: {reloads}"
        
        # App state
        in_dash = page.locator("#pageTitle:has-text('Dashboard')").is_visible()
        in_selection = page.locator(".rc").first.is_visible()
        in_login = page.locator("#gitBtn").is_visible()
        assert in_dash or in_selection, f"Unknown state. Login still visible: {in_login}"
        
        # Token valid
        token = page.evaluate("localStorage.getItem('token')")
        r = requests.get(f"{BASE}/api/auth/profile", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, f"Token invalid: {r.status_code}"
        
        print(f"\n[DIAG] JS logs: {len(logs)} entries, Errors: {len(errors)}")
        for log in logs:
            if "init" in log or "loadRepos" in log or "nav" in log:
                print(f"  {log}")
```

---

### S1-D：OAuth 跳转 URL 验证

**目的：** 无需 GitCode 授权即可验证 OAuth 流程的前半段

| # | 操作 | 预期 |
|---|------|------|
| D1 | 调用 `GET /api/auth/login/gitcode` | 返回 `{"redirect_url": "..."}` |
| D2 | redirect_url 以 `https://gitcode.com/oauth/authorize` 开头 | OAuth 端点正确 |
| D3 | redirect_url 含 `client_id=5efa6f4b16c34741b1f357baee074f63` | 配置的 client_id |
| D4 | redirect_url 含 `redirect_uri=http%3A//203.0.113.2/api/auth/callback/gitcode` | 配置的回调地址 |
| D5 | redirect_url 含 `state=`（32 字节 hex） | CSRF 防护 |
| D6 | OAuth 未配置时返回 400 | 错误处理正确 |

```python
class TestLoginD_OAuthURL:
    def test_oauth_url(self, page):
        r = requests.get(f"{BASE}/api/auth/login/gitcode")
        assert r.status_code in (200, 400)
        if r.status_code == 200:
            url = r.json()["redirect_url"]
            assert url.startswith("https://gitcode.com/oauth/authorize")
            assert "client_id=5efa6f4b16c34741b1f357baee074f63" in url
            assert "redirect_uri=http%3A//203.0.113.2/api/auth/callback/gitcode" in url
            assert "state=" in url
```

---

### S1-E：OAuth 回调 API 验证

**目的：** 绕过 GitCode UI，直接测试回调端点的异常处理

**诊断手段：** 验证回调后的 302 重定向和 state 安全性

| # | 操作 | 预期 |
|---|------|------|
| E1 | 构造有效 state → 调用 callback | 302 重定向到 `/?token=xxx` |
| E2 | 验证 Location header 含 `token=` | token 带回到 SPA |
| E3 | 无效 state 调用 callback | 400 "state 无效或已过期" |
| E4 | 空 code 调用 callback | 400 "state 无效或已过期" |
| E5 | state 重用（第二次用同一 state）| 400（state 已消耗，不可重放） |
| E6 | OAuth scope 缺失 | 400 "GitCode 授权失败" |

```python
class TestLoginE_Callback:
    def test_callback_state_security(self):
        from src.user import db as user_db
        # Create a valid state
        import hashlib, os
        state = hashlib.sha256(os.urandom(32)).hexdigest()
        user_db.save_oauth_state(state, "gitcode", {})
        
        # First use: should fail on code validation (code is fake)
        r = requests.get(f"{BASE}/api/auth/callback/gitcode",
            params={"code": "invalid_code", "state": state})
        assert r.status_code in (302, 502)  # 302 if GitCode accepts, 502 if fails
        
        # Second use: state already consumed
        r2 = requests.get(f"{BASE}/api/auth/callback/gitcode",
            params={"code": "invalid_code", "state": state})
        assert r2.status_code == 400
```

---

### S1-F：完整 OAuth 登录流程（Playwright 自动化）

**目的：** 使用真实 GitCode 凭据，自动化完整 OAuth 流程：SPA → OAuth 按钮 → GitCode 登录 → 授权 → 回调 → 验证首次登录行为。

**前置条件：**
- Playwright 已安装 (`pip install playwright && playwright install chromium`)
- 环境变量 `GITCODE_USER` 和 `GITCODE_PASS` 已设置（或直接填入凭据）

| # | 操作 | 预期 |
|---|------|------|
| F1 | 访问 SPA，验证 GitCode OAuth 按钮可见 | `#gitBtn` 含 "使用 GitCode 登录" |
| F2 | 获取 OAuth URL | `GET /api/auth/login/gitcode` 返回 redirect_url |
| F3 | Playwright 跳转到 GitCode 授权页 | URL 含 `gitcode.com/oauth/authorize` |
| F4 | GitCode 要求登录时，填写用户名密码 | 使用 `GITCODE_USER` / `GITCODE_PASS` |
| F5 | 提交登录 | 跳转到授权页 |
| F6 | 点击"授权"按钮 | 允许 DevOps Bot 访问 |
| F7 | GitCode 回调到 `http://203.0.113.2/api/auth/callback/gitcode` | 302 重定向到 `/?token=xxx` |
| F8 | SPA 加载，`init()` 捕获 URL token | `[init] start` 日志含 `hasToken:true` |
| F9 | `loadRepos()` 获取仓库列表 | `[loadRepos] repos` 日志显示仓库数 |
| F10 | 首次登录（managed_repos 为空）| 显示仓库选择页，`.rc` 卡片 > 0 |
| F11 | 已有托管仓库时 | 直接进入 Dashboard |
| F12 | 全程无 `pageerror` | 0 JS 错误 |
| F13 | 全程无 `[loadRepos] FAILED` 日志 | catch handler 未触发 |

```python
import os, time, requests
from playwright.sync_api import sync_playwright

BASE = "http://203.0.113.2"
GITCODE_USER = os.environ.get("GITCODE_USER", "18617109891")
GITCODE_PASS = os.environ.get("GITCODE_PASS", "")

class TestLoginF_OAuthFlow:
    def test_full_oauth_flow(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            logs, errors = [], []
            page.on("console", lambda msg: logs.append(f"[{msg.type}] {msg.text[:200]}"))
            page.on("pageerror", lambda e: errors.append(str(e)))
            
            # F1: SPA
            page.goto(BASE)
            page.wait_for_load_state("networkidle")
            assert page.locator("#gitBtn").is_visible(), "GitCode login button"
            
            # F2-F3: Get OAuth URL and navigate
            r = requests.get(f"{BASE}/api/auth/login/gitcode")
            assert r.status_code == 200, "OAuth URL generation"
            auth_url = r.json()["redirect_url"]
            page.goto(auth_url)
            page.wait_for_load_state("networkidle")
            
            # F4: If GitCode login page appears
            if "login" in page.url.lower():
                for sel in ['input[name="user[login]"]', 'input[type="text"]', 'input[name="login"]']:
                    el = page.locator(sel).first
                    if el.is_visible():
                        el.fill(GITCODE_USER); break
                for sel in ['input[type="password"]']:
                    el = page.locator(sel).first
                    if el.is_visible():
                        el.fill(GITCODE_PASS); break
                for sel in ['button[type="submit"]', 'input[type="submit"]']:
                    el = page.locator(sel).first
                    if el.is_visible():
                        with page.expect_navigation(wait_until="networkidle", timeout=30000):
                            el.click()
                        break
            
            # F5-F6: Authorize
            time.sleep(2)
            for sel in ['button:has-text("授权")', 'input[value*="授权"]', 'button[type="submit"]']:
                el = page.locator(sel).first
                if el.is_visible():
                    with page.expect_navigation(wait_until="networkidle", timeout=30000) as nav:
                        el.click()
                    break
            
            # F7-F8: Wait for callback -> SPA
            time.sleep(5)
            if BASE not in page.url:
                page.goto(BASE)
                page.wait_for_load_state("networkidle")
            time.sleep(3)
            
            # F9-F11: Check page state
            on_selection = page.locator("text=选择你要加入的").is_visible()
            on_dashboard = page.locator("#pageTitle:has-text('Dashboard')").is_visible()
            on_login = page.locator("#gitBtn").is_visible()
            
            # F12: Validate
            assert len(errors) == 0, f"JS errors: {errors}"
            assert not any("[loadRepos] FAILED" in l for l in logs)
            assert on_selection or on_dashboard, f"Unexpected state: selection={on_selection} dash={on_dashboard} login={on_login}"
            
            # Diagnostics
            print(f"Selection: {on_selection}, Dashboard: {on_dashboard}, Login: {on_login}")
            print(f"JS errors: {len(errors)}")
            for l in logs:
                if any(k in l for k in ["init","loadRepos","error"]):
                    print(f"  {l}")
            
            browser.close()
```

## 三、其余场景测试（S2–S12）

### 场景 2：Dashboard 工作流监控

**对应文档：** §2.2 Dashboard

| 步骤 | 操作 | 预期 |
|------|------|------|
| 2.1 | 进入 Dashboard | 标题 "Dashboard"，渐变头部卡片 |
| 2.2 | Agent 工作流 | 5 个横向 Agent 卡片：VOD → Issue → Coding → QA → SRE |
| 2.3 | Chat Agent 连接条 | `.cl` 卡片可见 |
| 2.4 | 最近活动列表 | `.ai` 列表含时间/标签/标题 |
| 2.5 | 点击 Agent 卡片 | 跳转 Agent 详情页 |
| 2.6 | 顶部搜索框 | 输入后回车显示搜索结果 |
| 2.7 | "☁ 云开发环境"按钮 | 可见 |
| 2.8 | 30s 自动刷新 | 数据更新 |

```python
def test_dashboard(page, token):
    login(page, token)
    assert page.locator("#pageTitle").inner_text() == "Dashboard"
    assert page.locator(".af .nd").count() == 5
    page.locator(".af .nd").first.click()
    time.sleep(1)
    assert "Agent" in page.locator("#pageTitle").inner_text()
```

### 场景 3：Pipeline 工作流

**对应文档：** §2.3 Pipeline

| 步骤 | 操作 | 预期 |
|------|------|------|
| 3.1 | 进入 Pipeline | 标题 "Pipeline"，活跃/今日计数 |
| 3.2 | 活跃工作流 | 5 Agent 状态卡片（VOD→Issue→Coding→QA→SRE） |
| 3.3 | 状态圆点 | 绿=完成 / 黄=进行中 / 红=失败 / 灰=待处理 |
| 3.4 | 历史表格 | Duration / Agents / Result / Release 列 |
| 3.5 | 点击历史行 | 行高亮 + 活跃工作流切换 |

```python
def test_pipeline(page, token):
    login(page, token)
    page.evaluate("app.nav('pipe')"); time.sleep(1)
    assert page.locator("#pageTitle").inner_text() == "Pipeline"
    assert page.locator(".af .nd").count() == 5
```

### 场景 4：Agent 详情

**对应文档：** §2.4 DevOps Agent 详情页面

| Agent | 头部颜色 | 图标 |
|-------|----------|------|
| VOD | `#fde8f0` | ♫ |
| Issue | `#fce5e5` | ⚠ |
| Coding | `#e3edfd` | ⚙ |
| QA | `#f3e9fe` | ✔ |
| SRE | `#fef0e4` | ♡ |

```python
@pytest.mark.parametrize("agent_id,name", [
    ("vod","VOD Agent"),("issue","Issue Agent"),("coding","Coding Agent"),
    ("qa","QA Agent"),("sre","SRE Agent")])
def test_agent(page, token, agent_id, name):
    login(page, token)
    page.evaluate(f"app.nav('{agent_id}')"); time.sleep(1)
    assert page.locator("#pageTitle").inner_text() == name
    assert page.locator("button:has-text('Configure')").is_visible()
    page.locator("button:has-text('Configure')").click(); time.sleep(0.3)
    assert page.locator("#agentCfgPanel").is_visible()
```

### 场景 5：Event Detail

```python
def test_event(page, token):
    login(page, token)
    page.evaluate("app.nav('vod')"); time.sleep(1)
    ev = page.locator(".ai")
    if ev.count() > 0:
        ev.first.click(); time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == "Event Detail"
        assert page.locator("button:has-text('返回')").is_visible()
        assert page.locator("#fbTitle").is_visible()
```

### 场景 6：Chat Agent

```python
def test_chat(page, token):
    login(page, token)
    page.evaluate("app.nav('chat')"); time.sleep(1)
    assert page.locator("#pageTitle").inner_text() == "Chat Agent"
    assert page.locator(".chat-side .agc").count() == 5
    assert page.locator(".chat-in input").is_visible()
```

### 场景 7：Settings

```python
def test_settings(page, token):
    login(page, token)
    page.evaluate("app.nav('settings')"); time.sleep(1)
    assert page.locator(".stabs a").count() == 3
    page.locator(".stabs a:nth-child(2)").click(); time.sleep(0.3)
    assert page.locator("input[type='checkbox']").count() >= 3
```

### 场景 9：DevOps Skills

```python
def test_skills(page, token):
    login(page, token)
    page.evaluate("app.nav('skills')"); time.sleep(1)
    assert page.locator("#pageTitle").inner_text() == "DevOps Skills"
```

### 场景 10：Backend Agents

```python
def test_backends(page, token):
    login(page, token)
    page.evaluate("app.nav('backends')"); time.sleep(1)
    assert page.locator("#pageTitle").inner_text() == "Backend Agents"
```

### 场景 11：侧边栏导航

```python
def test_navigation(page, token):
    login(page, token)
    for view, expected in [
        ("dash","Dashboard"),("pipe","Pipeline"),("vod","VOD Agent"),
        ("skills","DevOps Skills"),("chat","Chat Agent"),
        ("settings","General Settings"),("backends","Backend Agents"),
    ]:
        page.evaluate(f"app.nav('{view}')"); time.sleep(0.5)
        assert page.locator("#pageTitle").inner_text() == expected
```

### 场景 12：日常开发工作流

```python
def test_daily(page, token):
    login(page, token)
    page.evaluate("app.nav('dash')"); time.sleep(0.5)
    assert page.locator("#pageTitle").inner_text() == "Dashboard"
    for aid in ["vod","issue","coding","qa","sre","chat"]:
        page.evaluate(f"app.nav('{aid}')"); time.sleep(0.5)
        assert page.locator("#pageTitle").inner_text() != ""
```

---

## 四、移动端响应式测试

| 视口 | 预期 |
|------|------|
| 375×812 (iPhone X) | 侧边栏隐藏，内容区全宽 |
| 320×568 (iPhone SE) | 无横向滚动条 |
| 768×1024 (iPad) | 布局正常 |

```python
def test_mobile(page, token):
    page.set_viewport_size({"width": 375, "height": 812})
    page.add_init_script(f"""localStorage.setItem('token','{token}');localStorage.setItem('app_cache_ver','2');
    localStorage.setItem('currentRepo',JSON.stringify({{fullName:'Jason7602/devops-bot',owner:'Jason7602',name:'devops-bot'}}));""")
    page.goto(BASE); page.wait_for_load_state("networkidle"); time.sleep(2)
    assert page.locator("#sidebar").is_hidden()
```

---

## 五、异常场景测试

| # | 场景 | 操作 | 预期 |
|---|------|------|------|
| E1 | Token 过期 | localStorage 存无效 token 后加载 | 自动跳转登录页 |
| E2 | GitCode API 502 | 停用网络后加载 | 错误提示，不强制登出 |
| E3 | 404 Event | `GET /api/events/detail/999999` | 404 页面 |
| E4 | 空数据 | managed_repos 为空时 | 仓库选择页正常显示 |
| E5 | 快速连续点击导航 | 快速点击 10 次导航 | 页面不崩溃 |
| E6 | 浏览器后退 | 从 Dashboard 后退 | 不出现白屏 |

---

## 六、添加 JS 追踪日志（app.js 修改）

在 `app.js` 的 `init()` 和 `loadRepos()` 中添加诊断日志，Playwright 测试通过 `page.on("console")` 捕获：

```javascript
function init() {
  console.log("[init] start", JSON.stringify({
    hasToken: !!localStorage.getItem('token'),
    cacheVer: localStorage.getItem('app_cache_ver'),
    currentRepo: localStorage.getItem('currentRepo'),
    url: window.location.href
  }));
  // ... rest of init ...
}

function loadRepos() {
  console.log("[loadRepos] start");
  API.get('/repos/member').then(d => {
    const repos = d.repos || [];
    const managedRepos = repos.filter(r => r.is_joined || r.is_hosted);
    console.log("[loadRepos] repos", JSON.stringify({
      total: repos.length,
      managed: managedRepos.length
    }));
    // ... rest of loadRepos ...
  }).catch(() => {
    console.error("[loadRepos] FAILED");
    // ... catch handler ...
  });
}
```

---

## 七、测试执行

```bash
# 安装依赖
pip install playwright pytest
playwright install chromium

# 运行登录场景测试（S1-A 到 S1-E）
pytest tests/ui_scenario_test.py -k "Login" -v --tb=short

# 运行全部测试
pytest tests/ui_scenario_test.py -v --tb=short

# 生成 HTML 报告
pytest tests/ui_scenario_test.py --html=scenario-report.html
```

---

## 八、定位"空页面"问题速查表

| 现象 | 可能原因 | 验证方式 |
|------|---------|---------|
| 登录后 2s 页面空白 | `loadRepos()` 失败，catch 隐藏 sidebar/mainView | 检查 S1-C 控制台日志：有无 `[loadRepos] FAILED` |
| 页面空白 + JS 报错 classList null | HTML 缺少 `#repoView` 元素 | 检查 `$('repoView').classList` |
| 页面重新加载（`GET /`） | `location.reload()` 触发 | 检查 S1-C 请求日志：多次 `GET /` |
| 无错误但页面空白 | JS 语法错误导致 `app` 未定义 | 检查 S1-A A9：`typeof app` |
| 偶发性空白 | Uvicorn 多实例/内存 session 丢失 | `pkill -f uvicorn` 后重启单实例 |
| OAuth 回调后空白 | `history.replaceState` 触发浏览器异常 | 检查 S1-C 日志：`[init] replaceState done` |

---

## 九、场景与文档对照表

| 场景 | 文档章节 | 测试步骤 | 涉及页面 |
|------|---------|---------|---------|
| S1-A 登录渲染 | §2.1 | 10 | Login |
| S1-B 切换交互 | §2.1 | 9 | Login |
| S1-C 密码登录 | §2.1 | 10 + 诊断 | Login → Dashboard/仓库选择 |
| S1-D OAuth URL | §2.1 | 6 | API |
| S1-E 回调验证 | §2.1 | 6 | API |
| S1-F OAuth 全流程 | §2.1 | 13 | Login → GitCode → SPA |
| S2 Dashboard | §2.2 | 8 | Dashboard |
| S3 Pipeline | §2.3 | 5 | Pipeline |
| S4 Agent 详情 | §2.4 | 5 × 5 | 5 Agent 页 |
| S5 Event Detail | §2.5 | 4 | Event Detail |
| S6 Chat Agent | §2.6 + §2.7 | 4 | Chat |
| S7 Settings | §2.9 | 3 | General Settings |
| S9 Skills | §2.6 Skills | 2 | Skills |
| S10 Backend | §2.11 | 2 | Backend Agents |
| S11 导航 | §2.2 | 7 | 全局 |
| S12 日常流程 | §2.12 | 7 | 全页面 |
| M1-M3 移动端 | — | 3 | 全局 |
| E1-E6 异常 | — | 6 | 多页面 |
| **合计** | | **120** | |
