<template>
  <div class="beta">
    <AGCard v-if="ready" class="beta__card">
      <div class="beta__logo">
        <img :src="logoSrc" alt="Grape" />
        <h1>{{ t('title') }}</h1>
        <p>{{ t('subtitle') }}</p>
      </div>

      <div class="beta__step-title">{{ t('step_basic') }}</div>
      <div class="beta__field">
        <label>{{ t('lbl_platform') }} <span class="beta__req">*</span></label>
        <select v-model="form.fAccountType">
          <option value="gitcode">GitCode</option>
          <option value="github">GitHub</option>
        </select>
      </div>
      <div class="beta__field">
        <label>{{ t('lbl_username') }} <span class="beta__req">*</span></label>
        <input v-model="form.fAccountName" type="text" :placeholder="t('ph_username')" />
      </div>
      <div class="beta__field">
        <label>{{ t('lbl_email') }} <span class="beta__req">*</span></label>
        <input v-model="form.fEmail" type="email" placeholder="user@example.com" />
      </div>
      <div class="beta__field">
        <label>{{ t('lbl_phone') }}</label>
        <input v-model="form.fPhone" type="text" :placeholder="t('ph_phone_optional')" />
      </div>

      <div class="beta__step-title">{{ t('step_identity') }}</div>
      <div class="beta__field">
        <label>{{ t('lbl_identity') }} <span class="beta__req">*</span></label>
        <div class="beta__id-grid">
          <div
            v-for="id in identities"
            :key="id.type"
            :class="['beta__id-card', { selected: identity === id.type }]"
            @click="identity = id.type"
          >
            <span class="icon">{{ id.icon }}</span>
            <div class="info">
              <div class="title">{{ t(id.key) }}</div>
              <div class="desc">{{ t(id.descKey) }}</div>
            </div>
          </div>
        </div>
      </div>

      <!-- student branch -->
      <div v-if="identity === 'student'" class="beta__branch">
        <div class="beta__field">
          <label>{{ t('lbl_education') }} <span class="beta__req">*</span></label>
          <div class="beta__radio-group">
            <label
              v-for="opt in educationOpts"
              :key="opt"
              :class="['beta__radio-item', { selected: radio.education === v(opt) }]"
            >
              <input v-model="radio.education" type="radio" :value="v(opt)" style="display: none" />
              {{ t(opt) }}
              <input
                v-if="opt === 'opt_other'"
                v-model="other.education"
                type="text"
                :placeholder="t('ph_enter')"
                class="beta__other-text"
              />
            </label>
          </div>
        </div>
        <div class="beta__field">
          <label>{{ t('lbl_major') }} <span class="beta__req">*</span></label>
          <div class="beta__radio-group">
            <label
              v-for="opt in majorOpts"
              :key="opt"
              :class="['beta__radio-item', { selected: radio.major === v(opt) }]"
            >
              <input v-model="radio.major" type="radio" :value="v(opt)" style="display: none" />
              {{ t(opt) }}
              <input
                v-if="opt === 'opt_other'"
                v-model="other.major"
                type="text"
                :placeholder="t('ph_enter')"
                class="beta__other-text"
              />
            </label>
          </div>
        </div>
      </div>

      <!-- enterprise branch -->
      <div v-if="identity === 'enterprise'" class="beta__branch">
        <div class="beta__field">
          <label>{{ t('lbl_company_size') }} <span class="beta__req">*</span></label>
          <div class="beta__radio-group">
            <label
              v-for="opt in companySizeOpts"
              :key="opt"
              :class="['beta__radio-item', { selected: radio.companySize === v(opt) }]"
            >
              <input v-model="radio.companySize" type="radio" :value="v(opt)" style="display: none" />
              {{ t(opt) }}
            </label>
          </div>
        </div>
        <div class="beta__field">
          <label>{{ t('lbl_role') }} <span class="beta__req">*</span></label>
          <div class="beta__radio-group">
            <label
              v-for="opt in roleOpts"
              :key="opt"
              :class="['beta__radio-item', { selected: radio.role === v(opt) }]"
            >
              <input v-model="radio.role" type="radio" :value="v(opt)" style="display: none" />
              {{ t(opt) }}
              <input
                v-if="opt === 'opt_other'"
                v-model="other.role"
                type="text"
                :placeholder="t('ph_enter')"
                class="beta__other-text"
              />
            </label>
          </div>
        </div>
        <div class="beta__field">
          <label>{{ t('lbl_use_case') }} <span class="beta__req">*</span>{{ t('multiselect_suffix') }}</label>
          <div class="beta__checkbox-group">
            <label
              v-for="opt in entUseCaseOpts"
              :key="opt"
              :class="[
                'beta__checkbox-item',
                { selected: checks.entUseCases.some((c) => c.v === v(opt)) },
              ]"
            >
              <input
                type="checkbox"
                ::value="v(opt)"
                :checked="checks.entUseCases.some((c) => c.v === v(opt))"
                style="display: none"
                @change="toggleCheck('entUseCases', v(opt), $event)"
              />
              {{ t(opt) }}
              <input
                v-if="opt === 'opt_other'"
                v-model="other.entUseCases"
                type="text"
                :placeholder="t('ph_enter')"
                class="beta__other-text"
              />
            </label>
          </div>
        </div>
      </div>

      <!-- opc branch -->
      <div v-if="identity === 'opc'" class="beta__branch">
        <div class="beta__field">
          <label>{{ t('lbl_occupation') }} <span class="beta__req">*</span></label>
          <div class="beta__radio-group">
            <label
              v-for="opt in occupationOpts"
              :key="opt"
              :class="['beta__radio-item', { selected: radio.occupation === v(opt) }]"
            >
              <input v-model="radio.occupation" type="radio" :value="v(opt)" style="display: none" />
              {{ t(opt) }}
              <input
                v-if="opt === 'opt_other'"
                v-model="other.occupation"
                type="text"
                :placeholder="t('ph_enter')"
                class="beta__other-text"
              />
            </label>
          </div>
        </div>
        <div class="beta__field">
          <label>{{ t('lbl_opc_use_case') }} <span class="beta__req">*</span>{{ t('multiselect_suffix') }}</label>
          <div class="beta__checkbox-group">
            <label
              v-for="opt in opcUseCaseOpts"
              :key="opt"
              :class="[
                'beta__checkbox-item',
                { selected: checks.opcUseCases.some((c) => c.v === v(opt)) },
              ]"
            >
              <input
                type="checkbox"
                ::value="v(opt)"
                :checked="checks.opcUseCases.some((c) => c.v === v(opt))"
                style="display: none"
                @change="toggleCheck('opcUseCases', v(opt), $event)"
              />
              {{ t(opt) }}
              <input
                v-if="opt === 'opt_other'"
                v-model="other.opcUseCases"
                type="text"
                :placeholder="t('ph_enter')"
                class="beta__other-text"
              />
            </label>
          </div>
        </div>
      </div>

      <!-- other branch -->
      <div v-if="identity === 'other'" class="beta__branch">
        <div class="beta__field">
          <label>{{ t('lbl_other_desc') }} <span class="beta__req">*</span></label>
          <input v-model="form.fOtherDesc" type="text" :placeholder="t('ph_other_desc')" />
        </div>
        <div class="beta__field">
          <label
            >{{ t('lbl_other_use_case') }} <span class="beta__req">*</span>{{ t('multiselect_suffix') }}</label
          >
          <div class="beta__checkbox-group">
            <label
              v-for="opt in otherUseCaseOpts"
              :key="opt"
              :class="[
                'beta__checkbox-item',
                { selected: checks.otherUseCases.some((c) => c.v === v(opt)) },
              ]"
            >
              <input
                type="checkbox"
                ::value="v(opt)"
                :checked="checks.otherUseCases.some((c) => c.v === v(opt))"
                style="display: none"
                @change="toggleCheck('otherUseCases', v(opt), $event)"
              />
              {{ t(opt) }}
              <input
                v-if="opt === 'opt_other'"
                v-model="other.otherUseCases"
                type="text"
                :placeholder="t('ph_enter')"
                class="beta__other-text"
              />
            </label>
          </div>
        </div>
      </div>

      <div class="beta__step-title">{{ t('step_reason') }}</div>
      <div class="beta__field">
        <textarea
          v-model="form.fReason"
          rows="3"
          :placeholder="t('ph_reason')"
        ></textarea>
      </div>

      <AGButton block size="lg" :disabled="submitting" class="beta__submit" @click="submitForm">
        {{ submitting ? t('submitting') : t('submit') }}
      </AGButton>
    </AGCard>

    <div v-if="toastMsg" class="beta__toast">{{ toastMsg }}</div>

    <div v-if="success" class="beta__success-overlay">
      <div class="beta__success-box">
        <div class="check">✓</div>
        <h2>{{ t('success_title') }}</h2>
        <p>{{ t('success_body') }}</p>
        <p class="countdown">{{ t('countdown_suffix', { n: countdown }) }}</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref, watch, onMounted } from 'vue'

