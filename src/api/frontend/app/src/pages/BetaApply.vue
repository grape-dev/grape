<template>
  <div class="beta">
    <AGCard class="beta__card">
      <div class="beta__logo">
        <img :src="logoSrc" alt="Grape" />
        <h1>申请内测</h1>
        <p>加入 Grape 早期体验计划</p>
      </div>

      <div class="beta__step-title">基础信息</div>
      <div class="beta__field">
        <label>平台 <span class="beta__req">*</span></label>
        <select v-model="form.fAccountType">
          <option value="gitcode">GitCode</option>
          <option value="github">GitHub</option>
        </select>
      </div>
      <div class="beta__field">
        <label>用户名 <span class="beta__req">*</span></label>
        <input v-model="form.fAccountName" type="text" placeholder="GitCode 或 GitHub 用户名" />
      </div>
      <div class="beta__field">
        <label>邮箱 <span class="beta__req">*</span></label>
        <input v-model="form.fEmail" type="email" placeholder="user@example.com" />
      </div>
      <div class="beta__field">
        <label>手机号</label>
        <input v-model="form.fPhone" type="text" placeholder="选填" />
      </div>

      <div class="beta__step-title">身份选择</div>
      <div class="beta__field">
        <label>您的身份是？ <span class="beta__req">*</span></label>
        <div class="beta__id-grid">
          <div
            v-for="id in identities"
            :key="id.type"
            :class="['beta__id-card', { selected: identity === id.type }]"
            @click="identity = id.type"
          >
            <span class="icon">{{ id.icon }}</span>
            <div class="info">
              <div class="title">{{ id.title }}</div>
              <div class="desc">{{ id.desc }}</div>
            </div>
          </div>
        </div>
      </div>

      <!-- student branch -->
      <div v-if="identity === 'student'" class="beta__branch">
        <div class="beta__field">
          <label>您目前的学历是？ <span class="beta__req">*</span></label>
          <div class="beta__radio-group">
            <label
              v-for="opt in educationOpts"
              :key="opt"
              :class="['beta__radio-item', { selected: radio.education === opt }]"
            >
              <input v-model="radio.education" type="radio" :value="opt" style="display: none" />
              {{ opt }}
              <input
                v-if="opt === '其他'"
                v-model="other.education"
                type="text"
                placeholder="请输入"
                class="beta__other-text"
              />
            </label>
          </div>
        </div>
        <div class="beta__field">
          <label>您的专业方向是？ <span class="beta__req">*</span></label>
          <div class="beta__radio-group">
            <label
              v-for="opt in majorOpts"
              :key="opt"
              :class="['beta__radio-item', { selected: radio.major === opt }]"
            >
              <input v-model="radio.major" type="radio" :value="opt" style="display: none" />
              {{ opt }}
              <input
                v-if="opt === '其他'"
                v-model="other.major"
                type="text"
                placeholder="请输入"
                class="beta__other-text"
              />
            </label>
          </div>
        </div>
      </div>

      <!-- enterprise branch -->
      <div v-if="identity === 'enterprise'" class="beta__branch">
        <div class="beta__field">
          <label>您所在企业的规模？ <span class="beta__req">*</span></label>
          <div class="beta__radio-group">
            <label
              v-for="opt in companySizeOpts"
              :key="opt"
              :class="['beta__radio-item', { selected: radio.companySize === opt }]"
            >
              <input v-model="radio.companySize" type="radio" :value="opt" style="display: none" />
              {{ opt }}
            </label>
          </div>
        </div>
        <div class="beta__field">
          <label>您在企业中的角色？ <span class="beta__req">*</span></label>
          <div class="beta__radio-group">
            <label
              v-for="opt in roleOpts"
              :key="opt"
              :class="['beta__radio-item', { selected: radio.role === opt }]"
            >
              <input v-model="radio.role" type="radio" :value="opt" style="display: none" />
              {{ opt }}
              <input
                v-if="opt === '其他'"
                v-model="other.role"
                type="text"
                placeholder="请输入"
                class="beta__other-text"
              />
            </label>
          </div>
        </div>
        <div class="beta__field">
          <label>您计划将产品用于什么场景？ <span class="beta__req">*</span>（多选）</label>
          <div class="beta__checkbox-group">
            <label
              v-for="opt in entUseCaseOpts"
              :key="opt"
              :class="[
                'beta__checkbox-item',
                { selected: checks.entUseCases.some((c) => c.v === opt) },
              ]"
            >
              <input
                type="checkbox"
                ::value="opt"
                :checked="checks.entUseCases.some((c) => c.v === opt)"
                style="display: none"
                @change="toggleCheck('entUseCases', opt, $event)"
              />
              {{ opt }}
              <input
                v-if="opt === '其他'"
                v-model="other.entUseCases"
                type="text"
                placeholder="请输入"
                class="beta__other-text"
              />
            </label>
          </div>
        </div>
      </div>

      <!-- opc branch -->
      <div v-if="identity === 'opc'" class="beta__branch">
        <div class="beta__field">
          <label>您目前主要从事什么？ <span class="beta__req">*</span></label>
          <div class="beta__radio-group">
            <label
              v-for="opt in occupationOpts"
              :key="opt"
              :class="['beta__radio-item', { selected: radio.occupation === opt }]"
            >
              <input v-model="radio.occupation" type="radio" :value="opt" style="display: none" />
              {{ opt }}
              <input
                v-if="opt === '其他'"
                v-model="other.occupation"
                type="text"
                placeholder="请输入"
                class="beta__other-text"
              />
            </label>
          </div>
        </div>
        <div class="beta__field">
          <label>您目前主要使用产品解决什么问题？ <span class="beta__req">*</span>（多选）</label>
          <div class="beta__checkbox-group">
            <label
              v-for="opt in opcUseCaseOpts"
              :key="opt"
              :class="[
                'beta__checkbox-item',
                { selected: checks.opcUseCases.some((c) => c.v === opt) },
              ]"
            >
              <input
                type="checkbox"
                ::value="opt"
                :checked="checks.opcUseCases.some((c) => c.v === opt)"
                style="display: none"
                @change="toggleCheck('opcUseCases', opt, $event)"
              />
              {{ opt }}
              <input
                v-if="opt === '其他'"
                v-model="other.opcUseCases"
                type="text"
                placeholder="请输入"
                class="beta__other-text"
              />
            </label>
          </div>
        </div>
      </div>

      <!-- other branch -->
      <div v-if="identity === 'other'" class="beta__branch">
        <div class="beta__field">
          <label>请简单描述您的身份/职业 <span class="beta__req">*</span></label>
          <input v-model="form.fOtherDesc" type="text" placeholder="如：自由职业者、研究者等" />
        </div>
        <div class="beta__field">
          <label
            >您目前主要使用 AI 产品解决什么问题？ <span class="beta__req">*</span>（多选）</label
          >
          <div class="beta__checkbox-group">
            <label
              v-for="opt in otherUseCaseOpts"
              :key="opt"
              :class="[
                'beta__checkbox-item',
                { selected: checks.otherUseCases.some((c) => c.v === opt) },
              ]"
            >
              <input
                type="checkbox"
                ::value="opt"
                :checked="checks.otherUseCases.some((c) => c.v === opt)"
                style="display: none"
                @change="toggleCheck('otherUseCases', opt, $event)"
              />
              {{ opt }}
              <input
                v-if="opt === '其他'"
                v-model="other.otherUseCases"
                type="text"
                placeholder="请输入"
                class="beta__other-text"
              />
            </label>
          </div>
        </div>
      </div>

      <div class="beta__step-title">申请理由</div>
      <div class="beta__field">
        <textarea
          v-model="form.fReason"
          rows="3"
          placeholder="选填，告诉我们您为什么想加入内测"
        ></textarea>
      </div>

      <AGButton block size="lg" :disabled="submitting" class="beta__submit" @click="submitForm">
        {{ submitting ? '提交中...' : '提交申请' }}
      </AGButton>
    </AGCard>

    <div v-if="toastMsg" class="beta__toast">{{ toastMsg }}</div>

    <div v-if="success" class="beta__success-overlay">
      <div class="beta__success-box">
        <div class="check">✓</div>
        <h2>申请已提交</h2>
        <p>请等待审核，我们将在 1-3 个工作日内通过邮箱回复您</p>
        <p class="countdown">{{ countdown }} 秒后跳转到主页...</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref, watch, onMounted } from 'vue'
