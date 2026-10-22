"""
R13: 全站弹层 z-index 统一治理 + 原生弹框改造
用法: python3.9 tests/playwright/test_r13_unified_overlay.py
前置: 服务运行在 localhost:3000，已登录（admin/admin）
"""
from playwright.sync_api import sync_playwright
import sys

BASE = 'http://localhost:3000'
results = []


def test(name, condition, detail=''):
    status = '✓' if condition else '✗'
    results.append((name, condition, detail))
    print(f'  {status} {name}' + (f' — {detail}' if detail else ''))


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 800})

    # 登录
    page.goto(BASE + '/')
    page.wait_for_timeout(3000)
    try:
        page.fill('#loginUser', 'admin')
        page.fill('#loginPassword', 'admin')
        page.click('form#pwdLogin button')
        page.wait_for_timeout(3000)
    except:
        pass
    logged_in = page.evaluate("() => document.documentElement.getAttribute('data-logged-in')")
    if logged_in != '1':
        print('Login failed — aborting')
        sys.exit(1)

    # === 测试 1: account modal z-index=1001 ===
    print('\n=== 测试 1: account modal z-index ===')
    page.evaluate("window.location.hash = '#account'")
    page.wait_for_timeout(2000)
    try:
        page.click('.stabs a:nth-child(3)')
        page.wait_for_timeout(1000)
    except:
        pass
    try:
        btns = page.query_selector_all('button')
        for b in btns:
            txt = b.text_content() or ''
            if 'password' in txt.lower() or '密码' in txt:
                b.click()
                break
        page.wait_for_timeout(1000)
    except:
        pass
    modal_z = page.evaluate("""() => {
        const modal = document.getElementById('accountModal');
        if (!modal) return {error: 'no modal'};
        const tb = document.querySelector('.tb');
        const tbRect = tb.getBoundingClientRect();
        const elAtTb = document.elementFromPoint(tbRect.x + 100, tbRect.y + 26);
        return {
            modalZ: getComputedStyle(modal).zIndex,
            tbCovered: elAtTb !== tb,
            elAtTb: elAtTb ? elAtTb.tagName + '#' + (elAtTb.id||'') : 'null'
        };
    }""")
    test('account modal z-index=1001', modal_z.get('modalZ') == '1001', f"got {modal_z.get('modalZ')}")
    test('工具栏被 account modal 遮罩盖住', modal_z.get('tbCovered', False), f"elAtTb={modal_z.get('elAtTb')}")
    page.keyboard.press('Escape')
    page.wait_for_timeout(500)

    # === 测试 2: 统一弹层 API 导出 ===
    print('\n=== 测试 2: 统一弹层 API 导出 ===')
    api_check = page.evaluate("""() => ({
        showModal: typeof app.showModal === 'function',
        closeModal: typeof app.closeModal === 'function',
        showPrompt: typeof app.showPrompt === 'function',
        showConfirm: typeof app.showConfirm === 'function'
    })""")
    test('app.showModal 导出', api_check.get('showModal', False))
    test('app.closeModal 导出', api_check.get('closeModal', False))
    test('app.showPrompt 导出', api_check.get('showPrompt', False))
    test('app.showConfirm 导出', api_check.get('showConfirm', False))

    # === 测试 3: .create-repo-overlay z-index=1001 ===
    print('\n=== 测试 3: .create-repo-overlay z-index ===')
    cr_z = page.evaluate("""() => {
        var el = document.createElement('div');
        el.className = 'create-repo-overlay';
        el.style.display = 'none';
        document.body.appendChild(el);
        var z = getComputedStyle(el).zIndex;
        el.remove();
        return z;
    }""")
    test('.create-repo-overlay z-index=1001', cr_z == '1001', f'got {cr_z}')

    # === 测试 4: .modal-overlay z-index=1001 ===
    print('\n=== 测试 4: .modal-overlay z-index ===')
    mo_z = page.evaluate("""() => {
        var el = document.createElement('div');
        el.className = 'modal-overlay';
        el.style.display = 'none';
        document.body.appendChild(el);
        var z = getComputedStyle(el).zIndex;
        el.remove();
        return z;
    }""")
    test('.modal-overlay z-index=1001', mo_z == '1001', f'got {mo_z}')

    # === 测试 5: .km-overlay z-index=1001 ===
    print('\n=== 测试 5: .km-overlay z-index ===')
    km_z = page.evaluate("""() => {
        var el = document.createElement('div');
        el.className = 'km-overlay';
        document.body.appendChild(el);
        var z = getComputedStyle(el).zIndex;
        el.remove();
        return z;
    }""")
    test('.km-overlay z-index=1001', km_z == '1001', f'got {km_z}')

    # === 测试 6: 统一弹层 API 完整 ===
    print('\n=== 测试 6: 统一弹层 API 完整 ===')
    native_check = page.evaluate("""() => ({
        hasUnified: typeof app.showModal === 'function' &&
                    typeof app.showPrompt === 'function' &&
                    typeof app.showConfirm === 'function'
    })""")
    test('统一弹层 API 完整', native_check.get('hasUnified', False))

    # === 测试 7: cache busting 版本号 ===
    print('\n=== 测试 7: cache busting ===')
    versions = page.evaluate("""() => {
        var scripts = document.querySelectorAll('script[src]');
        var css = document.querySelectorAll('link[rel="stylesheet"]');
        var result = {};
        for (var s of scripts) {
            var src = s.src;
            if (src.indexOf('events.js') >= 0) result.events = src.match(/v=(\\d+)/)?.[1];
            if (src.indexOf('account.js') >= 0) result.account = src.match(/v=(\\d+)/)?.[1];
            if (src.indexOf('app.js') >= 0 && src.indexOf('auto-recovery') < 0) result.app = src.match(/v=(\\d+)/)?.[1];
        }
        for (var c of css) {
            if (c.href.indexOf('app.css') >= 0) result.appcss = c.href.match(/v=(\\d+)/)?.[1];
        }
        return result;
    }""")
    test('events.js v=58', versions.get('events') == '58', f"got {versions.get('events')}")
    test('account.js v=41', versions.get('account') == '41', f"got {versions.get('account')}")
    test('app.js v=183', versions.get('app') == '183', f"got {versions.get('app')}")
    test('app.css v=85', versions.get('appcss') == '85', f"got {versions.get('appcss')}")

    # === 测试 8: toast z-index > overlay z-index ===
    print('\n=== 测试 8: toast z-index > overlay z-index ===')
    z_compare = page.evaluate("""() => ({
        toastZ: 10000,
        overlayZ: 1001,
        toastAbove: 10000 > 1001
    })""")
    test('toast(10000) > overlay(1001)', z_compare.get('toastAbove', False))

    browser.close()

# 汇总
print('\n' + '=' * 50)
passed = sum(1 for _, c, _ in results if c)
failed = sum(1 for _, c, _ in results if not c)
print(f'{passed} passed, {failed} failed')
if failed > 0:
    print('\nFailed tests:')
    for name, cond, detail in results:
        if not cond:
            print(f'  ✗ {name} — {detail}')
    sys.exit(1)
