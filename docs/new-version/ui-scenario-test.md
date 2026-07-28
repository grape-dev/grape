# Agentic DevOps 场景测试方案

基于 `user-workflow-overview.md` 定义的完整用户旅程，使用 Playwright 对**远端 DevOps Bot** (`http://203.0.113.2`) 进行端到端场景测试。

> **测试目标：** 覆盖用户从首次登录到日常使用的全部 12 个场景，确保每个场景的页面渲染、交互逻辑、数据加载符合设计文档。

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
    """通过 localStorage 注入 token 模拟登录态"""
    page.add_init_script(f"""
    localStorage.setItem('token', '{token}');
    localStorage.setItem('app_cache_ver', '2');
    localStorage.removeItem('currentRepo');
    """)
    page.goto(BASE)
    page.wait_for_load_state("networkidle")
    time.sleep(2)

def get_token():
    """获取有效 token（通过密码登录 API）"""
    r = requests.post(f"{BASE}/api/auth/login",
        json={"username": "test", "password": "test"})
    return r.json()["token"]
```

---

## 二、场景测试用例

### 场景 1：首次登录 → 仓库选择 → 加入仓库

**对应文档：** §2.1 首次登录 + §2.1.1 选择仓库页面

**前置条件：** 用户首次登录，尚未加入任何仓库（`managed_repos` 为空）

| 步骤 | 操作 | 预期结果 |
|------|------|----------|
| 1.1 | 访问首页 | 显示登录页：标题 "Agentic DevOps"、副标题 "致力于实现 Loop Engineer"、GitCode OAuth 按钮 |
| 1.2 | 点击"← 用户名密码登录" | GitCode 按钮隐藏，显示用户名/密码输入框和返回链接 |
| 1.3 | 输入凭据后登录 | 页面跳转到仓库选择页 |
| 1.4 | 仓库选择页展示 | 显示 "Welcome, {用户名}"、搜索框、角色过滤器、仓库卡片网格 |
| 1.5 | 检查首位卡片 | 第一个卡片是"＋ 新增仓库"（渐变虚线边框，点击跳转 GitCode） |
| 1.6 | 检查仓库卡片结构 | 每张卡片含：仓库名 `owner/repo`、角色标签、Owner 信息、`🚧 Not added` 状态、`+ Join` 或 `Host Now` 按钮 |
| 1.7 | 点击仓库的 `+ Join` 按钮 | 调用 API `/api/repos/join`，成功后进入 Dashboard |
| 1.8 | Dashboard 验证 | 显示仓库名称、渐变头部卡片、5 Agent 工作流、Chat Agent 连接条、最近活动 |
| 1.9 | 侧边栏验证 | 侧边栏可见，仓库下拉包含已加入的仓库，显示用户名和在线状态 |
| 1.10 | 返回仓库选择页 | 侧边栏仓库下拉选"选择仓库..."返回选择页，确认所有已加入的仓库显示"已加入"状态 |

```python
def test_scenario_1_first_login(page):
    """S1: 首次登录 → 仓库选择 → 加入 → Dashboard"""
    # Step 1.1-1.2: Login page
    page.goto(BASE)
    page.wait_for_load_state("networkidle")
    assert page.locator("#gitBtn").is_visible()
    assert page.locator(".lbox h1").inner_text() == "Agentic DevOps"
    page.locator("#loginLinks a").click()
    time.sleep(0.3)
    assert page.locator("#gitBtn").is_hidden()
    assert page.locator("#loginUser").is_visible()
