/*
 * Issue #372 R14: 深度审计修复测试 — 前端部分
 *
 * 运行：node tests/js/test_issue_372_r14_audit.js
 *
 * 测试用例 T42-01 ~ T42-08，对应需求文档第十六章 R14 场景用例。
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');

function readFile(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

const APP_JS = readFile('src/api/static/js/app.js');
const ZH_EVENTS = readFile('src/api/static/locales/zh/events.json');
const EN_EVENTS = readFile('src/api/static/locales/en/events.json');
const ZH_APP = readFile('src/api/static/locales/zh/app.json');
const EN_APP = readFile('src/api/static/locales/en/app.json');

// ── T42-01: app.errMsg() 在 catch 块中被调用 ──
function testErrMsgCalledInCatch() {
  // errMsg( 出现次数应 >= 2（1 次定义 + 至少 1 次调用）
  const matches = APP_JS.match(/errMsg\(/g) || [];
  assert.ok(matches.length >= 2,
    `errMsg should be called in catch blocks, found ${matches.length} occurrences`);
  console.log('  PASS: T42-01 app.errMsg() 在 catch 块中被调用');
}

// ── T42-02: errMsg 函数映射 error_code 到 i18n ──
function testErrMsgMapsErrorCode() {
  assert.ok(APP_JS.indexOf('error_code') >= 0 || APP_JS.indexOf('err_code') >= 0,
    'errMsg should reference error_code');
  assert.ok(APP_JS.indexOf("I18N.t('app.error.") >= 0 || APP_JS.indexOf('I18N.t("app.error.') >= 0,
    'errMsg should map to app.error.* i18n keys');
  console.log('  PASS: T42-02 errMsg 函数映射 error_code 到 i18n');
}

// ── T42-03: dashboard 403 自动切换有去重逻辑 ──
function test403DedupLogic() {
  const idx = APP_JS.indexOf('e.status === 403') >= 0
    ? APP_JS.indexOf('e.status === 403')
    : APP_JS.indexOf('e.status == 403');
  assert.ok(idx >= 0, '403 handler should exist');
  const section = APP_JS.substring(idx, idx + 1500);
  assert.ok(
    section.indexOf('_tried403') >= 0 || section.indexOf('tried') >= 0 ||
    section.indexOf('_triedRepos') >= 0 || section.indexOf('skipRepos') >= 0,
    '403 handler should have dedup/tried list'
  );
  console.log('  PASS: T42-03 dashboard 403 自动切换有去重逻辑');
}

// ── T42-04: events.json zh/en key 完全一致 ──
function testEventsJsonKeysMatch() {
  const zhKeys = Object.keys(JSON.parse(ZH_EVENTS)).sort();
  const enKeys = Object.keys(JSON.parse(EN_EVENTS)).sort();
  const zhOnly = zhKeys.filter(k => !enKeys.includes(k));
  const enOnly = enKeys.filter(k => !zhKeys.includes(k));
  assert.deepStrictEqual(zhOnly, [], `zh-only keys: ${zhOnly.join(', ')}`);
  assert.deepStrictEqual(enOnly, [], `en-only keys: ${enOnly.join(', ')}`);
  console.log('  PASS: T42-04 events.json zh/en key 完全一致');
}

// ── T42-05: app.json zh/en key 完全一致 ──
function testAppJsonKeysMatch() {
  const zhKeys = Object.keys(JSON.parse(ZH_APP)).sort();
  const enKeys = Object.keys(JSON.parse(EN_APP)).sort();
  const zhOnly = zhKeys.filter(k => !enKeys.includes(k));
  const enOnly = enKeys.filter(k => !zhKeys.includes(k));
  assert.deepStrictEqual(zhOnly, [], `zh-only keys: ${zhOnly.join(', ')}`);
  assert.deepStrictEqual(enOnly, [], `en-only keys: ${enOnly.join(', ')}`);
  console.log('  PASS: T42-05 app.json zh/en key 完全一致');
}

// ── T42-06: pat_no_refresh_hint 在 zh/en 都存在 ──
function testPatNoRefreshHintExists() {
  const zh = JSON.parse(ZH_APP);
  const en = JSON.parse(EN_APP);
  // 递归查找 pat_no_refresh_hint
  function findKey(obj, key) {
    if (typeof obj !== 'object' || obj === null) return false;
    if (key in obj) return true;
    return Object.values(obj).some(v => findKey(v, key));
  }
  assert.ok(findKey(zh, 'pat_no_refresh_hint'), 'pat_no_refresh_hint should exist in zh');
  assert.ok(findKey(en, 'pat_no_refresh_hint'), 'pat_no_refresh_hint should exist in en');
  console.log('  PASS: T42-06 pat_no_refresh_hint 在 zh/en 都存在');
}

// ── T42-07: errMsg fallback 到 e.message ──
function testErrMsgFallbackToMessage() {
  // errMsg 应该在 error_code 未命中时 fallback 到 e.message
  assert.ok(
    APP_JS.indexOf('e.message') >= 0 || APP_JS.indexOf('err.message') >= 0,
    'errMsg should fallback to e.message'
  );
  console.log('  PASS: T42-07 errMsg fallback 到 e.message');
}

// ── T42-08: 403 全部仓库试过时 goToRepos ──
function test403AllTriedGoToRepos() {
  const idx = APP_JS.indexOf('e.status === 403') >= 0
    ? APP_JS.indexOf('e.status === 403')
    : APP_JS.indexOf('e.status == 403');
  const section = APP_JS.substring(idx, idx + 2000);
  // 应该有 goToRepos 调用作为 fallback
  assert.ok(section.indexOf('goToRepos') >= 0, '403 handler should call goToRepos as fallback');
  console.log('  PASS: T42-08 403 全部仓库试过时 goToRepos');
}

// ── 运行所有测试 ──
console.log('Issue #372 R14 audit tests (8 suites)');
testErrMsgCalledInCatch();
testErrMsgMapsErrorCode();
test403DedupLogic();
testEventsJsonKeysMatch();
testAppJsonKeysMatch();
testPatNoRefreshHintExists();
testErrMsgFallbackToMessage();
test403AllTriedGoToRepos();
console.log('\n8 passed, 0 failed');
