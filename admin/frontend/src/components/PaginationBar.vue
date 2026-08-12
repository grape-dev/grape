<template>
  <div v-if="total > 0" class="pagination-bar">
    <span class="page-info">
      <template v-if="totalPages > 1">第 <strong>{{ page + 1 }}</strong> / {{ totalPages }} 页（共 {{ total }} {{ unit }}）</template>
      <template v-else>共 {{ total }} {{ unit }}</template>
    </span>
    <select class="page-size-select" :value="size" @change="onSizeChange">
      <option v-for="n in sizeOptions" :key="n" :value="n">{{ n }}条/页</option>
    </select>
    <button v-if="totalPages > 1" class="page-btn" :disabled="page <= 0" @click="go(page - 1)">上一页</button>
    <button v-if="totalPages > 1" class="page-btn" :disabled="page >= totalPages - 1" @click="go(page + 1)">下一页</button>
  </div>
</template>

<script setup>
// 公共分页条（对齐 VOD el-pagination 语义，自研轻量实现）：
// 分页空间始终存在——共 N 条/条数选择器常显，页码/翻页按多页显隐
// （单页时仍可调整每页条数，避免选择锁死）
defineProps({
  total: { type: Number, required: true },
  totalPages: { type: Number, required: true },
  page: { type: Number, required: true },
  size: { type: Number, required: true },
  sizeOptions: { type: Array, default: () => [10, 20, 50] },
  unit: { type: String, default: '条' }
})
const emit = defineEmits(['update:page', 'update:size', 'change'])

function onSizeChange(e) {
  emit('update:size', Number(e.target.value))
  emit('update:page', 0)
  emit('change')
}
function go(p) {
  emit('update:page', p)
  emit('change')
}
</script>
