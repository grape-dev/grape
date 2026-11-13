/*
 * 解绑账号后仓库列表不可见（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy account.js；Vue 迁移后为 unbindProvider + onUnbindSuccess，
 * 且后端在存在关联仓库时要求二次确认。
 *   1. 解绑先弹确认；确认后 DELETE /auth/bind/<provider>
 *   2. 后端返回 need_confirm + warehouses → 二次确认列出受影响仓库（逐条 esc 转义）
 *   3. 成功后清理仓库列表缓存/可见性（onUnbindSuccess）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/AccountView.vue'), 'utf8');

function fnBody(name) {
  const i = VIEW.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('未找到 ' + name);
  let d = 0,
    started = false;
  for (let j = i; j < VIEW.length; j++) {
    if (VIEW[j] === '{') {
      d++;
      started = true;
    } else if (VIEW[j] === '}') {
      d--;
      if (started && d === 0) return VIEW.slice(i, j + 1);
    }
  }
  throw new Error(name + ' 括号不闭合');
}

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('解绑先确认 + DELETE 端点', function () {
  const b = fnBody('unbindProvider');
  assert.ok(/showConfirm\(/.test(b), '应先确认');
  assert.ok(/'\/auth\/bind\/' \+ provider/.test(b), '应 DELETE /auth/bind/{provider}');
  assert.ok(/\.del\(/.test(b), '应用 DELETE 方法');
});

check('有关联仓库时二次确认并逐条转义', function () {
  const b = fnBody('unbindProvider');
  assert.ok(/need_confirm/.test(b), '应识别后端二次确认信号');
  assert.ok(/warehouses/.test(b), '应读取受影响仓库列表');
  assert.ok(/esc\(w\)/.test(b), '仓库名应转义（防注入）');
  assert.ok(/unbind_affected_repos_prefix/.test(b), '应有提示文案');
});

check('成功后清理仓库可见性', function () {
  assert.ok(/function onUnbindSuccess\(/.test(VIEW), '应有 onUnbindSuccess');
  const i = VIEW.indexOf('function onUnbindSuccess(');
  const b = VIEW.slice(i, i + 900);
  assert.ok(/repos|currentRepo|clear/i.test(b), '应清理仓库列表/当前仓库');
});

console.log('\nunbind 全部通过 (' + passed + '/3)');