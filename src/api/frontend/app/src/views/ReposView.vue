<template>
  <div class="lobby">
    <header class="lobby-head">
      <div class="lobby-brand">
        <img class="lobby-logo" :src="logoSrc" alt="Grape" />
        <span class="lobby-brand-name">Grape</span>
        <span class="lobby-brand-sub">{{ t('repos.brand_sub') }}</span>
      </div>
      <nav class="lobby-tabs">
        <a :class="tabCls('projects')" @click="switchTab('projects')">{{
          t('repos.tab.projects')
        }}</a>
        <a :class="tabCls('templates')" @click="switchTab('templates')">{{
          t('repos.tab.templates')
        }}</a>
        <a :class="tabCls('workspace')" @click="switchTab('workspace')">{{
          t('repos.tab.workspace')
        }}</a>
      </nav>
      <div class="rv-head-right">
        <div class="lang-toggle rv-lang">
          <button
            v-for="lg in ['zh', 'en']"
            :key="lg"
            :class="langCls(lg)"
            :style="langStyle(lg)"
            @click="setAppLang(lg)"
          >
            {{ lg === 'zh' ? '中' : 'EN' }}
          </button>
        </div>
        <div class="rv-dd">
          <button class="rv-dd-btn" @click="toggleOrgMenu">
            <span class="rv-org-ic">🏢</span>
            <span class="lobby-org-name">{{ tOr('organizations.my_orgs', '我的组织') }}</span>
            <svg
              width="10"
              height="10"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="3"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </button>
          <div v-show="orgMenu.open" id="lobbyOrgMenu" class="rv-dd-menu">
            <div class="rv-dd-empty">…</div>
          </div>
        </div>
        <div class="rv-dd">
          <button class="rv-dd-btn" @click="toggleUserMenu">
            <span class="lobby-avatar">{{ avatar }}</span>
            <span class="lobby-user-name">{{ userName }}</span>
            <svg
              width="10"
              height="10"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="3"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </button>
          <div v-show="userMenu.open" id="lobbyUserMenu" class="rv-dd-menu">
            <div class="rv-dd-empty">…</div>
          </div>
        </div>
      </div>
    </header>
    <main class="lobby-main">
      <!-- projects tab -->
      <div v-if="activeTab === 'projects'" class="lobby-content rv-content">
        <div v-if="!projectsLoaded" class="loading">{{ t('repos.loading') }}</div>
        <template v-else-if="projectsError">
          <div class="error">{{ t('repos.load_failed') }}</div>
        </template>
        <template v-else>
          <div class="project-page">
            <div class="project-hero">
              <div class="project-hero-welcome">
                <span class="pw-icon">👋</span>
                <div class="pw-text">
                  <span class="pw-title">{{
                    t('repos.lobby.welcome_back', { name: userName })
                  }}</span>
                  <span class="pw-desc">{{ t('repos.lobby.welcome_tip') }}</span>
                </div>
              </div>
              <div class="project-stats">
                <div class="project-stat">
                  <b>{{ repos.length }}</b
                  ><span>{{ t('repos.lobby.stat_projects') }}</span>
                </div>
                <div class="project-stat">
                  <b>{{ hostedCount }}</b
                  ><span>{{ t('repos.lobby.stat_hosted') }}</span>
                </div>
              </div>
            </div>
            <div class="project-section">
              <div class="project-section-header">
                <div class="project-section-header-left">
                  <h2>{{ t('repos.lobby.my_projects') }}</h2>
                </div>
                <div class="project-section-header-right">
                  <button
                    class="project-new-btn"
                    :title="t('repos.lobby.new_project_btn')"
                    @click="openCreateModal"
                  >
                    <svg
                      width="14"
                      height="14"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="2.5"
                      stroke-linecap="round"
                    >
                      <line x1="12" y1="5" x2="12" y2="19" />
                      <line x1="5" y1="12" x2="19" y2="12" />
                    </svg>
                    <span>{{ t('repos.lobby.new_project_btn') }}</span>
                  </button>
                  <button
                    class="project-refresh-btn"
                    :title="t('repos.lobby.refresh_title')"
                    :disabled="refreshing"
                    @click="refresh"
                  >
                    {{ refreshing ? t('repos.sync.syncing') : t('repos.lobby.refresh_btn') }}
                  </button>
                  <input
                    v-model="searchQuery"
                    class="project-search"
                    type="text"
                    :placeholder="'🔍 ' + t('repos.lobby.search_placeholder')"
                    @input="onSearch"
                  />
                </div>
              </div>
              <div v-if="platforms.length > 1" class="rv-platform-bar">
                <button :class="platCls('')" @click="switchPlatform('')">
                  {{ t('repos.platform.all') }}
                </button>
                <button
                  v-for="p in platforms"
                  :key="p.key"
                  :class="platCls(p.key)"
                  @click="switchPlatform(p.key)"
                >
                  <span v-html="platformIcon(p.key, 14)"></span> {{ p.name }}
                </button>
              </div>
              <div class="project-list">
                <div
                  v-for="r in pageRepos"
                  :key="r.full_name"
                  class="project-card"
                  @click="openRepoUrl(r)"
                >
                  <span class="project-card-ic">{{ initials(r) }}</span>
                  <div class="project-card-info">
                    <span class="project-card-name" :title="r.full_name">{{ r.name }}</span>
                    <span class="project-card-meta"
                      ><span v-html="platformIconHtml(r)"></span> {{ platformName(r) }} ·
                      {{ r.owner }} · {{ r.role }}</span
                    >
                  </div>
                  <span
                    class="project-card-status"
                    :class="r.is_hosted ? 'project-card-status-hosted' : 'project-card-status-none'"
                    >{{
                      r.is_hosted
                        ? t('repos.card.status_hosted')
                        : t('repos.card.status_not_joined')
                    }}</span
                  >
                  <div class="project-card-actions">
                    <button
                      v-if="r.is_hosted"
                      class="project-card-btn project-card-btn-outline"
                      @click.stop="selectRepo(r)"
                    >
                      {{ t('repos.card.enter_project') }}→
                    </button>
                    <button
                      v-else-if="r.can_host"
                      class="project-card-btn project-card-btn-outline"
                      @click.stop="hostAndSelect(r, $event)"
                    >
                      {{ t('repos.card.join_and_host') }}→
                    </button>
                    <button
                      v-else
                      class="project-card-btn project-card-btn-outline"
                      @click.stop="hostNoPermission"
                    >
                      {{ t('repos.card.join_and_host') }}→
                    </button>
                  </div>
                </div>
                <div v-if="filteredRepos.length === 0" class="lobby-empty rv-empty">
                  <div class="lobby-empty-ic">🔭</div>
                  <h3>{{ t('repos.lobby.empty_title') }}</h3>
                  <p>{{ t('repos.lobby.empty_desc') }}</p>
                </div>
                <div v-if="pages > 1" class="project-pager">
                  <button :disabled="repoPage === 0" @click="goPage(-1)">‹</button>
                  <span>{{ repoPage + 1 }} / {{ pages }}</span>
                  <button :disabled="repoPage >= pages - 1" @click="goPage(1)">›</button>
                </div>
              </div>
            </div>
            <div class="project-section">
              <div class="project-section-header">
                <div class="project-section-header-left">
                  <h2>{{ t('repos.lobby.from_template') }}</h2>
                </div>
              </div>
              <div class="template-grid">
                <div
                  v-for="tp in lobbyTemplates"
                  :key="tp.name"
                  class="template-card"
                  @click="switchTab('templates')"
                >
                  <span class="template-card-ic">{{ tp.ic }}</span>
                  <div class="template-card-info">
                    <div class="template-card-name">{{ tp.name }}</div>
                    <div class="template-card-desc">{{ tp.desc }}</div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </template>
      </div>
      <!-- templates tab -->
      <div v-else-if="activeTab === 'templates'" class="lobby-content rv-content">
        <TemplatesView ref="templatesRef" @create="onTemplateCreate" />
      </div>
      <!-- workspace tab -->
      <div v-else class="lobby-content rv-content">
        <div v-if="!wsLoaded" class="loading">{{ t('common.loading') }}</div>
        <div v-else class="ws-empty">
          <div class="ws-empty-ic">🧭</div>
          <h2>{{ t('repos.lobby.ws_empty_title') }}</h2>
          <p>{{ t('repos.lobby.ws_empty_desc') }}</p>
          <div class="ws-empty-actions">
            <button class="ws-empty-btn2" @click="switchTab('projects')">
              {{ t('repos.lobby.ws_go_host') }}
            </button>
          </div>
        </div>
      </div>
    </main>
    <!-- create repo modal -->
    <div v-if="createModal.open" id="createRepoModal" class="create-repo-overlay">
      <div class="create-repo-modal">
        <div class="create-repo-head">
          <div class="create-repo-title">
            <span class="create-repo-ic">＋</span>
            <div>
              <h3>{{ t('repos.create.title') }}</h3>
              <p>{{ t('repos.create.desc') }}</p>
            </div>
          </div>
          <button class="create-repo-x" @click="closeCreateModal">✕</button>
        </div>
        <form class="create-repo-form" @submit.prevent="submitCreate">
          <div v-if="platforms.length > 1" class="create-repo-field">
            <label>{{ t('repos.create.platform_label') }} <span class="cr-req">*</span></label>
            <div class="create-repo-platforms">
              <button
                v-for="p in platforms"
                :key="p.key"
                type="button"
                class="cr-platform"
                :class="{ 'cr-platform-act': createModal.provider === p.key }"
                @click="createModal.provider = p.key"
              >
                <span v-html="platformIcon(p.key, 18)"></span> {{ p.name }}
              </button>
            </div>
          </div>
          <div class="create-repo-field">
            <label for="crName"
              >{{ t('repos.create.name_label') }} <span class="cr-req">*</span></label
            >
            <input
              id="crName"
              ref="crNameRef"
              v-model="createModal.name"
              type="text"
              :placeholder="t('repos.create.name_placeholder')"
              autocomplete="off"
              maxlength="64"
              required
            />
            <p class="cr-hint">{{ t('repos.create.name_hint') }}</p>
          </div>
          <div class="create-repo-field">
            <label for="crDesc">{{ t('repos.create.desc_label') }}</label>
            <textarea
              id="crDesc"
              v-model="createModal.desc"
              rows="3"
              :placeholder="t('repos.create.desc_placeholder')"
              maxlength="255"
            ></textarea>
          </div>
          <div class="create-repo-field">
            <label>{{ t('repos.create.vis_label') }}</label>
            <div class="create-repo-vis">
              <button
                type="button"
                class="cr-vis-btn"
                :class="{ 'cr-vis-act': createModal.vis === 'private' }"
                @click="createModal.vis = 'private'"
              >
                <span class="cr-vis-ic">🔒</span>
                <div>
                  <b>{{ t('repos.create.vis_private') }}</b
                  ><em>{{ t('repos.create.vis_private_desc') }}</em>
                </div>
              </button>
              <button
                type="button"
                class="cr-vis-btn"
                :class="{ 'cr-vis-act': createModal.vis === 'public' }"
                @click="createModal.vis = 'public'"
              >
                <span class="cr-vis-ic">🌐</span>
                <div>
                  <b>{{ t('repos.create.vis_public') }}</b
                  ><em>{{ t('repos.create.vis_public_desc') }}</em>
                </div>
              </button>
            </div>
          </div>
          <div class="create-repo-actions">
            <button type="button" class="cr-btn cr-btn-ghost" @click="closeCreateModal">
              {{ t('repos.create.btn_cancel') }}
            </button>
            <button
              id="crSubmit"
              type="submit"
              class="cr-btn cr-btn-primary"
              :disabled="createModal.submitting"
            >
              <span class="cr-btn-txt">{{ t('repos.create.btn_create') }}</span>
              <span v-if="createModal.submitting" class="cr-btn-loading">{{
                t('repos.create.btn_creating')
              }}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup>
