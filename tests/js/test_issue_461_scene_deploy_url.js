/**
 * Issue #461: 项目场景详情页「复制场景」按钮前的地球仪图标（登记场景访问地址）
 *
 * 运行：node tests/js/test_issue_461_scene_deploy_url.js
 *
 * 验证点：
 * 1. _renderLocalSceneDetailPage 头部：地球仪图标容器出现在「复制场景」按钮之前
 * 2. 详情页渲染时调用 _loadSceneDeployUrl(scene.id, owner, name)
 * 3. 三态渲染：未登记灰 globe（点击/右击编辑）/ 已登记绿 globe 链接（XSS 转义）/ 编辑输入框
 * 4. XSS 防护沿用 #446：_skillEsc 转义 href/title/value；_isValidSceneDeployUrl 校验 http(s) 前缀
 * 5. 长按编辑入口（触屏 600ms）与需求中心一致
 * 6. 保存走场景级端点 PUT /scenes/{scene_id}/deploy-url（非 /repos/deploy-url 需求中心端点）
 * 7. i18n 文案 key 中英齐全
 * 8. index.html dashboard.js 缓存版本 bump
 * 9. 需求中心 issues.js 零改动（回归保护）
 * 10. 语法完整（Function 构造可解析）
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '../..');
const dashSrc = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/dashboard.js'), 'utf8').replace(/\r/g, '');
const issuesSrc = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/issues.js'), 'utf8').replace(/\r/g, '');
const indexHtml = fs.readFileSync(path.join(ROOT, 'src/api/static/index.html'), 'utf8');
const zhJson = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/zh/dashboard.json'), 'utf8'));
const enJson = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/en/dashboard.json'), 'utf8'));

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

function detailSection() {
  const start = dashSrc.indexOf('function _renderLocalSceneDetailPage');
  assert(start >= 0, '_renderLocalSceneDetailPage 未找到');
  const end = dashSrc.indexOf('\nfunction _loadAdvancedOptionsInline', start);
  assert(end > start, '详情页渲染函数边界未找到');
  return dashSrc.slice(start, end);
}

console.log('Issue #461 场景详情页地球仪图标（登记场景访问地址）— 前端回归测试\n');

test('dashboard.js 语法完整', () => {
  new Function(dashSrc);
});

test('地球仪图标容器出现在「复制场景」按钮之前', () => {
  const section = detailSection();
  const globeIdx = section.indexOf('id="sceneDeployGlobe"');
  const copyIdx = section.indexOf("_copySceneFromDetail()");
  assert(globeIdx >= 0, '缺失 #sceneDeployGlobe 容器');
  assert(copyIdx >= 0, '缺失复制场景按钮');
  assert(globeIdx < copyIdx, '地球仪图标应在复制场景按钮之前');
});

test('详情页渲染时调用 _loadSceneDeployUrl(scene.id, owner, name)', () => {
  const section = detailSection();
  assert(section.includes('_loadSceneDeployUrl(scene.id, owner, name)'),
    '应在渲染后加载场景级访问地址');
});

test('_loadSceneDeployUrl 定义于全局（含 stale guard）', () => {
  assert(dashSrc.includes('function _loadSceneDeployUrl('), '_loadSceneDeployUrl 未定义');
  const start = dashSrc.indexOf('function _loadSceneDeployUrl(');
  const end = dashSrc.indexOf('\nfunction _renderSceneDeployGlobe', start);
  const section = dashSrc.slice(start, end);
  assert(section.includes("API.get('/scenes/'"), '应请求场景级端点');
  assert(section.includes('/deploy-url?repo='), '应带 repo 查询参数');
  assert(section.includes('encodeURIComponent'), '应 encodeURIComponent sceneId/repo');
  // stale guard：切换场景后丢弃过期响应
  assert(/_currentSceneDeployCtx\.sceneId !== sceneId/.test(section), '应有 stale guard');
});

test('三态渲染：未登记灰 globe / 已登记绿链接 / 编辑输入框', () => {
  const start = dashSrc.indexOf('function _renderSceneDeployGlobe');
  assert(start >= 0, '_renderSceneDeployGlobe 未定义');
  const end = dashSrc.indexOf('window._sceneDeployLongPressStart', start);
  const section = dashSrc.slice(start, end);
  assert(section.includes('sceneDeployUrlInput'), '编辑态应有输入框');
  assert(section.includes('_saveSceneDeployUrl()'), '编辑态应有保存按钮');
  assert(/target="_blank" rel="noopener"/.test(section), '已登记态应为新窗链接');
  assert(section.includes('opacity=.7'), '未登记态应为灰色半透明');
  assert(section.includes('🌐'), '应使用地球仪 emoji');
});

test('XSS 防护：href/title/value 均经 _skillEsc 转义（沿用 #446）', () => {
  const start = dashSrc.indexOf('function _renderSceneDeployGlobe');
  const end = dashSrc.indexOf('window._sceneDeployLongPressStart', start);
  const section = dashSrc.slice(start, end);
  assert(section.includes('value="${_skillEsc(_sceneDeployUrl)}"'), 'input value 应转义');
  assert(section.includes('var safeUrl = _skillEsc(_sceneDeployUrl)'), '应创建 safeUrl 变量');
  assert(section.includes('href="${safeUrl}"'), 'href 应使用转义后 safeUrl');
  assert(section.includes('title="${safeUrl}"'), 'title 应使用转义后 safeUrl');
});

test('校验：_isValidSceneDeployUrl http(s) 前缀（与需求中心一致）', () => {
  const start = dashSrc.indexOf('function _isValidSceneDeployUrl');
  assert(start >= 0, '_isValidSceneDeployUrl 未定义');
  const end = dashSrc.indexOf('\nfunction _loadSceneDeployUrl', start);
  const section = dashSrc.slice(start, end);
  assert(section.includes("indexOf('http://') === 0"), '应校验 http:// 前缀');
  assert(section.includes("indexOf('https://') === 0"), '应校验 https:// 前缀');
  // 沙箱执行行为断言
  const sandbox = new Function(section + '\nreturn _isValidSceneDeployUrl;')();
  assert.strictEqual(sandbox(''), true, '空串应允许（清除）');
  assert.strictEqual(sandbox('http://1.2.3.4:5000'), true, 'http:// 应通过');
  assert.strictEqual(sandbox('https://example.com'), true, 'https:// 应通过');
  assert.strictEqual(sandbox('javascript:alert(1)'), false, 'javascript: 应拒绝');
  assert.strictEqual(sandbox('x" onmouseover="alert(1)'), false, '引号注入应拒绝');
});

test('保存走场景级端点（非需求中心 /repos/deploy-url）', () => {
  const start = dashSrc.indexOf('window._saveSceneDeployUrl');
  const end = dashSrc.indexOf('\n// ═', start) > 0 ? dashSrc.indexOf('\n// ═', start) : dashSrc.indexOf('\n};', start) + 3;
  const section = dashSrc.slice(start, end);
  assert(section.includes("API.put('/scenes/'"), '应 PUT /scenes/{id}/deploy-url');
  assert(section.includes('/deploy-url?repo='), '应带 repo 参数');
  assert(!section.includes("/repos/deploy-url"), '不应误用需求中心端点');
  assert(section.includes('_isValidSceneDeployUrl(url)'), '保存前应校验 URL 格式');
  assert(section.includes('deploy_url: url'), 'body 应含 deploy_url 字段');
});

test('长按编辑入口（触屏 600ms，与需求中心一致）', () => {
  assert(dashSrc.includes('window._sceneDeployLongPressStart'), '应暴露 _sceneDeployLongPressStart');
  assert(dashSrc.includes('window._sceneDeployLongPressEnd'), '应暴露 _sceneDeployLongPressEnd');
  const start = dashSrc.indexOf('function _renderSceneDeployGlobe');
  const end = dashSrc.indexOf('window._sceneDeployLongPressStart', start);
  const section = dashSrc.slice(start, end);
  assert(section.includes('ontouchstart="_sceneDeployLongPressStart()"'), '应绑定 ontouchstart');
  assert(section.includes('ontouchend="_sceneDeployLongPressEnd()"'), '应绑定 ontouchend');
  const lpStart = dashSrc.indexOf('window._sceneDeployLongPressStart');
  const lpEnd = dashSrc.indexOf('window._sceneDeployLongPressEnd', lpStart);
  const lpSection = dashSrc.slice(lpStart, lpEnd);
  assert(lpSection.includes('600'), '长按时长应为 600ms（与需求中心一致）');
});

test('i18n 文案 key 中英齐全', () => {
  for (const k of ['deploy_url_register', 'deploy_url_edit_title', 'deploy_url_saved',
                   'deploy_url_save_failed', 'deploy_url_invalid']) {
    assert(zhJson[k], `zh/dashboard.json 缺失 ${k}`);
    assert(enJson[k], `en/dashboard.json 缺失 ${k}`);
  }
  assert(String(zhJson.deploy_url_save_failed).includes('{{msg}}'), 'zh deploy_url_save_failed 应含 {{msg}}');
  assert(String(enJson.deploy_url_save_failed).includes('{{msg}}'), 'en deploy_url_save_failed 应含 {{msg}}');
});

test('index.html dashboard.js 缓存版本已 bump（>= 99）', () => {
  const m = indexHtml.match(/<script src="\/static\/js\/pages\/dashboard\.js\?v=(\d+)"\s+defer><\/script>/);
  assert(m, 'dashboard.js script 标签应带 defer');
  assert(parseInt(m[1]) >= 99, `dashboard.js 版本应 >= 99，实际 ${m[1]}`);
});

test('需求中心 issues.js 零改动（deploy-url 逻辑保持原样）', () => {
  // 需求中心仍使用仓库级端点与本文件内既有函数
  assert(issuesSrc.includes("API.put('/repos/deploy-url'"), 'issues.js 需求中心保存路径不应被改动');
  assert(issuesSrc.includes('function _escapeHtml(s)'), 'issues.js _escapeHtml 不应被改动');
  assert(issuesSrc.includes('function _isValidDeployUrl(url)'), 'issues.js _isValidDeployUrl 不应被改动');
  assert(!issuesSrc.includes('sceneDeployGlobe'), 'issues.js 不应包含场景级 globe 逻辑');
});

test('复制场景按钮保持原有 onclick（无回归）', () => {
  const section = detailSection();
  const btnLine = section.split('\n').find(l => l.includes('_copySceneFromDetail()') && l.includes('<button'));
  assert(btnLine, '复制场景按钮应存在');
  assert(btnLine.includes("onclick=\"_copySceneFromDetail()\""), '复制场景按钮 onclick 不应被改动');
});

console.log(`\n结果: ${passed} 通过, ${failed} 失败`);
process.exit(failed ? 1 : 0);
