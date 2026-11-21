<template>
  <div class="project-page">
    <div v-if="!loaded" class="loading">{{ t('templates.loading') }}</div>
    <template v-else-if="error">
      <div class="error">{{ t('templates.load_failed', { msg: error }) }}</div>
    </template>
    <template v-else-if="categories.length === 0">
      <div class="lobby-empty tv-empty">
        <div class="lobby-empty-ic">📦</div>
        <h3>{{ t('templates.no_templates_title') }}</h3>
        <p>{{ t('templates.no_templates_desc') }}</p>
      </div>
    </template>
    <template v-else>
      <div class="tmpl-search-wide">
        <h2>{{ t('templates.gallery_title') }}</h2>
        <div class="tmpl-search-bar-inline">
          <input
            v-model="searchInput"
            type="text"
            :placeholder="t('templates.search_placeholder')"
            @keydown.enter="doSearch"
          />
          <button @click="doSearch">{{ t('templates.search_btn') }}</button>
        </div>
      </div>
      <div class="template-layout">
        <div class="template-layout-main">
          <div class="tmpl-tabs">
            <button
              v-for="(x, i) in categories"
              :key="x.id"
              class="tmpl-tab"
              :class="{ 'tmpl-tab-act': i === activeCat, 'tmpl-tab-fav': x.id === '_fav' }"
              @click="switchCat(i)"
            >
              <span class="tmpl-tab-ic">{{ x.icon || '📦' }}</span>
              <span class="tmpl-tab-name">{{ catName(x.name) }}</span>
              <span class="tmpl-tab-cnt">{{ (x.templates || []).length }}</span>
            </button>
          </div>
          <div class="template-list">
            <div
              v-for="tp in pageTemplates"
              :key="tp.id"
              class="template-card-item"
              @click="openCreate(tp)"
            >
              <span class="template-card-item-ic">{{ tp.icon || '📦' }}</span>
              <div class="template-card-item-info">
                <div class="template-card-item-name" :title="tp.name || ''">
                  {{ tp.name || tp.id || '' }}
                  <span v-if="platformLabel(tp)" class="tg tg-d tmpl-platform-tag">{{
                    platformLabel(tp)
                  }}</span>
                </div>
                <div class="template-card-item-cat">
                  {{ catName(tp._cat_name || activeCategory.name || '') }}
                </div>
                <div class="template-card-item-desc">
                  {{ tp.description || t('templates.no_description') }}
                </div>
                <div
                  v-if="tp.recommend_reason"
                  class="template-card-item-reason"
                  :title="tp.recommend_reason"
                >
                  💡 {{ tp.recommend_reason }}
                </div>
              </div>
              <span
                class="tmpl-fav-btn"
                :class="{ 'tmpl-fav-act': isFav(tp.id) }"
                style="
                  font-size: 16px;
                  cursor: pointer;
                  flex-shrink: 0;
                  padding: 0 2px;
                  line-height: 1;
                "
                @click.stop="toggleFav(tp.id)"
                >{{ isFav(tp.id) ? '★' : '☆' }}</span
              >
              <button class="template-card-item-btn" @click.stop="openCreate(tp)">
                {{ t('templates.create_from_template_arrow') }}
              </button>
            </div>
            <div v-if="pages > 1" class="top1000-pager" style="grid-column: 1/-1">
              <button :disabled="page === 0" @click="goPage(-1)">‹</button>
              <span>{{ page + 1 }} / {{ pages }}</span>
              <button :disabled="page >= pages - 1" @click="goPage(1)">›</button>
            </div>
          </div>
        </div>
        <div class="template-layout-side">
          <div class="feat-single">
            <div class="feat-single-h">{{ t('templates.featured_title') }}</div>
            <div class="feat-single-body">
              <div
                v-for="(x, si) in featuredCats"
                :key="x.id"
                class="feat-item"
                @click="onFeaturedClick(si)"
              >
                <template v-if="x.templates && x.templates.length > 0">
                  <span class="feat-item-ic">{{ x.templates[0].icon || '📄' }}</span>
                  <div class="feat-item-info">
                    <span class="feat-item-name">{{
                      x.templates[0].name || x.templates[0].display_name || x.templates[0].id || ''
                    }}</span>
                    <span class="feat-item-cat">{{ x.name }}</span>
                  </div>
                </template>
                <template v-else>
                  <span class="feat-item-ic">⏳</span>
                  <div class="feat-item-info">
                    <span class="feat-item-name" style="color: var(--t3); font-size: 12px">{{
                      t('templates.loading')
                    }}</span>
                    <span class="feat-item-cat">{{ catName(x.name) }}</span>
                  </div>
                </template>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- 弹窗 Teleport 到 body：祖先 .lobby-main 带 z-index:2 形成独立层叠上下文，
         留在原地时 overlay 的 z-index 再高也只能在该上下文内排序，压不过
         .lobby-head 的 z-index:3（顶栏按钮会浮在遮罩上） -->
    <Teleport to="body">
    <!-- fork modal -->
    <div v-if="forkModal.open" class="tv-modal-overlay">
      <div class="cd tv-fork-modal">
        <div class="tv-modal-head">
          <div class="tv-modal-head-inner">
            <span class="tv-modal-ic" style="font-size: 28px">{{ forkModal.t.icon || '📦' }}</span>
            <div class="tv-modal-title">
              <div class="tv-modal-name-row">
                <span class="tv-modal-name" style="font-size: 16px">{{
                  forkModal.t.name || forkModal.t.id
                }}</span>
                <span class="tg tg-d tmpl-platform-tag">{{
                  platformName(forkModal.t.source_platform)
                }}</span>
              </div>
              <div style="font-size: 12px; color: var(--t3); margin-top: 4px">
                {{ t('templates.forking') }}
              </div>
            </div>
          </div>
        </div>
        <div style="flex: 1; min-height: 0; overflow-y: auto; padding: 20px 28px">
          <div class="tv-progress" style="margin: 0; padding: 0">
            <div class="tv-progress-txt">{{ forkModal.progress.text }}</div>
            <div class="tv-progress-bar">
              <div class="tv-progress-fill" :style="{ width: forkModal.progress.pct + '%' }"></div>
            </div>
            <div class="tv-progress-pct">{{ forkModal.progress.pct }}%</div>
          </div>
        </div>
      </div>
    </div>

    <!-- create-from-template modal (same structure as legacy, one template at a time) -->
    <div v-if="modal.open" class="tv-modal-overlay">
      <div class="cd tv-modal">
        <div class="tv-modal-head">
          <div class="tv-modal-head-inner">
            <span class="tv-modal-ic">{{ modal.t.icon || '📦' }}</span>
            <div class="tv-modal-title">
              <div class="tv-modal-name-row">
                <span class="tv-modal-name">{{ modal.t.name || modal.t.id }}</span>
                <span v-if="platformLabel(modal.t)" class="tg tg-d tmpl-platform-tag">{{
                  platformLabel(modal.t)
                }}</span>
              </div>
              <div class="tv-modal-meta">
                <span v-if="modal.t.source === 'local'" class="tv-badge tv-badge-local"
                  >{{ t('templates.local_badge') }}</span
                >
                <span v-else-if="modal.t.source === 'git'" class="tv-badge tv-badge-git"
                  >{{ t('templates.cloud_badge') }}</span
                >
                <span
                  v-if="modal.t.stats && modal.t.stats.show && modal.t.stats.stars"
                  class="tv-stars"
                  >⭐ {{ fmtStars(modal.t.stats.stars) }}</span
                >
                <span v-if="modal.t.stats && modal.t.stats.language" class="tv-lang-txt">{{
                  modal.t.stats.language
                }}</span>
                <a
                  v-if="tplUrl(modal.t)"
                  :href="tplUrl(modal.t)"
                  target="_blank"
                  class="tv-external"
                  >{{ t('templates.view_origin') }}</a
                >
              </div>
            </div>
          </div>
          <span class="tv-close" @click="closeModal">×</span>
        </div>
        <div class="tv-modal-body">
          <div class="tv-preview-wrap">
            <div v-if="localPreview(modal.t.id)" v-html="localPreview(modal.t.id)"></div>
            <div v-else-if="modal.fetchedPreview" v-html="modal.fetchedPreview"></div>
            <div v-else class="tv-preview-fallback">
              <span style="font-size: 48px; opacity: 0.3">{{ modal.t.icon || '📦' }}</span>
            </div>
          </div>
          <div class="tv-section-title">📝 {{ t('templates.section_desc') }}</div>
          <p class="tv-desc">{{ modal.t.description || '' }}</p>
          <template v-if="modal.highlights.length">
            <div class="tv-section-title">✨ {{ t('templates.section_highlights') }}</div>
            <div v-for="h in modal.highlights" :key="h" class="tv-highlight">
              <span class="tv-hi-dot">◆</span><span>{{ h }}</span>
            </div>
          </template>
          <div class="tv-readme-wrap">
            <div class="tv-section-title">📖 {{ t('templates.section_readme') }}</div>
            <div class="tv-readme">{{ modal.readme || t('templates.readme_loading') }}</div>
          </div>
          <div class="tv-section-title">💡 {{ t('templates.section_pipeline', { id: modal.t.scene_id || 'default' }) }}</div>
          <div class="tv-stages">
            <template v-for="(s, i) in pipelineStages(modal.t)" :key="i">
              <span class="tv-stage"
                ><span class="tv-stage-num">{{ i + 1 }}</span
                ><span class="tv-stage-txt">{{ s }}</span></span
              >
              <span v-if="i < pipelineStages(modal.t).length - 1" class="tv-stage-arrow">→</span>
            </template>
          </div>
          <div v-if="(modal.t.tags || []).length" class="tv-tags">
            <span v-for="tg in modal.t.tags" :key="tg" class="tg tg-d" style="font-size: 11px"
              >#{{ tg }}</span
            >
          </div>
          <div v-if="modal.t.recommend_reason" class="tv-reason">
            💡 {{ modal.t.recommend_reason }}
          </div>
        </div>
        <div class="tv-modal-foot">
          <div class="tv-foot-left">
            <label class="ts t2 tv-repo-label">{{ t('templates.repo_name') }}</label>
            <input v-model="modal.repoName" class="tv-repo-input" />
            <select v-if="bindings.length > 1" v-model="modal.provider" class="tv-provider">
              <option v-for="b in bindings" :key="b" :value="b">
                {{ platformName(b) }}
              </option>
            </select>
          </div>
          <div class="tv-foot-right">
            <button v-if="tplUrl(modal.t)" class="bt bt-o bt-s" @click="openUrl(tplUrl(modal.t))">
              {{ t('templates.view_source') }}
            </button>
            <button class="bt bt-o bt-s" @click="closeModal">{{ t('templates.cancel') }}</button>
            <button v-if="modal.t.source === 'git'" class="bt bt-o bt-s" @click="forkOne">
              {{ t('templates.fork_one_click') }}
            </button>
            <button
              class="bt bt-p bt-s tv-create-btn"
              :disabled="modal.submitting"
              @click="doCreate"
            >
              {{
                modal.submitting
                  ? t('templates.creating')
                  : t('templates.create_from_template_arrow')
              }}
            </button>
          </div>
        </div>
        <div v-if="modal.progress.pct !== null" class="tv-progress">
          <div class="tv-progress-txt">{{ modal.progress.text }}</div>
          <div class="tv-progress-bar">
            <div class="tv-progress-fill" :style="{ width: modal.progress.pct + '%' }"></div>
          </div>
          <div class="tv-progress-pct">{{ modal.progress.pct }}%</div>
        </div>
      </div>
    </div>
    </Teleport>
  </div>
