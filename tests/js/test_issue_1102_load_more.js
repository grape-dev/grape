/**
 * Issue #1102 回归测试：任务会话页事件历史 tasks.load_earlier_events（提示文本/跳转/加载方向）
 *
 * 运行：node tests/js/test_issue_1102_load_more.js
 *
 * 背景（用户反馈单 7879，weekly-10 已上线 #926 实现暴露三缺陷，本次在 weekly-10 基线上按修正行为实现）：
 *   1. 提示文本不准确：事件历史按钮复用 tasks.load_more（看板滚动语义），新增 tasks.load_earlier_events
 *      zh/en 键并改 3 参 fallback，文案与实际「加载更早的事件」行为一致
 *   2. 点击异常跳转：按钮位于整卡 onclick=_jumpToPipePage()（→ #pipe/{issue}）的卡片内，
 *      按钮 onclick 未阻断冒泡 → 点击「加载更多」跳转 /app/#pipe/868
 *   3. 加载方向相反：events 为 seq DESC（最新在前），slice(events.length - shown) 取最旧段
 *      → 默认展示旧事件、「加载更多」向更新方向展开
 *
 * 验证点（vm 沙箱加载真实 issues.js，mock DOM/I18N，模拟 pipeline 接口数据）：
 *   1. zh/en tasks.json 含非空 load_earlier_events 键（i18n 落地，语义准确）
 *   2. 默认渲染最新 20 条：新事件在列、更早的被折叠；计数标注 (20/N)；渲染加载更多按钮
 *   3. 按钮 onclick 首句 event.stopPropagation()，不含 _jumpToPipePage（不冒泡→不跳转）
 *   4. 模拟点击：事件向更早方向追加（最旧事件出现、行数=全部、按钮隐藏、不触发跳转）
 *   5. 事件数 ≤ 默认批大小时不渲染按钮
 *   6. _openBoardDetail 进入新详情重置展开量（防跨任务串扰）
 *   7. index.html issues.js?v ≥ 133（缓存失效，用户能拿到新 JS）
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const ISSUES_JS = path.join(ROOT, 'src/api/static/js/pages/issues.js');
const INDEX_HTML = path.join(ROOT, 'src/api/static/index.html');
const ZH_TASKS = path.join(ROOT, 'src/api/static/locales/zh/tasks.json');
const EN_TASKS = path.join(ROOT, 'src/api/static/locales/en/tasks.json');

const DEFAULT_BATCH = 20;

// ── 极简 DOM mock（只覆盖 _renderPipelineSection / _openBoardDetail 依赖面）──
function makeSandbox() {
  const elements = {};
  const ids = ['_boardPipelineSection', '_grapePulseCSS', '_taskListPanel', 'pageContent',
    'taskBoard', 'taskList', '_taskAttachmentsList'];
  for (const id of ids) {
    elements[id] = {
      _id: id, _html: '',
      set innerHTML(v) { this._html = String(v); },
      get innerHTML() { return this._html; },
      textContent: '', style: {}, dataset: {},
      appendChild() {}, remove() {},
      addEventListener() {}, removeEventListener() {},
      querySelector() { return null; }, querySelectorAll() { return []; },
      classList: { add() {}, remove() {}, contains() { return false; } },
    };
  }
  const document = {
    getElementById(id) { return elements[id] || null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    createElement() {
      return { style: {}, classList: { add() {}, remove() {} }, textContent: '', innerHTML: '',
        appendChild() {}, remove() {}, addEventListener() {}, setAttribute() {} };
    },
    addEventListener() {},
    head: { appendChild() {} },
    body: { appendChild() {} },
  };
  const sandbox = {
    console, JSON, Math, Date, String, Number, Array, Object, Boolean, Promise,
    setTimeout, clearTimeout,
    // setInterval 桩：不真实调度（防 node 进程挂起），仅返回句柄
    setInterval() { return 0; }, clearInterval() {},
    document,
    localStorage: { getItem() { return ''; }, setItem() {}, removeItem() {} },
    sessionStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    fetch() { return Promise.resolve({ ok: false }); },
    requestAnimationFrame(fn) { fn(); return 0; },
    navigator: { userAgent: 'node-test' },
    location: { hash: '', href: 'http://localhost/' },
    // I18N mock：返回 key 本身，让断言可预测（渲染是否走 i18n key 可直接断言）
    I18N: { t(k) { return k; } },
    renderMarkdown(s) { return String(s || ''); },
    fmtLocalTime(s) { return s || ''; },
    _vcsUrl(repo, p) { return 'https://vcs.example/' + repo + p; },
    API: {
      get() { return Promise.resolve({}); },
      post() { return Promise.resolve({}); },
      put() { return Promise.resolve({}); },
    },
    app: { showToast() {}, nav() {}, currentRepo() { return null; } },
    _grapeConfirm() {},
  };
  sandbox.window = sandbox;
  sandbox._currentView = 'tasks';
  sandbox.__test = { elements };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(ISSUES_JS, 'utf8'), sandbox, { filename: 'issues.js' });
  return sandbox;
}

// pipeline 接口 mock：events 为 seq DESC（最新在前，与 get_events ORDER BY seq DESC 一致）
function pipelineData(n) {
  const events = [];
  for (let i = 0; i < n; i++) {
    events.push({ seq: 950 - i, agent_type: 'vod', event_type: 'a2a_event',
      status: 'completed', created_at: '2026-11-06T0' + (i % 10) + ':00:00Z',
      title: 'EV-' + i, summary: 'event ' + i });
  }
  return { scene_name: 'dev-scene', nodes: [], events };
}

function loadMoreOnclick(html) {
  const m = html.match(/onclick="([^"]*_loadMorePipeEvents[^"]*)"/);
  return m ? m[1].replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&') : null;
}

function countEventRows(html) {
  return (html.match(/EV-\d+/g) || []).length;
}

async function main() {
  const drain = () => new Promise(r => setImmediate(r));

  // 1. zh/en tasks.json 含非空 load_earlier_events 键（i18n 落地）
  {
    for (const f of [ZH_TASKS, EN_TASKS]) {
      const pack = JSON.parse(fs.readFileSync(f, 'utf8'));
      assert.ok(typeof pack.load_earlier_events === 'string' && pack.load_earlier_events.length > 0,
        `${path.basename(path.dirname(f))}/tasks.json 应含非空 load_earlier_events 键`);
      assert.notStrictEqual(pack.load_earlier_events, 'load_earlier_events',
        'load_earlier_events 文案不应是原始 key 回显');
    }
  }

  // 2. 默认渲染最新 20 条（新事件在列、更早的被折叠），计数 + 按钮齐备
  {
    const sb = makeSandbox();
    const { elements } = sb.__test;
    sb._renderPipelineSection(pipelineData(25));
    const html = elements._boardPipelineSection.innerHTML;
    assert.strictEqual(countEventRows(html), DEFAULT_BATCH,
      '默认应只渲染最新 20 条事件（分批展示）');
    assert.ok(html.indexOf('EV-0') >= 0 && html.indexOf('EV-19') >= 0,
      '默认应展示最新事件（EV-0 最新、EV-19 第 20 新）');
    assert.ok(html.indexOf('EV-20') < 0 && html.indexOf('EV-24') < 0,
      '更早的事件（EV-20..EV-24）默认应被折叠');
    assert.ok(html.indexOf('(20/25)') >= 0, '应渲染 (已展示/总数) 计数标注');
    const handler = loadMoreOnclick(html);
    assert.ok(handler, '应渲染「加载更多」按钮（引用 _loadMorePipeEvents）');
    assert.ok(html.indexOf('tasks.load_earlier_events') >= 0, '按钮文案应走 I18N tasks.load_earlier_events 键');
  }

  // 3. 按钮 onclick 阻断冒泡且不含跳转（点击不触发 #pipe/{n} 整页跳转）
  {
    const sb = makeSandbox();
    const { elements } = sb.__test;
    sb._renderPipelineSection(pipelineData(25));
    const handler = loadMoreOnclick(elements._boardPipelineSection.innerHTML);
    assert.ok(handler, '应渲染加载更多按钮');
    assert.ok(/^\s*event\.stopPropagation\(\)/.test(handler),
      'onclick 首句应 event.stopPropagation()（卡片 onclick=_jumpToPipePage 会整页跳转）');
    assert.ok(handler.indexOf('_jumpToPipePage') < 0, '按钮 onclick 不应触发跳转');
  }

  // 4. 模拟点击：向更早方向追加事件（最旧事件出现、行数=全部、按钮隐藏、冒泡被阻断）
  {
    const sb = makeSandbox();
    const { elements } = sb.__test;
    sb._renderPipelineSection(pipelineData(25));
    let html = elements._boardPipelineSection.innerHTML;
    const handler = loadMoreOnclick(html);
    let stopCalled = false;
    sb.event = { stopPropagation() { stopCalled = true; } };
    assert.doesNotThrow(() => vm.runInContext(handler, sb), '点击处理不应抛错');
    await drain();
    html = elements._boardPipelineSection.innerHTML;
    assert.ok(stopCalled, '点击应调用 event.stopPropagation()（不冒泡→不跳转）');
    assert.strictEqual(countEventRows(html), 25, '点击后应向更早方向追加至全部事件');
    assert.ok(html.indexOf('EV-24') >= 0, '最旧事件 EV-24 点击后应出现（方向：更早）');
    assert.ok(html.indexOf('EV-0') >= 0, '最新事件 EV-0 应始终保留（始终展示最新）');
    assert.strictEqual(loadMoreOnclick(html), null, '全部展示后按钮应隐藏');
  }

  // 5. 事件数 ≤ 默认批大小时不渲染按钮（也不折叠）
  {
    const sb = makeSandbox();
    const { elements } = sb.__test;
    sb._renderPipelineSection(pipelineData(12));
    const html = elements._boardPipelineSection.innerHTML;
    assert.strictEqual(countEventRows(html), 12, '≤20 条应全部渲染');
    assert.strictEqual(loadMoreOnclick(html), null, '≤20 条不应渲染按钮');
    assert.ok(html.indexOf('(12/12)') < 0, '全量展示时不需要计数标注');
  }

  // 6. _openBoardDetail 进入新详情重置展开量（防跨任务串扰）
  {
    const sb = makeSandbox();
    sb._tasksById = { 7: { id: 7, title: 'T-1102', repo_key: 'gitcode:demo/demo',
      issue_number: 868, status: 'processing', created_at: '2026-11-06T10:00:00Z' } };
    sb._pipeEventsShown = 40;   // 模拟上个任务已展开
    sb._openBoardDetail(7);
    await drain();
    assert.strictEqual(sb._pipeEventsShown, DEFAULT_BATCH,
      '进入新任务详情应重置展开量为默认批大小');
  }

  // 7. index.html 缓存版本号 bump（issues.js 内容已变更，必须 ≥ 133）
  {
    const html = fs.readFileSync(INDEX_HTML, 'utf8');
    const m = html.match(/issues\.js\?v=(\d+)/);
    assert.ok(m, 'index.html 应引用 issues.js?v=N');
    const v = parseInt(m[1], 10);
    assert.ok(v >= 133, `issues.js 版本号应 ≥ 133（当前 v=${v}，改动后未 bump 会导致缓存用户拿不到新 JS）`);
  }

  console.log('✅ Issue #1102 事件历史 load_earlier_events（i18n/不跳转/方向）测试全部通过（7 组断言）');
}

main().then(() => process.exit(0)).catch(e => { console.error('❌', e.message); process.exit(1); });
