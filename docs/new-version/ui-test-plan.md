# Agentic DevOps UI 测试方案（Playwright）

基于 `prototype.html` 中定义的 11 个页面和用户工作流文档 `user-workflow-overview.md`，
使用 **Playwright** 对 SPA 进行全链路自动化测试。

---

## 一、测试环境

```python
# 前置条件
from playwright.sync_api import sync_playwright
import subprocess, sys, os, time, requests

ROOT = r"项目根目录"

# 启动服务（独立端口避免冲突）
server = subprocess.Popen(
    [sys.executable, '-m', 'uvicorn', 'src.api.main:app',
     '--host', '127.0.0.1', '--port', '3099'],
    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(5)
```

---

## 二、测试总览

| 模块 | 页数 | 用例数 | 覆盖范围 |
|------|------|--------|----------|
| 登录 | 1 | 12 | GitCode OAuth / 密码登录 / 表单切换 |
| 仓库选择 | 1 | 8 | 首次登录 / 加入 / 托管 / 搜索 |
| Dashboard | 1 | 10 | 指标 / Agent 工作流 / 活动列表 |
| Pipeline | 1 | 8 | 活跃工作流 / 历史切换 / 状态 |
| Agent | 5 | 15 | 5 Agent 详情 / Config 面板 |
| Event | 1 | 8 | Context / 日志 / Info / 反馈 |
| Chat | 1 | 8 | 对话 / SSE 流 / Backend / IM 配置 |
| Settings | 1 | 8 | Credentials / Notifications / Skills |
| Skills | 1 | 6 | 分类展示 / 搜索 / View/Edit |
| Backend | 1 | 6 | 引擎状态 / 安装检测 / AK/SK |
| 侧边栏 + 导航 | 全局 | 10 | 链接 / 高亮 / 仓库切换 |
| **合计** | **14** | **99** | |

---

## 三、基础 Fixture

```python
import pytest
from playwright.sync_api import sync_playwright

@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()

@pytest.fixture
def page(browser):
    p = browser.new_page(viewport={"width": 1440, "height": 900})
    yield p
    p.close()

@pytest.fixture
def api():
    """API 请求辅助"""
    import requests
    return requests

# Helper: 创建测试用户并获取 token
def create_test_user(api) -> tuple[str, int]:
    from src.user import db as user_db
    uid = user_db.create_user("ui_test", "test123")
    r = api.post("http://127.0.0.1:3099/api/auth/login",
        json={"username": "ui_test", "password": "test123"})
    return r.json()["token"], uid["id"]

# Helper: 设置 localStorage 并重载
def login_as(page, token: str):
    page.evaluate("localStorage.setItem('token', '" + token + "')")
    page.evaluate("localStorage.setItem('app_cache_ver', '2')")
    page.goto("http://127.0.0.1:3099")
    page.wait_for_load_state("networkidle")
    time.sleep(1)

# Helper: 插入测试事件到 DB
def insert_test_event(repo_key: str):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (repo_key, event_type, agent_type, title, payload, status) VALUES (?,?,?,?,?,?)",
            (repo_key, "push", "vod", "Test event", "{}", "completed"))
        seq = c.lastrowid
        db.commit()
    return seq
```

---

## 四、分页测试用例

### 4.1 登录页

| # | 用例 | Playwright 断言 | 对应原型 |
|---|------|----------------|----------|
| L1 | 未登录时显示 GitCode OAuth 按钮 | `#gitBtn.is_visible()` 为 true | §2.1 |
| L2 | 页面标题 "Agentic DevOps" | `.lbox h1` 文本匹配 | §2.1 |
| L3 | 副标题 "致力于实现 Loop Engineer" | `.lbox .sub` 文本匹配 | §2.1 |
| L4 | 密码登录切换链接可见 | `#loginLinks a` clickable | §2.1 |
| L5 | 点击密码登录 → GitCode 按钮隐藏 | `#gitBtn.is_hidden()` 为 true | §2.1 |
| L6 | 密码表单展开（含用户名/密码输入框） | `#loginUser + #loginPass` 可见 | §2.1 |
| L7 | 返回 GitCode 登录链接可见 | 密码表单底部返回链接 clickable | §2.1 |
| L8 | 点击返回 → GitCode 按钮恢复 | `#gitBtn.is_visible()` 为 true | §2.1 |
| L9 | 密码登录提交空字段不跳转 | 点击登录无页面变化 | §2.1 |
| L10 | 密码登录错误凭据显示错误提示 | toast 消息"登录失败" | §2.1 |
| L11 | GitCode OAuth 按钮点击跳转 | 检查 redirect_url 含 `gitcode.com/oauth` | §2.1 |
| L12 | 404 页面不泄露内部信息 | 访问不存在路径返回 SPA 入口 | §2.1 |

