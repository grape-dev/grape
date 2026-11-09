/**
 * Issue #464: 消息撤回前端可达性与可读性（CSS 静态回归测试）。
 *
 * 覆盖：
 * 1. hover 桥接：.msg.r 伪元素命中桥覆盖气泡-按钮间隙，默认不拦截、hover 态激活
 * 2. 撤回提示条「重新编辑」链接配色覆盖（specificity 高于 #233 白色全局规则）
 * 3. #442 语义回归锚点：hover 显示撤回按钮 / recalled 隐藏撤回按钮规则仍在
 *
 * 运行：node tests/frontend/test_chat_recall_464.js
 * 零依赖：node:test / node:assert / node:fs。
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const CSS = fs.readFileSync(
  path.join(ROOT, 'src', 'api', 'static', 'css', 'app.css'),
  'utf8'
);
// Issue #464 QA：apple-theme.css 后加载且默认主题为 apple，必须双文件层叠断言
const APPLE_CSS = fs.readFileSync(
  path.join(ROOT, 'src', 'api', 'static', 'css', 'apple-theme.css'),
  'utf8'
);

function norm(s) { return String(s).replace(/\s+/g, ' ').trim(); }
function findRule(css, selector) {
  const body = css.replace(/\s+/g, ' ');
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  // 单选择器直接命中声明块
  let m = new RegExp(esc + '\\s*\\{([^}]*)\\}').exec(body);
  if (m) return norm(m[1]);
  // 选择器列表中的成员：selector, ... { decl }
  m = new RegExp(esc + '\\s*,[^{}]*\\{([^}]*)\\}').exec(body);
  return m ? norm(m[1]) : null;
}
function rule(selector) { return findRule(CSS, selector); }

// CSS 特异性 (a, b, c)：id 数 / class+attr+pseudo-class 数 / type+pseudo-element 数
function specificity(selector) {
  const s = selector.replace(/:[a-z-]+\(/g, ':'); // 归一化伪类函数参数
  const ids = (s.match(/#[\w-]+/g) || []).length;
  const classes = (s.match(/\.[\w-]+|\[[^\]]+\]|(?<!\\):(?![a-z-]+-element)[\w-]+/g) || []).length;
  const types = (s.match(/(^|[\s>+~])[a-z][\w-]*|::[\w-]+/g) || []).length;
  return [ids, classes, types];
}
function specGte(x, y) {
  return x[0] > y[0] || (x[0] === y[0] && (x[1] > y[1] || (x[1] === y[1] && x[2] >= y[2])));
}

test('hover 桥接：.msg.r::before 命中桥存在，默认 pointer-events:none 不拦截', () => {
  const decl = rule('.chat-body .msg.r::before');
  assert.ok(decl, 'missing .chat-body .msg.r::before hover bridge rule');
  assert.ok(/content:\s*""/.test(decl), 'bridge must be invisible (content:"")');
  assert.ok(/pointer-events:\s*none/.test(decl), 'bridge must not intercept clicks by default');
  const mLeft = /left:\s*(-\d+(?:\.\d+)?)px/.exec(decl);
  const mWidth = /width:\s*(\d+(?:\.\d+)?)px/.exec(decl);
  assert.ok(mLeft && mWidth, 'bridge must declare left/width in px');
  const left = Math.abs(parseFloat(mLeft[1]));
  const width = parseFloat(mWidth[1]);
  assert.ok(width >= left, `bridge width(${width}px) must cover its left offset(${left}px)`);
  assert.ok(left >= 58, `bridge must reach the recall button at left:-58px (got ${left}px)`);
  assert.ok(/height:\s*100%/.test(decl), 'bridge must cover full bubble height');
});

test('hover 桥接：hover 态桥激活（pointer-events:auto）保持 hover 链路', () => {
  const decl = rule('.chat-body .msg.r:hover::before');
  assert.ok(decl, 'missing .chat-body .msg.r:hover::before rule');
  assert.ok(/pointer-events:\s*auto/.test(decl), 'bridge must be active while hovering');
});

test('重新编辑配色：app.css 覆盖规则存在，:link/:visited/:hover 三态齐全，color:var(--a)', () => {
  const decl = rule('.chat-body .msg.r.chat-msg-recalled a');
  assert.ok(decl, 'missing .chat-body .msg.r.chat-msg-recalled a override rule');
  assert.ok(/color:\s*var\(--a\)/.test(decl), 're-edit link must use var(--a) accent color');
  assert.ok(rule('.chat-body .msg.r.chat-msg-recalled a:link'), 'missing a:link override');
  assert.ok(rule('.chat-body .msg.r.chat-msg-recalled a:visited'), 'missing a:visited override');
  assert.ok(rule('.chat-body .msg.r.chat-msg-recalled a:hover'), 'missing a:hover override');
});

test('层叠盲区（QA 根因）：apple-theme.css #233 白字规则锚点仍在', () => {
  const white = findRule(APPLE_CSS, '[data-theme="apple"] .chat-body .msg.r a');
  assert.ok(white, 'apple-theme.css #233 white rule anchor disappeared (update this test)');
  assert.ok(/color:\s*#fff/.test(white), '#233 white rule must stay color:#fff');
  const whiteHover = findRule(APPLE_CSS, '[data-theme="apple"] .chat-body .msg.r a:hover');
  assert.ok(whiteHover, 'apple-theme.css #233 a:hover rule anchor disappeared');
});

test('层叠胜出：[data-theme="apple"] 前缀覆盖规则特异性高于 #233，跨文件加载顺序必胜', () => {
  // apple-theme.css 后加载（index.html 第 10 行）且默认主题为 apple，
  // 同特异性 (0,4,1) 时白字规则层叠胜出 → 覆盖规则必须以 [data-theme="apple"] 前缀升至 (0,5,1)
  const whiteSel = '[data-theme="apple"] .chat-body .msg.r a';
  for (const pseudo of ['', ':link', ':visited']) {
    const sel = `[data-theme="apple"] .chat-body .msg.r.chat-msg-recalled a${pseudo}`;
    const decl = rule(sel);
    assert.ok(decl, `missing specificity-raised override: ${sel}`);
    assert.ok(/color:\s*var\(--a\)/.test(decl), `${sel} must use var(--a)`);
    assert.ok(
      specGte(specificity(sel), specificity(whiteSel + pseudo)),
      `${sel} specificity ${specificity(sel)} must beat #233 ${specificity(whiteSel + pseudo)}`
    );
  }
  const hoverSel = '[data-theme="apple"] .chat-body .msg.r.chat-msg-recalled a:hover';
  const hoverDecl = rule(hoverSel);
  assert.ok(hoverDecl, `missing specificity-raised override: ${hoverSel}`);
  assert.ok(
    specGte(specificity(hoverSel), specificity('[data-theme="apple"] .chat-body .msg.r a:hover')),
    `${hoverSel} specificity must beat #233 hover`
  );
  assert.ok(!/color:\s*#fff/.test(hoverDecl), 'hover override must not re-introduce #fff');
});

test('缓存版本：index.html 引用的 app.css ?v= 已 bump（覆盖规则可下发到存量客户端）', () => {
  const html = fs.readFileSync(path.join(ROOT, 'src', 'api', 'static', 'index.html'), 'utf8');
  const m = /app\.css\?v=(\d+)/.exec(html);
  assert.ok(m, 'index.html must reference app.css with ?v= cache buster');
  assert.ok(parseInt(m[1], 10) > 85, `app.css ?v=${m[1]} must be bumped past 85`);
});

test('回归锚点：#442 hover 显示撤回按钮 / recalled 隐藏撤回按钮语义不变', () => {
  const hoverShow = rule('.chat-body .msg.r:hover .msg-recall-btn');
  assert.ok(hoverShow && /display:\s*inline-flex/.test(hoverShow),
    '#442 hover-to-show recall button rule must stay');
  const recalledHide = rule('.chat-body .msg.r.chat-msg-recalled .msg-recall-btn');
  assert.ok(recalledHide && /display:\s*none/.test(recalledHide),
    'recalled hint must keep hiding recall button');
  const btn = rule('.chat-body .msg.r .msg-recall-btn');
  assert.ok(btn && /left:\s*-58px/.test(btn), 'recall button position anchor changed');
});
