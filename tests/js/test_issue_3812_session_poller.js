/*
 * Issue #3812：会话失效后事件详情页 / Autopilot 日志仍高频轮询并静默 401 的行为回归。
 *
 * 运行：node tests/js/test_issue_3812_session_poller.js
 *
 * 验证点：
 *   1. pollGuard.degradeIntervalFor：降频步长按 base 成比例（5s→15s），显式值优先；
 *   2. pollGuard.createSessionPoller：
 *      - 未 start 不挂表；start 后订阅会话事件；先清后挂（重复 start 不双跑）；
 *      - 401 / 会话已失效 → 停机 + onSessionExpired（幂等）；401 后不再 tick；
 *      - 非 401 连续 3 次失败 → 降频，任一成功即复位常规节奏；
 *      - grape:session-expired 事件驱动停机；grape:session-restored 只在跳变时回调；
 *      - dispose 退订并清表（之后派发事件不再回调）；
 *   3. api.js：新增 grape:session-restored 广播（仅失效→恢复跳变）、「无 token 直接抛错」
 *      分支补发失效信号；
 *   4. EventView.vue / AutopilotView.vue：接入 createSessionPoller、无裸 setInterval、
 *      会话横幅 + 重新登录入口、失效态停机与卸载退订；
 *   5. 看板既有 #3734 契约不回退（IssuesView 仍走 pollGuard 决策）。
 */
const assert = require('assert')
const path = require('path')
const fs = require('fs')
const os = require('os')

const root = path.resolve(__dirname, '../..')
const LIB = path.join(root, 'src/api/frontend/app/src/lib/pollGuard.js')
const API = path.join(root, 'src/api/frontend/shell/js/api.js')
const EVENT_VIEW = path.join(root, 'src/api/frontend/app/src/views/EventView.vue')
const AUTOPILOT_VIEW = path.join(root, 'src/api/frontend/app/src/views/AutopilotView.vue')
const ISSUES_VIEW = path.join(root, 'src/api/frontend/app/src/views/IssuesView.vue')

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
function sleep(ms) {
  return new Promise(function (r) {
    setTimeout(r, ms)
  })
}
// 假 window：只实现事件订阅面（单测里绝不碰 DOM）
function makeWin() {
  const listeners = {}
  return {
    listeners: listeners,
    addEventListener: function (name, fn) {
      ;(listeners[name] = listeners[name] || []).push(fn)
    },
    removeEventListener: function (name, fn) {
      listeners[name] = (listeners[name] || []).filter(function (f) {
        return f !== fn
      })
    },
    dispatch: function (name) {
      ;(listeners[name] || []).slice().forEach(function (fn) {
        fn({ type: name })
      })
    },
    count: function (name) {
      return (listeners[name] || []).length
    },
  }
}

