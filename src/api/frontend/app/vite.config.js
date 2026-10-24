import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// Vue refactor (phase 1): build output goes to ../static-dist so FastAPI can serve
// the new app under /static-vue/ without touching legacy /static serving.
// base must match the mount prefix chosen in src/api/main.py.
export default defineConfig({
  plugins: [vue()],
  base: '/static-vue/',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: '../static-dist',
    emptyOutDir: true,
  },
})
