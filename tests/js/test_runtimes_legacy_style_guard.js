/*
 * Agent 运行时页「旧风格」看护（源码级断言，零依赖）
 *
 * 背景：该页视觉曾在 11-12 被整体改成 theme-lab 药丸语言（搜索框变整组药丸、
 * 添加设备变实心 accent、卡片改圆角），用户裁定回到 origin/weekly-11 的旧页风格。
 * 旧风格的口径（页内 scoped + 全局按钮语言两处）一旦被再次改动，页面会静默变回新风格，
 * 这里把三处关键形态钉成守卫。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/views/RuntimesView.vue'), 'utf8');
const BRIDGE = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/styles/bridge.css'), 'utf8');

let count = 0;
function ok(cond, msg) {
  count++;
  assert.ok(cond, msg);
}

function block(src, sel) {
  const i = src.indexOf(sel + ' {');
  assert.ok(i >= 0, '未找到样式块 ' + sel);
  const j = src.indexOf('}', i);
  return src.slice(i, j + 1);
}

function fnBody(name) {
  const i = VIEW.indexOf('function ' + name + '(');
  assert.ok(i >= 0, '未找到 ' + name);
  let depth = 0, started = false;
  for (let j = i; j < VIEW.length; j++) {
    const c = VIEW[j];
    if (c === '{') { depth++; started = true; }
    else if (c === '}') { depth--; if (started && depth === 0) return VIEW.slice(i, j + 1); }
  }
  assert.fail('未闭合 ' + name);
}

// 0. 本页自锁旧调色板/几何：不得再被 apple 主题 token 覆盖
const rtvRoot = block(VIEW, '.rtv');
ok(rtvRoot.indexOf('--r2: 18px') >= 0, '本页卡片圆角应与旧页 apple 主题一致（--r2 18px）');
ok(rtvRoot.indexOf('--b: #e3e6ee') >= 0, '本页应锁旧描边色 #e3e6ee');
ok(rtvRoot.indexOf('--sh: 0 1px 3px') >= 0, '本页应锁旧投影');

// 1. 搜索框：旧形态是「输入框 + 独立按钮」两个方框拼接，整组不带边框/药丸圆角
const searchGrp = block(VIEW, '.rtv-searchgrp');
ok(searchGrp.indexOf('999px') < 0, '搜索组不应再是整组药丸（出现 999px）');
ok(searchGrp.indexOf('border:') < 0, '搜索组旧形态没有整组边框');
const searchInput = block(VIEW, '.rtv-search');
ok(searchInput.indexOf('border-radius: 10px 0 0 10px') >= 0, '搜索输入框左半 10px 圆角缺失');

// 2. 添加设备：旧形态是蓝紫渐变 10px 圆角，非实心 accent 药丸
const addBtn = block(VIEW, '.rtv-addbtn');
ok(addBtn.indexOf('linear-gradient(135deg, #3b82f6, #8b5cf6)') >= 0, '添加设备应保留旧蓝紫渐变');
ok(addBtn.indexOf('border-radius: 10px') >= 0, '添加设备应为 10px 圆角（非药丸）');

// 3. 卡片：旧形态是左侧 3px 状态描边 + 仅右侧圆角
const ecard = block(VIEW, '.rtv-ecard');
ok(ecard.indexOf('border-left: 3px solid') >= 0, '引擎卡片缺少 3px 左侧状态描边');
ok(ecard.indexOf('border-radius: 0 var(--r2) var(--r2) 0') >= 0, '引擎卡片应为「左侧直角、右侧圆角」旧形态');

// 4. 全局按钮语言不得再套住本页自绘控件（否则药丸 999px 会盖过页内几何）
const geoList = BRIDGE.slice(BRIDGE.indexOf('按钮语言'), BRIDGE.indexOf('/* 主按钮级别'));
ok(geoList.indexOf('.rtv-searchbtn') < 0, 'bridge.css 全局几何规则不应再作用 .rtv-searchbtn');
ok(geoList.indexOf('.rtv-addbtn') < 0, 'bridge.css 全局几何规则不应再作用 .rtv-addbtn');

// 5. 引擎卡在线 = 3px 绿条（旧页口径）；Workers 在线仍是 1px 中性描边
const clb = fnBody('cardLeftBorder');
ok(/borderLeft: '3px solid ' \+ \(RUNTIME_COLOR\[h\]/.test(clb), '引擎卡应统一 3px 状态色左边条（含在线绿条）');
ok(VIEW.indexOf('workerCardBorder(d)') >= 0, '设备卡应走 workerCardBorder（在线不出彩条）');
ok(/boxShadow: ABNORMAL_RING/.test(clb), '异常/离线引擎卡应带整卡红圈（旧页 0 0 0 1px var(--r)）');
const wcb = fnBody('workerCardBorder');
ok(wcb.indexOf('borderLeft: s.borderLeft') >= 0 && wcb.indexOf('boxShadow') < 0, '设备卡不得带整卡红圈');

// 6. 工具栏按钮（Engines/Workers/刷新等）压回旧页 .bt 口径，且搜索占位符有实词条
ok(/\.rtv :is\(\.bt, \.bt-p, \.bt-o/.test(VIEW) && /border-radius: 980px !important/.test(VIEW), '页内应有 .bt 旧几何覆盖（apple 药丸 980px）');
ok(/\.rtv :is\(\.bt, \.bt-p, \.bt-o[^{]*\{[^}]*font-size: 13px !important/.test(VIEW), '按钮字号应与旧页一致（13px）');
// .bt-p 选中态仍是「浅底非实心」：2026-11-18 起底色由 --a 派生（color-mix 8%），
// 供暗色档复用同一规则（暗色另抬到 18%，见第 9 组）——不再钉死 #0071e3 的 rgba 字面量。
const btP = (() => {
  const i = VIEW.indexOf('\n.rtv .bt-p {');
  assert.ok(i >= 0, '未找到 .rtv .bt-p 块');
  return VIEW.slice(i, VIEW.indexOf('}', i) + 1);
})();
ok(btP.indexOf('color-mix(in srgb, var(--a) 8%, transparent)') >= 0, '.bt-p 选中态应为浅蓝底（--a 派生 8%）');
ok(btP.indexOf('var(--a) !important') >= 0, '.bt-p 选中态字色应取 --a');
const zh = fs.readFileSync(path.join(ROOT, 'src/api/frontend/shell/locales/zh/runtimes.json'), 'utf8');
ok(/"search_placeholder"/.test(zh), 'zh 语言包必须含 runtimes.search_placeholder');

// 7. 设备卡操作按钮（改名/吊销/范围）压回旧页 inline 口径
ok(VIEW.indexOf('.rtv .rtv-wbtn') < 0, '设备卡按钮不得再单独压几何，跟随 .bt 家族（旧页 13px 药丸）');

// 8. 模板不得残留裸 ">" 文本节点（会渲染成「在线 > 本机」这种无意义箭头）
ok(!/-->\s*>\s*</.test(VIEW), '模板注释后残留了裸 ">" 文本节点');

// 9. 暗色档：本页自锁 token 必须自带暗色变体，否则暗色模式下页面永远停在浅色
//    （2026-11-18 用户报「暗色下 Engines/Workers 卡片还是白底」）
ok(
  /:root\[data-theme-mode='dark'\]\s*\.rtv\s*\{[\s\S]{0,400}--c:\s*#1c1c1e/.test(VIEW),
  '自锁 token 缺 data-theme-mode=dark 的暗色变体（卡片会保持白底）',
);
ok(
  /prefers-color-scheme:\s*dark[\s\S]{0,200}:root\[data-theme-mode='auto'\]\s*\.rtv\s*\{[\s\S]{0,400}--c:\s*#1c1c1e/.test(VIEW),
  'auto 模式缺 prefers-color-scheme:dark 的暗色变体',
);

// 10. 暗色档的徽章/危险按钮（2026-11-18 用户裁定）：
//     pastel 徽章压成「同色系低透明度底 + 提亮字」；吊销按钮由实心红改成卡片底 + 红描边红字
const DK = "(?:\\[data-theme-mode='dark'\\]|\\[data-theme-mode='auto'\\])";
ok(
  new RegExp(DK + "\\s*\\.rtv\\s*\\.rtv-hbadge\\.online\\s*\\{[^}]*background:\\s*rgba\\(48, 209, 88, 0\\.18\\)").test(VIEW),
  '暗色缺健康徽章 online 加深档（浅底 pastel 在深色卡片上过亮）',
);
ok(
  new RegExp(DK + "\\s*\\.rtv\\s*\\.rtv-sbadge\\.global\\s*\\{[^}]*color:\\s*#5b9eff").test(VIEW),
  '暗色缺范围徽章 global 加深档',
);
ok(
  new RegExp(
    DK + "\\s*\\.rtv\\s*\\.bt-r\\s*\\{[^}]*background:\\s*var\\(--c\\)[^}]*color:\\s*#ff453a",
  ).test(VIEW),
  '暗色吊销按钮应为「卡片同色底 + 红字」（不是实心红）',
);
ok(
  new RegExp(DK + "\\s*\\.rtv\\s*\\.bt-r\\s*\\{[^}]*border:\\s*1px solid").test(VIEW),
  '暗色吊销按钮应带红描边',
);

// 11. 凭据卡「沙箱平台」行（2026-11-18 用户两轮反馈：先说紫色不好看，再裁定「去掉链接，
//     点旁边的详情按钮就可以了」）。终态：平台名是只读文本（var(--t2)），行内不再有 <a> / ↗，
//     进平台页只走右侧「详情」按钮；同族一次性滤净：视图内不得再有 color: #6d28d9。
ok(
  /\.rtv-plat-name\s*\{[^}]*color:\s*var\(--t2\)/.test(VIEW),
  '平台名应是只读文本（次级文字色 var(--t2)）',
);
ok(
  !/rtv-plat-link/.test(VIEW),
  '平台名不得再做成行内链接（2026-11-18 用户裁定：进平台页只走右侧「详情」按钮）',
);
const PLAT_ROW = VIEW.slice(VIEW.indexOf('rtv-wcard-plat'), VIEW.indexOf('rtv-wcard-engines'));
ok(
  PLAT_ROW.indexOf('<a') === -1 && PLAT_ROW.indexOf('↗') === -1,
  '卡片平台行不得残留 <a> 或 ↗ 外链标记',
);
ok(
  /class="bt bt-o bt-s rtv-plat-btn"[\s\S]{0,200}gotoProviderDetail\(d\.provider_id\)/.test(VIEW),
  '详情按钮必须保留并仍调用 gotoProviderDetail(provider_id)',
);
ok(
  !/color:\s*#6d28d9/i.test(VIEW),
  '视图内不得再有 color: #6d28d9（紫色已从沙箱平台行整族移除）',
);

console.log('PASS runtimes legacy style guard (' + count + ' assertions)');