</template>

<script setup>
// Batch 14 (templates): legacy templates.js gallery — rendered inside the ReposView lobby shell
// (templates is a sub-tab of the repos lobby, one component so one vite chunk keeps the contract simple).
import { computed, onMounted, reactive, ref } from 'vue'
import { platformChoices, repoKeyOf } from '../lib/repo-provider.js'

const G = globalThis
const emit = defineEmits(['create'])

const loaded = ref(false)
const error = ref('')
const rawCats = ref([])
const activeCat = ref(1)
const page = ref(0)
const search = ref('')
const searchInput = ref('')
const featuredIdx = ref(null)

function t(key, params) {
  return G.I18N.t(key, params)
}
function tOr(key, fallback) {
  const v = G.I18N.t(key)
  return v === key ? fallback : v
}

const favState = reactive(
  (() => {
    try {
      return JSON.parse(localStorage.getItem('grape_template_favs') || '{}')
    } catch (e) {
      return {}
    }
  })()
)

function catName(name) {
  if (!name) return ''
  const v = t('templates.cat.' + name)
  return v !== 'templates.cat.' + name ? v : name
}

// categories: favorites + all + raw (same as legacy)
const categories = computed(() => {
  const allT = rawCats.value.flatMap((cat) =>
    (cat.templates || []).map((x) => ({ ...x, _cat_name: cat.name, _cat_icon: cat.icon || '📦' }))
  )
  const favT = allT.filter((x) => favState[x.id])
  return [
    {
      id: '_fav',
      name: t('templates.fav_cat'),
      icon: '⭐',
      description: t('templates.fav_cat_desc'),
      scene_id: 'default',
      page_size: 9,
      templates: favT,
    },
    {
      id: 'all',
      name: t('templates.all'),
      icon: '📋',
      description: t('templates.all_desc'),
      scene_id: 'default',
      page_size: 9,
      templates: allT,
    },
    ...rawCats.value,
  ]
})
const activeCategory = computed(() => categories.value[activeCat.value] || categories.value[0])
const featuredCats = computed(() =>
  categories.value.filter((x) => x.id !== 'all' && x.id !== '_fav')
)