const G = globalThis
import AGCard from '@/components/ui/AGCard.vue'
const logoSrc = '/static/logo-64.png'
import AGButton from '@/components/ui/AGButton.vue'

const _FORM_KEY = 'beta_apply_form'

// 选项一律存 i18n key：显示走当前语言包，提交值仍取 zh 规范文本（后端契约不变）
const identities = [
  { type: 'student', icon: '🎓', key: 'id_student', descKey: 'id_student_desc' },
  { type: 'enterprise', icon: '🏢', key: 'id_enterprise', descKey: 'id_enterprise_desc' },
  { type: 'opc', icon: '💼', key: 'id_opc', descKey: 'id_opc_desc' },
  { type: 'other', icon: '📋', key: 'id_other', descKey: 'id_other_desc' },
]
const educationOpts = ['edu_bachelor', 'edu_master', 'edu_phd', 'opt_other']
const majorOpts = ['major_cs', 'major_ai', 'major_ds', 'major_ee', 'major_stem', 'major_business', 'opt_other']
const companySizeOpts = ['size_1_10', 'size_11_50', 'size_51_200', 'size_201_1000', 'size_1000_plus']
const roleOpts = ['role_dev', 'role_architect', 'role_pm', 'role_pjm', 'role_mgmt', 'opt_other']
const entUseCaseOpts = [
  'ent_uc_software',
  'ent_uc_agent',
  'ent_uc_office',
  'ent_uc_internal',
  'ent_uc_data',
  'ent_uc_ops',
  'opt_other',
]
const occupationOpts = ['occ_software', 'occ_ai', 'occ_ecommerce', 'occ_content', 'occ_saas', 'occ_consulting', 'opt_other']
const opcUseCaseOpts = [
  'opc_uc_product',
  'opc_uc_coding',
  'opc_uc_agent',
  'opc_uc_automation',
  'opc_uc_content',
  'opc_uc_data',
  'opc_uc_ops',
  'opt_other',
]
const otherUseCaseOpts = [
  'other_uc_study',
  'other_uc_work',
  'other_uc_coding',
  'other_uc_startup',
  'other_uc_content',
  'other_uc_data',
  'other_uc_automation',
  'opt_other',
]

