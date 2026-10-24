<template>
  <div class="showcase">
    <header class="showcase__bar">
      <h1>@apple-ui 组件示例</h1>
      <AGButton size="sm" variant="plain" @click="toggleTheme">
        {{ theme === 'dark' ? '☀️ 亮色' : '🌙 暗色' }}
      </AGButton>
    </header>

    <section class="showcase__grid">
      <AGCard>
        <h2>Buttons</h2>
        <div class="row">
          <AGButton>Primary</AGButton>
          <AGButton variant="plain">Plain (iOS ghost)</AGButton>
          <AGButton variant="danger">Delete</AGButton>
          <AGButton size="sm">Small</AGButton>
          <AGButton disabled>Disabled</AGButton>
        </div>
      </AGCard>

      <AGCard>
        <h2>Inputs</h2>
        <div class="col">
          <AGInput v-model="demo.text" label="项目名称" placeholder="grape" />
          <AGInput v-model="demo.bad" label="校验失败" invalid hint="名称已被占用" />
          <AGSelect v-model="demo.opt" :options="opts" placeholder="选择分类" allow-placeholder />
        </div>
      </AGCard>

      <AGCard>
        <h2>Pills / Tags</h2>
        <div class="row">
          <AGPill>skill-chain</AGPill>
          <AGPill>branch: main</AGPill>
          <AGPill>weekly-08</AGPill>
        </div>
      </AGCard>

      <AGCard>
        <h2>Table</h2>
        <AGTable :columns="cols" :rows="rows" empty-text="暂无数据">
          <template #cell-status="{ row }">
            <span :class="row.status === '在线' ? 'ok' : 'warn'">{{ row.status }}</span>
          </template>
        </AGTable>
      </AGCard>

      <AGCard>
        <h2>Modal & Toast</h2>
        <div class="row">
          <AGButton variant="plain" @click="modal = true">打开弹窗</AGButton>
          <AGButton variant="plain" @click="toast = true">弹 Toast</AGButton>
        </div>
      </AGCard>

      <AGCard>
        <h2>Nav</h2>
        <AGNav>
          <RouterLink to="/">首页</RouterLink>
          <RouterLink to="/ui">组件库</RouterLink>
        </AGNav>
      </AGCard>
    </section>

    <AGModal :open="modal" title="确认操作" @close="modal = false">
      <p>这是一个毛玻璃弹窗示例，明暗主题各截一张验证。</p>
      <template #footer>
        <AGButton size="sm" variant="plain" @click="modal = false">取消</AGButton>
        <AGButton size="sm" @click="modal = false">确定</AGButton>
      </template>
    </AGModal>

    <AGToast :open="toast" message="已保存 ✓" tone="success" @close="toast = false" />
  </div>
</template>

<script setup>
import { reactive, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import AGButton from '@/components/ui/AGButton.vue'
import AGCard from '@/components/ui/AGCard.vue'
import AGInput from '@/components/ui/AGInput.vue'
import AGSelect from '@/components/ui/AGSelect.vue'
import AGModal from '@/components/ui/AGModal.vue'
import AGToast from '@/components/ui/AGToast.vue'
import AGPill from '@/components/ui/AGPill.vue'
import AGTable from '@/components/ui/AGTable.vue'
import AGNav from '@/components/ui/AGNav.vue'

const theme = ref(document.documentElement.dataset.theme || 'light')
const modal = ref(false)
const toast = ref(false)
const demo = reactive({ text: '', bad: 'grape-2', opt: '' })
const opts = [
  { value: 'vod', label: '视频' },
  { value: 'coding', label: '编码' },
  { value: 'sre', label: 'SRE' },
]
const cols = [
  { key: 'name', title: '引擎' },
  { key: 'type', title: '类型' },
  { key: 'status', title: '状态' },
]
const rows = [
  { name: 'Hermes', type: 'Agent', status: '在线' },
  { name: 'Codex', type: 'Agent', status: '离线' },
]

function toggleTheme() {
  theme.value = theme.value === 'dark' ? 'light' : 'dark'
}

watch(theme, (t) => {
  document.documentElement.dataset.theme = t
})
watch(toast, (v) => {
  if (v) setTimeout(() => (toast.value = false), 1600)
})
</script>

<style scoped>
.showcase {
  min-height: 100vh;
  background: var(--c-bg);
  color: var(--c-text);
  padding: var(--s-5);
}
.showcase__bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--s-4);
}
.showcase__bar h1 {
  font-size: var(--f-head-1);
}
.showcase__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: var(--s-4);
}
h2 {
  font-size: var(--f-head-3);
  margin-bottom: var(--s-3);
}
.row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--s-2);
  align-items: center;
}
.col {
  display: flex;
  flex-direction: column;
  gap: var(--s-3);
}
.ok {
  color: var(--c-success);
}
.warn {
  color: var(--c-warning);
}
</style>
