import { createRouter, createWebHistory } from 'vue-router'
import Home from '@/pages/Home.vue'
import UiShowcase from '@/pages/UiShowcase.vue'

// Migration phase: hash-free paths under /static-vue/ (FastAPI serves dist).
// Each migrated legacy module becomes a route here (batch by batch).
const router = createRouter({
  history: createWebHistory('/static-vue/'),
  routes: [
    { path: '/', component: Home },
    { path: '/ui', component: UiShowcase },
  ],
})

export default router
