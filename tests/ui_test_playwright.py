"""Playwright UI 自动化测试 — 全部 API 操作避免 DB 锁冲突"""
import sys, os, time, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest, requests

_SERVER = None

@pytest.fixture(scope="session")
def browser():
    global _SERVER
    _SERVER = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'src.api.main:app',
         '--host', '127.0.0.1', '--port', '3099'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(5)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()
    if _SERVER:
        _SERVER.terminate()
        _SERVER.wait()

@pytest.fixture
def page(browser):
    p = browser.new_page(viewport={"width": 1440, "height": 900})
    yield p
    p.close()

API = "http://127.0.0.1:3099"

def _mkuser():
    """Create user via API"""
    r = requests.post(f"{API}/api/auth/login",
        json={"username": "uitest", "password": "test123"})
    if r.status_code == 200:
        return r.json()["token"]
    # User doesn't exist, create first
    from src.user import db as user_db
    user_db.create_user("uitest", "test123")
    r = requests.post(f"{API}/api/auth/login",
        json={"username": "uitest", "password": "test123"})
    return r.json()["token"]

def _login(page, token):
    page.add_init_script(f"""
    localStorage.setItem('token', '{token}');
    localStorage.setItem('app_cache_ver', '2');
    localStorage.removeItem('currentRepo');
    """)
    page.goto(API)
    page.wait_for_load_state("networkidle")
    time.sleep(1.5)

def _join(token):
    requests.post(f"{API}/api/repos/join",
        json={"full_name": "org/test-repo"},
        headers={"Authorization": f"Bearer {token}"})


# ==================== 4.1 Login ====================
class TestLogin:
    def test_page_elements(self, page):
        page.goto(API)
        page.wait_for_load_state("networkidle")
        assert page.locator("#gitBtn").is_visible()
        assert page.locator(".lbox h1").inner_text() == "Agentic DevOps"

    def test_password_toggle(self, page):
        page.goto(API)
        page.wait_for_load_state("networkidle")
        page.locator(".lbox .links a").first.click()
        time.sleep(0.5)
        assert page.locator("#gitBtn").is_hidden()
        assert page.locator("#loginUser").is_visible()

    def test_password_toggle_back(self, page):
        page.goto(API)
        page.wait_for_load_state("networkidle")
        page.locator(".lbox .links a").first.click()
        time.sleep(0.3)
        page.locator("#pwdLogin a").first.click()
        time.sleep(0.3)
        assert page.locator("#gitBtn").is_visible()


# ==================== 4.2 Repo Selection ====================
class TestRepoSelection:
    def test_first_login_shows_selection(self, page):
        tk = _mkuser()
        _login(page, tk)
        assert page.locator("text=选择你要加入的").is_visible()
        assert page.locator(".rc").count() > 0
        assert page.locator("#sidebar").is_hidden()

    def test_join_shows_dashboard(self, page):
        tk = _mkuser()
        _join(tk)
        page.add_init_script(f"""
        localStorage.setItem('token', '{tk}');
        localStorage.setItem('app_cache_ver', '2');
        """)
        page.goto(API)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        assert page.locator("#pageTitle").inner_text() == "Dashboard"
        assert page.locator("#sidebar").is_visible()


# ==================== 4.3 Dashboard ====================
class TestDashboard:
    def test_dashboard_loaded(self, page):
        tk = _mkuser()
        _join(tk)
        page.add_init_script(f"""
        localStorage.setItem('token', '{tk}');
        localStorage.setItem('app_cache_ver', '2');
        """)
        page.goto(API)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        assert page.locator("#pageTitle").inner_text() == "Dashboard"
        assert page.locator(".af .nd").count() == 5

    def test_agent_flow(self, page):
        tk = _mkuser()
        _join(tk)
        page.add_init_script(f"""
        localStorage.setItem('token', '{tk}');
        localStorage.setItem('app_cache_ver', '2');
        """)
        page.goto(API)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        labels = ["VOD Agent", "Issue Agent", "Coding Agent", "QA Agent", "SRE Agent"]
        for i, label in enumerate(labels):
            assert label in page.locator(".af .nd").nth(i).inner_text()


# ==================== 4.4 Pipeline ====================
class TestPipeline:
    def test_pipeline_page(self, page):
        tk = _mkuser()
        _join(tk)
        page.add_init_script(f"""
        localStorage.setItem('token', '{tk}');
        localStorage.setItem('app_cache_ver', '2');""")
        page.goto(API)
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        page.locator("#nav-pipe").click()
        time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == "Pipeline"


# ==================== 4.5 Agents ====================
class TestAgents:
    @pytest.mark.parametrize("agent_id,name", [
        ("vod", "VOD Agent"), ("issue", "Issue Agent"),
        ("coding", "Coding Agent"), ("qa", "QA Agent"), ("sre", "SRE Agent"),
    ])
    def test_agent_page(self, page, agent_id, name):
        tk = _mkuser()
        _join(tk)
        page.add_init_script(f"""
        localStorage.setItem('token', '{tk}');
        localStorage.setItem('app_cache_ver', '2');""")
        page.goto(API)
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        page.locator(f"#nav-{agent_id}").click()
        time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == name

    def test_config_panel(self, page):
        tk = _mkuser()
        _join(tk)
        page.add_init_script(f"""
        localStorage.setItem('token', '{tk}');
        localStorage.setItem('app_cache_ver', '2');""")
        page.goto(API)
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        page.locator("#nav-vod").click()
        time.sleep(1)
        page.locator("button:has-text('Configure')").click()
        time.sleep(0.3)
        assert page.locator("#agentCfgPanel").is_visible()
        assert page.locator("#agentCfgPanel select").count() >= 1


