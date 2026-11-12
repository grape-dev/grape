<template>
  <div class="lab">
    <header class="lab__bar">
      <div class="lab__title">
        <h1>主题试点 · mac 深色 / Win Fluent 深色</h1>
        <p>
          同一套 AG 组件 + 同一份 mock 数据，只切皮肤变量。左右对照可判断「深色是不是浅色的反相」。
        </p>
      </div>
      <div class="lab__ctrl">
        <div class="chips">
          <button
            v-for="s in skins"
            :key="s.k"
            :class="{ on: shown.includes(s.k) }"
            @click="toggleSkin(s.k)"
          >
            {{ s.label }}
          </button>
        </div>
        <div class="chips">
          <button :class="{ on: mode === 'split' }" @click="setMode('split')">双栏对照</button>
          <button :class="{ on: mode === 'single' }" @click="setMode('single')">单栏</button>
        </div>
      </div>
    </header>

    <main class="lab__stage" :class="'lab__stage--' + mode">
      <section v-for="s in visible" :key="s.k" class="frame">
        <div class="frame__tab">
          <span class="frame__dot" :style="{ background: s.dot }"></span>
          <span class="frame__name">{{ s.label }}</span>
          <span class="frame__note">{{ s.note }}</span>
        </div>
        <div class="frame__body" :class="{ 'frame__body--scroll': mode === 'single' }">
          <ThemeMock :skin="s.k" />
        </div>
      </section>
    </main>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import ThemeMock from '@/components/pilot/ThemeMock.vue'

const skins = [
  {
    k: 'mac-dark',
    label: 'macOS 深色',
    note: 'Liquid Glass · 18px 圆角 · 玻璃模糊 · #0A84FF',
    dot: '#0a84ff',
  },
  {
    k: 'win-dark',
    label: 'Windows Fluent 深色',
    note: 'Mica 实体层 · 8px 圆角 · 无模糊 · #0078D4',
    dot: '#0078d4',
  },
  {
    k: 'mac-light',
    label: '浅色（现状参考）',
    note: 'Liquid Glass 浅色 · 用来判断深色是否有质感差异',
    dot: '#f5f5f7',
  },
]

const mode = ref('split')
const shown = ref(['mac-dark', 'win-dark'])

const visible = computed(() => skins.filter((s) => shown.value.includes(s.k)))

function toggleSkin(k) {
  const next = shown.value.includes(k) ? shown.value.filter((x) => x !== k) : [...shown.value, k]
  if (!next.length) return
  shown.value = skins.filter((s) => next.includes(s.k)).map((s) => s.k)
  if (shown.value.length > 1) mode.value = 'split'
}
function setMode(m) {
  mode.value = m
  if (m === 'single' && shown.value.length > 1) shown.value = [shown.value[0]]
}
</script>

<style scoped>
.lab {
  min-height: 100vh;
  background: var(--c-bg);
  color: var(--c-text);
  font-family: var(--f-family);
  display: flex;
  flex-direction: column;
}
.lab__bar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--s-4);
  padding: var(--s-4) var(--s-5);
  border-bottom: 1px solid var(--glass-border);
  background: var(--glass-bg-strong);
  -webkit-backdrop-filter: blur(var(--glass-blur));
  backdrop-filter: blur(var(--glass-blur));
  position: sticky;
  top: 0;
  z-index: 20;
}
.lab__title h1 {
  margin: 0;
  font-size: var(--f-head-2);
  letter-spacing: -0.01em;
}
.lab__title p {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--c-text-2);
}
.lab__ctrl {
  display: flex;
  gap: var(--s-3);
  flex-wrap: wrap;
  justify-content: flex-end;
}
.chips {
  display: flex;
  gap: 2px;
  padding: 2px;
  border-radius: var(--r-pill);
  background: var(--glass-bg);
  box-shadow: 0 0 0 1px var(--glass-border);
}
.chips button {
  border: 0;
  background: transparent;
  color: var(--c-text-2);
  font-family: inherit;
  font-size: 12px;
  padding: 5px 12px;
  border-radius: var(--r-pill);
  cursor: pointer;
  transition: 0.16s ease;
  white-space: nowrap;
}
.chips button:hover {
  color: var(--c-text);
}
.chips button.on {
  background: var(--c-accent);
  color: #fff;
  font-weight: 600;
}

.lab__stage {
  flex: 1;
  display: grid;
  gap: var(--s-3);
  padding: var(--s-4);
  min-height: 0;
}
.lab__stage--split {
  grid-template-columns: 1fr 1fr;
}
.lab__stage--single {
  grid-template-columns: 1fr;
}
.frame {
  display: flex;
  flex-direction: column;
  min-width: 0;
  border-radius: 20px;
  overflow: hidden;
  box-shadow:
    0 0 0 1px var(--glass-border),
    0 18px 48px rgba(0, 0, 0, 0.28);
  background: var(--c-bg-elevated);
}
.frame__tab {
  display: flex;
  align-items: center;
  gap: var(--s-2);
  padding: 8px var(--s-4);
  font-size: 12px;
  color: var(--c-text-2);
  background: var(--glass-bg-strong);
  border-bottom: 1px solid var(--glass-border);
}
.frame__dot {
  width: 9px;
  height: 9px;
  border-radius: var(--r-pill);
  box-shadow: 0 0 0 3px var(--c-accent-soft);
}
.frame__name {
  font-weight: 600;
  color: var(--c-text);
}
.frame__note {
  font-size: 11px;
  color: var(--c-text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.frame__body {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
.frame__body--scroll {
  overflow-y: auto;
}

@media (max-width: 1100px) {
  .lab__stage--split {
    grid-template-columns: 1fr;
  }
  .lab__bar {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
