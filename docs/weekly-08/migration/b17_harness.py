#!/usr/bin/env python3
"""Batch 17 admin migration harness: mock /api/admin/* on the real page via
playwright route interception, drive login → config → users → whitelists flow."""
import json, sys, time
from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:3000'
SHOTS = '/home/mys/grape/docs/weekly-08/screenshots'

USERS = [
    {"id": 1, "username": "alice", "nickname": "Alice", "email": "alice@x.com", "status": 1, "last_login_at": "2026-10-28 10:00"},
    {"id": 2, "username": "bob", "email": "bob@x.com", "status": 0, "last_login_at": None},
    {"id": 3, "username": "carol", "email": "carol@x.com", "status": 3, "last_login_at": "2026-10-27"},
]

def handle(route, req):
    p = req.url.replace(BASE + '/api/admin', '')
    m = req.method
    def j(body):
        route.fulfill(status=200, content_type='application/json', body=json.dumps(body))
    if p == '/login' and m == 'POST':
        j({"token": "T-" + str(int(time.time())), "user": {"username": "admin"}, "pwd_initial": False})
    elif p == '/config' and m == 'GET':
        j({"config": {"gitcode_api_token_present": True, "gitcode_api_token_tail": "abcd",
                      "github_bot_token_present": False, "github_bot_username": "grape-bot",
                      "github_bot_username_present": True,
                      "gitcode_oauth_client_id": "cid-123", "gitcode_oauth_client_id_present": True,
                      "github_oauth_client_id": "", "github_oauth_client_id_present": False,
                      "oauth_status": {"gitcode": True, "github": False},
                      "gitcode_oauth_redirect_uri": "", "github_oauth_redirect_uri": "",
                      "bot_usernames": ["grape_bot", "dev-bot"],
                      "webhook_url": "https://example.com/hook", "webhook_url_present": True,
                      "gitcode_webhook_token_present": True, "gitcode_webhook_token_tail": "wxyz",
                      "allow_signup": "0"}})
    elif p == '/config' and m == 'PUT':
        j({"ok": True})
    elif p == '/config/verify-token':
        j({"ok": True, "login": "grape-bot", "name": "Grape Bot"})
    elif p == '/bot-token-health':
        j({"overall": "degraded", "checked_at": "2026-10-29 09:00",
           "platforms": {"gitcode": {"unhealthy": False, "degraded": True, "error_type": "network",
                                     "error": "timeout", "consecutive_failures": 2}}})
    elif p.startswith('/users') and m == 'GET':
        j({"users": USERS, "total": len(USERS)})
    elif p == '/users' and m == 'POST':
        j({"ok": True})
    elif p == '/signup-whitelist':
        j({"whitelist": [{"id": 7, "type": "email", "value": "a@x.com", "remark": "", "created_at": "2026-10-01"}]})
    elif p == '/whitelist':
        j({"whitelist": [{"id": 3, "gitcode_username": "xocto", "github_username": "", "remark": "r", "created_at": "2026-10-02"}], "enabled": True})
    else:
        j({"ok": True})

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(('PASS ' if cond else 'FAIL ') + name)

with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp('http://127.0.0.1:9222')
    ctx = b.contexts[0] if b.contexts else b.new_context()
    page = ctx.new_page()
    page.route('**/api/admin/**', handle)

    page.goto(BASE + '/admin')
    page.evaluate("localStorage.removeItem('grape_admin_token')")
    page.goto(BASE + '/admin')
    page.wait_for_timeout(800)
    # login view visible
    check('login-view', page.locator('.admv-login').count() == 1)
    page.screenshot(path=f'{SHOTS}/b17-admin-login-light.png')

    # login
    page.fill('.admv-login .ipt >> nth=0', 'admin')
    page.fill('.admv-login .ipt >> nth=1', 'adminpass')
    page.click('.lbtn')
    page.wait_for_timeout(1200)
    check('main-frame', page.locator('.admv-main').count() == 1)
    # github_bot_username plaintext echo (3rd text input on cfg view)
    vals = [page.locator('.f-row input:not([type=password])').nth(i).input_value() for i in range(6)]
    print('  cfg plain inputs:', vals)
    check('cfg-github-username-echo', 'grape-bot' in vals)

    # config placeholders (token present → placeholder mentions tail)
    ph = page.locator('input[type=password]').first.get_attribute('placeholder')
    print('  token placeholder:', ph)
    check('cfg-token-placeholder', 'abcd' in (ph or ''))
    # verify token button → fill token first (legacy required non-empty input)
    page.locator('input[type=password]').first.fill('tok-test-123')
    btns = page.locator('.s-btn')
    btns.nth(0).click()
    page.wait_for_timeout(600)
    stv = page.locator('.st').first.text_content()
    print('  verify st:', stv)
    check('cfg-verify-token', 'grape-bot' in (stv or ''))
    # token health overall tag
    check('token-health-degraded', 'tag-w' in (page.locator('.tag-w').first.get_attribute('class') or '') and '降级' in (page.locator('.tag-w').first.text_content() or ''))
    page.screenshot(path=f'{SHOTS}/b17-admin-cfg-light.png')

    # users view
    page.click('.sb-nav a:has-text("用户列表")')
    page.wait_for_timeout(1500)
    print('  users view html len:', len(page.locator('.ct').inner_html() or ''))
    print('  requests:', page.evaluate("window.__reqs || []"))
    check('users-count', page.locator('.tbl tbody tr').count() == 3)
    check('users-status', '正常' in (page.locator('.tbl tbody tr').first.text_content() or ''))
    # row menu
    page.locator('.row-actions-btn').first.click()
    page.wait_for_timeout(200)
    check('row-menu-show', page.locator('.row-menu.show').count() == 1)
    # create user modal
    page.click('button:has-text("创建用户")')
    page.wait_for_timeout(300)
    check('user-modal', page.locator('.modal .mbox').count() >= 1)
    page.click('.modal .m-btn-o')
    page.wait_for_timeout(200)
    page.screenshot(path=f'{SHOTS}/b17-admin-users-light.png')

    # whitelist view
    page.click('.sb-nav a:has-text("注册白名单")')
    page.wait_for_timeout(1500)
    print('  after-wl-click ct text:', (page.locator('.ct').inner_text() or '')[:120].replace('\n', '|'))
    print('  wlrow count:', page.locator('.wlrow').count())
    check('wl-signup-rows', page.locator('.wlrow').count() == 2)
    # login whitelist toggle: 4th select (wl type / allow signup / login toggle / provider)
    check('wl-login-toggle-on', page.locator('.f-row select').nth(2).input_value() == '1')
    page.screenshot(path=f'{SHOTS}/b17-admin-wl-light.png')

    # dark theme screenshots (login first — token cleared by reload)
    page.evaluate("localStorage.setItem('grapeThemeMode','dark')")
    page.evaluate("localStorage.removeItem('grape_admin_token')")
    page.goto(BASE + '/admin')
    page.wait_for_timeout(1000)
    page.fill('.admv-login .ipt >> nth=0', 'admin')
    page.fill('.admv-login .ipt >> nth=1', 'adminpass')
    page.click('.lbtn')
    page.wait_for_timeout(1200)
    page.screenshot(path=f'{SHOTS}/b17-admin-cfg-dark.png')

    page.close()

fails = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
