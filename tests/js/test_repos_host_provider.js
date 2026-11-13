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
  // arch-weekly-11 d2787875 (D102-F)：筛选改成「被选平台前缀精确匹配 + 裸键归 gitcode」，
  // 原「非 github 即 gitcode」把 gitee 错归 gitcode 桶 —— 这里锁新口径。
  assert.ok(/return rk\.indexOf\(platformFilter\.value \+ ':'\) === 0/.test(VIEW), '筛选应按前缀精确匹配');
  assert.ok(/if \(!rk\) return platformFilter\.value === 'gitcode'/.test(VIEW), '裸键应归 gitcode');
  assert.ok(!/platformFilter[\s\S]{0,60}\.includes\(/.test(VIEW), '不应退化为 contains 匹配');
});

check('平台栏数据源 = 有仓库的平台 ∪ 身份绑定兜底', function () {
  // d2787875：PAT 不产生身份行，bindings 当权威会让纯 PAT 平台整条平台栏消失。
  assert.ok(/repos\.value\.forEach\(\(r\) => \{[\s\S]{0,160}repo_key/.test(VIEW), '平台应取自仓库 repo_key');
  assert.ok(/if \(!Object\.keys\(keys\)\.length\)[\s\S]{0,120}bindings\.forEach/.test(VIEW), '无仓库时回退 bindings');
  assert.ok(/rk\.indexOf\(':'\) > 0 \? rk\.split\(':'\)\[0\] : 'gitcode'/.test(VIEW), '裸键归 gitcode');
});

console.log('\nprovider 推导全部通过 (' + passed + '/4)');
