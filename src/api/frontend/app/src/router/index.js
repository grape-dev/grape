import { createRouter, createWebHistory } from 'vue-router'
import BetaApply from '@/pages/BetaApply.vue'

// 2026-11-12：只保留生产页 beta-apply；Home / UiShowcase / ThemeLab 是开发页，已删除。
const router = createRouter({
  history: createWebHistory('/static-vue/'),
  routes: [
    { path: '/', redirect: '/beta-apply' },
    { path: '/beta-apply', component: BetaApply },
    { path: '/:pathMatch(.*)*', redirect: '/beta-apply' },
  ],
})

export default router
