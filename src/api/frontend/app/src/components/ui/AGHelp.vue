<!--
  字段说明气泡：每个配置项右侧的 "?"。
  文案三段式（what / values / note），note 以 ⚠ 开头。
  点击可开（触屏），点外部/Esc 关闭；只读字段用空心样式。

  2026-11-15：气泡改成 Teleport 到 body + 计算定位（position:fixed）。
  原因（真浏览器实锤）：原来 absolute + z-index:50 挂在 .ap-group 里，而 .ap-group
  是 overflow:hidden（圆角裁切）→ 气泡被裁掉、elementFromPoint 命中的是页面容器，
  看着就是"被遮罩住看不到"。组件级修复后，任何页面的滚动/裁切容器都不再影响它。
-->
<template>
  <span class="ag-help" :class="{ ro: readonly }">
    <button
      ref="btn"
      type="button"
      class="ag-help-btn"
      :aria-expanded="open ? 'true' : 'false'"
      :aria-controls="pid"
      :aria-label="`说明：${title || ''}`"
      @click.stop="toggle"
      @keydown.esc="close"
    >?</button>
    <Teleport to="body">
      <div v-if="open" :id="pid" class="ag-help-pop" role="tooltip" :style="popStyle" @click.stop>
        <div v-if="title" class="hd">{{ title }}</div>
        <p v-if="what" class="what">{{ what }}</p>
        <p v-if="values" class="vals">{{ labelValues }}<code>{{ values }}</code></p>
        <p v-if="note" class="note">{{ note }}</p>
        <p v-if="!what && !values && !note" class="what">暂无说明（可在配置文件 _fields 补充）</p>
      </div>
    </Teleport>
  </span>
</template>

<script setup>
import { computed, onBeforeUnmount, ref } from 'vue'

defineProps({
  title: { type: String, default: '' },
  what: { type: String, default: '' },
  values: { type: String, default: '' },
  note: { type: String, default: '' },
  readonly: { type: Boolean, default: false },
})

// "取值：" 前缀随语言切换（admin.page.values_label）
const langVer = ref(0)
try { window.addEventListener('grape:langchange', () => { langVer.value += 1 }) } catch (e) { /* 忽略 */ }

const labelValues = computed(() => {
  void langVer.value
  try {
    const v = window.I18N && window.I18N.t('admin.page.values_label')
    if (v && v !== 'admin.page.values_label') return v
  } catch (e) { /* 忽略 */ }
  return '取值：'
})

const open = ref(false)
const btn = ref(null)
const popStyle = ref({ top: '0px', left: '0px' })
const pid = `aghelp-${Math.random().toString(36).slice(2, 9)}`
const POP_W = 300
const POP_H_GUESS = 160

// 以按钮矩形为锚点算 fixed 位置：下方放不下就翻到上方，左右夹在视口内
function place() {
  const el = btn.value
  if (!el) return
  const r = el.getBoundingClientRect()
  const vw = window.innerWidth || 1024
  const vh = window.innerHeight || 768
  const left = Math.max(8, Math.min(r.left, vw - POP_W - 8))
  let top = r.bottom + 6
  if (top + POP_H_GUESS > vh - 8) top = Math.max(8, r.top - POP_H_GUESS - 6)
  popStyle.value = { top: `${Math.round(top)}px`, left: `${Math.round(left)}px`, width: `${POP_W}px` }
}

function onDoc() { close() }
function onViewport() { if (open.value) place() }
function toggle() {
  if (open.value) { close(); return }
  open.value = true
  place()
  document.addEventListener('click', onDoc)
  window.addEventListener('scroll', onViewport, true)
  window.addEventListener('resize', onViewport)
}
function close() {
  open.value = false
  document.removeEventListener('click', onDoc)
  window.removeEventListener('scroll', onViewport, true)
  window.removeEventListener('resize', onViewport)
}
onBeforeUnmount(close)
</script>

<style scoped>
.ag-help { position: relative; display: inline-flex; align-items: center; margin-left: 4px; }
.ag-help-btn {
  width: 16px; height: 16px; line-height: 14px; padding: 0;
  border-radius: 50%; border: 1px solid #c9ced6; background: #fff; color: #6b7280;
  font-size: 11px; cursor: pointer;
}
.ag-help-btn:hover, .ag-help-btn[aria-expanded='true'] { border-color: #0071e3; color: #0071e3; }
.ag-help.ro .ag-help-btn { border-style: dashed; color: #9aa1ab; }
.ag-help-pop {
  /* fixed + 高 z-index：脱离 overflow:hidden 祖先的裁切，也不再被页面滚动层盖住 */
  position: fixed; z-index: 3000;
  background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0,0,0,.12); padding: 10px 12px; font-size: 12px;
  line-height: 1.5; color: #111827; text-align: left;
}
.ag-help-pop .hd { font-weight: 600; margin-bottom: 4px; }
.ag-help-pop .what { margin: 0; }
.ag-help-pop .vals { color: #374151; margin: 4px 0 0; }
.ag-help-pop .vals code { background: #f3f4f6; padding: 1px 4px; border-radius: 4px; }
.ag-help-pop .note { color: #b45309; margin: 4px 0 0; }
</style>