const filteredTemplates = computed(() => {
  const allT = activeCategory.value.templates || []
  if (!search.value) return allT
  const kw = search.value.toLowerCase()
  return allT.filter(
    (x) =>
      (x.name || '').toLowerCase().includes(kw) || (x.description || '').toLowerCase().includes(kw)
  )
})
const pages = computed(() => Math.max(1, Math.ceil(filteredTemplates.value.length / 12)))
const pageTemplates = computed(() => {
  let p = page.value
  if (p >= pages.value) p = 0
  return filteredTemplates.value.slice(p * 12, (p + 1) * 12)
})

function switchCat(idx) {
  activeCat.value = idx
  page.value = 0
  const cat = categories.value[idx]
  if (cat.id === '_fav' || cat.id === 'all') featuredIdx.value = null
  else featuredIdx.value = idx - 2
}
function goPage(dir) {
  page.value = (page.value + dir + pages.value) % pages.value
}
function doSearch() {
  search.value = searchInput.value.trim()
  page.value = 0
}
function onFeaturedClick(idx) {
  if (featuredIdx.value === idx) {
    featuredIdx.value = null
    switchCat(0)
  } else {
    featuredIdx.value = idx
    switchCat(idx + 2)
  }
}
function isFav(id) {
  return !!favState[id]
}
function toggleFav(id) {
  if (favState[id]) delete favState[id]
  else favState[id] = 1
  try {
    localStorage.setItem('grape_template_favs', JSON.stringify(favState))
  } catch (e) {
    /* ignore */
  }
}
function platformLabel(x) {
  // 模板来源平台名走平台目录（含 gitee/gitlab/未来平台），不再二分
  const p = x.source_platform || ''
  return (typeof window.platformLabel === 'function' && window.platformLabel(p)) || p || ''
}
function fmtStars(n) {
  return n >= 1000 ? (n / 1000).toFixed(1) + 'k' : n
}

// ── pipeline stages / tag highlights / local previews (data copied verbatim from legacy) ──
const PIPELINE_STAGES = {
  default: ['require', 'issue_ops', 'coding', 'pr_review', 'deploy'],
  'hello-demo': ['require', 'issue_classify', 'coding', 'pr_review', 'deploy'],
  'cloud-native': ['require', 'issue_ops', 'coding', 'pr_review', 'deploy'],
  iac: ['iac_require', 'iac_issue', 'iac_coding', 'iac_review', 'iac_deploy'],
  'agent-dev': ['require', 'issue_ops', 'coding', 'pr_review', 'deploy'],
  'skill-dev': ['require', 'skill_issue', 'skill_create', 'skill_test', 'skill_publish'],
  'api-skill-dev': ['require', 'skill_issue', 'api_skill_create', 'api_skill_test', 'skill_publish'],
  'blog-publish': ['blog_require', 'issue_ops', 'blog_generate', 'blog_publish'],
  harmony: ['require', 'issue_ops', 'coding', 'pr_review', 'deploy'],
}
function pipelineStages(x) {
  const keys = PIPELINE_STAGES[x.scene_id || 'default'] || PIPELINE_STAGES.default
  return keys.map((k) => G.I18N.t('templates.stage.' + k))
}

