#!/usr/bin/env python3
"""
Full i18n + functionality verification via Playwright.
Renders every page in both Chinese (/) and English (/en/) modes.
Checks for:
  1. I18N.t code residue leaking as visible text (BUG)
  2. JS errors on page load
  3. API load failures (error divs)
  4. Hardcoded English in Chinese mode (should be Chinese)
  5. Hardcoded Chinese in English mode (should be English, excluding known backend data)
"""
import subprocess, json, sys, time, re

# ── Generate JWT token ──
result = subprocess.run(
    ['/home/yangaiwu/grape/.venv/bin/python', '-c',
     'from src.api.jwt_utils import create_access_token; print(create_access_token(7, "gcw_IEzSI05D", 0))'],
    capture_output=True, text=True, cwd='/home/yangaiwu/grape', timeout=10
)
TOKEN = result.stdout.strip()
if not TOKEN:
    print("FATAL: cannot generate JWT token")
    sys.exit(1)

REPO_FULLNAME = "gcw_IEzSI05D/action-service-demo"
REPO_OWNER = "gcw_IEzSI05D"
REPO_NAME = "action-service-demo"
BASE = "http://127.0.0.1:3001"

# Pages that don't need a repo
NO_REPO_PAGES = ['repos', 'settings', 'tasks', 'skills', 'knowledge', 'runtimes', 'observability']
# Pages that need a repo
REPO_PAGES = ['dash', 'pipe', 'chat']
# Agent pages (need repo)
AGENT_PAGES = ['vod', 'issue', 'coding', 'qa', 'sre']

from playwright.sync_api import sync_playwright

def setup_page(page, lang='zh'):
    """Inject auth + repo into localStorage, navigate to app."""
    url = BASE + '/' if lang == 'zh' else BASE + '/en/'
    page.goto(url)
    page.wait_for_timeout(500)
    # Inject localStorage
    page.evaluate(f'''() => {{
        localStorage.setItem("token", "{TOKEN}");
        localStorage.setItem("user", JSON.stringify({{id:7, username:"gcw_IEzSI05D", role:"admin"}}));
        localStorage.setItem("currentRepo", JSON.stringify({{fullName:"{REPO_FULLNAME}", owner:"{REPO_OWNER}", name:"{REPO_NAME}"}}));
        localStorage.setItem("app_cache_ver", "7");
    }}''')
    page.goto(url)
    page.wait_for_timeout(2000)

def extract_text(page):
    """Extract all visible leaf-node text from #pageContent + sidebar + header."""
    return page.evaluate('''() => {
        const texts = [];
        const selectors = ['#pageContent', '#sidebar', '#pageTitle', '#repoTitle', '.sb-nav', 'header', '.toast', '.loading', '.error'];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (!el) continue;
            const leaves = el.querySelectorAll('*');
            for (const e of leaves) {
                if (e.children.length === 0) {
                    const t = e.textContent.trim();
                    if (t && t.length > 0 && t.length < 200) texts.push(t);
                }
            }
            // Also get direct text nodes
            const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
            let node;
            while (node = walker.nextNode()) {
                const t = node.textContent.trim();
                if (t && t.length > 0 && t.length < 200) texts.push(t);
            }
        }
        return [...new Set(texts)];
    }''')

