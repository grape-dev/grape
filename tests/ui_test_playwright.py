"""Playwright UI automation tests"""
import sys, os, time, subprocess
import pytest, requests

# 端到端 UI 测试：需 Playwright 浏览器 + 拉起本地 uvicorn 服务。
# 默认套件排除（pytest.ini addopts = -m "not e2e_ui"），用 `pytest -m e2e_ui` 显式运行。
pytestmark = pytest.mark.e2e_ui

_SERVER = None
BASE = "http://127.0.0.1:3099"
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

# Helpers that bypass server DB lock via subprocess
def _sql(script):
    env = dict(os.environ)
    env.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))
    r = subprocess.run([sys.executable, "-c", script],
        capture_output=True, cwd=ROOT, env=env)
    return r.stdout.decode().strip()

@pytest.fixture(scope="session", autouse=True)
def setup():
    _sql(f"import sqlite3, os; db=sqlite3.connect(os.environ['DEVOPS_DB_PATH'],timeout=30); c=db.cursor(); "
         "c.execute('DELETE FROM user_accounts WHERE username=\"uitest\"');"
         "c.execute('DELETE FROM managed_repos');c.execute('DELETE FROM events');db.commit();db.close()")
    global _SERVER
    _SERVER = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'src.api.main:app',
        '--host', '127.0.0.1', '--port', '3099'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(5)
    _sql(f"import sys; sys.path.insert(0, r'{ROOT}'); "
         "from src.user import db as udb; print(udb.create_user('uitest','test123'))")
    yield
    if _SERVER: _SERVER.terminate(); _SERVER.wait()

@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()

@pytest.fixture
def page(browser):
    p = browser.new_page(viewport={"width": 1440, "height": 900})
    yield p
    p.close()

def token():
    return requests.post(BASE + "/api/auth/login",
        json={"username": "uitest", "password": "test123"}, timeout=5).json()["token"]

def login(page, tk):
    page.add_init_script(f"localStorage.setItem('token','{tk}');localStorage.setItem('app_cache_ver','2');localStorage.removeItem('currentRepo');")
    page.goto(BASE)
    page.wait_for_load_state("networkidle")
    time.sleep(1.5)

def join_repo(tk):
    requests.post(BASE + "/api/repos/join", json={"full_name": "org/test-repo"},
        headers={"Authorization": f"Bearer {tk}"}, timeout=5)

# ==================== Tests ====================
class TestLogin:
    def test_page_elements(self, page):
        page.goto(BASE); page.wait_for_load_state("networkidle")
        assert page.locator("#gitBtn").is_visible()
        assert page.locator(".lbox h1").inner_text() == "Agentic DevOps"

    def test_password_toggle(self, page):
        page.goto(BASE); page.wait_for_load_state("networkidle")
        page.locator(".lbox .links a").first.click()
        time.sleep(0.5)
        assert page.locator("#gitBtn").is_hidden()
        assert page.locator("#loginUser").is_visible()

    def test_password_toggle_back(self, page):
        page.goto(BASE); page.wait_for_load_state("networkidle")
        page.locator(".lbox .links a").first.click(); time.sleep(0.3)
        page.locator("#pwdLogin a").first.click(); time.sleep(0.3)
        assert page.locator("#gitBtn").is_visible()

class TestRepoSelection:
    def test_first_login_shows_selection(self, page):
        login(page, token())
        assert page.locator("text=选择你要加入的").is_visible()
        assert page.locator(".rc").count() > 0
        assert page.locator("#sidebar").is_hidden()

    def test_join_shows_dashboard(self, page):
        tk = token(); join_repo(tk)
        login(page, tk); page.goto(BASE); page.wait_for_load_state("networkidle"); time.sleep(2)
        assert page.locator("#pageTitle").inner_text() == "Dashboard"
        assert page.locator("#sidebar").is_visible()

class TestDashboard:
    def test_dashboard_loaded(self, page):
        tk = token(); join_repo(tk)
        page.add_init_script(f"localStorage.setItem('token','{tk}');localStorage.setItem('app_cache_ver','2');")
        page.goto(BASE); page.wait_for_load_state("networkidle"); time.sleep(2)
        assert page.locator("#pageTitle").inner_text() == "Dashboard"
        assert page.locator(".af .nd").count() == 5

    def test_agent_flow_labels(self, page):
        tk = token(); join_repo(tk)
        page.add_init_script(f"localStorage.setItem('token','{tk}');localStorage.setItem('app_cache_ver','2');")
        page.goto(BASE); page.wait_for_load_state("networkidle"); time.sleep(2)
        for i, lbl in enumerate(["VOD Agent","Issue Agent","Coding Agent","QA Agent","SRE Agent"]):
            assert lbl in page.locator(".af .nd").nth(i).inner_text()

class TestPipeline:
    def test_pipeline_page(self, page):
        tk = token(); join_repo(tk)
        page.add_init_script(f"localStorage.setItem('token','{tk}');localStorage.setItem('app_cache_ver','2');")
        page.goto(BASE); page.wait_for_load_state("networkidle"); time.sleep(1)
        page.locator("#nav-pipe").click(); time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == "Pipeline"