```python
class TestLogin:
    def test_login_page_elements(self, page):
        page.goto("http://127.0.0.1:3099")
        page.wait_for_load_state("networkidle")
        assert page.locator("#gitBtn").is_visible()
        assert page.locator(".lbox h1").inner_text() == "Agentic DevOps"
        assert "Loop Engineer" in page.locator(".lbox .sub").inner_text()

    def test_password_toggle(self, page):
        page.goto("http://127.0.0.1:3099")
        page.wait_for_load_state("networkidle")
        page.locator("#loginLinks a").click()
        time.sleep(0.3)
        assert page.locator("#gitBtn").is_hidden()
        assert page.locator("#loginUser").is_visible()
        assert page.locator("#loginPass").is_visible()

    def test_oauth_redirect(self, page, api):
        r = api.get("http://127.0.0.1:3099/api/auth/login/gitcode")
        assert r.status_code in (200, 400)  # 400 if OAuth not configured
        if r.status_code == 200:
            assert "gitcode.com/oauth" in r.json()["redirect_url"]
```

### 4.2 仓库选择页

| # | 用例 | Playwright 断言 | 对应原型 |
|---|------|----------------|----------|
| RS1 | 首次登录（无 managed_repos）显示选择页 | `text=选择你要加入的` 可见 | §2.1.1 |
| RS2 | 仓库卡片列表渲染 | `.rc` 数量 > 0 | §2.1.1 |
| RS3 | 侧边栏在选择页中隐藏 | `#sidebar.is_hidden()` 为 true | §2.1.1 |
| RS4 | 主内容区在选择页中隐藏 | `#mainView.is_hidden()` 为 true | §2.1.1 |
| RS5 | "新增仓库"卡片在首位 | 首个 `.rc` 包含 "新增仓库" | §2.1.1 |
| RS6 | 搜索框可见 | `input[placeholder*="搜索"]` 可见 | §2.1.1 |
| RS7 | 角色过滤器下拉可见 | `select` 含 "全部/成员/管理员" | §2.1.1 |
| RS8 | 每张卡片含仓库名/角色/Owner/按钮 | 每个 `.rc` 含 `.rh .rn` + `.rf .bt` | §2.1.1 |

```python
class TestRepoSelection:
    def test_first_login_shows_selection(self, page, api):
        token, uid = create_test_user(api)
        login_as(page, token)
        assert page.locator("text=选择你要加入的").is_visible()
        assert page.locator(".rc").count() > 0
        assert page.locator("#sidebar").is_hidden()

    def test_join_repo_redirects_to_dashboard(self, page, api):
        token, uid = create_test_user(api)
        api.post("http://127.0.0.1:3099/api/repos/join",
            json={"full_name": "org/test-repo"},
            headers={"Authorization": "Bearer " + token})
        login_as(page, token)
        page.evaluate("localStorage.removeItem('currentRepo')")
        page.goto("http://127.0.0.1:3099")
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        assert page.locator("#pageTitle").inner_text() == "Dashboard"
        assert page.locator("#sidebar").is_visible()

    def test_repo_card_structure(self, page, api):
        token, uid = create_test_user(api)
        login_as(page, token)
        first_card = page.locator(".rc").nth(1)  # skip "新增仓库"
        assert first_card.locator(".rn").is_visible()  # repo name
        assert first_card.locator(".rf .bt").is_visible()  # join button
```

