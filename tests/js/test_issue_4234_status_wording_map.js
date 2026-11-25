/**
 * Issue #4234：统一状态词表（status → {列, 展示名} 单一映射）回归。
 *
 * 缺陷：同一条 status=active 需求，看板 in_progress 列头叫「处理中」、
 * 卡片/详情状态点却叫「待处理」——列分组口径（active+processing 合并）与
 * 状态词表（active=待处理）各自定义，无单一事实来源。
 *
 * 运行：node tests/js/test_issue_4234_status_wording_map.js
 *
 * 验证点：
 *   1. 单一事实来源：STATUS_VIEW_MAP 一处定义 api 集与展示名；TASK_STATUS_TO_API /
 *      BOARD_CURSOR_STATUSES / STATUS_LABELS / statusInfo 均由它派生，无重复字面量。
 *   2. 行为不变：statuses= 下发口径仍为 pending=active / in_progress=active,processing；
 *      看板共享游标仍为 active,processing（筛选/翻页/列头计数不回归）。
 *   3. 列名与卡片名一致：同一 status/stage 经 mapStatusL 落列后，卡片/详情状态点
 *      取的是同一词条（active+流水线 stage → 列与卡片均「处理中」；active 无 stage → 均「待处理」）。
 *   4. 详情状态点改走 mapStatusL 派生口径（不再裸比 tk.status === 'processing'）。
 *   5. zh/en 词条齐备，无缺失键。
 */
'use strict'

const test = require('node:test')
const assert = require('node:assert')
const fs = require('node:fs')
const path = require('node:path')

const ROOT = path.resolve(__dirname, '..', '..')
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8')
const ISS = read('src/api/frontend/app/src/views/IssuesView.vue')

function extract(src, re, what) {
  const m = src.match(re)
  assert.ok(m, `未能从 IssuesView.vue 提取 ${what}`)
  return m[0]
}

// ---- 提取可执行片段：STATUS_VIEW_MAP / viewStatusLabel / mapStatusL ----
const mapSrc = extract(
  ISS,
  /const STATUS_VIEW_MAP = \{[\s\S]*?\n\}/,
  'STATUS_VIEW_MAP 定义'
)
const labelSrc = extract(
  ISS,
  /function viewStatusLabel\(k\) \{[\s\S]*?\n\}/,
  'viewStatusLabel 定义'
)
const mapStatusSrc = extract(
  ISS,
  /function mapStatusL\(tk\) \{[\s\S]*?\n\}/,
  'mapStatusL 定义'
)

const ZH = {
  'status.tag_pending': '待处理',
  'status.tag_processing': '处理中',
  'status.tag_awaiting_input': '待澄清',
  'status.tag_pending_review': '待审核',
  'status.tag_completed': '已完成',
  'status.tag_cancelled': '已取消',
}
const t = (k) => ZH[k]
const stripTag = (v) => String(v || '').replace(/<[^>]*>/g, '')

const STATUS_VIEW_MAP = new Function(`${mapSrc}; return STATUS_VIEW_MAP`)()
const viewStatusLabel = new Function('t', 'stripTag', 'STATUS_VIEW_MAP', `${labelSrc}\nreturn viewStatusLabel`)(t, stripTag, STATUS_VIEW_MAP)
const mapStatusL = new Function(`${mapStatusSrc}; return mapStatusL`)()

