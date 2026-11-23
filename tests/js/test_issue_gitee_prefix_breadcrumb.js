/*
 * 回归测试：「当前项目」面包屑偶尔出现 gitee:/gitlab: 前缀。
 *
 * 运行：node tests/js/test_issue_gitee_prefix_breadcrumb.js
 *
 * 背景（2026-11-23 现场）：在需求中心打开 gitee 仓的卡片，再点「看流水线 / 看事件」，
 * setRepoFromKey() 把 repo_key 当 full_name 写进 sessionStorage.currentRepo；因为剥离
 * 正则只写死了 github|gitcode，前缀被带进顶栏「当前项目」；刷新时仓库清单
 * （后端 from_repo_key 全平台剥离）重新解析仓库，前缀才消失。
 *
 * 覆盖点：
 *   1. lib/repo-key.js 的 stripRepoPrefix 覆盖任意平台前缀（含 gitlab 子组）
 *   2. 运行时优先委托 shell 注入的 globalThis.stripRepoPrefix
 *   3. 前端视图不得再写死 `^(github|gitcode):`（平台字面量枚举）
 *   4. shell currentRepo() 读取时自愈：脏 fullName 被剥净，owner/name 同步纠正
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const root = path.resolve(__dirname, '../..');
const LIB = path.join(root, 'src/api/frontend/app/src/lib/repo-key.js');
const APP = path.join(root, 'src/api/frontend/shell/js/app.js');
const VIEWS_DIR = path.join(root, 'src/api/frontend/app/src/views');

async function loadLib() {
  const code = fs.readFileSync(LIB, 'utf8').replace(/^export\s+/gm, '');
  const url = 'data:text/javascript;base64,' +
    Buffer.from(code + '\nexport { stripRepoPrefix };\n').toString('base64');
  return import(url);
}

// 从 shell app.js 里截出某个顶层函数的源码（按花括号配平），供单测直接执行
function extractFn(src, name) {
  const start = src.indexOf('function ' + name + '(');
  assert.ok(start >= 0, '找不到函数 ' + name);
  let i = src.indexOf('{', start);
  let depth = 0;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') {
      depth--;
      if (depth === 0) return src.slice(start, j + 1);
    }
  }
  throw new Error('函数未闭合: ' + name);
}

(async () => {
  const { stripRepoPrefix } = await loadLib();

  // 1. 任意平台前缀都剥离（这是本次修复的核心：不再只认 github/gitcode）
  assert.strictEqual(stripRepoPrefix('gitee:acme-team/demo'), 'acme-team/demo');
  assert.strictEqual(stripRepoPrefix('gitlab:grp/sub/proj'), 'grp/sub/proj');
  assert.strictEqual(stripRepoPrefix('gitcode:a/b'), 'a/b');
  assert.strictEqual(stripRepoPrefix('github:a/b'), 'a/b');
  assert.strictEqual(stripRepoPrefix('codearts:o/r'), 'o/r');

  // 2. 无前缀 / 非前缀段的 ':' / 空值原样返回
  assert.strictEqual(stripRepoPrefix('a/b'), 'a/b');
  assert.strictEqual(stripRepoPrefix('weird:name'), 'weird:name');
  assert.strictEqual(stripRepoPrefix('http://x/y'), 'http://x/y');
  assert.strictEqual(stripRepoPrefix(':owner/repo'), ':owner/repo');
  assert.strictEqual(stripRepoPrefix(''), '');
  assert.strictEqual(stripRepoPrefix(null), '');

  // 3. 壳已装载时优先委托 window.stripRepoPrefix（平台目录驱动的真源）
  const prev = globalThis.stripRepoPrefix;
  globalThis.stripRepoPrefix = () => 'delegated/called';
  try {
    const lib2 = await loadLib();
    assert.strictEqual(lib2.stripRepoPrefix('gitee:a/b'), 'delegated/called');
  } finally {
    if (prev === undefined) delete globalThis.stripRepoPrefix;
    else globalThis.stripRepoPrefix = prev;
  }

  // 4. 前端视图目录里不得再有写死的双平台剥离正则（注释里提到可以）
  const offenders = [];
  for (const f of fs.readdirSync(VIEWS_DIR)) {
    if (!/\.(vue|js)$/.test(f)) continue;
    const src = fs.readFileSync(path.join(VIEWS_DIR, f), 'utf8');
    for (const m of src.matchAll(/replace\(\s*\/\^\((?:gitcode\|github|github\|gitcode)\):/g)) {
      offenders.push(f + ' -> ' + m[0]);
    }
  }
  assert.deepStrictEqual(offenders, [], '仍有写死的平台前缀剥离: ' + offenders.join(', '));

  // 5. shell currentRepo() 自愈：手写一个 gitee 前缀的脏快照，读一次应被剥净
  const appSrc = fs.readFileSync(APP, 'utf8');
  const sessionStore = {};
  const fakeSession = {
    getItem: (k) => (k in sessionStore ? sessionStore[k] : null),
    setItem: (k, v) => { sessionStore[k] = String(v); },
    removeItem: (k) => { delete sessionStore[k]; },
  };
  sessionStore.currentRepo = JSON.stringify({
    fullName: 'gitee:acme-team/demo',
    owner: 'gitee:acme-team',
    name: 'demo',
    repoKey: 'gitee:acme-team/demo',
  });
  const shellStrip = new Function(
    extractFn(appSrc, 'stripRepoPrefix') + '\nreturn stripRepoPrefix;'
  )();
  const currentRepo = new Function(
    'stripRepoPrefix', 'sessionStorage',
    extractFn(appSrc, 'currentRepo') + '\nreturn currentRepo;'
  )(shellStrip, fakeSession);
  const r = currentRepo();
  assert.strictEqual(r.fullName, 'acme-team/demo', 'fullName 未剥净');
  assert.strictEqual(r.owner, 'acme-team', 'owner 未剥净');
  assert.strictEqual(r.name, 'demo', 'name 被改坏');
  assert.strictEqual(r.repoKey, 'gitee:acme-team/demo', 'repoKey 必须保留前缀');
  // 自愈要落盘：再次读取不应依赖内存
  const again = JSON.parse(sessionStore.currentRepo);
  assert.strictEqual(again.fullName, 'acme-team/demo', '剥净结果未回写 sessionStorage');

  // 6. 干净快照不受影响（幂等）+ GitLab 子组 owner 反推正确
  sessionStore.currentRepo = JSON.stringify({
    fullName: 'gitlab:grp/sub/proj', owner: 'gitlab:grp', name: 'sub',
    repoKey: 'gitlab:grp/sub/proj',
  });
  const gl = currentRepo();
  assert.strictEqual(gl.fullName, 'grp/sub/proj');
  assert.strictEqual(gl.owner, 'grp/sub');
  assert.strictEqual(gl.name, 'proj');

  sessionStore.currentRepo = JSON.stringify({
    fullName: 'owner/repo', owner: 'owner', name: 'repo', repoKey: 'gitcode:owner/repo',
  });
  const clean = currentRepo();
  assert.strictEqual(clean.fullName, 'owner/repo');
  assert.strictEqual(clean.owner, 'owner');
  assert.strictEqual(clean.name, 'repo');

  console.log('PASS tests/js/test_issue_gitee_prefix_breadcrumb.js');
})().catch((e) => {
  console.error('FAIL', e && e.message);
  process.exit(1);
});