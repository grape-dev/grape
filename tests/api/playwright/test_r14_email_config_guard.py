"""
R14: 账号安全流程邮箱验证码可用性判断修复
用法: python3.9 tests/playwright/test_r14_email_config_guard.py
前置: 服务运行在 localhost:3000，未配置邮件服务，已登录（admin/admin）
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

    # 确认邮件服务未配置
    email_cfg = page.evaluate("() => window._emailConfigured")
    print(f'\nemail_configured: {email_cfg}')

    # 导航到账号页
    page.evaluate("window.location.hash = '#account'")
    page.wait_for_timeout(2000)
    try:
        page.click('.stabs a:nth-child(3)')
        page.wait_for_timeout(1000)
    except:
        pass

    # === 测试 1: 修改密码弹层不显示"忘记密码"链接 ===
    print('\n=== 测试 1: 修改密码弹层不显示"忘记密码"链接 ===')
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
    has_forgot_link = page.evaluate("""() => {
        const modal = document.getElementById('accountModal');
        if (!modal) return false;
        const links = modal.querySelectorAll('a');
        for (const a of links) {
            if (a.textContent.includes('忘记') || a.getAttribute('onclick')?.includes('ResetPassword')) return true;
        }
        return false;
    }""")
    test('修改密码弹层无"忘记密码"链接', not has_forgot_link, f'link found: {has_forgot_link}')
    page.keyboard.press('Escape')
    page.wait_for_timeout(500)

    # === 测试 2: _accountShowResetPassword 守卫 ===
    print('\n=== 测试 2: _accountShowResetPassword 守卫 ===')
    page.evaluate('_accountShowResetPassword()')
    page.wait_for_timeout(500)
    has_modal = page.evaluate("document.getElementById('accountModal') !== null")
    test('_accountShowResetPassword 不渲染流程', not has_modal, f'modal shown: {has_modal}')

    # === 测试 3: _accountShowSetPassword 守卫 ===
    print('\n=== 测试 3: _accountShowSetPassword 守卫 ===')
    page.evaluate('_accountShowSetPassword()')
    page.wait_for_timeout(500)
    has_modal = page.evaluate("document.getElementById('accountModal') !== null")
    test('_accountShowSetPassword 不渲染流程', not has_modal, f'modal shown: {has_modal}')

    # === 测试 4: _accountShowChangeEmail 守卫 ===
    print('\n=== 测试 4: _accountShowChangeEmail 守卫 ===')
    page.evaluate('_accountShowChangeEmail()')
    page.wait_for_timeout(500)
    has_modal = page.evaluate("document.getElementById('accountModal') !== null")
    test('_accountShowChangeEmail 不渲染弹层', not has_modal, f'modal shown: {has_modal}')

    # === 测试 5: 删除账号无验证码选项 ===
    print('\n=== 测试 5: 删除账号无验证码选项 ===')
    page.evaluate('_accountShowDeleteAccount()')
    page.wait_for_timeout(500)
    has_code_option = page.evaluate("""() => {
        const modal = document.getElementById('accountModal');
        if (!modal) return false;
        const btns = modal.querySelectorAll('button');
        for (const b of btns) {
            if (b.textContent.includes('验证码') || b.getAttribute('onclick')?.includes('daChooseCode')) return true;
        }
        return false;
    }""")
    test('删除账号无验证码选项', not has_code_option, f'code option found: {has_code_option}')
    page.keyboard.press('Escape')
    page.wait_for_timeout(500)

    # === 测试 6: cache busting ===
    print('\n=== 测试 6: cache busting ===')
    src = page.evaluate("""() => {
        const scripts = document.querySelectorAll('script[src*="account.js"]');
        return scripts.length > 0 ? scripts[0].src : null;
    }""")
    has_v42 = src and 'v=42' in src
    test('account.js cache version v=42', has_v42, f'src={src}')

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
