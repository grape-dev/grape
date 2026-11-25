<template>
  <section class="ap-page">
    <!-- 列表：一行一个平台，Apple 设置式分组列表 -->
    <template v-if="!current">
      <p class="ap-sub">
        {{ t('page.platforms_list_sub') }}
        <AGHelp
          :title="t('page.help_title')"
          :what="t('page.platforms_hint')"
          values="GET /api/admin/repo-platforms → PUT /{provider}/enabled、PUT/DELETE /{provider}/bot"
          :note="t('page.platforms_note')"
        />
      </p>
      <div class="ap-group">
        <div v-for="p in platforms" :key="p.provider" class="ap-row clickable" @click="open(p)">
          <div class="ap-main">
            <img v-if="logoFor(p.provider)" class="ap-logo" :src="logoFor(p.provider)" :alt="p.name || p.provider" />
            <div class="ap-title">{{ p.name || p.provider }}</div>
            <div class="ap-desc">
              {{ p.provider }} · {{ botSummary(p, t) }}
            </div>
          </div>
          <AGPill v-if="healthOf(p)" :tone="healthOf(p).tone">● {{ t(healthOf(p).key) }}</AGPill>
          <AGPill :tone="p.enabled ? 'ok' : 'mute'">{{ p.enabled ? t('page.pill_on') : t('page.pill_off') }}</AGPill>
          <span class="ap-chev">›</span>
        </div>
      </div>
      <p v-if="!loading && !platforms.length" class="ap-hint">{{ t('page.platforms_empty') }}</p>
      <p v-if="loading" class="ap-hint">{{ t('page.loading') }}</p>
      <p v-if="error" class="ap-err">{{ error }}</p>
      <p v-if="notice" class="ap-warn">{{ notice }}</p>

      <!-- Issue #3580/PR#1854：新增平台卡（自建实例一实例一卡：克隆现有同族卡改 id/host/base） -->
      <div class="ap-group">
        <div class="ap-row clickable" @click="openAdd()">
          <div class="ap-main">
            <div class="ap-title">＋ {{ t('page.add_card', '新增平台卡') }}</div>
            <div class="ap-desc">{{ t('page.add_card_desc', '为自建实例新建一张卡（如 gitlab-corp）：克隆现有同族卡后改 id/host/API 基址') }}</div>
          </div>
          <span class="ap-chev">›</span>
        </div>
        <div v-if="addOpen" class="ap-row">
          <span class="ap-label">{{ t('page.add_from', '基于') }}</span>
          <select v-model="addForm.from" class="ap-input">
            <option v-for="p in platforms" :key="p.provider" :value="p.provider">{{ p.name || p.provider }}</option>
          </select>
        </div>
        <div v-if="addOpen" class="ap-row">
          <span class="ap-label">provider id</span>
          <input v-model="addForm.provider" class="ap-input" :placeholder="t('page.add_id_ph', 'gitlab-corp（小写字母开头，字母数字下划线短横）')" />
        </div>
        <div v-if="addOpen" class="ap-row">
          <span class="ap-label">{{ t('page.add_name', '显示名') }}</span>
          <input v-model="addForm.name" class="ap-input" placeholder="GitLab (corp)" />
        </div>
        <div v-if="addOpen" class="ap-row">
          <span class="ap-label">host</span>
          <input v-model="addForm.host" class="ap-input" placeholder="gitlab.corp.example" />
        </div>
        <div v-if="addOpen" class="ap-row">
          <span class="ap-label">api_base</span>
          <input v-model="addForm.api_base" class="ap-input" placeholder="https://gitlab.corp.example/api/v4" />
        </div>
        <div v-if="addOpen" class="ap-add">
          <span class="ap-hint">{{ addError }}</span>
          <button class="ap-btn" :disabled="addSaving" @click="saveAdd()">{{ t('page.save', '保存') }}</button>
          <button class="ap-btn ap-btn-ghost" @click="cancelAdd()">{{ t('page.cancel', '取消') }}</button>
        </div>
      </div>
    </template>

    <!-- 详情 -->
    <template v-else>
      <div class="ap-back" @click="back()"><span class="ap-chev-l">‹</span> {{ t('page.back') }}</div>
      <div class="ap-detail-hd">
        <img v-if="logoFor(current.provider)" class="ap-logo ap-logo-lg" :src="logoFor(current.provider)" :alt="current.name || current.provider" />
        <b>{{ current.name }}</b><code class="ap-key">{{ current.provider }}</code>
        <AGPill v-if="healthOf(current)" :tone="healthOf(current).tone">● {{ t(healthOf(current).key) }}</AGPill>
      </div>
      <p class="ap-sub">{{ t('page.domain_note_platform') }}</p>

      <div class="ap-sect">{{ t('page.sec_state') }}</div>
      <div class="ap-group">
        <div class="ap-row">
          <span class="ap-label">{{ t('page.enabled') }}<AGHelp v-bind="docRow('enabled')" /></span>
          <span class="ap-right">
            <AGPill v-if="healthOf(current)" :tone="healthOf(current).tone">● {{ t(healthOf(current).key) }}</AGPill>
            <label class="ap-switch">
              <input type="checkbox" :checked="current.enabled" @change="toggleEnabled(current, $event)" />
              <span>{{ current.enabled ? t('page.pill_on') : t('page.pill_off') }}</span>
            </label>
          </span>
        </div>
        <div class="ap-row">
          <span class="ap-label">{{ t('page.bot') }}</span>
          <span class="ap-val mono">{{ current.bot_username || t('page.scope_none') }}</span>
        </div>
        <div class="ap-row">
          <span class="ap-label">PAT</span>
          <span class="ap-val mono">{{ current.pat_create_url || t('page.scope_none') }}</span>
        </div>
        <div v-if="healthOf(current) && healthOf(current).detail" class="ap-row">
          <span class="ap-label">{{ t('page.col_health') }}</span>
          <span class="ap-val danger">{{ healthOf(current).detail }}</span>
        </div>
      </div>

      <div class="ap-sect">{{ t('page.sec_definition') }}</div>
      <div class="ap-group">
        <div v-for="f in DEF_FIELDS" :key="f" class="ap-row">
          <span class="ap-label">{{ f }}<AGHelp v-bind="docRow(f)" readonly /></span>
          <span class="ap-val mono">{{ fmt(current[f]) }}</span>
        </div>
      </div>

      <!-- 平台卡高级配置：/admin/repo-platforms 的列表投影不含 ca_bundle/download_base/
           存储字段等，所以私有 CA 在旧页面上根本没有落点（2026-11-20 用户实测
           "管理台看不到配 CA 的地方"）。这里按平台卡 GET 的整卡展示并允许编辑，保存时整卡
           回写（未展示字段原样带回），避免手抄白名单漏字段导致的静默降级。
           文案必须平台无关：这块按卡渲染，gitcode/gitee/github/gitlab/自建实例共用一套，
           把某个平台的语义（例如 GitLab 私有 CA）写死进文案，其它平台点进来就是错的。 -->
      <div class="ap-sect">
        {{ t('page.sec_card', '平台卡（高级）') }}
        <AGHelp
          :title="t('page.sec_card', '平台卡（高级）')"
          :what="t('page.card_what', '平台卡的完整定义：私有 CA（ca_bundle，PEM 绝对路径，空=系统 CA）、PAT 创建页地址、下载基址、存储字段等都在这里；私有 CA 只对走 HTTPS 校验的实例有意义。')"
          values="PUT /api/admin/vcs-platforms"
          :note="t('page.card_note', '保存时以整卡回写，未展示的字段不会丢。改 host/api_base 前先确认实例可达；配置文件定义的平台，改动会落成管理面覆盖。')"
        />
        <button class="ap-btn" @click="editMode ? cancelEdit() : startEdit()">
          {{ editMode ? t('page.cancel') : t('page.edit', '编辑') }}
        </button>
      </div>
      <div class="ap-group">
        <div v-for="f in EDIT_FIELDS" :key="f" class="ap-row">
          <span class="ap-label">{{ f }}</span>
          <input
            v-if="editMode"
            v-model="editForm[f]"
            class="ap-input"
            :placeholder="f === 'ca_bundle' ? t('page.ca_ph', '/path/to/corp-ca.pem（空=系统 CA）') : ''"
          />
          <span v-else class="ap-val mono">{{ fmt(cardVal(f)) }}</span>
        </div>
        <div v-if="cardLoading" class="ap-row"><span class="ap-hint">{{ t('page.loading') }}</span></div>
        <div v-if="editMode" class="ap-add">
          <span class="ap-hint">{{ cardError }}</span>
          <button class="ap-btn" @click="saveCard(current)">{{ t('page.save', '保存') }}</button>
        </div>
        <div v-else-if="cardError" class="ap-row"><span class="ap-err">{{ cardError }}</span></div>
      </div>

      <!-- 平台 bot（D110）：一平台一个 bot，落 grape_t_repo_platforms.bot_token_enc。
           平台卡上的 bot_username 只做首次播种；管理面写下的值才是事实。 -->
      <div class="ap-sect">
        {{ t('page.bots') }}
        <AGHelp
          :title="t('page.bots')"
          :what="t('page.bots_what')"
          values="PUT/DELETE /api/admin/repo-platforms/{provider}/bot"
          :note="t('page.bots_note')"
        />
      </div>
      <div class="ap-group">
        <div class="ap-row">
          <span class="ap-label">{{ t('page.bot_username') }}</span>
          <span class="ap-val muted">{{ botOf(current).username || t('page.bot_none') }}</span>
          <AGPill v-if="botOf(current).status" :tone="botOf(current).status === 'active' ? 'ok' : 'warn'">
            {{ botOf(current).status }}
          </AGPill>
          <span v-if="botOf(current).last_used_at" class="ap-val muted">{{ botOf(current).last_used_at }}</span>
        </div>
        <div class="ap-row">
          <span class="ap-label">{{ t('page.bot_username') }}</span>
          <input v-model="botEdit[current.provider].username" class="ap-input" :placeholder="t('page.bot_username_ph')" />
        </div>
        <div class="ap-row">
          <span class="ap-label">{{ t('page.bot_token') }}</span>
          <input
            v-model="botEdit[current.provider].token"
            class="ap-input"
            type="password"
            :placeholder="t('page.bot_token_ph')"
          />
        </div>
        <div class="ap-add">
          <span class="ap-hint">{{ t('page.bots_hint') }}</span>
          <button class="ap-btn" :disabled="!botOf(current).token_present" @click="clearBot(current)">
            {{ t('page.bot_clear') }}
          </button>
          <button class="ap-btn" @click="saveBot(current)">{{ t('page.bot_save') }}</button>
        </div>
      </div>
      <p v-if="error" class="ap-err">{{ error }}</p>
    </template>

    <!-- 关闭平台确认层：Vue 模板 + Teleport 到 body，样式自带（不依赖 AGModal 的
         设计令牌——admin 页没有加载 ag-ui.css，令牌缺失会导致弹层失去 position:fixed
         而"掉"到页面外面，2026-11-15 实锤）。文案里的数字来自后端 409 的 impact。 -->
    <Teleport to="body">
      <div v-if="disableConfirm.open" class="ap-confirm-mask" @click.self="answerDisableConfirm(false)">
        <div class="ap-confirm-dialog" role="dialog" aria-modal="true">
          <h3 class="ap-confirm-title">{{ t('page.disable_confirm_title') }}</h3>
          <p class="ap-confirm-text">{{ disableConfirm.text }}</p>
          <div class="ap-confirm-btns">
            <button class="ap-btn ap-btn-ghost" @click="answerDisableConfirm(false)">
              {{ t('page.cancel') }}
            </button>
            <button class="ap-btn ap-btn-danger" @click="answerDisableConfirm(true)">
              {{ t('page.disable_confirm_btn') }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { botSummary } from '../lib/bot-summary.js'
import AGHelp from '../components/ui/AGHelp.vue'
import AGPill from '../components/ui/AGPill.vue'

const props = defineProps({ api: { type: Function, required: true } })

// i18n：与 AdminView 同源（admin.* 命名空间），缺键回落到接口 docs
const langVer = ref(0)
try { window.addEventListener('grape:langchange', () => { langVer.value += 1 }) } catch (e) { /* 忽略 */ }

function t(key, fallback = '') {
  void langVer.value
  try {
    const v = window.I18N && window.I18N.t('admin.' + key)
    if (v && v !== 'admin.' + key) return v
  } catch (e) { /* 忽略 */ }
  return fallback
}

const DEF_FIELDS = ['dialect', 'host', 'api_base', 'api_auth', 'webhook_path', 'event_header',
  'webhook_verify', 'webhook_dialect', 'clone_auth', 'issue_write_form', 'branch_create_form',
  'pr_head_filter', 'issue_search', 'git_tree_family']

const platforms = ref([])
const docs = ref({})
// 平台 bot 编辑态：{provider: {username, token}}；token 空=不改（清除走按钮）
const botEdit = reactive({})
const loading = ref(true)
const error = ref('')
const notice = ref('')
// 列表 → 详情：选中平台的 provider key（null = 列表视图）
const selected = ref(null)
// 品牌图标：与登录渠道页共用 /static/img/<provider>.svg（v=2 是图标内容变更的缓存版本）；
// 没有品牌图的平台不硬凑，回退纯文字，避免"图标错位"的假 logo。
const LOGO_KNOWN = new Set(['gitcode', 'github', 'gitee', 'gitlab', 'huawei'])
function logoFor(provider) {
  return LOGO_KNOWN.has(provider) ? `/static/img/${provider}.svg?v=3` : ''
}

const current = computed(() => platforms.value.find((p) => p.provider === selected.value) || null)

function back() {
  selected.value = null
}

function open(p) {
  error.value = ''
  selected.value = p.provider
  editMode.value = false
  cardError.value = ''
  loadCard(p.provider)
}

// ── 平台卡（高级）：整卡读 → 整卡回写 ───────────────────────────────────────
// 列表投影(/repo-platforms)只带用户面需要的键，ca_bundle/download_base/存储字段都不在，
// 所以编辑必须以 /admin/vcs-platforms 的整卡为底稿，保存时把未编辑字段原样带回。
const EDIT_FIELDS = ['name', 'dialect', 'host', 'api_base', 'api_auth', 'webhook_path',
  'event_header', 'webhook_verify', 'webhook_dialect', 'clone_auth', 'download_base',
  'ca_bundle', 'pat_create_url', 'brand_color']
// 只读派生键：不是卡字段，回写会被 Builder 判未知键整卡拒绝
const CARD_DROP = new Set(['provider', 'source', 'config_hash', 'enabled',
  'bot_token_configured', 'bot_token_health', 'webhook_token_configured',
  'webhook_token_supported', 'token_env'])
const card = ref(null)
const cardLoading = ref(false)
const cardError = ref('')
const editMode = ref(false)
const editForm = reactive({})

function cardVal(f) {
  const c = card.value || {}
  if (f in c) return c[f]
  return current.value ? current.value[f] : ''
}

async function loadCard(provider) {
  cardLoading.value = true
  cardError.value = ''
  try {
    const r = await props.api('/vcs-platforms')
    card.value = (r.cards || {})[provider] || null
    if (!card.value) cardError.value = t('page.card_missing', '该平台没有平台卡（仅表投影），高级配置不可编辑')
  } catch (e) {
    cardError.value = `${t('page.load_failed')}：${e.message || e}`
  } finally {
    cardLoading.value = false
  }
}

function startEdit() {
  if (!card.value) return
  cardError.value = ''
  for (const f of EDIT_FIELDS) editForm[f] = card.value[f] === undefined || card.value[f] === null ? '' : String(card.value[f])
  editMode.value = true
}

function cancelEdit() {
  editMode.value = false
  cardError.value = ''
}

// ── 新增平台卡（自建实例：克隆同族卡 → 新 provider id）──────────────────────────
// Issue #3580/PR#1854：PR 侧用 GRAPE_GITLAB_INSTANCES + gitlab@corp 一行 env 加实例；
// 我们的真源是平台卡，所以补一个「克隆现有卡」入口——不硬编码方言默认值
// （私有 CA/api_auth/webhook_verify 这些差异全跟着模板卡走），只要求改 id/host/api_base。
const addOpen = ref(false)
const addSaving = ref(false)
const addError = ref('')
const addForm = reactive({ from: '', provider: '', name: '', host: '', api_base: '' })

function openAdd() {
  addError.value = ''
  addForm.from = addForm.from || (platforms.value[0] && platforms.value[0].provider) || ''
  addForm.provider = ''
  addForm.name = ''
  addForm.host = ''
  addForm.api_base = ''
  addOpen.value = true
}

function cancelAdd() {
  addOpen.value = false
  addError.value = ''
}

async function saveAdd() {
  addError.value = ''
  const id = String(addForm.provider || '').trim().toLowerCase()
  if (!/^[a-z][a-z0-9_-]{0,39}$/.test(id)) {
    addError.value = t('page.add_bad_id', 'provider id 非法：小写字母开头，仅字母/数字/下划线/短横，≤40 字符')
    return
  }
  if (platforms.value.some((p) => p.provider === id)) {
    addError.value = t('page.add_exists', '该 provider id 已存在')
    return
  }
  addSaving.value = true
  try {
    const r0 = await props.api('/vcs-platforms')
    const src = ((r0 && r0.cards) || {})[addForm.from]
    if (!src) throw new Error(t('page.add_no_template', '模板卡不可用（所选平台没有平台卡）'))
    const base = {}
    for (const k of Object.keys(src)) {
      if (CARD_DROP.has(k)) continue
      const v = src[k]
      if (v === null || v === undefined) continue
      base[k] = v
    }
    base.name = String(addForm.name || id).trim()
    if (addForm.host) base.host = String(addForm.host).trim()
    if (addForm.api_base) base.api_base = String(addForm.api_base).trim()
    // 新实例的钩子路径必须带自己的 id，否则与模板卡抢同一个 /webhook/<provider>
    base.webhook_path = `/webhook/${id}`
    // 令牌创建页直达链接是模板平台的官方地址，自建实例不能继承
    delete base.pat_create_url
    const r = await props.api('/vcs-platforms', { method: 'PUT', body: { cards: { [id]: base } } })
    addOpen.value = false
    await load()
    if (r && r.errors && r.errors.length) {
      addError.value = r.errors.join('；')
      return
    }
    const created = platforms.value.find((p) => p.provider === id)
    if (created) open(created)
  } catch (e) {
    const d = e.detail || {}
    addError.value = d.errors ? d.errors.join('；') : d.message || e.message || String(e)
  } finally {
    addSaving.value = false
  }
}

async function saveCard(p) {
  cardError.value = ''
  const base = {}
  const src = card.value || {}
  for (const k of Object.keys(src)) {
    if (CARD_DROP.has(k)) continue
    const v = src[k]
    if (v === null || v === undefined) continue
    base[k] = v
  }
  for (const f of EDIT_FIELDS) base[f] = (editForm[f] || '').trim()
  try {
    const r = await props.api('/vcs-platforms', { method: 'PUT', body: { cards: { [p.provider]: base } } })
    editMode.value = false
    await loadCard(p.provider)
    await load()
    // A 方案（表为运行时真源）：管理面保存会把该平台行提升为 manual（rows: promoted），
    // 此后配置文件改动不再自动覆盖——这是接管语义，不是失败。必须明说，否则运维会
    // 以为「改文件重启就生效」。回退方式：管理面改回 / 删除该平台行后由导入器重建。
    const action = ((r && r.rows) || {})[p.provider]
    const ovs = ((r && r.overrides) || {})[p.provider] || {}
    if (action === 'promoted') {
      const fields = Object.keys(ovs)
      const base = t('page.card_taken_over', {},
        '该平台卡已由管理面接管（定义落表）：配置文件之后的改动不会再自动覆盖')
      cardError.value = fields.length
        ? base + t('page.card_taken_over_fields', { fields: fields.join(', ') },
            '；本次改动字段：{fields}')
        : base
    }
  } catch (e) {
    const d = e.detail || {}
    const detail = d.errors ? d.errors.join('；') : d.message || e.message || e
    cardError.value = `${t('page.save_failed')}：${detail}`
  }
}

function docRow(field) {
  // enabled 的语义在两个页面已经分叉（渠道=能否登录 / 平台=用户面是否显示），
  // 不能用同一份说明：field.enabled 是渠道口径，平台页用 field.enabled_platform。
  const key = field === 'enabled' ? 'enabled_platform' : field
  const d = docs.value[field] || {}
  return {
    title: field,
    what: t(`field.${key}.what`, d.what || ''),
    values: t(`field.${key}.values`, d.values || ''),
    note: t(`field.${key}.note`, d.note || ''),
  }
}
function fmt(v) {
  if (v === true) return 'true'
  if (v === false) return 'false'
  return v === undefined || v === null || v === '' ? t('page.empty') : String(v)
}

// 后端 per-platform 健康结构：{overall | unhealthy/degraded, error_type, error, consecutive_failures}
function healthOf(p) {
  const h = (p && p.bot_token_health) || {}
  if (h.unhealthy || h.overall === 'unhealthy') {
    return { tone: 'bad', key: 'page.health_unhealthy', detail: h.error || h.error_type || '' }
  }
  if (h.degraded || h.overall === 'degraded') {
    return { tone: 'warn', key: 'page.health_degraded', detail: h.error || h.error_type || '' }
  }
  if (h.overall || h.checked_at || h.consecutive_failures !== undefined) {
    return { tone: 'ok', key: 'page.health_healthy', detail: '' }
  }
  return null
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const r = await props.api('/repo-platforms')
    platforms.value = r.platforms || []
    docs.value = r.docs || {}
    for (const p of platforms.value) {
      if (!botEdit[p.provider]) botEdit[p.provider] = { username: '', token: '' }
    }
  } catch (e) {
    error.value = `${t('page.load_failed')}：${e.message || e}`
  } finally {
    loading.value = false
  }
}