```

---

### 场景 2：Dashboard 工作流监控

**对应文档：** §2.2 Dashboard

**前置条件：** 用户已加入至少一个仓库

| 步骤 | 操作 | 预期结果 |
|------|------|----------|
| 2.1 | 访问 Dashboard | 显示仓库名称、Webhook 状态标签、3 个指标（Auto-Resolution / Events/Week / Avg Response） |
| 2.2 | Agent 工作流区域 | 显示 5 个横向 Agent 卡片：VOD → Issue → Coding → QA → SRE，每个卡片含名称、描述、Backend 名、状态点、活动标签 |
| 2.3 | Chat Agent 连接条 | "Chat Agent · Personal Assistant" 卡片可见，含 Agent 标签按钮 |
| 2.4 | 最近活动列表 | 显示最近 5 条事件，每条含时间、Agent 标签、事件标题、状态 |
| 2.5 | 点击 Agent 卡片 | 跳转到对应 Agent 详情页 |
| 2.6 | 点击 Chat 连接条 | 跳转到 Chat Agent 页面 |
| 2.7 | 点击活动事件 | 跳转到 Event Detail 页面 |
| 2.8 | 顶部搜索框 | 输入关键字回车后显示搜索结果 |
| 2.9 | "☁ 云开发环境"按钮 | 按钮可见，点击弹出提示 |
| 2.10 | Dashboard 自动刷新 | 等待 30 秒后数据自动更新（验证活动列表变化） |

```python
def test_scenario_2_dashboard(page, token):
    """S2: Dashboard 工作流"""
    login(page, token)
    assert page.locator("#pageTitle").inner_text() == "Dashboard"
    # Agent flow: 5 cards
    assert page.locator(".af .nd").count() == 5
    # Chat lane
    assert page.locator(".cl").is_visible()
    assert "Chat Agent" in page.locator(".cl").inner_text()
    # Click agent card -> agent detail
    page.locator(".af .nd").first.click()
    time.sleep(1)
    assert "Agent" in page.locator("#pageTitle").inner_text()
```

---

### 场景 3：Pipeline 工作流查看与切换

**对应文档：** §2.3 Pipeline

**前置条件：** 仓库有多个 Issue 的事件记录

| 步骤 | 操作 | 预期结果 |
|------|------|----------|
| 3.1 | 进入 Pipeline 页面 | 显示标题 "Pipeline"、活跃 Pipeline 计数、今日完成数 |
| 3.2 | 活跃工作流 | 显示当前活跃 Issue 的 5 Agent 状态卡片（VOD→Issue→Coding→QA→SRE） |
| 3.3 | Agent 状态指示 | 每个卡片显示状态圆点（绿=完成 / 黄=进行中 / 红=失败 / 灰=待处理）和状态文字 |
| 3.4 | 历史表格 | 表格含 5 列：Pipeline / Duration / Agents / Result / Release |
| 3.5 | 点击历史行 | 选中行高亮（`sel` class），上方活跃工作流切换为该 Issue 的状态 |
| 3.6 | 连续切换 | 点击不同历史行，活跃工作流状态正确切换 |
| 3.7 | Agent 标签 | 表格中 Agents 列显示 V/I/C/Q/S 标签，颜色正确 |
| 3.8 | 无 Issue 时 | 显示空状态（无报错，显示 0 条） |

```python
def test_scenario_3_pipeline(page, token):
    """S3: Pipeline 工作流"""
    login(page, token)
    page.locator("#nav-pipe").click()
    time.sleep(1)
    assert page.locator("#pageTitle").inner_text() == "Pipeline"
    assert page.locator(".af .nd").count() == 5  # Agent status cards
    rows = page.locator(".pipe-row")
    if rows.count() > 1:
        rows.nth(1).click()
        time.sleep(0.5)
        assert "sel" in rows.nth(1).get_attribute("class")
