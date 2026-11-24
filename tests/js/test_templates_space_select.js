/*
 * 一键创建（模板页）支持「所属空间」选择 + 表单布局（源码级断言）
 *
 * 背景：项目创建有「所属空间」（个人空间 / 组路径），一键创建缺这一项。
 *   1. 弹窗内有空间下拉，且选项来自 /repos/create-targets（与 ReposView 同源）
 *   2. 换平台要重置空间并重新拉取（路径属于具体实例）
 *   3. 创建请求体带 owner；同名检查带 provider + owner（否则组下同名仓判错）
 *   4. 参数区改为标签在上、控件在下的栅格（窄屏自动单列），不再塞在一行 footer
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/TemplatesView.vue'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('弹窗含所属空间下拉（个人空间 + 组路径选项）', function () {
  assert.ok(/id="tvSpace"/.test(VIEW), '应有 tvSpace 下拉');
  assert.ok(/v-model="modal\.space"/.test(VIEW), '空间应绑定 modal.space');
  assert.ok(/repos\.create\.space_personal/.test(VIEW), '应有「个人空间」选项文案');
  assert.ok(/v-for="sp in modal\.spaces"/.test(VIEW), '空间选项应来自 modal.spaces');
});

check('空间清单来自 /repos/create-targets 且换平台重置', function () {
  assert.ok(/\/repos\/create-targets\?provider=/.test(VIEW), '应调用 create-targets');
  assert.ok(/function onProviderChange/.test(VIEW), '应有平台切换处理');
  assert.ok(/function onProviderChange\(\) \{[\s\S]{0,200}modal\.space = ''/.test(VIEW), '换平台必须清空已选空间');
  assert.ok(/modal\.provider !== asked/.test(VIEW), '迟到响应应按平台丢弃（竞态守卫）');
});

check('可见性（私有/公开）可在弹窗选择并随请求下发', function () {
  assert.ok(/id="tvPrivate"/.test(VIEW), '应有可见性下拉');
  assert.ok(/v-model="modal\.private"/.test(VIEW), '可见性应绑定 modal.private');
  assert.ok(/repos\.create\.vis_public/.test(VIEW) && /repos\.create\.vis_private/.test(VIEW), '应有公开/私有两个选项');
  assert.ok(/private: !!modal\.private/.test(VIEW), '创建请求应带 private');
});

check('创建请求体与同名检查都带空间', function () {
  assert.ok(/repo_name: repoName, provider, owner: \(modal\.space \|\| ''\)\.trim\(\)/.test(VIEW), '创建请求应带 owner');
  assert.ok(/'&owner=' \+ encodeURIComponent\(modal\.space \|\| ''\)/.test(VIEW), '同名检查应带 owner');
  assert.ok(/'&provider=' \+ encodeURIComponent\(modal\.provider \|\| ''\)/.test(VIEW), '同名检查应带 provider');
});

check('两行布局：平台+空间一行、仓库名+按钮一行', function () {
  assert.ok(/class="tv-create-row"/.test(VIEW), '应有第二行容器');
  assert.ok(/\.tv-create-row \{[\s\S]{0,80}display: flex/.test(VIEW), '第二行应横向排布');
  assert.ok(/\.tv-field-grow \{[\s\S]{0,40}flex: 1/.test(VIEW), '仓库名应自适应占满剩余宽度');
  const form = VIEW.slice(VIEW.indexOf('class="tv-create-form"'), VIEW.indexOf('class="tv-create-row"'));
  assert.ok(!/tvRepoName/.test(form), '仓库名不应留在平台/空间那行');
  assert.ok(/tvProvider/.test(form) && /tvSpace/.test(form), '平台与空间应在同一行');
});

check('Apple 风：分组卡 + 行内标签 + pill 按钮 + 窄屏折叠', function () {
  assert.ok(/\.tv-create-form \{[\s\S]{0,120}grid-template-columns: repeat\(auto-fit, minmax\(300px, 1fr\)\)/.test(VIEW),
    '平台/空间应为分组栅格');
  assert.ok(/\.tv-field \{[\s\S]{0,120}align-items: center/.test(VIEW), '每行应「标签左、控件右」');
  assert.ok(/\.tv-create-form,[\s\S]{0,120}border-radius: 14px/.test(VIEW), '分组卡应有圆角与 hairline 边框');
  assert.ok(/\.tv-modal-foot \.bt \{[\s\S]{0,80}border-radius: 980px/.test(VIEW), '按钮应为 pill 形状');
  assert.ok(/@media \(max-width: 640px\)/.test(VIEW), '窄屏应有折叠断点');
  assert.ok(/-apple-system, BlinkMacSystemFont/.test(VIEW), '应使用系统字体栈（Apple 风格）');
});

console.log('\n' + passed + ' 组断言全部通过');