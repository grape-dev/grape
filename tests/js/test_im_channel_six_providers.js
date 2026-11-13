/**
 * IM 渠道页六平台对齐（2026-11-13，合入 arch-weekly-11 后补齐）
 *
 * 背景：Vue 迁移时页面停在「飞书可用 / 企微钉钉即将支持」的 3 卡旧版，
 * 后端早已支持 6 个渠道（Type 枚举 + adapters/）。本测试锁住：
 *   T1. 页面 6 张卡（feishu/wecom/dingtalk/slack/telegram/wechat），且不再有硬编码 disabled
 *   T2. 字段表按平台区分（telegram 单 Token；slack 双 Token；wechat 只读+扫码）
 *   T3. 保存载荷走新形态 {im:{provider, app_id, app_secret}}
 *   T4. 徽标由 /im/installations 活状态驱动（connected / configured / none）
 *   T5. 每平台有接入指引（4 步 + tip + 后台链接），Slack 另有 App 清单复制
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const SRC = fs.readFileSync(
  path.join(ROOT, 'src', 'api', 'frontend', 'app', 'src', 'views', 'ImChannel.vue'),
  'utf8'
);
const BACKEND_TYPES = fs.readFileSync(
  path.join(ROOT, 'src', 'domains', 'runtime', 'im', 'channel', 'types.py'),
  'utf8'
);

test('T1 六张卡 + 与后端 Type 枚举一致 + 无硬编码 disabled', () => {
  const platformBlock = SRC.slice(SRC.indexOf('const platforms = {'), SRC.indexOf('for (const k of Object.keys(platforms))'));
  for (const p of ['feishu', 'wecom', 'dingtalk', 'slack', 'telegram', 'wechat']) {
    assert.ok(platformBlock.includes(p + ':'), `页面缺 ${p} 卡片`);
    assert.ok(BACKEND_TYPES.toUpperCase().includes(p.toUpperCase()), `后端 Type 枚举措缺 ${p}`);
  }
  assert.ok(!/disabled:\s*true/.test(SRC), '仍有硬编码 disabled:true');
});

test('T2 字段表按平台区分', () => {
  for (const p of ['feishu', 'wecom', 'dingtalk', 'slack', 'telegram', 'wechat']) {
    assert.match(SRC, new RegExp('\\n\\s{2}' + p + ': \\['), `FIELDS 缺 ${p}`);
  }
  const telegramBlock = SRC.slice(SRC.indexOf('  telegram: ['), SRC.indexOf('  wechat: ['));
  assert.equal((telegramBlock.match(/key: 'app_id'/g) || []).length + (telegramBlock.match(/key: 'app_secret'/g) || []).length, 1,
    'Telegram 应为单 Token 字段');
  const slackBlock = SRC.slice(SRC.indexOf('  slack: ['), SRC.indexOf('  telegram: ['));
  assert.ok(slackBlock.includes("key: 'app_secret'"), 'Slack 应为双 Token');
  const wechatBlock = SRC.slice(SRC.indexOf('  wechat: ['), SRC.indexOf('// 各平台接入后台地址'));
  assert.ok(wechatBlock.includes('readonly: true'), '微信凭据应为扫码写入（只读）');
});

test('T3 保存载荷走新形态 im={provider,app_id,app_secret}', () => {
  assert.match(SRC, /im: \{ provider: current\.value, app_id: form\.app_id, app_secret: secret \}/,
    '保存载荷不是新形态');
  assert.match(SRC, /im: \{ provider: current\.value, app_id: '', app_secret: '' \}/, '解绑载荷不对');
});

test('T4 徽标由 /im/installations 活状态驱动', () => {
  assert.match(SRC, /G\.API\.get\('\/im\/installations' \+ qs\(\)\)/, '未拉取安装状态');
  assert.match(SRC, /it\.connected/, '未消费 connected');
  assert.match(SRC, /badgeOf\(key\)/, '模板未用 badgeOf');
  assert.match(SRC, /badge_none|badge_configured_offline/, '缺少未接入/已配置徽标键');
});

test('T5 每平台接入指引 + Slack App 清单', () => {
  assert.match(SRC, /guide_' \+ key \+ '_s' \+ i/, '指引步骤未按平台拼键');
  assert.match(SRC, /GUIDE_URLS = \{/, '缺少各平台后台地址');
  for (const u of ['open.feishu.cn', 'wework_admin', 'open-dev.dingtalk.com', 'api.slack.com', 't.me/BotFather']) {
    assert.ok(SRC.includes(u), `GUIDE_URLS 缺 ${u}`);
  }
  assert.match(SRC, /const SLACK_MANIFEST = \{/, '缺 Slack App 清单');
  assert.match(SRC, /socket_mode_enabled: true/, 'Slack 清单未开 Socket Mode');
  assert.match(SRC, /copyManifest/, '缺清单复制动作');
});

test('T5b 微信扫码绑定走 ilink 接口', () => {
  assert.match(SRC, /\/chat\/im\/wechat\/login\/start/, '缺扫码启动接口');
  assert.match(SRC, /\/chat\/im\/wechat\/login\/status/, '缺扫码状态轮询');
  assert.match(SRC, /need_verifycode/, '缺 PIN 验证码分支');
});