// Batch 14 (repos): legacy shell lobby — renderSelectionShell/renderRepoSelection/renderRepos equivalent.
// platformIcon SVG generator kept identical (brand color inline). selectRepo stays a window global
// (app.js/scene-market/templates cross-call). Templates tab renders the TemplatesView child.
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import TemplatesView from './TemplatesView.vue'

const G = globalThis

// static asset must be runtime-bound — vite tries to resolve literal /static/... src as a module
const logoSrc = '/static/img/grape-logo.jpg?v=1'
const props = defineProps({
  initialRepos: { type: Array, default: null },
  initialTabName: { type: String, default: '' },
})
const emit = defineEmits(['create'])

const activeTab = ref('projects')
const repos = ref([])
const projectsLoaded = ref(false)
const projectsError = ref('')
const searchQuery = ref('')
const repoPage = ref(0)
const platformFilter = ref('')
const refreshing = ref(false)
const wsLoaded = ref(false)
const templatesRef = ref(null)
const crNameRef = ref(null)

const REPO_PAGE_SIZE = 9
const orgMenu = reactive({ open: false })
const userMenu = reactive({ open: false })
const createModal = reactive({
  open: false,
  name: '',
  desc: '',
  vis: 'private',
  provider: '',
  submitting: false,
})

function t(key, params) {
  return G.I18N.t(key, params)
}
function tOr(key, fallback) {
  const v = G.I18N.t(key)
  return v === key ? fallback : v
}