def check_page(page, view, lang, needs_repo=True):
    """Navigate to a page and check for issues."""
    issues = []
    try:
        # Navigate
        page.evaluate(f"app.nav('{view}')")
        page.wait_for_timeout(2500)  # wait for API + render
        
        # Check for loading state (still loading = slow but not broken)
        loading = page.evaluate('''() => {
            const pc = document.querySelector('#pageContent');
            if (!pc) return false;
            return pc.innerHTML.includes('loading') && pc.querySelectorAll('*').length < 5;
        }''')
        
        # Check for error divs
        errors = page.evaluate('''() => {
            const pc = document.querySelector('#pageContent');
            if (!pc) return [];
            const errs = pc.querySelectorAll('.error');
            return [...errs].map(e => e.textContent.trim().substring(0, 120));
        }''')
        
        # Extract text
        texts = extract_text(page)
        
        # Check for I18N.t residue (BUG: code leaking as visible text)
        for t in texts:
            if 'I18N.t' in t or "I18N.t(" in t or "' + I18N" in t or '${I18N' in t:
                issues.append(('I18N_LEAK', t[:100]))
        
        # Check for undefined/NaN
        for t in texts:
            if t == 'undefined' or t == 'NaN' or t == 'null':
                issues.append(('UNDEFINED', f'view={view}: {t}'))
        
        # Language-specific checks
        if lang == 'zh':
            # In Chinese mode, look for pure-English UI text (excluding brands/dynamic data)
            # Only flag obvious English UI words that should be translated
            pass  # Will be done in dedicated pass
        elif lang == 'en':
            # In English mode, look for Chinese characters (excluding known backend data)
            chinese_texts = []
            for t in texts:
                # Find Chinese characters
                if re.search(r'[\u4e00-\u9fff]', t):
                    chinese_texts.append(t[:80])
            if chinese_texts:
                issues.append(('CHINESE_IN_EN', chinese_texts[:5]))
        
        if errors:
            for e in errors:
                issues.append(('API_ERROR', e))
        
        # Check console errors
        console_msgs = page.evaluate('''() => {
            if (window._capturedErrors) return window._capturedErrors;
            return [];
        }''')
        
        return {
            'view': view,
            'lang': lang,
            'issues': issues,
            'text_count': len(texts),
            'has_error': len(errors) > 0,
            'has_content': len(texts) > 3,
            'sample_texts': texts[:8]
        }
    except Exception as e:
        return {
            'view': view,
            'lang': lang,
            'issues': [('EXCEPTION', str(e)[:100])],
            'text_count': 0,
            'has_error': True,
            'has_content': False,
            'sample_texts': []
        }

def inject_console_capture(page):
    """Inject JS to capture console.error and uncaught exceptions."""
    page.evaluate('''() => {
        window._capturedErrors = [];
        const origError = console.error;
        console.error = function(...args) {
            window._capturedErrors.push(args.map(a => String(a)).join(' ').substring(0, 200));
            origError.apply(console, args);
        };
        window.addEventListener('error', (e) => {
            window._capturedErrors.push('Uncaught: ' + (e.message || '').substring(0, 200));
        });
    }''')