class TestAgents:
    @pytest.mark.parametrize("agent_id,name", [
        ("vod","VOD Agent"),("issue","Issue Agent"),("coding","Coding Agent"),
        ("qa","QA Agent"),("sre","SRE Agent")])
    def test_agent_page(self, page, agent_id, name):
        tk = token(); join_repo(tk)
        page.add_init_script(f"localStorage.setItem('token','{tk}');localStorage.setItem('app_cache_ver','2');")
        page.goto(BASE); page.wait_for_load_state("networkidle"); time.sleep(1)
        page.locator(f"#nav-{agent_id}").click(); time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == name

    def test_config_panel(self, page):
        tk = token(); join_repo(tk)
        page.add_init_script(f"localStorage.setItem('token','{tk}');localStorage.setItem('app_cache_ver','2');")
        page.goto(BASE); page.wait_for_load_state("networkidle"); time.sleep(1)
        page.locator("#nav-vod").click(); time.sleep(1)
        page.locator("button:has-text('Configure')").click(); time.sleep(0.3)
        assert page.locator("#agentCfgPanel").is_visible()

class TestEventDetail:
    def test_event_page(self, page):
        tk = token(); join_repo(tk)
        seq = _sql(f"import sqlite3, os; db=sqlite3.connect(os.environ['DEVOPS_DB_PATH'],timeout=30); c=db.cursor(); "
                   "c.execute('INSERT INTO events (repo_key,event_type,agent_type,title,payload,status) VALUES (\"org/test-repo\",\"push\",\"vod\",\"Test\",\"{}\",\"completed\")');db.commit();print(c.lastrowid);db.close()")
        page.add_init_script(f"localStorage.setItem('token','{tk}');localStorage.setItem('app_cache_ver','2');")
        page.goto(BASE); page.wait_for_load_state("networkidle"); time.sleep(1)
        page.evaluate(f"app.nav('event',{seq})"); time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == "Event Detail"

class TestChat:
    def test_chat_page(self, page):
        login(page, token())
        page.locator("#nav-chat").click(); time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == "Chat Agent"
        assert page.locator(".chat-in input").is_visible()

    def test_agent_sidebar(self, page):
        login(page, token())
        page.locator("#nav-chat").click(); time.sleep(1)
        assert page.locator(".chat-side .agc").count() == 5

class TestSettings:
    def test_tabs(self, page):
        login(page, token())
        page.locator("#nav-settings-credentials").click(); time.sleep(1)
        assert page.locator(".stabs a").count() == 3

    def test_notifications(self, page):
        login(page, token())
        page.locator("#nav-settings-credentials").click(); time.sleep(1)
        page.locator(".stabs a:nth-child(2)").click(); time.sleep(0.3)
        assert page.locator("input[type='checkbox']").count() >= 3

class TestSkills:
    def test_skills_page(self, page):
        login(page, token())
        page.locator("#nav-skills").click(); time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == "DevOps Skills"

class TestBackendAgents:
    def test_runtimes_page(self, page):
        login(page, token())
        page.locator("#nav-runtimes").click(); time.sleep(1)
        assert page.locator("#pageTitle").inner_text() == "Agent Runtimes"

class TestNavigation:
    def test_all_links(self, page):
        tk = token(); join_repo(tk)
        page.add_init_script(f"localStorage.setItem('token','{tk}');localStorage.setItem('app_cache_ver','2');")
        page.goto(BASE); page.wait_for_load_state("networkidle"); time.sleep(1)
        for sel, exp in [("#nav-dash","Dashboard"),("#nav-pipe","Pipeline"),("#nav-vod","VOD Agent"),
                         ("#nav-skills","DevOps Skills"),("#nav-chat","Chat Agent"),
                         ("#nav-settings-credentials","General Settings"),("#nav-runtimes","Agent Runtimes")]:
            page.locator(sel).click(); time.sleep(0.5)
            assert page.locator("#pageTitle").inner_text() == exp

    def test_active_highlight(self, page):
        tk = token(); join_repo(tk)
        page.add_init_script(f"localStorage.setItem('token','{tk}');localStorage.setItem('app_cache_ver','2');")
        page.goto(BASE); page.wait_for_load_state("networkidle"); time.sleep(1)
        page.locator("#nav-pipe").click(); time.sleep(0.3)
        assert "act" in (page.locator("#nav-pipe").get_attribute("class") or "")
        page.locator("#nav-vod").click(); time.sleep(0.3)
        assert "act" in (page.locator("#nav-vod").get_attribute("class") or "")

class TestEdgeCases:
    def test_mobile_viewport(self, page):
        page.set_viewport_size({"width": 375, "height": 812})
        tk = token(); join_repo(tk)
        page.add_init_script(f"localStorage.setItem('token','{tk}');localStorage.setItem('app_cache_ver','2');")
        page.goto(BASE); page.wait_for_load_state("networkidle"); time.sleep(1)
        assert page.locator("#sidebar").is_hidden()