// ── platform icon (identical SVG output as legacy repos.js) ──
const PLATFORM_BRAND_COLOR = { gitcode: '#F54A36', github: '#1F2328' }
function platformIcon(provider, size) {
  size = size || 14
  const s = 'vertical-align:-2px;flex-shrink:0'
  const color = PLATFORM_BRAND_COLOR[provider] || 'var(--a)'
  if (provider === 'github') {
    return (
      '<svg width="' +
      size +
      '" height="' +
      size +
      '" viewBox="0 0 1024 1024" fill="' +
      color +
      '" class="platform-icon" style="' +
      s +
      '"><path d="M512 42.666667A464.64 464.64 0 0 0 42.666667 502.186667 460.373333 460.373333 0 0 0 363.52 938.666667c23.466667 4.266667 32-9.813333 32-22.186667v-78.08c-130.56 27.733333-158.293333-61.44-158.293333-61.44a122.026667 122.026667 0 0 0-52.053334-67.413333c-42.666667-28.16 3.413333-27.733333 3.413334-27.733334a98.56 98.56 0 0 1 71.68 47.36 101.12 101.12 0 0 0 136.533333 37.973334 99.413333 99.413333 0 0 1 29.866667-61.44c-104.106667-11.52-213.333333-50.773333-213.333334-226.986667a177.066667 177.066667 0 0 1 47.36-124.16 161.28 161.28 0 0 1 4.693334-121.173333s39.68-12.373333 128 46.933333a455.68 455.68 0 0 1 234.666666 0c89.6-59.306667 128-46.933333 128-46.933333a161.28 161.28 0 0 1 4.693334 121.173333A177.066667 177.066667 0 0 1 810.666667 477.866667c0 176.64-110.08 215.466667-213.333334 226.986666a106.666667 106.666667 0 0 1 32 85.333334v125.866666c0 14.933333 8.533333 26.88 32 22.186667A460.8 460.8 0 0 0 981.333333 502.186667 464.64 464.64 0 0 0 512 42.666667"/></svg>'
    )
  }
  return (
    '<svg width="' +
    size +
    '" height="' +
    size +
    '" viewBox="0 0 1024 1024" fill="' +
    color +
    '" class="platform-icon" style="' +
    s +
    '"><path d="M472.746667 896.853333c-72.533333-8.106667-151.466667-39.253333-200.106667-78.08-27.306667-21.76-72.533333-71.253333-89.6-97.706666-51.626667-80.213333-72.533333-179.626667-57.173333-269.653334 15.36-86.613333 50.773333-154.453333 113.92-216.32 109.653333-107.52 285.44-141.653333 425.813333-82.346666 46.08 19.626667 90.453333 46.933333 108.8 66.986666 35.84 39.68 22.613333 104.106667-25.173333 128-20.48 9.813333-58.453333 10.24-72.96 0.853334-14.933333-9.386667-28.16-35.84-32-62.293334-1.706667-12.8-3.84-23.04-4.266667-23.04-0.853333 0-11.946667 5.973333-23.893333 13.226667-40.106667 23.466667-51.626667 26.88-113.066667 30.72-32.426667 1.706667-67.84 4.693333-78.933333 6.4-20.053333 2.56-44.373333-0.853333-83.2-13.226667l-20.48-6.4 0.853333 42.24c0.426667 40.96 0 43.093333-17.493333 79.36-22.613333 46.933333-30.72 75.52-34.56 117.333334-8.106667 100.693333 46.506667 175.36 150.613333 205.226666 67.413333 19.2 179.2 15.36 239.36-8.533333 58.026667-23.04 108.8-76.8 108.8-114.773333 0-15.786667-20.053333-35.84-41.813333-40.96-8.96-2.56-43.946667-5.546667-77.226667-6.826667-69.973333-2.986667-119.04-9.386667-132.266667-18.346667-13.653333-8.533333-20.48-33.28-14.506666-51.2 5.973333-19.2 15.786667-27.306667 43.946666-37.973333 19.2-7.253333 33.28-8.533333 88.746667-8.533333 118.613333 0 174.08 14.933333 217.6 58.453333 49.92 50.346667 55.466667 127.146667 13.226667 194.986667-62.293333 99.84-135.68 156.586667-235.52 182.613333-33.706667 8.96-122.453333 14.933333-158.72 10.666667z"/></svg>'
  )
}
function platformIconHtml(r) {
  const rk = r.repo_key || ''
  return platformIcon(rk.indexOf('github:') === 0 ? 'github' : 'gitcode', 14)
}
function platformName(r) {
  const rk = r.repo_key || ''
  return rk.indexOf('github:') === 0 ? 'GitHub' : 'GitCode'
}
function repoUrl(r) {
  const rk = r.repo_key || ''
  const fn = r.full_name || (r.owner ? r.owner + '/' + r.name : '')
  if (rk.indexOf('github:') === 0) return 'https://github.com/' + fn
  return 'https://gitcode.com/' + fn
}

