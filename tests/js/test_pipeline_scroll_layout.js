/**
 * 流水线页面问题列表模块滚动条布局测试 — Issue #221
 *
 * 零依赖：仅用 Node 内置模块（node:test / node:assert / node:fs / node:vm）。
 * 运行方式：node tests/frontend/test_pipeline_scroll_layout.js
 *
 * 覆盖验收标准：
 *   1) .pl-table-card 声明 display:flex;flex-direction:column（使其成为 flex 列容器）
 *   2) .pl-table-card 保留 flex:1;overflow:hidden;min-height:0（既有约束不被破坏）
 *   3) .pl-table-wrap 保留 overflow-y:auto;min-height:0（滚动区域约束）
 *   4) renderPipeline 输出的 HTML 结构仍包含 pl-table-card > pl-table-wrap > pl-table，
 *      且流程图卡片 / 搜索栏 / 分页控件渲染不受影响
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..', '..');
const PIPELINE_SRC = fs.readFileSync(
  path.join(ROOT, 'src', 'api', 'static', 'js', 'pages', 'pipeline.js'),
  'utf8'
);

/** 从源码中提取指定 CSS 规则的声明文本（花括号内） */
function extractRule(src, selector) {
  const re = new RegExp(selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\{([^}]*)\\}');
  const m = src.match(re);
  return m ? m[1] : null;
}

/** 将 CSS 声明文本解析为 { prop: value } map */
function parseDeclarations(text) {
  const map = {};
  if (!text) return map;
  for (const part of text.split(';')) {
    const idx = part.indexOf(':');
    if (idx === -1) continue;
    const prop = part.slice(0, idx).trim();
    const val = part.slice(idx + 1).trim();
    if (prop) map[prop] = val;
  }
  return map;
}

test('.pl-table-card 声明 display:flex;flex-direction:column（flex 列容器）', () => {
  const decls = parseDeclarations(extractRule(PIPELINE_SRC, '.pl-table-card'));
  assert.ok(decls.display, '.pl-table-card 必须声明 display');
  assert.strictEqual(decls.display, 'flex', '.pl-table-card display 应为 flex');
  assert.ok(decls['flex-direction'], '.pl-table-card 必须声明 flex-direction');
  assert.strictEqual(decls['flex-direction'], 'column', '.pl-table-card flex-direction 应为 column');
});

test('.pl-table-card 保留既有约束 flex:1;overflow:hidden;min-height:0', () => {
  const decls = parseDeclarations(extractRule(PIPELINE_SRC, '.pl-table-card'));
  assert.strictEqual(decls.flex, '1', 'flex:1 保留');
  assert.strictEqual(decls.overflow, 'hidden', 'overflow:hidden 保留');
  assert.strictEqual(decls['min-height'], '0', 'min-height:0 保留');
});

test('.pl-table-wrap 保留 overflow-y:auto;min-height:0（滚动区域约束）', () => {
  const decls = parseDeclarations(extractRule(PIPELINE_SRC, '.pl-table-wrap'));
  assert.strictEqual(decls['overflow-y'], 'auto', 'overflow-y:auto 保留');
  assert.strictEqual(decls['min-height'], '0', 'min-height:0 保留');
});

test('.pl-table-wrap 在 flex 列父容器下声明 flex:1（占据剩余高度）', () => {
  const decls = parseDeclarations(extractRule(PIPELINE_SRC, '.pl-table-wrap'));
  assert.strictEqual(decls.flex, '1', '.pl-table-wrap flex:1 在父 flex 容器下生效');
});

test('renderPipeline：DOM 结构 pl-table-card > pl-table-wrap > pl-table 完整', () => {
  const pageContent = { innerHTML: '' };
  const sandbox = {
    I18N: { t: (k) => k },
    document: {
      getElementById: (id) => (id === 'pageContent' ? pageContent : { innerHTML: '' }),
      querySelectorAll: () => [],
      querySelector: () => null,
    },
    app: { nav: () => {} },
    API: { get: () => Promise.resolve({ events: [] }), post: () => Promise.resolve({}) },
    confirm: () => true,
    alert: () => {},
    _vcsUrl: (repoKey, p) => (repoKey || '') + (p || ''),
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(PIPELINE_SRC + '\n;(function(){const data={active:null,history:[{issue_id:"#101",title:"修复滚动条",stage:"coding",current_owner:"张三",parent_issue_id:""}],repo:"gitcode:o/r",active_count:0,today_count:0,failed_count:0,pipeline_config:null};_pipeQuery="";_pipePage=1;_pipeActiveIdx=0;renderPipeline(data);})();', sandbox, { filename: 'pipeline.js + harness' });
  const html = pageContent.innerHTML;
  const cardIdx = html.indexOf('class="pl-table-card"');
  const wrapIdx = html.indexOf('class="pl-table-wrap"');
  const tableIdx = html.indexOf('class="pl-table"');
  assert.notStrictEqual(cardIdx, -1, 'pl-table-card 渲染存在');
  assert.notStrictEqual(wrapIdx, -1, 'pl-table-wrap 渲染存在');
  assert.notStrictEqual(tableIdx, -1, 'pl-table 渲染存在');
  assert.ok(cardIdx < wrapIdx, 'pl-table-wrap 位于 pl-table-card 内部');
  assert.ok(wrapIdx < tableIdx, 'pl-table 位于 pl-table-wrap 内部');
});

test('renderPipeline：流程图卡片 / 搜索栏 / 分页控件渲染不受影响', () => {
  const pageContent = { innerHTML: '' };
  const sandbox = {
    I18N: { t: (k) => k },
    document: {
      getElementById: (id) => (id === 'pageContent' ? pageContent : { innerHTML: '' }),
      querySelectorAll: () => [],
      querySelector: () => null,
    },
    app: { nav: () => {} },
    API: { get: () => Promise.resolve({ events: [] }), post: () => Promise.resolve({}) },
    confirm: () => true,
    alert: () => {},
    _vcsUrl: (repoKey, p) => (repoKey || '') + (p || ''),
    console,
  };
  vm.createContext(sandbox);
  // 25 条记录，触发分页控件渲染
  const history = [];
  for (let i = 1; i <= 25; i++) {
    history.push({ issue_id: '#' + i, title: 'issue ' + i, stage: 'coding', current_owner: '张三', parent_issue_id: '' });
  }
  vm.runInContext(PIPELINE_SRC + '\n;(function(){var history=' + JSON.stringify(history) + ';var data={active:null,history:history,repo:"gitcode:o/r",active_count:0,today_count:0,failed_count:0,pipeline_config:null};_pipeQuery="";_pipePage=1;_pipeActiveIdx=0;renderPipeline(data);})();', sandbox, { filename: 'pipeline.js + harness' });
  const html = pageContent.innerHTML;
  assert.notStrictEqual(html.indexOf('pl-flow-card'), -1, '流程图卡片渲染存在');
  assert.notStrictEqual(html.indexOf('pl-search-bar'), -1, '搜索栏渲染存在');
  assert.notStrictEqual(html.indexOf('pl-pager'), -1, '分页控件渲染存在');
  assert.notStrictEqual(html.indexOf('id="pipeSearchInput"'), -1, '搜索输入框渲染存在');
});