# ==================== 4.6 Event Detail ====================
class TestEventDetail:
    def test_event_page(self, page):
        # Create event via API
        tk = _mkuser()
        _join(tk)
        h = {"Authorization": f"Bearer {tk}"}
        requests.post(f"{API}/api/repos/join",
            json={"full_name": "org/test-repo"}, headers=h)
        # Insert event via server process (use a new connection)
        import subprocess as sp
        sp.run([sys.executable, "-c", """
import sqlite3
db = sqlite3.connect('data/sessions.db', timeout=30)
c = db.cursor()
c.execute("INSERT INTO events (repo_key, event_type, agent_type, title, payload, status) VALUES (?,?,?,?,?,?)",
          ("org/test-repo", "push", "vod", "Test", "{}", "completed"))
db.commit()
print(c.lastrowid)
db.close()
"""], cwd=os.path.join(os.path.dirname(__file__), ".."), capture_output=True, text=True)
        seq = int(sp.run([sys.executable, "-c", """
import sqlite3; db=sqlite3.connect('data/sessions.db',timeout=30); c=db.cursor()
c.execute('SELECT MAX(seq) FROM events WHERE repo_key=\"org/test-repo\"')
print(c.fetchone()[0]); db.close()
"""], cwd=os.path.join(os.path.dirname(__file__), ".."), capture_output=True, text=True).stdout.strip())

        page.add_init_script(f"""
        localStorage.setItem('token', '{tk}');
        localStorage.setItem('app_cache_ver', '2');""")
        page.goto(API)
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        page.evaluate(f"app.nav('event', {seq})")
        time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == "Event Detail"


# ==================== 4.7 Chat ====================
class TestChat:
    def test_chat_page(self, page):
        tk = _mkuser()
        _login(page, tk)
        page.locator("#nav-chat").click()
        time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == "Chat Agent"
        assert page.locator(".chat-in input").is_visible()

    def test_agent_sidebar(self, page):
        tk = _mkuser()
        _login(page, tk)
        page.locator("#nav-chat").click()
        time.sleep(1)
        assert page.locator(".chat-side .agc").count() == 5


# ==================== 4.8 Settings ====================
class TestSettings:
    def test_tabs(self, page):
        tk = _mkuser()
        _login(page, tk)
        page.locator("#nav-settings-credentials").click()
        time.sleep(1)
        assert page.locator(".stabs a").count() == 3

    def test_notifications(self, page):
        tk = _mkuser()
        _login(page, tk)
        page.locator("#nav-settings-credentials").click()
        time.sleep(1)
        page.locator(".stabs a:nth-child(2)").click()
        time.sleep(0.3)
        assert page.locator("input[type='checkbox']").count() >= 3


# ==================== 4.9 Skills ====================
class TestSkills:
    def test_skills_page(self, page):
        tk = _mkuser()
        _login(page, tk)
        page.locator("#nav-skills").click()
        time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == "DevOps Skills"


# ==================== 4.10 Backend Agents ====================
class TestBackendAgents:
    def test_backend_page(self, page):
        tk = _mkuser()
        _login(page, tk)
        page.locator("#nav-backends").click()
        time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == "Backend Agents"


# ==================== 4.11 Navigation ====================
class TestNavigation:
    def test_all_links(self, page):
        tk = _mkuser()
        _join(tk)
        page.add_init_script(f"""
        localStorage.setItem('token', '{tk}');
        localStorage.setItem('app_cache_ver', '2');""")
        page.goto(API)
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        checks = [
            ("#nav-dash", "Dashboard"), ("#nav-pipe", "Pipeline"),
            ("#nav-vod", "VOD Agent"), ("#nav-skills", "DevOps Skills"),
            ("#nav-chat", "Chat Agent"),
            ("#nav-settings-credentials", "General Settings"),
            ("#nav-backends", "Backend Agents"),
        ]
        for sel, exp in checks:
            page.locator(sel).click()
            time.sleep(0.5)
            assert page.locator("#pageTitle").inner_text() == exp, f"{sel}: got '{page.locator('#pageTitle').inner_text()}'"

    def test_active_highlight(self, page):
        tk = _mkuser()
        _join(tk)
        page.add_init_script(f"""
        localStorage.setItem('token', '{tk}');
        localStorage.setItem('app_cache_ver', '2');""")
        page.goto(API)
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        page.locator("#nav-pipe").click()
        time.sleep(0.3)
        assert "act" in (page.locator("#nav-pipe").get_attribute("class") or "")
        page.locator("#nav-vod").click()
        time.sleep(0.3)
        assert "act" in (page.locator("#nav-vod").get_attribute("class") or "")


# ==================== Edge Cases ====================
class TestEdgeCases:
    def test_mobile_viewport(self, page):
        page.set_viewport_size({"width": 375, "height": 812})
        tk = _mkuser()
        _join(tk)
        page.add_init_script(f"""
        localStorage.setItem('token', '{tk}');
        localStorage.setItem('app_cache_ver', '2');""")
        page.goto(API)
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        assert page.locator("#sidebar").is_hidden()