// ── user / platforms ──
const user = computed(() => {
  try {
    const lu = localStorage.getItem('user') ? JSON.parse(localStorage.getItem('user')) : null
    return (lu && (lu.nickname || lu.username)) || 'User'
  } catch (e) {
    return 'User'
  }
})
const userName = computed(() => user.value)
const avatar = computed(() => (user.value || 'U')[0].toUpperCase())
const platforms = computed(() => {
  const bindings = (() => {
    try {
      const lu = localStorage.getItem('user') ? JSON.parse(localStorage.getItem('user')) : null
      return (lu && lu.bindings) || []
    } catch (e) {
      return []
    }
  })()
  const list = []
  if (bindings.indexOf('gitcode') >= 0) list.push({ key: 'gitcode', name: 'GitCode' })
  if (bindings.indexOf('github') >= 0) list.push({ key: 'github', name: 'GitHub' })
  return list
})
const hostedCount = computed(() => repos.value.filter((r) => r.is_hosted).length)

// ── list / search / pager (identical sort+filter logic) ──
const allRepos = computed(() => {
  let list = repos.value.slice()
  if (platformFilter.value) {
    list = list.filter((r) => {
      const rk = r.repo_key || ''
      if (platformFilter.value === 'github') return rk.indexOf('github:') === 0
      return rk.indexOf('github:') !== 0
    })
  }
  return list.sort((a, b) => {
    const pa = a.is_hosted ? 2 : a.is_joined ? 1 : 0
    const pb = b.is_hosted ? 2 : b.is_joined ? 1 : 0
    if (pa !== pb) return pb - pa
    const ca = a.created_at || ''
    const cb = b.created_at || ''
    if (ca && cb && ca !== cb) return ca < cb ? 1 : -1
    if (ca && !cb) return -1
    if (!ca && cb) return 1
    return (a.full_name || '').localeCompare(b.full_name || '')
  })
})
const filteredRepos = computed(() => {
  const q = searchQuery.value.toLowerCase().trim()
  if (!q) return allRepos.value
  return allRepos.value.filter(
    (r) =>
      (r.full_name || '').toLowerCase().includes(q) || (r.owner || '').toLowerCase().includes(q)
  )
})
const pages = computed(() => Math.max(1, Math.ceil(filteredRepos.value.length / REPO_PAGE_SIZE)))
const pageRepos = computed(() => {
  let p = repoPage.value
  if (p >= pages.value) p = 0
  return filteredRepos.value.slice(p * REPO_PAGE_SIZE, (p + 1) * REPO_PAGE_SIZE)
})