// 关闭平台确认层（AGModal）：resolve 由按钮回填，await 它的结果再决定是否重发
const disableConfirm = reactive({ open: false, text: '', impact: {}, resolve: null })
function askDisableConfirm(text, impact) {
  disableConfirm.text = text
  disableConfirm.impact = impact || {}
  disableConfirm.open = true
  return new Promise((resolve) => { disableConfirm.resolve = resolve })
}
function answerDisableConfirm(ok) {
  disableConfirm.open = false
  const r = disableConfirm.resolve
  disableConfirm.resolve = null
  if (r) r(ok)
}

// 开关：关闭平台是"会让存量用户看不到项目"的操作，后端因此要求 confirm=1。
// 前端必须把影响面**弹窗**给操作者看，再带 confirm=1 重发——否则开关永远 409
// （2026-11-15 实锤：页面上没有确认弹窗，导致所有关闭操作都失败）。
async function toggleEnabled(p, ev) {
  const enabled = !!ev.target.checked
  const url = `/repo-platforms/${p.provider}/enabled`
  const rollback = () => { ev.target.checked = !enabled }
  try {
    await props.api(url, { method: 'PUT', body: { enabled } })
    p.enabled = enabled
    return
  } catch (e) {
    if (!enabled && e.status === 409 && e.error_code === 'PLATFORM_DISABLE_NEEDS_CONFIRM') {
      const imp = (e.detail && e.detail.impact) || {}
      const msg = t('page.disable_confirm')
        .replace('{provider}', p.name || p.provider)
        .replace('{repos}', imp.repos || 0)
        .replace('{users}', imp.users || 0)
        .replace('{managed}', imp.managed_repos || 0)
        .replace('{pats}', imp.pat_users || 0)
      // 用 Vue 组件（AGModal）确认，不用 window.confirm——原生弹层不进设计体系、
      // 不能带影响面表格，也不受页面样式/主题控制。
      if (!(await askDisableConfirm(msg, imp))) { rollback(); return }
      try {
        await props.api(`${url}?confirm=1`, { method: 'PUT', body: { enabled } })
        p.enabled = enabled
        return
      } catch (e2) {
        rollback()
        error.value = `${t('page.toggle_failed')}：${e2.message || e2}`
        return
      }
    }
    rollback()
    error.value = `${t('page.toggle_failed')}：${e.message || e}`
  }
}

