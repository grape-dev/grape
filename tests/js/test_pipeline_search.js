/**
 * 轻量前端测试 — 流水线页面搜索功能（Issue #114 / PR #115）
 *
 * 零依赖：仅用 Node 内置模块（node:test / node:assert / node:vm / node:fs）。
 * 运行方式：node tests/frontend/test_pipeline_search.js
 *
 * 覆盖 QA 审查唯一 Required 项（变更验证）：
 *   1) _filteredPipes 客户端过滤逻辑（空查询 / 全字段 / 大小写 / trim / 无匹配）
 *   2) 过滤后 realIdx = allPipes.indexOf(p) 索引映射（点击/双击仍定位原始记录）
 *   3) renderPipeline 搜索框渲染 + 过滤后分页基于 filteredPipes 计算 + 分页重置
 *   4) i18n 中英文 search_placeholder 键完整性
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

/**
 * 在生产 pipeline.js 同一 vm 上下文中注入测试 harness，使断言能直接访问
 * 顶层 let/function（_pipeQuery / _filteredPipes / renderPipeline 等）。
 * 返回失败信息数组（空数组 = 全部通过）。
 */
function runPipelineHarness(harnessBody) {
  const pageContent = { innerHTML: '' };
  const sandbox = {
    I18N: { t: (k) => k }, // 返回 key 本身，便于断言
    document: {
      getElementById: (id) => (id === 'pageContent' ? pageContent : { innerHTML: '' }),
      querySelectorAll: () => [],
      querySelector: () => null,
    },
    app: { nav: () => {} },
    API: {
      get: () => Promise.resolve({ events: [] }),
      post: () => Promise.resolve({}),
    },
    confirm: () => true,
    alert: () => {},
    // _vcsUrl 定义于 app.js（multitask 合入后 pipeline.js 依赖），测试 harness 内注入桩
    _vcsUrl: (repoKey, path) => (repoKey || '') + (path || ''),
    console,
  };
  const failures = [];
  const checks = [];
  sandbox.__check = (cond, name) => {
    checks.push(name);
    if (!cond) failures.push('FAIL: ' + name);
  };
  sandbox.__capturePage = () => pageContent.innerHTML;
  vm.createContext(sandbox);
  vm.runInContext(PIPELINE_SRC + '\n;(' + harnessBody + ')();', sandbox, {
    filename: 'pipeline.js + harness',
  });
  return { failures, checks, pageContentHtml: pageContent.innerHTML };
}

test('filtering：空查询 / 空白查询返回全部', () => {
  const { failures } = runPipelineHarness(function () {
    const pipes = [
      { issue_id: '#101', title: '修复登录鉴权', stage: 'coding', current_owner: '张三' },
      { issue_id: '#102', title: '流水线搜索功能', stage: 'qa', current_owner: '李四' },
      { issue_id: '#103', title: 'Deploy v2.0', stage: 'sre', current_owner: 'Alice' },
    ];
    _pipeQuery = '';
    __check(_filteredPipes(pipes).length === 3, 'empty query returns all');
    _pipeQuery = '   ';
    __check(_filteredPipes(pipes).length === 3, 'whitespace-only query returns all');
    _pipeQuery = '';
  });
  assert.deepStrictEqual(failures, [], failures.join('\n'));
});

test('filtering：按各字段匹配（issue_id / title / parent / stage / scene / owner）', () => {
  const { failures } = runPipelineHarness(function () {
    const pipes = [
      { issue_id: '#101', title: '修复登录鉴权', parent_issue_id: '', stage: 'coding', scene_id: 'default', scene_name: '通用开发', current_owner: '张三' },
      { issue_id: '#102', title: '流水线搜索功能', parent_issue_id: '#101', stage: 'qa', scene_id: 'cloud-native', scene_name: '云原生', current_owner: '李四' },
      { issue_id: '#103', title: 'Deploy v2.0', parent_issue_id: '', stage: 'sre', scene_id: 'iac', scene_name: 'IaC', current_owner: 'Alice' },
    ];

    _pipeQuery = '102';
    let r = _filteredPipes(pipes);
    __check(r.length === 1 && r[0].issue_id === '#102', 'issue_id');

    _pipeQuery = '#103';
    r = _filteredPipes(pipes);
    __check(r.length === 1 && r[0].issue_id === '#103', 'issue_id with # prefix');

    _pipeQuery = 'deploy';
    r = _filteredPipes(pipes);
    __check(r.length === 1 && r[0].issue_id === '#103', 'title case-insensitive');

    _pipeQuery = 'coding';
    r = _filteredPipes(pipes);
    __check(r.length === 1 && r[0].stage === 'coding', 'stage');

    _pipeQuery = 'alice';
    r = _filteredPipes(pipes);
    __check(r.length === 1 && r[0].current_owner === 'Alice', 'owner case-insensitive');

    _pipeQuery = '云原生';
    r = _filteredPipes(pipes);
    __check(r.length === 1 && r[0].scene_name === '云原生', 'scene_name');

    _pipeQuery = 'iac';
    r = _filteredPipes(pipes);
    __check(r.length === 1 && r[0].scene_id === 'iac', 'scene_id');

    _pipeQuery = '#101';
    r = _filteredPipes(pipes);
    __check(r.length === 2, 'parent_issue_id (#101 命中 #101 自身 与 #102 的父任务)');

    _pipeQuery = 'zzz_none_match';
    r = _filteredPipes(pipes);
    __check(r.length === 0, 'no match returns empty');

    _pipeQuery = '';
  });
  assert.deepStrictEqual(failures, [], failures.join('\n'));
});

