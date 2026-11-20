/*
 * Vue 视图卫生守卫：裸 `<template>` 只能出现在 SFC 根。
 *
 * 2026-11-18 实测 bug（沙箱 Provider 详情「引擎模板」头部）：
 *   `<div class="pill"><template>🤖 {{ t(...) }}</template></div>`
 * Vue 把裸 `<template>` 当真实 DOM 节点渲染，子内容进 `.content`（DocumentFragment），
 * 浏览器不渲染它 —— 药丸里只剩一个空盒子，用户看到「凭据来源提示不见了」。
 *
 * 规则：一个 .vue 文件里不带任何属性的 `<template>` 只允许出现 1 次（SFC 根）；
 * 带 v-if / v-for / v-else / #slot 的模板片段是合法用法，不计入。
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEWS = path.join(ROOT, 'src/api/frontend/app/src');

let failures = [];
let files = 0;
let count = 0;

function walk(dir) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p);
    else if (e.name.endsWith('.vue')) check(p);
  }
}

function check(file) {
  files++;
  const src = fs.readFileSync(file, 'utf8');
  const rel = path.relative(ROOT, file);
  // 只认「裸」<template>：紧随其后就是 '>' 或空白 + '>'
  const bare = [...src.matchAll(/<template\s*>/g)];
  count++;
  if (bare.length > 1) {
    const line = src.slice(0, bare[1].index).split('\n').length;
    failures.push(`${rel}:${line} 出现第 ${bare.length} 个裸 <template>（只允许 SFC 根）；片段请用 <span>/<div> 或加 v-if/v-for`);
  }
  count++;
  if (bare.length === 0) {
    failures.push(`${rel}: 找不到 SFC 根 <template>`);
  }
}

walk(VIEWS);

if (failures.length) {
  console.error('FAIL vue template hygiene:');
  failures.forEach((f) => console.error('  - ' + f));
  process.exit(1);
}
console.log(`PASS vue template hygiene (${files} .vue files scanned, ${count} checks)`);
