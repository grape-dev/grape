"""
R12: 对话全屏 z-index 遮挡 + 全屏态交互缺陷修复
用法: python3.9 tests/playwright/test_r12_fullscreen_zindex.py
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

    # 前置: 登录
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

    # 导航到事件页
    page.evaluate("window.location.hash = '#event/1'")
    page.wait_for_timeout(2000)

    # === 测试 1: z-index 正确性 ===
    print('\n=== 测试 1: z-index 正确性 ===')
    try:
        page.click('button:has-text("全屏")')
        page.wait_for_timeout(500)
    except:
        try:
            page.evaluate('toggleConvFullscreen()')
            page.wait_for_timeout(500)
        except:
            pass

    z = page.evaluate("""() => {
        const fs = document.getElementById('convFullscreen');
        return fs ? fs.style.zIndex : null;
    }""")
    test('对话全屏 z-index=1001', z == '1001', f'got {z}')

    # === 测试 2: 还原按钮可点击 ===
    print('\n=== 测试 2: 还原按钮可点击 ===')
    btn_result = page.evaluate("""() => {
        const fs = document.getElementById('convFullscreen');
        if (!fs) return {error: 'no fullscreen'};
        const bar = fs.querySelector('div');
        const btns = bar ? bar.querySelectorAll('button') : [];
        if (btns.length === 0) return {error: 'no buttons'};
        const restoreBtn = btns[btns.length - 1];
        const r = restoreBtn.getBoundingClientRect();
        const el = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
        return {
            btnText: restoreBtn.textContent.trim(),
            topEl: el ? (el.tagName + (el.id ? '#' + el.id : '')) : 'null',
            isBtn: el === restoreBtn
        };
    }""")
    test('还原按钮可点击', btn_result.get('isBtn', False), f"topEl={btn_result.get('topEl')}")

    # === 测试 3: 清理对话按钮可点击 ===
    print('\n=== 测试 3: 清理对话按钮可点击 ===')
    clear_result = page.evaluate("""() => {
        const fs = document.getElementById('convFullscreen');
        if (!fs) return {error: 'no fullscreen'};
        const bar = fs.querySelector('div');
        const btns = bar ? bar.querySelectorAll('button') : [];
        if (btns.length === 0) return {error: 'no buttons'};
        const clearBtn = btns[0];
        const r = clearBtn.getBoundingClientRect();
        const el = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
        return {
            btnText: clearBtn.textContent.trim(),
            topEl: el ? (el.tagName + (el.id ? '#' + el.id : '')) : 'null',
            isBtn: el === clearBtn
        };
    }""")
    test('清理对话按钮可点击', clear_result.get('isBtn', False), f"topEl={clear_result.get('topEl')}")

    # === 测试 4: 工具栏被遮罩盖住 ===
    print('\n=== 测试 4: 工具栏被遮罩盖住 ===')
    tb_result = page.evaluate("""() => {
        const tb = document.querySelector('.tb');
        const fs = document.getElementById('convFullscreen');
        if (!tb || !fs) return {error: 'no tb or fs'};
        const tbRect = tb.getBoundingClientRect();
        const elAtTb = document.elementFromPoint(tbRect.x + 100, tbRect.y + 26);
        return {
            tbZ: getComputedStyle(tb).zIndex,
            fsZ: fs.style.zIndex,
            elAtTb: elAtTb ? (elAtTb.tagName + (elAtTb.id ? '#' + elAtTb.id : '')) : 'null',
            tbCovered: elAtTb !== tb
        };
    }""")
    test('工具栏被全屏遮罩盖住', tb_result.get('tbCovered', False), f"elAtTb={tb_result.get('elAtTb')}")

    # === 测试 5: ESC 退出对话全屏 ===
    print('\n=== 测试 5: ESC 退出对话全屏 ===')
    page.keyboard.press('Escape')
    page.wait_for_timeout(500)
    fs_after = page.evaluate("document.getElementById('convFullscreen') !== null")
    test('ESC 退出对话全屏', not fs_after, 'fullscreen removed')

    # === 测试 6: 切事件清理全屏 ===
    print('\n=== 测试 6: 切事件清理全屏 ===')
    try:
        page.click('button:has-text("全屏")')
        page.wait_for_timeout(500)
    except:
        try:
            page.evaluate('toggleConvFullscreen()')
            page.wait_for_timeout(500)
        except:
            pass
    has_fs = page.evaluate("document.getElementById('convFullscreen') !== null")
    if has_fs:
        page.evaluate("window.location.hash = '#event/2'")
        page.wait_for_timeout(2000)
        fs_after_switch = page.evaluate("document.getElementById('convFullscreen') !== null")
        test('切事件后全屏已清理', not fs_after_switch, 'fullscreen removed on event switch')
        has_conv = page.evaluate("document.getElementById('convMessages') !== null")
        test('新事件对话区域存在', has_conv)
    else:
        test('切事件清理全屏', False, 'could not enter fullscreen')

    # === 测试 7: task log 全屏 ESC 退出 ===
    print('\n=== 测试 7: task log 全屏 ESC 退出 ===')
    page.evaluate("window.location.hash = '#event/1'")
    page.wait_for_timeout(2000)
    try:
        page.click('button:has-text("执行日志")')
        page.wait_for_timeout(1000)
        page.click('#taskLogFullscreenBtn')
        page.wait_for_timeout(500)
        is_fs = page.evaluate("""() => {
            const box = document.querySelector('#taskLogOverlay > div');
            return box && box.dataset.fs === '1';
        }""")
        if is_fs:
            page.keyboard.press('Escape')
            page.wait_for_timeout(500)
            still_fs = page.evaluate("""() => {
                const box = document.querySelector('#taskLogOverlay > div');
                return box && box.dataset.fs === '1';
            }""")
            test('ESC 退出 task log 全屏', not still_fs, 'task log fullscreen exited')
        else:
            test('ESC 退出 task log 全屏', False, 'could not enter task log fullscreen')
        page.keyboard.press('Escape')
        page.wait_for_timeout(500)
    except Exception as e:
        test('ESC 退出 task log 全屏', False, str(e))

    # === 测试 8: 接收上下文全屏 z-index ===
    print('\n=== 测试 8: 接收上下文全屏 z-index ===')
    try:
        page.click('#promptFsBtn')
        page.wait_for_timeout(500)
        pz = page.evaluate("""() => {
            const fs = document.getElementById('promptFullscreen');
            return fs ? fs.style.zIndex : null;
        }""")
        test('接收上下文全屏 z-index=1001', pz == '1001', f'got {pz}')
        page.keyboard.press('Escape')
        page.wait_for_timeout(500)
    except:
        test('接收上下文全屏 z-index=1001', False, 'could not open prompt fullscreen')

    # === 测试 9: cache busting ===
    print('\n=== 测试 9: cache busting ===')
    src = page.evaluate("""() => {
        const scripts = document.querySelectorAll('script[src*="events.js"]');
        return scripts.length > 0 ? scripts[0].src : null;
    }""")
    has_v58 = src and 'v=58' in src
    test('events.js cache version v=58', has_v58, f'src={src}')

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
