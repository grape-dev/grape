/**
 * Issue #539 回归测试：需求详情页流水线卡片——需求级业务状态横幅 + SRE 验收结论块
 *
 * 运行：node tests/js/test_issue_539_review_status_render.js
 *
 * 背景（审查 note 197075321 Critical）：_renderPipelineSection 中 SRE 结论块引用了
 * 本函数作用域内不存在的 esc，导致 pending_review + SRE 结论（result_ref.status=
 * available 或 task_ref.task_id 非空）场景抛 ReferenceError: esc is not defined，
 * 流水线卡片整体不渲染。node --check 仅语法检查无法发现。
 *
 * 验证点（vm 沙箱加载真实 issues.js，mock pipeline 接口数据）：
 *   1. Critical 回归：pending_review + sre_result(available) 不抛 ReferenceError，
 *      卡片完整渲染（事件进度条 + SRE 结论块 + 横幅齐备）
 *   2. sre_result 三态渲染：
 *      a. available + summary → 渲染脱敏摘要（esc 转义后），含验收任务引用行
 *      b. pending → 渲染「SRE 验收任务在途」提示
 *      c. unavailable → 渲染「暂无 SRE 验收结论」提示
 *   3. pending_review 横幅：含待审核标识与提示文案
 *   4. esc 转义生效：summary/task_id 中的 HTML 特殊字符被转义（防注入）
 *   5. 非 pending_review（如 in_progress）不渲染横幅
 *   6. locale 文件含 issues.review_status.* 5 个键（zh/en）
 *   7. index.html 缓存版本号已 bump（issues.js?v=123 > 122）
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const ISSUES_JS = path.join(ROOT, 'src/api/static/js/pages/issues.js');
const INDEX_HTML = path.join(ROOT, 'src/api/static/index.html');
const ZH_ISSUES = path.join(ROOT, 'src/api/static/locales/zh/issues.json');
const EN_ISSUES = path.join(ROOT, 'src/api/static/locales/en/issues.json');

// ── 极简 DOM mock（只覆盖 _renderPipelineSection 依赖面）──
function makeSandbox() {
  const elements = {};
  const ids = ['_boardPipelineSection', '_grapePulseCSS', '_taskListPanel', 'pageContent', 'taskBoard', 'taskList'];
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
    setTimeout, clearTimeout, setInterval, clearInterval,
    document,
    localStorage: { getItem() { return ''; }, setItem() {}, removeItem() {} },
    sessionStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    fetch() { return Promise.resolve({ ok: false }); },
    requestAnimationFrame(fn) { fn(); return 0; },
    navigator: { userAgent: 'node-test' },
    location: { hash: '', href: 'http://localhost/' },
    // I18N mock：返回 key 本身，让断言可预测（渲染时回退文案不影响 key 断言）
    I18N: { t(k) { return k; } },
    renderMarkdown(s) { return String(s || ''); },
    fmtLocalTime(s) { return s || ''; },
    _vcsUrl(repo, p) { return 'https://vcs.example/' + repo + p; },
    _AGENT_TYPES: {},
    API: {
      get() { return Promise.resolve({}); },
      post() { return Promise.resolve({}); },
      put() { return Promise.resolve({}); },
    },
    app: { showToast() {}, nav() {}, currentRepo() { return null; } },
  };
  sandbox.window = sandbox;
  sandbox._currentView = 'tasks';
  sandbox.__test = { elements };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(ISSUES_JS, 'utf8'), sandbox, { filename: 'issues.js' });
  return sandbox;
}

// pipeline 接口 mock 数据（对应 /issues/{id}/pipeline 返回）
function pipelineData(opts) {
  opts = opts || {};
  return {
    scene_name: 'dev-scene',
    nodes: [
      { id: 'vod', name: 'VOD', node_status: 'completed', latest_event: { seq: 901 } },
      { id: 'coding', name: 'Coding', node_status: 'completed', latest_event: { seq: 902 } },
      { id: 'sre', name: 'SRE', node_status: 'completed', latest_event: { seq: 903 } },
    ],
    events: [
      { seq: 903, agent_type: 'sre', event_type: 'a2a_event', status: 'completed', created_at: '2026-10-30T06:00:00Z', title: 'SRE 验收' },
    ],
    review_status: opts.review_status || '',
    sre_result: opts.sre_result || null,
  };
}

function sreAvailable(over) {
  return Object.assign({
    task_ref: { task_id: 'task-539-abc', task_status: 'completed', query_path: '/tasks/1' },
    result_ref: { status: 'available', summary: '结论：验收通过，服务健康' },
  }, over || {});
}

async function main() {
  const drain = () => new Promise(r => setImmediate(r));

  // 1. Critical 回归：pending_review + available 不抛 ReferenceError，卡片完整渲染
  {
    const sb = makeSandbox();
    const { elements } = sb.__test;
    assert.doesNotThrow(() => sb._renderPipelineSection(pipelineData({
      review_status: 'pending_review',
      sre_result: sreAvailable(),
    })), 'pending_review + sre available 不应抛 ReferenceError（esc 未定义）');
    const html = elements._boardPipelineSection.innerHTML;
    assert.ok(html.length > 0, '流水线卡片应渲染出内容');
    assert.ok(html.indexOf('SRE') >= 0, '应包含流水线节点');
    assert.ok(html.indexOf('issues.review_status.sre_result_title') >= 0, '应包含 SRE 结论块标题');
    assert.ok(html.indexOf('结论：验收通过，服务健康') >= 0, '应包含 SRE 验收摘要');
    assert.ok(html.indexOf('task-539-abc') >= 0, '应包含验收任务引用');
  }

  // 2a. available 态：摘要 + 任务引用（含 task_status 括号）
  {
    const sb = makeSandbox();
    const { elements } = sb.__test;
    sb._renderPipelineSection(pipelineData({ sre_result: sreAvailable() }));
    const html = elements._boardPipelineSection.innerHTML;
    assert.ok(html.indexOf('issues.review_status.sre_result_title') >= 0, 'available：应有 SRE 结论标题');
    assert.ok(html.indexOf('结论：验收通过，服务健康') >= 0, 'available：应渲染摘要');
    assert.ok(html.indexOf('task-539-abc') >= 0 && html.indexOf('(completed)') >= 0, 'available：应渲染任务引用 + 状态');
  }

  // 2b. pending 态
  {
    const sb = makeSandbox();
    const { elements } = sb.__test;
    sb._renderPipelineSection(pipelineData({
      sre_result: {
        task_ref: { task_id: 'task-539-run', task_status: 'running', query_path: '/tasks/2' },
        result_ref: { status: 'pending' },
      },
    }));
    const html = elements._boardPipelineSection.innerHTML;
    assert.ok(html.indexOf('issues.review_status.sre_pending') >= 0, 'pending：应渲染「SRE 验收任务在途」提示');
    assert.ok(html.indexOf('task-539-run') >= 0, 'pending：仍应渲染在途任务引用');
    assert.ok(html.indexOf('sre_result_title') >= 0, 'pending：结论块标题存在');
  }

  // 2c. unavailable 态（无 task_id / result_ref.status=unavailable）
  {
    const sb = makeSandbox();
    const { elements } = sb.__test;
    sb._renderPipelineSection(pipelineData({
      sre_result: {
        task_ref: { task_id: null, task_status: 'unavailable', query_path: '' },
        result_ref: { status: 'unavailable' },
      },
    }));
    const html = elements._boardPipelineSection.innerHTML;
    assert.ok(html.indexOf('issues.review_status.sre_unavailable') >= 0, 'unavailable：应渲染「暂无 SRE 验收结论」提示');
    assert.ok(html.indexOf('undefined') < 0, 'unavailable：不应渲染 undefined 字样');
  }

  // 3. pending_review 横幅渲染
  {
    const sb = makeSandbox();
    const { elements } = sb.__test;
    sb._renderPipelineSection(pipelineData({ review_status: 'pending_review', sre_result: sreAvailable() }));
    const html = elements._boardPipelineSection.innerHTML;
    assert.ok(html.indexOf('issues.review_status.pending_review_hint') >= 0, 'pending_review：应渲染横幅提示文案');
    assert.ok(html.indexOf('tasks.status.tag_pending_review') >= 0, 'pending_review：横幅应含待审核标识');
  }

  // 4. esc 转义生效（防注入：HTML 特殊字符转义，标签不落 DOM）
  {
    const sb = makeSandbox();
    const { elements } = sb.__test;
    sb._renderPipelineSection(pipelineData({
      sre_result: sreAvailable({
        task_ref: { task_id: '<img src=x onerror=alert(1)>', task_status: 'completed' },
        result_ref: { status: 'available', summary: '<script>alert("xss")</script> & <b>bold</b>' },
      }),
    }));
    const html = elements._boardPipelineSection.innerHTML;
    assert.ok(html.indexOf('&lt;img src=x onerror=alert(1)&gt;') >= 0, 'esc：task_id 中 HTML 应被转义');
    assert.ok(html.indexOf('&lt;script&gt;') >= 0, 'esc：summary 中 script 标签应被转义');
    assert.ok(html.indexOf('<script>alert') < 0, 'esc：原始 script 标签不得落 DOM');
    assert.ok(html.indexOf('&amp;') >= 0, 'esc：& 应转义为 &amp;');
  }

  // 5. 非 pending_review 不渲染横幅
  {
    const sb = makeSandbox();
    const { elements } = sb.__test;
    sb._renderPipelineSection(pipelineData({ review_status: '', sre_result: sreAvailable() }));
    const html = elements._boardPipelineSection.innerHTML;
    assert.ok(html.indexOf('issues.review_status.pending_review_hint') < 0, '非 pending_review：不应渲染横幅');
    assert.ok(html.indexOf('issues.review_status.sre_result_title') >= 0, 'sre_result 存在时结论块仍渲染');
  }

  // 6. locale 文件含 issues.review_status.* 全部 5 键（zh/en）
  {
    const required = ['pending_review_hint', 'sre_result_title', 'sre_pending', 'sre_unavailable', 'sre_task_ref'];
    for (const f of [ZH_ISSUES, EN_ISSUES]) {
      const pack = JSON.parse(fs.readFileSync(f, 'utf8'));
      assert.ok(pack.review_status && typeof pack.review_status === 'object',
        `${path.basename(path.dirname(f))}/issues.json 应含 review_status 分组`);
      for (const k of required) {
        assert.ok(typeof pack.review_status[k] === 'string' && pack.review_status[k].length > 0,
          `review_status.${k} 应为非空字符串（${f}）`);
      }
    }
  }

  // 7. index.html 缓存版本号 bump（issues.js 内容已变更，必须 > 122）
  {
    const html = fs.readFileSync(INDEX_HTML, 'utf8');
    const m = html.match(/issues\.js\?v=(\d+)/);
    assert.ok(m, 'index.html 应引用 issues.js?v=N');
    const v = parseInt(m[1], 10);
    assert.ok(v >= 123, `issues.js 版本号应 ≥ 123（当前 v=${v}，#539 改动后未 bump 会导致缓存用户拿不到新 JS）`);
  }

  console.log('✅ Issue #539 流水线卡片 review_status/SRE 结论渲染测试全部通过（7 组断言）');
}

main().then(() => process.exit(0)).catch(e => { console.error('❌', e.message); process.exit(1); });
