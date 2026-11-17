<template>
  <div style="padding: 0" :data-langver="langVer">
    <!-- ============ Provider 详情页 ============ -->
    <div v-if="activeTab === 'providers' && view === 'detail' && selectedProvider">
      <!-- Provider 信息卡片 -->
      <div class="apple-card" style="padding: 16px; margin-bottom: 16px">
        <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px; flex-wrap: wrap">
          <h2 style="font-size: 16px; font-weight: 600">
            {{ selectedProvider.name }}
            <span
              v-if="selectedProvider.credential_name"
              style="font-size: 10px; font-weight: 400; color: #6d28d9; background: #ede9fe; border-radius: 999px; padding: 1px 8px; margin-left: 8px"
              :title="t('sbx.cred_from_title', { name: selectedProvider.credential_name })"
            >{{ t('sbx.cred_prefix') }}{{ selectedProvider.credential_name }}</span>
            <span
              v-else
              style="font-size: 10px; font-weight: 400; color: var(--t3); margin-left: 8px"
              :title="t('sbx.cred_manual_title')"
            >{{ t('sbx.cred_manual') }}</span>
          </h2>
          <span v-if="!selectedProvider.enabled" style="font-size: 10px; padding: 2px 6px; border-radius: 4px; background: #fee2e2; color: #dc2626">{{ t('sbx.badge_disabled') }}</span>
          <span style="margin-left: auto; display: inline-flex; gap: 6px">
            <button
              class="bt bt-o bt-s"
              :title="t('sbx.provider_edit_tip')"
              @click="openEditProvider(selectedProvider)"
            >✏ {{ t('sbx.edit') }}</button>
            <button
              class="bt bt-o bt-s"
              :title="t('sbx.provider_check_tip')"
              @click="healthCheck(selectedProvider)"
            >🩺 {{ t('sbx.check') }}</button>
            <button
              class="bt bt-o bt-s"
              :title="selectedProvider.enabled ? (t('sbx.provider_disable_tip')) : (t('sbx.provider_enable_tip'))"
              @click="toggleProvider(selectedProvider)"
            >{{ selectedProvider.enabled ? '⏸ ' + (t('sbx.disable')) : '▶ ' + (t('sbx.enable')) }}</button>
          </span>
        </div>
        <!-- 健康检查结果位 -->
        <div
          v-if="healthResults[selectedProvider.id]"
          style="font-size: 11px; margin-bottom: 10px; padding: 5px 10px; border-radius: 6px"
          :style="healthResults[selectedProvider.id].loading ? 'background:var(--bg);color:var(--t3)' : (healthResults[selectedProvider.id].healthy ? 'background:#dcfce7;color:#166534' : 'background:#fee2e2;color:#dc2626')"
        >
          {{ healthResults[selectedProvider.id].loading ? (t('sbx.checking')) : (healthResults[selectedProvider.id].healthy ? (t('sbx.reachable')) : '✗ ' + (healthResults[selectedProvider.id].reason || (t('sbx.unreachable')))) }}
        </div>
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 8px; font-size: 12px">
          <div :title="selectedProvider.platform"><span style="color: var(--t3)">Platform:</span> {{ selectedProvider.platform }}</div>
          <div :title="selectedProvider.endpoint"><span style="color: var(--t3)">API URL:</span> <span class="mono">{{ truncate(selectedProvider.endpoint, 30) }}</span></div>
          <div :title="selectedProvider.scope"><span style="color: var(--t3)">Scope:</span> {{ selectedProvider.scope === 'global' ? (t('sbx.scope_global')) : (selectedProvider.scope === 'repo' ? (t('sbx.scope_repo')) : (t('sbx.scope_user'))) }}</div>
          <div v-if="selectedProvider.scope === 'repo' && selectedProvider.repo_key" :title="selectedProvider.repo_key"><span style="color: var(--t3)">{{ t('sbx.repo_col') }}:</span> {{ truncate(selectedProvider.repo_key, 30) }}</div>
          <div :title="String(selectedProvider.max_sandboxes || 10)"><span style="color: var(--t3)">Max Sandboxes:</span> {{ selectedProvider.max_sandboxes || 10 }}</div>
          <!-- 动态展示平台特有字段 -->
          <template v-for="group in getProviderSchema(selectedProvider.platform).groups" :key="group.title">
            <div v-for="field in group.fields" :key="field.name">
              <span style="color: var(--t3)">{{ field.label }}:</span>
              {{ formatExtra(selectedProvider, field) }}
            </div>
          </template>
        </div>
        <div style="margin-top: 10px">
          <button class="bt bt-o bt-s" style="font-size: 10px" @click="viewSandboxes(selectedProvider.id)">📦 {{ t('sbx.view_sandboxes') }}</button>
        </div>
      </div>

      <!-- 模板列表 -->
      <div class="apple-card" style="padding: 16px">
        <div class="cd-h" style="justify-content: space-between; margin-bottom: 12px">
          <h2 style="font-size: 14px; font-weight: 600">📋 {{ t('sbx.engine_templates') }}</h2>
          <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap">
            <div
              style="padding: 4px 10px; border-radius: 6px; font-size: 11px"
              :style="llmCredReady === false ? 'background:#fef3c7;color:#92400e' : 'background:var(--bg);color:var(--t3)'"
              :title="t('sbx.llm_route_hint')"
            >
              <template>🤖 {{ t('sbx.llm_from') }}{{ llmCredSourceLabel }}{{ llmCredReady === false ? t('sbx.llm_not_ready') : '' }}<a v-if="llmCredReady === false" :href="(selectedProvider.scope === 'user') ? '#account/llm' : '#settings/llm'" style="color: var(--a); margin-left: 6px" :title="t('sbx.go_config')">{{ t('sbx.go_config') }} ↗</a></template>
            </div>
            <button
              v-if="scanCapable !== false"
              class="bt bt-o bt-s"
              style="font-size: 10px"
              :disabled="scanningTemplates"
              :title="t('sbx.scan_tip')"
              @click="scanTemplates"
            >{{ scanningTemplates ? (t('sbx.scanning')) : '🔍 ' + (t('sbx.scan_btn')) }}</button>
            <button
              v-else
              class="bt bt-o bt-s"
              style="font-size: 10px"
              disabled
              :title="t('sbx.scan_unsupported')"
            >🔍 {{ t('sbx.scan_btn') }}</button>
          </div>
        </div>
        <div v-if="templateLoading" class="tx t2" style="padding: 16px; text-align: center">{{ t('sbx.loading') }}</div>
        <div v-else-if="templates.length === 0" class="tx t2" style="padding: 16px; text-align: center">{{ t('empty.no_templates') }}</div>
        <div v-else style="overflow-x: auto">
          <table style="width: 100%; font-size: 11px; border-collapse: collapse">
            <thead>
              <tr style="border-bottom: 2px solid var(--b); text-align: left">
                <th style="padding: 8px">{{ t('sbx.name') }}</th>
                <th style="padding: 8px">Template ID</th>
                <th style="padding: 8px">Alias</th>
                <th style="padding: 8px">{{ t('sbx.description') }}</th>
                <th style="padding: 8px">{{ t('sbx.engines') }}</th>
                <th style="padding: 8px">Preinstalled</th>
                <th style="padding: 8px">Disk MB</th>
                <th style="padding: 8px">{{ t('sbx.status_enabled') }}</th>
                <th style="padding: 8px">Last Check</th>
                <th style="padding: 8px">{{ t('sbx.actions') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="tpl in templates" :key="tpl.id" style="border-bottom: 1px solid var(--b)">
                <td style="padding: 8px; font-weight: 600" :title="tpl.name">{{ tpl.name }} <span style="font-size: 9px; color: var(--t3); font-weight: 400" :title="t('sbx.tpl_dbid_tip')">#{{ tpl.id }}</span></td>
                <td style="padding: 8px; font-family: monospace; font-size: 10px" :title="tpl.template_id">{{ truncate(tpl.template_id, 18) }}</td>
                <td style="padding: 8px" :title="tpl.alias || '—'">{{ tpl.alias || '—' }}</td>
                <td style="padding: 8px; font-size: 10px; max-width: 120px; overflow: hidden; text-overflow: ellipsis" :title="tpl.description">{{ truncate(tpl.description, 20) }}</td>
                <td style="padding: 8px">
                  <span v-if="parseEngines(tpl) !== '—'" style="padding: 2px 6px; border-radius: 4px; background: #eef; border: 1px solid #ccd; font-size: 10px">{{ parseEngines(tpl) }}</span>
                  <span v-else style="color: var(--t3); font-size: 10px">—</span>
                </td>
                <td style="padding: 8px; font-size: 10px; max-width: 100px; overflow: hidden; text-overflow: ellipsis" :title="tpl.preinstalled">{{ truncate(tpl.preinstalled, 15) }}</td>
                <td style="padding: 8px">{{ tpl.disk_size_mb || 0 }}</td>
                <td style="padding: 8px"><span :style="{ color: tpl.enabled ? '#16a34a' : '#dc2626' }">{{ tpl.enabled ? '✅' : '❌' }}</span></td>
                <td style="padding: 8px; font-size: 10px; color: var(--t3)">{{ tpl.last_check_status || '—' }}</td>
                <td style="padding: 8px; display: flex; gap: 3px; flex-wrap: wrap">
                  <button class="bt bt-o bt-s" style="font-size: 9px; padding: 2px 6px" :title="t('sbx.tpl_edit_tip')" @click="openEditTemplate(tpl)">✏ {{ t('sbx.edit') }}</button>
                  <button class="bt bt-o bt-s" style="font-size: 9px; padding: 2px 6px" :title="tpl.enabled ? (t('sbx.tpl_disable_tip')) : (t('sbx.tpl_enable_tip'))" @click="toggleTemplate(tpl)">{{ tpl.enabled ? '⏸ ' + (t('sbx.disable')) : '▶ ' + (t('sbx.enable')) }}</button>
                  <button class="bt bt-o bt-s" style="font-size: 9px; padding: 2px 6px" :title="t('sbx.tpl_check_tip')" :disabled="checkingTemplateId === tpl.id" @click="checkTemplate(tpl)">{{ checkingTemplateId === tpl.id ? (t('sbx.checking')) : '🏥 ' + (t('sbx.check')) }}</button>
                  <button class="bt bt-o bt-s" style="font-size: 9px; padding: 2px 6px; color: #dc2626" :title="t('sbx.tpl_delete_tip')" @click="deleteTemplate(tpl)">🗑 {{ t('sbx.delete') }}</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ============ 弹窗：新增/编辑 Provider ============ -->
    <div v-if="showProviderDialog" style="position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0, 0, 0, 0.4); display: flex; align-items: center; justify-content: center; z-index: 9999" @click.self="showProviderDialog = false">
      <div class="apple-card" style="padding: 24px; width: 440px; max-width: 90vw; max-height: 85vh; overflow-y: auto">
        <h2 style="font-size: 15px; margin-bottom: 16px">{{ editingProvider ? (t('sbx.dialog_edit_title')) : (t('sbx.dialog_add_title')) }}</h2>
        <div style="margin-bottom: 10px">
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">{{ t('sbx.name') }}</label>
          <!-- Provider 名=凭据名（Worker 设备页改凭据名全链跟随），编辑态只读展示 -->
          <input
            v-model="providerForm.name"
            style="width: 100%; padding: 8px; border: 1px solid var(--b); border-radius: 8px; background: var(--c); font-size: 13px; color: var(--t3); cursor: not-allowed"
            disabled
            :title="t('sbx.provider_name_hint')"
          >
        </div>
        <div style="margin-bottom: 10px">
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">Platform</label>
          <select v-model="providerForm.platform" style="width: 100%; padding: 8px; border: 1px solid var(--b); border-radius: 8px; background: var(--c); font-size: 13px" :disabled="!!editingProvider">
            <option v-for="p in platforms" :key="p.platform" :value="p.platform">{{ p.platform }}</option>
          </select>
        </div>
        <div style="margin-bottom: 10px">
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">API URL</label>
          <input v-model="providerForm.endpoint" style="width: 100%; padding: 8px; border: 1px solid var(--b); border-radius: 8px; background: var(--c); font-size: 13px" placeholder="https://cube.example.com">
        </div>
        <div style="margin-bottom: 10px">
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">API Key {{ editingProvider ? '(' + (t('sbx.keep_empty')) + ')' : '' }}</label>
          <input v-model="providerForm.api_key" type="password" style="width: 100%; padding: 8px; border: 1px solid var(--b); border-radius: 8px; background: var(--c); font-size: 13px">
        </div>
        <div style="margin-bottom: 10px">
          <!-- 级别只读展示——范围在凭据上改（重新生成凭据），平台编辑只管接入配置 -->
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">{{ t('sbx.scope_ro_label') }}</label>
          <div style="padding: 8px; border: 1px solid var(--b); border-radius: 8px; background: var(--bg); font-size: 12px; color: var(--t3)" :title="t('sbx.scope_hint')">
            {{ providerForm.scope_level === 'global' ? (t('sbx.scope_global_label')) : providerForm.scope_level === 'repo' ? (t('sbx.scope_repo_label')) : (t('sbx.scope_user_label')) }}
          </div>
        </div>
        <div style="margin-bottom: 10px">
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">Max Sandboxes</label>
          <input v-model.number="providerForm.max_sandboxes" type="number" min="1" max="20" style="width: 100%; padding: 8px; border: 1px solid var(--b); border-radius: 8px; background: var(--c); font-size: 13px">
        </div>
        <!-- 动态平台专属字段 -->
        <div v-for="group in currentPlatformSchema.groups" :key="group.title" style="border-top: 1px solid var(--b); padding-top: 10px; margin-top: 10px">
          <label v-if="group.title" style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">{{ group.title }}</label>
          <div v-for="field in group.fields" :key="field.name" style="margin-bottom: 8px">
            <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">
              {{ field.label }}<span v-if="field.required" style="color: #dc2626">*</span>
            </label>
            <input
              v-if="field.type === 'text'"
              v-model="providerForm[field.name]"
              :placeholder="field.placeholder"
              autocomplete="off"
              style="width: 100%; padding: 8px; border: 1px solid var(--b); border-radius: 8px; background: var(--c); font-size: 12px"
            >
            <textarea
              v-else-if="field.type === 'textarea'"
              v-model="providerForm[field.name]"
              :placeholder="field.placeholder"
              autocomplete="off"
              style="width: 100%; padding: 8px; border: 1px solid var(--b); border-radius: 8px; background: var(--c); font-size: 11px; height: 80px; font-family: monospace"
            ></textarea>
            <input v-else-if="field.type === 'bool'" type="checkbox" v-model="providerForm[field.name]">
            <input
              v-else-if="field.type === 'number'"
              v-model.number="providerForm[field.name]"
              type="number"
              style="width: 100%; padding: 8px; border: 1px solid var(--b); border-radius: 8px; background: var(--c); font-size: 12px"
            >
            <select
              v-else-if="field.type === 'select'"
              v-model="providerForm[field.name]"
              style="width: 100%; padding: 8px; border: 1px solid var(--b); border-radius: 8px; background: var(--c); font-size: 12px"
            >
              <option v-for="opt in field.options" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
            </select>
          </div>
        </div>
        <div style="display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px">
          <button class="bt bt-o bt-s" @click="showProviderDialog = false">{{ t('sbx.cancel') }}</button>
          <button class="bt bt-o bt-s" style="background: var(--a); color: #fff; border-color: var(--a)" @click="saveProvider">{{ t('sbx.save') }}</button>
        </div>
      </div>
    </div>

    <!-- ============ 弹窗：沙箱启动失败原因 ============ -->
    <div v-if="sandboxErrorModal.visible" style="position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0, 0, 0, 0.4); display: flex; align-items: center; justify-content: center; z-index: 9999" @click.self="sandboxErrorModal.visible = false">
      <div style="background: var(--c); border-radius: 14px; padding: 20px; max-width: 560px; width: 90%; box-shadow: 0 8px 32px rgba(0, 0, 0, 0.15)">
        <h3 style="font-size: 14px; font-weight: 600; margin-bottom: 4px">{{ t('sbx.setup_error_title') }}</h3>
        <div style="font-size: 10px; color: var(--t3); font-family: monospace; margin-bottom: 10px; word-break: break-all" :title="sandboxErrorModal.sandboxId">{{ sandboxErrorModal.sandboxId }}</div>
        <div style="background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; padding: 10px 12px; font-size: 12px; color: #b91c1c; line-height: 1.6; max-height: 260px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; user-select: text">{{ sandboxErrorModal.error }}</div>
        <div style="display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px">
          <button class="bt bt-o bt-s" @click="copySandboxError()">📋 {{ t('sbx.copy') }}</button>
          <button class="bt bt-p bt-s" @click="sandboxErrorModal.visible = false">{{ t('sbx.close') }}</button>
        </div>
      </div>
    </div>

    <!-- ============ 弹窗：新增/编辑模板 ============ -->
    <div v-if="showTemplateDialog" style="position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0, 0, 0, 0.4); display: flex; align-items: center; justify-content: center; z-index: 9999; padding: 20px" @click.self="showTemplateDialog = false">
      <div class="apple-card" style="padding: 24px; width: 400px; max-width: 90vw; max-height: calc(100vh - 40px); overflow-y: auto">
        <h2 style="font-size: 14px; margin-bottom: 12px">{{ editingTemplate ? (t('sbx.tpl_edit')) : (t('sbx.tpl_add')) }}</h2>
        <div v-if="editingTemplate" style="font-size: 10px; color: #92400e; background: #fef3c7; border: 1px solid #fde68a; border-radius: 6px; padding: 6px 10px; margin-bottom: 12px; line-height: 1.6" :title="t('sbx.tpl_rename_hint')">
          ⚠ {{ t('sbx.tpl_rename_full') }}
        </div>
        <div style="margin-bottom: 8px">
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">{{ t('sbx.name') }}</label>
          <input v-model="templateForm.name" style="width: 100%; padding: 8px; border: 1px solid var(--b); border-radius: 8px; background: var(--c); font-size: 13px">
        </div>
        <div style="margin-bottom: 8px">
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">Template ID</label>
          <input
            v-model="templateForm.template_id"
            :disabled="!!editingTemplate"
            style="width: 100%; padding: 8px; border: 1px solid var(--b); border-radius: 8px; background: var(--c); font-size: 13px"
            :style="editingTemplate ? 'color:var(--t3);cursor:not-allowed;background:var(--bg)' : ''"
            :title="editingTemplate ? (t('sbx.tplid_immutable')) : ''"
          >
        </div>
        <div style="margin-bottom: 8px">
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">Alias</label>
          <input v-model="templateForm.alias" style="width: 100%; padding: 8px; border: 1px solid var(--b); border-radius: 8px; background: var(--c); font-size: 13px">
        </div>
        <div style="margin-bottom: 8px">
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">{{ t('sbx.description') }}</label>
          <input v-model="templateForm.description" style="width: 100%; padding: 8px; border: 1px solid var(--b); border-radius: 8px; background: var(--c); font-size: 13px">
        </div>
        <div style="margin-bottom: 8px">
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">Disk Size (MB)</label>
          <input v-model.number="templateForm.disk_size_mb" type="number" style="width: 100%; padding: 8px; border: 1px solid var(--b); border-radius: 8px; background: var(--c); font-size: 13px">
        </div>
        <div style="margin-bottom: 8px">
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">Preinstalled (JSON)</label>
          <input v-model="templateForm.preinstalled" style="width: 100%; padding: 8px; border: 1px solid var(--b); border-radius: 8px; background: var(--c); font-size: 12px; font-family: monospace" placeholder='{"python":"3.14","git":"2.39.5"}'>
        </div>
        <div style="margin-bottom: 8px">
          <label style="font-size: 11px; color: var(--t3); display: block; margin-bottom: 4px">{{ t('sbx.engines') }}</label>
          <div v-if="availableEngines.length" style="border: 1px solid var(--b); border-radius: 8px; overflow: hidden">
            <div style="display: grid; grid-template-columns: 1fr 88px 88px; padding: 5px 12px; background: var(--bg); font-size: 10px; color: var(--t3); border-bottom: 1px solid var(--b)">
              <span></span>
              <span style="text-align: center">{{ t('sbx.bind_header') }}</span>
            </div>
            <div
              v-for="e in availableEngines"
              :key="e.engine_type"
              style="display: grid; grid-template-columns: 1fr 88px; align-items: center; padding: 6px 12px; border-bottom: 1px solid var(--b); font-size: 12px"
              :style="templateForm.engines[e.engine_type] ? '' : 'color:var(--t3)'"
            >
              <span :style="templateForm.engines[e.engine_type] ? 'color:var(--t1)' : 'color:var(--t3)'">{{ e.engine_type }}</span>
              <span style="text-align: center" :title="t('sbx.bind_tip')">
                <input type="checkbox" :checked="!!templateForm.engines[e.engine_type]" @change="toggleEngineMode(e.engine_type, 'preinstalled')" style="accent-color: var(--a); cursor: pointer">
              </span>
            </div>
          </div>
          <div v-else style="font-size: 11px; color: var(--t3)">{{ t('sbx.loading_engines') }}</div>
          <div style="font-size: 10px; color: var(--t3); margin-top: 4px; line-height: 1.5">{{ t('sbx.bind_full_hint') }}</div>
        </div>
        <div style="display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px">
          <button class="bt bt-o bt-s" @click="showTemplateDialog = false">{{ t('sbx.cancel') }}</button>
          <button class="bt bt-o bt-s" style="background: var(--a); color: #fff; border-color: var(--a)" @click="saveTemplate">{{ t('sbx.save') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
// 沙箱平台管理页 — B2 移植（源 legacy static/js/pages/sandbox.js 711 行，Vue3 createApp
// 字符串模板 → SFC 选项式直译）。18 个功能点全量保留（见
// docs todo/2026-11-13-sandbox-frontend-feature-points.md）。
// 文案沿用原 isZh 双语内联形态（不走 locale key，避免 i18n 依赖）。
const G = globalThis

export default {
  data() {
    return {
      activeTab: 'providers',
      view: 'list', // list | detail（列表视图已按 2026-10-30 裁定移除，仅深链/单 Provider 落详情）
      isZh: typeof G.I18N !== 'undefined' && G.I18N && G.I18N.getLang ? G.I18N.getLang() === 'zh' : true,
      langVer: 0, // 语言切换重渲染锚（setLang 就位后派发 grape:langchange）
      platforms: [],
      providers: [],
      providerLoading: false,
      providerSearchQ: '',
      providerPage: 1,
      providerPageSize: 20,
      providerTotal: 0,
      selectedProvider: null,
      templates: [],
      templateLoading: false,
      showProviderDialog: false,
      editingProvider: null,
      providerForm: this._defaultProviderForm(),
      _lastAutoRepoKey: null,
      showTemplateDialog: false,
      editingTemplate: null,
      templateForm: { name: '', template_id: '', alias: '', description: '', preinstalled: '{}', disk_size_mb: 0, engines: {} },
      availableEngines: [],
      sandboxErrorModal: { visible: false, sandboxId: '', error: '' },
      checkingTemplateId: 0,
      scanningTemplates: false,
      healthResults: {},
      llmCredReady: null,
    }
  },
  computed: {
    providerTotalPages() {
      return Math.max(1, Math.ceil(this.providerTotal / this.providerPageSize))
    },
    // [USP] 平台能力位——manifest.capabilities 是否含 list_templates（不支持的平台扫描按钮禁用）
    scanCapable() {
      const sp = this.selectedProvider || {}
      const pf = (this.platforms || []).find((p) => p.platform === sp.platform)
      const caps = (pf && pf.manifest && pf.manifest.capabilities) || null
      return caps ? caps.indexOf('list_templates') !== -1 : null
    },
    // [repo-llm-credential] 凭据来源标签（按 Provider 关联凭据 scope——列表输出已 JOIN）
    llmCredSourceLabel() {
      const sc = (this.selectedProvider && this.selectedProvider.scope) || ''
      return this.t(sc === 'global' ? 'sbx.src_global' : sc === 'repo' ? 'sbx.src_repo' : 'sbx.src_user')
    },
    currentPlatformSchema() {
      const p = (this.platforms || []).find((x) => x.platform === this.providerForm.platform)
      return p && p.ui_schema ? p.ui_schema : { groups: [] }
    },
  },
  watch: {
    'providerForm.platform'(newVal) {
      this._initPlatformFields(newVal)
    },
    'providerForm.scope_level'(newVal) {
      if (newVal !== 'repo' || !this.providerForm) return
      if (this.providerForm.repo_key && this.providerForm.repo_key !== this._lastAutoRepoKey) return // 用户手输过
      const rk = this._currentWorkRepoKey()
      if (rk) {
        this.providerForm.repo_key = rk
        this._lastAutoRepoKey = rk
      }
    },
  },
  mounted() {
    this.loadPlatforms()
      .then(() => this.loadProviders())
      .then(() => this._maybeDeepLink())
    // #sandbox/provider/{id} 深链——Worker 设备卡「详情」直达平台详情
    if (!G._sbHashListener) {
      G._sbHashListener = true
      G.addEventListener('hashchange', () => this._maybeDeepLink())
    }
    if (!G._sbLangListener) {
      G._sbLangListener = true
      // G-18 词条化后文案走 t()：切换语言需触发整组件重渲染（模板读 langVer 锚）
      G.addEventListener('grape:langchange', () => {
        this.isZh = G.I18N && G.I18N.getLang ? G.I18N.getLang() === 'zh' : true
        this.langVer += 1
      })
    }
  },
  methods: {
    t(key, vars) {
      // i18n pack 按模块名分层（shell/locales/<lang>/sandbox.json → 'sandbox.'），同 ObservabilityView 的 observability. 前缀模式
      return G.I18N && G.I18N.t ? G.I18N.t('sandbox.' + key, vars) || G.I18N.t(key, vars) || key : key
    },
    async loadPlatforms() {
      try {
        const d = await G.API.get('/settings/sandbox-platforms')
        this.platforms = d.platforms || []
      } catch (e) {
        this.platforms = []
      }
    },
    _defaultProviderForm() {
      return { name: '', platform: '', endpoint: '', api_key: '', max_sandboxes: 10, enabled: true, scope_level: 'user', repo_key: this._currentWorkRepoKey(), _was_global: false }
    },
    _initPlatformFields(platform) {
      // 清除旧平台的动态字段（保留通用字段）
      const commonKeys = new Set(['name', 'platform', 'endpoint', 'api_key', 'max_sandboxes', 'enabled', 'scope_level', 'repo_key', '_was_global'])
      const allUiFields = new Set()
      ;(this.platforms || []).forEach((p) => {
        if (p.ui_schema) {
          ;(p.ui_schema.groups || []).forEach((g) => {
            ;(g.fields || []).forEach((f) => allUiFields.add(f.name))
          })
        }
      })
      Object.keys(this.providerForm).forEach((k) => {
        if (!commonKeys.has(k) && allUiFields.has(k)) {
          delete this.providerForm[k]
        }
      })
      // 初始化新平台的字段默认值
      const p = (this.platforms || []).find((x) => x.platform === platform)
      if (!p || !p.ui_schema) return
      ;(p.ui_schema.groups || []).forEach((g) => {
        ;(g.fields || []).forEach((f) => {
          if (this.providerForm[f.name] === undefined) {
            this.providerForm[f.name] = f.default !== undefined ? f.default : ''
          }
        })
      })
    },
    // ===== Provider 列表 =====
    async loadProviders() {
      this.providerLoading = true
      try {
        let url = '/settings/sandbox-providers?page=' + this.providerPage + '&page_size=' + this.providerPageSize
        if (this.providerSearchQ) url += '&q=' + encodeURIComponent(this.providerSearchQ)
        const d = await G.API.get(url)
        this.providers = d.providers || []
        this.providerTotal = d.total || 0
      } catch (e) {
        this.providers = []
        this.providerTotal = 0
      }
      this.providerLoading = false
    },
    // ===== Provider 详情 =====
    async selectProvider(p) {
      this.selectedProvider = p
      this.view = 'detail'
      await this.loadTemplates(p.id)
      this._checkLlmCredReady(p)
    },
    async _checkLlmCredReady(p) {
      // 来源位置未配置 → 黄色警示（个人级查用户凭据；全局/仓库级查当前工作仓库）
      this.llmCredReady = null
      try {
        const sc = (p && p.scope) || ''
        if (sc === 'global' || sc === 'repo') {
          const rk = this._currentWorkRepoKey()
          if (!rk) {
            this.llmCredReady = null
            return
          }
          const d = await G.API.get('/settings/llm-credentials/repo?repo_key=' + encodeURIComponent(rk))
          this.llmCredReady = (d.credentials || []).some((r) => r.enabled)
        } else {
          const d = await G.API.get('/settings/llm-credentials')
          this.llmCredReady = (d.credentials || []).some((r) => r.enabled)
        }
      } catch (e) {
        this.llmCredReady = null
      }
    },
    // [平台列表页移除 2026-10-30 用户裁定] 返回改为回 Agent 运行时页(列表视图废弃)
    backToList() {
      this.selectedProvider = null
      G.app.nav('runtimes')
    },
    _maybeDeepLink() {
      // 解析 #sandbox/provider/{id}——loadProviders 完成后按 id 进详情；
      // 裸 #sandbox(列表)不再展示——单 Provider 直进详情, 多 Provider/无法定位重定向运行时页
      // hash 先 decodeURIComponent 再匹配：详情按钮走 app.nav('sandbox','provider/{id}')，
      // shell 的 _pushHistory 对 param 整段 encodeURIComponent → #sandbox/provider%2F{id}；
      // legacy 直设 hash 不编码，两种形态 decode 后统一为字面 /。（Vue 移植回归：编码形态
      // 曾失配 → 兜底 nav('runtimes') 弹回设备页，实测 push:#sandbox/provider%2F3）
      try {
        const m = decodeURIComponent(location.hash || '').match(/^#sandbox\/provider\/([^/?#]+)/)
        if (!this.providers || !this.providers.length) return
        if (m) {
          const pid = m[1]
          const p = this.providers.find((x) => String(x.id) === pid)
          if (p) {
            this.selectProvider(p)
            return
          }
        }
        // 无 id/查不到(含裸 #sandbox): 列表视图已删——兜底落地
        if (this.view === 'list') {
          if (this.providers.length === 1) {
            this.selectProvider(this.providers[0])
          } else {
            G.app.nav('runtimes')
          }
        }
      } catch (e) {
        /* 深链解析失败忽略 */
      }
    },
    viewSandboxes(pid) {
      // 沙箱列表已迁运行驾驶舱——app.nav 触发视图切换与 history
      G.app.nav('observability', pid ? 'sandboxes?provider=' + pid : 'sandboxes')
    },
    // ===== Provider CRUD =====
    _currentWorkRepoKey() {
      let r = null
      try {
        if (typeof G.currentRepo === 'function') r = G.currentRepo()
      } catch (_) {}
      if (!r) {
        for (let i = 0; i < 2 && !r; i++) {
          try {
            r = JSON.parse((i === 0 ? sessionStorage : localStorage).getItem('currentRepo'))
          } catch (_) {
            r = null
          }
        }
      }
      if (!r) return ''
      return r.repoKey || r.repo_key || (r.fullName ? 'gitcode:' + r.fullName : '')
    },
    openCreateProvider() {
      this.editingProvider = null
      this.providerForm = this._defaultProviderForm()
      if (this.platforms.length > 0) {
        this.providerForm.platform = this.platforms[0].platform
      }
      this._initPlatformFields(this.providerForm.platform)
      this.showProviderDialog = true
    },
    openEditProvider(p) {
      this.editingProvider = p
      const extra = p.extra_config || {}
      this.providerForm = {
        name: p.name,
        platform: p.platform,
        endpoint: p.endpoint,
        api_key: '',
        max_sandboxes: p.max_sandboxes || 10,
        enabled: p.enabled,
        scope_level: p.scope === 'global' ? 'global' : p.scope === 'repo' ? 'repo' : 'user',
        repo_key: p.repo_key || this._currentWorkRepoKey(),
        _was_global: p.scope === 'system',
      }
      this._initPlatformFields(p.platform)
      if (extra) {
        for (const k in extra) {
          if (this.providerForm[k] !== undefined) {
            this.providerForm[k] = extra[k]
          }
        }
      }
      this.showProviderDialog = true
    },
    async saveProvider() {
      if (!this.providerForm.name || !this.providerForm.endpoint) {
        alert(this.t('sbx.name_endpoint_required'))
        return
      }
      // 前端必填校验
      const schema = this.currentPlatformSchema
      for (let gi = 0; gi < (schema.groups || []).length; gi++) {
        for (let fi = 0; fi < (schema.groups[gi].fields || []).length; fi++) {
          const f = schema.groups[gi].fields[fi]
          if (f.required && (!this.providerForm[f.name] || this.providerForm[f.name] === '')) {
            alert((this.t('sbx.required_prefix')) + (f.label || f.name))
            return
          }
        }
      }
      try {
        // 动态组装 extra_config
        const extra = {}
        ;(schema.groups || []).forEach((g) => {
          ;(g.fields || []).forEach((f) => {
            if (this.providerForm[f.name] !== undefined && this.providerForm[f.name] !== '') {
              extra[f.name] = this.providerForm[f.name]
            }
          })
        })
        const body = {
          name: this.providerForm.name,
          platform: this.providerForm.platform,
          endpoint: this.providerForm.endpoint,
          max_sandboxes: this.providerForm.max_sandboxes,
          enabled: this.providerForm.enabled,
          extra_config: extra,
        }
        if (this.providerForm.api_key) body.api_key = this.providerForm.api_key
        if (this.editingProvider) {
          await G.API.put('/settings/sandbox-providers/' + this.editingProvider.id, body)
        } else {
          await G.API.post('/settings/sandbox-providers', body)
        }
        this.showProviderDialog = false
        if (G.app) {
          G.app.showToast(this.editingProvider
            ? this.t('sbx.platform_updated')
            : this.t('sbx.platform_created'))
        }
        await this.loadProviders()
      } catch (e) {
        this._handleError(e)
      }
    },
    async toggleProvider(p) {
      try {
        await G.API.put('/settings/sandbox-providers/' + p.id + '/toggle', { enabled: !p.enabled })
        p.enabled = !p.enabled
        await this.loadProviders()
        // selectedProvider 独立引用 → 从刷新后的列表找回同 id 行同步（详情页徽标/按钮文案即时更新）
        if (this.selectedProvider && this.selectedProvider.id === p.id) {
          const fresh = (this.providers || []).find((x) => x.id === p.id)
          if (fresh) this.selectedProvider = { ...this.selectedProvider, ...fresh }
        }
      } catch (e) {
        this._handleError(e)
      }
    },
    async toggleTemplate(t) {
      try {
        await G.API.put('/settings/sandbox-templates/' + t.id + '/toggle', { enabled: !t.enabled })
        await this.loadTemplates(this.selectedProvider && this.selectedProvider.id)
      } catch (e) {
        this._handleError(e)
      }
    },
    async healthCheck(p) {
      this.healthResults = { ...this.healthResults, [p.id]: { loading: true } }
      try {
        const d = await G.API.post('/settings/sandbox-providers/' + p.id + '/health')
        this.healthResults = { ...this.healthResults, [p.id]: { healthy: d.healthy, reason: d.reason } }
      } catch (e) {
        this.healthResults = { ...this.healthResults, [p.id]: { healthy: false, reason: e.message } }
      }
    },
    // ===== 模板 =====
    async loadTemplates(providerId) {
      this.templateLoading = true
      try {
        const d = await G.API.get('/settings/sandbox-templates?provider_id=' + providerId + '&page=1&page_size=100')
        this.templates = d.templates || []
      } catch (e) {
        this.templates = []
      }
      this.templateLoading = false
    },
    openEditTemplate(t) {
      this.editingTemplate = t
      const cur = {}
      try {
        const arr = JSON.parse(t.engines || '[]')
        if (Array.isArray(arr)) arr.forEach((et) => { cur[et] = 'preinstalled' })
      } catch (_) {}
      // [云沙箱禁自动安装] 引擎模式已无意义（install 通道已移除），绑定统一归一 preinstalled
      Object.keys(cur).forEach((et) => { cur[et] = 'preinstalled' })
      this.templateForm = {
        name: t.name || '',
        template_id: t.template_id || '',
        alias: t.alias || '',
        description: t.description || '',
        preinstalled: typeof t.preinstalled === 'string' && t.preinstalled ? t.preinstalled : '{}',
        disk_size_mb: t.disk_size_mb || 0,
        engines: cur,
      }
      this.showTemplateDialog = true
      this._ensureEnginesLoaded()
    },
    async _ensureEnginesLoaded() {
      if (this.availableEngines.length) return
      try {
        const d = await G.API.get('/settings/sandbox-engines')
        this.availableEngines = d.engines || []
      } catch (e) {
        this.availableEngines = []
      }
    },
    toggleEngineMode(et, mode) {
      // 互斥三态: 点击已选中的 → 取消; 点击另一个 → 切换
      if (this.templateForm.engines[et] === mode) {
        delete this.templateForm.engines[et]
      } else {
        this.templateForm.engines[et] = mode
      }
    },
    async saveTemplate() {
      if (!this.templateForm.name || !this.templateForm.template_id) {
        alert(this.t('sbx.name_tplid_required'))
        return
      }
      try {
        const body = { ...this.templateForm, provider_id: this.selectedProvider.id, provider: this.selectedProvider.platform }
        let tid = this.editingTemplate ? this.editingTemplate.id : null
        if (this.editingTemplate) {
          await G.API.put('/settings/sandbox-templates/' + this.editingTemplate.id, body)
        } else {
          const d = await G.API.post('/settings/sandbox-templates', body)
          tid = d.id
        }
        // 内置引擎勾选 → 同步 bind-engine（新增/编辑统一处理）
        if (tid) {
          const want = Object.keys(this.templateForm.engines || {})
          let have = []
          try {
            have = JSON.parse((this.editingTemplate && this.editingTemplate.engines) || '[]')
          } catch (_) {
            have = []
          }
          if (!Array.isArray(have)) have = []
          for (let wi = 0; wi < want.length; wi++) {
            if (have.indexOf(want[wi]) < 0) {
              try {
                await G.API.post('/settings/sandbox-templates/' + tid + '/bind-engine', { engine_type: want[wi] })
              } catch (e) {
                if (G.app) G.app.showToast((this.t('sbx.engine_prefix')) + want[wi] + (this.t('sbx.bind_failed_suffix')) + (e.message || e), '', 6000)
                else alert(e.message || e)
              }
            }
          }
          for (let hi = 0; hi < have.length; hi++) {
            if (want.indexOf(have[hi]) < 0) {
              try {
                await G.API.del('/settings/sandbox-templates/' + tid + '/bind-engine/' + have[hi])
              } catch (e) {
                if (G.app) G.app.showToast((this.t('sbx.engine_prefix')) + have[hi] + (this.t('sbx.unbind_failed_suffix')) + (e.message || e), '', 6000)
                else alert(e.message || e)
              }
            }
          }
        }
        this.showTemplateDialog = false
        await this.loadTemplates(this.selectedProvider.id)
      } catch (e) {
        this._handleError(e)
      }
    },
    async deleteTemplate(t) {
      if (!confirm(this.t('sbx.confirm_delete_tpl'))) return
      try {
        await G.API.del('/settings/sandbox-templates/' + t.id)
        await this.loadTemplates(this.selectedProvider.id)
      } catch (e) {
        this._handleError(e)
      }
    },
    // ===== 沙箱错误查看 =====
    openSandboxError(s) {
      this.sandboxErrorModal = { visible: true, sandboxId: s.sandbox_id, error: s.last_error || (this.t('sbx.no_error_recorded')) }
    },
    copySandboxError() {
      const text = this.sandboxErrorModal.error
      const _toast = () => {
        if (G.app) G.app.showToast(this.t('sbx.error_copied'))
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(_toast).catch(() => this._copyFallback(text, _toast))
      } else {
        this._copyFallback(text, _toast)
      }
    },
    _copyFallback(text, cb) {
      const ta = document.createElement('textarea')
      ta.value = text
      document.body.appendChild(ta)
      ta.select()
      try {
        document.execCommand('copy')
        cb()
      } catch (e) {}
      ta.remove()
    },
    async scanTemplates() {
      if (this.scanningTemplates || !this.selectedProvider) return
      this.scanningTemplates = true
      try {
        const d = await G.API.post('/settings/sandbox-providers/' + this.selectedProvider.id + '/scan-templates')
        const msg = (this.t('sbx.scan_done_prefix')) + (d.valid != null ? d.valid : 0) +
          (this.t('sbx.scan_valid_mid')) + (d.existing != null ? d.existing : 0) +
          (this.t('sbx.scan_added_mid')) + (d.added != null ? d.added : 0) +
          (this.t('sbx.count_suffix'))
        alert(msg)
        await this.loadTemplates(this.selectedProvider.id)
      } catch (e) {
        this._handleError(e)
      }
      this.scanningTemplates = false
    },
    async checkTemplate(t) {
      if (this.checkingTemplateId) return
      this.checkingTemplateId = t.id
      try {
        const d = await G.API.post('/settings/sandbox-templates/' + t.id + '/check')
        t.last_check_status = d.status || 'error'
        t.last_check_error = d.error || ''
        if (d.status === 'ok') alert(this.t('sbx.check_passed'))
        else alert((this.t('sbx.check_failed_prefix')) + (d.status || '') + (d.error ? ' — ' + d.error : ''))
        await this.loadTemplates(this.selectedProvider.id)
      } catch (e) {
        this._handleError(e)
      }
      this.checkingTemplateId = 0
    },
    // ===== 工具 =====
    _handleError(e) {
      let msg = e.message || 'error'
      try {
        msg = JSON.parse(e.message).message || msg
      } catch (_) {}
      alert(msg)
    },
    stateColor(s) {
      return { running: '#16a34a', killed: '#6b7280', kill_failed: '#dc2626', stale: '#d97706', creating: '#2563eb', setup_failed: '#dc2626' }[s] || '#6b7280'
    },
    truncate(id, len) {
      return id && String(id).length > len ? String(id).substring(0, len) + '...' : id || '—'
    },
    fmtTime(ts) {
      if (!ts) return '—'
      try {
        return new Date(ts).toLocaleString()
      } catch (e) {
        return ts
      }
    },
    hasSSL(p) {
      const extra = p.extra_config || {}
      return !!(extra.ssl_cert_content)
    },
    hasGateway(p) {
      const extra = p.extra_config || {}
      return !!(extra.gateway_url || extra.gateway_ip)
    },
    getProviderSchema(platform) {
      const p = (this.platforms || []).find((x) => x.platform === platform)
      return p && p.ui_schema ? p.ui_schema : { groups: [] }
    },
    formatExtra(provider, field) {
      const extra = provider.extra_config || {}
      const val = extra[field.name]
      if (field.type === 'bool') return val ? '✅' : '❌'
      return val || '—'
    },
    parseEngines(t) {
      try {
        const e = JSON.parse(t.engines || '[]')
        return Array.isArray(e) ? e.join(', ') : t.engines
      } catch (_) {
        return t.engines || '—'
      }
    },
  },
}
</script>