const form = reactive({
  fAccountType: 'gitcode',
  fAccountName: '',
  fEmail: '',
  fPhone: '',
  fReason: '',
  fOtherDesc: '',
})
const identity = ref('')

// 语言包：zh 永远装载（提供提交用规范文本），当前语言用于显示
const ready = ref(false)
const pack = ref({})
const zhPack = ref({})
function t(key, vars) {
  let out = pack.value[key] || zhPack.value[key] || key
  if (vars) {
    for (const [k, v] of Object.entries(vars)) out = out.split('{' + k + '}').join(String(v))
  }
  return out
}
function v(key) {
  return zhPack.value[key] || key
}
async function loadPacks() {
  const lang = G.localStorage.getItem('grapeLang') || 'zh'
  const fetchPack = async (l) => {
    const r = await fetch('/static/locales/' + l + '/beta_apply.json')
    return r.ok ? await r.json() : {}
  }
  zhPack.value = await fetchPack('zh')
  pack.value = lang === 'zh' ? zhPack.value : await fetchPack(lang)
  ready.value = true
}
const radio = reactive({ education: '', major: '', companySize: '', role: '', occupation: '' })
const other = reactive({
  education: '',
  major: '',
  role: '',
  occupation: '',
  entUseCases: '',
  opcUseCases: '',
  otherUseCases: '',
})
const checks = reactive({ entUseCases: [], opcUseCases: [], otherUseCases: [] })
const submitting = ref(false)
const toastMsg = ref('')
const success = ref(false)
const countdown = ref(3)

