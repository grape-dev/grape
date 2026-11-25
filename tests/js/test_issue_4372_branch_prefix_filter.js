/*
 * Issue #4372：场景详情「高级选项」分支选择框前缀搜索（忽略大小写）。
 *
 * 运行：node tests/js/test_issue_4372_branch_prefix_filter.js
 *
 * 验证点：
 *   1. 纯函数 filterBranchesByPrefix：前缀命中、未命中、忽略大小写、空关键字、首尾空白、非数组/坏数据容错。
 *   2. DashboardView 接线：详情页已有分支与新建分支入口都使用过滤结果，空态词条存在，新建/刷新清空搜索。
 *   3. 中英文 i18n 词条齐备。
 */
const assert = require('assert')
const path = require('path')
const fs = require('fs')
const os = require('os')

const root = path.resolve(__dirname, '../..')
const LIB = path.join(root, 'src/api/frontend/app/src/lib/branchFilter.js')
const VIEW = path.join(root, 'src/api/frontend/app/src/views/DashboardView.vue')

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

;(async function main() {
  const tmpFile = path.join(os.tmpdir(), 'branchFilter-' + process.pid + '.mjs')
  fs.writeFileSync(tmpFile, fs.readFileSync(LIB, 'utf8'))
  const mod = await import('file://' + tmpFile)
  fs.unlinkSync(tmpFile)

  const list = [
    { name: 'main' },
    { name: 'feature/login', protected: true },
    { name: 'Feature/PAY' },
    { name: 'hotfix/1.0' },
  ]

  // ---- 1. 纯函数过滤 ----
  const empty = mod.filterBranchesByPrefix(list, '')
  ok(empty.length === 4, '空关键字返回全部')
  ok(mod.filterBranchesByPrefix(list, '   ').length === 4, '纯空白关键字视为空')

  const hit = mod.filterBranchesByPrefix(list, 'feat')
  ok(hit.length === 2, '前缀命中忽略大小写')
  ok(
    hit[0].name === 'feature/login' && hit[1].name === 'Feature/PAY',
    '命中保持原顺序'
  )
  ok(hit[0].protected === true, '命中保留原对象字段')

  ok(mod.filterBranchesByPrefix(list, 'MAIN').length === 1, '大写关键字命中小写分支')
  ok(mod.filterBranchesByPrefix(list, 'FeAtUrE/PaY').length === 1, '混合大小写前缀命中')

  ok(mod.filterBranchesByPrefix(list, 'nope').length === 0, '未命中返回空列表')
  ok(mod.filterBranchesByPrefix(list, 'ain').length === 0, '包含匹配不算命中（必须前缀）')
  ok(mod.filterBranchesByPrefix(list, ' feat ').length === 2, '关键字首尾空白被裁剪')

  ok(mod.filterBranchesByPrefix(null, 'main').length === 0, '非数组容错为空列表')
  ok(mod.filterBranchesByPrefix([null, { name: null }, { name: 'main' }], 'mai').length === 1, '坏数据跳过不抛错')

  ok(empty !== list, '空关键字返回副本而非原数组引用')

  // ---- 2. Vue 视图接线 ----
  const vue = fs.readFileSync(VIEW, 'utf8')
  ok(/import \{ filterBranchesByPrefix \} from '\.\.\/lib\/branchFilter\.js'/.test(vue), '引入过滤纯函数')
  ok(/const existingBranchSearch = ref\(''\)/.test(vue), '已有分支搜索状态存在')
  ok(/const newBranchRefSearch = ref\(''\)/.test(vue), '新建分支参考搜索状态存在')
  ok(
    /const filteredBranches = computed\(\(\) =>\s*filterBranchesByPrefix\(branches\.value, existingBranchSearch\.value\)/.test(
      vue
    ),
    '已有分支 computed 使用前缀过滤'
  )
  ok(
    /const filteredNewBranchRefs = computed\(\(\) =>\s*filterBranchesByPrefix\(branches\.value, newBranchRefSearch\.value\)/.test(
      vue
    ),
    '新建分支参考 computed 使用前缀过滤'
  )
  ok(/v-for="br in filteredBranches"/.test(vue), '已有分支下拉渲染过滤结果')
  ok(/v-for="br in filteredNewBranchRefs"/.test(vue), '新建分支入口渲染过滤结果')
  ok(/t\('branch_no_match'\)/.test(vue), '无匹配空态文案接线')
  ok(/disabled value=""/.test(vue), '空态以 disabled 选项承载')
  ok(/t\('branch_search_placeholder'\)/.test(vue), '搜索框占位提示接线')
  ok(
    /newBranchName\.value = ''\s*newBranchRefSearch\.value = ''\s*existingBranchSearch\.value = ''/.test(
      vue
    ),
    '新建成功后清空搜索词保证列表回显'
  )

  // ---- 3. i18n 词条 ----
  for (const lang of ['zh', 'en']) {
    const dict = JSON.parse(
      fs.readFileSync(path.join(root, `src/api/frontend/shell/locales/${lang}/dashboard.json`), 'utf8')
    )
    ok(!!dict.branch_search_placeholder, `${lang} 有 branch_search_placeholder`)
    ok(!!dict.branch_no_match, `${lang} 有 branch_no_match`)
  }

  console.log(`\n${pass} passed, ${fail} failed`)
  if (fail) process.exit(1)
})()