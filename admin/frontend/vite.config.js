import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const pad = (n) => String(n).padStart(2, '0')
const d = new Date()
const buildTime = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`

export default defineConfig({
  plugins: [vue()],
  define: {
    __BUILD_TIME__: JSON.stringify(buildTime)
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8081'
    }
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true
  }
})
