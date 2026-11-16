/*
 * Issue #2649（arch 线 Vue 适配）：看板整刷回合化的行为回归。
 *
 * 运行：node tests/js/test_issue_2649_vue_board_round.js
 *
 * 验证点：
 *   1. 帧身份：7 个维度任一变化都换 key（切仓/切视图/切排序/切搜索/切筛选）。
 *   2. 回合收敛：pending 归零才提交，未归零不提交。
 *   3. 聚合：跨列按 id 去重（先到先得），坏行不抛。
 *   4. 提交裁决：失败且有旧帧 → keep（保留旧帧 + 软横幅）；失败且无旧帧 → error；
 *      已作废 / 已离开看板 → drop。
 *   5. Vue 视图结构与接线：epoch 捕获、round 缓冲、原子替换、软横幅、i18n 键齐备。
 */
const assert = require('assert')
const path = require('path')
const fs = require('fs')

const root = path.resolve(__dirname, '../..')
const LIB = path.join(root, 'src/api/frontend/app/src/lib/boardRound.js')
const VIEW = path.join(root, 'src/api/frontend/app/src/views/IssuesView.vue')

let pass = 0
let fail = 0
function ok(cond, name) {
  if (cond) {
    pass++
  } else {
    fail++
    console.error('✗ ' + name)
  }
}

// ---- 1. 帧身份 ----
const source = fs.readFileSync(LIB, 'utf8')
const os = require('os')
const tmpFile = path.join(os.tmpdir(), 'boardRound-' + process.pid + '.mjs')
fs.writeFileSync(tmpFile, source)
const mod = require(tmpFile) && null // 占位，实际用下面的动态 import

;(async function main() {
const mod = await import('file://' + tmpFile)
ok(mod.boardFrameKey(['a', 'b']) === 'a|b', '帧身份按 | 拼接')
ok(mod.boardFrameKey(['a', 'b', '']) !== mod.boardFrameKey(['a', 'b', 'x']), '维度变化即换 key')
ok(mod.boardFrameKey([null, undefined]) === '|', '空值归一为空串')

// ---- 2. 回合收敛 ----
const round = mod.createBoardRound(['k1', 'k2', 'k3'])
ok(round.pending === 3, 'pending 初始 = 列数')
ok(mod.settleBoardRound(round) === false, '未归零不提交')
ok(mod.settleBoardRound(round) === false, '仍未归零')
ok(mod.settleBoardRound(round) === true, '归零即提交')

// ---- 3. 聚合 ----
const agg = mod.aggregateBoardTasks([
  [{ id: 1, a: 1 }, { id: 2 }],
  [{ id: 1, b: 2 }, null, { id: 3 }],
])
ok(agg.list.length === 3, '跨列按 id 去重')
ok(agg.byId[1].a === 1 && agg.byId[1].b === 2, '同 id 字段合并')
ok(agg.list[0].id === 1, '先到先得保序')

// ---- 3b. 聚合索引不得命中原型链（id=__proto__ 会污染 Object.prototype）----
const protoAgg = mod.aggregateBoardTasks([[{ id: '__proto__', polluted: 'yes' }]])
ok(protoAgg.list.length === 1, '__proto__ 作为普通 id 参与列表')
ok({}.polluted === undefined, '不写 Object.prototype')
ok(Object.prototype.polluted === undefined, 'Object.prototype 未被污染')

// ---- 4. 提交裁决 ----
const failed = { pending: 0, failed: true, aborted: false }
ok(mod.decideBoardRound(failed, { inBoardView: true, hasOldFrame: true }).action === 'keep', '失败+旧帧 → 保留')
ok(mod.decideBoardRound(failed, { inBoardView: true, hasOldFrame: false }).action === 'error', '失败+无帧 → 整页错误')
ok(mod.decideBoardRound({ pending: 0, failed: false, aborted: true }, { inBoardView: true, hasOldFrame: true }).action === 'drop', '作废回合 → 丢弃')
ok(mod.decideBoardRound({ pending: 0, failed: false }, { inBoardView: false }).action === 'drop', '离开看板 → 丢弃')
ok(mod.decideBoardRound({ pending: 0, failed: false }, { inBoardView: true }).action === 'commit', '成功 → 提交')

// ---- 5. Vue 视图接线 ----
const vue = fs.readFileSync(VIEW, 'utf8')
ok(/boardEpoch\.value \+= 1/.test(vue), 'reset 自增 epoch')
ok(/const epoch = boardEpoch\.value/.test(vue), 'boardFetch 捕获 epoch')
ok(/if \(epoch !== boardEpoch\.value\) return/.test(vue), '过期响应双侧丢弃')
ok(/round\.items\[key\] = items/.test(vue), '整刷先缓冲到回合')
ok(/tasks\.value = agg\.list/.test(vue), '回合提交为单次原子替换')
ok(/boardRefreshFailed\.value = true/.test(vue), '失败保留旧帧（软失败态）')
ok(/isk-board-banner/.test(vue), '软横幅进模板')
ok(/retryBoardRefresh/.test(vue), '软横幅带重试入口')
ok(/board\.refresh_failed_keep/.test(vue) && /board\.retry/.test(vue), 'i18n 键与默认文案齐备')
ok(!/tasks\.value = \[\]\n  tasksTotal/.test(vue), 'reset 不再无条件清空旧帧')
ok(/if \(round\) \{\n\s+round\.failed = true/.test(vue), '在途列跳过时回合仍收敛（keep 而非悬挂）')

const zh = JSON.parse(fs.readFileSync(path.join(root, 'src/api/frontend/shell/locales/zh/tasks.json'), 'utf8'))
const en = JSON.parse(fs.readFileSync(path.join(root, 'src/api/frontend/shell/locales/en/tasks.json'), 'utf8'))
ok(zh.board && zh.board.refresh_failed_keep && zh.board.retry, 'zh 语言包含新键')
ok(en.board && en.board.refresh_failed_keep && en.board.retry, 'en 语言包含新键')

console.log(`结果: ${pass} 通过, ${fail} 失败`)
try { fs.unlinkSync(tmpFile) } catch (e) {}
process.exit(fail === 0 ? 0 : 1)
})()