/*
 * 仓库宿主 provider 推导（FS-01，源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy repos.js；Vue 迁移后为按 repo_key 前缀推导。
 *   1. github: 前缀 → github 品牌/域名；其它（gitcode:）→ gitcode
 *   2. 外部链接按前缀拼正确域名（github.com / gitcode.com）
 *   3. 平台筛选按前缀匹配，不靠字符串包含（避免 githubxx 误判）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/ReposView.vue'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('前缀推导品牌图标与名称', function () {
  assert.ok(/indexOf\('github:'\) === 0 \? 'github' : 'gitcode'/.test(VIEW), '应按前缀给品牌');
  assert.ok(/indexOf\('github:'\) === 0 \? 'GitHub' : 'GitCode'/.test(VIEW), '应按前缀给名称');
});

check('外链域名按前缀拼', function () {
  assert.ok(/https:\/\/github\.com\/' \+ fn/.test(VIEW), 'github 前缀应拼 github.com');
  const i = VIEW.indexOf('https://github.com/');
  assert.ok(/gitcode\.com/.test(VIEW), 'gitcode 应有对应域名');
});

check('筛选按前缀严格匹配（非 includes）', function () {
  assert.ok(/platformFilter\.value === 'github'\)[^\n]*rk\.indexOf\('github:'\) === 0/.test(VIEW), '筛选应按前缀');
  assert.ok(!/platformFilter[\s\S]{0,60}\.includes\(/.test(VIEW), '不应退化为 contains 匹配');
});

console.log('\nprovider 推导全部通过 (' + passed + '/3)');