<template>
  <div class="admv-root">
    <!-- ===== Login ===== -->
    <div v-if="!loggedIn" class="admv-login">
      <div class="lbox">
        <img class="logo" :src="'/static/img/grape-logo.jpg?v=1'" alt="Grape Admin" />
        <h1>Grape Admin</h1>
        <p class="sub">{{ t('login.subtitle') }}</p>
        <input
          v-model="loginUser"
          class="ipt"
          :placeholder="t('login.admin_account')"
          autocomplete="username"
        />
        <input
          v-model="loginPass"
          class="ipt"
          type="password"
          :placeholder="t('login.password')"
          autocomplete="current-password"
          @keydown.enter="doLogin"
        />
        <button class="lbtn" @click="doLogin">{{ t('login.login_btn') }}</button>
        <div class="err">{{ loginErr }}</div>
        <div class="lback">
          <a href="/">{{ '← ' + t('login.back_to_app') }}</a>
        </div>
      </div>
      <div class="lang-switch" @click="toggleLang">
        <span>{{ lang === 'zh' ? 'EN' : '中' }}</span>
      </div>
    </div>

    <!-- ===== Main frame ===== -->
    <div v-else class="admv-main">
      <div class="sb">
        <div class="sb-h">
          <div class="logo-s">
            <img class="sq" :src="'/static/img/grape-logo.jpg?v=1'" alt="A" />
            <h1>Grape Admin</h1>
          </div>
          <div class="tagline">Config Center</div>
        </div>
        <div class="sb-nav">
          <div class="grp">{{ t('nav.config_center') }}</div>
          <a :class="{ act: view === 'cfg' }" @click="showCfg"
            ><span class="ic">⚙</span><span>{{ t('nav.credentials') }}</span></a
          >
          <a :class="{ act: view === 'channels' }" @click="view = 'channels'"
            ><span class="ic">🔐</span><span>{{ t('nav.channels') }}</span></a
          >
          <a :class="{ act: view === 'platforms' }" @click="view = 'platforms'"
            ><span class="ic">🧩</span><span>{{ t('nav.platforms') }}</span></a
          >
          <div class="grp">{{ t('nav.user_management') }}</div>
          <a :class="{ act: view === 'users' }" @click="showUsers(1)"
            ><span class="ic">👥</span><span>{{ t('nav.user_list') }}</span></a
          >
          <a :class="{ act: view === 'wl' }" @click="showWhitelist"
            ><span class="ic">📋</span><span>{{ t('nav.whitelist') }}</span></a
          >
          <a :class="{ act: view === 'lwl' }" @click="showLoginWhitelist"
            ><span class="ic">🔐</span><span>{{ t('nav.login_whitelist') }}</span></a
          >
          <div class="grp">Account</div>
          <a @click="openPwdModal('self')"
            ><span class="ic">🔑</span><span>{{ t('nav.change_password') }}</span></a
          >
        </div>
        <div class="sb-ft">
          <div class="ft-row">
            <div class="ft-user">
              <span class="badge">A</span><span>{{ adminUser }}</span>
            </div>
            <div class="lang-toggle">
              <button class="lang-btn" :class="{ active: lang === 'zh' }" @click="setLang('zh')">
                中
              </button>
              <button class="lang-btn" :class="{ active: lang === 'en' }" @click="setLang('en')">
                EN
              </button>
            </div>
          </div>
          <a class="out" @click="doLogout">{{ t('nav.logout') }}</a>
        </div>
      </div>
      <div class="mn">
        <!-- topbar -->
        <div class="tb">
          <div class="bc">
            <b v-if="view === 'cfg'">{{ t('config.title') }}</b>
            <template v-else-if="view === 'users'"
              ><b>{{ t('users.title') }}</b>
              <span class="cnt">{{ t('users.total', { count: usersTotal }) }}</span></template
            >
            <b v-else-if="view === 'wl'">{{ t('whitelist.title') }}</b>
            <b v-else-if="view === 'lwl'">{{ t('login_whitelist.title') }}</b>
            <b v-else-if="view === 'channels'">{{ t('nav.channels') }}</b>
            <b v-else-if="view === 'platforms'">{{ t('nav.platforms') }}</b>
          </div>
          <div v-if="view === 'users'" class="tb-tools">
            <div class="search-wrap">
              <span class="search-ic">🔍</span>
              <input
                v-model="userSearch"
                class="ipt"
                :placeholder="t('users.search_ph')"
                @input="searchUsersDebounced"
              />
            </div>
            <button class="btn btn-o btn-sm" @click="openUserModal('create')">
              {{ t('users.create_user') }}
            </button>
          </div>
        </div>

        <AdminChannels v-if="view === 'channels'" :api="api" class="ct" />
        <AdminPlatforms v-if="view === 'platforms'" :api="api" class="ct" />
        <div v-if="view === 'cfg'" class="ct">
          <div class="card">
            <div class="card-h">
              <h2>
                {{ t('config.webhook_url') }}
                <span class="tag">{{ t('config.webhook_url_hint') }}</span>
              </h2>
            </div>
            <div class="f-row">
              <label>webhook_url</label>
              <input
                v-model="cfg.webhook_url"
                :placeholder="
                  cfgP.webhook_url_present ? t('config.configured') : t('config.webhook_url_ph')
                "
              />
            </div>
          </div>
          <!-- Issue #470：运行参数（超时 / DevSpace 规格）——改完当前实例即时生效 -->
          <div class="card">
            <div class="card-h">
              <h2>
                {{ t('config.runtime_params') }}
                <span class="tag">{{ t('config.runtime_params_desc') }}</span>
              </h2>
            </div>
            <div class="f-row">
              <label>runtime_task_timeout</label>
              <input v-model="cfg.runtime_task_timeout" type="number" min="1" placeholder="3600" />
            </div>
            <div class="hint">{{ t('config.runtime_timeout_hint') }}</div>
            <div class="f-row">
              <label>devstage_template_id</label>
              <input v-model="cfg.devstage_template_id" placeholder="4ef59a2b…" />
            </div>
            <div class="f-row">
              <label>devstage_flavor_id</label>
              <input v-model="cfg.devstage_flavor_id" placeholder="23ebdfe3…" />
            </div>
            <div class="f-row">
              <label>devstage_cores</label>
              <input v-model="cfg.devstage_cores" type="number" min="1" placeholder="3" />
            </div>
            <div class="f-row">
              <label>devstage_memory</label>
              <input v-model="cfg.devstage_memory" type="number" min="1" placeholder="6" />
            </div>
            <div class="f-row">
              <label>devstage_arch</label>
              <select v-model="cfg.devstage_arch">
                <option value="ARM">ARM</option>
                <option value="x86">x86</option>
              </select>
            </div>
            <div class="hint">{{ t('config.devstage_hint') }}</div>
            <div v-if="cfgP.runtime_task_timeout_effective" class="hint">
              {{ t('config.runtime_effective') }}: {{ cfgP.runtime_task_timeout_effective }}s
            </div>
          </div>
          <div class="save-bar">
            <button class="btn btn-p" :disabled="saving" @click="saveConfig">
              {{ t('config.save') }}
            </button>
            <span class="st">{{ t('config.save_hint') }}</span>
          </div>
        </div>

        <!-- Users -->
        <div v-else-if="view === 'users'" class="ct">
          <table v-if="users.length" class="tbl">
            <thead>
              <tr>
                <th>{{ t('users.col_username') }}</th>
                <th>{{ t('users.col_email') }}</th>
                <th>{{ t('users.col_status') }}</th>
                <th>{{ t('users.col_last_login') }}</th>
                <th style="width: 40px"></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="u in users" :key="u.id" @click="onRowClick(u, $event)">
                <td>
                  <div class="user-name">{{ u.username }}</div>
                  <div v-if="u.nickname" class="user-sub">{{ u.nickname }}</div>
                </td>
                <td class="td-sub">{{ u.email || '—' }}</td>
                <td>
                  <span class="status-dot" :class="dotCls(u.status)"></span
                  >{{ statusLabel(u.status) }}
                </td>
                <td class="td-sub">{{ u.last_login_at || '—' }}</td>
                <td class="row-actions" @click.stop>
                  <button class="row-actions-btn" @click="toggleRowMenu(u.id)">⋯</button>
                  <div class="row-menu" :class="{ show: openMenuId === u.id }">
                    <button v-if="u.status !== 2" @click="openUserModal('edit', u)">
                      {{ t('users.edit') }}
                    </button>
                    <button v-if="u.status !== 2" @click="openUserModal('reset', u)">
                      {{ t('users.reset_password') }}
                    </button>
                    <button v-if="u.status === 1" class="danger" @click="disableUser(u)">
                      {{ t('users.disable') }}
                    </button>
                    <button v-else-if="u.status === 0 || u.status === 3" @click="restoreUser(u)">
                      {{ t('users.restore') }}
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-else class="empty-state">
            <div class="empty-ic">🔍</div>
            <div class="empty-tx">{{ t('users.no_results') }}</div>
          </div>
          <div class="pager-bar">
            <div class="pager-info">{{ pagerInfo }}</div>
            <div class="pager-btns">
              <button class="pager-btn" :disabled="userPage <= 1" @click="showUsers(userPage - 1)">
                ‹
              </button>
              <span class="pager-label">{{ userPage }} / {{ totalPages }}</span>
              <button
                class="pager-btn"
                :disabled="userPage >= totalPages"
                @click="showUsers(userPage + 1)"
              >
                ›
              </button>
            </div>
          </div>
        </div>

        <!-- Whitelist（注册白名单）-->
        <div v-else-if="view === 'wl'" class="ct">
          <div class="card">
            <div class="card-h">
              <h2>{{ t('whitelist.add') }}</h2>
            </div>
            <div class="f-row">
              <label>{{ t('whitelist.type') }}</label>
              <select v-model="wl.type" class="ipt">
                <option value="email">{{ t('whitelist.type_email') }}</option>
                <option value="domain">{{ t('whitelist.type_domain') }}</option>
              </select>
            </div>
            <div class="f-row">
              <label>{{ t('whitelist.value') }}</label>
              <input v-model="wl.value" class="ipt" :placeholder="t('whitelist.value_ph')" />
            </div>
            <div class="f-row">
              <label>{{ t('whitelist.remark') }}</label>
              <input v-model="wl.remark" class="ipt" :placeholder="t('whitelist.remark_ph')" />
              <button class="btn btn-p btn-sm" @click="addWhitelist">
                {{ t('whitelist.add_btn') }}
              </button>
            </div>
          </div>
          <div class="card">
            <div class="card-h">
              <h2>{{ t('whitelist.signup_settings') }}</h2>
            </div>
            <div class="f-row">
              <label>{{ t('whitelist.signup_switch') }}</label>
              <select v-model="allowSignup" class="ipt">
                <option value="0">{{ t('whitelist.signup_off') }}</option>
                <option value="1">{{ t('whitelist.signup_on') }}</option>
              </select>
              <button class="btn btn-p btn-sm" @click="saveAllowSignup">
                {{ t('whitelist.save') }}
              </button>
            </div>
            <div class="hint">{{ t('whitelist.signup_hint') }}</div>
          </div>
          <div class="card">
            <div class="card-h">
              <h2>{{ t('whitelist.list') }}</h2>
            </div>
            <table class="tbl">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>{{ t('whitelist.col_type') }}</th>
                  <th>{{ t('whitelist.col_value') }}</th>
                  <th>{{ t('whitelist.col_remark') }}</th>
                  <th>{{ t('whitelist.col_created') }}</th>
                  <th>{{ t('whitelist.col_actions') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="w in signupWl" :key="w.id" class="wlrow">
                  <td>{{ w.id }}</td>
                  <td>
                    {{
                      w.type === 'email' ? t('whitelist.type_email') : t('whitelist.type_domain')
                    }}
                  </td>
                  <td>{{ w.value }}</td>
                  <td>{{ w.remark || '-' }}</td>
                  <td class="td-sub">{{ w.created_at || '-' }}</td>
                  <td>
                    <button class="btn-del-sm" @click="deleteWhitelist(w)">
                      {{ t('whitelist.delete') }}
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
        <div v-else-if="view === 'lwl'" class="ct">
          <div class="card">
            <div class="card-h">
              <h2>
                {{ t('login_whitelist.title') }}
                <span class="tag">{{ t('login_whitelist.desc') }}</span>
              </h2>
            </div>
            <div class="f-row">
              <label>{{ t('login_whitelist.toggle') }}</label>
              <select v-model="loginWlEnabled" class="ipt">
                <option value="0">{{ t('login_whitelist.toggle_off') }}</option>
                <option value="1">{{ t('login_whitelist.toggle_on') }}</option>
              </select>
              <button class="btn btn-p btn-sm" @click="toggleLoginWhitelist">
                {{ t('whitelist.save') }}
              </button>
            </div>
            <div class="hint">{{ t('login_whitelist.toggle_hint') }}</div>
          </div>
          <div v-if="loginWlEnabled === '1' && !loginWl.length" class="card card-warn">
            <div class="hint warn">{{ t('login_whitelist.empty_warn') }}</div>
          </div>
          <div class="card">
            <div class="card-h">
              <h2>{{ t('login_whitelist.add') }}</h2>
            </div>
            <div class="f-row">
              <label>{{ t('login_whitelist.provider') }}</label>
              <!-- 渠道目录驱动（D109：grape_t_oauth_channels 为真源）：新渠道注册即出现在下拉里，
                   前端零平台字面量；provider key 是存储/判定键，name 只做展示。 -->
              <select v-model="lw.provider" class="ipt">
                <option v-for="p in lwProviders" :key="p.provider" :value="p.provider">
                  {{ providerLabel(p.provider) }}
                </option>
              </select>
              <span v-if="!lwProviders.length" class="inline-hint">{{
                t('login_whitelist.no_providers')
              }}</span>
              <label>{{ t('login_whitelist.username') }}</label>
              <input
                v-model="lw.username"
                class="ipt"
                :placeholder="t('login_whitelist.username_ph')"
              />
              <label>{{ t('login_whitelist.remark') }}</label>
              <input
                v-model="lw.remark"
                class="ipt"
                :placeholder="t('login_whitelist.remark_ph')"
              />
              <button class="btn btn-p btn-sm" @click="addLoginWhitelist">
                {{ t('login_whitelist.add_btn') }}
              </button>
            </div>
          </div>
          <div class="card">
            <div class="card-h">
              <h2>{{ t('login_whitelist.list') }}</h2>
            </div>
            <table class="tbl">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>{{ t('login_whitelist.col_provider') }}</th>
                  <th>{{ t('login_whitelist.col_username') }}</th>
                  <th>{{ t('login_whitelist.col_remark') }}</th>
                  <th>{{ t('login_whitelist.col_created') }}</th>
                  <th>{{ t('login_whitelist.col_actions') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="w in loginWl" :key="w.id + ':' + w.provider" class="wlrow">
                  <td>{{ w.id }}</td>
                  <td>
                    {{ providerLabel(w.provider) }}
                    <span v-if="w.legacy" class="tag">{{ t('login_whitelist.legacy_tag') }}</span>
                  </td>
                  <td>{{ w.username || '-' }}</td>
                  <td>{{ w.remark || '-' }}</td>
                  <td class="td-sub">{{ w.created_at || '-' }}</td>
                  <td>
                    <button class="btn-del-sm" @click="deleteLoginWhitelist(w)">
                      {{ t('login_whitelist.delete') }}
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- ===== pwd modal ===== -->
    <div v-if="pwdModal.open" class="modal">
      <div class="mbox">
        <h3>{{ t('pwd_modal.title') }}</h3>
        <div class="msub">{{ t('pwd_modal.desc') }}</div>
        <input
          v-model="pwdModal.old"
          class="ipt mbox-ipt"
          type="password"
          :placeholder="t('pwd_modal.old_password')"
        />
        <input
          v-model="pwdModal.new_"
          class="ipt mbox-ipt"
          type="password"
          :placeholder="t('pwd_modal.new_password')"
        />
        <input
          v-model="pwdModal.confirm"
          class="ipt mbox-ipt"
          type="password"
          :placeholder="t('pwd_modal.confirm_password')"
        />
        <div class="m-actions">
          <button class="m-btn-o" @click="pwdModal.open = false">{{ t('pwd_modal.skip') }}</button>
          <button class="m-btn-p" @click="changePwd">{{ t('pwd_modal.confirm') }}</button>
        </div>
      </div>
    </div>

    <!-- ===== user modal ===== -->
    <div v-if="userModal.open" class="modal" @click.self="userModal.open = false">
      <div class="mbox">
        <h3>{{ userModalTitle }}</h3>
        <div v-if="userModal.mode === 'edit'" class="msub">
          {{ t('users.edit') + ': ' + (userModal.user ? userModal.user.username : '') }}
        </div>
        <template v-if="userModal.mode === 'create'">
          <input
            v-model="um.username"
            class="ipt mbox-ipt"
            :placeholder="t('users.create_username')"
          />
          <input v-model="um.email" class="ipt mbox-ipt" :placeholder="t('users.create_email')" />
          <input
            v-model="um.password"
            class="ipt mbox-ipt"
            type="password"
            :placeholder="t('users.create_password')"
          />
          <input
            v-model="um.nickname"
            class="ipt mbox-ipt"
            :placeholder="t('users.create_nickname')"
          />
        </template>
        <input
          v-else-if="userModal.mode === 'reset'"
          v-model="um.password"
          class="ipt mbox-ipt"
          type="password"
          :placeholder="t('users.reset_new_password')"
        />
        <template v-else>
          <input v-model="um.email" class="ipt mbox-ipt" :placeholder="t('users.edit_email')" />
          <input
            v-model="um.nickname"
            class="ipt mbox-ipt"
            :placeholder="t('users.edit_nickname')"
          />
        </template>
        <div class="m-actions">
          <button class="m-btn-o" @click="userModal.open = false">{{ t('pwd_modal.skip') }}</button>
          <button class="m-btn-p" @click="submitUserModal">{{ userModalBtn }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
// P10d（#406 同类）：密码最小长度口径统一，禁止在调用点散落长度阈值硬编码
const PWD_MIN_LEN = 8
function isPwdTooShort(p) {
  return !p || String(p).length < PWD_MIN_LEN
}

// Batch 17 (admin): standalone admin site (login + config center + users + whitelists)
// migrated from legacy/js/admin.js. This page does NOT live in the app.js shell —
// it is its own document (admin.html) with its own I18N namespace "admin.*".
// Behavior-equivalent migration, no visual upgrades (visual debt tracked separately).
import { reactive, ref, computed } from 'vue'
import AdminChannels from './AdminChannels.vue'
import AdminPlatforms from './AdminPlatforms.vue'

const G = globalThis

const TOKEN_KEY = 'grape_admin_token'

// ---------- state ----------
const loggedIn = ref(false)
const loginUser = ref('')
const loginPass = ref('')
const loginErr = ref('')
const adminUser = ref('admin')
const view = ref('cfg')
const lang = ref(initLang())
const openMenuId = ref(null)

const saving = ref(false)
const cfg = reactive({
  // 只放部署级配置：webhook_url + 运行参数。平台/渠道凭据归各自的菜单
  // （登录渠道 → client_id/secret；仓库平台 → bot_username/bot 凭据）。
  webhook_url: '',
  // Issue #470：运行参数（空 = 不改；数值字段由后端校验正整数）
  runtime_task_timeout: '',
  devstage_template_id: '',
  devstage_flavor_id: '',
  devstage_cores: '',
  devstage_memory: '',
  devstage_arch: '',
})
const cfgP = reactive({}) // presence flags from server (placeholders only, never rendered value)
const users = ref([])
const usersTotal = ref(0)
const userPage = ref(1)
const userSearch = ref('')
const PER_PAGE = 25
const totalPages = computed(() => Math.max(1, Math.ceil(usersTotal.value / PER_PAGE)))
const pagerInfo = computed(() => {
  const s = usersTotal.value === 0 ? 0 : (userPage.value - 1) * PER_PAGE + 1
  const e = Math.min(userPage.value * PER_PAGE, usersTotal.value)
  return s + '-' + e + ' / ' + usersTotal.value
})

const wl = reactive({ type: 'email', value: '', remark: '' })
const allowSignup = ref('0')
const signupWl = ref([])
const loginWlEnabled = ref('0')
const lw = reactive({ provider: '', username: '', remark: '' })
const loginWl = ref([])
// 渠道目录（GET /admin/whitelist 的 providers）：D109 下真源是 grape_t_oauth_channels，
// 新渠道注册即出现在下拉里——前端不再写死平台集合。
const lwProviders = ref([])

const pwdModal = reactive({ open: false, old: '', new_: '', confirm: '' })
const userModal = reactive({ open: false, mode: 'create', user: null })
const um = reactive({ username: '', email: '', password: '', nickname: '' })
const userModalTitle = computed(() =>
  userModal.mode === 'create'
    ? t('users.create_user')
    : userModal.mode === 'reset'
      ? t('users.reset_password')
      : t('users.edit')
)
const userModalBtn = computed(() =>
  userModal.mode === 'create'
    ? t('users.create_btn')
    : userModal.mode === 'reset'
      ? t('users.reset_btn')
      : t('users.edit_btn')
)

// ---------- i18n ----------
function initLang() {
  return (G.localStorage.getItem('grapeLang') || 'zh') === 'en' ? 'en' : 'zh'
}
function t(key, params) {
  // admin.* namespace; tOr pattern: key equality means miss
  const v = G.I18N.t('admin.' + key, params || {})
  return v === 'admin.' + key ? key : v
}
function setLang(l) {
  if (l === lang.value) return
  G.I18N.setLang(l).then(() => {
    lang.value = l
  })
}
function toggleLang() {
  setLang(lang.value === 'zh' ? 'en' : 'zh')
}
function dotCls(s) {
  return { 0: 's-off', 1: 's-on', 2: 's-del', 3: 's-cool' }[s] || 's-del'
}
function statusLabel(s) {
  return t(
    'users.status_' + { 0: 'disabled', 1: 'active', 2: 'deleted', 3: 'cooldown' }[s] || 'deleted'
  )
}

// ---------- toast ----------
function toast(msg, ok) {
  let wrap = document.querySelector('.toast-wrap')
  if (!wrap) {
    wrap = document.createElement('div')
    wrap.className = 'toast-wrap'
    document.body.appendChild(wrap)
  }
  const el = document.createElement('div')
  el.className = 'toast ' + (ok ? 'ok' : 'err')
  el.textContent = msg
  wrap.appendChild(el)
  setTimeout(() => {
    el.remove()
  }, 3500)
}

// ---------- api ----------
function getToken() {
  return G.localStorage.getItem(TOKEN_KEY) || ''
}
function setToken(v) {
  v ? G.localStorage.setItem(TOKEN_KEY, v) : G.localStorage.removeItem(TOKEN_KEY)
}

function errMsg(e) {
  const code = (e && e.error_code) || 'UNKNOWN'
  return t('error.' + code)
}

async function api(path, opts) {
  opts = opts || {}
  opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {})
  const tk = getToken()
  if (tk && path.indexOf('/login') < 0) opts.headers['Authorization'] = 'Bearer ' + tk
  if (opts.body && typeof opts.body === 'object') opts.body = JSON.stringify(opts.body)
  try {
    return await jsonFetch('/api/admin' + path, opts)
  } catch (err) {
    if (err && err.status === 401 && path.indexOf('/login') < 0) {
      // P1-2: try refresh once, then redirect to login
      try {
        const d = await jsonFetch('/api/admin/refresh', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
          credentials: 'same-origin',
        })
        setToken(d.token)
        opts.headers['Authorization'] = 'Bearer ' + d.token
        return await jsonFetch('/api/admin' + path, opts)
      } catch (e2) {
        setToken(null)
        loggedIn.value = false
        loginErr.value = ''
        throw err
      }
    }
    throw err
  }
}

async function jsonFetch(url, opts) {
  const r = await G.fetch(url, opts)
  const data = await r.json()
  if (!r.ok) {
    const e = new Error((data && data.detail && data.detail.message) || 'UNKNOWN')
    e.status = r.status
    e.error_code = (data && data.detail && data.detail.error_code) || 'UNKNOWN'
    // 409 影响面预检要把统计读出来（关闭平台前弹窗展示），否则前端只能干瞪眼
    e.detail = (data && data.detail) || null
    e.payload = data
    throw e
  }
  return data
}

// ---------- login / logout ----------
function doLogin() {
  const u = loginUser.value.trim()
  const p = loginPass.value.trim()
  if (!u || !p) {
    loginErr.value = t('login.enter_credentials')
    return
  }
  loginErr.value = ''
  api('/login', { method: 'POST', body: { username: u, password: p } })
    .then((d) => {
      setToken(d.token)
      adminUser.value = d.user.username
      loggedIn.value = true
      if (d.pwd_initial) pwdModal.open = true
      showCfg()
    })
    .catch((e) => {
      loginErr.value = errMsg(e)
    })
}

function doLogout() {
  api('/logout', { method: 'POST' })
    .catch(() => {})
    .then(() => {
      setToken(null)
      G.location.reload()
    })
}

// ---------- config ----------
function showCfg() {
  view.value = 'cfg'
  api('/config')
    .then((d) => {
      const c = d.config || {}
      Object.assign(cfgP, { webhook_url_present: c.webhook_url_present })
      cfg.webhook_url = c.webhook_url || ''
      // Issue #470：运行参数（GET 已回显设置值/默认值 + 当前生效值）
      cfg.runtime_task_timeout =
        c.runtime_task_timeout === undefined || c.runtime_task_timeout === null
          ? ''
          : String(c.runtime_task_timeout)
      cfg.devstage_template_id = c.devstage_template_id || ''
      cfg.devstage_flavor_id = c.devstage_flavor_id || ''
      cfg.devstage_cores =
        c.devstage_cores === undefined || c.devstage_cores === null ? '' : String(c.devstage_cores)
      cfg.devstage_memory =
        c.devstage_memory === undefined || c.devstage_memory === null
          ? ''
          : String(c.devstage_memory)
      cfg.devstage_arch = c.devstage_arch || ''
      cfgP.runtime_task_timeout_effective = c.runtime_task_timeout_effective || ''
    })
    .catch((e) => {
      toast(t('config.load_failed') + ': ' + errMsg(e), false)
    })
}

function saveConfig() {
  saving.value = true
  const body = {}
  // 只提交部署级配置；平台/渠道凭据一律不从这里写（各自菜单负责，避免双入口互相覆盖）
  if (cfg.webhook_url.trim()) body.webhook_url = cfg.webhook_url.trim()
  // Issue #470：运行参数——非空才提交（空 = 保持现值）；数值字段转 int
  const _rtTimeout = String(cfg.runtime_task_timeout || '').trim()
  if (_rtTimeout) {
    const n = parseInt(_rtTimeout, 10)
    if (n > 0) body.runtime_task_timeout = n
  }
  ;['devstage_template_id', 'devstage_flavor_id', 'devstage_arch'].forEach(function (k) {
    const v = String(cfg[k] || '').trim()
    if (v) body[k] = v
  })
  ;['devstage_cores', 'devstage_memory'].forEach(function (k) {
    const v = parseInt(String(cfg[k] || '').trim(), 10)
    if (v > 0) body[k] = v
  })
  api('/config', { method: 'PUT', body })
    .then(() => {
      toast(t('config.save_success'), true)
      showCfg()
    })
    .catch((e) => {
      toast(t('config.save_failed') + ': ' + errMsg(e), false)
    })
    .finally(() => {
      saving.value = false
    })
}

// ---------- users ----------
let searchTimer = null
function searchUsersDebounced() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    showUsers(1)
  }, 300)
}