```

---

### 场景 4：Agent 详情查看与配置

**对应文档：** §2.4 DevOps Agent 详情页面

**前置条件：** 用户已加入仓库

| 步骤 | 操作 | 预期结果 |
|------|------|----------|
| 4.1 | 进入 VOD Agent 页面 | 显示彩色头部（粉色背景）、Agent 图标 ♫、名称 "VOD Agent"、描述、Configure 和 Events 按钮 |
| 4.2 | 进入 Issue Agent 页面 | 红色背景头部、图标 ⚠、名称 "Issue Agent" |
| 4.3 | 进入 Coding Agent 页面 | 蓝色背景头部、图标 ⚙、名称 "Coding Agent" |
| 4.4 | 进入 QA Agent 页面 | 紫色背景头部、图标 ✔、名称 "QA Agent" |
| 4.5 | 进入 SRE Agent 页面 | 橙色背景头部、图标 ♡、名称 "SRE Agent" |
| 4.6 | 统计指标 | 3 个 Agent 专属指标卡片 |
| 4.7 | 事件历史 | 最近事件列表，点击事件跳转 Event Detail |
| 4.8 | Skill 配置 | 显示当前 Skill，含名称和路径 |
| 4.9 | 点击 Configure | 展开配置面板：Backend Engine 下拉选择器（OpenCode/CodeArts/Hermes/Cursor）、Skill 下拉选择器 |
| 4.10 | 选择 Backend 后 Save | 调用 API `/api/agents/{name}/...`，面板关闭，提示保存成功 |
| 4.11 | 点击 Cancel | 面板关闭，不保存 |

```python
@pytest.mark.parametrize("agent_id,name,color", [
    ("vod", "VOD Agent", "#fde8f0"),
    ("issue", "Issue Agent", "#fce5e5"),
    ("coding", "Coding Agent", "#e3edfd"),
    ("qa", "QA Agent", "#f3e9fe"),
    ("sre", "SRE Agent", "#fef0e4"),
])
def test_scenario_4_agent_detail(page, token, agent_id, name, color):
    """S4: Agent 详情"""
    login(page, token)
    page.locator(f"#nav-{agent_id}").click()
    time.sleep(1)
    assert page.locator("#pageTitle").inner_text() == name
    # Config button opens panel
    page.locator("button:has-text('Configure')").click()
    time.sleep(0.3)
    assert page.locator("#agentCfgPanel").is_visible()
```

---

### 场景 5：Event Detail 查看与反馈提交

**对应文档：** §2.5 Event Detail 页面

**前置条件：** 仓库有事件记录

| 步骤 | 操作 | 预期结果 |
|------|------|----------|
| 5.1 | 从 Agent 页面点击事件 | 进入 Event Detail 页面 |
| 5.2 | 返回按钮 | "← 返回 {Agent}" 按钮可见，点击返回上一页 |
| 5.3 | 左栏 Event Context | 显示 Trigger、Repository、Issue、Time 等上下文信息 |
| 5.4 | 左栏 Agent Execution Log | 终端风格日志框，显示 Agent 执行步骤（带时间戳） |
| 5.5 | 右栏 Event Info | 显示 Backend、Skill、Issue、Duration、Status |
| 5.6 | 右栏 Feedback 表单 | 含 Issue title 输入框、Description 文本域 |
| 5.7 | 提交 Feedback | 填写表单后点击 "Submit Feedback"，提示提交成功 |
| 5.8 | 直接访问不存在的事件 | 返回 404 错误提示 |

```python
def test_scenario_5_event_detail(page, token):
    """S5: Event Detail"""
    login(page, token)
    # Navigate to first agent's first event
    page.locator("#nav-vod").click()
    time.sleep(1)
    events = page.locator(".ai")
    if events.count() > 0:
        events.first.click()
        time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == "Event Detail"
        assert page.locator("button:has-text('返回')").is_visible()
        # Feedback form
        assert page.locator("#fbTitle").is_visible()
        assert page.locator("#fbBody").is_visible()