;(async function main() {
  const source = fs.readFileSync(LIB, 'utf8')
  const tmpFile = path.join(os.tmpdir(), 'pollGuard-3812-' + process.pid + '.mjs')
  fs.writeFileSync(tmpFile, source)
  const mod = await import('file://' + tmpFile)

  // ---- 1. 降频步长 ----
  ok(typeof mod.createSessionPoller === 'function', '导出 createSessionPoller')
  ok(mod.SESSION_EXPIRED_EVENT === 'grape:session-expired', '失效事件名与 #3734 一致')
  ok(mod.SESSION_RESTORED_EVENT === 'grape:session-restored', '新导出恢复事件名')
  ok(mod.degradeIntervalFor(5000) === 15000, '5s 轮询降频到 15s（3 倍）')
  ok(mod.degradeIntervalFor(2000, 10000) === 10000, '显式降频值优先（2s→10s）')
  ok(mod.degradeIntervalFor(3000, 10000) === 10000, '3s 日志轮询降频 10s')
  ok(mod.degradeIntervalFor(30000) === 90000, '缺省三倍放大（30s→90s）')

  // ---- 2. 轮询器：挂表 / 先清后挂 / 401 停机 ----
  let ticks = 0
  let expiredCb = 0
  let restoredCb = 0
  const win = makeWin()
  const p = mod.createSessionPoller({
    intervalMs: 5,
    degradeIntervalMs: 20,
    tick: function () {
      ticks++
    },
    onSessionExpired: function () {
      expiredCb++
    },
    onSessionRestored: function () {
      restoredCb++
    },
    window: win,
  })
  ok(p.isRunning() === false, '未 start 不挂表')
  ok(p.isExpired() === false, '初始非失效态')
  p.start()
  ok(p.isRunning() === true, 'start 后挂表')
  ok(win.count('grape:session-expired') === 1, 'start 订阅会话失效事件')
  ok(win.count('grape:session-restored') === 1, 'start 订阅会话恢复事件')
  await sleep(40)
  ok(ticks >= 3, '定时器按 base 周期触发 tick')
  const before = ticks
  p.start() // 重复 start 必须「先清后挂」，不得出现双定时器
  await sleep(40)
  ok(ticks - before <= 14, '重复 start 不产生双跑定时器（实际增量 ' + (ticks - before) + '）')
  ok(p.noteError({ status: 401 }) === 'stop', '401 → stop')
  ok(p.isExpired() === true, '401 置失效态')
  ok(p.isRunning() === false, '401 后定时器被清')
  ok(expiredCb === 1, '401 触发停机回调一次')
  const afterStop = ticks
  await sleep(30)
  ok(ticks === afterStop, '停机后不再 tick（5 分钟内 401 归零的前提）')
  ok(p.noteError({ status: 401 }) === 'stop', '失效后再失败仍返回 stop')
  ok(expiredCb === 1, '停机回调幂等（不重复亮横幅）')
  p.dispose()
  ok(p.isRunning() === false, 'dispose 清表')
  ok(win.count('grape:session-expired') === 0, 'dispose 退订失效事件')
  ok(win.count('grape:session-restored') === 0, 'dispose 退订恢复事件')

  // ---- 3. 非 401 连续失败降频 / 成功复位 ----
  const win2 = makeWin()
  const p2 = mod.createSessionPoller({
    intervalMs: 5,
    degradeIntervalMs: 20,
    tick: function () {},
    window: win2,
  })
  p2.start()
  ok(p2.noteError({ status: 500 }) === 'continue', '非 401 第 1 次失败 → 继续')
  ok(p2.noteError({ status: 500 }) === 'continue', '非 401 第 2 次失败 → 继续')
  ok(p2.noteError({}) === 'degrade', '非 401 连续 3 次失败（含网络错误）→ 降频')
  ok(p2.currentInterval() === 20, '降频后间隔 = degradeIntervalMs')
  ok(p2.isRunning() === true, '降频不退化为停机')
  ok(p2.isExpired() === false, '降频不是失效态（不误报会话失效）')
  p2.noteSuccess()
  ok(p2.currentInterval() === 5, '任一次成功即复位常规节奏')
  ok(p2.failureCount() === 0, '成功清零失败计数')
  p2.dispose()

  // ---- 4. 会话事件驱动：失效 → 停机；恢复 → 仅跳变回调 ----
  const win3 = makeWin()
  let t3 = 0
  let ev3 = 0
  let rs3 = 0
  const p3 = mod.createSessionPoller({
    intervalMs: 5,
    tick: function () {
      t3++
    },
    onSessionExpired: function () {
      ev3++
    },
    onSessionRestored: function () {
      rs3++
    },
    window: win3,
  })
  p3.start()
  await sleep(20)
  ok(t3 >= 1, '事件接管前正常 tick')
  win3.dispatch('grape:session-expired')
  ok(p3.isExpired() === true, 'grape:session-expired → 失效态')
  ok(p3.isRunning() === false, 'grape:session-expired → 定时器清空')
  const t3b = t3
  await sleep(20)
  ok(t3 === t3b, '事件停机后不再 tick')
  win3.dispatch('grape:session-expired')
  ok(ev3 === 1, '重复失效事件不重复回调（幂等）')
  win3.dispatch('grape:session-restored')
  ok(p3.isExpired() === false, 'grape:session-restored → 复位失效态')
  ok(rs3 === 1, '恢复事件触发一次回调')
  win3.dispatch('grape:session-restored')
  ok(rs3 === 1, '无失效态时不重复触发恢复回调（只看跳变）')
  p3.dispose()

  // ---- 5. subscribe:false 不订阅（视图自己挂桥时用）----
  const win4 = makeWin()
  const p4 = mod.createSessionPoller({ intervalMs: 5, subscribe: false, tick: function () {}, window: win4 })
  p4.start()
  ok(win4.count('grape:session-expired') === 0, 'subscribe:false 不订阅事件')
  p4.dispose()

  // ---- 6. api.js：恢复广播 + 无 token 补发失效信号 ----
  const api = fs.readFileSync(API, 'utf8')
  ok(/grape:session-restored/.test(api), 'api.js 广播会话恢复事件')
  ok(
    /dispatchEvent\(\s*new CustomEvent\(\s*['"]grape:session-restored['"]/.test(api),
    '恢复事件用 CustomEvent 广播'
  )
  ok(/function _notifySessionRestored\(\)/.test(api), '恢复广播有独立封装')
  ok(
    /if \(_sessionExpiredNotified\) \{[\s\S]{0,120}?_notifySessionRestored\(\)/.test(api),
    '恢复广播仅在「失效→恢复」跳变时发出'
  )
  ok(
    /if \(!localStorage\.getItem\('token'\)\) _notifySessionExpired\(\)/.test(api),
    '无 token 直接抛错分支补发失效信号（#3812 第 3 项）'
  )

  // ---- 7. EventView.vue 接线 ----
  const ev = fs.readFileSync(EVENT_VIEW, 'utf8')
  ok(/import \{ createSessionPoller \} from '\.\.\/lib\/pollGuard\.js'/.test(ev), '事件页引入 createSessionPoller')
  ok(!/\bsetInterval\(/.test(ev), '事件页不再有裸 setInterval（详情 5s / 日志 5s 全走封装）')
  ok(/const detailPoller = createSessionPoller\(/.test(ev), '事件详情轮询器实例')
  ok(/const tlStatusPoller = createSessionPoller\(/.test(ev), '任务日志状态轮询器实例')
  ok(/EVENT_POLL_INTERVAL_MS = 5000/.test(ev), '事件详情保留 5s 常规节奏')
  ok(/EVENT_POLL_DEGRADE_MS = 15000/.test(ev), '事件详情降频 15s')
  ok(/const sessionExpired = ref\(false\)/.test(ev), '事件页有 sessionExpired 状态')
  ok(/ev-session-banner/.test(ev), '事件页会话失效横幅进模板')
  ok(/function markSessionExpired\(\)/.test(ev), '事件页失效收敛函数')
  ok(
    /function markSessionExpired\(\) \{[\s\S]{0,200}?stopPoll\(\)[\s\S]{0,200}?tlStatusPoller\.stop\(\)/.test(ev),
    '失效收敛清两类定时器'
  )
  ok(/addEventListener\('grape:session-expired', markSessionExpired\)/.test(ev), 'onMounted 监听失效事件')
  ok(
    /removeEventListener\('grape:session-expired', markSessionExpired\)/.test(ev),
    'onBeforeUnmount 移除失效监听'
  )
  ok(/detailPoller\.dispose\(\)/.test(ev), '卸载时 dispose 详情轮询器')
  ok(/tlStatusPoller\.dispose\(\)/.test(ev), '卸载时 dispose 日志轮询器')
  ok(/detailPoller\.noteError\(e\)/.test(ev), '详情 401 走 noteError 停机（不再静默吞错）')
  ok(/tb\('session_expired'\)/.test(ev) && /tb\('relogin'\)/.test(ev), '横幅复用看板文案键（不新增中文）')
  ok(/httpErr\.status = resp\.status/.test(ev), '任务日志流错误带 HTTP 状态码')
  ok(
    /err\.status === 401[\s\S]{0,120}?markSessionExpired\(\)/.test(ev),
    '日志流 401 直接停机（不每 2s 重连打点）'
  )
  ok(/'tasks\.board\.' \+ key/.test(ev), '文案取 tasks.board.* 语言包键')

  // ---- 8. AutopilotView.vue 接线 ----
  const ap = fs.readFileSync(AUTOPILOT_VIEW, 'utf8')
  ok(
    /import \{ createSessionPoller \} from '\.\.\/lib\/pollGuard\.js'/.test(ap),
    'Autopilot 页引入 createSessionPoller'
  )
  ok(!/\bsetInterval\(/.test(ap), 'Autopilot 页不再有裸 setInterval（2s / 3s 全走封装）')
  ok(/const logDlgPoller = createSessionPoller\(/.test(ap), '日志弹窗轮询器实例（2s）')
  ok(/const logPollers = \{\}/.test(ap), '执行卡片轮询器表（替代 logPollTimers）')
  ok(/APV_LOG_POLL_INTERVAL_MS = 2000/.test(ap), '弹窗日志保留 2s 常规节奏')
  ok(/APV_EXEC_POLL_INTERVAL_MS = 3000/.test(ap), '执行卡片保留 3s 常规节奏')
  ok(/const sessionExpired = ref\(false\)/.test(ap), 'Autopilot 页有 sessionExpired 状态')
  ok(/apv-session-banner/.test(ap), 'Autopilot 页会话失效横幅进模板')
  ok(
    /function markSessionExpired\(\) \{[\s\S]{0,240}?stopLogTimer\(\)[\s\S]{0,240}?stopAllLogPolls\(\)/.test(ap),
    '失效收敛清弹窗 + 全部执行卡片轮询'
  )
  ok(/Object\.keys\(logPollers\)\.forEach\(stopLogPoll\)/.test(ap), 'stopAllLogPolls 整表清空')
  ok(/function resumeAfterSessionRestored\(\)/.test(ap), '登录恢复后按打开态重建轮询')
  ok(/if \(sessionExpired\.value\) return/.test(ap), '失效态不新挂轮询（打开日志不复活打点）')
  ok(/addEventListener\('grape:session-expired', markSessionExpired\)/.test(ap), 'onMounted 监听失效事件')
  ok(
    /removeEventListener\('grape:session-expired', markSessionExpired\)/.test(ap),
    'onBeforeUnmount 移除失效监听'
  )
  ok(/logDlgPoller\.dispose\(\)/.test(ap), '卸载时 dispose 弹窗轮询器')
  ok(/tb\('session_expired'\)/.test(ap) && /tb\('relogin'\)/.test(ap), '横幅复用看板文案键（不新增中文）')

  // ---- 9. #3734 契约不回退 ----
  const issues = fs.readFileSync(ISSUES_VIEW, 'utf8')
  ok(/from '\.\.\/lib\/pollGuard\.js'/.test(issues), '看板仍复用 pollGuard')
  ok(/nextPollAction\(/.test(issues), '看板仍用 nextPollAction 决策')
  ok(/isk-session-banner/.test(issues), '看板横幅未被改动')

  console.log(`结果: ${pass} 通过, ${fail} 失败`)
  try {
    fs.unlinkSync(tmpFile)
  } catch (e) {}
  process.exit(fail === 0 ? 0 : 1)
})().catch(function (e) {
  console.error(e && e.stack ? e.stack : e)
  process.exit(1)
})