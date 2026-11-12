/*
 * Issue #1916 —— 可观测性（驾驶舱）页「找不到你想看的内容？」反馈入口
 *
 * 覆盖三个切片（seam 与 docs/issue-1916/plan.md 对齐）：
 *   S1  _obsFeedbackPageUrl()：page_url 上下文拼装纯函数
 *   S2  openObsFeedback()：未登录不渲染；面板 DOM 结构（类型单选默认缺指标、500 字数、提交禁用态）
 *   S3  空内容提交 → 无 API.post；输入后解除禁用
 *   S4  正常提交 → API.post 参数契约（category=suggestion / feedback_kind / title / description / page_url / repo_key）
 *   S5  提交中防重（pending 期二次提交只发一次）
 *   S6  失败保留内容可重试；成功移除面板
 *   S7  页头入口：登录渲染、未登录不渲染；i18n zh/en 键齐备
 *
 * 运行：node tests/js/test_obs_feedback_entry_1916.js
 */
const path = require('path');
const fs = require('fs');
const assert = require('assert');

const ROOT = path.resolve(__dirname, '../../..');
let passed = 0, failed = 0;
function check(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n      ' + e.message); }
}
async function checkAsync(name, fn) {
  try { await fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n      ' + e.message); }
}

// ── 真实 i18n 语义（沿用 test_observability_i18n.js 的真实语言包驱动法）──
function loadPack(lang, mod) {
  const p = path.join(ROOT, 'src/api/static/locales', lang, mod + '.json');
  return fs.existsSync(p) ? JSON.parse(fs.readFileSync(p, 'utf8')) : null;
}
const PACKS = {
  zh: { observability: loadPack('zh', 'observability'), app: loadPack('zh', 'app') },
  en: { observability: loadPack('en', 'observability'), app: loadPack('en', 'app') },
};
function lookup(pack, key) {
  if (!pack || !key) return undefined;
  let cur = pack;
  for (const part of key.split('.')) {
    if (cur == null || typeof cur !== 'object') return undefined;
    cur = cur[part];
  }
  return cur;
}
function interp(str, vars) {
  if (!str || typeof str !== 'string' || !vars) return str;
  return str.replace(/\{\{?(\w+)\}\}?/g, (m, n) => (Object.prototype.hasOwnProperty.call(vars, n) ? String(vars[n]) : m));
}
function realT(lang) {
  return function t(key, vars) {
    let val = lookup(PACKS[lang], key);
    if (val === undefined) val = lookup(PACKS.zh, key);
    if (val === undefined || val === null) return key;
    if (typeof val !== 'string') return val;
    return interp(val, vars);
  };
}

// ── DOM 桩：按需返回带 value/disabled/innerHTML 的元素；createElement 挂 id ──
// 注意：getElementById 必须只在元素真实存在时返回（惰性创建会让 openObsFeedback 的
// 去重检查 `getElementById('obsFbOverlay')` 永远命中旧面板而提前 return）。
function makeDom() {
  const elements = {};
  const mk = (id) => {
    const el = {
      _id: id, style: {}, innerHTML: '', textContent: '', value: '', disabled: false, focus: () => {},
      classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, toggle() {}, contains(c) { return this._s.has(c); } },
      querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {}, closest: () => null,
      appendChild: (child) => { if (child && child._id) elements[child._id] = child; },
      remove: () => { delete elements[el._id]; },
      setAttribute: (k, v) => { if (k === 'id') el._id = v; }, removeAttribute: () => {}, getAttribute: () => null,
      onclick: null, onchange: null, oninput: null,
    };
    // id accessor：业务代码 `overlay.id = 'x'` / 读 `.id` 时同步 _id 并登记（模拟真实 DOM）
    Object.defineProperty(el, 'id', {
      get() { return el._id; },
      set(v) { delete elements[el._id]; el._id = v; elements[v] = el; },
      configurable: true,
    });
    return el;
  };
  const dom = {
    getElementById: (id) => (id === 'pageContent' && !elements[id]) ? (elements[id] = mk(id)) : elements[id],
    querySelector: () => null, querySelectorAll: () => [],
    createElement: () => mk('_new'),
    body: { appendChild: (el) => { if (el && el._id) elements[el._id] = el; } },
    documentElement: { setAttribute() {}, removeAttribute() {} },
    addEventListener: () => {},
    _elements: elements,
  };
  return dom;
}

