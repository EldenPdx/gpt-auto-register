import http, { setCsrfToken } from './request'

export async function getSession() {
  const result = await http.get('/api/auth/session')
  setCsrfToken(result.csrf_token)
  return result
}

export async function login(payload) {
  const result = await http.post('/api/auth/login', payload)
  setCsrfToken(result.csrf_token)
  return result
}

export async function logout() {
  await http.post('/api/auth/logout')
  setCsrfToken('')
}

export async function changePassword(payload) {
  const result = await http.post('/api/auth/password', payload)
  setCsrfToken('')
  return result
}
