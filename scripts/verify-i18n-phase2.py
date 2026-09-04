#!/usr/bin/env python3
"""Phase 2: Check untranslated English in Chinese mode + I18N.t residue in both langs."""
import subprocess, json, sys, time, re
from playwright.sync_api import sync_playwright

result = subprocess.run(
    ['/home/yangaiwu/grape/.venv/bin/python', '-c',
     'from src.api.jwt_utils import create_access_token; print(create_access_token(7, "gcw_IEzSI05D", 0))'],
    capture_output=True, text=True, cwd='/home/yangaiwu/grape', timeout=10
)
TOKEN = result.stdout.strip()
BASE = "http://127.0.0.1:3001"
REPO_FN = "gcw_IEzSI05D/action-service-demo"

NO_REPO_PAGES = ['repos', 'settings', 'tasks', 'skills', 'knowledge', 'runtimes', 'observability']
REPO_PAGES = ['dash', 'pipe', 'chat']
AGENT_PAGES = ['vod', 'issue', 'coding', 'qa', 'sre']

# Known brands/abbreviations — legitimately English in Chinese mode
BRANDS = {'VOD', 'Agent', 'Agents', 'CodeArts', 'GitCode', 'Hermes', 'hermes', 'API', 'SSE',
          'JWT', 'OAuth', 'OAuth2', 'SSH', 'HTML', 'CSS', 'JSON', 'URL', 'ID', 'CPU', 'GPU',
          'MFA', 'TOTP', 'OBS', 'ECS', 'VPC', 'RDS', 'CCI', 'CCE', 'DevOps', 'CCE',
          'Pipeline', 'Issue', 'Issues', 'Coding', 'QA', 'SRE', 'TODO', 'WIP', 'PR',
          'Liquid Glass', 'Grape', 'n8n', 'Docker', 'Python', 'Java', 'Go', 'Node',
          'JavaScript', 'TypeScript', 'Vue', 'React', 'npm', 'pip', 'Linux', 'Windows',
          'macOS', 'GitHub', 'Token', 'Session', 'Cookie', 'Header', 'Body', 'Status',
          'GET', 'POST', 'PUT', 'DELETE', 'OK', 'N/A', 'TBD', 'SLA', 'SLO',
          'ClawHub', 'ModelArts', 'Terraform', 'ansible', 'FFmpeg', 'Edge', 'TTS',
          'Wan', 'qwen', 'A14B', 'T2V', 'MP4', 'GIF', 'SVG', 'JPEG', 'PNG',
          'vim', 'git', 'curl', 'bash', 'shell', 'ssh', 'scp', 'rsync',
          'CI', 'CD', 'opencode', 'OpenCode', 'Codex', 'Claude'}

def setup_page(page, lang='zh'):
    url = BASE + '/' if lang == 'zh' else BASE + '/en/'
    page.goto(url)
    page.wait_for_timeout(500)
    page.evaluate(f'''() => {{
        localStorage.setItem("token", "{TOKEN}");
        localStorage.setItem("user", JSON.stringify({{id:7, username:"gcw_IEzSI05D", role:"admin"}}));
        localStorage.setItem("currentRepo", JSON.stringify({{fullName:"{REPO_FN}", owner:"gcw_IEzSI05D", name:"action-service-demo"}}));
        localStorage.setItem("app_cache_ver", "7");
    }}''')
    page.goto(url)
    page.wait_for_timeout(2000)

def extract_text(page):
    return page.evaluate('''() => {
        const texts = [];
        const selectors = ['#pageContent', '#sidebar', '#pageTitle', '#repoTitle', '.sb-nav', 'header', '.toast'];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (!el) continue;
            const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
            let node;
            while (node = walker.nextNode()) {
                const t = node.textContent.trim();
                if (t && t.length > 0 && t.length < 200) texts.push(t);
            }
        }
        return [...new Set(texts)];
    }''')

