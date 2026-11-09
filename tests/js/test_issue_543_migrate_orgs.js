/**
 * Issue #543 回归测试：迁移目标组织下拉框（PR #747 审查修复验证）
 *
 * 运行：node tests/js/test_issue_543_migrate_orgs.js
 *
 * 背景（PR #747 审查 Critical + Required）：
 *   - Critical：_loadMigrateOrgs 中 o.login/o.name/personal 未转义直接拼 option 的
 *     value/label，组织名含 HTML 字符时可注入任意 JS（存储型 XSS，无 CSP 兜底）。
 *     修复：新增 _migrateEsc()（与 app.js:1229 _esc / issues.js:177 _escapeHtml 同口径），
 *     login/name/personal 全部转义后再拼接。
 *   - Required（工作流）：前端 0 测试，下拉构建/排除当前 owner/失败降级路径无 js 覆盖。
 *
 * 验证点（vm 沙箱加载真实 settings.js，mock /repos/migrate-orgs 响应）：
 *   1. XSS 回归：login/name/personal 含 HTML 特殊字符（<、"、&、'>）时全部被转义，
 *      注入载荷（\"><img src=x onerror=...>）原样不落 DOM
 *   2. _migrateEsc 单元行为：五类特殊字符 & < > " ' 全转义，空值/非字符串安全
 *   3. 正常渲染：personal + orgs → 占位符 + 个人选项（带「个人」标注）+ 组织选项
 *   4. 排除当前 owner：orgs 含当前仓库 owner 的项不出现；personal === 当前 owner 也不出现
 *   5. label 拼装：name 与 login 不同 → "login (name)"；相同/缺失 → 仅 login
 *   6. 失败降级：API reject → 下拉变「加载失败」占位 + showToast warning
 *   7. 空列表降级：orgs 空 && personal 空 →「无可迁移的组织」占位
 */

const assert = require('assert');
const constfs = require('fs');
const fs = constfs;
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const SETTINGS_JS = path.join(ROOT, 'src/api/static/js/pages/settings.js');

// ── 极简 DOM mock（只覆盖 _loadMigrateOrgs / _migrateEsc 依赖面）──
function makeSandbox(opts) {
  opts = opts || {};
  const elements = {
    migrateTargetOwner: {
      _id: 'migrateTargetOwner', _html: '',
      set innerHTML(v) { this._html = String(v); },
      get innerHTML() { return this._html; },
      value: '',
    },
    migrateTargetRepo: { value: 'demo-repo', _html: '' },
  };
  const document = {
    getElementById(id) { return elements[id] || null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    createElement() { return { style: {}, classList: { add() {}, remove() {} }, textContent: '', innerHTML: '', appendChild() {}, remove() {}, addEventListener() {}, setAttribute() {} }; },
    addEventListener() {},
    body: { appendChild() {} },
  };
  const toasts = [];
  const sandbox = {
    console, JSON, Math, Date, String, Number, Array, Object, Boolean, Promise,
    setTimeout, clearTimeout, setInterval, clearInterval,
    document,
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    sessionStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    // I18N mock：返回 key 本身，让断言可预测
    I18N: { t(k) { return k; } },
    API: {
      get(url) {
        if (url === '/repos/migrate-orgs') {
          if (opts.apiError) return Promise.reject(new Error(opts.apiError));
          return Promise.resolve(opts.apiData || {});
        }
        return Promise.reject(new Error('Unknown: ' + url));
      },
    },
    app: {
      _toasts: toasts,
      showToast(msg, kind) { toasts.push({ msg: msg, kind: kind || '' }); },
      nav() {},
      currentRepo() { return opts.currentRepo || null; },
      initSidebarRepos() {},
      showConfirm() {},
    },
  };
  sandbox.window = sandbox;
  sandbox.__test = { elements, toasts };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SETTINGS_JS, 'utf8'), sandbox, { filename: 'settings.js' });
  return sandbox;
}

const INJECTION = '"><img src=x onerror=alert(1)>';

function drain() {
  return new Promise(r => setImmediate(() => setImmediate(r)));
}