### 4.3 Dashboard 页

| # | 用例 | Playwright 断言 | 对应原型 |
|---|------|----------------|----------|
| D1 | 有 managed_repo 时登录进入 Dashboard | `#pageTitle` 文本为 "Dashboard" | §2.2 |
| D2 | 仓库标题显示 | `#repoTitle` 内容不为空 | §2.2 |
| D3 | 渐变头部卡片可见 | `.cd` 含渐变色背景 | §2.2 |
| D4 | 3 个统计指标 | `.stats .st` 数量 = 3 | §2.2 |
| D5 | 5 个 Agent 工作流卡片 | `.af .nd` 数量 = 5 | §2.2 |
| D6 | Chat Agent 连接条 | `.cl` 可见，文本含 "Chat Agent" | §2.2 |
| D7 | 最近活动事件列表 | `.ai` 数量 > 0 | §2.2 |
| D8 | 点击活动事件跳转 Event Detail | 点击 `.ai` 后 URL/页面切换 | §2.2 → §2.5 |
| D9 | Dashboard 30s 自动刷新 | 等待 30s 检查数据更新 | §7.2 |
| D10 | 顶部搜索框可见 | `.srch input` 可见 | §2.2 |

```python
class TestDashboard:
    def test_dashboard_loaded(self, page, api):
        token, uid = create_test_user(api)
        api.post("http://127.0.0.1:3099/api/repos/join",
            json={"full_name": "org/test-repo"},
            headers={"Authorization": "Bearer " + token})
        login_as(page, token)
        page.evaluate("localStorage.removeItem('currentRepo')")
        page.goto("http://127.0.0.1:3099")
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        assert page.locator("#pageTitle").inner_text() == "Dashboard"
        assert page.locator(".af .nd").count() == 5
        assert page.locator(".cl").is_visible()

    def test_click_activity_opens_event(self, page, api):
        seq = insert_test_event("org/test-repo")
        token, uid = create_test_user(api)
        api.post("http://127.0.0.1:3099/api/repos/join",
            json={"full_name": "org/test-repo"},
            headers={"Authorization": "Bearer " + token})
        login_as(page, token)
        page.evaluate("localStorage.removeItem('currentRepo')")
        page.goto("http://127.0.0.1:3099")
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        if page.locator(".ai").count() > 0:
            page.locator(".ai").first.click()
            time.sleep(1)
            assert page.locator("#pageTitle").inner_text() == "Event Detail"
```

### 4.4 Pipeline 页

| # | 用例 | Playwright 断言 | 对应原型 |
|---|------|----------------|----------|
| P1 | Pipeline 页面加载 | `#pageTitle` 文本为 "Pipeline" | §2.3 |
| P2 | 活跃 Pipeline 显示 Issue ID | 工作流标题包含 `#` + 数字 | §2.3 |
| P3 | 5 Agent 工作流状态卡片 | `.af .nd` 数量 = 5，各有状态点 | §2.3 |
| P4 | 历史表格含 Duration/Agents/Result/Release | `th` 包含 "Duration" 和 "Release" | §2.3 |
| P5 | 点击历史行切换活跃工作流 | 点击不同行，上方工作流变化 | §2.3 |
| P6 | 选中行高亮（sel class） | 点击后该行有 `sel` class | §2.3 |
| P7 | 活跃计数显示 | `text=Active:` 包含数字 | §2.3 |
| P8 | 无 Issue 时显示空状态 | 空数据时不报错，显示 "—" | §2.3 |

