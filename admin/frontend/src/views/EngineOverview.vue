<template>
  <div class="rgrid">
    <div v-for="e in engines" :key="e.name" class="rc">
      <div>
        <div class="rh"><span class="rn">{{ e.name }}</span><span class="t3">{{ e.binary || '' }}</span></div>
        <div class="rd mt8">共 {{ e.total }} 个实例</div>
        <div class="flex gap6 mt8" style="flex-wrap:wrap">
          <span v-for="(cnt, h) in e.health" :key="h" :class="['tg', healthClass(h)]">
            {{ healthText(h) }} {{ cnt }}
          </span>
        </div>
      </div>
      <div class="rf">
        <div class="s">版本分布</div>
      </div>
      <table class="mt8">
        <tbody>
          <tr v-for="(cnt, v) in e.versions" :key="v">
            <td class="t2">{{ v }}</td><td style="text-align:right">{{ cnt }} 台</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
  <p v-if="!engines.length" class="t3 mt16">暂无引擎数据</p>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import http from '../api'

const engines = ref([])
const HEALTH = { online: ['tg-g', '在线'], recently_lost: ['tg-s', '失联中'], offline: ['tg-d', '离线'], about_to_gc: ['tg-q', '待回收'] }
function healthText(h) { return (HEALTH[h] || ['tg-d', h])[1] }
function healthClass(h) { return (HEALTH[h] || ['tg-d', ''])[0] }

async function load() {
  const { data } = await http.get('/engines')
  engines.value = data
}
onMounted(load)
</script>
