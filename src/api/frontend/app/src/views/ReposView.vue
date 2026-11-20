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
          <!-- 凭据坏了/过期时：后端按口径乙隐藏该平台仓库并回 hint（含"已隐藏其仓库"）；
               * 页面必须把原因和修法显示出来，否则用户看到的就是凭空少一个平台的仓库 -->
          <div v-if="projectsHint" class="rv-cred-hint">
            <span class="rv-cred-hint-ic">⚠️</span>
            <span class="rv-cred-hint-tx">{{ projectsHint }}</span>
            <button v-if="projectsBroken.length" class="rv-cred-hint-btn" @click="goFixCredential">
              {{ t('repos.cred.fix_btn') }}
            </button>
          </div>
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
                  <b>{{ hostedCount }}</b
                  ><span>{{ t('repos.lobby.stat_hosted') }}</span>
                </div>
                <div class="project-stat">
                  <b>{{ repos.length }}</b
                  ><span>{{ t('repos.lobby.stat_visible') }}</span>
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
                    :title="onlyHosted ? t('repos.lobby.scope_hosted_title') : t('repos.lobby.scope_all_title')"
                    @click="onlyHosted = !onlyHosted"
                  >
                    {{ onlyHosted ? t('repos.lobby.scope_hosted') : t('repos.lobby.scope_all') }}
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
                  <div class="lobby-empty-ic">{{ onlyHosted ? '📦' : '🔭' }}</div>
                  <h3>{{ onlyHosted ? t('repos.lobby.empty_hosted_title') : t('repos.lobby.empty_title') }}</h3>
                  <p>{{ onlyHosted ? t('repos.lobby.empty_hosted_desc') : t('repos.lobby.empty_desc') }}</p>
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
                @click="selectCreateProvider(p.key)"
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
            <label for="crSpace">{{ t('repos.create.space_label') }}</label>
            <select
              id="crSpace"
              v-model="createModal.space"
              class="cr-space-select"
              :disabled="createModal.spaceLoading"
            >
              <option value="">{{ t('repos.create.space_personal') }}</option>
              <option v-for="sp in createModal.spaces" :key="sp.path" :value="sp.path">
                {{ sp.path }}
              </option>
            </select>
            <p v-if="createModal.spaces.length" class="cr-hint">
              {{ t('repos.create.space_hint') }}
            </p>
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
import { providerFromRepoKey } from '../lib/repo-provider.js'
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
const projectsHint = ref('')
const projectsBroken = ref([])
const searchQuery = ref('')
const repoPage = ref(0)
// 项目大厅默认只列「已托管」：VCS 能看到的仓库动辄上百（组织仓库全在里面），
// 混在一起看不出哪些真在本平台托管。切到「全部仓库」才会显示可托管的候选。
const onlyHosted = ref(true)
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
  space: '',              // 所属空间：'' = 个人空间；GitLab 组/子组填 full_path
  spaces: [],             // 该平台可建仓的空间（/repos/create-targets）
  spaceLoading: false,
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
function platformIcon(provider, size) {
  // 单一真源：shell/js/platform-icons.js（不再本地维护只有 github/gitcode 的表）
  return typeof window.platformIcon === 'function' ? window.platformIcon(provider, size) : ''
}
function repoProvider(r) {
  const rk = (r && r.repo_key) || ''
  const i = rk.indexOf(':')
  return i > 0 ? rk.slice(0, i) : 'gitcode'
}
function platformIconHtml(r) {
  return platformIcon(repoProvider(r), 14)
}
function platformName(r) {
  const prov = repoProvider(r)
  return typeof window.platformLabel === 'function' ? window.platformLabel(prov) : prov
}
function repoUrl(r) {
  const prov = repoProvider(r)
  const fn = r.full_name || (r.owner ? r.owner + '/' + r.name : '')
  // 域名走平台目录（platform-icons.js），未知平台兜底 gitcode
  const dom = (typeof window.platformHost === 'function' ? window.platformHost(prov) : '') || 'gitcode.com'
  return 'https://' + dom + '/' + fn
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
// 平台名册（与 arch-weekly-11 d2787875 同源：旧壳 _getAvailablePlatforms 的 cat 兜底）
const PLATFORM_ORDER = ['gitcode', 'github', 'gitee', 'gitlab']
const platforms = computed(() => {
  const bindings = (() => {
    try {
      const lu = localStorage.getItem('user') ? JSON.parse(localStorage.getItem('user')) : null
      return (lu && lu.bindings) || []
    } catch (e) {
      return []
    }
  })()
  // 平台栏数据源 = 当前账号实际有仓库的平台（repo_key 前缀）∪ 身份绑定（无仓库时兜底）。
  // 不能用 bindings 当权威：PAT 是「仓库执行凭据」、不产生身份行（A2 对象模型），
  // 纯 PAT 平台永远不在 bindings 里 → 平台栏不渲染（platforms.length <= 1）→
  // 该平台的仓库只能靠翻页找（2026-11-13 实测：gitee 7 条落第 7/8 页、无页签）。
  const keys = {}
  repos.value.forEach((r) => {
    const rk = (r && r.repo_key) || ''
    const k = rk.indexOf(':') > 0 ? rk.split(':')[0] : 'gitcode' // 裸键归 gitcode
    if (k) keys[k] = true
  })
  if (!Object.keys(keys).length) {
    bindings.forEach((b) => {
      keys[b] = true // 尚无仓库（新账号/加载中）→ 回退身份绑定
    })
  }
  const rank = (k) => {
    const i = PLATFORM_ORDER.indexOf(k)
    return i < 0 ? PLATFORM_ORDER.length : i
  }
  return Object.keys(keys)
    .sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
    .map((k) => ({ key: k, name: (typeof window.platformLabel === 'function' ? window.platformLabel(k) : k) }))
})
const hostedCount = computed(() => repos.value.filter((r) => r.is_hosted).length)

// ── list / search / pager (identical sort+filter logic) ──
const allRepos = computed(() => {
  let list = repos.value.slice()
  if (onlyHosted.value) list = list.filter((r) => r.is_hosted)
  if (platformFilter.value) {
    list = list.filter((r) => {
      const rk = r.repo_key || ''
      if (!rk) return platformFilter.value === 'gitcode' // 裸键归 gitcode（与平台栏同口径，评审 I-2）
      return rk.indexOf(platformFilter.value + ':') === 0 // D102-F: 前缀精确匹配（原「非 github 即 gitcode」把 gitee 错归 gitcode 桶）
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
  if (!projectsLoaded.value) loadProjects()
  else if (!projectsHint.value) loadProjects({ silent: true })
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
function loadProjects(opts) {
  // silent：只补拉提示条/列表，不动已渲染的首屏（initialRepos 缓存种子路径用），避免闪烁
  const silent = !!(opts && opts.silent)
  if (!silent) projectsLoaded.value = false
  projectsError.value = ''
  if (!silent) {
    projectsHint.value = ''
    projectsBroken.value = []
  }
  G.API.get('/repos/member')
    .then((d) => {
      repos.value = d.repos || []
      // 后端在"凭据坏了/过期/只读到缓存"时会带 hint + _credential_broken——
      // 以前这里整块丢掉，用户只看到空列表或旧数据，没有任何解释（2026-11-18）。
      projectsHint.value = d.hint || ''
      projectsBroken.value = d._credential_broken || (d._reauth_provider ? [d._reauth_provider] : [])
      projectsLoaded.value = true
    })
    .catch((e) => {
      projectsLoaded.value = true
      projectsError.value = e.message
      const det = (e && e.detail) || {}
      if (det.needs_reauth) {
        projectsError.value = ''
        projectsHint.value = det.message || ''
        projectsBroken.value = det.providers || (det.provider ? [det.provider] : [])
      }
    })
}
// 「去修复凭据」：账号页的凭据 tab（resolveInitialTab 支持 #account-<tab> 形态）
function goFixCredential() {
  // shell 的 nav(view, param) 会把 hash 写成 '#account/<param>'；先设 hash 会被它覆盖，
  // 所以把 tab 作为 param 传进去（AccountView 的 resolveInitialTab 兼容该形态）。
  if (G.app && typeof G.app.nav === 'function') {
    try {
      G.app.nav('account', 'credentials')
      return
    } catch (e) {
      /* 老壳没有 nav 时靠 hash 也能进 */
    }
  }
  window.location.hash = '#account-credentials'
}
function refresh() {
  refreshing.value = true
  G.API.post('/repos/sync')
    .then((d) => {
      repoPage.value = 0
      if (G.app._clearCachedReposData) G.app._clearCachedReposData()
      repos.value = (d && d.repos) || []
      // 口径乙（2026-11-18）：/repos/sync 与 /repos/member 同口径——凭据失效平台的
      // 仓库不返回，但必须把 hint 显示出来，禁止"同步成功"后悄悄少一个平台的仓库。
      projectsHint.value = (d && d.hint) || ''
      projectsBroken.value = (d && d._credential_broken) || (d && d._reauth_provider ? [d._reauth_provider] : [])
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
  // 前缀即平台（任何在册平台都成立，不再二分 github/gitcode）
  const provider = providerFromRepoKey(repoKey)
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
  createModal.space = ''
  createModal.spaces = []
  loadCreateTargets(createModal.provider)
  createModal.open = true
  document.body.style.overflow = 'hidden'
  nextTick(() => {
    if (crNameRef.value) setTimeout(() => crNameRef.value.focus(), 60)
  })
}
function selectCreateProvider(key) {
  if (createModal.provider === key) return
  createModal.provider = key
  createModal.space = ''            // 换平台时空间必须重置（路径属于具体实例）
  createModal.spaces = []
  loadCreateTargets(key)
}
function loadCreateTargets(provider) {
  // Issue #3580：GitLab 实例可建仓在组/子组 → 空间下拉；其它平台返回空 = 只有个人空间
  if (!provider) return
  createModal.spaceLoading = true
  G.API.get('/repos/create-targets?provider=' + encodeURIComponent(provider))
    .then((d) => {
      if (createModal.provider !== provider) return      // 期间切了平台，丢弃迟到响应
      createModal.spaces = (d && d.spaces) || []
    })
    .catch(() => {
      if (createModal.provider === provider) createModal.spaces = []
    })
    .finally(() => {
      if (createModal.provider === provider) createModal.spaceLoading = false
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
  const _space = (createModal.space || '').trim()
  G.API.get('/repos/check-name?name=' + encodeURIComponent(name)
    + '&provider=' + encodeURIComponent(createModal.provider)
    + '&owner=' + encodeURIComponent(_space))
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
          owner: _space,
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
    // 2026-11-18：initialRepos 只是首屏种子；hint / _credential_broken 只存在于
    // /repos/member 响应里，不补拉一次，"已隐藏其仓库"提示条在缓存恢复路径上永远不出现。
    if (activeTab.value === 'projects') loadProjects({ silent: true })
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
/* 凭据异常提示条：解释"为什么列表是空的/是旧的"，并提供一键去修复 */
.rv-cred-hint {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 0 14px;
  padding: 10px 14px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--y) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--y) 35%, transparent);
  color: var(--t1);
  font-size: 13px;
  line-height: 1.5;
}
.rv-cred-hint-ic {
  flex: none;
}
.rv-cred-hint-tx {
  flex: 1 1 auto;
}
.rv-cred-hint-btn {
  flex: none;
  padding: 5px 12px;
  border-radius: 8px;
  border: 1px solid color-mix(in srgb, var(--y) 60%, transparent);
  background: transparent;
  color: var(--a);
  font-size: 12px;
  cursor: pointer;
}
.rv-cred-hint-btn:hover {
  background: color-mix(in srgb, var(--y) 16%, transparent);
}
.rv-content :deep(.template-card-item-name) {
  display: flex;
  align-items: center;
  gap: 4px;
}
</style>