const lobbyTemplates = computed(() => [
  { ic: '🚀', name: t('repos.template.hello_world'), desc: t('repos.template.hello_world_desc') },
  { ic: '🤖', name: t('repos.template.agent'), desc: t('repos.template.agent_desc') },
  { ic: '☁️', name: t('repos.template.cloud'), desc: t('repos.template.cloud_desc') },
  { ic: '🤖', name: t('repos.template.ai_office'), desc: t('repos.template.ai_office_desc') },
  { ic: '🔧', name: t('repos.template.iac'), desc: t('repos.template.iac_desc') },
])

// ── tab / lang / dropdowns ──
function tabCls(tb) {
  return 'rs-tab' + (activeTab.value === tb ? ' rs-tab-act' : '')
}
function switchTab(tab) {
  if (tab === 'workspace') {
    activeTab.value = 'workspace'
    wsLoaded.value = false
    G.API.get('/repos/member')
      .then((d) => {
        const rs = d.repos || []
        const hosted = rs.filter((r) => r.is_hosted)
        if (hosted.length > 0) {
          let saved = null
          try {
            saved = G.app.currentRepo()
          } catch (e) {
            saved = null
          }
          const matched = saved && hosted.find((r) => r.full_name === saved.fullName)
          const target = matched || hosted[0]
          selectRepo(target.full_name, target.owner, target.name, target.repo_key)
        } else {
          wsLoaded.value = true
        }
      })
      .catch(() => {
        switchTab('projects')
      })
    return
  }
  if (tab === 'templates') {
    activeTab.value = 'templates'
    localStorage.setItem('sidebarTab', 'templates')
    return
  }
  activeTab.value = 'projects'
  localStorage.setItem('sidebarTab', 'projects')
  if (!projectsLoaded.value && !props.initialRepos) loadProjects()
}
function setAppLang(lg) {
  G._setAppLang(lg)
}
function langCls(lg) {
  return 'lang-btn' + (G.I18N.getLang() === lg ? ' active' : '')
}
function langStyle(lg) {
  return G.I18N.getLang() === lg
    ? 'background:var(--a);color: var(--on-accent)'
    : 'background:none;color:var(--t3)'
}
function toggleOrgMenu() {
  orgMenu.open = !orgMenu.open
  if (orgMenu.open) {
    userMenu.open = false
    try {
      G.app._toggleLobbyOrgDropdown()
    } catch (e) {
      /* ignore */
    }
  }
}
function toggleUserMenu() {
  userMenu.open = !userMenu.open
  if (userMenu.open) {
    orgMenu.open = false
    try {
      G.app._toggleLobbyUserMenu()
    } catch (e) {
      /* ignore */
    }
  }
}

function platCls(key) {
  return platformFilter.value === key ? 'bt bt-tg' : 'bt bt-o'
}
function switchPlatform(key) {
  platformFilter.value = key
  repoPage.value = 0
}
function initials(r) {
  return (r.name || '?').substring(0, 2).toUpperCase()
}
function onSearch() {
  repoPage.value = 0
}
function goPage(dir) {
  repoPage.value = (repoPage.value + dir + pages.value) % pages.value
}
function openRepoUrl(r) {
  window.open(repoUrl(r), '_blank')
}

