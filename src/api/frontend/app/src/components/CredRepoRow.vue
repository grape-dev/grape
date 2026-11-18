<!-- 仓库平台凭据行（Vue 组件）：只表达 PAT 域。
     行内 = 平台名 + PAT 徽章 + [配置/重新配置] + [清除]；不渲染任何 OAuth 徽章、
     绑定/解绑入口或令牌页直达（令牌页入口在 PatDialog 内）。
     2026-11-18：徽章四态——已配置 / 已损坏（密文解不开）/ 由登录授权提供 / 未配置。
     * 后两态的区分很重要：以前 OAuth 令牌被算成 PAT，页面写 PAT 已配置、点清除又清不掉。 -->
<template>
  <div class="cred-row">
    <div class="cred-main">
      <div class="cred-name">
        <span v-html="icon"></span>
        <span class="ac__name">{{ label }}</span>
        <span
          class="cred-badge"
          :class="badge.cls"
          :title="badge.title"
          >{{ badge.text }}</span
        >
      </div>
      <div v-if="!hasPat && (patBroken || tokenSource === 'oauth')" class="cred-sub">
        {{ patBroken ? t('pat_broken_desc') : t('pat_oauth_desc') }}
      </div>
    </div>
    <div class="cred-btns">
      <button class="bt bt-s bt-p" @click="emit('configure', platform.provider)">
        {{ hasPat || patBroken ? t('pat_btn_reconfigure') : t('pat_btn_configure') }}
      </button>
      <button v-if="hasPat" class="bt bt-s bt-o" :title="t('pat_btn_clear_title')" @click="emit('clear', platform.provider)">
        {{ t('pat_btn_clear') }}
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 行尺寸（padding/gap/圆角/边框/阴影）由 app.css §23c 行容器单一源统一提供，
   与账号页 `.ac__prow` 同款且同源（Issue #3081 第 5 条：12·16 vs 14·16 的 4px 差已消除）。 */
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
.cred-badge--warn {
  background: color-mix(in srgb, var(--y) 16%, transparent);
  color: var(--y);
}
.cred-badge--oauth {
  background: color-mix(in srgb, var(--a) 14%, transparent);
  color: var(--a);
}
.cred-sub {
  font-size: 11px;
  color: var(--t3);
  line-height: 1.45;
  margin-top: 2px;
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
  patBroken: { type: String, default: '' },
  tokenSource: { type: String, default: '' },
  oauthLogin: { type: String, default: '' },
})
const emit = defineEmits(['configure', 'clear'])

function t(key, vars) {
  const full = 'app.account.' + key
  const got = G.I18N && G.I18N.t ? G.I18N.t(full, vars) : full
  return got && got !== full ? got : key
}
const badge = computed(function () {
  if (props.hasPat) {
    return { text: t('pat_badge_configured'), title: t('pat_badge_manual_title'), cls: 'cred-badge--bound' }
  }
  // 2026-11-18：OAuth 可用时优先报"由登录授权提供"——该平台实际是通的，
  // 让"PAT 已损坏"压在上面会把可用平台渲染成故障（评审实测：账号既有可用
  // OAuth 又有坏 PAT）。坏 PAT 仍在下一行副文案里提示重配。
  if (props.tokenSource === 'oauth') {
    return {
      text: t('pat_badge_oauth'),
      title: t('pat_oauth_desc') + (props.oauthLogin ? '（' + props.oauthLogin + '）' : ''),
      cls: 'cred-badge--oauth',
    }
  }
  if (props.patBroken) {
    return { text: t('pat_badge_broken'), title: t('pat_broken_desc'), cls: 'cred-badge--warn' }
  }
  return { text: t('pat_badge_none'), title: t('pat_badge_none_title'), cls: 'cred-badge--muted' }
})
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