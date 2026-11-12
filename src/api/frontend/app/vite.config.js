import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// Vue refactor (phase 2): two output kinds
// 1. SPA at frontend/static-dist (base /static-vue/) — standalone migrated pages
// 2. view entry chunks copied to frontend/legacy/js/vue/ — Vue views mounted INSIDE
//    the legacy shell (index.html + app.js), replacing js/pages/*.js modules batch by
//    batch. These chunks keep the legacy global contract (renderMyFeedback etc.) and
//    reuse legacy I18N/API/app globals, so behavior stays identical.
//    (Chunks are built into static-dist/viewjs/ then copied by a post-build step,
//    because rollup forbids ../ relative output patterns.)
export default defineConfig({
  plugins: [
    vue(),
    {
      name: 'copy-view-chunks',
      closeBundle() {
        const fs = require('node:fs')
        const path = require('node:path')
        // 2026-11-12 单一产物：不再把 chunk 拷到 legacy/js/{vue,assets}（那是同一份构建的副本）。
        // 旧壳 index.html 直接吃 /static-vue/viewjs/*.js，entry 注入 /static-vue/assets/*.css。
        const assetsDir = path.resolve(__dirname, '../static-dist/assets')
        const dstAssets = assetsDir
        fs.mkdirSync(path.join(dstAssets, 'viewcss'), { recursive: true })
        const viewCssMap = {
          'my-feedback': /^my-feedback-.*\.css$/,
          'my-feedback-detail': /^my-feedback-detail-.*\.css$/,
          'im-channel': /^im-channel-.*\.css$/,
          organizations: /^organizations-.*\.css$/,
          pipeline: /^[pP]ipeline-.*\.css$/,
          'agent-view': /^[aA]gent-[vV]iew-.*\.css$/,
          'scene-market': /^scene-market-.*\.css$/,
          'skills-view': /^skills-[vV]iew-.*\.css$/,
          'chat-view': /^chat-[vV]iew-.*\.css$/,
          'account-view': /^account-[vV]iew-.*\.css$/,
          'event-view': /^event-[vV]iew-.*\.css$/,
          'issues-view': /^issues-[vV]iew-.*\.css$/,
          'autopilot-view': /^autopilot-[vV]iew-.*\.css$/,
          'runtimes-view': /^runtimes-[vV]iew-.*\.css$/,
          'dashboard-view': /^dashboard-[vV]iew-.*\.css$/,
          'observability-view': /^observability-[vV]iew-.*\.css$/,
          'repos-view': /^repos-[vV]iew-.*\.css$/,
          'knowledge-view': /^knowledge-[vV]iew-.*\.css$/,
          'feedback-dialog': /^feedback-[dD]ialog-.*\.css$/,
          'admin-view': /^admin-[vV]iew-.*\.css$/,
          }
        for (const [name, re] of Object.entries(viewCssMap)) {
          const hit = fs.readdirSync(assetsDir).find((f) => re.test(f))
          if (hit) fs.copyFileSync(path.join(assetsDir, hit), path.join(dstAssets, 'viewcss', name + '.css'))
        }
        // ag-ui.css = concat of all AG* component css (stable aggregate for shell views)
        const agCss = fs
          .readdirSync(assetsDir)
          .filter((f) => /^AG[A-Za-z]+-.*\.css$/.test(f))
          .sort()
          .map((f) => fs.readFileSync(path.join(assetsDir, f), 'utf8'))
          .join('\n')
        fs.writeFileSync(path.join(dstAssets, 'ag-ui.css'), agCss)
        // bridge tokens (hand-maintained, stable name)
        fs.copyFileSync(
          path.resolve(__dirname, './src/styles/bridge.css'),
          path.join(dstAssets, 'bridge.css')
        )
        console.log(`wrote stable view css/assets -> ${dstAssets}`)
      },
    },
  ],
  base: '/static-vue/',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: '../static-dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        index: fileURLToPath(new URL('./index.html', import.meta.url)),
        'my-feedback': fileURLToPath(new URL('./src/views/entries/my-feedback.js', import.meta.url)),
        'my-feedback-detail': fileURLToPath(
          new URL('./src/views/entries/my-feedback-detail.js', import.meta.url)
        ),
        'im-channel': fileURLToPath(new URL('./src/views/entries/im-channel.js', import.meta.url)),
        organizations: fileURLToPath(new URL('./src/views/entries/organizations.js', import.meta.url)),
        pipeline: fileURLToPath(new URL('./src/views/entries/pipeline.js', import.meta.url)),
        'agent-view': fileURLToPath(new URL('./src/views/entries/agent-view.js', import.meta.url)),
        'scene-market': fileURLToPath(new URL('./src/views/entries/scene-market.js', import.meta.url)),
        'skills-view': fileURLToPath(new URL('./src/views/entries/skills-view.js', import.meta.url)),
        'chat-view': fileURLToPath(new URL('./src/views/entries/chat-view.js', import.meta.url)),
        'account-view': fileURLToPath(new URL('./src/views/entries/account-view.js', import.meta.url)),
        'event-view': fileURLToPath(new URL('./src/views/entries/event-view.js', import.meta.url)),
        'issues-view': fileURLToPath(new URL('./src/views/entries/issues-view.js', import.meta.url)),
        'autopilot-view': fileURLToPath(new URL('./src/views/entries/autopilot-view.js', import.meta.url)),
        'runtimes-view': fileURLToPath(new URL('./src/views/entries/runtimes-view.js', import.meta.url)),
        'dashboard-view': fileURLToPath(new URL('./src/views/entries/dashboard-view.js', import.meta.url)),
        'observability-view': fileURLToPath(new URL('./src/views/entries/observability-view.js', import.meta.url)),
        'repos-view': fileURLToPath(new URL('./src/views/entries/repos-view.js', import.meta.url)),
        'knowledge-view': fileURLToPath(new URL('./src/views/entries/knowledge-view.js', import.meta.url)),
        'feedback-dialog': fileURLToPath(new URL('./src/views/entries/feedback-dialog.js', import.meta.url)),
        'admin-view': fileURLToPath(new URL('./src/views/entries/admin-view.js', import.meta.url)),
      },
      output: {
        entryFileNames: (chunkInfo) =>
          chunkInfo.name === 'index' ? 'assets/index-[hash].js' : 'viewjs/[name].js',
      },
    },
  },
})
