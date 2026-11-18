/*
 * LLM 凭据弹窗表单控件守护（2026-11-18 用户实测：「添加 和 编辑 LLM 凭据界面，
 * 暗夜模式，输入框 文本 和背景颜色不对」）。
 *
 * 回归背景（两个独立缺陷，缺一条断言就会复发其一）：
 *  A. 组件内联样式：两个 <select> 只写了 background: var(--c) 没写字色 → 暗色下字落回
 *     UA 黑；API Key 输入框既没字色也没底色 → UA 白底黑字；base_url / model 只写了
 *     color: var(--t) 没写底色 → UA 白底 + 白字，**文字完全看不见**。
 *  B. 构建链路：LlmCredManager 被 AccountView(user) 与 SettingsView(repo) 两个 entry
 *     共用，Vite 把它的 <style scoped> 拆成独立 assets/LlmCredManager-<hash>.css；
 *     该组件不在 vite.config.js 的 viewCssMap 里 → 没有任何 <link> 加载它（entry 自注入
 *     已在 2026-11-16 废弃，改由 <head> 阻塞引入 all-views.css），于是「改成 class 写法」
 *     后样式整段失效，控件落回 UA 默认灰底黑边——比修之前更糟。
 *
 * 看护口径：控件样式只由 .llmc-fld 承载（字色 var(--t) + 底色 var(--c)），模板里
 * 不再出现带 style 的表单控件；共享组件的 CSS 必须进 viewCssMap 稳定名表。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VUE = path.join(ROOT, 'src/api/frontend/app/src/views/LlmCredManager.vue');
const VITE = path.join(ROOT, 'src/api/frontend/app/vite.config.js');
const INDEX = path.join(ROOT, 'src/api/frontend/shell/index.html');

const src = fs.readFileSync(VUE, 'utf8');
const vite = fs.readFileSync(VITE, 'utf8');
const html = fs.readFileSync(INDEX, 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

// ── A. 组件内样式 ────────────────────────────────────────────────
check('弹窗五个控件全部走 class="llmc-fld"', function () {
  const controls = src.match(/<(select|input)\b[^>]*>/g) || [];
  assert.strictEqual(controls.length, 5, '控件数量变了（期望 2 select + 3 input），请核对断言');
  controls.forEach(function (tag) {
    assert.ok(/class="llmc-fld"/.test(tag), '有控件没挂 llmc-fld: ' + tag.slice(0, 80));
  });
});

check('表单控件不再写内联 style（字色/底色只由 .llmc-fld 决定）', function () {
  const controls = src.match(/<(select|input)\b[^>]*>/g) || [];
  const offenders = controls.filter(function (tag) {
    return /\sstyle="/.test(tag);
  });
  assert.strictEqual(
    offenders.length,
    0,
    '控件又走回内联样式（暗色下必然漂移）: ' + offenders.map(function (t) {
      return t.slice(0, 80);
    }).join(' | ')
  );
});

check('.llmc-fld 同时给字色与底色（缺一个就是黑字/白底白字）', function () {
  const i = src.indexOf('.llmc-fld {');
  assert.ok(i > 0, '找不到 .llmc-fld 规则块');
  const block = src.slice(i, src.indexOf('}', i));
  assert.ok(/color:\s*var\(--t\)/.test(block), '.llmc-fld 缺 color: var(--t)');
  assert.ok(/background:\s*var\(--c\)/.test(block), '.llmc-fld 缺 background: var(--c)');
});

check('placeholder 与 option 跟随主题（暗色下不落 UA 灰）', function () {
  assert.ok(/\.llmc-fld::placeholder\s*\{[^}]*color:\s*var\(--t3\)/.test(src), '缺 ::placeholder 字色');
  assert.ok(/\.llmc-fld option\s*\{[^}]*color:\s*var\(--t\)/.test(src), '缺 option 字色');
  assert.ok(/\.llmc-fld option\s*\{[^}]*background:\s*var\(--c\)/.test(src), '缺 option 底色');
});

// ── B. 构建链路 ──────────────────────────────────────────────────
check('vite.config.js 的 viewCssMap 必须收录 LlmCredManager 的 CSS', function () {
  assert.ok(
    /'llm-cred-manager':\s*\/\^LlmCredManager-\.\*\\\.css\$\//.test(vite),
    'LlmCredManager 未进 viewCssMap → 它的 scoped CSS 不会被 any viewcss/all-views.css 收编，页面加载不到'
  );
});

check('index.html 以阻塞方式引入合并后的 all-views.css', function () {
  assert.ok(
    /<link rel="stylesheet" href="\/static-vue\/assets\/viewcss\/all-views\.css\?v=\d+">/.test(html),
    'index.html 没有引入 all-views.css —— 视图样式整片不会生效'
  );
});

console.log('PASS llm-cred-dialog form styles (' + passed + ' assertions)');
