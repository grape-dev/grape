/*
 * Issue #4230：场景卡片把 market_description 当纯文本渲染，Markdown 原样露出。
 *
 * 运行：node tests/js/test_issue_4230_plain_text_summary.js
 *
 * 验证点：
 *   1. 标题/列表/强调/引用/HTML 标记被剥离，文字保留；
 *   2. 链接与图片语法保留可见文字，代码围栏移除、行内代码去反引号；
 *   3. 取首个非空段落，空白归一；
 *   4. 空值/纯标记返回空串，供组件走本地化兜底；
 *   5. 超长摘要优先在句末收口并加省略号，title 用不截断摘要；
 *   6. 卡片接线：摘要工具 + DEFAULTS/tf() 兜底，且不改详情页纯文本渲染。
 */
const assert = require('assert')
const path = require('path')
const fs = require('fs')
const os = require('os')

const root = path.resolve(__dirname, '../..')
const LIB = path.join(root, 'src/api/frontend/app/src/lib/plainTextSummary.js')
const VIEW = path.join(root, 'src/api/frontend/app/src/views/SceneCard.vue')
const DETAIL = path.join(root, 'src/api/frontend/app/src/views/SceneMarket.vue')
const ZH = path.join(root, 'src/api/frontend/shell/locales/zh/skills.json')
const EN = path.join(root, 'src/api/frontend/shell/locales/en/skills.json')

let pass = 0
function ok(cond, name) {
  assert.ok(cond, name)
  pass += 1
}

;(async function main() {
  const tmp = path.join(os.tmpdir(), 'plainTextSummary-' + process.pid + '.mjs')
  fs.writeFileSync(tmp, fs.readFileSync(LIB, 'utf8'))
  const mod = await import('file://' + tmp)
  const summary = mod.plainTextSummary
  const full = (s) => mod.plainTextSummary(s, 0)

  // ---- 1. 标题 / 列表 / 强调 / 引用 ----
  ok(summary('# 场景标题\n\n这是正文。') === '场景标题', '标题符剥离，取首段标题文字')
  ok(summary('## 二级**标题**') === '二级标题', '标题符与强调符同时剥离')
  ok(
    summary('1. 第一项\n2. 第二项\n- 第三项') === '第一项 第二项 第三项',
    '有序/无序列表符剥离且不残留成串列表符'
  )
  ok(summary('这是**重点**和*斜体*，还有~~删除~~。') === '这是重点和斜体，还有删除。', '强调/删除线标记剥离')
  ok(summary('> 引用内容\n> 第二行') === '引用内容 第二行', '引用符剥离')

  // ---- 2. 链接 / 图片 / 代码 / HTML ----
  ok(summary('查看[文档](https://example.com)与![架构图](/a.png)。') === '查看文档与架构图。', '链接图片保留文字')
  ok(
    summary('```js\nconst a = 1\n```\n\n使用 `npm test` 运行。') === '使用 npm test 运行。',
    '代码围栏整块移除，行内代码去反引号'
  )
  ok(summary('<p>第一段<strong>重点</strong></p><br><p>第二段</p>') === '第一段重点 第二段', '简单 HTML 标签剥离并保留文字')
  ok(summary('A &amp; B &lt;C&gt;') === 'A & B <C>', 'HTML 实体解码')

  // ---- 3. 首段 + 空白归一 ----
  ok(summary('第一段\n\n第二段') === '第一段', '取首个非空段落')
  ok(summary('  第一段   有   多余空格 \n\n第二段') === '第一段 有 多余空格', '空白归一')

  // ---- 4. 空值 / 纯标记 ----
  for (const empty of ['', '   ', null, undefined, '##   ', '```\ncode\n```', '** **']) {
    ok(summary(empty) === '', '空值/纯标记返回空串: ' + JSON.stringify(empty))
  }

  // ---- 5. 超长截断 ----
  const long = '甲'.repeat(200)
  const cut = summary(long)
  ok(Array.from(cut).length <= 141, '默认最大长度 140 + 省略号')
  ok(cut.endsWith('…'), '截断后带省略号')
  ok(!/[#*_~`]/.test(cut), '截断结果不残留 Markdown 标记')
  ok(full(long) === long, 'title 用不截断摘要')

  const sentences = '这是第一句。'.repeat(30)
  const sentenceCut = summary(sentences)
  ok(sentenceCut.endsWith('…') && sentenceCut.includes('。'), '超长优先在句末收口')
  ok(Array.from(sentenceCut).length <= 141, '句末收口仍不超过上限')

  // ---- 6. 组件与词条接线 ----
  const vue = fs.readFileSync(VIEW, 'utf8')
  ok(/from '\.\.\/lib\/plainTextSummary\.js'/.test(vue), 'SceneCard 引入摘要工具')
  ok(/plainTextSummary\(rawDesc\.value, 0\)/.test(vue), 'title 使用不截断摘要')
  ok(/plainTextSummary\(rawDesc\.value\) \|\| defaultDesc\.value/.test(vue), '摘要为空走兜底文案')
  ok(/no_description: '暂无描述'/.test(vue), 'DEFAULTS 含 no_description')
  ok(/tf\('no_description'\)/.test(vue), '兜底走 tf() 本地化')
  ok(!/\{\{\s*s\.market_description/.test(vue), '卡片不再直接插值原始 Markdown 字段')
  ok(!/v-html/.test(vue), '卡片不引入 v-html')

  const zh = JSON.parse(fs.readFileSync(ZH, 'utf8'))
  const en = JSON.parse(fs.readFileSync(EN, 'utf8'))
  ok(zh.no_description === '暂无描述', '中文词条存在')
  ok(en.no_description === 'No description', '英文词条存在')

  const detail = fs.readFileSync(DETAIL, 'utf8')
  ok(/\{\{\s*detail\.market_description \|\| detail\.description \|\| ''\s*\}\}/.test(detail), '详情页保持原有纯文本渲染（回归不变）')

  console.log('test_issue_4230_plain_text_summary: ' + pass + ' checks passed')
})().catch((e) => {
  console.error(e.stack || e)
  process.exit(1)
})