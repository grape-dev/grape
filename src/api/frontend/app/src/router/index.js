import { createRouter, createWebHistory } from 'vue-router'
import Home from '@/pages/Home.vue'
import UiShowcase from '@/pages/UiShowcase.vue'
import BetaApply from '@/pages/BetaApply.vue'
import ThemeLab from '@/pages/ThemeLab.vue'

// Migration phase: hash-free paths under /static-vue/ (FastAPI serves dist).
// Each migrated legacy module becomes a route here (batch by batch).
const router = createRouter({
  history: createWebHistory('/static-vue/'),
  routes: [
    { path: '/', component: Home },
    { path: '/ui', component: UiShowcase },
    { path: '/beta-apply', component: BetaApply },
    { path: '/theme-lab', component: ThemeLab },
  ],
})

export default router