```python
class TestPipeline:
    def test_pipeline_page(self, page, api):
        token, uid = create_test_user(api)
        api.post("http://127.0.0.1:3099/api/repos/join",
            json={"full_name": "org/test-repo"},
            headers={"Authorization": "Bearer " + token})
        login_as(page, token)
        page.locator("#nav-pipe").click()
        time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == "Pipeline"
        assert page.locator(".af .nd").count() == 5

    def test_click_history_switches_active(self, page, api):
        # 需提前在 events 表插入多 Issue 的数据
        token, uid = create_test_user(api)
        api.post("http://127.0.0.1:3099/api/repos/join",
            json={"full_name": "org/test-repo"},
            headers={"Authorization": "Bearer " + token})
        login_as(page, token)
        page.locator("#nav-pipe").click()
        time.sleep(1)
        rows = page.locator(".pipe-row")
        if rows.count() > 1:
            first_text = page.locator(".pipe-row.sel td").first.inner_text()
            rows.nth(1).click()
            time.sleep(0.5)
            second_text = page.locator(".pipe-row.sel td").first.inner_text()
            assert first_text != second_text
```

### 4.5 Agent 详情页（VOD / Issue / Coding / QA / SRE）

| # | 用例 | Playwright 断言 | 对应原型 |
|---|------|----------------|----------|
| A1 | 点击侧边栏 Agent 链接进入详情 | 5 个 Agent 各自 Navigation 正常 | §2.4 |
| A2 | Agent 彩色头部可见 | `.cd` 含 `background:#fde8f0` 等 | §2.4 |
| A3 | Agent 图标 + 名称 + 描述 | 头部含 Agent 名和描述文本 | §2.4 |
| A4 | Configure 按钮可见 | `button:has-text("Configure")` 可见 | §2.4 |
| A5 | Events 按钮可见 | `button:has-text("Events")` 可见 | §2.4 |
| A6 | 点击 Configure 展开 Config 面板 | `#agentCfgPanel` 可见 | §2.4 |
| A7 | Config 面板含 Backend Engine 选择 | 面板内 `select` 含 OpenCode/CodeArts | §2.4 |
| A8 | Config 面板含 Skill 选择 | 面板内第二个 `select` 含 "Default" | §2.4 |
| A9 | Config 面板 Save/Cancel | Save 调用 API, Cancel 关闭面板 | §2.4 |
| A10 | 3 个统计指标 | `.stats .st` 数量 = 3 | §2.4 |
| A11 | Event History 列表 | `.ai` 列表可见 | §2.4 |
| A12 | Skill 显示 | `.tg-d` 含 "Default·skills/..." | §2.4 |
| A13 | 未知 Agent 返回 404 | 访问 `/api/agents/unknown/...` → 404 | §2.4 |
| A14 | 返回 Dashboard 链接 | 从 Agent 返回 Dashboard | §2.4 |
| A15 | 不同 Agent 颜色不同 | 各 Agent 头部背景色不同 | §2.4 |

```python
class TestAgents:
    @pytest.mark.parametrize("agent", ["vod", "issue", "coding", "qa", "sre"])
    def test_agent_page(self, page, api, agent):
        token, uid = create_test_user(api)
        api.post("http://127.0.0.1:3099/api/repos/join",
            json={"full_name": "org/test-repo"},
            headers={"Authorization": "Bearer " + token})
        login_as(page, token)
        page.locator("#nav-" + agent).click()
        time.sleep(1)
        assert page.locator("#pageTitle").inner_text() in \
            ["VOD Agent", "Issue Agent", "Coding Agent", "QA Agent", "SRE Agent"]
        assert page.locator("button:has-text('Configure')").is_visible()

    def test_agent_config_panel(self, page, api):
        token, uid = create_test_user(api)
        api.post("http://127.0.0.1:3099/api/repos/join",
            json={"full_name": "org/test-repo"},
            headers={"Authorization": "Bearer " + token})
        login_as(page, token)
        page.locator("#nav-vod").click()
        time.sleep(1)
        page.locator("button:has-text('Configure')").click()
        time.sleep(0.3)
        assert page.locator("#agentCfgPanel").is_visible()
        assert page.locator("#agentCfgPanel select").count() == 2
```

### 4.6 Event Detail 页

