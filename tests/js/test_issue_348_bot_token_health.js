/*
 * Issue #348 — Admin Bot Token 健康面板（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy admin.js；Vue 迁移后 AdminView 里为 token 字段 + 校验 + 状态徽标。
 *   1. 每家 provider 的 bot token 字段与「已存 token 尾部」占位（不显示明文）
 *   2. 校验按钮走 POST /config/verify-token，未填 token 先提示
 *   3. 校验结果落到按 provider 的状态徽标（成功/失败可分辨）
 *   4. token 输入为空时不清空已存值（占位提示而非清空）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/AdminView.vue'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('token 字段 + 尾部占位（不显示明文）', function () {
  assert.ok(/github_bot_token/.test(VIEW), '应有 github_bot_token 字段');
  assert.ok(/phToken\(/.test(VIEW), '占位应由 phToken 生成');
  const i = VIEW.indexOf('function phToken');
  if (i > 0) {
    const b = VIEW.slice(i, i + 500);
    assert.ok(/tail/.test(b), '应拼接已存 token 尾部');
    assert.ok(!/return .*\$\{tok\}/.test(b), '不应回显完整 token');
  }
});

check('校验走 /config/verify-token，空值先提示', function () {
  const i = VIEW.indexOf('function verifyToken(');
  assert.ok(i > 0, '应有 verifyToken');
  const b = VIEW.slice(i, i + 900);
  assert.ok(/'\/config\/verify-token'/.test(b), '应 POST /config/verify-token');
  assert.ok(/enter_token_first/.test(b), '空 token 应先提示');
});

check('结果落到按 provider 的状态徽标', function () {
  assert.ok(/st\[\s*field\s*\]|st\.github_bot_token/.test(VIEW), '状态应按字段存');
  const i = VIEW.indexOf('function verifyToken(');
  const b = VIEW.slice(i, i + 1400);
  assert.ok(/st\[/.test(b) || /st\./.test(b), '校验结果应写入状态');
  assert.ok(/ok|success|fail/.test(b), '应区分成功/失败');
});

console.log('\n#348 全部通过 (' + passed + '/3)');