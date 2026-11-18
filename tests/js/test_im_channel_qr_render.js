/**
 * IM 渠道扫码路径回归锁（2026-11-18）
 *
 * 背景：cc117a17b（六渠道版）把扫码两个入口写坏且无人发现：
 *   1. 飞书按钮调 `G.startFeishuOnboard(...)`，但该函数只是 ChatView 的 <script setup>
 *      局部函数，从没挂到 window → 点击即 TypeError，弹窗根本不出现；
 *   2. 微信读 `res.id`，后端返回的却是 `login_id` → 轮询立即 return，状态永远不动；
 *      同时把 `qrcode_img_content`（一个扫码页 **链接**）直接塞 `<img src>` → 破图；
 *   3. 六渠道新增的整段 CSS 被追加在 `</style>` 之后 → 全部样式失效（二维码框/引导列表
 *      都是裸排版）。
 * 本测试锁住：字段名、QRCode 渲染方式、飞书弹窗自持、以及 CSS 必须在 <style> 块内。
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const VIEW_PATH = path.join(ROOT, 'src', 'api', 'frontend', 'app', 'src', 'views', 'ImChannel.vue');
const SRC = fs.readFileSync(VIEW_PATH, 'utf8');

test('微信扫码：会话 id 读后端 login_id（不是 res.id）', function () {
  assert.ok(/res\.login_id \|\| res\.id/.test(SRC), '应读 login_id（兼容旧字段）');
  assert.ok(!/qr\.id = \(res && res\.id\)/.test(SRC), '不应只读 res.id');
});

test('微信二维码：链接必须经 QRCode 库渲染，不能塞进 <img src>', function () {
  assert.ok(/function renderQr\(/.test(SRC) && /new G\.QRCode\(/.test(SRC), '应有 renderQr 走 QRCode 库');
  assert.ok(/renderQr\(qr\.link, qrEl/.test(SRC), '微信二维码应渲染到 canvas 容器');
  assert.ok(!/<img[^>]+:src="qr\.img"/.test(SRC), '不应把扫码链接当图片地址');
});

test('微信状态文案走 i18n（wechat_* 映射），不回显英文状态码', function () {
  assert.ok(/WECHAT_STATUS_TEXT/.test(SRC), '应有状态→i18n 映射');
  assert.ok(/wechat_wait|wechat_scaned|wechat_confirmed/.test(SRC), '应使用既有 wechat_* 文案');
  assert.ok(!/G\.I18N\.t\('chat\.im_channel\.loading'\)/.test(SRC), '不应引用不存在的 loading key');
});

test('飞书扫码：IM 页自持弹窗，不再依赖全局 startFeishuOnboard', function () {
  assert.ok(/G\.API\.post\('\/feishu\/onboard\/start'/.test(SRC), '应直连 onboard/start');
  assert.ok(/\/feishu\/onboard\/status\?task_id=/.test(SRC), '应轮询 onboard/status');
  assert.ok(!/G\.startFeishuOnboard/.test(SRC), '不得调用未挂载的 G.startFeishuOnboard');
  assert.ok(/imc__ob-mask/.test(SRC), '应有扫码弹窗 DOM 与样式');
});

test('飞书扫码：409 询问重开 + 取消旧任务', function () {
  assert.ok(/e\.status === 409/.test(SRC), '409 应单独处理');
  assert.ok(/chat\.onboard\.confirm_restart/.test(SRC), '应询问是否重开');
  assert.ok(/\/feishu\/onboard\/cancel/.test(SRC), '应取消旧任务');
});

test('微信扫码：与飞书一致走弹窗（imc__ob-mask），不再内联在配置卡片里', function () {
  const masks = SRC.match(/class="imc__ob-mask"/g) || [];
  assert.strictEqual(masks.length, 2, '微信 + 飞书各一个弹窗遮罩');
  assert.ok(/v-if="qr\.open" class="imc__ob-mask"/.test(SRC), '微信弹窗由 qr.open 控制');
  assert.ok(!/class="imc__qr"/.test(SRC), '不应再有卡片内联的 imc__qr 区块');
  assert.ok(/imc__ob-pin/.test(SRC), 'PIN 输入在弹窗内');
  assert.ok(/qr\.expired/.test(SRC), '过期后应给出重新获取入口');
});

test('SFC 样式必须全部在 <style> 块内（防止再次落到 </style> 之后失效）', function () {
  const open = SRC.indexOf('<style');
  assert.ok(open >= 0, '应有 <style> 块');
  const close = SRC.indexOf('</style>', open);
  assert.ok(close > open, '应有 </style>');
  assert.strictEqual(SRC.indexOf('</style>', close + 1), -1, '不应有第二个 </style>');
  const after = SRC.slice(close + '</style>'.length).trim();
  assert.strictEqual(after, '', '</style> 之后不应再有内容（历史 bug：整段 CSS 在块外被丢弃）');
  assert.ok(SRC.slice(open, close).includes('.imc__ob-mask'), '弹窗样式应在 style 块内');
  assert.ok(SRC.slice(open, close).includes('.imc__ob-pin'), 'PIN 输入样式应在 style 块内');
});