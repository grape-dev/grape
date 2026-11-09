/*
 * Issue #648 回归测试：issue 详情页附件区渲染（内联 attachments 优先 + 端点回退）
 *
 * 运行：node tests/js/test_issue_648_attachments_inline.js
 *
 * 验证点：
 *   1. 卡片自带 attachments 数组（详情/统一列表 API 新增字段）→ 直接渲染，不发 API 请求
 *   2. 图片附件渲染缩略图 <img>，非图片渲染 📎 + 下载链接，均含文件名/大小/删除按钮
 *   3. 空数组 → 「暂无附件」
 *   4. 无内联字段（旧列表）→ 回退 GET /issues/{id}/attachments
 *   5. forceRefresh=true → 强制走端点刷新（上传/删除后）
 *   6. 端点失败 → 「加载失败」
 *
 * 策略：从 issues.js 源码提取 _renderTaskAttachments / _loadTaskAttachments / _formatSize
 * 函数体，在 mock 沙箱执行。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const issuesJsPath = path.resolve(__dirname, '../../src/api/static/js/pages/issues.js');
const src = fs.readFileSync(issuesJsPath, 'utf8');

function extractFn(name) {
  const re = new RegExp('function ' + name + '\\([^)]*\\)\\s*\\{([\\s\\S]*?)\\n\\}(?:\\s*\\n)');
  const m = src.match(re);
  if (!m) {
    console.error('✗ 无法从 issues.js 提取 ' + name + ' 函数体');
    process.exit(1);
  }
  return m[1];
}

const renderBody = extractFn('_renderTaskAttachments');
const loadBody = extractFn('_loadTaskAttachments');
const sizeBody = extractFn('_formatSize');

function makeEl(id) {
  return { _id: id, innerHTML: '', textContent: '', style: {} };
}

function makeSandbox() {
  const apiCalls = [];
  const elements = { _taskAttachmentsList: makeEl('_taskAttachmentsList') };
  const document = {
    getElementById: (id) => elements[id] || null,
  };
  const API = {
    get: (url) => { apiCalls.push(['get', url]); return Promise.resolve(sandbox._nextResp || { attachments: [] }); },
  };
  const sandbox = {
    apiCalls,
    elements,
    document,
    API,
    window: { _currentDetailTask: null },
    _nextResp: null,
    _renderTaskAttachments: null,
    _loadTaskAttachments: null,
    _formatSize: null,
  };
  // 相互引用：函数体里调用全局 _renderTaskAttachments / _formatSize
  const ctx = {
    document,
    API,
    window: sandbox.window,
    _nextResp: undefined,
    _renderTaskAttachments: null,
    _loadTaskAttachments: null,
    _formatSize: null,
  };
  // 用 eval 在同一作用域内定义三个函数（共享全局名）
  const code = `
    function _formatSize(bytes) { ${sizeBody} }
    function _renderTaskAttachments(taskId, atts) { ${renderBody} }
    function _loadTaskAttachments(taskId, forceRefresh) { ${loadBody} }
    sandbox._formatSize = _formatSize;
    sandbox._renderTaskAttachments = _renderTaskAttachments;
    sandbox._loadTaskAttachments = _loadTaskAttachments;
  `;
  const factory = new Function('sandbox', 'document', 'API', 'window', code);
  factory(sandbox, document, API, sandbox.window);
  return sandbox;
}

// ── 1. 内联字段优先，不发请求 ──
{
  const s = makeSandbox();
  s.window._currentDetailTask = { id: 11, attachments: [
    { filename: 'shot.png', url: 'http://u/shot.png', size: 2048, is_image: true },
    { filename: 'doc.md', url: 'http://u/doc.md', size: 512, is_image: false },
  ] };
  s._loadTaskAttachments(11);
  assert.strictEqual(s.apiCalls.length, 0, '内联渲染不应发 API 请求');
  const html = s.elements._taskAttachmentsList.innerHTML;
  assert.ok(html.includes('<img src="http://u/shot.png"'), '图片附件应有缩略图 img');
  assert.ok(html.includes('>shot.png<'), '图片附件应含文件名链接');
  assert.ok(html.includes('📎'), '非图片附件应有文件图标');
  assert.ok(html.includes('>doc.md<'), '非图片附件应含文件名链接');
  assert.ok(html.includes('2.0KB') && html.includes('512B'), '应含格式化大小');
  assert.ok(html.includes('_deleteTaskAttach(11'), '应含删除按钮');
  console.log('✓ 1. 内联 attachments 直接渲染（图片预览/下载/大小/删除）');
}

// ── 2. 空数组 → 暂无附件 ──
{
  const s = makeSandbox();
  s.window._currentDetailTask = { id: 12, attachments: [] };
  s._loadTaskAttachments(12);
  assert.strictEqual(s.apiCalls.length, 0);
  assert.ok(s.elements._taskAttachmentsList.innerHTML.includes('暂无附件'));
  console.log('✓ 2. 空数组 → 暂无附件');
}

// ── 3. 无内联字段 → 回退端点 ──
{
  const s = makeSandbox();
  s.window._currentDetailTask = { id: 13 };  // 旧列表卡片：无 attachments 字段
  s._nextResp = { attachments: [{ filename: 'a.md', url: 'http://u/a.md', size: 10, is_image: false }] };
  s._loadTaskAttachments(13);
  assert.deepStrictEqual(s.apiCalls, [['get', '/issues/13/attachments']]);
  // promise 微任务后渲染
  Promise.resolve().then(() => Promise.resolve()).then(() => {
    assert.ok(s.elements._taskAttachmentsList.innerHTML.includes('>a.md<'), '端点数据应被渲染');
    console.log('✓ 3. 无内联字段 → 回退 GET /issues/{id}/attachments');

    // ── 4. forceRefresh 强制走端点（即便有内联字段）──
    const s2 = makeSandbox();
    s2.window._currentDetailTask = { id: 14, attachments: [{ filename: 'old.png', url: 'http://u/old.png', size: 1, is_image: true }] };
    s2._nextResp = { attachments: [{ filename: 'new.md', url: 'http://u/new.md', size: 2, is_image: false }] };
    s2._loadTaskAttachments(14, true);
    assert.deepStrictEqual(s2.apiCalls, [['get', '/issues/14/attachments']]);
    Promise.resolve().then(() => Promise.resolve()).then(() => {
      assert.ok(!s2.elements._taskAttachmentsList.innerHTML.includes('old.png'), '刷新后应替换内联旧数据');
      assert.ok(s2.elements._taskAttachmentsList.innerHTML.includes('>new.md<'));

      // ── 5. 端点失败 → 加载失败 ──
      const s3 = makeSandbox();
      s3.window._currentDetailTask = null;
      s3.API.get = () => Promise.reject(new Error('net'));
      s3._loadTaskAttachments(15);
      Promise.resolve().then(() => Promise.resolve()).then(() => {
        assert.ok(s3.elements._taskAttachmentsList.innerHTML.includes('加载失败'));
        console.log('✓ 4. forceRefresh 强制端点刷新');
        console.log('✓ 5. 端点失败 → 加载失败');
        console.log('\n全部通过：issue 详情页附件区渲染（Issue #648）');
      });
    });
  });
}
