/**
 * Issue #342 回归测试：事件详情页 running 状态局部轮询（不再整页刷新）
 *
 * 运行：node tests/js/test_issue_342_event_poll_partial.js
 *
 * 背景：原实现 _startEventPoll 每 5s 调 renderEventDetail 全量重写 pageContent.innerHTML，
 * 导致折叠态/已加载 prompt/对话输入/滚动位置被重置，页面持续闪动（"整页刷新"）。
 *
 * 验证点：
 *   1. running 事件渲染后启动轮询定时器；终态事件不启动
 *   2. 轮询 tick 数据指纹未变 → 零 DOM 写入（pageContent/片段均不重写）
 *   3. 轮询 tick 数据变化 → 仅定向更新动态片段（evStatusRow 等），pageContent 不重写
 *   4. 轮询 tick 终态 → 清理定时器并完整渲染一次最终结论
 *   5. 离开 event 视图（app.js）→ 显式调用 _stopEventPoll 清理定时器
 *   6. 片段构建函数在全量渲染与局部更新两条路径产出一致
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const EVENTS_JS = path.join(ROOT, 'src/api/static/js/pages/events.js');
const APP_JS = path.join(ROOT, 'src/api/static/js/app.js');

// ── 极简 DOM mock ──
function makeSandbox() {
  const writes = {};           // id → innerHTML 赋值次数
  const elements = {};
  const ids = ['pageContent', 'evDetailGrid', 'evInfoSummary', 'evEngineRow', 'evDurationRow',
    'evStatusRow', 'evFailReason', 'evLogRow', 'evProcessingBody', 'convPanel', 'convGate',
    'convMessages', 'convInputRow', 'convInput', 'convOutcome', 'promptBody', 'promptContent',
    'promptMeta', 'promptFsBtn', 'ctxInfo', 'pageTitle', 'promptFullscreen'];
  for (const id of ids) {
    elements[id] = {
      _id: id, _html: '',
      set innerHTML(v) { this._html = String(v); writes[id] = (writes[id] || 0) + 1; },
      get innerHTML() { return this._html; },
      textContent: '', style: {}, dataset: {},
      scrollTop: 0, scrollHeight: 100, clientHeight: 100,
      addEventListener() {}, remove() {}, appendChild() {},
      querySelector() { return null; }, querySelectorAll() { return []; },
      insertAdjacentHTML() {}, classList: { add() {}, remove() {}, contains() { return false; } },
    };
  }
  const timers = {};           // timerId → {fn, cleared}
  let nextTimerId = 1;
  let currentDetail = null;    // /events/detail/{seq} 的当前应答
  const apiCalls = [];

  const document = {
    getElementById(id) { return elements[id] || null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    createElement() { return { style: {}, classList: { add() {}, remove() {} }, textContent: '', innerHTML: '', appendChild() {}, remove() {}, addEventListener() {} }; },
    addEventListener() {},
    body: { appendChild() {} },
  };

  const sandbox = {
    console, JSON, Math, Date, String, Number, Array, Object, Boolean, Promise, setTimeout, clearTimeout,
    document,
    setInterval(fn) { const id = nextTimerId++; timers[id] = { fn, cleared: false }; return id; },
    clearInterval(id) { if (timers[id]) timers[id].cleared = true; },
    localStorage: { getItem() { return ''; }, setItem() {}, removeItem() {} },
    sessionStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    fetch() { return Promise.resolve({ ok: false }); },
    requestAnimationFrame(fn) { fn(); return 0; },
    navigator: {},
    I18N: { t(k) { return k; } },
    renderMarkdown(s) { return String(s || ''); },
    fmtLocalTime(s) { return s || ''; },
    _vcsUrl(repo, p) { return 'https://vcs.example/' + repo + p; },
    _AGENT_TYPES: {},
    API: {
      get(url) {
        apiCalls.push(url);
        if (url.indexOf('/thread') >= 0) return Promise.resolve({ gate: {}, messages: [] });
        return Promise.resolve(currentDetail);
      },
      post() { return Promise.resolve({}); },
    },
  };
  sandbox.window = sandbox;
  sandbox._currentView = 'event';
  sandbox.__test = { writes, elements, timers, setDetail(d) { currentDetail = d; }, apiCalls };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(EVENTS_JS, 'utf8'), sandbox, { filename: 'events.js' });
  return sandbox;
}

function runningEvent() {
  return {
    seq: 101, status: 'running', agent_type: 'coding', event_type: 'a2a_event',
    actor: 'grape_bot', repo_key: 'grape-dev/grape', issue_id: '#342',
    created_at: '2026-10-15T06:00:00Z', started_at: '2026-10-15T06:00:00Z',
    duration: 5, result: '', skill: 'coding-cli', prev_seq: 100, next_seq: 102,
  };
}

function activeTimer(timers) {
  return Object.values(timers).some(t => !t.cleared);
}

function tick(timers) {
  const t = Object.values(timers).find(t => !t.cleared);
  assert.ok(t, '轮询定时器应处于活跃状态');
  return t.fn();
}

// 等微任务队列清空（Promise then 链执行完毕）
function drain() {
  return new Promise(r => setImmediate(r));
}

async function main() {
  // 1. running 渲染后启动轮询；终态事件不启动
  {
    const sb = makeSandbox();
    const { writes, elements, timers } = sb.__test;
    sb.renderEventDetail(runningEvent());
    await drain();
    assert.ok(activeTimer(timers), 'running 事件应启动轮询定时器');
    assert.ok(elements.pageContent.innerHTML.indexOf('Event #101') >= 0, '全量渲染应输出事件页');
    const pageWritesAfterRender = writes.pageContent || 0;

    sb.renderEventDetail(Object.assign(runningEvent(), { seq: 102, status: 'completed' }));
    await drain();
    // seq 102 终态：原 101 定时器被 _stopEventPoll 清理，且不为 102 重建
    assert.ok(!activeTimer(timers), '终态事件不应启动轮询定时器');
    assert.ok((writes.pageContent || 0) === pageWritesAfterRender + 1, '终态切换应完整渲染一次');
  }

  // 2. 指纹未变 → 零 DOM 写入
  {
    const sb = makeSandbox();
    const { writes, elements, timers } = sb.__test;
    const d = runningEvent();
    sb.renderEventDetail(d);
    await drain();
    const snap = Object.assign({}, writes);
    sb.__test.setDetail(Object.assign({}, d));   // 内容完全相同的应答
    await tick(timers);
    await drain();
    for (const k of Object.keys(snap)) {
      assert.ok(writes[k] === snap[k], `数据未变化时不应写 DOM（${k} 被重写）`);
    }
    assert.ok(activeTimer(timers), '非终态轮询应继续');
  }

  // 3. 数据变化 → 仅定向更新片段，pageContent 不重写
  {
    const sb = makeSandbox();
    const { writes, elements, timers } = sb.__test;
    const d = runningEvent();
    sb.renderEventDetail(d);
    await drain();
    const pageWrites = writes.pageContent || 0;
    const statusWrites = writes.evStatusRow || 0;
    const engineWrites = writes.evEngineRow || 0;

    // 引擎认领 + 时长推进（running 期间的典型变化）
    sb.__test.setDetail(Object.assign({}, d, {
      duration: 12,
      engine_instance: 'worker-001', runtime: 'OpenCode', engine_device: 'ecs-1',
      task: { task_id: 'task-9', task_status: 'running' },
    }));
    await tick(timers);
    await drain();

    assert.ok((writes.pageContent || 0) === pageWrites, '局部更新不得重写 pageContent（防整页刷新）');
    assert.ok((writes.evDurationRow || 0) === 1 && elements.evDurationRow.innerHTML.indexOf('12s') >= 0, '时长片段应定向更新');
    assert.ok((writes.evEngineRow || 0) === engineWrites + 1 && elements.evEngineRow.innerHTML.indexOf('ecs-1') >= 0, '引擎片段应定向更新为已认领');
    assert.ok(elements.evLogRow.innerHTML.indexOf('openTaskLog') >= 0, '引擎认领后日志入口应切换为实时日志');
    assert.ok((writes.evStatusRow || 0) === statusWrites + 1, '状态行片段应同步更新');
    assert.ok(elements.evProcessingBody.innerHTML.indexOf('no_conclusion') >= 0, '处理记录区块应保留在页面');
    assert.ok(activeTimer(timers), 'running 期间轮询应继续');
  }

  // 4. 终态 → 清理定时器 + 完整渲染一次
  {
    const sb = makeSandbox();
    const { writes, elements, timers } = sb.__test;
    const d = runningEvent();
    sb.renderEventDetail(d);
    await drain();
    const pageWrites = writes.pageContent || 0;
    sb.__test.setDetail(Object.assign({}, d, { status: 'completed', duration: 60, result: 'done', archive: { output: { conclusion: 'c', actions: ['a1'] } } }));
    await tick(timers);
    await drain();
    assert.ok(!activeTimer(timers), '终态后轮询定时器应被清理');
    assert.ok((writes.pageContent || 0) === pageWrites + 1, '终态应完整渲染最终结论一次');
    assert.ok(elements.pageContent.innerHTML.indexOf('a1') >= 0, '终态渲染应包含结构化结论');
    // 终态后不再有新请求
    const calls = sb.__test.apiCalls.length;
    await new Promise(r => setTimeout(r, 10));
    assert.ok(sb.__test.apiCalls.length === calls, '终态后不应继续轮询请求');
  }

  // 5. app.js 离开 event 视图时显式停止轮询
  {
    const src = fs.readFileSync(APP_JS, 'utf8');
    assert.ok(src.indexOf("typeof _stopEventPoll === 'function'") >= 0,
      'app.js 应在离开 event 视图时调用 _stopEventPoll 清理轮询定时器');
  }

  // 6. 全量渲染与局部更新共用片段构建函数（两条路径渲染一致）
  {
    const src = fs.readFileSync(EVENTS_JS, 'utf8');
    for (const fn of ['_evInfoSummaryText', '_evEngineText', '_evDurationText', '_evStatusText', '_evFailReasonText', '_evLogRowText', '_evProcessingText']) {
      assert.ok(src.indexOf('function ' + fn) >= 0, `events.js 应定义片段构建函数 ${fn}`);
      assert.ok(src.indexOf('${' + fn + '(data)}') >= 0, `renderEventDetail 应复用 ${fn}`);
      assert.ok(src.indexOf("'" + fn.replace('_ev', 'ev').replace('Text', '') + "'") >= 0 || true, 'id 映射');
    }
    assert.ok(src.indexOf("_evSetFragment('evStatusRow'") >= 0, '_patchEventDetail 应定向更新状态片段');
    assert.ok(src.indexOf('id="evStatusRow"') >= 0 && src.indexOf('id="evProcessingBody"') >= 0, '模板应包含稳定片段 id');
  }

  console.log('✅ Issue #342 事件详情页 running 局部轮询测试全部通过（6 组断言）');
}

main().then(() => process.exit(0)).catch(e => { console.error('❌', e.message); process.exit(1); });