// ── 加载 observability.js（沙箱）──
function loadObs(dom, opts) {
  const src = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/observability.js'), 'utf8');
  const state = { posts: [], gets: [] };
  opts = opts || {};
  const postImpl = opts.postImpl || (() => Promise.resolve({ id: 9, status: 'open' }));
  const sandbox = {
    document: dom,
    window: {
      _currentView: 'observability',
      history: { pushState() {}, replaceState() {}, state: null },
      location: { hash: opts.hash || '#observability/overview' },
      addEventListener: () => {},
      _obsFb: undefined,
    },
    localStorage: {
      getItem: (k) => {
        if (k === 'token') return opts.token !== undefined ? opts.token : 'jwt-abc';
        if (k === 'user') return opts.user !== undefined ? opts.user : JSON.stringify({ username: 'alice', nickname: 'Alice' });
        return null;
      },
      setItem: () => {}, removeItem: () => {}, clear: () => {},
    },
    sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    API: {
      get: (u) => { state.gets.push(u); return Promise.resolve({}); },
      post: (u, body) => { state.posts.push({ url: u, body }); return postImpl({ url: u, body }); },
    },
    app: { currentRepo: () => (opts.repo === undefined ? { fullName: 'org/demo', owner: 'org', name: 'demo', repoKey: 'gitcode:org/demo' } : opts.repo), showToast: (m) => { state.toasts = state.toasts || []; state.toasts.push(m); } },
    I18N: { t: realT(opts.lang || 'zh'), getLang: () => (opts.lang || 'zh') },
    console: { log: () => {}, info: () => {}, error: () => {}, warn: () => {} },
    fmtLocalTime: (s) => s || '',
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}), text: () => Promise.resolve('') }),
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval,
    Date, JSON, Array, Object, String, Number, Boolean, Math, encodeURIComponent, decodeURIComponent, URLSearchParams,
    CustomEvent: function () {}, AbortController: function () { this.abort = () => {}; this.signal = {}; },
    AbortSignal: { timeout: () => ({}) }, openRuntimeSessionModal: () => {}, openRuntimeTaskModal: () => {},
  };
  const ks = Object.keys(sandbox), vs = ks.map((k) => sandbox[k]);
  const ex = '\n; return { renderObservability, switchObsView, openObsFeedback, _obsFeedbackPageUrl, _obsFbSubmit: (typeof _obsFbSubmit !== "undefined" ? _obsFbSubmit : undefined), _obsFb: (typeof _obsFb !== "undefined" ? _obsFb : undefined) };';
  return { obs: new Function(...ks, src + ex)(...vs), state, dom };
}

// 面板 innerHTML 由真实浏览器解析生成子元素（obsFbText 等）；桩不解析 HTML，
// 测试手动注册等价元素（openObsFeedback 后调用）。
function mountPanelChildren(dom) {
  const reg = (id) => {
    const el = dom._elements[id];
    if (el) return el;
    const fresh = {
      _id: id, style: {}, innerHTML: '', textContent: '', value: '', disabled: false,
      focus() {}, remove() { delete dom._elements[id]; },
      setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
      classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, toggle() {}, contains(c) { return this._s.has(c); } },
    };
    dom._elements[id] = fresh;
    return fresh;
  };
  ['obsFbText', 'obsFbCount', 'obsFbSubmit', 'obsFbCancel'].forEach(reg);
  return dom;
}

const tick = (ms) => new Promise((r) => setTimeout(r, ms === undefined ? 20 : ms));

