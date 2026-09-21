/*
 * Issue #202 回归测试：需求中心新建需求不再强制检查标题重复性
 *
 * 运行：node tests/js/test_issue_202_dup_title_not_blocked.js
 *
 * 验证点：
 *   1. createTaskSubmit 在 _tasksAll 已含同名任务时，不阻断提交（不 showErr、不 return）
 *   2. createTaskSubmit 正常调用 API.post 提交表单
 *   3. _tasksAll 为空时正常提交（基线不回归）
 *   4. 标题为空时仍保留必填校验（只移除重复检查，不移除必填）
 *
 * 策略：从 issues.js 源码中提取 createTaskSubmit 函数体，在 mock 沙箱中执行，
 * 避免加载整个文件对 app/fmtLocalTime 等全局的依赖。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const issuesJsPath = path.resolve(__dirname, '../../src/api/static/js/pages/issues.js');
const src = fs.readFileSync(issuesJsPath, 'utf8');

// ── 从源码中提取 createTaskSubmit 函数定义 ──
// 匹配 function createTaskSubmit() { ... } 直到下一个顶层 function 定义
const fnMatch = src.match(/function createTaskSubmit\(\)\s*\{([\s\S]*?)\n\}\n\n(?:function |\/\/)/);
if (!fnMatch) {
  console.error('✗ 无法从 issues.js 提取 createTaskSubmit 函数体');
  process.exit(1);
}
const fnBody = fnMatch[1];

// ── 极简 DOM mock ──
function makeDom() {
  const elements = {};
  function el(id) {
    return {
      _id: id,
      style: { display: '' },
      textContent: '',
      value: '',
      innerHTML: '',
      disabled: false,
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener: () => {},
      remove() {},
    };
  }
  ['tcCreateTitle','tcCreateDesc','tcCreateScene','tcError','tcSubmitBtn','taskCreateOverlay'].forEach(id => { elements[id] = el(id); });
  const dom = {
    getElementById(id) { return elements[id] || null; },
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => el('tmp'),
    addEventListener: () => {},
  };
  return { dom, elements };
}

// ── 构建可执行函数 ──
// createTaskSubmit 引用的全局：document, app, I18N, API, _tasksAll, _createAttachments, loadTasks, encodeURIComponent
function makeCreateTaskSubmit(dom, tasksAll, apiPostResult) {
  let _posted = false;
  let _postUrl = null;
  let _postBody = null;
  let _lastErrKey = null;

  const app = {
    currentRepo: () => ({ repoKey: 'gitcode:owner/repo', fullName: 'owner/repo', owner: 'owner', name: 'repo' }),
    showToast: () => {},
  };
  const I18N = {
    t: (k, params) => {
      if (typeof k === 'string' && k.indexOf('err_dup') >= 0) _lastErrKey = k;
      return k;
    },
    getLang: () => 'zh',
  };
  const API = {
    get: () => Promise.resolve({ issues: tasksAll, tasks: tasksAll }),
    post: (url, body) => { _posted = true; _postUrl = url; _postBody = body; return Promise.resolve(apiPostResult || { message: 'ok' }); },
    put: () => Promise.resolve({}),
  };

  // 构建函数：把 fnBody 包成一个参数化函数
  // createTaskSubmit 内部用的标识符都从闭包传入
  const wrapped = new Function('document', 'app', 'I18N', 'API', '_tasksAll', '_createAttachments', 'loadTasks', 'encodeURIComponent',
    fnBody
    + '\n;');

  function createTaskSubmit() {
    wrapped(dom, app, I18N, API, tasksAll, [], () => {}, encodeURIComponent);
  }

  return {
    createTaskSubmit,
    _wasPosted: () => _posted,
    _getPostBody: () => _postBody,
    _getPostUrl: () => _postUrl,
    _lastErrKey: () => _lastErrKey,
  };
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch(e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

console.log('Issue #202: 需求中心新建需求不强制检查标题重复性 — 回归测试\n');

// ─── 测试 1：_tasksAll 已含同名任务时，createTaskSubmit 不阻断、正常提交 ───
test('createTaskSubmit: _tasksAll 已含同名任务 → 不阻断，调用 API.post', () => {
  const tasksAll = [
    { id: 1, issue_number: 100, title: '重复标题', assignee: 'u', status: 'open' },
  ];
  const { dom, elements } = makeDom();
  elements.tcCreateTitle.value = '重复标题';
  elements.tcCreateDesc.value = '描述内容';
  elements.tcCreateScene.value = '';
  const ctx = makeCreateTaskSubmit(dom, tasksAll, { message: 'created #101' });
  ctx.createTaskSubmit();
  // 核心断言：不因标题重复而阻断 → API.post 被调用
  assert.ok(ctx._wasPosted(), '同名需求应被提交（API.post 被调用），不应被本地查重阻断');
  // 不应触发 err_dup_title
  assert.ok(ctx._lastErrKey() === null, '不应触发 err_dup_title 提示，实际触发了: ' + ctx._lastErrKey());
});

// ─── 测试 2：_tasksAll 为空时正常提交（基线不回归）───
test('createTaskSubmit: _tasksAll 为空 → 正常提交（基线不回归）', () => {
  const { dom, elements } = makeDom();
  elements.tcCreateTitle.value = '全新标题';
  elements.tcCreateDesc.value = '描述';
  elements.tcCreateScene.value = '';
  const ctx = makeCreateTaskSubmit(dom, [], { message: 'created' });
  ctx.createTaskSubmit();
  assert.ok(ctx._wasPosted(), '无同名时也应正常提交');
});

// ─── 测试 3：标题为空时仍应被必填校验阻断（保留必填，只移除重复检查）───
test('createTaskSubmit: 标题为空 → 仍被必填校验阻断（保留 err_no_title）', () => {
  const { dom, elements } = makeDom();
  elements.tcCreateTitle.value = '';
  elements.tcCreateDesc.value = '描述';
  elements.tcCreateScene.value = '';
  const ctx = makeCreateTaskSubmit(dom, [], { message: 'created' });
  ctx.createTaskSubmit();
  assert.ok(!ctx._wasPosted(), '标题为空时不应提交（必填校验保留）');
});

// ─── 测试 4：描述为空时仍被必填校验阻断（保留 err_no_desc）───
test('createTaskSubmit: 描述为空 → 仍被必填校验阻断（保留 err_no_desc）', () => {
  const { dom, elements } = makeDom();
  elements.tcCreateTitle.value = '有标题';
  elements.tcCreateDesc.value = '';
  elements.tcCreateScene.value = '';
  const ctx = makeCreateTaskSubmit(dom, [], { message: 'created' });
  ctx.createTaskSubmit();
  assert.ok(!ctx._wasPosted(), '描述为空时不应提交（必填校验保留）');
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
