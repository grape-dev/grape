/**
 * Issue #297 回归测试：Chat 多图发送错误提示与持久化（前端静态走查）
 *
 * 运行：node tests/js/test_issue_297_multi_image_error_and_persist.js
 *
 * 验证点：
 *   1. nginx-grape.conf：server 块含 client_max_body_size 50m（多图 base64 请求体放行）
 *   2. api.js：handleResp 非 ok 分支走 _safeErrJson（HTML/解析失败降级 null），
 *      通用错误回退 I18N.t('common.request_failed', {status: r.status}) 且 e.status=r.status
 *   3. chat.js：doSend catch 渲染 e.message / request_failed({status})，不再出现
 *      request_failed 无参 + 裸 e.message 拼接（Unexpected token '<' 入口）
 *   4. chat.js：多图/视频 submit 加长超时（120000），避免大请求体 30s 误超时
 *   5. locales zh/en：request_failed 均含 {{status}} 占位符
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');

function readFile(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

function testNginxBodySize() {
  const conf = readFile('nginx-grape.conf');
  assert.ok(
    /client_max_body_size\s+50m\s*;/.test(conf),
    'nginx-grape.conf 应含 client_max_body_size 50m;（多图 base64 ≈45MB+ 放行）'
  );
  console.log('  ✓ nginx-grape.conf client_max_body_size 50m');
}

function testApiJsSafeErrJson() {
  const js = readFile('src/api/static/js/api.js');

  // 降级函数存在：HTML 直接 null，json 解析失败也 null
  assert.ok(js.includes('function _safeErrJson(r)'), 'api.js 应定义 _safeErrJson(r)');
  assert.ok(
    /_safeErrJson[\s\S]{0,400}text\/html[\s\S]{0,200}Promise\.resolve\(null\)/.test(js) ||
    js.includes("ct.indexOf('text/html') !== -1) return Promise.resolve(null)"),
    '_safeErrJson 应对 text/html 响应直接返回 null（不 r.json()）'
  );
  assert.ok(
    /r\.json\(\)\.catch\(\(\) => null\)/.test(js),
    '_safeErrJson 应捕获 JSON 解析异常（Unexpected token \'<\'）返回 null'
  );

  // handleResp 非 ok 通用分支使用 _safeErrJson + {status: r.status} 回退
  assert.ok(
    js.includes("I18N.t('common.request_failed', {status: r.status})"),
    'handleResp 通用错误回退应传 {status: r.status}'
  );
  assert.ok(
    js.includes('e.status = r.status'),
    'handleResp 抛出的错误应带 e.status'
  );
  // 非 ok 三个分支均不再裸用 r.json()
  const handleRespSrc = js.slice(js.indexOf('function handleResp'), js.indexOf('// Issue #52'));
  assert.ok(
    !handleRespSrc.includes('return r.json().then(err'),
    'handleResp 非 ok 分支不应再裸 r.json()（HTML 错误页会抛 Unexpected token）'
  );
  assert.ok(
    (handleRespSrc.match(/_safeErrJson\(r\)/g) || []).length >= 3,
    'handleResp 三个非 ok 分支（401+retry/401/通用）均应使用 _safeErrJson'
  );
  console.log('  ✓ api.js handleResp 非 JSON 错误降级');
}

function testChatJsSendErrorRender() {
  const js = readFile('src/api/static/js/pages/chat.js');

  // 旧模式必须移除：无参 request_failed + 裸 e.message 拼接（{{status}} 未渲染 + 异常串入提示）
  assert.ok(
    !js.includes("I18N.t('common.request_failed') + (e.message || '')"),
    'doSend catch 不应再拼接无参 request_failed + 裸 e.message（{{status}} 不渲染根因）'
  );
  // 新模式：status 渲染链路
  assert.ok(
    /I18N\.t\('common\.request_failed',\s*\{status:\s*\(e\s*&&\s*e\.status\)\s*\|\|\s*''\}\)/.test(js) ||
    /I18N\.t\('common\.request_failed',\s*\{status:\s*e\.status/.test(js),
    'doSend catch 应以 {status: e.status} 渲染 request_failed 兜底'
  );
  console.log('  ✓ chat.js doSend 错误提示 status 渲染');
}

function testChatJsSubmitTimeout() {
  const js = readFile('src/api/static/js/pages/chat.js');
  assert.ok(
    /hasMedia\s*\?+\s*120000/.test(js),
    '多图/视频 submit 应传加长超时（120000ms），避免大请求体 30s 误超时'
  );
  console.log('  ✓ chat.js 多图 submit 超时 120s');
}

function testLocalesPlaceholder() {
  for (const lang of ['zh', 'en']) {
    const json = JSON.parse(readFile(`src/api/static/locales/${lang}/common.json`));
    // common.json 为扁平结构（i18n 的 'common.' 前缀映射到该文件）
    assert.ok(
      /\{\{status\}\}/.test(json.request_failed),
      `${lang} request_failed 应含 {{status}} 占位符`
    );
  }
  console.log('  ✓ locales zh/en request_failed {{status}} 占位符');
}

// 行为级验证：从 api.js 提取 _safeErrJson + handleResp 实源，在沙箱中执行
function testHandleRespBehavior() {
  const js = readFile('src/api/static/js/api.js');
  const start = js.indexOf('function _safeErrJson(r)');
  const end = js.indexOf('// Issue #52');
  assert.ok(start > 0 && end > start, 'api.js 应含 _safeErrJson 定义');
  const src = js.slice(start, end);
  const I18N = { t: (k, v) => `请求失败 (${v && v.status})` };
  const noop = () => {};
  const handleResp = new Function('I18N', 'localStorage', 'location', 'window', 'app',
    src + '\nreturn handleResp;')(I18N, { removeItem: noop }, { pathname: '/' }, {}, { showLogin: noop });

  // 场景 1：nginx 413 HTML 错误页（r.json() 必抛 Unexpected token）→ 降级 request_failed({status})
  const htmlResp = {
    ok: false, status: 413,
    headers: { get: () => 'text/html; charset=utf-8' },
    json: () => Promise.reject(new SyntaxError("Unexpected token '<' in JSON")),
  };
  // 场景 2：正常 JSON 错误响应 → detail.message 透传 + e.status
  const jsonResp = {
    ok: false, status: 500,
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve({ detail: { message: 'boom' } }),
  };

  return Promise.all([
    handleResp(htmlResp).then(
      () => { throw new Error('HTML 413 响应应 reject'); },
      e => {
        assert.strictEqual(e.status, 413, 'e.status 应为 413');
        assert.ok(e.message.includes('413'), `提示应含状态码 413: ${e.message}`);
        assert.ok(!e.message.includes('Unexpected token'), `不应含裸 parse 异常: ${e.message}`);
      }
    ),
    handleResp(jsonResp).then(
      () => { throw new Error('JSON 500 响应应 reject'); },
      e => {
        assert.strictEqual(e.message, 'boom');
        assert.strictEqual(e.status, 500);
      }
    ),
  ]).then(() => console.log('  ✓ handleResp 行为：413 HTML 降级 / JSON detail 透传'));
}

async function main() {
  console.log('Issue #297: Chat 多图发送错误 + F5 持久化（前端/部署层）');
  testNginxBodySize();
  testApiJsSafeErrJson();
  testChatJsSendErrorRender();
  testChatJsSubmitTimeout();
  testLocalesPlaceholder();
  await testHandleRespBehavior();
  console.log('\n全部通过 ✓');
}

main();
