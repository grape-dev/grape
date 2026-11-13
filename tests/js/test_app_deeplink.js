/*
 * 需求详情深链回归测试：/app/<owner>/<repo>/#tasks/<N>（区分仓库）
 *
 * 运行：node tests/js/test_app_deeplink.js
 *
 * 验证点：
 *   1. 路径解析：带/不带结尾斜杠、带 query、带 hash、URL 编码、大小写保留。
 *   2. 非法路径（/、/app/、缺段、多余层级、空段、'.'/'..'）一律不认，避免误把普通页面当深链。
 *   3. buildAppPath 反拼与 sameRepo 大小写不敏感比较。
 *   4. index.html 接线：deeplink.js 先于 app.js 加载、两者版本串已 bump、i18n 缓存版本已 bump。
 *   5. app.js 真正接线：启动走 _bootRestore（先定仓库再路由）、仓库切换同步路径。
 *   6. zh/en 都有 app.deeplink.repo_not_found（缺键时 I18N.t 原样返回键名，|| 兜底不生效）。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const root = path.resolve(__dirname, '../..');
const DL = require(path.join(root, 'src/api/static/js/deeplink.js'));
const appJs = fs.readFileSync(path.join(root, 'src/api/static/js/app.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(root, 'src/api/static/index.html'), 'utf8');

let pass = 0;
function ok(cond, msg) {
  assert.ok(cond, msg);
  pass++;
}

// 1. 合法深链
{
  const r = DL.parseAppPath('/app/acme/demo/');
  ok(r.owner === 'acme' && r.repo === 'demo' && r.full === 'acme/demo' && r.prefix === '',
     '带结尾斜杠');
}
ok(DL.parseAppPath('/app/acme/demo').full === 'acme/demo', '不带结尾斜杠');
ok(DL.parseAppPath('/app/acme/demo/#tasks/12').full === 'acme/demo', 'pathname 自带 hash 也能解析');
ok(DL.parseAppPath('/app/acme/demo?x=1').full === 'acme/demo', '带 query');
ok(DL.parseAppPath('/app/Octo-Org/Demo.Portal/').full === 'Octo-Org/Demo.Portal', '大小写与点号保留');
ok(DL.parseAppPath('/app/%E9%A9%AC%E5%B2%A9%E6%9D%BE/demo/').owner === '马岩松', 'URL 编码解码');

// 2. 非法路径
['/', '', '/app/', '/app/acme/', '/app/acme/demo/extra', '/app//demo/', '/app/acme//',
 '/app/acme/..', '/app/./demo', '/static/js/app.js', '/api/repos', null, undefined]
  .forEach(function (p) {
    assert.strictEqual(DL.parseAppPath(p), null, '不应认作深链: ' + p);
    pass++;
  });

// 3. 反向拼路径 / 仓库比较
ok(DL.buildAppPath('acme', 'demo') === '/app/acme/demo/', 'buildAppPath 拼法');
ok(DL.parseAppPath('/en/app/acme/demo/').prefix === '/en', '英文站前缀被识别');
ok(DL.parseAppPath('/en/app/acme/demo/').full === 'acme/demo', '英文站深链仓库正确');
ok(DL.buildAppPath('acme', 'demo', '/en') === '/en/app/acme/demo/', '/en 前缀回拼');
ok(DL.parseAppPath('/ent/app/acme/demo/') === null, '/ent 不是语言前缀');
ok(DL.buildAppPath('马岩松', 'demo') === '/app/%E9%A9%AC%E5%B2%A9%E6%9D%BE/demo/', 'buildAppPath 编码');
ok(DL.buildAppPath('', 'demo') === '/', '缺 owner 回根路径');
ok(DL.sameRepo('Acme/Demo', 'acme/demo') === true, 'sameRepo 大小写不敏感');
ok(DL.sameRepo('acme/demo', 'acme/demo2') === false, 'sameRepo 不同仓库');

// 4. index.html 接线与版本串
const dlIdx = indexHtml.indexOf('/static/js/deeplink.js?v=1');
const _appVer = /\/static\/js\/app\.js\?v=(\d+)/.exec(indexHtml);
const appIdx = _appVer ? indexHtml.indexOf(_appVer[0]) : -1;
ok(dlIdx > 0, 'index.html 引用了 deeplink.js?v=1');
// 版本号只要求「不低于引入本特性时的 258」：合并到其它周线时会被继续 bump（如 260）
ok(_appVer && Number(_appVer[1]) >= 258, 'app.js 版本串不低于 258（本特性引入时的基线）');
ok(dlIdx < appIdx, 'deeplink.js 必须先于 app.js 执行（defer 按文档顺序）');
ok(indexHtml.indexOf("__I18N_CACHE_VER__='43'") > 0, 'i18n 缓存版本已 bump（新增文案）');

// 5. app.js 接线（结构断言：防回退成「只认 hash、不认仓库」）
ok(appJs.indexOf('function _bootRestore()') > 0, 'app.js 有 _bootRestore');
ok(appJs.indexOf('_applyAppPathRepo().then(function (hit)') > 0,
   '_bootRestore 必须先定仓库再恢复 hash 路由');
ok(appJs.indexOf('_bootRestore();') > 0, '登录后启动流程已切到 _bootRestore');
ok(appJs.indexOf('_restoreNavAfterLogin();\n    }).catch') < 0, '不得绕过深链直接 _restoreNavAfterLogin');
ok(appJs.indexOf('_syncAppPath(parts[0], parts[1]);') > 0, '仓库切换后同步路径');
ok(appJs.indexOf("API.get('/repos/member')") > 0, '深链仓库以 /repos/member 服务端结果为准');
ok(appJs.indexOf("window.history.replaceState(window.history.state, '', target +") > 0,
   '路径同步用 replaceState（不新增历史条目）');

// 5b. 仓库大堂进入项目（repos.js selectRepo）也要把路径写上，否则地址栏永远是 /
const reposJs = fs.readFileSync(path.join(root, 'src/api/static/js/pages/repos.js'), 'utf8');
ok(appJs.indexOf('syncAppPath: _syncAppPath') > 0, 'app 导出 syncAppPath 供 repos.js 调用');
ok(reposJs.indexOf('window.app.syncAppPath(owner, name)') > 0, 'selectRepo 进入项目时同步深链路径');
{
  const m = /pages\/repos\.js\?v=(\d+)/.exec(indexHtml);
  ok(m && Number(m[1]) >= 67, 'repos.js 版本串已 bump（改过内容）');
}

// 6. 语言包键（zh/en 都要有，否则缺键返回键名且 || 兜底失效）
['zh', 'en'].forEach(function (lang) {
  const d = JSON.parse(fs.readFileSync(
    path.join(root, 'src/api/static/locales', lang, 'app.json'), 'utf8'));
  ok(d.deeplink && typeof d.deeplink.repo_not_found === 'string' && d.deeplink.repo_not_found,
     lang + ' 有 app.deeplink.repo_not_found');
});

console.log('— 静态解析与接线断言通过（' + pass + ' 项），继续行为验证');

// ---------------------------------------------------------------- 7. 行为验证（源码抽取 + 纯 JS 沙箱）
// 说明：不依赖浏览器/dom，用大括号配对从 app.js 抽出深链接线函数，在桩环境里跑真实分支。
function extractFn(src, name) {
  const start = src.indexOf('function ' + name + '(');
  assert.ok(start >= 0, '未找到函数 ' + name);
  const braceStart = src.indexOf('{', src.indexOf(')', start));
  let depth = 0;
  for (let i = braceStart; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error('无法配对 ' + name);
}

function buildSandbox(opts) {
  const store = {};
  const session = {};
  const calls = { toast: [], routed: [], replaceState: [] };
  const win = {
    GrapeDeepLink: DL,
    location: { pathname: opts.pathname || '/', hash: opts.hash || '', search: '' },
    history: {
      state: null,
      replaceState: function (st, t, url) {
        calls.replaceState.push(url);
        win.__url = url;
        // 真浏览器的 replaceState 会更新 location；桩必须同款，否则「同仓不重复写」判不出来
        const parts = String(url).split('#');
        win.location.pathname = parts[0] || '/';
        win.location.hash = parts.length > 1 ? '#' + parts[1] : '';
      }
    }
  };
  const sandbox = {
    window: win,
    GrapeDeepLink: DL,
    sessionStorage: {
      getItem: k => (k in session ? session[k] : null),
      setItem: (k, v) => { session[k] = String(v); },
      removeItem: k => { delete session[k]; }
    },
    localStorage: {
      getItem: k => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: k => { delete store[k]; }
    },
    API: {
      get: function (path) {
        calls.apiPath = path;
        return Promise.resolve({ repos: opts.repos || [] });
      }
    },
    I18N: { t: k => k },
    showToast: function (msg) { calls.toast.push(msg); },
    REPO_CACHE_KEY: 'grape_repos_cache',
    JSON: JSON,
    String: String,
    Promise: Promise,
    console: console
  };
  if (opts.cachedRepos) store['grape_repos_cache'] = JSON.stringify({ repos: opts.cachedRepos });
  if (opts.currentRepo) session['currentRepo'] = JSON.stringify(opts.currentRepo);

  const names = ['currentRepo', '_appPathCtx', '_setCurrentRepoFromDeeplink',
                 '_applyAppPathRepo', '_bootRestore', '_syncAppPath', '_appPathPrefix',
                 '_syncAppPathToRoot'];
  const body = names.map(n => extractFn(appJs, n)).join('\n');
  const routedFn = 'function _restoreNavAfterLogin() { window.__routed = (window.__routed || 0) + 1; }\n' +
    'function nav(v, p) { window.__nav = (v || "") + "/" + (p === undefined || p === null ? "" : p); }';
  const runner = new Function('window', 'GrapeDeepLink', 'sessionStorage', 'localStorage',
    'API', 'I18N', 'showToast', 'REPO_CACHE_KEY', '__routedMark',
    routedFn + '\n' + body + '\nreturn {boot: _bootRestore, apply: _applyAppPathRepo, ' +
    'sync: _syncAppPath, syncRoot: _syncAppPathToRoot, currentRepo: currentRepo};');
  const api = runner(win, DL, sandbox.sessionStorage, sandbox.localStorage, sandbox.API,
    sandbox.I18N, sandbox.showToast, sandbox.REPO_CACHE_KEY);
  return { api, win, calls, session, sandbox };
}

// _bootRestore 不返回 Promise（真实代码不需要）；用 setImmediate 冲净微任务后再断言
function flush() { return new Promise(r => setImmediate(r)); }

(async function () {
  // 7a. 深链仓库命中 → currentRepo 切换，且 hash 路由在仓库就绪之后才恢复
  {
    const env = buildSandbox({
      pathname: '/app/acme/demo/', hash: '#tasks/12',
      repos: [{ full_name: 'acme/demo', owner: 'acme', name: 'demo', repo_key: 'gitcode:acme/demo' },
              { full_name: 'other/thing', owner: 'other', name: 'thing' }]
    });
    env.api.boot();
    await flush();
    const cur = JSON.parse(env.sandbox.sessionStorage.getItem('currentRepo'));
    ok(cur.fullName === 'acme/demo', '7a currentRepo 切到深链仓库');
    ok(cur.repoKey === 'gitcode:acme/demo', '7a repo_key 带上');
    ok(env.win.__routed === 1, '7a 仓库就绪后恢复 hash 路由（_restoreNavAfterLogin 被调用一次）');
    ok(env.calls.toast.length === 0, '7a 命中仓库不弹提示');
  }

  // 7b. 深链仓库不在列表 → 不设置上下文、给提示、仍继续路由（不白屏）
  {
    const env = buildSandbox({
      pathname: '/app/secret/nope/', hash: '#tasks/3',
      repos: [{ full_name: 'acme/demo', owner: 'acme', name: 'demo' }]
    });
    env.api.boot();
    await flush();
    ok(env.sandbox.sessionStorage.getItem('currentRepo') === null, '7b 未命中不写 currentRepo');
    ok(env.calls.toast.length === 1, '7b 弹一次「无权/未托管」提示');
    ok(env.win.__routed === 1, '7b 提示后仍继续路由（走引导页/仓库选择）');
  }

  // 7c. 缓存命中但服务端已解绑 → 清上下文（不拿旧仓库打开别人的单）
  {
    const env = buildSandbox({
      pathname: '/app/gone/repo/', hash: '#tasks/9',
      cachedRepos: [{ full_name: 'gone/repo', owner: 'gone', name: 'repo' }],
      repos: []
    });
    env.api.boot();
    await flush();
    ok(env.sandbox.sessionStorage.getItem('currentRepo') === null, '7c 解绑后清掉缓存里的仓库');
    ok(env.calls.toast.length === 1, '7c 提示一次');
  }

  // 7d. 非深链路径（普通首页）→ 不发请求、不改仓库、照常路由
  {
    const env = buildSandbox({ pathname: '/', hash: '#tasks/1', repos: [] });
    env.api.boot();
    await flush();
    ok(env.calls.apiPath === undefined, '7d 非深链不发 /repos/member 请求');
    ok(env.win.__routed === 1, '7d 直接走原有路由');
  }

  // 7e. 仓库切换 → 路径同步为 /app/<owner>/<repo>/ 并保留 hash；同仓不重复写
  {
    const env = buildSandbox({ pathname: '/', hash: '#tasks/12', repos: [] });
    env.api.sync('acme', 'demo');
    ok(env.calls.replaceState[0] === '/app/acme/demo/#tasks/12', '7e 同步路径并保留 hash');
    env.api.sync('acme', 'demo');
    ok(env.calls.replaceState.length === 1, '7e 同仓不重复 replaceState');
  }

  // 7g. 仓库显示名 ≠ URL 名 → 以 currentRepo.fullName 拼路径（防死链）
  {
    const env = buildSandbox({
      pathname: '/', hash: '#tasks/5',
      currentRepo: { fullName: 'acme/demo-url', owner: 'acme', name: '显示名', repoKey: '' }
    });
    env.api.sync('acme', '显示名');
    ok(env.calls.replaceState[0] === '/app/acme/demo-url/#tasks/5',
       '7g 用 fullName 的 URL 段拼路径（不是显示名）');
  }

  // 7f. 回仓库大堂：深链路径还原成根路径（保留 hash），非深链路径不动
  {
    const env = buildSandbox({ pathname: '/app/acme/demo/', hash: '#repos', repos: [] });
    env.api.syncRoot();
    ok(env.calls.replaceState[0] === '/#repos', '7f 深链前缀还原为 /');
    const env2 = buildSandbox({ pathname: '/', hash: '#repos', repos: [] });
    env2.api.syncRoot();
    ok(env2.calls.replaceState.length === 0, '7f 非深链路径不动');
  }

  // 7i. 只有仓库没有 hash（/app/o/r/）→ 进该仓库工作台，不是仓库大堂
  {
    const env = buildSandbox({
      pathname: '/app/acme/demo/', hash: '',
      repos: [{ full_name: 'acme/demo', owner: 'acme', name: 'demo' }]
    });
    env.api.boot();
    await flush();
    ok(env.win.__nav === 'dash/', '7i 无 hash 时进入 dash（当前仓工作台）');
    ok(env.win.__routed === undefined, '7i 不再走仓库大堂/引导页分支');
  }

  // 7j. 只有仓库没有 hash 且仓库无权 → 仍走原路由（引导页），不白屏
  {
    const env = buildSandbox({ pathname: '/app/secret/nope/', hash: '', repos: [] });
    env.api.boot();
    await flush();
    ok(env.win.__nav === undefined && env.win.__routed === 1, '7j 未命中仓库走原路由并提示');
    ok(env.calls.toast.length === 1, '7j 有提示');
  }

  // 7h. 英文站：/en/app/<owner>/<repo>/ → 回大堂还原成 /en/
  {
    const env = buildSandbox({ pathname: '/en/app/acme/demo/', hash: '#repos', repos: [] });
    env.api.syncRoot();
    ok(env.calls.replaceState[0] === '/en/#repos', '7h 英文站路径还原保留 /en 前缀');
  }

  console.log('结果: ' + pass + ' 通过, 0 失败');
  console.log('✓ 需求详情深链（/app/<owner>/<repo>/#tasks/N）全部断言通过');
})();