| # | 用例 | Playwright 断言 | 对应原型 |
|---|------|----------------|----------|
| E1 | 从 Agent 页面点击事件进入详情 | Event Detail 页面加载 | §2.5 |
| E2 | 返回按钮可见 | `button:has-text("返回")` 可见 | §2.5 |
| E3 | 左栏 Event Context | 文本含 "Trigger" 和 "Repository" | §2.5 |
| E4 | 左栏 Agent 执行日志 | 终端风格日志框可见 | §2.5 |
| E5 | 右栏 Event Info | 文本含 "Backend"/"Skill"/"Issue" | §2.5 |
| E6 | 右栏 Feedback 表单含 Title + Description | `#fbTitle` + `#fbBody` 可见 | §2.5 |
| E7 | Submit Feedback 按钮 | 点击提交调用 API | §2.5 |
| E8 | 事件不存在时显示 404 | 访问无效 seq 返回 404 | §2.5 |

```python
class TestEventDetail:
    def test_event_detail_page(self, page, api):
        seq = insert_test_event("org/test-repo")
        token, uid = create_test_user(api)
        api.post("http://127.0.0.1:3099/api/repos/join",
            json={"full_name": "org/test-repo"},
            headers={"Authorization": "Bearer " + token})
        login_as(page, token)
        page.goto("http://127.0.0.1:3099/")
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        app.nav("event", seq)  # 通过 JS 导航
        # 或用直接 API: page.goto 后解析
        assert page.locator("#pageTitle").inner_text() == "Event Detail"
        assert page.locator("button:has-text('返回')").is_visible()
```

### 4.7 Chat Agent 页

| # | 用例 | Playwright 断言 | 对应原型 |
|---|------|----------------|----------|
| C1 | Chat 页面加载 | `#pageTitle` 文本为 "Chat Agent" | §2.6 |
| C2 | 主聊天区可见 | `.chat-main` 可见 | §2.6 |
| C3 | 预置对话消息 | `.msg` 数量 > 0 | §2.6 |
| C4 | 输入框 + 发送按钮 | `.chat-in input` + `.chat-in .bt` 可见 | §2.6 |
| C5 | 右侧 5 Agent 面板 | `.chat-side .agc` 数量 = 5 | §2.6 |
| C6 | Backend 配置面板 | `.cd` 含 "Backend" | §2.6 |
| C7 | IM 配置面板 | `.cd` 含 "Feishu" / "WeCom" / "DingTalk" | §2.6 |
| C8 | SSE 流式回复测试 | 发送消息后见逐步输出 | §7.1 |

```python
class TestChat:
    def test_chat_page(self, page, api):
        token, uid = create_test_user(api)
        login_as(page, token)
        page.locator("#nav-chat").click()
        time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == "Chat Agent"
        assert page.locator(".chat-in input").is_visible()
        assert page.locator(".chat-side .agc").count() == 5

    def test_send_message(self, page, api):
        token, uid = create_test_user(api)
        login_as(page, token)
        page.locator("#nav-chat").click()
        time.sleep(1)
        page.locator(".chat-in input").fill("Hello")
        page.locator(".chat-in .bt").click()
        time.sleep(1)
        assert page.locator(".msg.l").count() > 0
```

### 4.8 Settings 页

| # | 用例 | Playwright 断言 | 对应原型 |
|---|------|----------------|----------|
| S1 | Settings 页面加载 | `#pageTitle` 文本为 "General Settings" | §2.9 |
| S2 | 3 个 Tab（Credentials/Notifications/Skills） | `.stabs a` 数量 = 3 | §2.9 |
| S3 | Credentials Tab 默认激活 | `.stabs a.ac` 文本为 "Credentials" | §2.9 |
| S4 | GitCode API Token 显示 | 文本含 "API Token" | §2.9 |
| S5 | 切换到 Notifications Tab | 点击后复选框可见 | §2.9 |
| S6 | Notifications 复选框 | 5 个 `input[type=checkbox]` | §2.9 |
| S7 | 切换到 Skills Tab | 点击后含 "Upload Skill" 按钮 | §2.9 |
| S8 | Skills Tab 含路由列表 | 含 `custom-pr-review` | §2.9 |