test('T1 单一事实来源：api 集与展示名都定义在 STATUS_VIEW_MAP，消费方均由它派生', () => {
  assert.ok(STATUS_VIEW_MAP.pending, 'map 含 pending')
  assert.ok(STATUS_VIEW_MAP.in_progress, 'map 含 in_progress')
  for (const k of Object.keys(STATUS_VIEW_MAP)) {
    assert.ok(Array.isArray(STATUS_VIEW_MAP[k].api), `${k}.api 应为数组`)
    assert.ok(STATUS_VIEW_MAP[k].labelKey, `${k}.labelKey 缺失`)
  }
  // TASK_STATUS_TO_API 由映射表派生，且不再有第二处字面量定义
  assert.match(ISS, /const TASK_STATUS_TO_API = Object\.fromEntries\(\s*\n?\s*Object\.entries\(STATUS_VIEW_MAP\)/, 'TASK_STATUS_TO_API 未由 STATUS_VIEW_MAP 派生')
  assert.doesNotMatch(ISS, /const TASK_STATUS_TO_API = \{\s*\n\s*pending: \['active'\]/, 'TASK_STATUS_TO_API 仍有重复字面量定义')
  // BOARD_CURSOR_STATUSES 由映射表派生（不再硬编码 'active,processing'）
  assert.match(ISS, /active_flow: \[\.\.\.new Set\(\[\.\.\.TASK_STATUS_TO_API\.pending, \.\.\.TASK_STATUS_TO_API\.in_progress\]\)\]\.join\(','\)/, 'active_flow 游标未由映射表派生')
  assert.doesNotMatch(ISS, /active_flow: 'active,processing'/, 'active_flow 游标仍为硬编码字面量')
  // 列头/卡片文案同源：STATUS_LABELS 与 statusInfo 均走 viewStatusLabel
  assert.match(ISS, /const STATUS_LABELS = computed\(\(\) => \{[\s\S]{0,300}viewStatusLabel/, 'STATUS_LABELS 未走 viewStatusLabel')
  assert.match(ISS, /text: viewStatusLabel\(ms\) \|\| viewStatusLabel\('pending'\)/, 'statusInfo 展示名未与列头同源')
})

test('T2 行为不变：statuses= 下发口径与共享游标取值与修复前一致', () => {
  const derived = {}
  Object.entries(STATUS_VIEW_MAP).forEach(([k, v]) => (derived[k] = v.api))
  assert.deepEqual(derived.pending, ['active'], 'pending 下发口径应为 [active]')
  assert.deepEqual(derived.in_progress, ['active', 'processing'], 'in_progress 下发口径应为 [active, processing]')
  assert.deepEqual(derived.awaiting_input, ['awaiting_input'])
  assert.deepEqual(derived.pending_review, ['pending_review'])
  assert.deepEqual(derived.done, ['completed'])
  assert.deepEqual(derived.cancelled, ['cancelled'])
  // 共享游标 = pending ∪ in_progress 的 api 集，仍为 active,processing
  const flow = [...new Set([...derived.pending, ...derived.in_progress])].join(',')
  assert.strictEqual(flow, 'active,processing', 'active_flow 游标取值变化会导致筛选/翻页回归')
  // 看板列集合不变（六列，列 key 不增不减）
  const boardSrc = extract(
    ISS,
    /const boardCols = computed\(\(\) => \[[\s\S]*?\n\]\)/,
    '看板列定义 boardCols'
  )
  const colKeys = [...boardSrc.matchAll(/k: '(\w+)'/g)].map((m) => m[1])
  assert.deepEqual(
    colKeys,
    ['pending', 'in_progress', 'awaiting_input', 'pending_review', 'done', 'cancelled'],
    '看板列 key 集合不得变化'
  )
})

test('T3 列名与卡片名一致：同一 status 落列后卡片取同一词条', () => {
  // 列头词条：列 k 的头文案 = STATUS_VIEW_MAP[k].labelKey 的词条
  assert.strictEqual(viewStatusLabel('pending'), '待处理')
  assert.strictEqual(viewStatusLabel('in_progress'), '处理中')
  // active + 流水线 stage：列 in_progress（处理中），卡片/详情同取「处理中」
  for (const stage of ['vod', 'issue', 'coding', 'qa', 'sre']) {
    const ms = mapStatusL({ status: 'active', stage })
    assert.strictEqual(ms, 'in_progress', `active+stage=${stage} 应落 in_progress 列`)
    assert.strictEqual(viewStatusLabel(ms), '处理中', `active+stage=${stage} 卡片/详情应显示「处理中」`)
  }
  // active 无 stage：列 pending（待处理），卡片/详情「待处理」
  const msA = mapStatusL({ status: 'active', stage: '' })
  assert.strictEqual(msA, 'pending', 'active 无 stage 应落 pending 列')
  assert.strictEqual(viewStatusLabel(msA), '待处理')
  // processing：列 in_progress，卡片/详情「处理中」
  const msP = mapStatusL({ status: 'processing', stage: '' })
  assert.strictEqual(msP, 'in_progress')
  assert.strictEqual(viewStatusLabel(msP), '处理中')
  // 其他状态不回归
  assert.strictEqual(mapStatusL({ status: 'completed', stage: '' }), 'done')
  assert.strictEqual(mapStatusL({ status: 'cancelled', stage: '' }), 'cancelled')
  assert.strictEqual(mapStatusL({ status: 'awaiting_input', stage: '' }), 'awaiting_input')
  assert.strictEqual(mapStatusL({ status: 'pending_review', stage: '' }), 'pending_review')
})

test('T4 详情状态点改走 mapStatusL 派生口径（与列/卡片同源）', () => {
  assert.match(ISS, /const ms = mapStatusL\(tk\)\s*\n\s*const isDone = ms === 'done'/, 'detailCardHtml 未改用 mapStatusL 派生')
  assert.doesNotMatch(ISS, /const isProc = tk\.status === 'processing'/, '详情仍裸比原始 status=processing（旧口径残留）')
  assert.doesNotMatch(ISS, /const isDone = tk\.status === 'completed'/, '详情仍裸比原始 status=completed（旧口径残留）')
})

test('T5 zh/en 词条齐备：映射表引用的 labelKey 无缺失', () => {
  for (const lang of ['zh', 'en']) {
    const d = JSON.parse(read(`src/api/frontend/shell/locales/${lang}/tasks.json`))
    for (const k of Object.keys(STATUS_VIEW_MAP)) {
      const parts = STATUS_VIEW_MAP[k].labelKey.split('.')
      let node = d
      for (const p of parts) node = node ? node[p] : undefined
      assert.ok(node, `${lang}/tasks.json 缺 ${STATUS_VIEW_MAP[k].labelKey}`)
    }
  }
})