const TAG_HIGHLIGHTS = {
  python: 'templates.tag.python',
  flask: 'templates.tag.flask',
  fastapi: 'templates.tag.fastapi',
  web: 'templates.tag.web',
  api: 'templates.tag.api',
  cli: 'templates.tag.cli',
  game: 'templates.tag.game',
  pygame: 'templates.tag.pygame',
  starter: 'templates.tag.starter',
  test: 'templates.tag.test',
  html: 'templates.tag.html',
  css: 'templates.tag.css',
  js: 'templates.tag.js',
  frontend: 'templates.tag.frontend',
  blog: 'templates.tag.blog',
  docker: 'templates.tag.docker',
  k8s: 'templates.tag.k8s',
  terraform: 'templates.tag.terraform',
  ai: 'templates.tag.ai',
  agent: 'templates.tag.agent',
  automation: 'templates.tag.automation',
  skill: 'templates.tag.skill',
  harmonyos: 'templates.tag.harmonyos',
  arkts: 'templates.tag.arkts',
  ecommerce: 'templates.tag.ecommerce',
  mcp: 'templates.tag.mcp',
  vue: 'templates.tag.vue',
  golang: 'templates.tag.golang',
  gateway: 'templates.tag.gateway',
  iac: 'templates.tag.iac',
  spider: 'templates.tag.spider',
  crawler: 'templates.tag.crawler',
  markdown: 'templates.tag.markdown',
  tool: 'templates.tag.tool',
  helm: 'templates.tag.helm',
  cncf: 'templates.tag.cncf',
}
function highlightsOf(x) {
  const out = []
  ;(x.tags || []).forEach((tg) => {
    const key = TAG_HIGHLIGHTS[tg.toLowerCase()]
    const desc = key ? G.I18N.t(key) : ''
    if (desc && out.indexOf(desc) < 0) out.push(desc)
  })
  const st = x.stats || {}
  if (st.language && !out.some((h) => h.indexOf(st.language) >= 0)) {
    const lkey = TAG_HIGHLIGHTS[(st.language || '').toLowerCase()]
    if (lkey) out.push(G.I18N.t(lkey))
  }
  return out
}

