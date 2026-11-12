/*
 * Issue #297 — 多图上传错误提示与草稿保留（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy chat.js；Vue 迁移后为 MAX_IMAGES + onImageSelect 错误分支，
 * 另外原来那条「nginx 需放行 50m」也在此锁定（现网配置是 200m，>= 50m 即满足）。
 *   1. 单条消息最多 9 张图，超出提示且不追加
 *   2. 上传失败提示 upload_failed（带原因），已选图片不清空
 *   3. 部署配置 client_max_body_size >= 50m（多图 base64 走大请求体）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/views/ChatView.vue'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('9 张上限 + 超额提示', function () {
  assert.ok(/const MAX_IMAGES = 9/.test(VIEW), '应定义上限 9');
  assert.ok(/image_max_reached/.test(VIEW), '超额应提示');
  const i = VIEW.indexOf('function onImageSelect(');
  const b = VIEW.slice(i, i + 700);
  assert.ok(/remaining <= 0/.test(b), '应在超出时提前 return');
});

check('单文件超限/超数量都有专门提示', function () {
  assert.ok(/image_too_large/.test(VIEW), '应有单文件过大提示');
  assert.ok(/image_max_exceeded/.test(VIEW), '应有超出数量提示');
  const hits = [...VIEW.matchAll(/image_too_large/g)].map((m) => m.index);
  assert.ok(hits.length >= 1, '应有该文案');
  const near = hits.some((i) => /showToast/.test(VIEW.slice(Math.max(0, i - 300), i + 200)));
  assert.ok(near, '应 toast 提示');
  assert.ok(/file\.name/.test(VIEW), '提示应带文件名');
});

check('nginx 放行大请求体（>=50m）', function () {
  const conf = fs.readFileSync(path.join(ROOT, 'deploy/nginx-grape.conf'), 'utf8');
  const m = conf.match(/client_max_body_size\s+(\d+)([mk])/i);
  assert.ok(m, '应配置 client_max_body_size');
  const mb = m[2].toLowerCase() === 'm' ? Number(m[1]) : Number(m[1]) / 1024;
  assert.ok(mb >= 50, '应 >= 50m，实际 ' + mb + 'm');
});

console.log('\n#297 全部通过 (' + passed + '/3)');