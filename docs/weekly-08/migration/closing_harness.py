#!/usr/bin/env python3
"""Closing batch harness: shell routing regression after removing typeof guards
and bumping all vue chunks to v7. Real login (vue-mig-test), mock repo binding,
drive nav() through previously-guarded routes, assert no JS errors."""
import json, sys
from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:3000'
results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(('PASS ' if cond else 'FAIL ') + name)

with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp('http://127.0.0.1:9222')
    ctx = b.contexts[0] if b.contexts else b.new_context()
    page = ctx.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)[:200]))
    # mock dashboard + knowledge APIs so the fake binding (test account: 0 real
    # bindings) doesn't 403 → R13.6 auto-kicks back to repos lobby (legacy
    # behavior, unrelated to this batch)
    def handle(route, req):
        url = req.url
        import json as _j
        if '/api/dashboard/' in url or (url.endswith('/api/dashboard') or '/dashboard?' in url):
            route.fulfill(status=200, content_type='application/json',
                          body=_j.dumps({"scenes": [], "role": "Maintainer", "_owner": "xocto", "_name": "demo"}))
        elif '/api/knowledge/stats' in url:
            route.fulfill(status=200, content_type='application/json',
                          body=_j.dumps({"total": 1, "agent": 0, "user": 0, "content_limit": 1048576}))
        elif '/api/knowledge?' in url or '/api/knowledge/' in url:
            route.fulfill(status=200, content_type='application/json',
                          body=_j.dumps({"items": [], "total": 0}))
        else:
            route.continue_()
    page.route('**/api/**', handle)
    page.set_viewport_size({'width': 1440, 'height': 900})

    # login via API to get token
    page.goto(BASE + '/')
    r = page.evaluate("""async () => {
      const r = await fetch('/api/auth/login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'vue-mig-test',password:'VueMig-Test-2026'})});
      const d = await r.json();
      if (d.token) { localStorage.setItem('token', d.token); localStorage.setItem('user', JSON.stringify({username:'vue-mig-test'})); return 'ok'; }
      return 'fail:' + JSON.stringify(d).slice(0,120);
    }""")
    check('login', r == 'ok')

    page.goto(BASE + '/')
    page.wait_for_timeout(2500)
    # seed fake binding + currentRepo (test account has 0 bindings)
    page.evaluate("""() => {
      sessionStorage.setItem('currentRepo', JSON.stringify({owner:'xocto',name:'demo',fullName:'xocto/demo',repoKey:'gitcode:xocto/demo'}));
    }""")
    page.wait_for_timeout(500)

    def nav(v):
        page.evaluate(f"app.nav('{v}')")
        page.wait_for_timeout(1600)

    # routes whose guards were removed / direct calls now
    for v in ['dash', 'runtimes', 'observability', 'autopilot', 'skills', 'scene-market']:
        nav(v)
        html = page.evaluate("document.getElementById('pageContent') ? document.getElementById('pageContent').innerHTML.length : -1")
        check(f'nav-{v}-renders', html > 200)

    # knowledge (app.js 3-guard handle) — nav away then back
    # NOTE: test account gets 403 on /api/knowledge → firstLoad may stay pending
    # (legacy-equivalent behavior); the CONTRACT under test is the app-handle
    # lifecycle, which works regardless of data.
    nav('knowledge')
    k1 = page.evaluate("!!window._knowledgeVueApp")
    nav('dash')
    k2 = page.evaluate("!!window._knowledgeVueApp")
    nav('knowledge')
    k3 = page.evaluate("!!window._knowledgeVueApp")
    check('knowledge-mount-cycle', k1 and (not k2) and k3)

    # organizations / my-feedback (guards removed)
    nav('organizations')
    check('nav-organizations', page.evaluate("document.getElementById('pageContent').innerHTML.length") > 100)
    nav('my-feedback')
    check('nav-my-feedback', page.evaluate("document.getElementById('pageContent') ? true : false"))

    # feedback dialog still opens from user menu contract
    # (dialog Teleports to <body> — assert overlay, not host innerHTML)
    page.evaluate("openFeedbackDialog()")
    page.wait_for_timeout(700)
    check('feedback-dialog-opens', page.evaluate("!!document.querySelector('#feedbackOverlay') && document.querySelector('#feedbackOverlay').getBoundingClientRect().width > 100"))

    # dual-theme screenshots on dashboard
    page.evaluate("localStorage.setItem('grapeThemeMode','auto')")
    page.evaluate("app.nav('dash')")
    page.wait_for_timeout(2000)
    page.emulate_media(color_scheme='light')
    page.wait_for_timeout(400)
    page.screenshot(path='/home/mys/grape/docs/weekly-08/screenshots/closing-dash-light.png')
    page.emulate_media(color_scheme='dark')
    page.wait_for_timeout(600)
    page.screenshot(path='/home/mys/grape/docs/weekly-08/screenshots/closing-dash-dark.png')
    page.evaluate("app.nav('runtimes')")
    page.wait_for_timeout(1800)
    page.screenshot(path='/home/mys/grape/docs/weekly-08/screenshots/closing-runtimes-dark.png')

    print('  JS errors:', errors[:5] if errors else 'none')
    check('no-page-errors', len(errors) == 0)
    page.close()

from PIL import Image
for f in ['closing-dash-light.png', 'closing-dash-dark.png', 'closing-runtimes-dark.png']:
    im = Image.open('/home/mys/grape/docs/weekly-08/screenshots/' + f).convert('L')
    vals = list(im.getdata())
    print(f'  {f}: mean={sum(vals)/len(vals):.1f}')

fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