// 平台 bot 只有一个：读侧摘要（capabilities 同形），写侧走 repo-platforms 子资源
function botOf(p) {
  const b = (p && p.bot_accounts && p.bot_accounts[0]) || {}
  return {
    username: b.username || '',
    status: b.status || '',
    last_used_at: b.last_used_at || '',
    token_present: Boolean(b.username || (b.status && b.status !== 'disabled')),
  }
}

async function saveBot(p) {
  const st = botEdit[p.provider] || { username: '', token: '' }
  const body = {}
  if ((st.username || '').trim()) body.username = st.username.trim()
  if ((st.token || '').trim()) body.token = st.token.trim()
  if (!body.username && !body.token) {
    error.value = t('page.bot_need_username')
    return
  }
  try {
    const res = await props.api(`/repo-platforms/${p.provider}/bot`, { method: 'PUT', body })
    await load()
    st.username = ''
    st.token = ''
    error.value = ''
    // Issue #4301: show the binding-time workflow precheck result (missing or unknown scope).
    const warn = (res && res.scope_warnings) || []
    notice.value = warn.length ? warn.join('\n') : ''
  } catch (e) {
    error.value = `${t('page.bot_save_failed')}：${e.message || e}`
  }
}

async function clearBot(p) {
  try {
    await props.api(`/repo-platforms/${p.provider}/bot`, { method: 'DELETE' })
    notice.value = ''
    await load()
    error.value = ''
  } catch (e) {
    error.value = `${t('page.bot_del_failed')}：${e.message || e}`
  }
}

