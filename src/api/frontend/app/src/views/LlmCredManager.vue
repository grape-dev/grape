<template>
  <div class="cd">
    <div class="cd-h" style="justify-content: space-between">
      <h2>{{ t('title') }}</h2>
      <button class="bt bt-p" @click="openAdd">{{ t('add_btn') }}</button>
    </div>
    <p class="tx t2" style="font-size: 11px; line-height: 1.6; margin-bottom: 12px">{{ t('desc') }}</p>
    <div v-if="loading" class="loading">…</div>
    <div v-else-if="error" style="color: var(--r); font-size: 12px">{{ error }}</div>
    <div v-else-if="!creds.length" class="tx t2" style="padding: 16px; text-align: center">{{ t('empty') }}</div>
    <div v-else>
      <div v-for="c in creds" :key="c.id" style="display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--b)">
        <div style="flex: 1; min-width: 0">
          <div style="font-size: 12px; font-weight: 600; color: var(--t)">
            {{ c.provider || 'custom' }}
            <span
              style="display: inline-block; font-size: 10px; font-weight: 500; padding: 1px 7px; border-radius: 10px; margin-left: 6px"
              :style="{ color: (c.engine_type || 'common') === 'common' ? '#86868b' : '#0071e3', background: (c.engine_type || 'common') === 'common' ? '#f1f1f4' : '#e3f0ff' }"
              >{{ (c.engine_type || 'common') === 'common' ? (isZh ? '通用' : 'Common') : c.engine_type }}</span
            >
            <span v-if="!c.enabled" style="font-size: 10px; color: var(--t3); margin-left: 6px">({{ isZh ? '已禁用' : 'disabled' }})</span>
          </div>
          <div style="font-size: 11px; color: var(--t3); margin-top: 2px">{{ c.base_url || '—' }} · {{ c.model || '—' }}</div>
          <div style="font-size: 10px; color: var(--t3); margin-top: 2px">{{ t('updated_at') }}: {{ c.updated_at || '—' }}</div>
        </div>
        <button v-if="c.enabled" class="bt bt-o bt-s" @click="toggle(c.id, false)">{{ t('action.disable') }}</button>
        <button v-else class="bt bt-o bt-s" style="color: var(--g); border-color: var(--g)" @click="toggle(c.id, true)">{{ t('action.enable') }}</button>
        <button class="bt bt-o bt-s" @click="openEdit(c)">{{ t('action.edit') }}</button>
        <button class="bt bt-r bt-s" @click="remove(c.id)">{{ t('action.delete') }}</button>
      </div>
    </div>

    <!-- 添加/编辑弹窗 -->
    <div
      v-if="dlg.open"
      style="position: fixed; inset: 0; background: rgba(0, 0, 0, 0.5); display: flex; align-items: center; justify-content: center; z-index: 9999"
      @click.self="dlg.open = false"
    >
      <div style="background: var(--c); border-radius: 14px; padding: 24px; max-width: 480px; width: 90%; box-shadow: 0 8px 32px rgba(0, 0, 0, 0.15)">
        <h3 style="font-size: 15px; font-weight: 600; margin-bottom: 16px">{{ dlg.id ? t('dialog.edit_title') : t('dialog.add_title') }}</h3>
        <div style="margin-bottom: 12px">
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">{{ t('dialog.engine_type') }}</label>
          <select v-model="dlg.engineType" style="width: 100%; padding: 8px 12px; border: 1px solid var(--b); border-radius: 8px; font-size: 12px; background: var(--c)">
            <option v-for="et in engineTypes" :key="et.value" :value="et.value">{{ et.label || et.value }}</option>
          </select>
          <p style="font-size: 10px; color: var(--t3); margin-top: 4px; line-height: 1.4">{{ t('dialog.engine_type_desc') }}</p>
        </div>
        <div style="margin-bottom: 12px">
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">{{ t('dialog.provider') }}</label>
          <select v-model="dlg.provider" style="width: 100%; padding: 8px 12px; border: 1px solid var(--b); border-radius: 8px; font-size: 12px; background: var(--c)">
            <option v-for="k in presetKeys" :key="k" :value="k">{{ k }}</option>
          </select>
          <p style="font-size: 10px; color: var(--t3); margin-top: 4px; line-height: 1.4">{{ presets[dlg.provider] ? presets[dlg.provider].desc : '' }}</p>
        </div>
        <div style="margin-bottom: 12px">
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px"
            >{{ t('dialog.api_key') }}{{ dlg.id ? ' (' + t('dialog.api_key_keep_empty') + ')' : '' }}</label
          >
          <input
            v-model="dlg.apiKey"
            type="password"
            :placeholder="dlg.id ? t('dialog.api_key_keep_empty') : 'sk-...'"
            autocomplete="new-password"
            style="width: 100%; padding: 8px 12px; border: 1px solid var(--b); border-radius: 8px; font-size: 12px"
          />
        </div>
        <div style="margin-bottom: 12px">
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">{{ t('dialog.base_url') }}</label>
          <input
            v-model="dlg.baseUrl"
            type="text"
            placeholder="https://api.openai.com/v1"
            style="width: 100%; padding: 8px 12px; border: 1px solid var(--b); border-radius: 8px; font-size: 12px; color: var(--t)"
          />
        </div>
        <div style="margin-bottom: 16px">
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">{{ t('dialog.model') }}</label>
          <input
            v-model="dlg.model"
            type="text"
            :placeholder="isZh ? '单个模型名，如 gpt-4o / glm-4' : 'single model name, e.g. gpt-4o'"
            :title="isZh ? '仅支持单个模型名——引擎按此调用 LLM，多个候选请分别保存凭据' : 'Single model name only; save separate credentials for alternates'"
            style="width: 100%; padding: 8px 12px; border: 1px solid var(--b); border-radius: 8px; font-size: 12px; color: var(--t)"
          />
        </div>
        <div style="display: flex; gap: 8px; justify-content: flex-end">
          <button class="bt bt-o" style="padding: 8px 16px; font-size: 12px" @click="dlg.open = false">{{ t('dialog.cancel') }}</button>
          <button class="bt bt-p" style="padding: 8px 16px; font-size: 12px" @click="save">{{ t('dialog.save') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
// LLM 凭据管理（B4 移植，legacy account.js _llm* 族 + settings.js _repoLlm* 族）。
// 双模式共享组件：mode='user'（用户设置 tab，/settings/llm-credentials）；
// mode='repo'（仓库设置子页签，/settings/llm-credentials/repo?repo_key=…）。
// 文案走 shell locales sandbox.llm.* 词条。
import { ref, reactive, computed, onMounted, watch } from 'vue'

const G = globalThis

export default {
  props: {
    mode: { type: String, default: 'user' }, // 'user' | 'repo'
    repoKey: { type: String, default: '' }, // mode=repo 必填
  },
  setup(props) {
    const t = (key, vars) => {
      const v = G.I18N.t('sandbox.llm.' + key, vars)
      return v && v !== 'sandbox.llm.' + key ? v : key
    }
    const isZh = computed(() => !!(G.I18N && G.I18N.getLang && G.I18N.getLang() === 'zh'))
    const creds = ref([])
    const loading = ref(false)
    const error = ref('')
    const engineTypes = ref([])
    const dlg = reactive({ open: false, id: null, provider: 'custom', apiKey: '', baseUrl: '', model: '', engineType: 'common' })

    const _presets = (zh) => ({
      custom: { url: '', model: '', desc: zh ? '自定义兼容 OpenAI API 的厂商' : 'Custom OpenAI-compatible provider' },
      openai: { url: 'https://api.openai.com/v1', model: 'gpt-4o', desc: zh ? 'OpenAI 官方 API' : 'OpenAI official API' },
      deepseek: { url: 'https://api.deepseek.com/v1', model: 'deepseek-chat', desc: zh ? 'DeepSeek（深度求索）' : 'DeepSeek' },
      zhipu: { url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4', desc: zh ? '智谱 GLM 系列' : 'Zhipu GLM series' },
      moonshot: { url: 'https://api.moonshot.cn/v1', model: 'moonshot-v1-8k', desc: zh ? 'Moonshot AI（月之暗面）' : 'Moonshot AI' },
      qwen: { url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus', desc: zh ? '通义千问（阿里云）' : 'Tongyi Qianwen (Alibaba)' },
      azure: { url: '', model: '', desc: zh ? 'Azure OpenAI（需填完整 deployment URL）' : 'Azure OpenAI (fill full deployment URL)' },
    })
    const presets = computed(() => _presets(isZh.value))
    const presetKeys = Object.keys(_presets(true))

    async function loadEngineTypes() {
      if (props.mode !== 'user') {
        // 仓库级弹窗（legacy _repoLlmShowDialog）：引擎类型固定三档
        engineTypes.value = [
          { value: 'common', label: isZh.value ? '通用' : 'Common' },
          { value: 'hermes', label: 'Hermes' },
          { value: 'opencode', label: 'OpenCode' },
        ]
        return
      }
      try {
        const d = await G.API.get('/settings/llm-engine-types')
        const list = (d && d.engine_types) || []
        engineTypes.value = list.length
          ? list
          : [
              { value: 'common', label: isZh.value ? '通用' : 'Common' },
              { value: 'hermes', label: 'Hermes' },
              { value: 'opencode', label: 'OpenCode' },
            ]
      } catch (e) {
        engineTypes.value = [
          { value: 'common', label: isZh.value ? '通用' : 'Common' },
          { value: 'hermes', label: 'Hermes' },
          { value: 'opencode', label: 'OpenCode' },
        ]
      }
    }

    async function load() {
      loading.value = true
      error.value = ''
      try {
        const url = props.mode === 'repo' ? '/settings/llm-credentials/repo?repo_key=' + encodeURIComponent(props.repoKey) : '/settings/llm-credentials'
        const d = await G.API.get(url)
        creds.value = d.credentials || []
      } catch (e) {
        error.value = (isZh.value ? '加载失败: ' : 'Load failed: ') + (e.message || '')
        creds.value = []
      }
      loading.value = false
    }

    function openAdd() {
      const p = presets.value.custom
      dlg.id = null
      dlg.provider = 'custom'
      dlg.apiKey = ''
      dlg.baseUrl = p.url
      dlg.model = p.model
      dlg.engineType = 'common'
      dlg.open = true
    }
    function openEdit(c) {
      dlg.id = c.id
      dlg.provider = c.provider || 'custom'
      dlg.apiKey = ''
      dlg.baseUrl = c.base_url || ''
      dlg.model = c.model || ''
      dlg.engineType = c.engine_type || 'common'
      dlg.open = true
    }
    // 厂商切换联动 base_url/model 预填（legacy _llmProviderChange；编辑态不覆盖用户已改值——
    // openEdit 已带入原值，这里只在新增语义下预填：以 open 时的初值对比判断）
    let _openedWith = null
    watch(
      () => dlg.open,
      (v) => {
        if (v) _openedWith = { baseUrl: dlg.baseUrl, model: dlg.model }
      }
    )
    watch(
      () => dlg.provider,
      (nv, ov) => {
        if (!dlg.open || nv === ov) return
        const p = presets.value[nv] || presets.value.custom
        // 新增态（无 id）：直接预填；编辑态：仅当用户未手改 open 时的值才跟随
        if (!dlg.id || dlg.baseUrl === _openedWith.baseUrl) dlg.baseUrl = p.url
        if (!dlg.id || dlg.model === _openedWith.model) dlg.model = p.model
      }
    )

    async function save() {
      const model = (dlg.model || '').trim()
      // [model 单值校验 2026-10-30 seq#6] 多值列表整串传引擎 → 404 Model not found
      if (/[,;\s]/.test(model)) {
        G.app.showToast(t('toast.model_single'), 'warning')
        return
      }
      if (!dlg.id && !dlg.apiKey.trim()) {
        G.app.showToast(t('toast.api_key_required'))
        return
      }
      let body = {
        provider: dlg.provider,
        base_url: (dlg.baseUrl || '').trim(),
        model: model,
        engine_type: dlg.engineType,
      }
      if (dlg.apiKey.trim()) body.api_key = dlg.apiKey.trim()
      if (props.mode === 'repo') {
        if (!props.repoKey) return
        body.repo_key = props.repoKey
      }
      try {
        await G.API.post(props.mode === 'repo' ? '/settings/llm-credentials/repo' : '/settings/llm-credentials', body)
        G.app.showToast(t('toast.saved'))
        dlg.open = false
        load()
      } catch (e) {
        G.app.showToast(t('toast.save_failed') + ': ' + (e.message || ''))
      }
    }

    async function toggle(id, enabled) {
      try {
        const base = props.mode === 'repo' ? '/settings/llm-credentials/repo/' : '/settings/llm-credentials/'
        await G.API.put(base + id + '/toggle', { enabled: enabled })
        G.app.showToast(enabled ? t('toast.enabled') : t('toast.disabled'))
        load()
      } catch (e) {
        G.app.showToast(t('toast.toggle_failed') + ': ' + (e.message || ''))
      }
    }

    function remove(id) {
      G.app.showConfirm(t('confirm_delete'), async function () {
        try {
          const base = props.mode === 'repo' ? '/settings/llm-credentials/repo/' : '/settings/llm-credentials/'
          await G.API.del(base + id)
          G.app.showToast(t('toast.deleted'))
          load()
        } catch (e) {
          G.app.showToast(t('toast.delete_failed') + ': ' + (e.message || ''))
        }
      }, { danger: true })
    }

    onMounted(function () {
      loadEngineTypes()
      load()
    })
    // 仓库切换时重新加载（repo 模式）
    watch(
      () => props.repoKey,
      function () {
        if (props.mode === 'repo' && props.repoKey) load()
      }
    )

    return { t, isZh, creds, loading, error, engineTypes, dlg, presets, presetKeys, openAdd, openEdit, save, toggle, remove }
  },
}
</script>
