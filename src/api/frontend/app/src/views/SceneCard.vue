<template>
  <div class="scn-card" @click="$emit('open')">
    <div :class="['scn-card-bar', catBg]"></div>
    <div class="scn-card-body">
      <div class="scn-card-head">
        <div :class="['scn-ic', catBg]">
          <img
            v-if="iconIsUrl"
            :src="s.icon"
            alt=""
            :data-fallback="s.category_icon || '⚙'"
            @error="onIconErr"
          />
          <template v-else>{{ iconText }}</template>
        </div>
        <div class="scn-card-title">{{ s.display_name || s.name }}</div>
        <span v-if="s.is_featured" class="scn-star">🌟</span>
      </div>
      <div class="scn-card-desc" :title="s.market_description || s.description || ''">
        {{ s.market_description || s.description || '' }}
      </div>
      <div v-if="catPath" class="scn-cat">{{ catPath }}</div>
      <div v-if="tags.length" class="scn-chips">
        <span v-for="(t, i) in tags" :key="'t' + i" class="scn-chip">{{ t.name }}</span>
      </div>
      <div class="scn-chain">
        <template v-for="(sk, i) in skills" :key="i">
          <span v-if="i > 0" class="scn-arr">→</span>
          <span class="scn-tag">{{ sk.name || sk.skill || sk }}</span>
        </template>
      </div>
      <div class="scn-card-foot">
        <span class="scn-meta">
          {{ skills.length }} {{ tf('skills_suffix') }} · {{ s.repo_count || 0 }}
          {{ tf('repos_in_use') }}
        </span>
        <span class="scn-like" @click.stop="$emit('like')">
          {{ s.liked_by_me ? '❤️' : '🤍' }} <span>{{ s.like_count || 0 }}</span>
        </span>
        <div class="scn-card-btns">
          <button class="bt bt-o bt-s" @click.stop="$emit('apply')">{{ tf('apply_scene') }}</button>
          <button
            v-if="s.recommended_template"
            class="bt bt-o bt-s"
            @click.stop="$emit('experience')"
          >
            {{ tf('experience') }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({ s: { type: Object, required: true } })
defineEmits(['open', 'apply', 'experience', 'like'])

const G = globalThis
const CAT_BG = {
  1: 'bg-default',
  2: 'bg-harmony',
  3: 'bg-iac',
  4: 'bg-skill',
  5: 'bg-agent',
  6: 'bg-blog',
}
const DEFAULTS = {
  skills_suffix: '技能',
  repos_in_use: '仓库在用',
  apply_scene: '应用',
  experience: '体验',
}
const catBg = computed(() => CAT_BG[props.s.category_id] || 'bg-default')
const skills = computed(() => props.s.skills || [])
const tags = computed(() => (props.s.tags || []).slice(0, 3))
const catPath = computed(() => {
  const c = props.s.category || {}
  const l1 = c.l1 && c.l1.name
  const l2 = c.l2 && c.l2.name
  if (l1 && l2) return l1 + ' / ' + l2
  return l1 || l2 || props.s.category_name || ''
})
const iconIsUrl = computed(() => {
  const v = props.s.icon || ''
  return v.startsWith('http') || v.startsWith('/static/') || v.startsWith('/uploads/')
})
const iconText = computed(() => props.s.icon || '⚙')

// I18N 陷阱：t(key) 找不到返回 key 本身，须与完整 key 比对再兜底
function tf(key) {
  const full = 'skills.' + key
  const v = G.I18N.t(full)
  return v && v !== full ? v : DEFAULTS[key] || key
}
function onIconErr(e) {
  e.target.style.display = 'none'
  e.target.parentElement.innerText = e.target.dataset.fallback || '⚙'
}
</script>

<style scoped>
.scn-like {
  cursor: pointer;
  font-size: 11px;
  color: var(--t2);
  display: flex;
  align-items: center;
  gap: 3px;
}
.scn-like span {
  font-size: 11px;
}
.scn-arr {
  color: var(--t3);
  font-size: 10px;
}
.scn-cat {
  font-size: 10px;
  color: var(--t3);
  margin-top: 2px;
}
.scn-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.scn-chip {
  font-size: 10px;
  line-height: 1;
  padding: 2px 6px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--a) 12%, transparent);
  color: var(--a);
}
/* scn-card / scn-card-bar / scn-card-body / scn-card-head / scn-card-title /
   scn-card-desc / scn-chain / scn-tag / scn-card-foot / scn-meta / scn-card-btns /
   scn-ic / bg-* / bt 均沿用老壳 app.css —— 该文件继续被 scene detail 等引用，不删 */
</style>