```python
class TestSettings:
    def test_settings_tabs(self, page, api):
        token, uid = create_test_user(api)
        login_as(page, token)
        page.locator("#nav-settings-credentials").click()
        time.sleep(1)
        assert page.locator(".stabs a").count() == 3
        assert page.locator(".stabs a.ac").inner_text() == "Credentials"

    def test_settings_notifications(self, page, api):
        token, uid = create_test_user(api)
        login_as(page, token)
        page.locator("#nav-settings-credentials").click()
        time.sleep(1)
        page.locator(".stabs a:nth-child(2)").click()
        time.sleep(0.3)
        assert page.locator("input[type='checkbox']").count() >= 3
```

### 4.9 Skills 页

| # | 用例 | Playwright 断言 | 对应原型 |
|---|------|----------------|----------|
| SK1 | Skills 页面加载 | `#pageTitle` 文本为 "DevOps Skills" | §2.6 Skills |
| SK2 | 5 个 Agent 分类展示 | 每个分类含标题（VOD/Issue/Coding/QA/SRE） | §2.6 Skills |
| SK3 | 搜索框可见 | `input[placeholder*="搜索"]` 可见 | §2.6 Skills |
| SK4 | 每个卡片含 View/Edit 按钮 | 每个技能卡片 `.bt.bt-o` 数量 ≥ 2 | §2.6 Skills |
| SK5 | 官方/用户标签 | 卡片 `.tg` 含 "官方技能" 或 "用户上传" | §2.6 Skills |
| SK6 | 卡片点击可交互 | 点击卡片无报错 | §2.6 Skills |

```python
class TestSkills:
    def test_skills_page(self, page, api):
        token, uid = create_test_user(api)
        login_as(page, token)
        page.locator("#nav-skills").click()
        time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == "DevOps Skills"
        # 验证 5 个 Agent 分类标题
        for name in ["VOD Agent", "Issue Agent", "Coding Agent", "QA Agent", "SRE Agent"]:
            assert page.locator("h3:has-text('" + name + "')").is_visible()
```

### 4.10 Backend Agents 页

| # | 用例 | Playwright 断言 | 对应原型 |
|---|------|----------------|----------|
| B1 | Backend Agents 页面加载 | `#pageTitle` 文本为 "Backend Agents" | §2.11 |
| B2 | 4 个引擎卡片（OpenCode/CodeArts/Hermes/Cursor） | `.cd` 数量 ≥ 4 | §2.11 |
| B3 | 运行状态圆点 | 每个卡片头部含状态圆点 | §2.11 |
| B4 | 版本号/路径显示 | 文本含 "Version:" / "Path:" | §2.11 |
| B5 | Test Connection 按钮 | `button:has-text("Test Connection")` 可见 | §2.11 |
| B6 | Refresh All 按钮 | `button:has-text("Refresh")` 可见 | §2.11 |

```python
class TestBackendAgents:
    def test_backend_page(self, page, api):
        token, uid = create_test_user(api)
        login_as(page, token)
        page.locator("#nav-backends").click()
        time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == "Backend Agents"
        assert page.locator("button:has-text('Test Connection')").is_visible()
        assert page.locator("button:has-text('Refresh')").is_visible()
```

### 4.11 侧边栏 + 全局导航

| # | 用例 | Playwright 断言 | 对应原型 |
|---|------|----------------|----------|
| N1 | 侧边栏所有入口可见 | 14 个导航链接可见 | §2.2 Sidebar |
| N2 | 点击导航切换页面 | 点击后 `#pageTitle` 变化 | §2.2 |
| N3 | 活跃导航高亮 | 当前页面对应链接有 `act` class | §2.2 |
| N4 | 仓库下拉切换 | 选择不同 repo 后 Dashboard 数据变化 | §2.2 |
| N5 | 用户信息显示 | `#userName` 显示用户名 | §2.2 |
| N6 | 登出按钮 | 点击 ↩ 返回登录页 | §2.2 |
| N7 | 搜索框输入后回车搜索 | 输入后回车，显示搜索结果 | §2.2 |
| N8 | 云开发环境按钮 | `button:has-text("云开发环境")` 可见 | §2.2 |
| N9 | 面包屑路径 | `.bc` 显示当前页面层级 | §2.2 |
| N10 | 浏览器前进/后退 | 浏览器导航按钮不导致白屏 | §2.2 |