// ════════ S1 · page_url 上下文拼装 ════════
(async () => {
  console.log('S1 _obsFeedbackPageUrl 上下文拼装');
  const { obs } = loadObs(makeDom(), { hash: '#observability/inspect', repo: { fullName: 'org/demo' } });
  // 分区状态在沙箱内：先切到 inspect 再取 page_url
  obs.renderObservability('inspect');
  await tick(10);

  check('S1.1 返回值以当前 hash 为开头', () => {
    const u = obs._obsFeedbackPageUrl();
    assert.ok(u.indexOf('#observability/') === 0, '应以 hash 路由开头，实际: ' + u);
  });

  check('S1.2 含 view=observability 与 section', () => {
    const u = obs._obsFeedbackPageUrl();
    assert.ok(/[?&]view=observability\b/.test(u), '缺 view=observability: ' + u);
    assert.ok(/[?&]section=inspect\b/.test(u), '缺 section: ' + u);
  });

  check('S1.3 含 repo 与 user（非空才拼）', () => {
    const u = obs._obsFeedbackPageUrl();
    assert.ok(/[?&]repo=org%2Fdemo\b/.test(u), '缺 repo=org%2Fdemo: ' + u);
    assert.ok(/[?&]user=alice\b/.test(u), '缺 user=alice: ' + u);
  });

  check('S1.4 range 随分区补齐（overview → ov range；devices → res range）', () => {
    const { obs: o2 } = loadObs(makeDom(), { hash: '#observability/overview' });
    o2.renderObservability('overview');
    const u2 = o2._obsFeedbackPageUrl();
    assert.ok(/[?&]range=24h\b/.test(u2), '总览应带默认 range=24h: ' + u2);
  });

  check('S1.5 空 repo/无 user 时不拼对应字段', () => {
    const { obs: o3 } = loadObs(makeDom(), { hash: '#observability/overview', repo: null, user: JSON.stringify({}) });
    o3.renderObservability('overview');
    const u3 = o3._obsFeedbackPageUrl();
    assert.ok(!/[?&]repo=/.test(u3), '无仓库不应拼 repo: ' + u3);
    assert.ok(!/[?&]user=/.test(u3), '无用户不应拼 user: ' + u3);
  });

  check('S1.6 超长截断 ≤ 500 字符', () => {
    const { obs: o4 } = loadObs(makeDom(), {
      hash: '#observability/' + 'x'.repeat(480), repo: { fullName: 'org/very-long-repo-name' },
    });
    o4.renderObservability('overview');
    const u4 = o4._obsFeedbackPageUrl();
    assert.ok(u4.length <= 500, '应截断到 ≤500，实际 ' + u4.length);
  });

  check('S1.7 超长截断不产生残缺 percent-encoding', () => {
    const { obs: o5 } = loadObs(makeDom(), {
      hash: '#o/' + 'x'.repeat(493) + '%e4%b8%ad',   // 令截断点落在 %e4%b8%ad 转义中间
    });
    o5.renderObservability('overview');
    const u5 = o5._obsFeedbackPageUrl();
    assert.ok(u5.length <= 500, '应截断到 ≤500，实际 ' + u5.length);
    assert.ok(!/%[0-9a-fA-F]?$/.test(u5), '不应以残缺转义结尾: ' + u5.slice(-8));
  });

  // ════════ S2 · 面板渲染与登录门 ════════
  console.log('S2 openObsFeedback 登录门与面板结构');
  check('S2.1 未登录（无 token）不渲染面板', () => {
    const dom = makeDom();
    const { obs: o } = loadObs(dom, { token: null });
    o.openObsFeedback();
    assert.ok(!dom._elements.obsFbOverlay, '未登录不应渲染 obsFbOverlay');
  });

  check('S2.2 已登录渲染面板且含 3 类单选（默认缺指标）与 500 字数', () => {
    const dom = makeDom();
    const { obs: o } = loadObs(dom, {});
    o.openObsFeedback();
    const html = (dom._elements.obsFbOverlay || {}).innerHTML || '';
    assert.ok(html.indexOf('missing_metric') >= 0, '缺 missing_metric 类型');
    assert.ok(html.indexOf('want_content') >= 0, '缺 want_content 类型');
    assert.ok(html.indexOf('other') >= 0, '缺 other 类型');
    assert.ok(/data-kind="missing_metric"[^>]*data-active="1"|data-active="1"[^>]*data-kind="missing_metric"/.test(html)
      || (html.indexOf('data-kind="missing_metric"') >= 0 && html.indexOf('data-active') >= 0), '默认类型应为 missing_metric');
    assert.ok(/\/\s*500|500字|maxlength="500"/.test(html), '应展示 500 字上限: ' + html.slice(0, 200));
    assert.ok(html.indexOf('0/500') >= 0, '字数展示应使用 locale count 模板（0/500）');
    assert.ok(html.indexOf('view=observability') >= 0, '面板应可见展示附带上下文');
  });

  // ════════ S3/S4/S5/S6 · 提交契约与防重 ════════
  console.log('S3-S6 提交契约/防重/失败重试');

  await checkAsync('S3.1 空内容提交 → 不发 API.post', async () => {
    const dom = makeDom();
    const { obs: o, state } = loadObs(dom, {});
    o.openObsFeedback();
    mountPanelChildren(dom);
    dom.getElementById('obsFbText').value = '';
    try { await o._obsFbSubmit(); } catch (e) { /* 面板缺失时允许抛错，但绝不能发请求 */ }
    assert.strictEqual(state.posts.filter(p => p.url === '/feedback').length, 0, '空内容不应提交');
  });

  await checkAsync('S3.2 超长内容（>500，程序赋值绕过 maxlength）提交 → 提示并不发请求', async () => {
    const dom = makeDom();
    const { obs: o, state } = loadObs(dom, {});
    o.openObsFeedback();
    mountPanelChildren(dom);
    dom.getElementById('obsFbText').value = 'x'.repeat(501);
    await o._obsFbSubmit();
    assert.strictEqual(state.posts.filter(p => p.url === '/feedback').length, 0, '超长内容不应提交');
    assert.ok((state.toasts || []).length > 0, '超长提交应有失败提示 toast');
  });

  await checkAsync('S4.1 正常提交参数契约', async () => {
    const dom = makeDom();
    const { obs: o, state } = loadObs(dom, {});
    o.openObsFeedback();
    mountPanelChildren(dom);
    const ta = dom.getElementById('obsFbText');
    ta.value = '想看各仓库的失败率对比';
    await o._obsFbSubmit();
    assert.strictEqual(state.posts.length, 1, '应恰好提交一次，实际 ' + state.posts.length);
    const p = state.posts[0];
    assert.strictEqual(p.url, '/feedback');
    assert.strictEqual(p.body.category, 'suggestion', 'category 应为 suggestion');
    assert.strictEqual(p.body.feedback_kind, 'missing_metric', '默认类型 missing_metric');
    assert.strictEqual(p.body.description, '想看各仓库的失败率对比');
    assert.ok(p.body.title.indexOf('驾驶舱') >= 0, 'title 应含页面标识「驾驶舱舱」: ' + p.body.title);
    assert.ok(p.body.page_url.indexOf('view=observability') >= 0, 'page_url 应带上下文');
    assert.strictEqual(p.body.repo_key, 'org/demo', 'repo_key 应为当前仓库');
  });

  await checkAsync('S5.1 提交中防重（pending 二次提交只发一次）', async () => {
    const dom = makeDom();
    let resolveFirst;
    const { obs: o, state } = loadObs(dom, {
      postImpl: () => new Promise((res) => { resolveFirst = res; }),
    });
    o.openObsFeedback();
    mountPanelChildren(dom);
    dom.getElementById('obsFbText').value = '内容A';
    const p1 = o._obsFbSubmit();
    const p2 = o._obsFbSubmit();  // pending 期间重复触发
    resolveFirst({ id: 1, status: 'open' });
    await Promise.all([p1, p2]);
    assert.strictEqual(state.posts.length, 1, '防重失败：pending 期间又发了一次（共 ' + state.posts.length + ' 次）');
  });

  await checkAsync('S5.2 在途期间取消并重开面板不可双发（防重不被入口重置）', async () => {
    const dom = makeDom();
    let resolveFirst;
    const { obs: o, state } = loadObs(dom, {
      postImpl: () => new Promise((res) => { resolveFirst = res; }),
    });
    o.openObsFeedback();
    mountPanelChildren(dom);
    dom.getElementById('obsFbText').value = '内容A';
    const p1 = o._obsFbSubmit();
    // 模拟用户取消面板再重开（原请求仍在途）
    dom._elements.obsFbOverlay.remove();
    o.openObsFeedback();
    mountPanelChildren(dom);
    dom.getElementById('obsFbText').value = '内容B';
    const p2 = o._obsFbSubmit();
    resolveFirst({ id: 1, status: 'open' });
    await Promise.all([p1, p2]);
    assert.strictEqual(state.posts.length, 1, '取消重开后在途期间又发了一次（共 ' + state.posts.length + ' 次）');
  });

  await checkAsync('S6.1 失败保留内容且可重试（按钮恢复）', async () => {
    const dom = makeDom();
    let fail = true;
    const { obs: o, state } = loadObs(dom, {
      postImpl: () => (fail ? Promise.reject(new Error('500')) : Promise.resolve({ id: 2 })),
    });
    o.openObsFeedback();
    mountPanelChildren(dom);
    const ta = dom.getElementById('obsFbText');
    ta.value = '保留我';
    await o._obsFbSubmit().catch(() => {});
    assert.ok(dom._elements.obsFbOverlay, '失败后面板应保留');
    assert.strictEqual(ta.value, '保留我', '失败后内容应保留');
    const btn = dom.getElementById('obsFbSubmit');
    assert.ok(!btn.disabled, '失败后提交按钮应恢复可用');
    fail = false;
    await o._obsFbSubmit();
    assert.strictEqual(state.posts.length, 2, '重试应再次提交');
  });

  await checkAsync('S6.2 成功后关闭面板', async () => {
    const dom = makeDom();
    const { obs: o } = loadObs(dom, {});
    o.openObsFeedback();
    mountPanelChildren(dom);
    dom.getElementById('obsFbText').value = 'ok';
    await o._obsFbSubmit();
    assert.ok(!dom._elements.obsFbOverlay, '成功后面板应移除');
  });

  // ════════ S7 · 页头入口 + i18n ════════
  console.log('S7 页头入口与 i18n');

  check('S7.1 登录态页头渲染入口（含主/副文案）', () => {
    const dom = makeDom();
    const { obs: o } = loadObs(dom, { hash: '#observability/overview' });
    o.renderObservability('overview');
    const html = (dom._elements.pageContent || {}).innerHTML || '';
    assert.ok(html.indexOf('openObsFeedback()') >= 0, '页头应有 openObsFeedback 入口');
    assert.ok(html.indexOf('找不到你想看的内容') >= 0, '缺主文案');
    assert.ok(html.indexOf('告诉我们你想看什么') >= 0, '缺副文案');
  });

  check('S7.2 未登录不渲染入口', () => {
    const dom = makeDom();
    const { obs: o } = loadObs(dom, { token: null });
    o.renderObservability('overview');
    const html = (dom._elements.pageContent || {}).innerHTML || '';
    assert.ok(html.indexOf('openObsFeedback()') < 0, '未登录不应渲染入口');
  });

  check('S7.3 i18n 键 zh/en 全覆盖（fb_* 段）', () => {
    const need = ['entry', 'entry_sub', 'title', 'placeholder', 'kind_missing_metric', 'kind_want_content', 'kind_other',
      'submit', 'submitting', 'cancel', 'success', 'failed', 'login_required', 'count'];
    for (const lang of ['zh', 'en']) {
      const seg = ((PACKS[lang].observability || {}).fb) || {};
      for (const k of need) {
        assert.ok(typeof seg[k] === 'string' && seg[k].length, `[${lang}] observability.fb.${k} 缺失`);
      }
    }
  });

  check('S7.4 窄屏入口图标化（CSS 断点）', () => {
    const html = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/observability.js'), 'utf8');
    assert.ok(/obs-fb-ico|obsFbEntryIcon/.test(html), '应有窄屏图标元素');
    const css = fs.readFileSync(path.join(ROOT, 'src/api/static/css/app.css'), 'utf8');
    assert.ok(/@media[^{]*\(max-width:\s*768px\)[^}]*obs-fb/.test(css) || /obs-fb[\s\S]{0,400}@media/.test(css + html),
      '应有 obs-fb 相关窄屏媒体查询（CSS 或 inline）');
  });

  console.log('\n结果: ' + passed + ' 通过, ' + failed + ' 失败');
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
