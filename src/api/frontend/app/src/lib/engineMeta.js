// [P0b B6/C08-A2] 引擎元数据单源消费（/api/engines/meta，descriptor 派生）。
// 前端零硬编码引擎名/颜色/图标——新增引擎落一份 descriptor JSON 即自动生效。
// 匹配口径与旧 p0 前端一致：canonical includes 优先、aliases 后置
// （'aishell' 命中 hwcloud 的 canonical，颜色/图标取其 descriptor 值）。
const G = globalThis
const API = () => G.API

const state = { loaded: false, loading: false, metas: [], dot: {}, icon: {} }

export function loadEngineMeta() {
  if (state.loaded || state.loading) return
  state.loading = true
  API()
    .get('/engines/meta')
    .then(function (d) {
      const metas = (d && d.engines) || []
      state.metas = metas
      state.dot = {}
      state.icon = {}
      metas.forEach(function (m) {
        state.dot[m.canonical] = (m.display && m.display.dot_color) || ''
        // 图标键=page_type（hwcloud 显示历史页签 aishell 与别名共用资产）
        state.icon[m.page_type || m.canonical] = (m.display && m.display.icon) || ''
      })
      state.loaded = true
    })
    .catch(function () {
      state.loading = false
    })
}

export function engineMetas() {
  return state.metas
}

// 引擎名（或含引擎名的字符串）→ canonical；meta 未载入/未命中返回 ''
export function engineTypeOf(name) {
  const l = (name || '').toLowerCase()
  if (!l) return ''
  for (let i = 0; i < state.metas.length; i++) {
    if (l.includes(state.metas[i].canonical)) return state.metas[i].canonical
  }
  for (let i = 0; i < state.metas.length; i++) {
    const aliases = state.metas[i].aliases || []
    for (let j = 0; j < aliases.length; j++) {
      if (l.includes(aliases[j])) return state.metas[i].canonical
    }
  }
  return ''
}

// 引擎徽标颜色点（AgentView 消费）；meta 未载入返回 ''
export function engineDotOf(name) {
  return state.dot[engineTypeOf(name)] || ''
}

// 引擎水印图标资产路径（RuntimesView 消费，键=后端 annotate 的 page_type）
export function engineWatermark(pageType) {
  return state.icon[pageType || ''] || ''
}