onMounted(load)
</script>

<style scoped>
/* Apple 设置式分组列表：灰底页面 + 白色内嵌组 + 细分割线 + 右侧 chevron */
.ap-page { max-width: 880px; }
.ap-detail-hd { display: flex; align-items: center; gap: 8px; font-size: 17px; font-weight: 600; color: #1d1d1f; margin: 2px 0 6px; }
.ap-h1 { display: flex; align-items: center; gap: 8px; font-size: 24px; font-weight: 700; letter-spacing: -0.02em; color: #1d1d1f; margin: 4px 0 6px; }
.ap-sub { font-size: 13px; color: #6e6e73; margin: 0 0 18px; line-height: 1.5; }
.ap-sect { display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: #86868b; margin: 24px 4px 8px; }
.ap-group { background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05); margin-bottom: 6px; }
.ap-row { display: flex; align-items: center; gap: 10px; padding: 13px 16px; border-bottom: 1px solid #f0f0f2; min-height: 44px; }
.ap-row:last-child { border-bottom: 0; }
.ap-row.clickable { cursor: pointer; transition: background 0.15s; }
.ap-row.clickable:hover { background: #fafafa; }
.ap-main { flex: 1; min-width: 0; }
.ap-title { font-size: 15px; font-weight: 600; color: #1d1d1f; }
.ap-logo { width: 20px; height: 20px; flex: 0 0 20px; object-fit: contain; border-radius: 4px; }
.ap-logo-lg { width: 28px; height: 28px; flex: 0 0 28px; margin-right: 2px; }
.ap-desc { font-size: 12.5px; color: #6e6e73; margin-top: 2px; }
.ap-label { font-size: 13px; color: #3a3a3c; min-width: 168px; display: inline-flex; align-items: center; }
.ap-val { font-size: 13px; color: #1d1d1f; word-break: break-all; flex: 1; text-align: right; }
.ap-val.mono, .ap-title.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
.ap-val.danger { color: #b3261e; }
.ap-val.muted { color: #8e8e93; text-align: left; }
.ap-key { background: #f2f2f7; border-radius: 6px; padding: 2px 8px; font-size: 12px; color: #3a3a3c; font-weight: 400; }
.ap-right { display: inline-flex; align-items: center; gap: 10px; }
.ap-switch { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: #3a3a3c; }
.ap-chev { color: #c7c7cc; font-size: 20px; line-height: 1; }
.ap-chev-l { font-size: 18px; margin-right: 2px; }
.ap-back { display: inline-flex; align-items: center; gap: 2px; font-size: 14px; color: #0071e3; cursor: pointer; margin-bottom: 10px; }
.ap-back:hover { text-decoration: underline; }
.ap-add { display: flex; align-items: center; gap: 8px; padding: 12px 16px; border-top: 1px solid #f0f0f2; flex-wrap: wrap; background: #fcfcfd; }
.ap-input { flex: 1; min-width: 150px; padding: 8px 10px; border: 1px solid #d2d2d7; border-radius: 8px; font-size: 13px; color: #1d1d1f; background: #fff; }
.ap-input:focus { outline: none; border-color: #0071e3; box-shadow: 0 0 0 3px rgba(0, 113, 227, 0.14); }
.ap-check { display: inline-flex; align-items: center; gap: 4px; font-size: 12.5px; color: #3a3a3c; }
.ap-btn { padding: 8px 16px; border: 0; border-radius: 980px; background: #0071e3; color: #fff; cursor: pointer; font-size: 13px; font-weight: 500; }
.ap-btn-ghost { background: #f5f5f7; color: #1d1d1f; }
.ap-btn-ghost:hover { background: #ececef; }
.ap-btn-danger { background: #e5484d; }
.ap-btn-danger:hover { background: #d13d42; }
.ap-confirm-mask { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.35); display: flex; align-items: center; justify-content: center; z-index: 2000; }
.ap-confirm-dialog { width: min(92vw, 520px); background: #fff; border-radius: 14px; box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18); padding: 20px 22px; color: #1d1d1f; }
.ap-confirm-title { font-size: 15px; font-weight: 600; margin: 0 0 10px; }
.ap-confirm-btns { display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; }
.ap-confirm-text { line-height: 1.7; color: #1d1d1f; margin: 0; }
.ap-btn:hover { background: #0077ed; }
.ap-icon { padding: 4px 10px; border: 1px solid #d2d2d7; border-radius: 980px; background: #fff; font-size: 12px; color: #0071e3; cursor: pointer; }
.ap-icon.danger { color: #b3261e; border-color: #f0c9c5; }
.ap-hint { color: #6e6e73; font-size: 13px; padding: 8px 4px; }
.ap-err { color: #b3261e; font-size: 13px; padding: 8px 4px; }
.ap-warn { color: var(--c-warning); font-size: 13px; padding: 8px 4px; white-space: pre-line; }
</style>