async function main() {
  const drain = () => new Promise(r => setImmediate(r));

  // 1. XSS 回归：login/name/personal 含注入载荷 → 全部转义，标签不落 DOM
  {
    const sb = makeSandbox({
      currentRepo: { owner: 'curOwner', name: 'demo-repo', fullName: 'curOwner/demo-repo' },
      apiData: {
        orgs: [
          { login: INJECTION, name: '"><script>alert("xss")</script> & <b>bold</b>', avatar_url: '' },
          { login: "org's & \"quotes\"", name: '', avatar_url: '' },
        ],
        personal: INJECTION,
      },
    });
    const { elements } = sb.__test;
    sb._loadMigrateOrgs();
    await drain();
    const html = elements.migrateTargetOwner.innerHTML;
    assert.ok(html.indexOf('<img src=x onerror=') < 0, 'XSS：注入的 img 标签不得原样落 DOM');
    assert.ok(html.indexOf('<script>') < 0, 'XSS：注入的 script 标签不得原样落 DOM');
    assert.ok(html.indexOf('&lt;img src=x onerror=alert(1)&gt;') >= 0, 'XSS：login 中载荷应被转义（label 侧）');
    assert.ok(html.indexOf('&quot;&gt;&lt;img') >= 0, 'XSS：personal 载荷应被转义（含引号/尖括号）');
    assert.ok(html.indexOf('&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;') >= 0, 'XSS：name 中 script 应被转义');
    assert.ok(html.indexOf('&#39;') >= 0 && html.indexOf('&amp;') >= 0, "XSS：单引号与 & 应转义（' &#39; / & &amp;）");
    // value 属性侧同样转义：提取每个 option 的 value 属性内容，不得出现裸 <（实体 &lt; 可以）
    const valueAttrs = [];
    const re = /<option value="([^"<]*)">/g;
    let vm2;
    while ((vm2 = re.exec(html)) !== null) valueAttrs.push(vm2[1]);
    for (const v of valueAttrs) {
      assert.ok(v.indexOf('<') < 0, 'XSS：option value 属性内不得出现未转义 <（实际: ' + v + '）');
    }
    // 至少渲染出 3 个 option（占位/personal/注入 org），且 value 均为实体形态
    assert.ok(valueAttrs.length >= 3,
      'XSS：至少渲染出 3+ 个 option（占位/personal/org），实际 ' + valueAttrs.length);
    console.log('  ✅ 1. XSS 转义：login/name/personal 注入载荷全部转义，标签不落 DOM');
  }

  // 2. _migrateEsc 单元行为
  {
    const sb = makeSandbox({});
    const esc = sb._migrateEsc;
    assert.strictEqual(typeof esc, 'function', '_migrateEsc 应为函数');
    assert.strictEqual(esc('<a href="x">&\''), '&lt;a href=&quot;x&quot;&gt;&amp;&#39;',
      '_migrateEsc：& < > " \' 五类字符全转义');
    assert.strictEqual(esc(''), '', '_migrateEsc：空串返回空串');
    assert.strictEqual(esc(null), '', '_migrateEsc：null 安全返回空串');
    assert.strictEqual(esc(undefined), '', '_migrateEsc：undefined 安全返回空串');
    assert.strictEqual(esc(0), '', '_migrateEsc：0 走 falsy 分支返回空串');
    assert.strictEqual(esc(123), '123', '_migrateEsc：非字符串数字转字符串');
    assert.strictEqual(esc('plain-org'), 'plain-org', '_migrateEsc：普通组织名原样返回');
    console.log('  ✅ 2. _migrateEsc 单元行为：五类特殊字符 + 空值/非字符串安全');
  }

  // 3. 正常渲染：personal + orgs → 占位符 + 个人选项 + 组织选项
  {
    const sb = makeSandbox({
      currentRepo: { owner: 'curOwner', name: 'demo-repo', fullName: 'curOwner/demo-repo' },
      apiData: {
        orgs: [{ login: 'orgA', name: 'Org Alpha', avatar_url: '' }],
        personal: 'alice',
      },
    });
    const { elements } = sb.__test;
    sb._loadMigrateOrgs();
    await drain();
    const html = elements.migrateTargetOwner.innerHTML;
    assert.ok(html.indexOf('settings.host.migrate_owner_placeholder') >= 0, '正常：首项应为占位符选项');
    assert.ok(/<option value="alice">alice \(settings\.host\.migrate_personal\)<\/option>/.test(html),
      '正常：personal 应渲染为 "alice (个人)" 选项');
    assert.ok(/<option value="orgA">/.test(html), '正常：orgA 应出现在下拉中');
    assert.strictEqual(sb.__test.toasts.length, 0, '正常：不应弹 toast');
    console.log('  ✅ 3. 正常渲染：占位符 + personal (个人) + orgs');
  }

  // 4. 排除当前 owner：org 项与 personal 均排除
  {
    const sb = makeSandbox({
      currentRepo: { owner: 'curOwner', name: 'demo-repo', fullName: 'curOwner/demo-repo' },
      apiData: {
        orgs: [
          { login: 'curOwner', name: 'Current Owner Org', avatar_url: '' },
          { login: 'orgB', name: 'Org B', avatar_url: '' },
        ],
        personal: 'curOwner',
      },
    });
    const { elements } = sb.__test;
    sb._loadMigrateOrgs();
    await drain();
    const html = elements.migrateTargetOwner.innerHTML;
    assert.ok(/<option value="curOwner">/.test(html) === false, '排除：当前 owner 的 org 项不得出现');
    assert.ok(html.indexOf('Current Owner Org') < 0, '排除：当前 owner org 的 name 不得出现');
    assert.ok(html.indexOf('settings.host.migrate_personal') < 0, '排除：personal === 当前 owner 时个人选项不得出现');
    assert.ok(/<option value="orgB">/.test(html), '排除：非当前 owner 的 orgB 仍应出现');
    console.log('  ✅ 4. 排除当前 owner：org 项与 personal 双路径排除');
  }

  // 5. label 拼装：name ≠ login → "login (name)"；name 缺失/相同 → 仅 login
  {
    const sb = makeSandbox({
      currentRepo: { owner: 'curOwner', name: 'demo-repo', fullName: 'curOwner/demo-repo' },
      apiData: {
        orgs: [
          { login: 'orgX', name: 'Display Name', avatar_url: '' },
          { login: 'orgY', name: 'orgY', avatar_url: '' },
          { login: 'orgZ', name: '', avatar_url: '' },
        ],
        personal: '',
      },
    });
    const { elements } = sb.__test;
    sb._loadMigrateOrgs();
    await drain();
    const html = elements.migrateTargetOwner.innerHTML;
    assert.ok(/<option value="orgX">orgX \(Display Name\)<\/option>/.test(html),
      'label：name ≠ login → "login (name)"');
    assert.ok(/<option value="orgY">orgY<\/option>/.test(html),
      'label：name === login → 仅 login');
    assert.ok(/<option value="orgZ">orgZ<\/option>/.test(html),
      'label：name 为空 → 仅 login');
    console.log('  ✅ 5. label 拼装：login (name) / 仅 login 三种形态');
  }

  // 6. 失败降级：API reject → 「加载失败」占位 + warning toast
  {
    const sb = makeSandbox({
      currentRepo: { owner: 'curOwner', name: 'demo-repo', fullName: 'curOwner/demo-repo' },
      apiError: 'network down',
    });
    const { elements, toasts } = sb.__test;
    sb._loadMigrateOrgs();
    await drain();
    const html = elements.migrateTargetOwner.innerHTML;
    assert.ok(/<option value="">settings\.host\.migrate_orgs_load_failed<\/option>/.test(html),
      '降级：失败后下拉应为「加载失败」占位');
    assert.ok(toasts.some(t => t.msg === 'settings.host.migrate_orgs_load_failed' && t.kind === 'warning'),
      '降级：应弹 warning toast');
    console.log('  ✅ 6. 失败降级：加载失败占位 + warning toast');
  }

  // 7. 空列表降级：orgs 空 && personal 空 → 「无可迁移的组织」
  {
    const sb = makeSandbox({
      currentRepo: { owner: 'curOwner', name: 'demo-repo', fullName: 'curOwner/demo-repo' },
      apiData: { orgs: [], personal: '' },
    });
    const { elements } = sb.__test;
    sb._loadMigrateOrgs();
    await drain();
    const html = elements.migrateTargetOwner.innerHTML;
    assert.ok(/<option value="">settings\.host\.migrate_orgs_empty<\/option>/.test(html),
      '空列表：应渲染「无可迁移的组织」占位');
    assert.ok(html.indexOf('migrate_owner_placeholder') < 0, '空列表：占位符选项应被替换而非追加');
    console.log('  ✅ 7. 空列表降级：无可迁移的组织占位');
  }

  console.log('\n✅ Issue #543 迁移组织下拉框测试全部通过（7 组断言：XSS 转义 / esc 单元 / 正常渲染 / 排除 owner / label 拼装 / 失败降级 / 空列表降级）');
}

main().then(() => process.exit(0)).catch(e => { console.error('❌', e.message); console.error(e.stack); process.exit(1); });
