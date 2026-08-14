import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import { initEllipsisTip } from './utils/ellipsisTip'
import './style.css'

initEllipsisTip()

createApp(App).use(router).mount('#app')