import AGCard from '@/components/ui/AGCard.vue'
const logoSrc = '/static/logo-64.png'
import AGButton from '@/components/ui/AGButton.vue'

const _FORM_KEY = 'beta_apply_form'

const identities = [
  { type: 'student', icon: '🎓', title: '学生', desc: '在校学生用户' },
  { type: 'enterprise', icon: '🏢', title: '企业', desc: '企业团队用户' },
  { type: 'opc', icon: '💼', title: 'OPC', desc: '独立开发者/创作者' },
  { type: 'other', icon: '📋', title: '其他', desc: '其他身份' },
]
const educationOpts = ['本科生', '硕士研究生', '博士研究生', '其他']
const majorOpts = [
  '计算机/软件工程',
  '人工智能',
  '数据科学',
  '电子/自动化',
  '其他理工科',
  '经管/商科',
  '其他',
]
const companySizeOpts = ['1-10人', '11-50人', '51-200人', '201-1000人', '1000人以上']
const roleOpts = ['开发人员', '技术负责人/架构师', '产品经理', '项目经理', '管理人员', '其他']
const entUseCaseOpts = [
  '软件开发',
  'AI Agent / Agent 开发',
  '自动化办公',
  '企业内部工具',
  '数据分析',
  '运维 / DevOps',
  '其他',
]
const occupationOpts = ['软件/IT', 'AI/Agent', '电商', '内容创作', 'SaaS', '咨询/服务', '其他']
const opcUseCaseOpts = [
  '产品开发',
  '编程开发',
  'Agent 开发',
  '自动化任务',
  '内容生产',
  '数据处理/分析',
  '运营',
  '其他',
]
const otherUseCaseOpts = ['学习', '工作', '编程', '创业', '内容创作', '数据分析', '自动化', '其他']