function showUsers(page) {
  userPage.value = page || 1
  view.value = 'users'
  openMenuId.value = null
  const q = userSearch.value || ''
  api('/users?page=' + userPage.value + '&per_page=' + PER_PAGE + '&q=' + encodeURIComponent(q))
    .then((d) => {
      users.value = d.users || []
      usersTotal.value = d.total
    })
    .catch((e) => {
      toast(t('users.load_failed') + ': ' + errMsg(e), false)
    })
}

function onRowClick(u, e) {
  if (!e.target.closest('.row-actions')) openUserModal('edit', u)
}
function toggleRowMenu(uid) {
  openMenuId.value = openMenuId.value === uid ? null : uid
}
function disableUser(u) {
  if (!G.confirm(t('users.disable_confirm'))) return
  api('/users/' + u.id + '/disable', { method: 'POST', body: { reason: 'admin_disabled' } })
    .then(() => {
      toast(t('users.disabled'), true)
      showUsers(userPage.value)
    })
    .catch((e) => {
      toast(t('users.disable_failed') + ': ' + errMsg(e), false)
    })
}
function restoreUser(u) {
  api('/users/' + u.id + '/restore', { method: 'POST' })
    .then(() => {
      toast(t('users.restored'), true)
      showUsers(userPage.value)
    })
    .catch((e) => {
      toast(t('users.restore_failed') + ': ' + errMsg(e), false)
    })
}