// ── data load ──
function loadProjects() {
  projectsLoaded.value = false
  projectsError.value = ''
  G.API.get('/repos/member')
    .then((d) => {
      repos.value = d.repos || []
      projectsLoaded.value = true
    })
    .catch((e) => {
      projectsLoaded.value = true
      projectsError.value = e.message
    })
}
function refresh() {
  refreshing.value = true
  G.API.post('/repos/sync')
    .then((d) => {
      repoPage.value = 0
      if (G.app._clearCachedReposData) G.app._clearCachedReposData()
      repos.value = (d && d.repos) || []
      refreshing.value = false
      G.app.showToast(t('repos.sync.refresh_success'))
    })
    .catch((e) => {
      refreshing.value = false
      G.app.showToast(t('repos.sync.refresh_failed', { msg: e.message || e }))
    })
}

// ── repo actions ──
function selectRepo(r) {
  // 这里必须走 window 全局（app.js 定义）；裸调用会命中自己 → 无限递归，点了没反应
  G.selectRepo(r.full_name, r.owner, r.name, r.repo_key)
}
function selectRepoGlobal(fullName, owner, name, repoKey) {
  sessionStorage.setItem(
    'currentRepo',
    JSON.stringify({ fullName, owner, name, repoKey: repoKey || '' })
  )
  const sidebar = document.getElementById('sidebar')
  const mainView = document.getElementById('mainView')
  const tb = document.getElementById('tb')
  const title = document.getElementById('pageTitle')
  if (mainView) {
    mainView.style.position = ''
    mainView.style.inset = ''
    mainView.style.zIndex = ''
    mainView.style.display = 'flex'
  }
  if (sidebar) sidebar.style.display = 'flex'
  if (tb) tb.style.display = 'flex'
  if (title) title.textContent = t('app.nav.dashboard')
  G.app.initSidebarRepos()
  G.app.nav('dash')
}

function showBotCollaboratorPrompt(resp, fullName, owner, name, repoKey) {
  const botUser = resp.bot_username || ''
  const status = resp.bot_status || ''
  const inviteUrl = resp.bot_invite_url || ''
  let title
  let message
  if (status === 'not_configured') {
    title = tOr('repos.bot.title', 'Bot 未配置')
    message = tOr(
      'repos.bot.not_configured',
      'GitHub Bot 未配置，请联系管理员在配置中心设置 GitHub Bot Token 和 Username。仓库已托管，但 Agent 自动操作可能受限。'
    )
  } else if (status === 'invite_pending') {
    title = tOr('repos.bot.title', '需要添加 Bot 为维护者')
    message = tOr(
      'repos.bot.invite_pending',
      'Bot {{bot}} 已被邀请加入仓库，但邀请尚未被接受。\n请前往 GitHub 仓库 Settings → Collaborators 接受邀请，或手动将 {{bot}} 添加为维护者。'
    ).replace(/\{\{bot\}\}/g, botUser)
  } else if (status === 'add_failed') {
    title = tOr('repos.bot.title', '需要手动添加 Bot')
    message = tOr(
      'repos.bot.add_failed',
      '自动添加 Bot {{bot}} 为仓库维护者失败。\n请前往 GitHub 仓库 Settings → Collaborators 手动添加 {{bot}} 为维护者（Maintainer）。'
    ).replace(/\{\{bot\}\}/g, botUser)
  } else if (status === 'insufficient_permission') {
    title = tOr('repos.bot.title', 'Bot 权限不足')
    message = tOr(
      'repos.bot.insufficient',
      'Bot {{bot}} 已加入仓库但权限不足。\n请在 GitHub 仓库 Settings → Collaborators 中将 {{bot}} 的权限设为 Maintainer 或 Admin。'
    ).replace(/\{\{bot\}\}/g, botUser)
  } else {
    title = tOr('repos.bot.title', '需要添加 Bot 为维护者')
    message = tOr(
      'repos.bot.default',
      '请前往 GitHub 仓库将 Bot {{bot}} 添加为维护者，确保 Agent 可以正常操作仓库。'
    ).replace(/\{\{bot\}\}/g, botUser)
  }
  if (G.window._grapeConfirm) {
    G.window._grapeConfirm({
      title,
      message,
      confirmText: tOr('repos.bot.go_github', '前往 GitHub 设置'),
      cancelText: tOr('repos.bot.enter_repo', '先进入仓库'),
      onConfirm() {
        if (inviteUrl) window.open(inviteUrl, '_blank')
        G.app.showToast(tOr('repos.bot.come_back', '在 GitHub 设置完成后，回来重新托管或刷新页面'))
      },
      onCancel() {
        G.app.loadProfile().then(() => selectRepoGlobal(fullName, owner, name, repoKey))
      },
    })
  } else {
    G.app.showToast(title + ': ' + message)
    if (inviteUrl) window.open(inviteUrl, '_blank')
    G.app.loadProfile().then(() => selectRepoGlobal(fullName, owner, name, repoKey))
  }
}

