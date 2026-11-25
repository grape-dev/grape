/*
 * Issue #4233 — 无托管权限（can_host=false）项目卡片禁用态
 *
 * 断言策略：纯源码匹配 + 关键路径真实执行（提取函数体到沙箱运行，断言实际行为）。
 *
 * 覆盖：
 *   1. can_host=false 分支渲染 disabled 按钮，不再绑定 hostNoPermission / 不发请求
 *   2. 禁用文案复用 repos.card.no_permission_host，并用 title 说明权限要求
 *   3. 禁用态样式：cursor:not-allowed、hover 不出现实心高亮
 *   4. is_hosted / can_host 两个分支外观与行为零回归
 *   5. 死代码 hostNoPermission 已移除
 *   6. zh/en 词条同步（no_permission_host + no_permission_host_title）
 *   7. 凭据刷新后真实清缓存（执行 refreshRepoCreds，断言清内存 + localStorage）
 *   8. 刷新凭据返回大厅真实重取（执行 _goToReposInner，断言真的 GET /repos/member 并重挂）
 *   9. ReposView 重挂后真实重取（执行 onMounted + loadProjects，种子路径也静默重拉）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = fs.readFileSync(
  path.join(ROOT, 'src/api/frontend/app/src/views/ReposView.vue'),
  'utf8'
);
const ACCOUNT = fs.readFileSync(
  path.join(ROOT, 'src/api/frontend/app/src/views/AccountView.vue'),
  'utf8'
);
const APP = fs.readFileSync(
  path.join(ROOT, 'src/api/frontend/shell/js/app.js'),
  'utf8'
);
const ZH = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'src/api/frontend/shell/locales/zh/repos.json'), 'utf8')
);
const EN = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'src/api/frontend/shell/locales/en/repos.json'), 'utf8')
);

// ── 源码提取：按花括号配平取函数体（vm 沙箱执行用）──
function extractFunctionBody(src, name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) return null;
  const braceOpen = src.indexOf('{', start);
  if (braceOpen < 0) return null;
  let depth = 0;
  for (let i = braceOpen; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(braceOpen + 1, i);
    }
  }
  return null;
}

function extractOnMountedBody(src) {
  const marker = 'onMounted(() => {';
  const start = src.indexOf(marker);
  if (start < 0) return null;
  const braceOpen = src.indexOf('{', start);
  let depth = 0;
  for (let i = braceOpen; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(braceOpen + 1, i);
    }
  }
  return null;
}

let passed = 0;
const checks = [];
function check(name, fn) {
  checks.push({ name: name, fn: fn });
}

check('can_host=false 分支渲染 disabled 按钮且不绑定 click 处理', function () {
  assert.ok(
    /<button\s+v-else\s+class="project-card-btn project-card-btn-outline project-card-btn-disabled"\s+:title="t\('repos\.card\.no_permission_host_title'\)"\s+disabled\s+@click\.stop\s*>/.test(
      VIEW
    ),
    'v-else 分支应为 disabled 按钮，带禁用类与 title'
  );
  const i = VIEW.indexOf('project-card-btn-disabled');
  const branch = VIEW.slice(i, VIEW.indexOf('</button>', i));
  assert.ok(branch.indexOf('hostAndSelect') < 0, '禁用分支不应再绑定 hostAndSelect');
  assert.ok(branch.indexOf('hostNoPermission') < 0, '禁用分支不应再绑定 hostNoPermission');
  assert.ok(branch.indexOf('G.API') < 0 && branch.indexOf('showToast') < 0, '禁用分支不应发请求/弹 toast');
});

check('禁用文案复用 no_permission_host，title 说明权限要求', function () {
  assert.ok(
    /\{\{\s*t\('repos\.card\.no_permission_host'\)\s*\}\}/.test(VIEW),
    '按钮文案应复用 repos.card.no_permission_host'
  );
  assert.ok(
    /:title="t\('repos\.card\.no_permission_host_title'\)"/.test(VIEW),
    '应通过 title 说明无托管权限原因'
  );
});

check('禁用态样式与可点击态区分（cursor:not-allowed，hover 无实心高亮）', function () {
  const i = VIEW.indexOf('.project-card-btn-disabled');
  assert.ok(i >= 0, '应有 .project-card-btn-disabled 样式');
  const body = VIEW.slice(i, i + 400);
  assert.ok(/cursor:\s*not-allowed/.test(body), '禁用态应使用 cursor:not-allowed');
  assert.ok(
    /\.project-card-btn-disabled:hover\s*\{[\s\S]{0,200}background:\s*transparent/.test(body),
    '禁用态 hover 不应出现实心高亮'
  );
});

check('is_hosted 分支保持进入项目（外观/行为零回归）', function () {
  assert.ok(
    /<button\s+v-if="r\.is_hosted"\s+class="project-card-btn project-card-btn-outline"\s+@click\.stop="selectRepo\(r\)"\s*>[\s\S]{0,80}t\('repos\.card\.enter_project'\)[\s\S]{0,10}→/.test(
      VIEW
    ),
    'is_hosted 分支应保持 selectRepo + 进入项目→'
  );
});

check('can_host 分支保持加入并托管（外观/行为零回归）', function () {
  assert.ok(
    /<button\s+v-else-if="r\.can_host"\s+class="project-card-btn project-card-btn-outline"\s+@click\.stop="hostAndSelect\(r, \$event\)"\s*>[\s\S]{0,80}t\('repos\.card\.join_and_host'\)[\s\S]{0,10}→/.test(
      VIEW
    ),
    'can_host 分支应保持 hostAndSelect + 加入并托管→'
  );
});

check('死代码 hostNoPermission 已移除', function () {
  assert.ok(VIEW.indexOf('hostNoPermission') < 0, 'hostNoPermission 不应再出现在 ReposView.vue');
});

check('zh 词条同步（无权限文案 + 权限要求 title）', function () {
  const c = ZH.card;
  assert.ok(c.no_permission_host && c.no_permission_host.length > 0, 'no_permission_host 应存在');
  assert.ok(
    c.no_permission_host_title && c.no_permission_host_title.indexOf('Maintainer') >= 0,
    'zh title 应说明需 Maintainer 及以上权限'
  );
});

check('en 词条同步（无权限文案 + 权限要求 title）', function () {
  const c = EN.card;
  assert.ok(c.no_permission_host && c.no_permission_host.length > 0, 'no_permission_host 应存在');
  assert.ok(
    c.no_permission_host_title && c.no_permission_host_title.indexOf('Maintainer') >= 0,
    'en title 应说明需 Maintainer 及以上权限'
  );
});

check('凭据刷新后真实清缓存（执行 refreshRepoCreds）', function () {
  const body = extractFunctionBody(ACCOUNT, 'refreshRepoCreds');
  assert.ok(body, 'AccountView.vue 应定义 refreshRepoCreds()');

  const calls = [];
  const G = {
    app: {
      _clearCachedReposData() {
        calls.push('clear-memory-repos');
      },
    },
    window: {
      localStorage: {
        removeItem(key) {
          calls.push('remove:' + key);
        },
      },
      invalidateProjectsCache() {
        calls.push('invalidate-projects-cache');
      },
    },
  };
  new Function('G', 'function refreshRepoCreds() {' + body + '}\nrefreshRepoCreds();')(G);

  assert.ok(calls.indexOf('clear-memory-repos') >= 0, '刷新凭据应清内存仓库缓存 _cachedReposData');
  assert.ok(
    calls.indexOf('remove:repo_member_cache') >= 0,
    '刷新凭据应移除 localStorage repo_member_cache'
  );
  assert.ok(
    calls.indexOf('invalidate-projects-cache') >= 0,
    '刷新凭据应调用 invalidateProjectsCache（若存在）'
  );
});

check('刷新凭据返回大厅真实重取（执行 _goToReposInner → GET /repos/member + 重挂）', async function () {
  const body = extractFunctionBody(APP, '_goToReposInner');
  assert.ok(body, 'app.js 应定义 _goToReposInner()');

  const apiCalls = [];
  const rendered = [];
  const win = { _currentView: '' };
  const doc = {
    getElementById() {
      return null;
    },
    createElement() {
      return { id: '', style: {}, textContent: '', remove() {} };
    },
    body: { appendChild() {} },
  };
  const stub = function () {};
  const run = new Function(
    '_obExitMode',
    'window',
    '_syncAppPathToRoot',
    '_pushHistory',
    '_refreshTimer',
    'localStorage',
    'document',
    'I18N',
    '_hasVcsBinding',
    '$',
    '_renderBindingGuide',
    'API',
    'renderRepoSelection',
    '_updateTbOrgName',
    '_updateLobbyOrgName',
    'showToast',
    '_cachedReposData',
    'function _goToReposInner() {' + body + '}\nreturn _goToReposInner();'
  );

  const p = run(
    stub,
    win,
    stub,
    stub,
    null,
    { setItem: stub },
    doc,
    { t: (k) => k },
    () => true,
    () => ({ style: {} }),
    stub,
    {
      get(url) {
        apiCalls.push(url);
        return Promise.resolve({ repos: [{ name: 'remote-fresh' }] });
      },
    },
    (repos) => {
      rendered.push(repos);
    },
    stub,
    stub,
    stub,
    null
  );
  await Promise.resolve(p);
  await new Promise((r) => setTimeout(r, 0));

  assert.ok(
    apiCalls.indexOf('/repos/member') >= 0,
    '刷新凭据返回大厅必须真实 GET /repos/member，不得只读缓存'
  );
  assert.strictEqual(rendered.length, 1, '拿到列表后必须重挂 ReposView（renderRepoSelection）');
  assert.strictEqual(rendered[0][0].name, 'remote-fresh', '重挂应使用服务端最新列表');
});

check('ReposView 重挂后真实重取（执行 onMounted + loadProjects，种子路径也静默重拉）', async function () {
  const loadBody = extractFunctionBody(VIEW, 'loadProjects');
  const mountedBody = extractOnMountedBody(VIEW);
  assert.ok(loadBody, 'ReposView.vue 应定义 loadProjects()');
  assert.ok(mountedBody, 'ReposView.vue 应定义 onMounted 回调');

  function makeRef(v) {
    return { value: v };
  }

  function mountWith(initialRepos) {
    const state = {
      activeTab: makeRef('projects'),
      repos: makeRef(initialRepos ? initialRepos.slice() : []),
      projectsLoaded: makeRef(false),
      projectsError: makeRef(''),
      projectsHint: makeRef(''),
      projectsBroken: makeRef([]),
    };
    const apiCalls = [];
    const G = {
      API: {
        get(url) {
          apiCalls.push(url);
          return Promise.resolve({ repos: [{ name: 'remote-fresh' }], hint: '', _credential_broken: [] });
        },
      },
      app: { _updateLobbyOrgName() {} },
    };
    const loadProjects = new Function(
      'G',
      'repos',
      'projectsLoaded',
      'projectsError',
      'projectsHint',
      'projectsBroken',
      'function loadProjects(opts) {' + loadBody + '}\nreturn loadProjects;'
    )(G, state.repos, state.projectsLoaded, state.projectsError, state.projectsHint, state.projectsBroken);

    const onMounted = new Function(
      'activeTab',
      'props',
      'repos',
      'projectsLoaded',
      'loadProjects',
      'localStorage',
      'G',
      mountedBody
    );
    onMounted(
      state.activeTab,
      { initialRepos: initialRepos, initialTabName: '' },
      state.repos,
      state.projectsLoaded,
      loadProjects,
      { getItem() { return null; }, setItem() {} },
      G
    );
    return { state: state, apiCalls: apiCalls };
  }

  // 种子路径：initialRepos 只是首屏缓存，挂载后仍必须真实重拉
  const seeded = mountWith([{ name: 'seed-cache' }]);
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(
    seeded.apiCalls.indexOf('/repos/member') >= 0,
    '带 initialRepos 种子挂载必须静默重拉 /repos/member（否则刚获权限用户被缓存挡住）'
  );
  assert.strictEqual(seeded.state.repos.value[0].name, 'remote-fresh', '种子路径重取结果应覆盖旧缓存');
  assert.strictEqual(seeded.state.projectsLoaded.value, true, '重取完成后应标记 loaded');

  // 无种子路径：常规挂载同样真实拉取
  const plain = mountWith(null);
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(
    plain.apiCalls.indexOf('/repos/member') >= 0,
    '无种子挂载也必须真实拉取 /repos/member'
  );
});

(async function () {
  for (const c of checks) {
    await c.fn();
    passed++;
    console.log('  ✓ ' + c.name);
  }
  console.log('\n#4233 can_host 禁用态全部通过 (' + passed + '/' + checks.length + ')');
})().catch(function (e) {
  console.error('\n✗ 测试失败:', (e && e.stack) || e);
  process.exit(1);
});