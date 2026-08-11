<template>
  <div class="page">
    <div class="cards">
      <div v-for="e in engines" :key="e.name" class="card">
        <h4>{{ e.name }} <span class="count">×{{ e.total }}</span></h4>
        <p class="meta">binary: {{ e.binary || '-' }}</p>
        <div class="health">
          <span v-for="(cnt, h) in e.health" :key="h" :class="['badge', healthClass(h)]">
            {{ healthText(h) }} {{ cnt }}
          </span>
        </div>
        <table class="ver">
          <tbody>
            <tr v-for="(cnt, v) in e.versions" :key="v">
              <td class="v">{{ v }}</td><td>{{ cnt }} 台</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <p v-if="!engines.length" class="empty">暂无引擎数据</p>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import http from '../api'

const engines = ref([])
const HEALTH = { online: ['on', '在线'], recently_lost: ['warn', '失联中'], offline: ['off', '离线'], about_to_gc: ['gc', '待回收'] }
function healthText(h) { return (HEALTH[h] || ['', h])[1] }
function healthClass(h) { return (HEALTH[h] || ['', ''])[0] }

async function load() {
  const { data } = await http.get('/engines')
  engines.value = data
}
onMounted(load)
</script>

<style scoped>
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px; }
.card { background: #fff; border-radius: 6px; padding: 16px; }
.card h4 { margin-bottom: 6px; }
.count { color: #409eff; font-size: 14px; }
.meta { color: #999; font-size: 12px; margin-bottom: 8px; }
.health { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
.badge { padding: 2px 8px; border-radius: 10px; font-size: 12px; }
.badge.on { background: #e1f3d8; color: #67c23a; }
.badge.warn { background: #fdf6ec; color: #e6a23c; }
.badge.off { background: #fde2e2; color: #f56c6c; }
.badge.gc { background: #f0f2f5; color: #909399; }
.ver { width: 100%; font-size: 13px; border-collapse: collapse; }
.ver td { padding: 4px 0; border-top: 1px solid #f5f6f8; }
.v { color: #666; }
.empty { color: #999; text-align: center; padding: 40px; }
</style>