```python
class TestNavigation:
    def test_all_nav_links(self, page, api):
        token, uid = create_test_user(api)
        api.post("http://127.0.0.1:3099/api/repos/join",
            json={"full_name": "org/test-repo"},
            headers={"Authorization": "Bearer " + token})
        login_as(page, token)
        nav_links = [
            ("#nav-dash", "Dashboard"),
            ("#nav-pipe", "Pipeline"),
            ("#nav-vod", "VOD Agent"),
            ("#nav-issue", "Issue Agent"),
            ("#nav-coding", "Coding Agent"),
            ("#nav-qa", "QA Agent"),
            ("#nav-sre", "SRE Agent"),
            ("#nav-skills", "DevOps Skills"),
            ("#nav-chat", "Chat Agent"),
            ("#nav-settings-credentials", "General Settings"),
            ("#nav-backends", "Backend Agents"),
        ]
        for link_id, expected_title in nav_links:
            page.locator(link_id).click()
            time.sleep(0.5)
            assert page.locator("#pageTitle").inner_text() == expected_title

    def test_active_nav_highlight(self, page, api):
        token, uid = create_test_user(api)
        api.post("http://127.0.0.1:3099/api/repos/join",
            json={"full_name": "org/test-repo"},
            headers={"Authorization": "Bearer " + token})
        login_as(page, token)
        page.locator("#nav-pipe").click()
        time.sleep(0.3)
        assert page.locator("#nav-pipe").get_attribute("class") == "act"
        page.locator("#nav-vod").click()
        time.sleep(0.3)
        assert page.locator("#nav-vod").get_attribute("class") == "act"
```

---

## 五、交互链路测试

覆盖从原型定义的端到端工作流：

| # | 链路 | 步骤 | 对应用例 |
|---|------|------|----------|
| F1 | 首次登录 → 选择仓库 → Dashboard | 登录 → 选择页 → 加入 → Dashboard | L1 + RS1 + RS2 |
| F2 | Dashboard → Agent 详情 | 点击 Agent 卡片 → Agent 页 | D5 + A1 |
| F3 | Agent 详情 → Config → Save | 点 Configure → 选引擎 → Save | A4 + A6 + A9 |
| F4 | Agent 详情 → Event Detail | 点击事件 → Event Detail | A11 + E1 |
| F5 | Event Detail → Feedback → Submit | 填表单 → 提交 → 成功提示 | E6 + E7 |
| F6 | Pipeline → 切换历史 | 点击历史行 → 活跃工作流变化 | P5 + P6 |
| F7 | Chat → SSE 流式对话 | 发消息 → 逐步输出 | C8 |
| F8 | Settings → 切换各 Tab | Credentials → Notifications → Skills | S1 + S5 + S7 |
| F9 | 侧边栏导航 → 页面切换 | 点击所有导航链接不报错 | N1 + N2 |
| F10 | 登出 → 重新登录 | 登出 → 回到登录页 → 再登录 | N6 |

```python
class TestInteractions:
    def test_full_onboarding_flow(self, page, api):
        """F1: 首次登录 → 选择仓库 → Dashboard"""
        token, uid = create_test_user(api)
        login_as(page, token)
        assert page.locator("text=选择你要加入的").is_visible()
        # 加入第一个非"新增仓库"的仓库
        repo_cards = page.locator(".rc")
        for i in range(1, repo_cards.count()):
            card = repo_cards.nth(i)
            if card.locator(".bt").is_visible():
                card.locator(".bt").click()
                time.sleep(2)
                break
        assert page.locator("#pageTitle").inner_text() == "Dashboard"

    def test_agent_to_event_to_feedback(self, page, api):
        """F4+F5: Agent → Event → Feedback"""
        seq = insert_test_event("org/test-repo")
        token, uid = create_test_user(api)
        api.post("http://127.0.0.1:3099/api/repos/join",
            json={"full_name": "org/test-repo"},
            headers={"Authorization": "Bearer " + token})
        login_as(page, token)
        # 通过 JS 导航到 Event Detail
        page.evaluate("app.nav('event', " + str(seq) + ")")
        time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == "Event Detail"
        # 提交 Feedback
        fb_title = page.locator("#fbTitle")
        fb_body = page.locator("#fbBody")
        if fb_title.is_visible():
            fb_title.fill("Test feedback")
            fb_body.fill("This is a test")
            page.locator("button:has-text('Submit Feedback')").click()
            time.sleep(0.5)
            assert "反馈" in (page.locator(".toast").inner_text() if page.locator(".toast").is_visible() else "")
```