function toggleCheck(group, value, ev) {
  const list = checks[group]
  const idx = list.findIndex((c) => c.v === value)
  if (ev.target.checked && idx === -1) list.push({ v: value, o: '' })
  if (!ev.target.checked && idx !== -1) list.splice(idx, 1)
}

function showToast(msg) {
  toastMsg.value = msg
  setTimeout(() => (toastMsg.value = ''), 3000)
}

// Issue #205 parity: persist to sessionStorage on every input/change
function collectFormData() {
  return {
    fAccountType: form.fAccountType,
    fAccountName: form.fAccountName,
    fEmail: form.fEmail,
    fPhone: form.fPhone,
    fReason: form.fReason,
    fOtherDesc: form.fOtherDesc,
    _identity: identity.value,
    r_education: radio.education,
    r_major: radio.major,
    r_companySize: radio.companySize,
    r_role: radio.role,
    r_occupation: radio.occupation,
    ro_education: other.education,
    ro_major: other.major,
    ro_role: other.role,
    ro_occupation: other.occupation,
    c_entUseCases: checks.entUseCases.map((c) => ({
      v: c.v,
      ...(c.v === v('opt_other') ? { o: other.entUseCases } : {}),
    })),
    c_opcUseCases: checks.opcUseCases.map((c) => ({
      v: c.v,
      ...(c.v === v('opt_other') ? { o: other.opcUseCases } : {}),
    })),
    c_otherUseCases: checks.otherUseCases.map((c) => ({
      v: c.v,
      ...(c.v === v('opt_other') ? { o: other.otherUseCases } : {}),
    })),
  }
}
function saveForm() {
  try {
    sessionStorage.setItem(_FORM_KEY, JSON.stringify(collectFormData()))
  } catch (e) {
    /* ignore */
  }
}
watch([form, identity, radio, other, checks], () => saveForm(), { deep: true })

// Issue #205 parity: restore from sessionStorage, then URL params override platform/username
onMounted(async () => {
  await loadPacks()
  try {
    const raw = sessionStorage.getItem(_FORM_KEY)
    if (raw) {
      const data = JSON.parse(raw)
      for (const k of [
        'fAccountType',
        'fAccountName',
        'fEmail',
        'fPhone',
        'fReason',
        'fOtherDesc',
      ]) {
        if (data[k] !== undefined && data[k] !== '') form[k] = data[k]
      }
      if (data._identity) identity.value = data._identity
      for (const name of ['education', 'major', 'companySize', 'role', 'occupation']) {
        if (data['r_' + name] !== undefined) radio[name] = data['r_' + name]
        if (data['ro_' + name] !== undefined) other[name] = data['ro_' + name]
      }
      for (const dn of ['entUseCases', 'opcUseCases', 'otherUseCases']) {
        if (Array.isArray(data['c_' + dn])) {
          checks[dn] = data['c_' + dn]
          const oth = data['c_' + dn].find((c) => c.v === v('opt_other'))
          if (oth && oth.o) other[dn] = oth.o
        }
      }
    }
  } catch (e) {
    /* ignore */
  }
  const params = new URLSearchParams(window.location.search)
  const platform = params.get('platform')
  const username = params.get('username')
  if (platform) form.fAccountType = platform
  if (username) form.fAccountName = username
})

function otherOr(value, otherValue, fallback) {
  if (value === v('opt_other')) return otherValue.trim() ? otherValue.trim() : fallback
  return value
}