function hostAndSelect(r, event) {
  const fullName = r.full_name
  const owner = r.owner
  const name = r.name
  const repoKey = r.repo_key || ''
  const provider =
    String(repoKey || '').indexOf('github:') === 0
      ? 'github'
      : String(repoKey || '').indexOf('gitcode:') === 0
        ? 'gitcode'
        : ''
  const url = '/repos/host' + (provider ? '?provider=' + provider : '')
  const doHost = function () {
    G.API.post(url, { full_name: fullName })
      .then((rr) => {
        if (rr.bot_collaborator_needed) {
          showBotCollaboratorPrompt(rr, fullName, owner, name, repoKey)
        } else {
          G.app.showToast(t('repos.sync.host_success'))
          G.app.loadProfile().then(() => selectRepoGlobal(fullName, owner, name, repoKey))
        }
      })
      .catch((e) => {
        // Issue #455：后端 error_code 明确映射——403 属权限/归属，502 类属上游可重试
        const code = (e && (e.error_code || (e.detail && e.detail.error_code))) || ''
        let toastMsg
        if (code === 'PERMISSION_DENIED' || code === 'TOKEN_PERMISSION_INSUFFICIENT') {
          toastMsg = t('repos.host.permission_denied')
        } else if (code === 'REPO_NOT_FOUND') {
          toastMsg = t('repos.host.repo_not_found', { repo: fullName })
        } else if (String(code).indexOf('UPSTREAM_') === 0) {
          toastMsg = t('repos.host.upstream_error', { msg: (e && e.message) || '' })
        } else {
          toastMsg = t('repos.sync.host_failed', { msg: (e && e.message) || '' })
        }
        G.app.showToast(toastMsg)
      })
  }
  if (G.window._grapeConfirm) {
    G.window._grapeConfirm(
      {
        title: t('repos.host.confirm_title'),
        message: t('repos.host.confirm'),
        confirmText: t('repos.host.confirm_btn'),
        onConfirm: doHost,
      },
      event
    )
  } else {
    G.app.showConfirm(t('repos.host.confirm'), doHost, { confirmText: t('repos.host.confirm_btn') })
  }
}

function hostNoPermission() {
  G.app.showToast(t('repos.card.no_permission_host'))
}

function unhostRepo(fullName, event) {
  const doUnhost = function () {
    G.API.post('/repos/remove', { full_name: fullName })
      .then(() => {
        G.app.showToast(t('repos.unhost.done'))
        try {
          localStorage.removeItem('repo_member_cache')
        } catch (e) {
          /* ignore */
        }
        G.app.loadProfile().then(() => {
          const cur = G.app.currentRepo()
          const wasCurrent = cur && cur.fullName === fullName
          if (wasCurrent) sessionStorage.removeItem('currentRepo')
          G.app.initSidebarRepos()
          G.API.get('/repos/managed').then((md) => {
            const remaining = md.repos || []
            const sidebar = document.getElementById('sidebar')
            const tb = document.getElementById('tb')
            const mv = document.getElementById('mainView')
            const title = document.getElementById('pageTitle')
            if (wasCurrent || remaining.length === 0) {
              G.API.get('/repos/member').then((d) => {
                if (sidebar) sidebar.style.display = 'none'
                if (tb) tb.style.display = 'none'
                if (mv) {
                  mv.style.display = 'flex'
                  mv.style.position = 'fixed'
                  mv.style.inset = '0'
                  mv.style.zIndex = '100'
                }
                if (title) title.textContent = t('repos.unhost.select_repo')
                G.window.renderRepoSelection((d && d.repos) || [])
              })
            } else {
              if (title) title.textContent = t('repos.unhost.select_repo')
              G.window.renderRepos()
            }
          })
        })
      })
      .catch((e) => G.app.showToast(t('repos.unhost.failed', { msg: e.message })))
  }
  if (G.window._grapeConfirm) {
    G.window._grapeConfirm(
      {
        title: tOr('repos.unhost.confirm_title', '解除托管'),
        message: t('repos.unhost.confirm', { name: fullName }),
        confirmText: tOr('repos.unhost.confirm_btn', '解除托管'),
        danger: true,
        onConfirm: doUnhost,
      },
      event
    )
  } else {
    G.app.showConfirm(t('repos.unhost.confirm', { name: fullName }), doUnhost, {
      danger: true,
      confirmText: tOr('repos.unhost.confirm_btn', '解除托管'),
    })
  }
}

