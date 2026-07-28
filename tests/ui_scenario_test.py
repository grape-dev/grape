"""DevOps Bot 场景测试 — 远端 http://203.0.113.2"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest, requests

BASE = "http://203.0.113.2"
TOKEN = None
TEST_REPO = "Jason7602/devops-bot"  # Must be a real GitCode repo

@pytest.fixture(scope="session", autouse=True)
def setup():
    global TOKEN
    r = requests.post(f"{BASE}/api/auth/login",
        json={"username": "test", "password": "test123"}, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"Login failed: {r.text[:100]}")
    TOKEN = r.json()["token"]
    # Join a real repo
    requests.post(f"{BASE}/api/repos/join",
        json={"full_name": TEST_REPO},
        headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10)

@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b

@pytest.fixture
def page(browser):
    p = browser.new_page(viewport={"width": 1440, "height": 900})
    yield p

def _login(page):
    page.add_init_script(f"""
    localStorage.setItem('token','{TOKEN}');
    localStorage.setItem('app_cache_ver','2');
    localStorage.setItem('currentRepo', JSON.stringify({{fullName:'{TEST_REPO}',owner:'Jason7602',name:'devops-bot'}}));
    """)
    page.goto(BASE)
    page.wait_for_load_state('networkidle')
    time.sleep(2)

def _nav(page, view, param=None):
    if param is not None:
        page.evaluate(f"app.nav('{view}',{param})")
    else:
        page.evaluate(f"app.nav('{view}')")
    time.sleep(1.5)


# ==================== S1: 首次登录 ====================
class TestScenario1FirstLogin:
    def test_s1_login_page(self, page):
        page.goto(BASE)
        page.wait_for_load_state('networkidle')
        assert page.locator('#gitBtn').is_visible()
        assert page.locator('.lbox h1').inner_text() == "Agentic DevOps"
        page.locator('#loginLinks a').first.click()
        time.sleep(0.5)
        assert page.locator('#gitBtn').is_hidden()
        assert page.locator('#loginUser').is_visible()

    def test_s1_join_and_dashboard(self, page):
        _login(page)
        assert page.locator('#pageTitle').inner_text() == "Dashboard", "Should reach Dashboard"
        assert page.locator('#sidebar').is_visible(), "Sidebar visible"


# ==================== S2: Dashboard ====================
class TestScenario2Dashboard:
    def test_s2_dashboard(self, page):
        _login(page)
        _nav(page, 'dash')
        assert page.locator('#pageTitle').inner_text() == "Dashboard"
        page.wait_for_selector('.af .nd', timeout=15000)
        assert page.locator('.af .nd').count() == 5, "5 agent cards"


# ==================== S3: Pipeline ====================
class TestScenario3Pipeline:
    def test_s3_pipeline(self, page):
        _login(page)
        _nav(page, 'pipe')
        assert page.locator('#pageTitle').inner_text() == "Pipeline"
        page.wait_for_selector('.af .nd', timeout=15000)
        assert page.locator('.af .nd').count() == 5, "5 agent cards"
        rows = page.locator('.pipe-row')
        if rows.count() > 1:
            rows.nth(1).click()
            time.sleep(0.5)
            assert 'sel' in (rows.nth(1).get_attribute('class') or '')


# ==================== S4: Agents ====================
class TestScenario4Agents:
    @pytest.mark.parametrize("agent_id,name", [
        ("vod","VOD Agent"),("issue","Issue Agent"),("coding","Coding Agent"),
        ("qa","QA Agent"),("sre","SRE Agent")])
    def test_s4_agent(self, page, agent_id, name):
        _login(page)
        _nav(page, agent_id)
        assert page.locator('#pageTitle').inner_text() == name
        cfg = page.locator('button:has-text("Configure")')
        assert cfg.is_visible()
        cfg.click()
        time.sleep(0.3)
        assert page.locator('#agentCfgPanel').is_visible()
        assert page.locator('#agentCfgPanel select').count() >= 1


# ==================== S5: Event Detail ====================
class TestScenario5EventDetail:
    def test_s5_event(self, page):
        _login(page)
        _nav(page, 'vod')
        ev = page.locator('.ai')
        if ev.count() > 0:
            ev.first.click()
            time.sleep(1)
            assert page.locator('#pageTitle').inner_text() == "Event Detail"
            assert page.locator('button:has-text("返回")').is_visible()


# ==================== S6: Chat ====================
class TestScenario6Chat:
    def test_s6_chat(self, page):
        _login(page)
        _nav(page, 'chat')
        assert page.locator('#pageTitle').inner_text() == "Chat Agent"
        assert page.locator('.chat-side .agc').count() == 5


# ==================== S7: Settings ====================
class TestScenario7Settings:
    def test_s7_settings(self, page):
        _login(page)
        _nav(page, 'settings', "'credentials'")
        assert page.locator('#pageTitle').inner_text() == "General Settings"
        assert page.locator('.stabs a').count() == 3


# ==================== S9: Skills ====================
class TestScenario9Skills:
    def test_s9_skills(self, page):
        _login(page)
        _nav(page, 'skills')
        assert page.locator('#pageTitle').inner_text() == "DevOps Skills"


# ==================== S10: Backend ====================
class TestScenario10Backends:
    def test_s10_backends(self, page):
        _login(page)
        _nav(page, 'backends')
        assert page.locator('#pageTitle').inner_text() == "Backend Agents"


# ==================== S11: Navigation ====================
class TestScenario11Navigation:
    def test_s11_nav(self, page):
        _login(page)
        checks = [
            ("dash","Dashboard"),("pipe","Pipeline"),("vod","VOD Agent"),
            ("skills","DevOps Skills"),("chat","Chat Agent"),
            ("settings","General Settings"),("backends","Backend Agents"),
        ]
        for view, expected in checks:
            _nav(page, view)
            assert page.locator('#pageTitle').inner_text() == expected


# ==================== S12: Daily Workflow ====================
class TestScenario12DailyWorkflow:
    def test_s12_daily(self, page):
        _login(page)
        _nav(page, 'dash')
        assert page.locator('#pageTitle').inner_text() == "Dashboard"
        for aid in ["vod","issue","coding","qa","sre","chat"]:
            _nav(page, aid)
            assert page.locator('#pageTitle').inner_text() != ""


# ==================== Mobile ====================
class TestMobileResponsive:
    def test_mobile(self, page):
        page.set_viewport_size({"width": 375, "height": 812})
        _login(page)
        assert page.locator('#sidebar').is_hidden()