```

---

### 场景 6：Chat Agent 对话与配置

**对应文档：** §2.6 + §2.7 Chat Agent

**前置条件：** 用户已登录

| 步骤 | 操作 | 预期结果 |
|------|------|----------|
| 6.1 | 进入 Chat Agent 页面 | 显示标题 "Chat Agent · Personal Assistant" |
| 6.2 | 主聊天区 | 显示预置对话消息（机器人问候语和示例对话） |
| 6.3 | 输入框 + 发送按钮 | 输入框 placeholder "Ask me anything..."，Send 按钮 |
| 6.4 | 发送消息 | 输入文字后发送，消息出现在聊天区（左侧蓝色气泡），收到回复 |
| 6.5 | SSE 流式回复 | 发送后看到逐步输出的思考过程（思考中...→分析中...→完成） |
| 6.6 | 右侧 Agent 面板 | 5 个 Agent 状态卡片（VOD/Issue/Coding/QA/SRE），各带计数和彩色背景 |
| 6.7 | Backend 配置 | Type 下拉（Default/Custom）、URL 输入、API Key 输入 |
| 6.8 | IM 配置 | Feishu 连接状态（✓ 绿色）、App ID/Secret 输入、WeCom + 按钮、DingTalk + 按钮、"Scan QR to auto-configure" 提示 |

```python
def test_scenario_6_chat(page, token):
    """S6: Chat Agent"""
    login(page, token)
    page.locator("#nav-chat").click()
    time.sleep(1)
    assert page.locator("#pageTitle").inner_text() == "Chat Agent"
    # Agent sidebar
    assert page.locator(".chat-side .agc").count() == 5
    # Backend config
    assert page.locator(".chat-side select").is_visible()
    # IM config
    assert "Feishu" in page.locator(".chat-side").inner_text()
    # Send message
    page.locator(".chat-in input").fill("Hello")
    page.locator(".chat-in .bt").click()
    time.sleep(2)
    assert page.locator(".msg.l").count() > 0
```

---

### 场景 7：Settings 配置管理

**对应文档：** §2.9 General Settings 页面

**前置条件：** 用户已登录

| 步骤 | 操作 | 预期结果 |
|------|------|----------|
| 7.1 | 进入 General Settings | 显示标题 "General Settings"，3 个 Tab：Credentials / Notifications / Skills |
| 7.2 | Credentials Tab（默认） | 显示 GitCode API Token（已填充，disabled）、Account 信息、Huawei Cloud AK/SK 输入框 |
| 7.3 | 切换到 Notifications Tab | 显示 5 个复选框：Issue events / PR events / Build failures / Release events / VOD signals |
| 7.4 | 切换到 Skills Tab | 显示 "Upload Skill" 按钮、技能路由列表（含仓库名、事件类型、自定义 Skill 名、Edit 按钮） |
| 7.5 | 华为云 AK/SK 输入 | 输入后点击 Save，提示保存成功 |

```python
def test_scenario_7_settings(page, token):
    """S7: Settings"""
    login(page, token)
    page.locator("#nav-settings-credentials").click()
    time.sleep(1)
    assert page.locator("#pageTitle").inner_text() == "General Settings"
    assert page.locator(".stabs a").count() == 3
    # Notifications tab
    page.locator(".stabs a:nth-child(2)").click()
    time.sleep(0.3)
    assert page.locator("input[type='checkbox']").count() >= 3
```

---

### 场景 8：GitCode Repo 配置

**对应文档：** §2.10 GitCode Repo 页面

**前置条件：** 用户已加入仓库

| 步骤 | 操作 | 预期结果 |
|------|------|----------|
| 8.1 | 进入 GitCode Repo Settings | 显示 DevOps Bot Token 区域（Bot Token 输入、Scope 说明） |
| 8.2 | Webhook Bot 区域 | Webhook Token 输入、Webhook URL（只读）、Bot Username 输入 |
| 8.3 | Bot Token 输入后 Save | 提示配置已更新 |
| 8.4 | 默认 Bot Token 配置 | 支持对 `default` 条目配置系统默认 Token |

---

### 场景 9：DevOps Skills 浏览

**对应文档：** §2.6 Skills 库

**前置条件：** 用户已登录

| 步骤 | 操作 | 预期结果 |
|------|------|----------|
| 9.1 | 进入 Skills Library | 显示标题 "DevOps Skills" |
| 9.2 | 搜索框 | 可见，可输入搜索技能 |
| 9.3 | 按 Agent 分类展示 | 5 个分类区域：VOD Agent Skills / Issue Agent Skills / Coding Agent Skills / QA Agent Skills / SRE Agent Skills |
| 9.4 | 技能卡片 | 每张卡片含 Agent 图标、技能名称、类型标签（官方技能/用户上传）、描述、View/Edit 按钮 |
| 9.5 | 卡片计数 | 每个分类下至少 1 个技能 |

```python
def test_scenario_9_skills(page, token):
    """S9: Skills Library"""
    login(page, token)
    page.locator("#nav-skills").click()
    time.sleep(1)
    assert page.locator("#pageTitle").inner_text() == "DevOps Skills"
    for cat in ["VOD Agent", "Issue Agent", "Coding Agent", "QA Agent", "SRE Agent"]:
        assert page.locator(f"h3:has-text('{cat}')").is_visible()