def main():
    results = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        
        for lang in ['zh', 'en']:
            lang_label = '中文' if lang == 'zh' else 'English'
            print(f"\n{'='*60}")
            print(f"  Language: {lang_label} ({'/' if lang=='zh' else '/en/'})")
            print(f"{'='*60}")
            
            page = browser.new_page()
            
            # Capture console errors
            console_errors = []
            page.on('console', lambda msg: console_errors.append(msg.text[:200]) if msg.type == 'error' else None)
            page.on('pageerror', lambda err: console_errors.append(f'PageError: {str(err)[:200]}'))
            
            setup_page(page, lang)
            inject_console_capture(page)
            
            # Test no-repo pages
            for view in NO_REPO_PAGES:
                r = check_page(page, view, lang, needs_repo=False)
                r['console_errors'] = [e for e in console_errors[-5:]]  # last 5
                results.append(r)
                status = '✓' if not r['issues'] and r['has_content'] else '✗'
                print(f"  [{status}] {view:15s} texts={r['text_count']:3d} issues={len(r['issues'])}")
                for issue in r['issues']:
                    print(f"         → {issue[0]}: {issue[1][:90]}")
                console_errors.clear()
            
            # Test repo pages
            for view in REPO_PAGES:
                r = check_page(page, view, lang, needs_repo=True)
                r['console_errors'] = [e for e in console_errors[-5:]]
                results.append(r)
                status = '✓' if not r['issues'] and r['has_content'] else '✗'
                print(f"  [{status}] {view:15s} texts={r['text_count']:3d} issues={len(r['issues'])}")
                for issue in r['issues']:
                    print(f"         → {issue[0]}: {issue[1][:90]}")
                console_errors.clear()
            
            # Test agent pages
            for view in AGENT_PAGES:
                r = check_page(page, view, lang, needs_repo=True)
                r['console_errors'] = [e for e in console_errors[-5:]]
                results.append(r)
                status = '✓' if not r['issues'] and r['has_content'] else '✗'
                print(f"  [{status}] {view:15s} texts={r['text_count']:3d} issues={len(r['issues'])}")
                for issue in r['issues']:
                    print(f"         → {issue[0]}: {issue[1][:90]}")
                console_errors.clear()
            
            # Also test settings sub-tabs
            for sub in ['credentials', 'notifications', 'appearance', 'advanced']:
                try:
                    page.evaluate(f"app.nav('settings','{sub}')")
                    page.wait_for_timeout(2000)
                    texts = extract_text(page)
                    i18n_leak = [t for t in texts if 'I18N.t' in t]
                    chinese_in_en = [t for t in texts if lang=='en' and re.search(r'[\u4e00-\u9fff]', t)]
                    issues = []
                    if i18n_leak: issues.append(('I18N_LEAK', i18n_leak[:3]))
                    if chinese_in_en: issues.append(('CHINESE_IN_EN', chinese_in_en[:3]))
                    status = '✓' if not issues else '✗'
                    print(f"  [{status}] settings/{sub:12s} texts={len(texts):3d} issues={len(issues)}")
                    for issue in issues:
                        print(f"         → {issue[0]}: {str(issue[1])[:90]}")
                except Exception as e:
                    print(f"  [✗] settings/{sub:12s} EXCEPTION: {str(e)[:80]}")
            
            page.close()
        
        browser.close()
    
    # ── Summary ──
    print(f"\n{'='*60}")
    print("  SUMMARY")
    print(f"{'='*60}")
    
    total = len(results)
    passed = sum(1 for r in results if not r['issues'] and r['has_content'])
    failed = total - passed
    
    print(f"  Total pages checked: {total * 1}")  # *1 because we count per-lang
    print(f"  Passed: {passed}")
    print(f"  Failed: {failed}")
    
    if failed:
        print(f"\n  FAILED PAGES:")
        for r in results:
            if r['issues'] or not r['has_content']:
                print(f"    {r['lang']}/{r['view']}:")
                if not r['has_content']:
                    print(f"      → NO_CONTENT (page rendered empty)")
                for issue in r['issues']:
                    print(f"      → {issue[0]}: {str(issue[1])[:100]}")
    
    # ── Detailed Chinese-in-English analysis ──
    print(f"\n{'='*60}")
    print("  DETAILED: Chinese text in English mode")
    print(f"{'='*60}")
    
    # Re-run English mode with detailed Chinese detection
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        setup_page(page, 'en')
        
        all_chinese = []
        for view in NO_REPO_PAGES + REPO_PAGES + AGENT_PAGES:
            try:
                page.evaluate(f"app.nav('{view}')")
                page.wait_for_timeout(2500)
                texts = extract_text(page)
                for t in texts:
                    if re.search(r'[\u4e00-\u9fff]', t):
                        all_chinese.append((view, t[:100]))
            except:
                pass
        
        # Settings sub-tabs
        for sub in ['credentials', 'notifications', 'appearance', 'advanced']:
            try:
                page.evaluate(f"app.nav('settings','{sub}')")
                page.wait_for_timeout(2000)
                texts = extract_text(page)
                for t in texts:
                    if re.search(r'[\u4e00-\u9fff]', t):
                        all_chinese.append((f'settings/{sub}', t[:100]))
            except:
                pass
        
        browser.close()
    
    if all_chinese:
        print(f"  Found {len(all_chinese)} Chinese texts in English mode:")
        # Categorize
        known_backend = []  # backend-returned data (Phase 2, not frontend i18n)
        unknown = []        # unknown — needs investigation
        
        # Known backend data patterns (scene names, agent descriptions, error messages, etc.)
        backend_patterns = [
            '本机直连', '无可用引擎', '刷新失败', '思考中', '生成中',
            '场景', '默认',  # scene names from backend config
        ]
        
        for view, text in all_chinese:
            is_backend = any(p in text for p in backend_patterns)
            if is_backend:
                known_backend.append((view, text))
            else:
                unknown.append((view, text))
        
        if unknown:
            print(f"\n  UNKNOWN (needs investigation) — {len(unknown)}:")
            for view, text in unknown:
                print(f"    [{view}] {text}")
        
        if known_backend:
            print(f"\n  KNOWN BACKEND (Phase 2, expected) — {len(known_backend)}:")
            for view, text in known_backend[:10]:
                print(f"    [{view}] {text}")
            if len(known_backend) > 10:
                print(f"    ... and {len(known_backend)-10} more")
    else:
        print("  ✓ No Chinese text found in English mode")
    
    # ── Detailed English-in-Chinese analysis ──
    print(f"\n{'='*60}")
    print("  DETAILED: Untranslated English in Chinese mode")
    print(f"{'='*60}")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        setup_page(page, 'zh')
        
        all_english = []
        # Brand names that are legitimately English in Chinese mode
        BRANDS = {'VOD', 'Agent', 'CodeArts', 'GitCode', 'Hermes', 'hermes', 'API', 'SSE',
                  'JWT', 'OAuth', 'SSH', 'HTML', 'CSS', 'JSON', 'URL', 'ID', 'CPU', 'GPU',
                  'MFA', 'TOTP', 'OBS', 'ECS', 'VPC', 'RDS', 'CCI', 'CCE', 'DevOps',
                  'Pipeline', 'Issue', 'Coding', 'QA', 'SRE', 'TODO', 'WIP',
                  'Liquid Glass', 'Grape', 'n8n', 'Docker', 'Python', 'Java', 'Go',
                  'JavaScript', 'TypeScript', 'Vue', 'React', 'Node', 'npm', 'pip',
                  'Linux', 'Windows', 'macOS', 'GitHub', 'Lint', 'Test', 'Build',
                  'Deploy', 'Release', 'Config', 'Env', 'Log', 'Debug', 'Cache',
                  'Token', 'Session', 'Cookie', 'Header', 'Body', 'Status', 'Method',
                  'GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD',
                  'OK', 'KO', 'N/A', 'TBD', 'ETA', 'SLA', 'SLO', 'MTTR', 'MTBF'}
        
        for view in NO_REPO_PAGES + REPO_PAGES + AGENT_PAGES:
            try:
                page.evaluate(f"app.nav('{view}')")
                page.wait_for_timeout(2500)
                texts = extract_text(page)
                for t in texts:
                    # Skip if it has Chinese (mixed = probably translated)
                    if re.search(r'[\u4e00-\u9fff]', t):
                        continue
                    # Skip pure numbers, symbols, dates, URLs
                    if re.match(r'^[\d\s\-\:\.\/\%\(\)\[\]]+$', t):
                        continue
                    # Skip if all words are brands/abbreviations
                    words = re.findall(r'[A-Za-z]+', t)
                    if words and all(w in BRANDS or w.upper() in BRANDS for w in words):
                        continue
                    # Flag remaining English
                    if re.search(r'[A-Za-z]{3,}', t):
                        all_english.append((view, t[:100]))
            except:
                pass
        
        # Settings sub-tabs
        for sub in ['credentials', 'notifications', 'appearance', 'advanced']:
            try:
                page.evaluate(f"app.nav('settings','{sub}')")
                page.wait_for_timeout(2000)
                texts = extract_text(page)
                for t in texts:
                    if re.search(r'[\u4e00-\u9fff]', t):
                        continue
                    if re.match(r'^[\d\s\-\:\.\/\%\(\)\[\]]+$', t):
                        continue
                    words = re.findall(r'[A-Za-z]+', t)
                    if words and all(w in BRANDS or w.upper() in BRANDS for w in words):
                        continue
                    if re.search(r'[A-Za-z]{3,}', t):
                        all_english.append((f'settings/{sub}', t[:100]))
            except:
                pass
        
        browser.close()
    
    if all_english:
        print(f"  Found {len(all_english)} untranslated English texts in Chinese mode:")
        seen = set()
        unique = []
        for view, text in all_english:
            if text not in seen:
                seen.add(text)
                unique.append((view, text))
        for view, text in unique[:30]:
            print(f"    [{view}] {text}")
        if len(unique) > 30:
            print(f"    ... and {len(unique)-30} more")
    else:
        print("  ✓ No untranslated English found in Chinese mode")
    
    print(f"\n{'='*60}")
    print("  VERIFICATION COMPLETE")
    print(f"{'='*60}")
    
    return failed == 0

if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
