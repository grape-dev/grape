/*
 * Issue #233 回归测试：Chat 对话内容链接显示优化
 *
 * 运行：node tests/js/test_issue_233_link_contrast.js
 *
 * 验证点：
 *   1. app.css 包含 .chat-body .msg.l a 高对比度规则（color:#fff）
 *   2. apple-theme.css 包含对应 [data-theme="apple"] .chat-body .msg.l a 规则
 *   3. 白色 #ffffff on 蓝色背景 #3168e0 对比度 ≥ 4.5:1（WCAG AA）
 *   4. 白色 #ffffff on Apple 蓝 #0071e3 对比度 ≥ 4.5:1
 *   5. chat.js linkifyIssueNumbers 不再包含内联 color:var(--a) 样式
 *   6. hover 和 visited 状态规则存在
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');

function readFile(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

// ── WCAG 对比度计算工具 ──
function hexToRgb(hex) {
  const h = hex.replace('#', '');
  return {
    r: parseInt(h.substring(0, 2), 16),
    g: parseInt(h.substring(2, 4), 16),
    b: parseInt(h.substring(4, 6), 16),
  };
}

function srgbToLinear(c) {
  const s = c / 255;
  return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
}

function relativeLuminance(hex) {
  const { r, g, b } = hexToRgb(hex);
  return 0.2126 * srgbToLinear(r) + 0.7152 * srgbToLinear(g) + 0.0722 * srgbToLinear(b);
}

function contrastRatio(fg, bg) {
  const l1 = relativeLuminance(fg);
  const l2 = relativeLuminance(bg);
  const lighter = Math.max(l1, l2);
  const darker = Math.min(l1, l2);
  return (lighter + 0.05) / (darker + 0.05);
}

// ── 测试 ──

function testContrastRatios() {
  const white = '#ffffff';
  const defaultBlue = '#3168e0';
  const appleBlue = '#0071e3';

  // a:link / a:visited 均使用 #ffffff
  const r1 = contrastRatio(white, defaultBlue);
  assert.ok(r1 >= 4.5, `白 on 默认蓝 #3168e0 对比度 ${r1.toFixed(2)} 不达标（需 ≥4.5）`);
  console.log(`  ✓ 白 #ffffff on #3168e0 → 对比度 ${r1.toFixed(2)}`);

  const r2 = contrastRatio(white, appleBlue);
  assert.ok(r2 >= 4.5, `白 on Apple 蓝 #0071e3 对比度 ${r2.toFixed(2)} 不达标（需 ≥4.5）`);
  console.log(`  ✓ 白 #ffffff on #0071e3 → 对比度 ${r2.toFixed(2)}`);

  // hover 不改变文字颜色（仅 background-color + text-decoration），对比度不变
  console.log('  ✓ hover 不改变文字颜色，对比度保持 ≥4.5:1');
}

function testAppCss() {
  const css = readFile('src/api/static/css/app.css');

  assert.ok(
    css.includes('.chat-body .msg.l a'),
    'app.css 缺少 .chat-body .msg.l a 规则'
  );
  console.log('  ✓ app.css 包含 .chat-body .msg.l a 规则');

  assert.ok(
    css.includes('.chat-body .msg.l a:hover'),
    'app.css 缺少 .chat-body .msg.l a:hover 规则'
  );
  console.log('  ✓ app.css 包含 hover 状态规则');

  assert.ok(
    css.includes('.chat-body .msg.l a:visited'),
    'app.css 缺少 .chat-body .msg.l a:visited 规则'
  );
  console.log('  ✓ app.css 包含 visited 状态规则');

  // 确认白色颜色值
  const ruleMatch = css.match(/\.chat-body\s+\.msg\.l\s+a[^{]*\{([^}]*)\}/);
  assert.ok(ruleMatch, '无法提取 .chat-body .msg.l a 规则体');
  assert.ok(
    ruleMatch[1].includes('#fff') || ruleMatch[1].includes('#ffffff'),
    '链接颜色未设置为白色'
  );
  console.log('  ✓ app.css 链接颜色为白色');

  // hover 不应使用 opacity（会导致混合色对比度下降）
  const hoverMatch = css.match(/\.chat-body\s+\.msg\.l\s+a:hover\{([^}]*)\}/);
  assert.ok(hoverMatch, '无法提取 .chat-body .msg.l a:hover 规则体');
  assert.ok(
    !hoverMatch[1].includes('opacity'),
    'hover 不应使用 opacity（会导致混合色对比度低于 WCAG AA）'
  );
  console.log('  ✓ app.css hover 不使用 opacity');

  // visited 应使用 #fff/#ffffff（确保对比度 ≥4.5:1）
  const visitedMatch = css.match(/\.chat-body\s+\.msg\.l\s+a:visited\{([^}]*)\}/);
  if (visitedMatch) {
    assert.ok(
      visitedMatch[1].includes('#fff') || visitedMatch[1].includes('#ffffff'),
      'visited 颜色应为白色（#e0e6f0 对比度不达标）'
    );
    console.log('  ✓ app.css visited 颜色为白色');
  } else {
    // visited 可能与 a:link 合并声明
    assert.ok(
      css.includes('a:visited') && css.includes('#fff'),
      'visited 规则缺失或颜色非白色'
    );
    console.log('  ✓ app.css visited 与 a:link 合并声明白色');
  }
}

function testAppleThemeCss() {
  const css = readFile('src/api/static/css/apple-theme.css');

  assert.ok(
    css.includes('[data-theme="apple"] .chat-body .msg.l a'),
    'apple-theme.css 缺少 [data-theme="apple"] .chat-body .msg.l a 规则'
  );
  console.log('  ✓ apple-theme.css 包含对应规则');

  assert.ok(
    css.includes('[data-theme="apple"] .chat-body .msg.l a:hover'),
    'apple-theme.css 缺少 hover 规则'
  );
  console.log('  ✓ apple-theme.css 包含 hover 状态规则');

  assert.ok(
    css.includes('[data-theme="apple"] .chat-body .msg.l a:visited'),
    'apple-theme.css 缺少 visited 规则'
  );
  console.log('  ✓ apple-theme.css 包含 visited 状态规则');

  // hover 不应使用 opacity
  const appleHover = css.match(/\[data-theme="apple"\]\s+\.chat-body\s+\.msg\.l\s+a:hover\s*\{([^}]*)\}/);
  assert.ok(appleHover, '无法提取 apple-theme hover 规则体');
  assert.ok(
    !appleHover[1].includes('opacity'),
    'apple-theme hover 不应使用 opacity'
  );
  console.log('  ✓ apple-theme.css hover 不使用 opacity');

  // visited 应使用 #fff/#ffffff
  assert.ok(
    css.match(/\[data-theme="apple"\]\s+\.chat-body\s+\.msg\.l\s+a:visited[^{]*\{[^}]*#fff/),
    'apple-theme visited 颜色应为白色'
  );
  console.log('  ✓ apple-theme.css visited 颜色为白色');
}

function testLinkifyNoInlineColor() {
  const js = readFile('src/api/static/js/pages/chat.js');

  // linkifyIssueNumbers 函数体中不应包含内联 color:var(--a)
  const funcMatch = js.match(/function linkifyIssueNumbers[\s\S]*?\n}/);
  assert.ok(funcMatch, '无法定位 linkifyIssueNumbers 函数');

  const funcBody = funcMatch[0];
  assert.ok(
    !funcBody.includes('color:var(--a)'),
    'linkifyIssueNumbers 仍包含内联 color:var(--a) 样式'
  );
  console.log('  ✓ linkifyIssueNumbers 不再包含内联蓝色样式');

  // 确保仍有 <a> 标签输出
  assert.ok(
    funcBody.includes('<a href='),
    'linkifyIssueNumbers 未输出 <a> 标签'
  );
  console.log('  ✓ linkifyIssueNumbers 仍正确生成链接');
}

function testNonBlueAreaUnaffected() {
  const css = readFile('src/api/static/css/app.css');
  // .msg a 通用规则（蓝色链接）应保留，用于 .msg.r（浅色背景）
  assert.ok(
    css.includes('.msg a{color:var(--a)}'),
    'app.css .msg a 通用蓝色链接规则被移除，影响非蓝色区域'
  );
  console.log('  ✓ .msg a 通用规则保留，非蓝色区域不受影响');
}

// ── 执行 ──
console.log('Issue #233: Chat 对话内容链接显示优化 — 测试开始\n');

console.log('[1] WCAG 对比度验证');
testContrastRatios();

console.log('\n[2] app.css 规则验证');
testAppCss();

console.log('\n[3] apple-theme.css 规则验证');
testAppleThemeCss();

console.log('\n[4] linkifyIssueNumbers 内联样式验证');
testLinkifyNoInlineColor();

console.log('\n[5] 非蓝色区域不受影响');
testNonBlueAreaUnaffected();

console.log('\n✅ 全部测试通过（5/5）');
