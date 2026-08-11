<template>
  <div>
    <!-- 统计块 -->
    <div class="stats mb12">
      <div class="st"><span class="v">{{ engines.length }}</span><span class="l">引擎类型</span></div>
      <div class="st"><span class="v">{{ totalInstances }}</span><span class="l">实例总数</span></div>
      <div class="st"><span class="v tgx">{{ onlineCount }}</span><span class="l">在线</span></div>
      <div class="st"><span class="v tr">{{ offlineCount }}</span><span class="l">离线</span></div>
    </div>

    <!-- 引擎卡片网格（对齐 Grape runtimes 页 Engines 视图） -->
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px">
      <div v-for="e in engines" :key="e.name"
           style="background:var(--c);border:1px solid var(--b);border-radius:var(--r2);padding:12px 14px;box-shadow:var(--sh)">
        <!-- 头部：健康徽章 + 名称 + 版本 -->
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
          <span v-for="(cnt, h) in e.health" :key="h"
                :style="badgeStyle(h)">
            <span :style="dotStyle(h)"></span>{{ healthText(h) }} {{ cnt }}
          </span>
          <span style="font-weight:600;font-size:14px">
            {{ e.name }}
            <span style="font-weight:400;color:var(--t3);font-size:12px">
              x{{ e.total }} 实例<span v-if="e.binary"> · {{ e.binary }}</span>
            </span>
          </span>
        </div>
        <!-- 版本分布 -->
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:12px;color:var(--t2)">
          <div v-for="(cnt, v) in e.versions" :key="v">
            <span style="color:var(--t3);display:inline-block;min-width:46px">版本</span> {{ v || '(未知)' }}
            <span style="color:var(--t3);float:right">{{ cnt }} 台</span>
          </div>
        </div>
      </div>
    </div>
    <p v-if="!engines.length" class="ts t2" style="padding:12px">暂无引擎</p>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import http from '../api'

const engines = ref([])

// 对齐 Grape HEALTH_BADGE 配色
const BADGE = {
  online: { color: 'var(--g)', bg: '#dcfce7', label: '在线' },
  recently_lost: { color: 'var(--or)', bg: '#fef3c7', label: '刚失联' },
  offline: { color: 'var(--t3)', bg: '#eef0f3', label: '离线' },
  about_to_gc: { color: 'var(--r)', bg: '#fee2e2', label: '待回收' },
  revoked: { color: 'var(--t3)', bg: '#eef0f3', label: '已吊销' }
}
function healthText(h) { return (BADGE[h] || { label: h }).label }
function badgeStyle(h) {
  const b = BADGE[h] || { color: 'var(--t3)', bg: '#eef0f3' }
  return {
    display: 'inline-flex', alignItems: 'center', gap: '5px', fontSize: '12px',
    fontWeight: 600, color: b.color, background: b.bg, padding: '3px 10px',
    borderRadius: '999px', whiteSpace: 'nowrap'
  }
}
function dotStyle(h) {
  const b = BADGE[h] || { color: 'var(--t3)' }
  return { width: '7px', height: '7px', borderRadius: '50%', background: b.color, display: 'inline-block' }
}

const totalInstances = computed(() => engines.value.reduce((s, e) => s + e.total, 0))
const onlineCount = computed(() => engines.value.reduce((s, e) => s + (e.health?.online || 0), 0))
const offlineCount = computed(() => engines.value.reduce((s, e) => s + (e.health?.offline || 0) + (e.health?.recently_lost || 0) + (e.health?.about_to_gc || 0), 0))

async function load() {
  const { data } = await http.get('/engines')
  engines.value = data
}
onMounted(load)
</script>