// ── create repo modal ──
function openCreateModal() {
  createModal.name = ''
  createModal.desc = ''
  createModal.vis = 'private'
  createModal.submitting = false
  createModal.provider = platforms.value.length > 0 ? platforms.value[0].key : 'gitcode'
  createModal.open = true
  document.body.style.overflow = 'hidden'
  nextTick(() => {
    if (crNameRef.value) setTimeout(() => crNameRef.value.focus(), 60)
  })
}
function closeCreateModal() {
  createModal.open = false
  document.body.style.overflow = ''
}
function submitCreate() {
  const name = (createModal.name || '').trim()
  if (!name) return G.app.showToast(t('repos.create.err_no_name'))
  if (createModal.submitting) return
  createModal.submitting = true
  G.API.get('/repos/check-name?name=' + encodeURIComponent(name))
    .then((check) => {
      if (check.exists) {
        createModal.submitting = false
        G.app.showToast(t('repos.create.err_exists', { name: check.full_name }))
        return
      }
      G.API.post(
        '/repos/create',
        {
          name,
          description: createModal.desc.trim(),
          visibility: createModal.vis,
          provider: createModal.provider,
        },
        120000
      )
        .then((r) => {
          if (r.hosted === false && r.message) {
            G.app.showToast(r.message)
          } else {
            G.app.showToast(t('repos.create.success', { name: r.full_name || name }))
          }
          closeCreateModal()
          // R13.1: partial refresh — force sync then re-render list (no full reload)
          G.API.post('/repos/sync')
            .then((d) => {
              const rs = (d && d.repos) || []
              try {
                localStorage.setItem(
                  'repo_member_cache',
                  JSON.stringify({ repos: rs, ts: Date.now() })
                )
              } catch (e) {
                /* ignore */
              }
              if (G.app._clearCachedReposData) G.app._clearCachedReposData()
              repos.value = rs
            })
            .catch(() => {
              G.API.get('/repos/member')
                .then((d) => {
                  repos.value = (d && d.repos) || []
                })
                .catch(() => {
                  loadProjects()
                })
            })
        })
        .catch((err) => {
          G.app.showToast(t('repos.create.err_create_failed', { msg: err.message || err }))
          createModal.submitting = false
        })
    })
    .catch((err) => {
      createModal.submitting = false
      G.app.showToast(t('repos.create.err_check_failed', { msg: err.message || err }))
    })
}

// template tab child → create modal is owned by templates view itself; bubble up for list refresh
function onTemplateCreate() {
  emit('create')
}

// external contract: switch sidebar tab without full re-render (legacy switchSidebarTab('templates'))
function showTemplatesTab() {
  activeTab.value = 'templates'
  localStorage.setItem('sidebarTab', 'templates')
}

onMounted(() => {
  // renderRepoSelection contract (legacy): the projects list ALWAYS renders the projects tab,
  // regardless of the persisted sidebarTab — old code called renderSelectionShell('projects', ...) directly.
  activeTab.value =
    props.initialTabName ||
    (props.initialRepos ? 'projects' : null) ||
    localStorage.getItem('sidebarTab') ||
    'projects'
  if (activeTab.value === 'workspace') activeTab.value = 'projects'
  if (activeTab.value === 'projects') localStorage.setItem('sidebarTab', 'projects')
  if (props.initialRepos) {
    repos.value = props.initialRepos
    projectsLoaded.value = true
  } else if (activeTab.value === 'projects') {
    loadProjects()
  } else if (activeTab.value === 'templates') {
    // templates view loads itself
  }
  try {
    G.app._updateLobbyOrgName()
  } catch (e) {
    /* ignore */
  }
})

defineExpose({
  selectRepoGlobal,
  hostAndSelect,
  unhostRepo,
  openCreateModal,
  showTemplatesTab,
  refresh,
  platformIcon,
})
</script>

<style scoped>
.rv-head-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.rv-lang {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 3px;
  border-radius: 8px;
  border: 1px solid var(--b);
  background: var(--c);
}
.rv-lang button {
  padding: 3px 10px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 500;
  border: none;
  cursor: pointer;
  white-space: nowrap;
}
.rv-dd {
  position: relative;
}
.rv-dd-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px;
  border-radius: 8px;
  border: 1px solid var(--b);
  background: var(--c);
  color: var(--t);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
}
.rv-dd-menu {
  position: absolute;
  top: 100%;
  right: 0;
  margin-top: 4px;
  min-width: 260px;
  max-height: 480px;
  overflow-y: auto;
  background: var(--c);
  border: 1px solid var(--b);
  border-radius: 10px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
  z-index: 9999;
  padding: 4px;
}
.rv-dd-empty {
  color: var(--t3);
  font-size: 12px;
  padding: 6px 10px;
}
.rv-org-ic {
  font-size: 14px;
}
.rv-platform-bar {
  display: flex;
  gap: 6px;
  margin-bottom: 14px;
}
.rv-platform-bar button {
  font-size: 12px;
}
.rv-empty {
  grid-column: 1/-1;
  margin: 0;
  border: none;
  background: none;
}
.rv-content :deep(.template-card-item-name) {
  display: flex;
  align-items: center;
  gap: 4px;
}
</style>