const form = reactive({
  fAccountType: 'gitcode',
  fAccountName: '',
  fEmail: '',
  fPhone: '',
  fReason: '',
  fOtherDesc: '',
})
const identity = ref('')
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
      ...(c.v === '其他' ? { o: other.entUseCases } : {}),
    })),
    c_opcUseCases: checks.opcUseCases.map((c) => ({
      v: c.v,
      ...(c.v === '其他' ? { o: other.opcUseCases } : {}),
    })),
    c_otherUseCases: checks.otherUseCases.map((c) => ({
      v: c.v,
      ...(c.v === '其他' ? { o: other.otherUseCases } : {}),
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
onMounted(() => {
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
          const oth = data['c_' + dn].find((c) => c.v === '其他')
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
  if (value === '其他') return otherValue.trim() ? otherValue.trim() : fallback
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
      c.v === '其他' ? (other.entUseCases.trim() ? other.entUseCases.trim() : '其他') : c.v
    )
    if (!size || !radio.role || !role || cases.length === 0) return null
    return { identity: 'enterprise', companySize: size, role, useCases: cases }
  }
  if (identity.value === 'opc') {
    const occ = otherOr(radio.occupation, other.occupation, '')
    const cases = checks.opcUseCases.map((c) =>
      c.v === '其他' ? (other.opcUseCases.trim() ? other.opcUseCases.trim() : '其他') : c.v
    )
    if (!radio.occupation || !occ || cases.length === 0) return null
    return { identity: 'opc', occupation: occ, useCases: cases }
  }
  if (identity.value === 'other') {
    const desc = form.fOtherDesc.trim()
    const cases = checks.otherUseCases.map((c) =>
      c.v === '其他' ? (other.otherUseCases.trim() ? other.otherUseCases.trim() : '其他') : c.v
    )
    if (!desc || cases.length === 0) return null
    return { identity: 'other', description: desc, useCases: cases }
  }
  return null
}

function submitForm() {
  const accountName = form.fAccountName.trim()
  const email = form.fEmail.trim()
  if (!accountName) return showToast('请填写用户名')
  if (!email) return showToast('请填写邮箱')
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return showToast('邮箱格式不正确')
  if (!identity.value) return showToast('请选择身份')
  const surveyData = buildSurveyData()
  if (!surveyData) return showToast('请完成身份相关的必填问题')

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
        showToast('提交失败，请稍后重试')
        submitting.value = false
      }
    })
    .catch((e) => {
      showToast('网络错误: ' + e)
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