function openUserModal(mode, user) {
  userModal.mode = mode
  userModal.user = user || null
  um.username = ''
  um.email = (user && user.email) || ''
  um.password = ''
  um.nickname = (user && user.nickname) || ''
  userModal.open = true
  openMenuId.value = null
}

function submitUserModal() {
  if (userModal.mode === 'create') {
    const username = um.username.trim()
    const password = um.password.trim()
    if (!username) {
      toast(t('users.username_required'), false)
      return
    }
    if (isPwdTooShort(password)) {
      toast(t('users.password_required'), false)
      return
    }
    api('/users', {
      method: 'POST',
      body: { username, password, email: um.email.trim(), nickname: um.nickname.trim() },
    })
      .then(() => {
        toast(t('users.user_created'), true)
        userModal.open = false
        showUsers(1)
      })
      .catch((e) => {
        toast(t('users.user_create_failed') + ': ' + errMsg(e), false)
      })
  } else if (userModal.mode === 'reset') {
    const password = um.password.trim()
    if (isPwdTooShort(password)) {
      toast(t('users.password_required'), false)
      return
    }
    api('/users/' + userModal.user.id + '/reset-password', { method: 'POST', body: { password } })
      .then(() => {
        toast(t('users.password_reset'), true)
        userModal.open = false
      })
      .catch((e) => {
        toast(t('users.password_reset_failed') + ': ' + errMsg(e), false)
      })
  } else {
    api('/users/' + userModal.user.id, {
      method: 'PUT',
      body: { email: um.email.trim(), nickname: um.nickname.trim() },
    })
      .then(() => {
        toast(t('users.user_updated'), true)
        userModal.open = false
        showUsers(userPage.value)
      })
      .catch((e) => {
        toast(t('users.user_update_failed') + ': ' + errMsg(e), false)
      })
  }
}