const LOCAL_PREVIEW = {
  'hello-flask':
    '<div style="background:#f0f2f5;border-radius:8px;padding:12px;overflow:hidden;position:relative;height:140px"><div style="text-align:center;font-size:11px;color:#888;margin-bottom:6px">Mood Tracker 预览</div><div style="display:flex;justify-content:space-around;margin-bottom:6px"><span style="font-size:18px">😀</span><span style="font-size:18px">😊</span><span style="font-size:18px">😐</span><span style="font-size:18px">😟</span><span style="font-size:18px">😢</span></div><div style="background: var(--c);border-radius:6px;padding:6px 8px;margin-bottom:4px"><div style="font-size:9px;color:#4caf50;font-weight:600">开心 · 今天完成了一个有趣的功能</div></div><div style="display:flex;gap:6px;margin-top:4px"><div style="flex:1;background: var(--c);border-radius:4px;padding:4px;text-align:center"><div style="font-size:14px;font-weight:700;color:#3168e0">3</div><div style="font-size:7px;color:#888">记录</div></div><div style="flex:1;background: var(--c);border-radius:4px;padding:4px;text-align:center"><div style="font-size:14px;font-weight:700;color:#3168e0">4.3</div><div style="font-size:7px;color:#888">平均</div></div></div></div>',
  'hello-fastapi':
    '<div style="background:#0d1117;border-radius:8px;padding:12px;overflow:hidden;position:relative;height:140px;font-family:monospace"><div style="font-size:9px;color:#58a6ff;margin-bottom:4px">GET /</div><div style="font-size:9px;color:#7ee787;margin-bottom:6px">{ "message": "Hello, World!" }</div><div style="font-size:9px;color:#58a6ff;margin-bottom:4px">GET /health</div><div style="font-size:9px;color:#7ee787">{ "status": "ok", "service": "hello-fastapi" }</div><div style="position:absolute;bottom:8px;right:10px;font-size:8px;color:#8b949e">⚡ FastAPI</div></div>',
  'hello-todo':
    '<div style="background: var(--c);border-radius:8px;padding:12px;overflow:hidden;position:relative;height:140px;font-family:monospace"><div style="font-size:10px;color:var(--t2);font-weight:600;margin-bottom:6px">TODO CLI</div><div style="font-size:9px;color:#888;margin-bottom:3px">> add 学习 Flask</div><div style="font-size:9px;color:#888;margin-bottom:3px">> add 写单元测试</div><div style="font-size:9px;color:#4caf50;margin-bottom:3px">✓ 学习 Flask</div><div style="font-size:9px;color:#888;margin-bottom:3px">> list</div><div style="font-size:9px;color:#888">1. [✓] 学习 Flask</div><div style="font-size:9px;color:#888">2. [ ] 写单元测试</div></div>',
  'hello-calculator':
    '<div style="background:#1a1a2e;border-radius:8px;padding:12px;overflow:hidden;position:relative;height:140px"><div style="text-align:center;font-size:10px;color:#888;margin-bottom:6px">简单计算器</div><div style="background:#16213e;border-radius:6px;padding:8px;margin-bottom:6px;text-align:right"><span style="font-size:16px;color:#4caf50;font-family:monospace">3 + 4 = 7</span></div><div style="display:grid;grid-template-columns:repeat(4,1fr);gap:3px"><div style="background:#2a2a4e;border-radius:3px;padding:3px;text-align:center;font-size:10px;color:#ccc">7</div><div style="background:#2a2a4e;border-radius:3px;padding:3px;text-align:center;font-size:10px;color:#ccc">8</div><div style="background:#2a2a4e;border-radius:3px;padding:3px;text-align:center;font-size:10px;color:#ccc">9</div><div style="background:#4caf50;border-radius:3px;padding:3px;text-align:center;font-size:10px;color: var(--on-accent)">÷</div><div style="background:#2a2a4e;border-radius:3px;padding:3px;text-align:center;font-size:10px;color:#ccc">4</div><div style="background:#2a2a4e;border-radius:3px;padding:3px;text-align:center;font-size:10px;color:#ccc">5</div><div style="background:#2a2a4e;border-radius:3px;padding:3px;text-align:center;font-size:10px;color:#ccc">6</div><div style="background:#4caf50;border-radius:3px;padding:3px;text-align:center;font-size:10px;color: var(--on-accent)">×</div></div></div>',
  'hello-html':
    '<div style="background: var(--c);border-radius:8px;padding:0;overflow:hidden;position:relative;height:140px"><div style="background:#3168e0;padding:6px 10px"><span style="font-size:9px;color: var(--on-accent)">Hello Static Site</span></div><div style="padding:10px"><div style="font-size:14px;font-weight:700;color:#1a1a2e;margin-bottom:4px">Hello, World!</div><div style="font-size:9px;color:#666;margin-bottom:6px">静态网页模板，HTML + CSS + JavaScript</div><div style="display:inline-block;background:#3168e0;color: var(--on-accent);font-size:9px;padding:3px 10px;border-radius:4px;cursor:pointer">点击交互</div><div style="margin-top:6px;display:flex;gap:4px"><div style="width:24px;height:24px;background:#f0f4f8;border-radius:4px"></div><div style="width:24px;height:24px;background:#e8f5e9;border-radius:4px"></div><div style="width:24px;height:24px;background:#fff3e0;border-radius:4px"></div></div></div></div>',
  'hello-blog':
    '<div style="background: var(--c);border-radius:8px;padding:12px;overflow:hidden;position:relative;height:140px"><div style="font-size:12px;font-weight:700;color:var(--t2);margin-bottom:8px">🗏 Hello Mini Blog</div><div style="padding:4px 0;border-bottom:1px solid #eee"><div style="font-size:10px;color:#2563eb">我的第一篇博客</div></div><div style="padding:4px 0;border-bottom:1px solid #eee"><div style="font-size:10px;color:#2563eb">Hello World</div></div><div style="padding:4px 0"><div style="font-size:9px;color:#888">欢迎来到我的迷你博客！这是学习 Flask 的第一站。</div></div><div style="position:absolute;bottom:8px;right:10px;font-size:8px;color:#ccc">Flask</div></div>',
  'snake-game':
    '<div style="background:#16213e;border-radius:8px;padding:12px;overflow:hidden;position:relative;height:140px"><div style="display:flex;gap:12px;margin-bottom:6px"><span style="font-size:10px;color:#4caf50">得分: 30</span><span style="font-size:10px;color:#888">速度: 2</span></div><div style="position:relative;width:100%;height:90px;background:#0d1117;border-radius:4px;overflow:hidden"><div style="position:absolute;top:20px;left:40px;width:14px;height:14px;background:#4caf50;border-radius:2px"></div><div style="position:absolute;top:20px;left:26px;width:14px;height:14px;background:#388e3c;border-radius:2px"></div><div style="position:absolute;top:20px;left:12px;width:14px;height:14px;background:#388e3c;border-radius:2px"></div><div style="position:absolute;top:40px;left:120px;width:12px;height:12px;background:#f44336;border-radius:50%"></div></div><div style="position:absolute;bottom:8px;right:10px;font-size:8px;color:#4caf50">↑↓←→ 控制</div></div>',
  'alien-invasion':
    '<div style="background:#000;border-radius:8px;padding:12px;overflow:hidden;position:relative;height:140px;font-family:monospace"><div style="display:flex;justify-content:space-between;margin-bottom:6px"><span style="font-size:10px;color:#4caf50">SCORE: 1200</span><span style="font-size:10px;color:#f44336">LIVES: 3</span></div><div style="position:relative;width:100%;height:90px;background:#111;border-radius:4px;overflow:hidden"><div style="position:absolute;bottom:6px;left:40px;font-size:16px">🚀</div><div style="position:absolute;top:10px;left:30px;font-size:14px">🔥</div><div style="position:absolute;top:10px;left:60px;font-size:14px">🔥</div><div style="position:absolute;top:20px;right:30px;font-size:16px">🔥</div><div style="position:absolute;top:20px;right:60px;font-size:16px">🔥</div><div style="position:absolute;top:20px;right:90px;font-size:16px">🔥</div></div><div style="position:absolute;bottom:8px;left:12px;font-size:8px;color:#666">pygame · 方向键移动 · 空格射击</div></div>',
  'alien-game':
    '<div style="background:#0a0a23;border-radius:8px;padding:12px;overflow:hidden;position:relative;height:140px"><div style="text-align:center;font-size:10px;color:#4caf50;margin-bottom:4px">ALIEN GAME</div><div style="display:flex;justify-content:space-between;margin-bottom:4px"><span style="font-size:9px;color:#888">Score: 560</span><span style="font-size:9px;color:#888">High: 1200</span></div><div style="position:relative;width:100%;height:80px;background:#111827;border-radius:4px;overflow:hidden"><div style="position:absolute;bottom:4px;left:50%;transform:translateX(-50%);font-size:14px">🛳</div><div style="position:absolute;top:8px;left:20%;font-size:12px">👾</div><div style="position:absolute;top:8px;left:50%;font-size:12px">👾</div><div style="position:absolute;top:8px;left:80%;font-size:12px">👾</div><div style="position:absolute;top:24px;left:50%;width:2px;height:12px;background:#4caf50"></div></div><div style="position:absolute;bottom:8px;right:10px;font-size:8px;color:#555">pygame</div></div>',
  'hello-blog-sys':
    '<div style="background: var(--c);border-radius:8px;padding:0;overflow:hidden;position:relative;height:140px"><div style="background:#2c3e50;padding:6px 10px;display:flex;justify-content:space-between"><span style="font-size:9px;color: var(--on-accent);font-weight:600">BLOG-SYS API</span><span style="font-size:8px;color:#95a5a6">v1.0</span></div><div style="padding:8px;font-family:monospace"><div style="font-size:9px;color:#e74c3c;margin-bottom:3px">POST /api/posts</div><div style="font-size:8px;color:#888;margin-bottom:4px">{ "title": "Hello", "content": "..." }</div><div style="font-size:9px;color:#27ae60;margin-bottom:3px">GET /api/posts</div><div style="font-size:8px;color:#888;margin-bottom:4px">[ { "id": 1, ... }, { "id": 2, ... } ]</div><div style="font-size:9px;color:#f39c12;margin-bottom:3px">DELETE /api/posts/1</div><div style="font-size:8px;color:#888">{ "status": "deleted" }</div></div><div style="position:absolute;bottom:6px;right:10px;font-size:8px;color:#ccc">Flask REST</div></div>',
}
function localPreview(id) {
  return LOCAL_PREVIEW[id] || ''
}

