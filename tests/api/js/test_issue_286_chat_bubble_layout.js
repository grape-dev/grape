/**
 * Issue #286 回归测试：Chat 对话框气泡左右位置调整
 *
 * 运行：node tests/js/test_issue_286_chat_bubble_layout.js
 *
 * 验证点：
 *   1. chat.js 历史会话加载：用户消息渲染为 msg r（右侧），agent 消息为 msg l（左侧）
 *   2. chat.js 实时发送：用户消息 msg r，agent 占位/回复 msg l
 *   3. chat.js renderChatMessages：同上（历史加载不回退）
 *   4. chat.js 任务恢复（switchConversation/resumeChatTask）占位符为 msg l
 *   5. app.css：.msg.r 为蓝色右对齐（margin-left:auto，背景 var(--a)），尾角右下
 *   6. app.css：.msg.l 为浅色左对齐（背景 var(--bg)），尾角左下
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..', '..');

function readFile(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

function testChatJsUserRight() {
  const js = readFile('src/api/static/js/pages/chat.js');

  // 历史会话加载（switchConversation 内联模板）
  assert.ok(
    js.includes("m.role === 'user' ? 'r' : 'l'"),
    '历史会话加载：用户消息应渲染为 msg r，agent 为 msg l'
  );
  console.log('  ✓ 历史会话加载：用户 → msg r，agent → msg l');

  // 实时发送：用户消息（#442 后统一走 _userMsgHtml 助手，带 data-msg-id + 撤回入口）
  assert.ok(/_userMsgHtml\(/.test(js), '实时发送应通过 _userMsgHtml 渲染用户气泡');
  assert.ok(/class=\\?"msg r/.test(js), '实时发送：用户气泡 class 应为 msg r');
  console.log('  ✓ 实时发送：用户消息 → _userMsgHtml → msg r');

  // renderChatMessages：用户消息（同样走 _userMsgHtml 助手）
  const renderIdx = js.indexOf('function renderChatMessages');
  assert.ok(renderIdx > -1, '应存在 renderChatMessages');
  const renderBody = js.substring(renderIdx, renderIdx + 4000);
  assert.ok(/_userMsgHtml\(/.test(renderBody), 'renderChatMessages 应通过 _userMsgHtml 渲染用户气泡');
  console.log('  ✓ renderChatMessages：用户消息 → _userMsgHtml → msg r');
}

function testChatJsAgentLeft() {
  const js = readFile('src/api/static/js/pages/chat.js');

  // renderChatMessages：agent 消息
  assert.ok(
    js.includes("'<div class=\"msg l\">' + renderMarkdown(content)"),
    'renderChatMessages：agent 消息应为 msg l'
  );
  console.log('  ✓ renderChatMessages：agent 消息 → msg l');

  // agent 占位气泡（发送后/任务恢复）不应再使用 msg r。
  // 精确判定：带 id 的 msg r 只允许出现在「用户气泡」构造点（变量名含 user/userBubble），
  // agent 占位用的是 elId / tid 这类运行时 id。
  const rIds = [...js.matchAll(/class="msg r" id="' \+ ([A-Za-z_$][\w$]*)/g)].map((m) => m[1]);
  assert.ok(rIds.length >= 1, '应存在用户气泡 msg r 构造点');
  rIds.forEach((v) => {
    assert.ok(/user/i.test(v), 'msg r 构造点应用于用户气泡，实际变量 ' + v);
  });
  const lIds = [...js.matchAll(/class="msg l" id="' \+ ([A-Za-z_$][\w$]*)/g)].map((m) => m[1]);
  assert.ok(lIds.length >= 2, '应存在多处 agent 占位气泡（msg l）构造点');
  console.log('  ✓ agent 占位气泡（' + lIds.length + ' 处）全部为 msg l；用户气泡仅 msg r');
}

function testAppCss() {
  const css = readFile('src/api/static/css/app.css');

  // 用户气泡：蓝色、右对齐、尾角右下
  const rMatch = css.match(/\.chat-body \.msg\.r\{([^}]*)\}/);
  assert.ok(rMatch, 'app.css 缺少 .chat-body .msg.r 气泡规则');
  assert.ok(rMatch[1].includes('background:var(--a)'), '用户气泡应为蓝色背景 var(--a)');
  assert.ok(rMatch[1].includes('margin-left:auto'), '用户气泡应右对齐（margin-left:auto）');
  assert.ok(rMatch[1].includes('border-radius:10px 10px 2px 10px'), '用户气泡尾角应在右下');
  console.log('  ✓ .msg.r：蓝色右对齐气泡，尾角右下');

  // agent 气泡：浅色、左对齐、尾角左下
  const lMatch = css.match(/\.chat-body \.msg\.l\{([^}]*)\}/);
  assert.ok(lMatch, 'app.css 缺少 .chat-body .msg.l 气泡规则');
  assert.ok(lMatch[1].includes('background:var(--bg)'), 'agent 气泡应为浅色背景 var(--bg)');
  assert.ok(!lMatch[1].includes('margin-left:auto'), 'agent 气泡应默认左对齐');
  assert.ok(lMatch[1].includes('border-radius:10px 10px 10px 2px'), 'agent 气泡尾角应在左下');
  console.log('  ✓ .msg.l：浅色左对齐气泡，尾角左下');
}

// ── 执行 ──
console.log('Issue #286: Chat 对话框气泡左右位置调整 — 测试开始\n');

console.log('[1] chat.js 用户消息右侧');
testChatJsUserRight();

console.log('\n[2] chat.js agent 消息左侧');
testChatJsAgentLeft();

console.log('\n[3] app.css 气泡样式');
testAppCss();

console.log('\n✅ 全部测试通过（3/3）');
