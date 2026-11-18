import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// Vue refactor (phase 2): two output kinds
// 1. SPA at frontend/static-dist (base /static-vue/) — standalone migrated pages
// 2. view entry chunks copied to frontend/shell/js/vue/ — Vue views mounted INSIDE
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
          'login-view': /^[lL]ogin-[vV]iew-.*\.css$/,
          'register-view': /^[rR]egister-[vV]iew-.*\.css$/,
          'forgot-view': /^[fF]orgot-[vV]iew-.*\.css$/,
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
          'onboarding-view': /^[oO]nboarding-[vV]iew-.*\.css$/,
          // 共享组件（跨两个 entry 使用 → Vite 拆成独立 CSS chunk，必须给稳定名让 entry 引）
          'bind-dialog': /^BindDialog-.*\.css$/,
          'pat-dialog': /^PatDialog-.*\.css$/,
          'cred-repo-row': /^CredRepoRow-.*\.css$/,
          // LLM 凭据弹窗：AccountView(user) 与 SettingsView(repo) 两个 entry 共用 →
          // Vite 拆成独立 CSS chunk；不进这张表就没有任何 <link> 加载它（entry 自注入已废弃），
          // 组件样式会整段失效（表单控件落回 UA 默认灰底黑边）。
          'llm-cred-manager': /^LlmCredManager-.*\.css$/,
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
          'sandbox-view': /^sandbox-[vV]iew-.*\.css$/,
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
        // 全站基础样式（原 legacy/css）：单一来源 = app/src/styles/global，产物固定名进 assets/css/
        const globalCss = path.resolve(__dirname, './src/styles/global')
        const dstGlobal = path.join(dstAssets, 'css')
        fs.mkdirSync(dstGlobal, { recursive: true })
        for (const f of ['tokens.css', 'app.css', 'auth.css']) {
          fs.copyFileSync(path.join(globalCss, f), path.join(dstGlobal, f))
        }

        // 2026-11-16：view 级 CSS 合并成一份「阻塞式」产物，由 shell 的 <head> 直接引入。
        // 背景：原来每个 entry 在模块求值时 createElement('link') 注入自己的 css，
        // 而老壳是同步 mount 的 → 首帧渲染出无样式界面（第二次刷新因缓存命中才正常）。
        // 现在 CSS 在 <head> 里随 HTML 阻塞加载，首帧就有样式；各 entry 不再各自注入。
        // view 的 scoped 样式都带 data-v-* 前缀，合并成一份不会互相污染。
        const viewCssDir = path.join(dstAssets, 'viewcss')
        // admin-view.css 必须排除：AdminView.vue 的 <style> 是**非 scoped** 的（.ct/.tb/.card 等
        // 都是全局选择器，最大宽度 980px 就是壳内容区的），只该在 admin.html 里加载。
        // 一旦并进全站 bundle 会把主壳内容压到 980px（表现为“只显示半屏”）。
        const viewFiles = fs
          .readdirSync(viewCssDir)
          .filter((f) => f.endsWith('.css') && f !== 'admin-view.css' && f !== 'all-views.css')
          .sort()
        const bundle = [
          fs.readFileSync(path.join(dstAssets, 'bridge.css'), 'utf8'),
          fs.readFileSync(path.join(dstAssets, 'ag-ui.css'), 'utf8'),
          ...viewFiles.map((f) => fs.readFileSync(path.join(viewCssDir, f), 'utf8')),
        ].join('\n')
        fs.writeFileSync(path.join(viewCssDir, 'all-views.css'), bundle)
        console.log(`wrote view css bundle -> ${path.join(viewCssDir, 'all-views.css')} (${viewFiles.length} views)`)

        console.log('wrote global css -> ' + dstGlobal)
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
        'login-view': fileURLToPath(new URL('./src/views/entries/login-view.js', import.meta.url)),
        'register-view': fileURLToPath(new URL('./src/views/entries/register-view.js', import.meta.url)),
        'forgot-view': fileURLToPath(new URL('./src/views/entries/forgot-view.js', import.meta.url)),
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
        'onboarding-view': fileURLToPath(new URL('./src/views/entries/onboarding-view.js', import.meta.url)),
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
        'sandbox-view': fileURLToPath(new URL('./src/views/entries/sandbox-view.js', import.meta.url)),
      },
      output: {
        entryFileNames: (chunkInfo) =>
          chunkInfo.name === 'index' ? 'assets/index-[hash].js' : 'viewjs/[name].js',
      },
    },
  },
})
