/*
 * Issue #237: 华为云凭据清除 Bug — 前端 saveCredentials body 构建逻辑测试
 *
 * 运行：node tests/js/test_issue_237_cred_clear.js
 *
 * 验证点（与 src/api/static/js/pages/settings.js saveCredentials 一致）：
 *   1. ******** 掩码值 → 字段不传（代表未修改，后端不动）
 *   2. 空字符串 → 字段传空（清除已有凭据）
 *   3. 真实值 → 字段传真实值（写入）
 *   4. 占位符值 → 字段照传（后端 _is_placeholder 拒绝写入，保持已有值）
 */

const assert = require('assert');

// ── 提取 saveCredentials 的 body 构建逻辑（与 settings.js 一致）──
function buildCredBody(akVal, skVal, ghVal) {
  const body = {};
  // Issue #237：始终传递 AK/SK（含空字符串=清除），排除 ******** 掩码值（代表未修改）
  if (akVal !== '********') body.huawei_cloud_ak = akVal;
  if (skVal !== '********') body.huawei_cloud_sk = skVal;
  if (ghVal !== '********') body.github_token = ghVal;
  return body;
}

// ── 测试 ──

console.log('Issue #237: saveCredentials body 构建逻辑测试\n');

// 1. ******** 掩码 → 不传字段（未修改）
let body = buildCredBody('********', '********', '********');
assert.ok(!('huawei_cloud_ak' in body), '******** mask: AK should not be sent');
assert.ok(!('huawei_cloud_sk' in body), '******** mask: SK should not be sent');
assert.ok(!('github_token' in body), '******** mask: GitHub token should not be sent');
console.log('  PASS: ******** 掩码值不传字段（未修改语义）');

// 2. 空字符串 → 传空（清除）
body = buildCredBody('', '', '');
assert.strictEqual(body.huawei_cloud_ak, '', 'empty: AK should be empty string');
assert.strictEqual(body.huawei_cloud_sk, '', 'empty: SK should be empty string');
assert.strictEqual(body.github_token, '', 'empty: GitHub token should be empty string');
console.log('  PASS: 空字符串传空值（清除语义）');

// 3. 真实值 → 传真实值
body = buildCredBody('AKID_real123', 'SK_real456', 'ghp_token789');
assert.strictEqual(body.huawei_cloud_ak, 'AKID_real123');
assert.strictEqual(body.huawei_cloud_sk, 'SK_real456');
assert.strictEqual(body.github_token, 'ghp_token789');
console.log('  PASS: 真实值正常传递');

// 4. 占位符值 → 照传（后端拒绝写入，保持已有值）
body = buildCredBody('AK-demo-access-key-xxxx', 'SK-demo-secret-key-xxxx', '');
assert.strictEqual(body.huawei_cloud_ak, 'AK-demo-access-key-xxxx');
assert.strictEqual(body.huawei_cloud_sk, 'SK-demo-secret-key-xxxx');
console.log('  PASS: 占位符值照传（由后端 _is_placeholder 拒绝）');

// 5. 混合场景：AK 清除、SK 未修改、GitHub token 新值
body = buildCredBody('', '********', 'ghp_new_token');
assert.strictEqual(body.huawei_cloud_ak, '', 'mixed: AK cleared');
assert.ok(!('huawei_cloud_sk' in body), 'mixed: SK not sent (unmodified)');
assert.strictEqual(body.github_token, 'ghp_new_token', 'mixed: GitHub token set');
console.log('  PASS: 混合场景（清除/未修改/新值）');

// 6. 空白字符串 → trim 后为空 → 传空（清除）
//    注意：settings.js 中 akVal = ak.value.trim()，空白输入 trim 后为空
body = buildCredBody('   '.trim(), '********', '');
assert.strictEqual(body.huawei_cloud_ak, '', 'whitespace: AK trimmed to empty = clear');
console.log('  PASS: 空白输入 trim 后清除');

console.log('\n全部通过 (6/6)\n');
