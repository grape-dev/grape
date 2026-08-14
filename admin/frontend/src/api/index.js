import axios from 'axios'
import { getToken, getRefreshToken, setTokens, clearTokens } from '../utils/auth'

const http = axios.create({
  baseURL: '/api/admin',
  timeout: 30000
})

http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 并发 401 时只发一次 refresh，其余请求复用同一个 Promise
let refreshing = null

async function doRefresh() {
  const refreshToken = getRefreshToken()
  if (!refreshToken) throw new Error('no refresh token')
  const resp = await http.post('/auth/refresh', { refreshToken })
  const { token, refreshToken: newRefresh } = resp.data
  setTokens(token, newRefresh)
  return token
}

http.interceptors.response.use(
  (resp) => resp,
  async (err) => {
    const { response, config } = err
    // access token 过期（401）：先尝试 refresh 续期并重放原请求
    // 排除 /auth/login（密码错误）与 /auth/refresh（refresh 本身失效）
    if (response && response.status === 401 && !config._retried
        && config.url !== '/auth/login' && config.url !== '/auth/refresh') {
      config._retried = true
      try {
        if (!refreshing) {
          refreshing = doRefresh().finally(() => { refreshing = null })
        }
        const newToken = await refreshing
        config.headers = config.headers || {}
        config.headers.Authorization = `Bearer ${newToken}`
        return http(config)
      } catch (e) {
        // refresh 也失败（refresh token 过期/失效）：清登录态回登录页
        clearTokens()
        if (window.location.pathname !== '/login') {
          window.location.href = '/login'
        }
        return Promise.reject(err)
      }
    } else if (!response || response.status >= 500) {
      // 502/503/504/网络错误：后端可能正在部署/重启
      alert('服务暂时不可用（可能正在部署/重启），请稍后重试')
    }
    return Promise.reject(err)
  }
)

export function login(username, password) {
  return http.post('/auth/login', { username, password })
}

export function fetchMenus() {
  return http.get('/menus')
}

export default http
