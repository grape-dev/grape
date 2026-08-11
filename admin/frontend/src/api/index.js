import axios from 'axios'
import { getToken, clearTokens } from '../utils/auth'

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

http.interceptors.response.use(
  (resp) => resp,
  (err) => {
    if (err.response && err.response.status === 401) {
      clearTokens()
      if (window.location.pathname !== '/login') {
        window.location.href = '/login'
      }
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
