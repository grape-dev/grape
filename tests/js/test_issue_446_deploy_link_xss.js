/*
 * Issue #446 回归测试：deploy link XSS 防护 + 触屏编辑入口
 *
 * 运行：node tests/js/test_issue_446_deploy_link_xss.js
 *
 * 验证点：
 *   1. issues.js 包含 _escapeHtml 转义函数
 *   2. _renderDeployLink 中 href/title/value 均使用 _escapeHtml(_deployUrl) 转义
 *   3. _saveDeployUrl 包含客户端 URL 格式校验（http(s):// 前缀）
 *   4. 存在长按编辑入口（_deployLongPressStart/_deployLongPressEnd）供触屏设备使用
 *   5. index.html 中 issues.js 版本号带 defer 属性
 *   6. routes/repos.py 服务端校验 deploy_url http(s) 前缀 + 长度限制
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');

function readFile(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

const issuesJs = readFile('src/api/static/js/pages/issues.js');

assert.ok(issuesJs.includes('function _escapeHtml(s)'), 'issues.js 应包含 _escapeHtml 转义函数');
assert.ok(issuesJs.includes('value="${_escapeHtml(_deployUrl)}"'), 'input value 应使用 _escapeHtml 转义');
assert.ok(issuesJs.includes('const safeUrl = _escapeHtml(_deployUrl)'), '应创建 safeUrl 变量');
assert.ok(issuesJs.includes('href="${safeUrl}"'), 'href 属性应使用转义后的 safeUrl');
assert.ok(issuesJs.includes('title="${safeUrl}"'), 'title 属性应使用转义后的 safeUrl');

assert.ok(issuesJs.includes('function _isValidDeployUrl(url)'), '应包含 _isValidDeployUrl 校验函数');
assert.ok(issuesJs.includes("url.startsWith('http://')") && issuesJs.includes("url.startsWith('https://')"), '应校验 http(s) 前缀');
assert.ok(issuesJs.includes('if (url && !_isValidDeployUrl(url))'), '_saveDeployUrl 应在提交前校验 URL 格式');

assert.ok(issuesJs.includes('window._deployLongPressStart'), '应暴露 _deployLongPressStart');
assert.ok(issuesJs.includes('window._deployLongPressEnd'), '应暴露 _deployLongPressEnd');
assert.ok(issuesJs.includes('ontouchstart="_deployLongPressStart()"'), '应绑定 ontouchstart');
assert.ok(issuesJs.includes('ontouchend="_deployLongPressEnd()"'), '应绑定 ontouchend');

const indexHtml = readFile('src/api/static/index.html');
const issuesScriptMatch = indexHtml.match(/<script src="\/static\/js\/pages\/issues\.js\?v=(\d+)"\s+defer><\/script>/);
assert.ok(issuesScriptMatch, 'issues.js script 标签应带 defer 属性');
assert.ok(parseInt(issuesScriptMatch[1]) >= 120, 'issues.js 版本号应 >= 120');

const reposPy = readFile('src/api/routes/repos.py');
assert.ok(reposPy.includes('def _validate_deploy_url(url: str)'), 'repos.py 应包含 _validate_deploy_url');
assert.ok(reposPy.includes('url.startswith("http://")') && reposPy.includes('url.startswith("https://")'), '应校验 http(s) 前缀');
assert.ok(reposPy.includes('len(url) > 512'), '应校验 URL 长度 <= 512');
assert.ok(reposPy.includes('deploy_url = _validate_deploy_url(body.deploy_url)'), 'set_deploy_url 应调用校验');

console.log('All assertions passed: deploy link XSS prevention + touch edit entry');