// ---------- change password ----------
function openPwdModal() {
  pwdModal.open = true
}
function changePwd() {
  const o = pwdModal.old
  const newP = pwdModal.new_
  const c = pwdModal.confirm
  if (!o || !newP) {
    toast(t('pwd_modal.empty_fields'), false)
    return
  }
  if (isPwdTooShort(newP)) {
    toast(t('pwd_modal.too_short'), false)
    return
  }
  // P12b：弱口令预检（pwd-blocklist.js 提供 window.isWeakPassword），避免前端全绿后端 400
  if (typeof window !== 'undefined' && window.isWeakPassword && window.isWeakPassword(newP)) {
    toast(t('pwd_modal.too_common'), false)
    return
  }
  if (newP !== c) {
    toast(t('pwd_modal.mismatch'), false)
    return
  }
  api('/change-password', { method: 'POST', body: { old_password: o, new_password: newP } })
    .then(() => {
      toast(t('pwd_modal.changed'), true)
      setToken(null)
      pwdModal.open = false
      loggedIn.value = false
      loginPass.value = ''
      loginErr.value = ''
    })
    .catch((e) => {
      toast(errMsg(e), false)
    })
}

// ---------- whitelists ----------
function showWhitelist() {
  view.value = 'wl'
  loadWhitelist()
  loadAllowSignup()
}