function tplUrl(x) {
  let u = x.html_url || ''
  if (!u && x.source_repo) {
    u =
      'https://' +
      ((typeof window.platformHost === 'function' && window.platformHost(x.source_platform)) || 'gitcode.com') +
      '/' + x.source_repo
  } else if (!u && x.source === 'local') {
    u = 'https://gitcode.com/grape-dev/grape-template/tree/main/' + (x.id || '')
  }
  return u
}
function openUrl(u) {
  window.open(u, '_blank')
}

// 可一键创建的平台：不能只看 localStorage.user.bindings（那只有 OAuth 身份行，
// 纯 PAT 平台永不出现 → 多仓库权限的用户在创建页看不到平台选择；2026-11-14 实锤）。
// 口径 = 服务端 /auth/platform-links（关系 ∪ PAT）∪ 已加载仓库前缀。
const linkProviders = ref([])
const repos = ref([])
const bindings = computed(() =>
  platformChoices(
    linkProviders.value,
    repos.value || [],
    (k) => (typeof window.platformLabel === 'function' ? window.platformLabel(k) : k)
  ).map((p) => p.key)
)
const platformName = (k) => (typeof window.platformLabel === 'function' ? window.platformLabel(k) : k)

// ── create modal ──
const modal = reactive({
  open: false,
  t: {},
  repoName: '',
  provider: '',
  readme: '',
  fetchedPreview: '',
  highlights: [],
  submitting: false,
  progress: { pct: null, text: '' },
})

function findTemplateById(id) {
  for (const cat of categories.value) {
    for (const x of cat.templates || []) if (x.id === id) return x
  }
  return null
}

function openCreate(tp) {
  modal.t = tp
  modal.repoName = tp.default_repo_name || tp.id
  modal.provider = bindings.value.length > 0 ? bindings.value[0] : ''
  modal.readme = ''
  modal.fetchedPreview = ''
  modal.highlights = highlightsOf(tp)
  modal.submitting = false
  modal.progress.pct = null
  modal.progress.text = ''
  modal.open = true
  G.API.get('/project-templates/' + encodeURIComponent(tp.id) + '/readme')
    .then((data) => {
      const c = (data.content || '').trim()
      modal.readme = c || '该模板暂无文档'
    })
    .catch(() => {
      modal.readme = '文档加载失败'
    })
  if (!localPreview(tp.id)) {
    G.API.get('/project-templates/' + encodeURIComponent(tp.id) + '/preview')
      .then((data) => {
        const c = (data.content || '').trim()
        if (c) modal.fetchedPreview = c
      })
      .catch(() => {
        /* ignore */
      })
  }
}
function closeModal() {
  modal.open = false
}

let progressTimer = null
const PROGRESS_STEPS = [
  { pct: 10, key: 'templates.progress_checking' },
  { pct: 25, key: 'templates.progress_creating_repo' },
  { pct: 45, key: 'templates.progress_downloading' },
  { pct: 65, key: 'templates.progress_pushing' },
  { pct: 80, key: 'templates.progress_hosting' },
  { pct: 95, key: 'templates.progress_applying_scene' },
]
function setProgress(pct, text) {
  modal.progress.pct = pct
  modal.progress.text = text
}
function stopProgressTimer() {
  if (progressTimer) {
    clearInterval(progressTimer)
    progressTimer = null
  }
}

function findAvailableRepoName(baseName, attempt, maxAttempt, onFound, onError) {
  const candidate = attempt === 0 ? baseName : baseName + '-' + (attempt + 1)
  G.API.get('/repos/check-name?name=' + encodeURIComponent(candidate))
    .then((check) => {
      if (!check.exists) onFound(candidate)
      else if (attempt < maxAttempt)
        findAvailableRepoName(baseName, attempt + 1, maxAttempt, onFound, onError)
      else onError(t('templates.repo_auto_rename_exhausted', { base: baseName }))
    })
    .catch(onError)
}

function doCreate() {
  const tp = modal.t
  let repoName = (modal.repoName || '').trim()
  const provider = modal.provider
  if (modal.submitting) return
  modal.submitting = true
  let step = 0
  stopProgressTimer()
  progressTimer = setInterval(() => {
    if (step < PROGRESS_STEPS.length) {
      setProgress(PROGRESS_STEPS[step].pct, t(PROGRESS_STEPS[step].key))
      step++
    }
  }, 5000)
  setProgress(10, t('templates.progress_checking'))
  findAvailableRepoName(
    repoName,
    0,
    20,
    (finalName) => {
      if (finalName !== repoName) {
        repoName = finalName
        modal.repoName = finalName
        G.app.showToast(t('templates.repo_auto_renamed', { name: finalName }))
      }
      setProgress(25, tOr('templates.progress_creating', t('templates.progress_creating_repo')))
      G.API.post(
        '/project-templates/create',
        { template_id: tp.id, repo_name: repoName, provider },
        300000
      )
        .then((r) => {
          stopProgressTimer()
          setProgress(100, t('templates.progress_done'))
          setTimeout(() => {
            modal.open = false
          }, 800)
          if (r.host_warning) {
            G.app.showToast(r.host_warning, 10000)
          } else {
            G.app.showToast(
              t('templates.create_success', {
                msg: r.message || t('templates.create_success_default'),
              })
            )
          }
          const parts = (r.full_name || '').split('/')
          const rk = repoKeyOf(r)
          if (G.app._forceSyncRepos) G.app._forceSyncRepos()
          setTimeout(() => {
            G.window.selectRepo(r.full_name, parts[0], parts[1], rk)
          }, 900)
          emit('create')
        })
        .catch((e) => {
          stopProgressTimer()
          modal.submitting = false
          setProgress(0, t('templates.progress_failed', { msg: e.message }))
          // Issue #3563（Spec v2 B3）：401 reauth——全局重授权流程已由 api.js
          // 触发（provider 分流跳转/凭据页直达），不再透传「创建失败」文案
          if (e && e.status === 401 && e.detail && e.detail.action === 'reauth_required') return
          G.app.showToast(t('templates.create_failed', { msg: e.message }))
        })
    },
    (err) => {
      stopProgressTimer()
      modal.submitting = false
      modal.progress.pct = null
      // Issue #3563（Spec v2 B3）：401 reauth（check-name 阶段）——同上走全局流程；
      // 其它错误保持「检查失败」提示（err 可为字符串——auto rename 耗尽场景）
      if (err && typeof err === 'object' && err.status === 401 && err.detail
        && err.detail.action === 'reauth_required') return
      G.app.showToast(err || t('templates.check_failed', { msg: '' }))
    }
  )
}