function buildSurveyData() {
  if (identity.value === 'student') {
    const edu = otherOr(radio.education, other.education, '')
    const major = otherOr(radio.major, other.major, '')
    if (!radio.education || !radio.major || !edu || !major) return null
    return { identity: 'student', education: edu, major }
  }
  if (identity.value === 'enterprise') {
    const size = radio.companySize
    const role = otherOr(radio.role, other.role, '')
    const cases = checks.entUseCases.map((c) =>
      c.v === '其他' ? (other.entUseCases.trim() ? other.entUseCases.trim() : v('opt_other')) : c.v
    )
    if (!size || !radio.role || !role || cases.length === 0) return null
    return { identity: 'enterprise', companySize: size, role, useCases: cases }
  }
  if (identity.value === 'opc') {
    const occ = otherOr(radio.occupation, other.occupation, '')
    const cases = checks.opcUseCases.map((c) =>
      c.v === '其他' ? (other.opcUseCases.trim() ? other.opcUseCases.trim() : v('opt_other')) : c.v
    )
    if (!radio.occupation || !occ || cases.length === 0) return null
    return { identity: 'opc', occupation: occ, useCases: cases }
  }
  if (identity.value === 'other') {
    const desc = form.fOtherDesc.trim()
    const cases = checks.otherUseCases.map((c) =>
      c.v === '其他' ? (other.otherUseCases.trim() ? other.otherUseCases.trim() : v('opt_other')) : c.v
    )
    if (!desc || cases.length === 0) return null
    return { identity: 'other', description: desc, useCases: cases }
  }
  return null
}