// 登录白名单是独立页面（渠道维度）；注册白名单是邮箱/域名维度，两者语义不同不混排
function showLoginWhitelist() {
  view.value = 'lwl'
  loadLoginWhitelist()
}

function loadWhitelist() {
  api('/signup-whitelist')
    .then((d) => {
      signupWl.value = d.whitelist || []
    })
    .catch((e) => {
      toast(t('whitelist.load_failed') + ': ' + errMsg(e), false)
    })
}
function addWhitelist() {
  if (!wl.value.trim()) {
    toast(t('whitelist.enter_value'), false)
    return
  }
  api('/signup-whitelist', {
    method: 'POST',
    body: { type: wl.type, value: wl.value.trim(), remark: wl.remark.trim() },
  })
    .then(() => {
      toast(t('whitelist.added'), true)
      wl.value = ''
      wl.remark = ''
      loadWhitelist()
    })
    .catch((e) => {
      toast(t('whitelist.add_failed') + ': ' + errMsg(e), false)
    })
}
function deleteWhitelist(w) {
  api('/signup-whitelist/' + w.id, { method: 'DELETE' })
    .then(() => {
      toast(t('whitelist.deleted'), true)
      loadWhitelist()
    })
    .catch((e) => {
      toast(t('whitelist.delete_failed') + ': ' + errMsg(e), false)
    })
}
function loadAllowSignup() {
  api('/config')
    .then((d) => {
      allowSignup.value = ((d.config || {}).allow_signup || '0') === '1' ? '1' : '0'
    })
    .catch(() => {})
}
function saveAllowSignup() {
  api('/allow-signup', { method: 'PUT', body: { enabled: allowSignup.value === '1' } })
    .then(() => {
      toast(t('whitelist.updated'), true)
    })
    .catch((e) => {
      toast(t('whitelist.update_failed') + ': ' + errMsg(e), false)
    })
}
function loadLoginWhitelist() {
  api('/whitelist')
    .then((d) => {
      loginWl.value = d.whitelist || []
      loginWlEnabled.value = d && d.enabled ? '1' : '0'
      lwProviders.value = d.providers || []
      // 默认选中第一个在册渠道；当前选中项已不在目录里（渠道下线）也要纠正，
      // 否则点"添加"必然 400 OAUTH_CHANNEL_NOT_CONFIGURED。
      const keys = lwProviders.value.map((p) => p.provider)
      if (!keys.includes(lw.provider)) lw.provider = keys[0] || ''
    })
    .catch((e) => {
      toast(t('login_whitelist.load_failed') + ': ' + errMsg(e), false)
    })
}
// provider key → 展示名（卡 name）；目录里没有就用 key 本身（存量列式条目的平台可能已下线）
// 渠道显示名：优先 i18n 专有名词（华为云 / Huawei Cloud），其次渠道目录里的 name，
// 最后回退 provider key。中文库里存的 name 是中文，英文界面直接用它会漏出中文。
function providerLabel(key) {
  const loc = t('provider_name.' + key, '')
  if (loc) return loc
  const hit = lwProviders.value.find((p) => p.provider === key)
  return (hit && hit.name) || key || '-'
}
function addLoginWhitelist() {
  if (!lw.provider) {
    toast(t('login_whitelist.no_providers'), false)
    return
  }
  if (!lw.username.trim()) {
    toast(t('login_whitelist.username_required'), false)
    return
  }
  api('/whitelist', {
    method: 'POST',
    body: { provider: lw.provider, username: lw.username.trim(), remark: lw.remark.trim() },
  })
    .then(() => {
      toast(t('login_whitelist.added'), true)
      lw.username = ''
      lw.remark = ''
      loadLoginWhitelist()
    })
    .catch((e) => {
      toast(t('login_whitelist.add_failed') + ': ' + errMsg(e), false)
    })
}
function deleteLoginWhitelist(w) {
  // 老列式行按 provider 定向清列（一人多平台老行互不误伤，与后端 S11 口径一致）；
  // 新模式行带 provider 也无副作用（按 (id, provider) 删）。
  const q = w.provider ? '?provider=' + encodeURIComponent(w.provider) : ''
  api('/whitelist/' + w.id + q, { method: 'DELETE' })
    .then(() => {
      toast(t('login_whitelist.deleted'), true)
      loadLoginWhitelist()
    })
    .catch((e) => {
      toast(t('login_whitelist.delete_failed') + ': ' + errMsg(e), false)
    })
}
function toggleLoginWhitelist() {
  api('/whitelist/toggle', { method: 'PUT', body: { enabled: loginWlEnabled.value === '1' } })
    .then(() => {
      toast(t('login_whitelist.toggle_updated'), true)
    })
    .catch((e) => {
      toast(t('login_whitelist.toggle_failed') + ': ' + errMsg(e), false)
    })
}