---

## 六、边界与异常测试

| # | 场景 | 预期 |
|---|------|------|
| ERR1 | 后端未启动时访问 SPA | 显示网络错误页面 |
| ERR2 | Token 过期后调用 API | 自动跳转登录页 |
| ERR3 | GitCode API 返回 502 | 错误提示，不强制登出 |
| ERR4 | 空 managed_repos | 仓库选择页正常显示 |
| ERR5 | events 表为空 | Dashboard/Pipeline 空状态 |
| ERR6 | 移动端视口（375px） | 侧边栏折叠，内容适配 |
| ERR7 | 极窄视口（320px） | 不横向溢出 |
| ERR8 | 搜索无结果 | 显示空结果提示 |
| ERR9 | 快速连续点击导航 | 页面不崩溃 |
| ERR10 | localStorage 空间满 | 降级表现为未登录 |

```python
class TestEdgeCases:
    def test_mobile_viewport(self, page, api):
        page.set_viewport_size({"width": 375, "height": 812})
        token, uid = create_test_user(api)
        login_as(page, token)
        assert page.locator("#sidebar").is_hidden()  # 移动端侧边栏隐藏

    def test_empty_events(self, page, api):
        token, uid = create_test_user(api)
        api.post("http://127.0.0.1:3099/api/repos/join",
            json={"full_name": "org/test-repo"},
            headers={"Authorization": "Bearer " + token})
        login_as(page, token)
        page.locator("#nav-dash").click()
        time.sleep(1)
        # 即使 events 为空，Dashboard 也应加载
        assert page.locator("#pageTitle").inner_text() == "Dashboard"
```

---

## 七、执行方式

```bash
# 安装依赖
pip install playwright pytest
playwright install chromium

# 启动服务（独立终端）
python -m uvicorn src.api.main:app --host 127.0.0.1 --port 3099

# 运行全部测试
pytest tests/ui_test_playwright.py -v --tb=short

# 运行特定模块
pytest tests/ui_test_playwright.py::TestLogin -v
pytest tests/ui_test_playwright.py::TestDashboard -v

# 生成报告
pytest tests/ui_test_playwright.py --html=ui-report.html
```

**测试代码位置：** `tests/ui_test_playwright.py`（按本方案实现）

---

## 八、与 prototype 的对应关系

| 原型区域 | 页码 | 测试类 | 用例数 |
|---------|------|--------|--------|
| Login | §2.1 | `TestLogin` | 12 |
| 仓库选择 | §2.1.1 | `TestRepoSelection` | 8 |
| Dashboard | §2.2 | `TestDashboard` | 10 |
| Pipeline | §2.3 | `TestPipeline` | 8 |
| Agent Detail | §2.4 | `TestAgents` | 15 |
| Event Detail | §2.5 | `TestEventDetail` | 8 |
| Chat Agent | §2.6 | `TestChat` | 8 |
| Settings | §2.9 | `TestSettings` | 8 |
| Skills | §2.6 Skills | `TestSkills` | 6 |
| Backend Agents | §2.11 | `TestBackendAgents` | 6 |
| Sidebar/Nav | §2.2 | `TestNavigation` | 10 |
| 交互链路 | — | `TestInteractions` | 5 (场景) |
| 边界/异常 | — | `TestEdgeCases` | 6 |
| **合计** | | | **110** |