// one-click fork (git-source templates only)
const forkModal = reactive({ open: false, t: {}, progress: { pct: null, text: '' } })
let forkTimer = null
function forkOne() {
  const tp = modal.t
  if (tp.source !== 'git') {
    G.app.showToast(tOr('templates.fork_not_git_template', '仅云端模板支持 Fork'))
    return
  }
  modal.open = false
  forkModal.open = true
  forkModal.t = tp
  forkModal.progress.pct = null
  const steps = [
    { pct: 15, text: tOr('templates.progress_forking', '正在 Fork 仓库...') },
    { pct: 50, text: tOr('templates.progress_hosting', '正在配置仓库托管...') },
    { pct: 80, text: tOr('templates.progress_applying_scene', '正在应用开发场景...') },
  ]
  let step = 0
  if (forkTimer) clearInterval(forkTimer)
  forkTimer = setInterval(() => {
    if (step < steps.length) {
      forkModal.progress.pct = steps[step].pct
      forkModal.progress.text = steps[step].text
      step++
    }
  }, 6000)
  forkModal.progress.pct = 15
  forkModal.progress.text = steps[0].text
  G.API.post('/project-templates/fork', { template_id: tp.id }, 300000)
    .then((r) => {
      if (forkTimer) clearInterval(forkTimer)
      forkModal.progress.pct = 100
      forkModal.progress.text = tOr('templates.progress_done', '✅ 完成！')
      setTimeout(() => {
        forkModal.open = false
      }, 800)
      if (r.host_warning) G.app.showToast(r.host_warning, 10000)
      else {
        let msg = t('templates.fork_success', {
          msg: r.message || tOr('templates.fork_success_default', 'Fork 成功'),
        })
        if (msg === 'templates.fork_success') msg = 'Fork 成功'
        G.app.showToast(msg)
      }
      const parts = (r.full_name || '').split('/')
      const rk = repoKeyOf(r)
      if (G.app._forceSyncRepos) G.app._forceSyncRepos()
      setTimeout(() => {
        G.window.selectRepo(r.full_name, parts[0], parts[1], rk)
      }, 900)
      emit('create')
    })
    .catch((e) => {
      if (forkTimer) clearInterval(forkTimer)
      const errMsg = e.message || ''
      let toastMsg = t('templates.fork_failed', { msg: errMsg })
      if (toastMsg === 'templates.fork_failed') toastMsg = 'Fork 失败: ' + errMsg
      if (errMsg.indexOf('已存在') >= 0 || errMsg.indexOf('already exists') >= 0) {
        toastMsg = tOr(
          'templates.fork_exists',
          '⚠️ 仓库已存在（可能已 Fork 过该模板），可直接在仓库管理中托管'
        )
      } else if (errMsg.indexOf('权限') >= 0 || errMsg.indexOf('403') >= 0) {
        toastMsg = tOr('templates.fork_no_permission', '⚠️ 无 Fork 权限，请检查 Token 权限设置')
      }
      forkModal.progress.pct = 0
      forkModal.progress.text =
        tOr('templates.progress_failed', '❌ ' + errMsg) === 'templates.progress_failed'
          ? '❌ ' + errMsg
          : t('templates.progress_failed', { msg: errMsg })
      G.app.showToast(toastMsg)
      setTimeout(() => {
        forkModal.open = false
      }, 2000)
    })
}

onMounted(() => {
  // 平台选择的数据源（两条都是轻量接口：platform-links 读关系表，repos/member 命中缓存）
  G.API.get('/auth/platform-links')
    .then((d) => { linkProviders.value = (d && d.providers) || [] })
    .catch(() => {})
  G.API.get('/repos/member')
    .then((d) => { repos.value = (d && d.repos) || [] })
    .catch(() => {})
  G.API.get('/project-templates')
    .then((data) => {
      rawCats.value = data.categories || []
      activeCat.value = 1
      page.value = 0
      search.value = ''
      searchInput.value = ''
      loaded.value = true
    })
    .catch((e) => {
      loaded.value = true
      error.value = e.message
    })
})

defineExpose({ findTemplateById, openCreate })
</script>

<style scoped>
.tv-empty {
  border: none;
  background: none;
  color: var(--t2);
}
.tv-modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(6px);
  display: flex;
  /* 满高排版：弹窗贴满上下边缘、只在两侧居中（对齐迁移前 weekly-11 的口径），
     内容多时不再把弹窗挤成"中间一块"，滚动的只有正文区。 */
  align-items: stretch;
  justify-content: center;
  padding: 0 24px;
  z-index: 1200;
}
/* 弹窗材质：实心白底（浅色 #fff / 暗色 #2c2c2e），与列表卡片同一材质。
   不再吃 §23 的 --glass-strong 玻璃——玻璃在浅色下会把背后的卡片透出来，显脏。
   全局 §23 的规则带 !important，这里用 :root 前缀 + !important 压过它。 */