// ---------- init ----------
// already-logged-in path (legacy init(): showApp + showCfg)
if (getToken()) {
  api('/config')
    .then(() => {
      loggedIn.value = true
      showCfg()
    })
    .catch(() => {})
}
</script>

<style>
/* migrated from admin.html inline styles (unscoped: page is standalone, admin.css tokens live in tokens.css) */
/* ── Auth 区（登录 / 二次校验）= 固定深色场景 ─────────────────────────
   判定（2026-11-11）：apple-design-suite 的 scene 例外——登录页自带深色地面
   （linear-gradient(180deg,#1d1d1f,#000)）+ 玻璃卡，两主题下都保持深色，不跟随
   主题翻转。所以这里的 --auth-* 是「场景局部 token」，不是主题 token：
   --auth-text/#f5f5f7、--auth-text-secondary/#86868b 是深色地面上的固定前景。
   新增 auth 样式时继续用这套，别改成 var(--t)/var(--t3)。 */
.admv-root {
  min-height: 100vh;
}
/* 登录面板是「固定深色场景」：把 auth 局部 token 钉死，不跟主题翻。
   admin.html 里的 :root 映射写成 var(--t)/var(--glass)，浅色档会把登录框变成
   白底深字（肉眼还行但跟深色地面冲突）。 */
