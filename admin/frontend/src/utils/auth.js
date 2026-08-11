const TOKEN_KEY = 'admin_token'
const REFRESH_KEY = 'admin_refresh_token'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setTokens(token, refreshToken) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(REFRESH_KEY, refreshToken)
}

export function clearTokens() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(REFRESH_KEY)
}
