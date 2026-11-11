#!/usr/bin/env node
/**
 * theme-contrast.mjs — 明暗双主题 token 对比度静态核算（不依赖浏览器）
 *
 * 用法：node scripts/theme-contrast.mjs
 * 口径：
 *   - 正文（--t / --t2）           ≥ 4.5:1（WCAG AA，apple-design-suite 硬规则）
 *   - caption/placeholder（--t3）  ≥ 3.0:1（非正文档，suite 只把 4.5 压给正文；
 *      压到 4.5 会让 --t3 深过 --t2，三级层级反而倒挂）
 *   - 彩色底上的字                 ≥ 3.0:1（大字/控件口径）
 *
 * token 来源（与真机同源，直接读文件，不复制数值）：
 *   legacy/css/tokens.css            → :root 与 [data-theme="apple"] 的浅色档
 *   legacy/css/apple-theme.css       → 强制 dark 与 auto-dark 两块的暗色档
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '../..') // → src/api/frontend
const TOKENS = path.join(root, 'legacy/css/tokens.css')
const THEME = path.join(root, 'legacy/css/apple-theme.css')

/** 从一段 CSS 文本里抽出 --name: value; 对 */
function parseVars(block) {
  const out = {}
  const re = /(--[a-z0-9-]+)\s*:\s*([^;}]+)/gi
  let m
  while ((m = re.exec(block))) out[m[1]] = m[2].trim()
  return out
}
/** 取某个选择器块的 var 集合（先把注释剥掉，否则注释里的同名选择器会误导 indexOf） */
function blockOf(css, selector) {
  const clean = css.replace(/\/\*[\s\S]*?\*\//g, '')
  const sel = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const m = new RegExp(sel + '\\s*\\{').exec(clean)
  if (!m) return {}
  const start = clean.indexOf('{', m.index)
  let depth = 0
  let end = start
  for (; end < clean.length; end++) {
    if (clean[end] === '{') depth++
    else if (clean[end] === '}') {
      depth--
      if (depth === 0) break
    }
  }
  return parseVars(clean.slice(start, end))
}

const tokensCss = fs.readFileSync(TOKENS, 'utf8')
const themeCss = fs.readFileSync(THEME, 'utf8')

const LIGHT = { ...blockOf(tokensCss, ':root'), ...blockOf(tokensCss, '[data-theme="apple"]') }
const lightBase = blockOf(tokensCss, ':root')
const lightApple = blockOf(tokensCss, '[data-theme="apple"]')
const DARK_FORCED = blockOf(themeCss, '[data-theme="apple"][data-theme-mode="dark"]')
const DARK_AUTO = blockOf(themeCss, '[data-theme="apple"][data-theme-mode="auto"]')

/** 解析 #rgb/#rrggbb/rgba()/颜色名（只支持本仓用到的写法） */
function parseColor(v) {
  if (!v) return null
  v = v.trim().toLowerCase()
  let m = /^#([0-9a-f]{3})$/.exec(v)
  if (m) return [...m[1]].map((c) => parseInt(c + c, 16)).concat(1)
  m = /^#([0-9a-f]{6})$/.exec(v)
  if (m) return [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16)).concat(1)
  m = /^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+))?\s*\)$/.exec(v)
  if (m) return [+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]]
  return null
}
function lum([r, g, b]) {
  const f = (c) => {
    c /= 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
}
/** alpha 合成：fg over bg */
function over(fg, bg) {
  const a = fg[3]
  return [0, 1, 2].map((i) => Math.round(fg[i] * a + bg[i] * (1 - a))).concat(1)
}
function ratio(fg, bg) {
  const a = lum(fg),
    b = lum(bg)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}

function resolve(env, name, depth = 0) {
  let raw = env[name]
  if (!raw || depth > 6) return null
  raw = raw.trim()
  const varMatch = /^var\((--[a-z0-9-]+)/i.exec(raw)
  if (varMatch) return resolve(env, varMatch[1], depth + 1)
  if (/^color-mix\(/i.test(raw)) return null // 计算不了，跳过
  if (/^transparent$/i.test(raw)) return [0, 0, 0, 0]
  return parseColor(raw)
}

const PAIRS = [
  ['正文 --t / --bg', '--t', '--bg', 4.5],
  ['正文 --t / --c(面板)', '--t', '--c', 4.5],
  ['次要 --t2 / --c', '--t2', '--c', 4.5],
  ['三级 --t3 / --c', '--t3', '--c', 3.0],
  ['三级 --t3 / --bg', '--t3', '--bg', 3.0],
  ['彩色底白字 --on-accent / --a', '--on-accent', '--a', 3.0],
  ['白字 --on-accent / --r', '--on-accent', '--r', 3.0],
  ['白字 --on-accent / --g-fill', '--on-accent', '--g-fill', 3.0],
]

console.log('\n== 明暗 token 对比度核算（WCAG 2.1）==\n')
let fail = 0
for (const [theme, env, base] of [
  ['light(默认)', lightBase, lightBase],
  ['light(apple)', { ...lightBase, ...lightApple }, lightApple],
  ['dark(强制)', { ...lightBase, ...lightApple, ...DARK_FORCED }, DARK_FORCED],
  ['dark(auto)', { ...lightBase, ...lightApple, ...DARK_AUTO }, DARK_AUTO],
]) {
  console.log(`— ${theme} —`)
  for (const [label, fgK, bgK, min] of PAIRS) {
    let fg = resolve(env, fgK)
    let bg = resolve(env, bgK)
    if (!fg || !bg) {
      console.log(`  ${label.padEnd(30)} 跳过（含 color-mix/透明）`)
      continue
    }
    if (fg[3] < 1) fg = over(fg, bg)
    const r = ratio(fg, bg)
    const ok = r >= min
    if (!ok) fail++
    console.log(`  ${label.padEnd(30)} ${r.toFixed(2)}:1  ${ok ? '✅' : '❌ <' + min}`)
  }
  console.log('')
}
console.log(fail === 0 ? '全部达标 ✅' : `有 ${fail} 项不达标 ❌`)
process.exit(fail === 0 ? 0 : 1)