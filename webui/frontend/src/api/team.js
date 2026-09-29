import http from './request'

const LONG_REQUEST = { timeout: 600000 }
const part = (value) => encodeURIComponent(value)
const base = (id) => `/api/team/workspaces/${part(id)}`

export const listTeamWorkspaces = () => http.get('/api/team/workspaces')
export const saveTeamWorkspace = (payload) => http.post('/api/team/workspaces', payload)
export const getTeamWorkspace = (id) => http.get(base(id))
export const deleteTeamWorkspace = (id) => http.delete(base(id))

export const listTeamAccounts = (id) => http.get(`${base(id)}/accounts`, LONG_REQUEST)
export const listTeamMembers = (id) => http.get(`${base(id)}/members`, LONG_REQUEST)
export const changeTeamMemberSeat = (id, userId, seatType, expectedSeatType) =>
  http.patch(`${base(id)}/members/${part(userId)}/seat`, {
    seat_type: seatType,
    expected_seat_type: expectedSeatType,
  }, LONG_REQUEST)
export const listTeamInvites = (id) => http.get(`${base(id)}/invites`, LONG_REQUEST)
export const getTeamSnapshot = (id) => http.get(`${base(id)}/snapshot`, LONG_REQUEST)
export const listSub2apiGroups = (id) => http.get(`${base(id)}/sub2api/groups`, LONG_REQUEST)
export const reuseExportSub2apiConfig = (id) =>
  http.post(`${base(id)}/sub2api/reuse-export-config`, {}, LONG_REQUEST)

export const sendTeamInvites = (id, emails) =>
  http.post(`${base(id)}/invites`, { emails }, LONG_REQUEST)
export const deleteTeamInvite = (id, email) =>
  http.delete(`${base(id)}/invites`, { ...LONG_REQUEST, data: { email } })
export const acceptTeamInvite = (id, inviteId) =>
  http.patch(`${base(id)}/invites/${part(inviteId)}`, { accept_request: true }, LONG_REQUEST)
export const autoJoinTeam = (id, emails) =>
  http.post(`${base(id)}/auto_join`, { emails }, LONG_REQUEST)

export const oauthTeamAccount = (id, email) =>
  http.post(`${base(id)}/accounts/${part(email)}/oauth`, {}, LONG_REQUEST)
export const probeTeamAccountUsage = (id, email) =>
  http.post(`${base(id)}/accounts/${part(email)}/usage`, {}, LONG_REQUEST)
export const boardTeamAccounts = (id, emails) =>
  http.post(`${base(id)}/board`, { emails }, LONG_REQUEST)
export const pushTeamMembersToSub2api = (id, accounts) =>
  http.post(`${base(id)}/members/push-sub2api`, { accounts }, LONG_REQUEST)
export const submitTeamMemberLoginCredentials = (id, credentials) =>
  http.post(`${base(id)}/members/login-credentials`, credentials, LONG_REQUEST)
export const offboardTeamAccounts = (id, accounts) =>
  http.post(`${base(id)}/offboard`, { accounts }, LONG_REQUEST)
export const testTeamSub2api = (id) =>
  http.post(`${base(id)}/sub2api/test`, {}, LONG_REQUEST)
