"""DevOps Bot 场景测试 — 远端 http://203.0.113.2"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest, requests

BASE = "http://203.0.113.2"
TOKEN = None
TEST_REPO = "Jason7602/devops-bot"


# ==================== S0: 首次登录（无托管 → 仓库选择页）========================
class TestFirstLoginNoRepos:
    """首次登录场景：无托管项目时应进入仓库选择页而非 Dashboard
    
    前置条件：
    - managed_repos 表为空
    - 使用 Jason7602 账户（含 GitCode API Token）
    - 不设置 currentRepo
    """

    @pytest.fixture(scope="module")
    def browser(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            yield b

    def _setup(self):
        import paramiko, hashlib
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect('203.0.113.2', 22, 'root', 'YOUR_SSH_PASSWORD', timeout=10)
        c.exec_command("python3 -c \"import sqlite3;db=sqlite3.connect('/root/devops-bot/data/sessions.db');"
                       "db.execute('DELETE FROM managed_repos');db.commit();print('OK')\"", timeout=5)
        pw = hashlib.sha256(b"testpass123").hexdigest()
        c.exec_command(f'python3 -c "import sqlite3,hashlib;db=sqlite3.connect(\\\"/root/devops-bot/data/sessions.db\\\");'
                       f'db.execute(\\\"UPDATE user_accounts SET password_hash=?,password=? WHERE username=?\\\",'
                       f"(\\\"{pw}\\\",\\\"testpass123\\\",\\\"Jason7602\\\"));db.commit();print(\\\"OK\\\")\"", timeout=5)
        c.close()
        r = requests.post(f"{BASE}/api/auth/login",
            json={"username": "Jason7602", "password": "testpass123"}, timeout=10)
        assert r.status_code == 200, f"Jason7602 login failed: {r.text[:100]}"
        return r.json()["token"]

    def test_first_login_shows_repo_selection(self, browser):
        """首次登录 → 仓库选择页（而非 Dashboard）"""
        token = self._setup()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        # add_init_script must be on the context, not page, to persist across goto
        logs, errors = [], []
        page.on("console", lambda msg: logs.append(f"[{msg.type}] {msg.text[:200]}"))
        page.on("pageerror", lambda e: errors.append(str(e)))
        
        browser.contexts[0].add_init_script(f"""
        localStorage.setItem('token', '{token}');
        localStorage.setItem('app_cache_ver', '2');
        localStorage.removeItem('currentRepo');
        """)
        page.goto(BASE)
        page.wait_for_load_state("networkidle")
        time.sleep(3)

        on_selection = page.locator("text=选择你要加入的").is_visible()
        on_dashboard = page.locator("#pageTitle:has-text('Dashboard')").is_visible()
        repo_cards = page.locator(".rc").count()

        for l in logs:
            if any(k in l for k in ["init", "loadRepos", "error"]):
                print(f"  LOG: {l}")
        for e in errors:
            print(f"  ERR: {e}")

        assert on_selection, f"Expected selection page, got dash={on_dashboard} repo_cards={repo_cards}"
        assert not on_dashboard, "Should NOT be on Dashboard"
        assert repo_cards > 0, f"No repo cards rendered"
        assert len(errors) == 0, f"JS errors: {errors}"

        # 加入仓库 → Dashboard
        btn = page.locator(".bt:has-text('+ Join')").first
        if btn.is_visible():
            btn.click()
            time.sleep(3)
            assert page.locator("#pageTitle:has-text('Dashboard')").is_visible(), "Dashboard after join"
            print("  -> Dashboard after join: OK")

        page.close()

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
