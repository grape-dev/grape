#!/usr/bin/env python3
"""Check DOM context of English text in repos page (Chinese mode)."""
import subprocess, json
from playwright.sync_api import sync_playwright

result = subprocess.run(
    ['/home/yangaiwu/grape/.venv/bin/python', '-c',
     'from src.api.jwt_utils import create_access_token; print(create_access_token(7, "gcw_IEzSI05D", 0))'],
    capture_output=True, text=True, cwd='/home/yangaiwu/grape', timeout=10
)
TOKEN = result.stdout.strip()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('http://127.0.0.1:3001/')
    page.wait_for_timeout(500)
    page.evaluate(f'''() => {{
        localStorage.setItem("token", "{TOKEN}");
        localStorage.setItem("user", JSON.stringify({{id:7, username:"gcw_IEzSI05D", role:"admin"}}));
        localStorage.setItem("currentRepo", JSON.stringify({{fullName:"gcw_IEzSI05D/action-service-demo", owner:"gcw_IEzSI05D", name:"action-service-demo"}}));
        localStorage.setItem("app_cache_ver", "7");
    }}''')
    page.goto('http://127.0.0.1:3001/')
    page.wait_for_timeout(2000)
    page.evaluate("app.nav('repos')")
    page.wait_for_timeout(3000)
    
    # 找包含 Skill/Engine/Events/Failed 的元素及其上下文
    elements = page.evaluate('''() => {
        const results = [];
        const targets = ['Skill', 'Engine', 'Events', 'Failed', 'Maintainer', 'Active', 'Joined'];
        for (const target of targets) {
            const all = document.querySelectorAll('#pageContent *');
            for (const el of all) {
                if (el.children.length === 0 && el.textContent.trim() === target) {
                    const parent = el.parentElement;
                    results.push({
                        text: target,
                        tag: el.tagName,
                        class: el.className,
                        parent_tag: parent ? parent.tagName : '',
                        parent_class: parent ? parent.className : '',
                        parent_html: parent ? parent.innerHTML.substring(0, 300) : '',
                    });
                }
            }
        }
        return results;
    }''')
    
    print(json.dumps(elements, ensure_ascii=False, indent=2))
    
    # Also check skills page "Agent Skills"
    page.evaluate("app.nav('skills')")
    page.wait_for_timeout(2500)
    skills_els = page.evaluate('''() => {
        const results = [];
        const all = document.querySelectorAll('#pageContent *');
        for (const el of all) {
            if (el.children.length === 0) {
                const t = el.textContent.trim();
                if (t.includes('Agent Skills') || t.includes('Skills')) {
                    results.push({text: t.substring(0, 80), tag: el.tagName, class: el.className});
                }
            }
        }
        return results;
    }''')
    print("\n=== skills page Agent Skills ===")
    print(json.dumps(skills_els, ensure_ascii=False, indent=2))
    
    browser.close()