test('realIdx = allPipes.indexOf(p)：过滤后点击/双击仍映射原始索引', () => {
  const { failures } = runPipelineHarness(function () {
    const pipes = [
      { issue_id: '#101', title: 'A' },
      { issue_id: '#102', title: 'B' },
      { issue_id: '#103', title: 'C' },
    ];
    _pipeQuery = '102';
    const f = _filteredPipes(pipes);
    // pagePipes.forEach 内的 realIdx 映射原始 allPipes 索引
    const realIdx = pipes.indexOf(f[0]);
    __check(realIdx === 1, 'filtered first item maps to original index 1');
    _pipeQuery = '';
  });
  assert.deepStrictEqual(failures, [], failures.join('\n'));
});

test('renderPipeline：搜索框渲染 + 过滤分页 + 分页重置', () => {
  const { failures, pageContentHtml } = runPipelineHarness(function () {
    const data = {
      active: null,
      history: [
        { issue_id: '#101', title: '修复登录鉴权', stage: 'coding', current_owner: '张三', parent_issue_id: '' },
        { issue_id: '#102', title: '流水线搜索功能', stage: 'qa', current_owner: '李四', parent_issue_id: '#101' },
        { issue_id: '#103', title: 'Deploy v2.0', stage: 'sre', current_owner: 'Alice', parent_issue_id: '' },
      ],
      repo: 'gitcode:o/r',
      active_count: 0,
      today_count: 0,
      failed_count: 0,
      pipeline_config: null,
    };

    // 无搜索：3 行全部渲染
    _pipeQuery = '';
    _pipePage = 1;
    _pipeActiveIdx = 0;
    renderPipeline(data);
    __check(true, 'renderPipeline no query renders without exception');

    // 搜索框存在
    __check(__capturePage().indexOf('id="pipeSearchInput"') !== -1, 'search input rendered');
    __check(__capturePage().indexOf('pipeline.search_placeholder') !== -1, 'placeholder bound via I18N.t');

    // 过滤到 1 条：data-idx / onclick 应映射原始索引 1
    _pipeQuery = '102';
    _pipePage = 1;
    renderPipeline(data);
    const html = __capturePage();
    __check(html.indexOf('data-idx="1"') !== -1, 'filtered row data-idx maps to original index 1');
    __check(html.indexOf('onclick="selPipe(1)"') !== -1, 'filtered row onclick uses original index 1');

    // 过滤后分页基于 filteredPipes：过滤为 1 条时不应出现分页控件（totalPages=1，style 里的 .pl-pager 不计）
    __check(html.indexOf('class="pl-pager"') === -1, 'single filtered result hides pager');

    // 分页重置：page 越界时 renderPipeline 重置为 1
    _pipeQuery = '103';
    _pipePage = 99;
    renderPipeline(data);
    __check(_pipePage === 1, '_pipePage reset to 1 after out-of-range page');
  });
  assert.deepStrictEqual(failures, [], failures.join('\n'));
  assert.ok(pageContentHtml.length > 0, 'renderPipeline produced HTML');
});

test('i18n：中英文 search_placeholder 键均存在', () => {
  for (const lang of ['zh', 'en']) {
    const p = path.join(ROOT, 'src', 'api', 'static', 'locales', lang, 'pipeline.json');
    const j = JSON.parse(fs.readFileSync(p, 'utf8'));
    assert.ok(
      typeof j.search_placeholder === 'string' && j.search_placeholder.length > 0,
      `${lang}/pipeline.json search_placeholder present`
    );
  }
});