function submitForm() {
  const accountName = form.fAccountName.trim()
  const email = form.fEmail.trim()
  if (!accountName) return showToast(t('err_username'))
  if (!email) return showToast(t('err_email'))
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return showToast(t('err_email_format'))
  if (!identity.value) return showToast(t('err_identity'))
  const surveyData = buildSurveyData()
  if (!surveyData) return showToast(t('err_required'))

  submitting.value = true
  const payload = {
    accountType: form.fAccountType,
    accountName,
    email,
    identityType: identity.value,
    phone: form.fPhone.trim(),
    reason: form.fReason.trim(),
    surveyData,
  }
  fetch('/api/settings/beta-apply/submit', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
    .then((r) => r.json())
    .then((d) => {
      if (d.success) {
        try {
          sessionStorage.removeItem(_FORM_KEY)
        } catch (e) {
          /* ignore */
        }
        success.value = true
        const timer = setInterval(() => {
          countdown.value--
          if (countdown.value <= 0) {
            clearInterval(timer)
            window.location.href = '/'
          }
        }, 1000)
      } else if (d.error) {
        showToast(d.error)
        submitting.value = false
      } else if (d.detail && d.detail.message) {
        showToast(d.detail.message)
        submitting.value = false
      } else {
        showToast(t('err_submit'))
        submitting.value = false
      }
    })
    .catch((e) => {
      showToast(t('err_network', { msg: e }))
      submitting.value = false
    })
}
</script>

<style scoped>
/* Liquid Glass twin of the legacy beta-apply page: same structure, token-driven. */
.beta {
  min-height: 100vh;
  background: var(--c-bg);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--s-5);
  color: var(--c-text);
}
.beta__card {
  width: 480px;
  max-width: 100vw;
  max-height: 95vh;
  overflow-y: auto;
  padding: var(--s-6);
}
.beta__logo {
  text-align: center;
  margin-bottom: var(--s-5);
}
.beta__logo img {
  width: 64px;
  height: 64px;
  border-radius: var(--r-card);
}
.beta__logo h1 {
  font-size: var(--f-head-2);
  font-weight: 700;
  margin-top: var(--s-2);
}
.beta__logo p {
  color: var(--c-text-2);
  font-size: var(--f-caption);
  margin-top: var(--s-1);
}
.beta__step-title {
  font-size: var(--f-body);
  font-weight: 600;
  color: var(--c-text-2);
  margin: var(--s-4) 0 var(--s-2);
  padding-bottom: var(--s-1);
  border-bottom: 1px solid var(--glass-border);
}
.beta__field {
  margin-bottom: var(--s-3);
}
.beta__field label {
  display: block;
  font-size: var(--f-caption);
  color: var(--c-text-2);
  margin-bottom: var(--s-1);
  font-weight: 500;
}
.beta__req {
  color: var(--c-danger);
}
.beta__field input,
.beta__field select,
.beta__field textarea {
  width: 100%;
  padding: var(--s-2) var(--s-3);
  border: 1px solid var(--glass-border);
  border-radius: var(--r-control);
  font-size: var(--f-body);
  background: var(--glass-bg-strong);
  color: var(--c-text);
  outline: none;
  transition: border-color 0.2s;
  font-family: inherit;
}
.beta__field input:focus,
.beta__field select:focus,
.beta__field textarea:focus {
  border-color: var(--c-accent);
}
.beta__field textarea {
  resize: vertical;
  min-height: 60px;
}
.beta__id-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--s-2);
}
.beta__id-card {
  display: flex;
  align-items: center;
  gap: var(--s-2);
  padding: var(--s-3);
  border: 2px solid var(--glass-border);
  border-radius: var(--r-control);
  cursor: pointer;
  transition: all 0.15s;
  background: var(--glass-bg-strong);
}
.beta__id-card:hover {
  border-color: var(--c-accent-soft);
}
.beta__id-card.selected {
  border-color: var(--c-accent);
  background: var(--c-accent-soft);
}
.beta__id-card .icon {
  font-size: 22px;
  flex-shrink: 0;
}
.beta__id-card .title {
  font-size: var(--f-body);
  font-weight: 600;
}
.beta__id-card .desc {
  font-size: var(--f-caption);
  color: var(--c-text-2);
}
.beta__radio-group {
  display: flex;
  flex-direction: column;
  gap: var(--s-2);
}
.beta__radio-item,
.beta__checkbox-item {
  display: inline-flex;
  align-items: center;
  gap: var(--s-2);
  padding: var(--s-2) var(--s-3);
  border: 1px solid var(--glass-border);
  border-radius: var(--r-control);
  cursor: pointer;
  font-size: var(--f-body);
  transition: all 0.15s;
}
.beta__radio-item:hover,
.beta__checkbox-item:hover {
  border-color: var(--c-accent-soft);
}
.beta__radio-item.selected,
.beta__checkbox-item.selected {
  border-color: var(--c-accent);
  background: var(--c-accent-soft);
}
.beta__checkbox-group {
  display: flex;
  flex-wrap: wrap;
  gap: var(--s-2);
}
.beta__other-text {
  width: 180px !important;
  padding: var(--s-1) var(--s-2) !important;
  font-size: var(--f-body);
  margin-left: var(--s-1);
}
.beta__submit {
  margin-top: var(--s-3);
}
.beta__toast {
  position: fixed;
  bottom: var(--s-6);
  left: 50%;
  transform: translateX(-50%);
  background: var(--c-text);
  color: var(--c-bg);
  padding: var(--s-3) var(--s-5);
  border-radius: var(--r-control);
  font-size: var(--f-body);
  z-index: 9999;
  box-shadow: var(--shadow-2);
}
.beta__success-overlay {
  position: fixed;
  inset: 0;
  z-index: 10000;
  background: rgba(0, 0, 0, 0.35);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  display: flex;
  align-items: center;
  justify-content: center;
}
.beta__success-box {
  background: var(--glass-bg-strong);
  backdrop-filter: blur(var(--glass-blur));
  -webkit-backdrop-filter: blur(var(--glass-blur));
  border-radius: var(--r-card);
  padding: var(--s-6);
  text-align: center;
  width: 360px;
  max-width: 90vw;
  box-shadow: var(--shadow-2);
}
.beta__success-box .check {
  width: 64px;
  height: 64px;
  border-radius: 50%;
  background: var(--c-success);
  margin: 0 auto var(--s-4);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 32px;
  color: #fff;
}
.beta__success-box h2 {
  font-size: var(--f-head-2);
  margin-bottom: var(--s-2);
}
.beta__success-box p {
  font-size: var(--f-body);
  color: var(--c-text-2);
  margin-bottom: var(--s-3);
}
.beta__success-box .countdown {
  font-size: var(--f-caption);
  color: var(--c-text-3);
}
</style>
