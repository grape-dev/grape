<!-- 仓库平台凭据行（Vue 组件）：只表达 PAT 域。
     行内 = 平台名 + PAT 徽章两态 + [配置/重新配置] + [清除]；不渲染任何 OAuth 徽章、
     绑定/解绑入口或令牌页直达（令牌页入口在 PatDialog 内）。 -->
<template>
  <div class="cred-row">
    <div class="cred-main">
      <div class="cred-name">
        <span v-html="icon"></span>
        <span class="ac__name">{{ label }}</span>
        <span
          class="cred-badge"
          :class="hasPat ? 'cred-badge--bound' : 'cred-badge--muted'"
          :title="hasPat ? t('pat_badge_manual_title') : t('pat_badge_none_title')"
          >{{ hasPat ? t('pat_badge_configured') : t('pat_badge_none') }}</span
        >
      </div>
    </div>
    <div class="cred-btns">
      <button class="bt bt-s bt-p" @click="emit('configure', platform.provider)">
        {{ hasPat ? t('pat_btn_reconfigure') : t('pat_btn_configure') }}
      </button>
      <button v-if="hasPat" class="bt bt-s" :title="t('pat_btn_clear_title')" @click="emit('clear', platform.provider)">
        {{ t('pat_btn_clear') }}
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 行样式自带（不依赖 AccountView 的 scoped class，避免右对齐失效）：
   与账号页原 .ac__prow 同款尺寸 —— flex 行、左信息右按钮 */
.cred-row {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 12px 16px;
  border: 1px solid var(--b);
  border-radius: 12px;
  background: var(--c);
  margin-bottom: 10px;
}
.cred-main {
  flex: 1;
  min-width: 0;
}
.cred-name {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 2px;
}
.cred-name .ac__name {
  font-size: 14px;
  font-weight: 600;
  color: var(--t);
}
.cred-badge {
  padding: 2px 6px;
  border-radius: 4px;
  border: 1px solid transparent;
  white-space: nowrap;
  font-size: 10px;
}
.cred-badge--bound {
  background: rgba(52, 199, 89, 0.15);
  color: #34c759;
}
.cred-badge--muted {
  background: var(--bg);
  color: var(--t3);
}
.cred-btns {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
  margin-left: auto;
}
</style>

<script setup>
import { computed } from 'vue'

const G = globalThis
const props = defineProps({
  platform: { type: Object, required: true },
  hasPat: { type: Boolean, default: false },
})
const emit = defineEmits(['configure', 'clear'])

function t(key, vars) {
  const full = 'app.account.' + key
  const got = G.I18N && G.I18N.t ? G.I18N.t(full, vars) : full
  return got && got !== full ? got : key
}
const label = computed(function () {
  const l = typeof G.window.platformLabel === 'function' ? G.window.platformLabel(props.platform.provider) : ''
  return l || props.platform.name || props.platform.provider
})
const icon = computed(function () {
  return typeof G.window.platformIcon === 'function'
    ? G.window.platformIcon(props.platform.provider, 18)
    : ''
})
</script>