```

---

### 场景 10：Backend Agents 引擎检测

**对应文档：** §2.11 Backend Agents 页面

**前置条件：** 用户已登录

| 步骤 | 操作 | 预期结果 |
|------|------|----------|
| 10.1 | 进入 Backend Agents | 显示标题 "Backend Agents" |
| 10.2 | 4 个引擎卡片 | OpenCode / CodeArts / Hermes / Cursor |
| 10.3 | 引擎状态 | 每个卡片头部有运行状态圆点（● 绿 / ● 灰） |
| 10.4 | 安装信息 | 显示 Version 和 Path 或 "Not Installed" |
| 10.5 | 操作按钮 | Test Connection / View Logs / Save AK/SK（仅 CodeArts） |
| 10.6 | Refresh All 按钮 | 可见 |

```python
def test_scenario_10_backends(page, token):
    """S10: Backend Agents"""
    login(page, token)
    page.locator("#nav-backends").click()
    time.sleep(1)
    assert page.locator("#pageTitle").inner_text() == "Backend Agents"
    backends = page.locator(".cd h3")
    names = [backends.nth(i).inner_text() for i in range(backends.count())]
    for eng in ["OpenCode", "CodeArts", "Hermes", "Cursor"]:
        assert eng in names, f"{eng} not found"