.admv-login {
  --auth-text: #f5f5f7;
  --auth-text-secondary: rgba(235, 235, 245, 0.62);
  --auth-bg-surface: rgba(255, 255, 255, 0.06);
  --auth-border: rgba(255, 255, 255, 0.12);
  --auth-accent: var(--a);
  --auth-accent-hover: var(--ah);
  --auth-error: var(--r);
}
/* 登录面板（固定深色场景）的输入框 */
.admv-login .ipt {
  width: 100%;
  padding: 12px 14px;
  border-radius: 12px;
  border: 1px solid var(--auth-border, rgba(255, 255, 255, 0.1));
  background: var(--auth-bg-surface, rgba(255, 255, 255, 0.05));
  color: var(--auth-text, #f5f5f7);
  font-size: 15px;
  outline: none;
  margin-bottom: 12px;
  transition:
    border-color 0.2s,
    box-shadow 0.2s;
}
.admv-login .ipt::placeholder {
  color: var(--auth-text-secondary, #86868b);
}
.admv-login .ipt:focus {
  border-color: var(--auth-accent, #0071e3);
}
/* 控制台内的输入框/下拉（白名单/弹窗/搜索）：跟随主题的紧凑尺寸。
   之前这里被 .admv-root .ipt（登录场景样式）通吃 → 15px 字号 + 半透明底，
   在 flex 行里又被三个 200px 标签挤成 30px 宽。 */
.ipt {
  width: 100%;
  flex: 1 1 200px;
  min-width: 0;
  padding: 9px 12px;
  border-radius: 10px;
  border: 1px solid var(--b, #e3e6ee);
  background: var(--c, #fff);
  color: var(--t, #101724);
  font-size: 13px;
  outline: none;
  transition:
    border-color 0.18s,
    box-shadow 0.18s;
}
.ipt::placeholder {
  color: var(--t3, #8d97a8);
}
.ipt:focus {
  border-color: var(--a, #3168e0);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--a, #3168e0) 14%, transparent);
}
.admv-root .lback {
  margin-top: 18px;
  font-size: 13px;
  color: var(--auth-text-secondary, #86868b);
}
.admv-root .lback a {
  color: var(--auth-text-secondary, #86868b);
  text-decoration: none;
}

/* Login */
.admv-login {
  position: fixed;
  inset: 0;
  z-index: 200;
  background: linear-gradient(180deg, #1d1d1f 0%, #000000 100%);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow-y: auto;
  padding: 40px 20px;
}
.admv-login .lbox {
  width: 400px;
  max-width: 100%;
  text-align: center;
}
.lbox .logo {
  display: inline-block;
  width: 80px;
  height: 80px;
  border-radius: 20px;
  margin-bottom: 20px;
  object-fit: cover;
}
.lbox h1 {
  color: var(--auth-text, #f5f5f7);
  font-size: 24px;
  font-weight: 600;
  letter-spacing: -0.02em;
  margin-bottom: 4px;
}
.lbox .sub {
  color: var(--auth-text-secondary, #86868b);
  font-size: 14px;
  font-weight: 400;
  margin-bottom: 28px;
}
.lbtn {
  width: 100%;
  padding: 14px;
  border-radius: 999px;
  border: none;
  background: var(--auth-accent, #0071e3);
  color: var(--on-accent);
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.3s cubic-bezier(0.25, 0.1, 0.25, 1);
  margin-bottom: 16px;
  min-height: 44px;
}
.lbtn:hover {
  background: var(--ah, #0077ed);
}
.err {
  color: var(--auth-error, #ff3b30);
  font-size: 13px;
  margin-top: 10px;
  min-height: 18px;
}
.lang-switch {
  position: fixed;
  bottom: 16px;
  right: 16px;
  padding: 6px 14px;
  border-radius: 980px;
  background: rgba(255, 255, 255, 0.08);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  font-size: 12px;
  font-weight: 500;
  color: var(--auth-text-secondary, #86868b);
  cursor: pointer;
  transition: all 0.2s ease;
  z-index: 210;
  user-select: none;
  border: 1px solid rgba(255, 255, 255, 0.06);
}
.lang-switch:hover {
  background: rgba(255, 255, 255, 0.12);
  color: var(--auth-text, #f5f5f7);
}
.lang-toggle {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 2px;
  border-radius: 999px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.04);
}
.lang-btn {
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
  border: none;
  cursor: pointer;
  background: none;
  color: rgba(255, 255, 255, 0.4);
  transition: all 0.2s;
}
.lang-btn:hover {
  color: rgba(255, 255, 255, 0.7);
}
.lang-btn.active {
  background: var(--a, #3168e0);
  color: var(--on-accent);
}

/* Main frame */
.admv-main {
  display: flex;
  min-height: 100vh;
}
.sb {
  width: 232px;
  background: #0c1222;
  color: var(--on-accent);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  position: sticky;
  top: 0;
  height: 100vh;
}
.sb-h {
  padding: 18px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}
.sb-h .logo-s {
  display: flex;
  align-items: center;
  gap: 9px;
}
.sb-h .logo-s .sq {
  width: 26px;
  height: 26px;
  border-radius: 7px;
  object-fit: cover;
}
.sb-h h1 {
  font-size: 15px;
  font-weight: 700;
}
.sb-h .tagline {
  font-size: 10px;
  color: var(--t3, #8d97a8);
  text-transform: uppercase;
  letter-spacing: 0.8px;
}
.sb-nav {
  flex: 1;
  padding: 10px;
  overflow-y: auto;
}
.sb-nav a {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 10px;
  border-radius: 7px;
  color: #c2cad6;
  text-decoration: none;
  font-size: 13px;
  cursor: pointer;
  margin-bottom: 2px;
}
.sb-nav a:hover {
  background: #161e30;
  color: var(--on-accent);
}
.sb-nav a.act {
  background: var(--a, #3168e0);
  color: var(--on-accent);
  font-weight: 500;
}
.sb-nav .grp {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 1.1px;
  color: var(--t3, #8d97a8);
  padding: 14px 10px 5px;
  font-weight: 600;
}
.sb-nav a .ic {
  width: 18px;
  text-align: center;
}
.sb-ft {
  padding: 14px;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
  font-size: 12px;
  color: #c2cad6;
}
.sb-ft .ft-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.sb-ft .ft-user {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #c2cad6;
}
.sb-ft .ft-user .badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 5px;
  background: rgba(255, 255, 255, 0.1);
  font-size: 10px;
  font-weight: 600;
  color: var(--on-accent);
}
.sb-ft .out {
  color: #ff6b6b;
  cursor: pointer;
  display: block;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid rgba(255, 255, 255, 0.04);
}

.mn {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.tb {
  background: var(--c, #fff);
  border-bottom: 1px solid var(--b, #e3e6ee);
  padding: 0 28px;
  height: 52px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.tb .bc {
  font-size: 13px;
  color: var(--t2, #5b667b);
}
.tb .bc b {
  color: var(--t, #101724);
  font-weight: 600;
}
.tb .cnt {
  color: var(--t2, #5b667b);
  font-size: 12px;
  margin-left: 8px;
}
.tb-tools {
  margin-left: auto;
  display: flex;
  gap: 8px;
}
.ct {
  padding: 22px 24px;
  overflow-y: auto;
  flex: 1;
  max-width: 980px;
}

/* Card */
.card {
  background: var(--c, #fff);
  border: 1px solid var(--b, #e3e6ee);
  border-radius: var(--r2, 12px);
  padding: 20px 22px;
  margin-bottom: 14px;
  box-shadow:
    0 2px 8px rgba(0, 0, 0, 0.04),
    0 4px 16px rgba(0, 0, 0, 0.06);
}
.card.sub {
  margin-top: 8px;
  padding: 14px 18px;
}
/* 空表 fail-closed 警示（S6 语义）：红边浅底，视觉上必须比普通卡更"响" */
.card.card-warn {
  border-color: #f0b4ae;
  background: #fff5f4;
  padding: 12px 18px;
}
.hint.warn {
  margin: 0;
  color: #b3261e;
}
.inline-hint {
  font-size: 12px;
  color: #b3261e;
}
.card-h {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}
.card-h h2 {
  font-size: 15px;
  font-weight: 600;
}
.card-h h2 .tag {
  font-size: 11px;
  font-weight: 400;
  color: var(--t2, #5b667b);
  background: var(--bg, #f5f6f8);
  padding: 2px 8px;
  border-radius: 8px;
  margin-left: 6px;
}
.f-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 12px;
}
.f-row label {
  width: 200px;
  font-size: 13px;
  color: var(--t2, #5b667b);
  flex-shrink: 0;
}
.f-row input {
  flex: 1 1 200px;
  min-width: 180px;
  padding: 9px 12px;
  border-radius: var(--rd, 8px);
  border: 1px solid var(--b, #e3e6ee);
  font-size: 13px;
  outline: none;
  background: var(--c);
  color: var(--t, #101724);
}
.f-row input:focus {
  border-color: var(--a, #3168e0);
}
/* 次要操作（校验/刷新）：透明底 + 淡蓝描边 + 蓝字，hover 实心蓝。
   原来只写在 .f-row 下，card-h 里的「刷新」按钮完全没样式（浏览器原生按钮）。 */
.s-btn {
  padding: 4px 14px;
  border-radius: 999px;
  border: 1px solid color-mix(in srgb, var(--a, #3168e0) 42%, transparent);
  background: transparent;
  font-size: 12px;
  cursor: pointer;
  color: var(--a, #3168e0);
  flex-shrink: 0;
  transition:
    background 0.18s,
    color 0.18s,
    border-color 0.18s;
}
.s-btn:hover {
  background: var(--a, #3168e0);
  border-color: var(--a, #3168e0);
  color: #fff;
}
.f-row .st {
  font-size: 12px;
  color: var(--g, #1ca44c);
  min-width: 90px;
}
.f-row select {
  flex: 0 0 auto;
  min-width: 120px;
  max-width: 240px;
  padding: 9px 12px;
  border-radius: var(--rd, 8px);
  border: 1px solid var(--b, #e3e6ee);
  font-size: 13px;
  outline: none;
  background: var(--c);
  color: var(--t, #101724);
}
.f-row select:focus {
  border-color: var(--a, #3168e0);
}
.hint {
  font-size: 12px;
  color: var(--t2, #5b667b);
  margin: -6px 0 12px 210px;
}
.save-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 8px;
}
.btn {
  padding: 7px 18px;
  border-radius: 999px;
  border: none;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition:
    background 0.18s,
    filter 0.18s;
}
.btn-sm {
  padding: 4px 14px;
  font-size: 12px;
  border-radius: 999px;
}
.btn-p {
  background: var(--a, #3168e0);
  color: var(--on-accent);
  border: 1px solid transparent;
  border-radius: 999px;
}
.btn-p:hover:not(:disabled) {
  background: var(--ah, #0077ed);
  filter: brightness(1.06);
}
.btn-p:disabled {
  opacity: 0.5;
  cursor: default;
}
.btn-o {
  background: transparent;
  color: var(--a, #3168e0);
  border: 1px solid color-mix(in srgb, var(--a, #3168e0) 42%, transparent);
  border-radius: 999px;
}
.btn-o:hover {
  background: var(--a, #3168e0);
  border-color: var(--a, #3168e0);
  color: #fff;
}
.btn-del-sm {
  padding: 4px 12px;
  border: 1px solid color-mix(in srgb, var(--r, #dc2828) 55%, transparent);
  border-radius: 999px;
  background: transparent;
  font-size: 12px;
  color: var(--r, #dc2828);
  cursor: pointer;
}
.btn-del-sm:hover {
  background: var(--r, #dc2828);
  color: #fff;
}

/* users view inputs */
.search-wrap {
  position: relative;
  display: flex;
  align-items: center;
}
.search-wrap .ipt {
  width: 220px;
  padding: 6px 10px 6px 32px;
  font-size: 13px;
  margin-bottom: 0;
  background: var(--c);
  color: var(--t, #101724);
  border: 1px solid var(--b, #e3e6ee);
  border-radius: 8px;
}
.search-wrap .ipt:focus {
  border-color: var(--a, #3168e0);
}
.search-wrap .search-ic {
  position: absolute;
  left: 10px;
  font-size: 13px;
  color: var(--t3, #8d97a8);
  pointer-events: none;
}
.tbl {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.tbl th {
  padding: 10px 12px;
  text-align: left;
  border-bottom: 2px solid var(--b, #e3e6ee);
  color: var(--t2, #5b667b);
  font-weight: 600;
  white-space: nowrap;
}
.tbl td {
  padding: 10px 12px;
  border-bottom: 1px solid #f0f1f4;
  color: var(--t, #101724);
}
.tbl tr:hover td {
  background: var(--bg, #f5f6f8);
}
.tbl tr {
  cursor: pointer;
  transition: background 0.15s;
}
.td-sub {
  color: var(--t2, #5b667b);
}
.wlrow {
  cursor: default;
}
.user-name {
  font-weight: 500;
  color: var(--t, #101724);
}
.user-sub {
  font-size: 11px;
  color: var(--t3, #8d97a8);
  margin-top: 2px;
}
.status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
  vertical-align: middle;
}
.status-dot.s-on {
  background: var(--g, #1ca44c);
}
.status-dot.s-off {
  background: var(--r, #dc2828);
}
.status-dot.s-del {
  background: var(--t2, #5b667b);
}
.status-dot.s-cool {
  background: var(--y, #d49c0f);
}
.row-actions {
  position: relative;
}
.row-actions-btn {
  padding: 4px 10px;
  border: 1px solid var(--b, #e3e6ee);
  border-radius: 999px;
  background: transparent;
  cursor: pointer;
  font-size: 14px;
  color: var(--t2, #5b667b);
  line-height: 1;
  transition:
    background 0.18s,
    color 0.18s,
    border-color 0.18s;
}
.row-actions-btn:hover {
  background: color-mix(in srgb, var(--a, #3168e0) 10%, transparent);
  border-color: color-mix(in srgb, var(--a, #3168e0) 42%, transparent);
  color: var(--a, #3168e0);
}
.row-menu {
  position: absolute;
  right: 0;
  top: 100%;
  margin-top: 4px;
  background: var(--c);
  border: 1px solid var(--b, #e3e6ee);
  border-radius: 10px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
  z-index: 10;
  min-width: 140px;
  display: none;
  overflow: hidden;
}
.row-menu.show {
  display: block;
}
.row-menu button {
  display: block;
  width: 100%;
  padding: 8px 14px;
  border: none;
  background: none;
  text-align: left;
  font-size: 13px;
  color: var(--t, #101724);
  cursor: pointer;
}
.row-menu button:hover {
  background: var(--bg, #f5f6f8);
}
.row-menu button.danger {
  color: var(--r, #dc2828);
}
.empty-state {
  text-align: center;
  padding: 48px 20px;
  color: var(--t3, #8d97a8);
}
.empty-state .empty-ic {
  font-size: 32px;
  margin-bottom: 8px;
  opacity: 0.4;
}
.empty-state .empty-tx {
  font-size: 14px;
}
.pager-bar {
  margin-top: 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.pager-info {
  font-size: 12px;
  color: var(--t2, #5b667b);
}
.pager-btns {
  display: flex;
  gap: 6px;
  align-items: center;
}
.pager-label {
  font-size: 12px;
  color: var(--t2, #5b667b);
  padding: 0 4px;
}
.pager-btn {
  padding: 4px 12px;
  border: 1px solid var(--b, #e3e6ee);
  border-radius: 999px;
  background: transparent;
  font-size: 12px;
  cursor: pointer;
  color: var(--t2, #5b667b);
  transition:
    background 0.18s,
    color 0.18s,
    border-color 0.18s;
}
.pager-btn:hover {
  background: color-mix(in srgb, var(--a, #3168e0) 10%, transparent);
  border-color: color-mix(in srgb, var(--a, #3168e0) 42%, transparent);
  color: var(--a, #3168e0);
}
.pager-btn:disabled {
  opacity: 0.4;
  cursor: default;
}
.tag-s {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 500;
  background: #e3f7ec;
  color: #1a8a3e;
}
.tag-d {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 500;
  background: color-mix(in srgb, var(--r) 12%, transparent);
  color: #c93030;
}
.tag-w {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 500;
  background: #fff3e0;
  color: #b0651a;
}

/* Modal */
.modal {
  position: fixed;
  inset: 0;
  z-index: 300;
  background: rgba(0, 0, 0, 0.35);
  backdrop-filter: blur(6px);
  display: flex;
  align-items: center;
  justify-content: center;
}
.mbox {
  width: 420px;
  max-width: 92vw;
  background: var(--c);
  border-radius: 18px;
  padding: 26px;
}
.mbox h3 {
  font-size: 17px;
  font-weight: 700;
  margin-bottom: 6px;
}
.mbox .msub {
  font-size: 13px;
  color: var(--t2, #5b667b);
  margin-bottom: 18px;
  line-height: 1.6;
}
.mbox .mbox-ipt {
  background: var(--chip-bg);
  color: var(--t, #1d1d1f);
}
.mbox .m-actions {
  display: flex;
  gap: 10px;
  justify-content: center;
  margin-top: 18px;
}
.mbox .m-actions button {
  flex: 1;
  padding: 9px;
  border-radius: 999px;
  border: none;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition:
    background 0.18s,
    color 0.18s,
    filter 0.18s;
}
.m-btn-o {
  background: transparent;
  color: var(--a, #0071e3);
  border: 1px solid color-mix(in srgb, var(--a, #0071e3) 42%, transparent);
}
.m-btn-o:hover {
  background: var(--a, #0071e3);
  border-color: var(--a, #0071e3);
  color: #fff;
}
.m-btn-p {
  background: var(--a, #0071e3);
  color: var(--on-accent);
}
.m-btn-p:hover {
  background: var(--ah, #0077ed);
  filter: brightness(1.06);
}
</style>