def check_i18n_residue(page, texts):
    """Check for I18N.t() code leaking as visible text."""
    leaks = []
    for t in texts:
        if 'I18N.t' in t or "' + I18N" in t or '${I18N' in t or 'I18N.t(' in t:
            leaks.append(t[:100])
    return leaks

def check_english_in_zh(texts):
    """Find untranslated English UI text in Chinese mode."""
    issues = []
    for t in texts:
        # Skip if contains Chinese (already translated)
        if re.search(r'[\u4e00-\u9fff]', t):
            continue
        # Skip pure numbers/symbols/dates
        if re.match(r'^[\d\s\-\:\.\/\%\(\)\[\]\,\+\*\=\#\@\!\?\;\|]+$', t):
            continue
        # Skip if all words are brands
        words = re.findall(r'[A-Za-z]+', t)
        if words and all(w in BRANDS or w.upper() in BRANDS for w in words):
            continue
        # Flag English text with 3+ letter words
        if re.search(r'[A-Za-z]{3,}', t):
            issues.append(t[:100])
    return issues

def main():
    all_pages = NO_REPO_PAGES + REPO_PAGES + AGENT_PAGES
    
    print("="*60)
    print("  CHECK 1: I18N.t() code residue (both languages)")
    print("="*60)
    
    total_leaks = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for lang in ['zh', 'en']:
            page = browser.new_page()
            setup_page(page, lang)
            for view in all_pages:
                try:
                    page.evaluate(f"app.nav('{view}')")
                    page.wait_for_timeout(2500)
                    texts = extract_text(page)
                    leaks = check_i18n_residue(page, texts)
                    if leaks:
                        total_leaks += len(leaks)
                        for leak in leaks:
                            print(f"  [BUG] {lang}/{view}: {leak}")
                except Exception as e:
                    print(f"  [ERR] {lang}/{view}: {str(e)[:60]}")
            
            # Settings sub-tabs
            for sub in ['credentials', 'notifications', 'appearance', 'advanced']:
                try:
                    page.evaluate(f"app.nav('settings','{sub}')")
                    page.wait_for_timeout(2000)
                    texts = extract_text(page)
                    leaks = check_i18n_residue(page, texts)
                    if leaks:
                        total_leaks += len(leaks)
                        for leak in leaks:
                            print(f"  [BUG] {lang}/settings/{sub}: {leak}")
                except:
                    pass
            page.close()
        browser.close()
    
    if total_leaks == 0:
        print("  ✓ No I18N.t() code residue found in any page (both languages)")
    else:
        print(f"\n  ✗ Found {total_leaks} I18N.t() code leaks!")
    
    print(f"\n{'='*60}")
    print("  CHECK 2: Untranslated English in Chinese mode")
    print("="*60)
    
    all_issues = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        setup_page(page, 'zh')
        
        for view in all_pages:
            try:
                page.evaluate(f"app.nav('{view}')")
                page.wait_for_timeout(2500)
                texts = extract_text(page)
                issues = check_english_in_zh(texts)
                if issues:
                    for t in issues:
                        all_issues.append((view, t))
            except:
                pass
        
        for sub in ['credentials', 'notifications', 'appearance', 'advanced']:
            try:
                page.evaluate(f"app.nav('settings','{sub}')")
                page.wait_for_timeout(2000)
                texts = extract_text(page)
                issues = check_english_in_zh(texts)
                if issues:
                    for t in issues:
                        all_issues.append((f'settings/{sub}', t))
            except:
                pass
        
        browser.close()
    
    if all_issues:
        # Deduplicate
        seen = set()
        unique = []
        for view, text in all_issues:
            key = text.lower()
            if key not in seen:
                seen.add(key)
                unique.append((view, text))
        
        print(f"  Found {len(unique)} unique untranslated English texts in Chinese mode:")
        for view, text in unique[:40]:
            print(f"    [{view}] {text}")
        if len(unique) > 40:
            print(f"    ... and {len(unique)-40} more")
    else:
        print("  ✓ No untranslated English found in Chinese mode")
    
    print(f"\n{'='*60}")
    print("  CHECK 3: JS syntax validation (node --check)")
    print("="*60)
    
    js_files = subprocess.run(
        ['find', 'src/api/static/js', '-name', '*.js', '-not', '-path', '*/vendor/*'],
        capture_output=True, text=True, cwd='/home/yangaiwu/grape'
    ).stdout.strip().split('\n')
    
    syntax_errors = 0
    for f in js_files:
        if not f: continue
        r = subprocess.run(['node', '--check', f], capture_output=True, text=True, cwd='/home/yangaiwu/grape')
        if r.returncode != 0:
            syntax_errors += 1
            print(f"  [SYNTAX ERROR] {f}: {r.stderr[:100]}")
    
    if syntax_errors == 0:
        print(f"  ✓ All {len(js_files)} JS files pass syntax check")
    else:
        print(f"\n  ✗ {syntax_errors} files have syntax errors!")
    
    print(f"\n{'='*60}")
    print("  CHECK 4: i18n key cross-reference (JS vs JSON packs)")
    print("="*60)
    
    # Extract all I18N.t('module.key') calls from JS and verify they exist in zh+en JSON
    js_dir = 'src/api/static/js/pages'
    locale_dir = 'src/api/static/locales'
    
    # Collect all keys from JS
    all_keys = set()
    for f in js_files:
        if '/pages/' not in f: continue
        content = subprocess.run(['cat', f], capture_output=True, text=True, cwd='/home/yangaiwu/grape').stdout
        # Match I18N.t('...') and I18N.t("...")
        keys = re.findall(r"I18N\.t\(['\"]([^'\"]+)['\"]", content)
        all_keys.update(keys)
    
    # Check each key against zh and en packs
    def get_keys(lang):
        keys = set()
        import os
        for mod_file in os.listdir(f'{locale_dir}/{lang}'):
            if mod_file.endswith('.json'):
                with open(f'{locale_dir}/{lang}/{mod_file}') as fp:
                    data = json.load(fp)
                def flatten(d, prefix=''):
                    for k, v in d.items():
                        full = f'{prefix}.{k}' if prefix else k
                        if isinstance(v, dict):
                            flatten(v, full)
                        else:
                            keys.add(full)
                    return keys
                flatten(data)
        return keys
    
    zh_keys = get_keys('zh')
    en_keys = get_keys('en')
    
    missing_zh = all_keys - zh_keys
    missing_en = all_keys - en_keys
    
    if missing_zh:
        print(f"  Missing in zh packs ({len(missing_zh)}):")
        for k in sorted(missing_zh)[:20]:
            print(f"    {k}")
    else:
        print("  ✓ All JS I18N.t() keys exist in zh language packs")
    
    if missing_en:
        print(f"  Missing in en packs ({len(missing_en)}):")
        for k in sorted(missing_en)[:20]:
            print(f"    {k}")
    else:
        print("  ✓ All JS I18N.t() keys exist in en language packs")
    
    print(f"\n{'='*60}")
    print("  FINAL SUMMARY")
    print("="*60)
    print(f"  I18N.t code residue:  {total_leaks}")
    print(f"  Untranslated EN in ZH: {len(unique) if all_issues else 0}")
    print(f"  JS syntax errors:     {syntax_errors}")
    print(f"  Missing zh keys:      {len(missing_zh)}")
    print(f"  Missing en keys:      {len(missing_en)}")
    
    ok = total_leaks == 0 and syntax_errors == 0 and len(missing_zh) == 0 and len(missing_en) == 0
    print(f"\n  Overall: {'✓ ALL CHECKS PASS' if ok else '✗ ISSUES FOUND'}")
    return ok

if __name__ == '__main__':
    sys.exit(0 if main() else 1)