```

---

### 场景 11：侧边栏导航全链路

**对应文档：** §2.2 Sidebar

**前置条件：** 用户已加入仓库

| 步骤 | 操作 | 预期结果 |
|------|------|----------|
| 11.1 | 验证所有导航链接 | 侧边栏 11 个链接全部可点击，页面正确切换 |
| 11.2 | 活跃高亮 | 点击后当前页面链接有 `act` class，其他无 |
| 11.3 | 仓库下拉切换 | 切换仓库后 Dashboard 数据响应变化 |
| 11.4 | 用户信息 | 侧边栏底部显示用户名和角色（Admin/Member） |
| 11.5 | 登出 | 点击 ↩ 登出，返回登录页，清除 localStorage |

```python
def test_scenario_11_navigation(page, token):
    """S11: 导航全链路"""
    login(page, token)
    links = [
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
    for sel, expected in links:
        page.locator(sel).click()
        time.sleep(0.5)
        actual = page.locator("#pageTitle").inner_text()
        assert actual == expected, f"{sel}: expected '{expected}', got '{actual}'"
        assert "act" in (page.locator(sel).get_attribute("class") or "")
```

---

### 场景 12：日常开发工作流（端到端）

**对应文档：** §2.12 日常使用 + §3 Agent 工作流

**前置条件：** 仓库有完整的 5 Agent 事件记录

| 步骤 | 操作 | 预期结果 |
|------|------|----------|
| 12.1 | Dashboard 查看概览 | 看到 5 Agent 状态、最近活动 |
| 12.2 | 进入 Pipeline | 查看当前活跃 Issue 的完整 5 步工作流 |
| 12.3 | 查看 VOD Agent | 查看声音收集记录和信号分析 |
| 12.4 | 查看 Issue Agent | 查看 Issue 分类和指派记录 |
| 12.5 | 查看 Coding Agent | 查看 PR 提交记录 |
| 12.6 | 查看 QA Agent | 查看 Code Review 和测试结果 |
| 12.7 | 查看 SRE Agent | 查看发布记录和部署状态 |
| 12.8 | Chat Agent 查询 | 通过对话获取工作流状态 |
| 12.9 | 搜索 | 通过顶部搜索框搜索 Issue/PR 编号 |
| 12.10 | Event 详情 + 反馈 | 查看事件详情，提交反馈 |

```python
def test_scenario_12_daily_workflow(page, token):
    """S12: 日常开发工作流（全链路）"""
    login(page, token)
    
    # Step 1: Dashboard
    assert page.locator("#pageTitle").inner_text() == "Dashboard"
    
    # Step 2-7: Visit all 5 agents
    for agent_id in ["vod", "issue", "coding", "qa", "sre"]:
        page.locator(f"#nav-{agent_id}").click()
        time.sleep(0.5)
        assert "Agent" in page.locator("#pageTitle").inner_text()
    
    # Step 8: Chat
    page.locator("#nav-chat").click()
    time.sleep(0.5)
    assert page.locator("#pageTitle").inner_text() == "Chat Agent"
    
    # Step 9: Search
    page.locator("#searchInput").fill("issue")
    page.locator("#searchInput").press("Enter")
    time.sleep(1)
    assert "Search" in page.locator(".cd-h h2").inner_text()
```

---

## 三、移动端响应式测试

| 场景 | 视口 | 预期 |
|------|------|------|
| M1 | 375×812 (iPhone X) | 侧边栏隐藏，内容区全宽，Agent Flow 纵向排列 |
| M2 | 768×1024 (iPad) | 侧边栏可见或可折叠，布局正常 |
| M3 | 320×568 (iPhone SE) | 无横向滚动条，所有按钮可点击 |

```python
def test_mobile_responsive(page, token):
    page.set_viewport_size({"width": 375, "height": 812})
    login(page, token)
    assert page.locator("#sidebar").is_hidden()
    assert page.locator(".ct").is_visible()
```

---

## 四、异常场景测试

| 场景 | 操作 | 预期 |
|------|------|------|
| E1 | Token 过期后操作 | `api.js` 捕获 401，清除 token，跳转登录页 |
| E2 | GitCode API 不可达 | `GET /api/repos/member` 返回 502，显示错误提示，不强制登出 |
| E3 | 访问不存在的 Event | `GET /api/events/detail/999999` → 404 页面 |
| E4 | 空数据状态 | managed_repos 为空时显示仓库选择页；events 为空时 Dashboard 正常加载 |
| E5 | 快速连续点击导航 | 页面不崩溃，URL 不混乱 |
| E6 | 浏览器前进/后退 | SPA 路由正常，不出现白屏 |

---

## 五、测试执行

```bash
# 运行全部 12 个场景
pytest tests/ui_scenario_test.py -v --tb=short

# 运行特定场景
pytest tests/ui_scenario_test.py::test_scenario_1_first_login -v

# 生成 HTML 报告
pytest tests/ui_scenario_test.py --html=scenario-report.html
```

---

## 六、场景与文档对照表

| 场景 | 文档章节 | 测试步骤 | 涉及页面 |
|------|---------|---------|---------|
| S1 首次登录 | §2.1 + §2.1.1 | 10 | Login → 仓库选择 |
| S2 Dashboard | §2.2 | 10 | Dashboard |
| S3 Pipeline | §2.3 | 8 | Pipeline |
| S4 Agent 详情 | §2.4 | 11 | 5 Agent 详情页 |
| S5 Event Detail | §2.5 | 8 | Event Detail |
| S6 Chat Agent | §2.6 + §2.7 | 8 | Chat |
| S7 Settings | §2.9 | 5 | General Settings |
| S8 GitCode Repo | §2.10 | 4 | GitCode Repo |
| S9 Skills | §2.6 Skills | 5 | Skills Library |
| S10 Backend | §2.11 | 6 | Backend Agents |
| S11 导航 | §2.2 Sidebar | 5 | 全局 |
| S12 日常流程 | §2.12 + §3 | 10 | 全页面 |
| M1-M3 移动端 | — | 3 | 全局 |
| E1-E6 异常 | — | 6 | 多页面 |
| **合计** | | **99** | |