:root .tv-modal,
:root .tv-fork-modal {
  border-radius: 12px !important;
  background: var(--glass-solid) !important;
  -webkit-backdrop-filter: none !important;
  backdrop-filter: none !important;
  border: 1px solid var(--hairline) !important;
  box-shadow:
    0 24px 60px rgba(0, 0, 0, 0.18),
    0 2px 8px rgba(0, 0, 0, 0.06) !important;
}
.tv-modal {
  width: 100%;
  max-width: 900px;
  margin: 0 auto;
  padding: 0;
  border-radius: 12px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  height: 100vh;
  max-height: 100vh;
}
/* 头部：图标+标题一块、关闭按钮固定在右上角。
   原来 .tv-modal-head 是块级，关闭按钮被挤到第二行（120px 高的"排版错位"）。 */
.tv-modal-head {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 20px 56px 14px 32px;
  flex-shrink: 0;
  border-bottom: 1px solid var(--hairline);
}
.tv-modal-head-inner {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: 1;
  min-width: 0;
}
.tv-modal-ic {
  font-size: 36px;
  flex-shrink: 0;
}
.tv-modal-title {
  flex: 1;
  min-width: 0;
}
.tv-modal-name-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.tv-modal-name {
  font-size: 20px;
  font-weight: 700;
  color: var(--t);
}
.tv-modal-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 5px;
  flex-wrap: wrap;
}
.tv-badge {
  font-size: 10px;
  padding: 3px 9px;
  border-radius: 999px;
}
.tv-badge-local {
  color: var(--g-fill);
  background: color-mix(in srgb, var(--g) 14%, transparent);
}
.tv-badge-git {
  color: var(--a-text);
  background: color-mix(in srgb, var(--a) 12%, transparent);
}
.tv-stars {
  font-size: 11px;
  color: var(--y);
}
.tv-lang-txt {
  font-size: 11px;
  color: var(--t3);
}
.tv-external {
  font-size: 10px;
  color: var(--a-text);
  text-decoration: none;
  margin-left: 8px;
}
.tv-close {
  position: absolute;
  top: 10px;
  right: 12px;
  width: 30px;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  font-size: 22px;
  line-height: 1;
  color: var(--t3);
  cursor: pointer;
  transition:
    background 0.18s,
    color 0.18s;
}
.tv-close:hover {
  background: var(--chip-bg);
  color: var(--t);
}
.tv-modal-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 20px 32px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.tv-preview-wrap {
  flex-shrink: 0;
}
.tv-preview-fallback {
  background: var(--bg);
  border-radius: 10px;
  padding: 40px 20px;
  text-align: center;
  height: 220px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.tv-section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--t);
  margin-bottom: 8px;
}
.tv-desc {
  font-size: 13px;
  color: var(--t2);
  line-height: 1.7;
}
.tv-highlight {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}
.tv-hi-dot {
  color: var(--a);
  font-size: 10px;
  flex-shrink: 0;
}
.tv-highlight span:last-child {
  font-size: 11px;
  color: var(--t2);
  line-height: 1.5;
}
.tv-readme-wrap {
  flex: 1;
  min-height: 120px;
  display: flex;
  flex-direction: column;
}
.tv-readme {
  background: var(--bg);
  border-radius: 10px;
  padding: 14px 16px;
  flex: 1;
  overflow-y: auto;
  font-size: 12px;
  line-height: 1.7;
  color: var(--t2);
  white-space: pre-wrap;
}
.tv-stages {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0;
  background: var(--bg);
  padding: 12px 14px;
  border-radius: 10px;
}
.tv-stage {
  display: inline-flex;
  align-items: center;
}
.tv-stage-num {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--a);
  color: var(--on-accent);
  font-size: 9px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.tv-stage-txt {
  font-size: 11px;
  color: var(--t2);
  margin-left: 3px;
}
.tv-stage-arrow {
  color: var(--t3);
  font-size: 10px;
  margin: 0 3px;
}
.tv-tags {
  display: flex;
  gap: 5px;
  flex-wrap: wrap;
  flex-shrink: 0;
}
.tv-reason {
  flex-shrink: 0;
  margin-top: 6px;
  font-size: 11px;
  color: var(--a-text);
  background: color-mix(in srgb, var(--a) 10%, transparent);
  padding: 6px 10px;
  border-radius: 6px;
}
.tv-modal-foot {
  padding: 14px 32px;
  flex-shrink: 0;
  border-top: 1px solid var(--hairline);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.tv-foot-left {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 12px;
}
.tv-repo-label {
  font-size: 13px;
  white-space: nowrap;
  flex-shrink: 0;
}
/* 输入框/下拉原来没写 color → 继承浏览器默认的纯黑，暗色下发黑看不清 */
.tv-repo-input {
  flex: 1;
  min-width: 0;
  padding: 10px 14px;
  border: 1px solid var(--b);
  border-radius: 10px;
  font-size: 14px;
  outline: none;
  background: var(--bg);
  color: var(--t);
  box-sizing: border-box;
  transition:
    border-color 0.18s,
    box-shadow 0.18s;
}
.tv-repo-input::placeholder {
  color: var(--t3);
}
.tv-repo-input:focus {
  border-color: var(--a);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--a) 14%, transparent);
}
.tv-provider {
  padding: 9px 12px;
  border: 1px solid var(--b);
  border-radius: 10px;
  font-size: 13px;
  background: var(--c);
  color: var(--t);
  outline: none;
  flex-shrink: 0;
}
.tv-provider:focus {
  border-color: var(--a);
}
.tv-foot-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.tv-create-btn {
  padding: 8px 24px;
}
.tv-fork-modal {
  width: 100%;
  max-width: 460px;
  border-radius: 12px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  max-height: 90vh;
  margin: auto;
}
.tv-progress {
  margin-top: 14px;
  padding: 0 32px 14px;
}
.tv-progress-txt {
  font-size: 11px;
  color: var(--t2);
  margin-bottom: 6px;
}
.tv-progress-bar {
  height: 6px;
  background: var(--bg);
  border-radius: 3px;
  overflow: hidden;
}
.tv-progress-fill {
  height: 100%;
  background: var(--a);
  border-radius: 3px;
  transition: width 0.4s ease;
}
.tv-progress-pct {
  text-align: right;
  font-size: 10px;
  color: var(--t3);
  margin-top: 3px;
}
</style>
