<script setup>
import { computed, onActivated, reactive, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  acceptTeamInvite,
  autoJoinTeam,
  boardTeamAccounts,
  changeTeamMemberSeat,
  deleteTeamInvite,
  deleteTeamWorkspace,
  getTeamSnapshot,
  getTeamWorkspace,
  listSub2apiGroups,
  listTeamAccounts,
  listTeamInvites,
  listTeamMembers,
  listTeamWorkspaces,
  oauthTeamAccount,
  offboardTeamAccounts,
  probeTeamAccountUsage,
  pushTeamMembersToSub2api,
  reuseExportSub2apiConfig,
  saveTeamWorkspace,
  sendTeamInvites,
  submitTeamMemberLoginCredentials,
  testTeamSub2api,
} from '@/api/team'
import { updateCredentials } from '@/api/register'
import { useProxyStore } from '@/stores/proxy'
import StatusDot from '@/components/StatusDot.vue'

const SECRET_FIELDS = ['owner_access_token', 'owner_session_token', 'proxy', 'sub2api_api_key']
const { list: proxyList } = storeToRefs(useProxyStore())
const FORM_FIELDS = [
  'name', 'workspace_id', 'owner_email', 'owner_access_token', 'owner_session_token',
  'owner_device_id', 'proxy', 'client_build_number', 'client_version', 'sub2api_url',
  'sub2api_api_key', 'sub2api_timeout',
]

function blankWorkspace() {
  return {
    id: null,
    name: '',
    workspace_id: '',
    owner_email: '',
    owner_access_token: '',
    owner_session_token: '',
    owner_device_id: '',
    proxy: '',
    client_build_number: '',
    client_version: '',
    sub2api_url: '',
    sub2api_api_key: '',
    sub2api_timeout: 30,
    import_owner_credentials: false,
  }
}

const workspaces = ref([])
const selectedWorkspaceId = ref(null)
const currentWorkspace = ref(null)
const snapshot = ref({})
const accounts = ref([])
const accountsLoaded = ref(false)
const members = ref([])
const invites = ref([])
const sub2apiGroups = ref([])
const selectedAccounts = ref([])
const selectedMembers = ref([])
const selectedInvites = ref([])
const listLoading = ref(false)
const detailLoading = ref(false)
const busy = reactive({})

const workspaceDialog = ref(false)
const workspaceSaving = ref(false)
const workspaceForm = reactive(blankWorkspace())
const savedSecrets = reactive({})

const credentialDialog = ref(false)
const credentialSaving = ref(false)
const credentialForm = reactive({
  email: '',
  updatePassword: false,
  password: '',
  updateTotp: false,
  totp_secret: '',
})
const remoteOauthDialog = ref(false)
const seatDialog = ref(false)
const seatForm = reactive({ workspaceId: null, userId: '', email: '', expected: '', target: '' })
const remoteOauthForm = reactive({
  email: '',
  access_token: '',
  refresh_token: '',
  id_token: '',
})
const remoteLoginDialog = ref(false)
const remoteLoginForm = reactive({
  email: '',
  openai_password: '',
  totp_secret: '',
  mailbox_password: '',
  mail_client_id: '',
  mail_refresh_token: '',
})

const inviteEmails = ref('')
const operationResults = ref([])
let resultId = 0
let detailGeneration = 0

const sub2apiForm = reactive({ url: '', apiKey: '', groupIds: [], timeout: 30 })
const sub2apiKeySaved = ref(false)
const currentId = computed(() => currentWorkspace.value?.id || selectedWorkspaceId.value)
const localEmails = computed(() => new Set(accounts.value.map(emailOf).map((email) => email.toLowerCase())))
const joinableInviteEmails = computed(() => [...new Set(
  selectedInvites.value.filter(canAutoJoinInvite).map(emailOf)
    .map((email) => email.toLowerCase()).filter(Boolean),
)])
const hasManualInvites = computed(() => selectedInvites.value.some((row) => !canAutoJoinInvite(row)))

function itemsOf(value) {
  if (Array.isArray(value)) return value
  return value?.items || value?.data?.items || []
}

function workspaceOf(value) {
  return value?.data || value?.workspace || value?.item || value || null
}

function valueOf(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== '') ?? '—'
}

function emailOf(row) {
  return row?.email || row?.email_address || row?.user?.email || ''
}

function userIdOf(row) {
  return row?.id || row?.user_id || ''
}

const seatOptions = computed(() => {
  const options = [
    { value: 'default', label: '标准席位（Standard）' },
    { value: 'prolite', label: '高级席位（Premium）' },
  ]
  if (seatForm.expected === 'usage_based' || members.value.some((row) => row.seat_type === 'usage_based')) {
    options.push({ value: 'usage_based', label: 'Codex 席位（usage_based）' })
  }
  return options
})

function seatLabel(value) {
  return seatOptions.value.find((option) => option.value === value)?.label || value || '—'
}

function canAutoJoinInvite(row) {
  return localEmails.value.has(emailOf(row).toLowerCase())
}

function sub2apiStatus(row) {
  const status = row?.sub2api?.status
  return {
    ready: { label: 'Sub2API 已推送', type: 'success' },
    missing: { label: 'Sub2API 未推送', type: 'info' },
    unavailable: { label: 'Sub2API 已存在但不可用', type: 'warning' },
    unknown: { label: 'Sub2API 未知', type: 'warning' },
  }[status] || { label: 'Sub2API 未知', type: 'warning' }
}

function formatTime(value) {
  if (!value) return '—'
  const date = new Date(typeof value === 'number' ? value * 1000 : value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false })
}

function parseEmails(text) {
  return [...new Set(String(text || '').split(/[\s,;，；]+/).map((item) => item.trim().toLowerCase()).filter(Boolean))]
}

function errorText(error) {
  const detail = error?.data?.detail
  const upstreamErrors = detail?.detail?.detail
  if (typeof upstreamErrors === 'string') return upstreamErrors
  if (Array.isArray(upstreamErrors) && upstreamErrors.length) {
    return upstreamErrors.map((item) => item.msg).filter(Boolean).join('；')
  }
  return (typeof detail === 'string' ? detail : detail?.message)
    || error?.data?.message || error?.message || '请求失败'
}

async function confirmAction(message, title = '确认操作') {
  try {
    await ElMessageBox.confirm(message, title, {
      type: 'warning',
      confirmButtonText: '确定',
      cancelButtonText: '取消',
    })
    return true
  } catch (_) {
    return false
  }
}

function normalizeResults(response, fallbackEmail = '') {
  const raw = response?.results
  if (Array.isArray(raw)) return raw
  if (raw && typeof raw === 'object') {
    return Object.entries(raw).map(([email, value]) => (
      value && typeof value === 'object' ? { email, ...value } : { email, message: String(value) }
    ))
  }
  if (!fallbackEmail) return []
  return [{
    email: fallbackEmail,
    ok: response?.ok !== false,
    status: response?.status || response?.state || '',
    message: response?.message || response?.error || '完成',
  }]
}

function rememberResults(title, response, fallbackEmail = '') {
  const results = normalizeResults(response, fallbackEmail)
  if (!results.length) return
  operationResults.value.unshift({ id: ++resultId, title, results })
  operationResults.value = operationResults.value.slice(0, 8)
}

async function runOperation(key, title, request, fallbackEmail = '') {
  busy[key] = true
  try {
    const response = await request()
    rememberResults(title, response, fallbackEmail)
    const results = normalizeResults(response, fallbackEmail)
    const ok = response?.ok !== false && results.every(resultOk)
    if (ok) ElMessage.success(`${title}完成`)
    else ElMessage.warning(`${title}已结束，但存在失败项`)
    await loadDetail()
    return ok
  } catch (error) {
    ElMessage.error(`${title}失败：${errorText(error)}`)
    return false
  } finally {
    busy[key] = false
  }
}

async function loadWorkspaces(preferredId = selectedWorkspaceId.value) {
  listLoading.value = true
  try {
    workspaces.value = itemsOf(await listTeamWorkspaces())
    const preferred = workspaces.value.find((item) => String(item.id) === String(preferredId))
    selectedWorkspaceId.value = preferred?.id ?? workspaces.value[0]?.id ?? null
    if (selectedWorkspaceId.value) await loadDetail()
    else clearDetail()
  } catch (error) {
    ElMessage.error(errorText(error))
  } finally {
    listLoading.value = false
  }
}

function clearDetail() {
  detailGeneration += 1
  detailLoading.value = false
  currentWorkspace.value = null
  snapshot.value = {}
  accounts.value = []
  accountsLoaded.value = false
  members.value = []
  invites.value = []
  sub2apiGroups.value = []
}

async function loadDetail() {
  const generation = ++detailGeneration
  const id = selectedWorkspaceId.value
  if (!id) return clearDetail()
  detailLoading.value = true
  accountsLoaded.value = false
  selectedAccounts.value = []
  selectedMembers.value = []
  selectedInvites.value = []
  try {
    const workspace = workspaceOf(await getTeamWorkspace(id))
    const requests = await Promise.allSettled([
      getTeamSnapshot(id),
      listTeamAccounts(id),
      listTeamMembers(id),
      listTeamInvites(id),
      listSub2apiGroups(id),
    ])
    if (generation !== detailGeneration || String(id) !== String(selectedWorkspaceId.value)) return
    currentWorkspace.value = workspace
    snapshot.value = requests[0].status === 'fulfilled'
      ? (requests[0].value?.snapshot || requests[0].value || {}) : {}
    accounts.value = requests[1].status === 'fulfilled' ? itemsOf(requests[1].value) : []
    accountsLoaded.value = requests[1].status === 'fulfilled'
    members.value = requests[2].status === 'fulfilled' ? itemsOf(requests[2].value) : []
    invites.value = requests[3].status === 'fulfilled' ? itemsOf(requests[3].value) : []
    sub2apiGroups.value = requests[4].status === 'fulfilled'
      ? (requests[4].value?.groups || itemsOf(requests[4].value)) : []
    const failures = requests.filter((result) => result.status === 'rejected')
    if (failures.length) {
      ElMessage.warning(`${failures.length} 项远端数据加载失败：${errorText(failures[0].reason)}`)
    }
    fillSub2apiForm()
  } catch (error) {
    if (generation === detailGeneration) {
      clearDetail()
      ElMessage.error(errorText(error))
    }
  } finally {
    if (generation === detailGeneration) detailLoading.value = false
  }
}

function selectWorkspace(id) {
  seatDialog.value = false
  selectedWorkspaceId.value = id
  loadDetail()
}

function openCreateWorkspace() {
  Object.assign(workspaceForm, blankWorkspace())
  Object.keys(savedSecrets).forEach((key) => delete savedSecrets[key])
  workspaceDialog.value = true
}

function openEditWorkspace(workspace = currentWorkspace.value) {
  if (!workspace) return
  Object.assign(workspaceForm, blankWorkspace(), { id: workspace.id })
  for (const field of FORM_FIELDS) {
    if (!SECRET_FIELDS.includes(field)) workspaceForm[field] = workspace[field] ?? workspaceForm[field]
  }
  for (const field of SECRET_FIELDS) {
    workspaceForm[field] = ''
    savedSecrets[field] = Boolean(workspace[field])
  }
  workspaceDialog.value = true
}

function workspacePayload() {
  const payload = {
    id: workspaceForm.id || undefined,
    import_owner_credentials: Boolean(workspaceForm.import_owner_credentials),
  }
  for (const field of FORM_FIELDS) {
    const value = typeof workspaceForm[field] === 'string'
      ? workspaceForm[field].trim() : workspaceForm[field]
    payload[field] = SECRET_FIELDS.includes(field) && workspaceForm.id && !value ? '***' : value
  }
  return payload
}

async function saveWorkspace() {
  const missing = []
  if (!workspaceForm.name.trim()) missing.push('名称')
  if (!workspaceForm.workspace_id.trim()) missing.push('Workspace ID')
  if (!workspaceForm.owner_email.trim()) missing.push('Owner 邮箱')
  if (
    !workspaceForm.import_owner_credentials &&
    !workspaceForm.owner_access_token.trim() &&
    !savedSecrets.owner_access_token
  ) missing.push('Owner Access Token')
  if (missing.length) {
    ElMessage.warning(`请填写必填项：${missing.join('、')}`)
    return
  }
  workspaceSaving.value = true
  try {
    const response = await saveTeamWorkspace(workspacePayload())
    const saved = workspaceOf(response)
    workspaceDialog.value = false
    ElMessage.success('工作空间已保存')
    await loadWorkspaces(saved?.id || workspaceForm.id)
  } catch (error) {
    ElMessage.error(errorText(error))
  } finally {
    workspaceSaving.value = false
  }
}

async function removeWorkspace() {
  const workspace = currentWorkspace.value
  if (!workspace || !(await confirmAction(
    `删除工作空间「${workspace.name || workspace.workspace_id}」的本地配置？此操作不可恢复。`,
    '删除工作空间',
  ))) return
  busy.deleteWorkspace = true
  try {
    await deleteTeamWorkspace(workspace.id)
    ElMessage.success('工作空间已删除')
    selectedWorkspaceId.value = null
    await loadWorkspaces()
  } catch (error) {
    ElMessage.error(errorText(error))
  } finally {
    busy.deleteWorkspace = false
  }
}

async function sendInvites() {
  const emails = parseEmails(inviteEmails.value)
  if (!emails.length) return ElMessage.warning('请输入邀请邮箱')
  if (!(await confirmAction(`向 ${emails.length} 个邮箱发送 Team 邀请？`, '批量邀请'))) return
  const ok = await runOperation('invite', '批量邀请', () => sendTeamInvites(currentId.value, emails))
  if (ok) inviteEmails.value = ''
}

async function autoJoin() {
  const emails = joinableInviteEmails.value
  if (!emails.length) return ElMessage.warning('请从邀请列表选择本地账号')
  if (!(await confirmAction(`让 ${emails.length} 个账号自动加入当前工作空间？`, '全自动加入'))) return
  await runOperation('autoJoin', '全自动加入', () => autoJoinTeam(currentId.value, emails))
}

async function removeInvite(row) {
  const email = emailOf(row)
  if (!email || !(await confirmAction(`删除 ${email} 的邀请或加入请求？`, '删除邀请'))) return
  await runOperation(`invite-delete:${email}`, '删除邀请', () => deleteTeamInvite(currentId.value, email), email)
}

async function acceptInvite(row) {
  const inviteId = row.invite_id || row.id
  if (!inviteId) return ElMessage.warning('缺少 invite_id')
  await runOperation(`invite-accept:${inviteId}`, '接受加入请求', () => acceptTeamInvite(currentId.value, inviteId), emailOf(row))
}

function canAccept(row) {
  const marker = String(row.kind || row.type || row.status || '')
  return Boolean((row.invite_id || row.id) && (row.is_request || row.requested || /request/i.test(marker)))
}

function roleOf(row) {
  const role = String(
    row.role || row.account_user_role || row.user?.role || row.user?.account_user_role
    || row.account?.role || row.account?.account_user_role || '',
  ).toLowerCase()
  if (!role) return 'unknown'
  if (role.includes('owner')) return 'owner'
  if (role === 'admin' || role.includes('admin')) return 'admin'
  if (role.includes('member') || role.includes('user')) return 'member'
  return 'unknown'
}

function openSeatDialog(row) {
  const userId = userIdOf(row)
  const current = String(row?.seat_type || '')
  if (!userId || !current) return ElMessage.warning('缺少成员 ID 或当前席位类型')
  Object.assign(seatForm, {
    workspaceId: currentId.value, userId, email: emailOf(row), expected: current, target: current,
  })
  seatDialog.value = true
}

async function saveMemberSeat() {
  if (seatForm.target === seatForm.expected) return ElMessage.info('席位类型未变化')
  const confirmed = await confirmAction(
    `将 ${seatForm.email} 的席位由 ${seatLabel(seatForm.expected)} 改为 ${seatLabel(seatForm.target)}？请先在远端核对可用席位及可能产生的费用。`,
    '确认远端席位调整',
  )
  if (!confirmed) return
  busy.seatChange = true
  try {
    await changeTeamMemberSeat(
      seatForm.workspaceId, seatForm.userId, seatForm.target, seatForm.expected,
    )
    ElMessage.success('远端席位已更新并核实')
  } catch (error) {
    const message = errorText(error)
    if (error?.data?.detail?.category === 'verification_incomplete') {
      ElMessage.warning(`请求结果待核实：${message}`)
    } else {
      ElMessage.error(`调整席位失败：${message}`)
    }
  } finally {
    seatDialog.value = false
    await loadDetail()
    busy.seatChange = false
  }
}

function roleType(row) {
  return { owner: 'danger', admin: 'warning', member: 'info', unknown: 'danger' }[roleOf(row)]
}

function roleLabel(row) {
  return { owner: '所有者', admin: '管理员', member: '普通成员', unknown: '身份未知' }[roleOf(row)]
}

function platformPresence(row) {
  if (row.platform_presence === 'remote_only' || row.platform_presence === 'shared') {
    return row.platform_presence
  }
  if (!accountsLoaded.value || !emailOf(row)) return 'unknown'
  return localEmails.value.has(emailOf(row).toLowerCase()) ? 'shared' : 'remote_only'
}

function canOffboard(row) {
  return ['admin', 'member'].includes(roleOf(row)) && Boolean(emailOf(row) && userIdOf(row))
    && emailOf(row).toLowerCase() !== String(currentWorkspace.value?.owner_email || '').toLowerCase()
}

async function offboardSelected() {
  const targets = selectedMembers.value.filter(canOffboard)
    .map((row) => ({ email: emailOf(row), user_id: userIdOf(row) }))
  if (!targets.length) return ElMessage.warning('请选择可下车成员')
  if (!(await confirmAction(`让选中的 ${targets.length} 个成员退出当前工作空间？`, '一键下车'))) return
  await runOperation('offboard', '一键下车', () => offboardTeamAccounts(currentId.value, targets))
}

async function pushMembersToSub2api() {
  const emails = [...new Set(selectedMembers.value.map(emailOf).filter(Boolean))]
  if (!emails.length) return ElMessage.warning('请选择远端成员')
  const localEmails = new Set(accounts.value.map(emailOf).map((email) => email.toLowerCase()))
  const remoteOnly = emails.filter((email) => !localEmails.has(email.toLowerCase()))
  if (remoteOnly.length) {
    if (emails.length !== 1) {
      ElMessage.warning('远端独有成员需要补充 OAuth 凭据，请每次单独选择一个后推送')
      return
    }
    Object.assign(remoteOauthForm, {
      email: remoteOnly[0], access_token: '', refresh_token: '', id_token: '',
    })
    remoteOauthDialog.value = true
    return
  }
  if (!(await confirmAction(
    `将选中的 ${emails.length} 个成员推送到当前工作空间配置的 Sub2API 分组？`,
    '推送至 Sub2API',
  ))) return
  await runOperation(
    'pushSub2api', '推送至 Sub2API',
    () => pushTeamMembersToSub2api(currentId.value, emails.map((email) => ({ email }))),
  )
}

async function pushRemoteMemberWithOauth() {
  const credentials = {
    email: remoteOauthForm.email,
    access_token: remoteOauthForm.access_token.trim(),
    refresh_token: remoteOauthForm.refresh_token.trim(),
    id_token: remoteOauthForm.id_token.trim(),
  }
  if (!credentials.access_token || !credentials.refresh_token || !credentials.id_token) {
    ElMessage.warning('请完整填写 Access Token、Refresh Token 和 ID Token')
    return
  }
  const ok = await runOperation(
    'remoteOauthPush', '推送至 Sub2API',
    () => pushTeamMembersToSub2api(currentId.value, [credentials]),
  )
  if (ok) remoteOauthDialog.value = false
}

function clearRemoteOauthForm() {
  Object.assign(remoteOauthForm, {
    email: '', access_token: '', refresh_token: '', id_token: '',
  })
}

function openRemoteLogin(row) {
  Object.assign(remoteLoginForm, {
    email: emailOf(row), openai_password: '', totp_secret: '', mailbox_password: '',
    mail_client_id: '', mail_refresh_token: '',
  })
  remoteLoginDialog.value = true
}

function clearRemoteLoginForm() {
  for (const field of Object.keys(remoteLoginForm)) remoteLoginForm[field] = ''
}

async function submitRemoteLogin() {
  const payload = {
    ...remoteLoginForm,
    email: remoteLoginForm.email.trim(),
    totp_secret: remoteLoginForm.totp_secret.trim(),
    mail_client_id: remoteLoginForm.mail_client_id.trim(),
    mail_refresh_token: remoteLoginForm.mail_refresh_token.trim(),
  }
  const ok = await runOperation(
    'remoteLogin', '补充凭据并自动上车',
    () => submitTeamMemberLoginCredentials(currentId.value, payload), payload.email,
  )
  if (ok) remoteLoginDialog.value = false
}

async function boardSelected() {
  const emails = [...new Set(selectedAccounts.value.map(emailOf).filter(Boolean))]
  if (!emails.length) return ElMessage.warning('请选择本地账号')
  if (!(await confirmAction(`让选中的 ${emails.length} 个账号上车？`, '一键上车'))) return
  await runOperation('board', '一键上车', () => boardTeamAccounts(currentId.value, emails))
}

async function runOauth(row) {
  const email = emailOf(row)
  await runOperation(`oauth:${email}`, '手动 OAuth', () => oauthTeamAccount(currentId.value, email), email)
}

async function probeUsage(row) {
  const email = emailOf(row)
  await runOperation(`usage:${email}`, '用量探测', () => probeTeamAccountUsage(currentId.value, email), email)
}

function openCredentials(row) {
  Object.assign(credentialForm, {
    email: emailOf(row),
    updatePassword: false,
    password: '',
    updateTotp: false,
    totp_secret: '',
  })
  credentialDialog.value = true
}

async function saveCredentials() {
  if (!credentialForm.updatePassword && !credentialForm.updateTotp) {
    ElMessage.warning('请勾选要修改的字段')
    return
  }
  if (!(await confirmAction('仅修改本地凭据记录，不会同步 OpenAI。确定保存？', '编辑账号凭据'))) return
  const payload = { email: credentialForm.email }
  if (credentialForm.updatePassword) payload.password = credentialForm.password
  if (credentialForm.updateTotp) payload.totp_secret = credentialForm.totp_secret.trim()
  credentialSaving.value = true
  try {
    await updateCredentials(payload)
    credentialDialog.value = false
    ElMessage.success('本地凭据已保存')
    await loadDetail()
  } catch (error) {
    ElMessage.error(`保存失败：${errorText(error)}`)
  } finally {
    credentialSaving.value = false
  }
}

function credentialPresent(row, field) {
  const direct = row[`has_${field}`]
  if (direct !== undefined) return Boolean(direct)
  return Boolean(row[field] || row[`${field}_len`] > 0)
}

function usageValue(row, field) {
  const value = row.usage?.[field] ?? row[field]
  return value === undefined || value === null || value === '' ? '—' : `${value}%`
}

function fillSub2apiForm() {
  const workspace = currentWorkspace.value || {}
  sub2apiForm.url = workspace.sub2api_url || ''
  sub2apiForm.apiKey = ''
  const selected = String(workspace.sub2api_group_ids || '')
    .split(',').map((id) => id.trim()).filter(Boolean)
  const available = new Set(sub2apiGroups.value.map((group) => String(group.id)))
  sub2apiForm.groupIds = available.size
    ? selected.filter((id) => available.has(id))
    : selected
  sub2apiForm.timeout = Number(workspace.sub2api_timeout || 30)
  sub2apiKeySaved.value = Boolean(workspace.sub2api_api_key)
}

async function saveSub2api() {
  if (!currentWorkspace.value) return
  busy.sub2apiSave = true
  try {
    await saveTeamWorkspace({
      id: currentWorkspace.value.id,
      name: currentWorkspace.value.name,
      workspace_id: currentWorkspace.value.workspace_id,
      sub2api_url: sub2apiForm.url.trim(),
      sub2api_api_key: sub2apiForm.apiKey.trim() || '***',
      sub2api_group_ids: sub2apiForm.groupIds.join(','),
      sub2api_timeout: Number(sub2apiForm.timeout || 30),
    })
    ElMessage.success('当前工作空间的 Sub2API 配置已保存')
    await loadWorkspaces(currentWorkspace.value.id)
  } catch (error) {
    ElMessage.error(errorText(error))
  } finally {
    busy.sub2apiSave = false
  }
}

async function reuseExportSub2api() {
  if (!currentWorkspace.value) return
  busy.sub2apiReuse = true
  try {
    await reuseExportSub2apiConfig(currentId.value)
    ElMessage.success('已复用自动导出的 Sub2API BaseURL 和 API Key')
    await loadWorkspaces(currentId.value)
  } catch (error) {
    ElMessage.error(errorText(error))
  } finally {
    busy.sub2apiReuse = false
  }
}

async function testSub2api() {
  await runOperation('sub2apiTest', '测试 Sub2API', () => testTeamSub2api(currentId.value))
}

function resultOk(row) {
  if (row.ok !== undefined) return Boolean(row.ok)
  if (row.success !== undefined) return Boolean(row.success)
  if (/^(failed|error|blocked)$/i.test(String(row.status || row.state || ''))) return false
  return !row.error
}

function resultMessage(row) {
  const error = row.error
  if (error && typeof error === 'object') return error.message || error.category || '操作失败'
  return row.message || error || row.detail || '—'
}

onActivated(() => loadWorkspaces())
</script>

<template>
  <div class="page team-page">
    <el-tabs model-value="workspaces">
      <el-tab-pane label="Team 工作空间" name="workspaces">
        <div class="page-toolbar">
          <el-space wrap>
            <el-button type="primary" @click="openCreateWorkspace">新增工作空间</el-button>
            <el-button :loading="listLoading" @click="loadWorkspaces()">
              <el-icon><Refresh /></el-icon>刷新
            </el-button>
          </el-space>
          <el-select
            v-model="selectedWorkspaceId" placeholder="选择工作空间" style="width: 280px"
            @change="selectWorkspace"
          >
            <el-option
              v-for="workspace in workspaces" :key="workspace.id"
              :label="workspace.name || workspace.workspace_id" :value="workspace.id"
            />
          </el-select>
        </div>

        <el-skeleton v-if="listLoading && !workspaces.length" :rows="3" animated />
        <el-empty v-else-if="!workspaces.length" description="还没有 Team 工作空间">
          <el-button type="primary" @click="openCreateWorkspace">新增工作空间</el-button>
        </el-empty>
        <div v-else class="workspace-grid">
          <div
            v-for="workspace in workspaces" :key="workspace.id"
            class="workspace-card" :class="{ active: workspace.id === selectedWorkspaceId }"
            role="button" tabindex="0" @click="selectWorkspace(workspace.id)"
            @keydown.enter="selectWorkspace(workspace.id)"
          >
            <div class="workspace-title">{{ workspace.name || workspace.workspace_id }}</div>
            <div class="mono workspace-id">{{ workspace.workspace_id }}</div>
            <div class="workspace-meta">
              <span>{{ workspace.owner_email || '未设置 owner' }}</span>
              <el-tag size="small" :type="workspace.sub2api_url ? 'success' : 'info'">
                Sub2API {{ workspace.sub2api_url ? '已配置' : '未配置' }}
              </el-tag>
            </div>
          </div>
        </div>

        <el-empty v-if="workspaces.length && !currentWorkspace" description="请选择工作空间" />
        <div v-else-if="currentWorkspace" v-loading="detailLoading" class="detail-stack">
          <el-card shadow="never">
            <template #header>
              <div class="card-header">
                <div>
                  <span class="section-title">{{ currentWorkspace.name }}</span>
                  <span class="mono hint">{{ currentWorkspace.workspace_id }}</span>
                </div>
                <el-space>
                  <el-button size="small" @click="openEditWorkspace()">编辑</el-button>
                  <el-button
                    size="small" type="danger" plain :loading="busy.deleteWorkspace"
                    @click="removeWorkspace"
                  >删除</el-button>
                </el-space>
              </div>
            </template>
            <el-descriptions :column="3" border size="small">
              <el-descriptions-item label="Owner">{{ currentWorkspace.owner_email || '—' }}</el-descriptions-item>
              <el-descriptions-item label="套餐">
                {{ valueOf(snapshot.plan_type, snapshot.subscription?.plan_type, currentWorkspace.plan_type) }}
              </el-descriptions-item>
              <el-descriptions-item label="状态">
                <StatusDot
                  :type="snapshot.is_deactivated ? 'danger' : 'success'"
                  :text="snapshot.is_deactivated ? '已停用' : valueOf(snapshot.status, '正常')"
                />
              </el-descriptions-item>
              <el-descriptions-item label="席位">
                {{ valueOf(snapshot.seats_in_use, snapshot.subscription?.seats_in_use, 0) }} /
                {{ valueOf(snapshot.seats_entitled, snapshot.subscription?.seats_entitled, '—') }}
              </el-descriptions-item>
              <el-descriptions-item label="成员">{{ valueOf(snapshot.member_count, members.length) }}</el-descriptions-item>
              <el-descriptions-item label="待处理邀请">{{ valueOf(snapshot.pending_invites, invites.length) }}</el-descriptions-item>
            </el-descriptions>
          </el-card>

          <el-card shadow="never">
            <template #header><span class="section-title">邀请与自动加入</span></template>
            <el-input
              v-model="inviteEmails" type="textarea" :rows="3"
              placeholder="每行一个邮箱，也支持逗号或空格分隔"
            />
            <el-space wrap style="margin: 10px 0 14px">
              <el-button type="primary" :loading="busy.invite" @click="sendInvites">批量发送邀请</el-button>
              <el-button :disabled="!joinableInviteEmails.length" :loading="busy.autoJoin" @click="autoJoin">
                全自动加入 ({{ joinableInviteEmails.length }})
              </el-button>
            </el-space>
            <el-alert
              v-if="hasManualInvites" type="info" :closable="false" style="margin-bottom: 14px"
              title="所选非本地邮箱需账号本人接受邀请并提供 OAuth 凭据，无法全自动加入。"
            />
            <el-table
              :data="invites" size="small" stripe
              @selection-change="(rows) => (selectedInvites = rows)"
            >
              <el-table-column type="selection" width="44" />
              <el-table-column label="邮箱" min-width="220">
                <template #default="{ row }">{{ emailOf(row) }}</template>
              </el-table-column>
              <el-table-column prop="role" label="角色" width="130" />
              <el-table-column prop="seat_type" label="席位类型" width="130" />
              <el-table-column label="状态" width="130">
                <template #default="{ row }">{{ valueOf(row.status, row.kind, row.type) }}</template>
              </el-table-column>
              <el-table-column label="时间" width="175">
                <template #default="{ row }">{{ formatTime(row.created_time || row.created_at) }}</template>
              </el-table-column>
              <el-table-column label="操作" width="190" fixed="right">
                <template #default="{ row }">
                  <el-button
                    v-if="canAccept(row)" size="small" text type="primary"
                    :loading="busy[`invite-accept:${row.invite_id || row.id}`]" @click="acceptInvite(row)"
                  >接受请求</el-button>
                  <el-button
                    size="small" text type="danger"
                    :loading="busy[`invite-delete:${emailOf(row)}`]" @click="removeInvite(row)"
                  >删除</el-button>
                </template>
              </el-table-column>
              <template #empty><el-empty description="暂无邀请或加入请求" :image-size="60" /></template>
            </el-table>
          </el-card>

          <el-card shadow="never">
            <template #header>
              <div class="card-header">
                <span class="section-title">远端成员</span>
                <el-space>
                  <el-button
                    type="primary" plain :disabled="!selectedMembers.length"
                    :loading="busy.pushSub2api" @click="pushMembersToSub2api"
                  >推送至 Sub2API ({{ selectedMembers.length }})</el-button>
                  <el-button
                    type="danger" plain :disabled="!selectedMembers.some(canOffboard)"
                    :loading="busy.offboard" @click="offboardSelected"
                  >一键下车 ({{ selectedMembers.filter(canOffboard).length }})</el-button>
                </el-space>
              </div>
            </template>
            <el-table
              :data="members" size="small" stripe
              @selection-change="(rows) => (selectedMembers = rows)"
            >
              <el-table-column type="selection" width="44" />
              <el-table-column label="邮箱" min-width="220">
                <template #default="{ row }">{{ emailOf(row) }}</template>
              </el-table-column>
              <el-table-column label="账号归属" width="120">
                <template #default="{ row }">
                  <el-tag v-if="platformPresence(row) === 'shared'" size="small" type="success">双端共有</el-tag>
                  <el-tag v-else-if="platformPresence(row) === 'remote_only'" size="small" type="info">远端独有</el-tag>
                  <span v-else>—</span>
                </template>
              </el-table-column>
              <el-table-column label="角色" width="110">
                <template #default="{ row }">
                  <el-tag :type="roleType(row)" size="small">{{ roleLabel(row) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="席位类型" width="180">
                <template #default="{ row }">{{ seatLabel(row.seat_type) }}</template>
              </el-table-column>
              <el-table-column label="user_id" min-width="210" show-overflow-tooltip>
                <template #default="{ row }"><span class="mono">{{ userIdOf(row) }}</span></template>
              </el-table-column>
              <el-table-column label="Sub2API" min-width="160">
                <template #default="{ row }">
                  <el-tag :type="sub2apiStatus(row).type" size="small">
                    {{ sub2apiStatus(row).label }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="230" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" text type="primary" @click="openSeatDialog(row)">调整席位</el-button>
                  <el-button
                    v-if="platformPresence(row) === 'remote_only' || row.login_credentials_supplemented"
                    size="small" text type="primary"
                    @click="openRemoteLogin(row)"
                  >{{ row.login_credentials_supplemented ? '更新登录凭据' : '补充登录凭据' }}</el-button>
                </template>
              </el-table-column>
              <template #empty><el-empty description="暂无成员" :image-size="60" /></template>
            </el-table>
          </el-card>

          <el-card shadow="never">
            <template #header>
              <div class="card-header">
                <span class="section-title">本地账号与运维</span>
                <el-button
                  type="primary" plain :disabled="!selectedAccounts.length"
                  :loading="busy.board" @click="boardSelected"
                >一键上车 ({{ selectedAccounts.length }})</el-button>
              </div>
            </template>
            <el-table
              :data="accounts" size="small" stripe
              @selection-change="(rows) => (selectedAccounts = rows)"
            >
              <el-table-column type="selection" width="44" />
              <el-table-column label="邮箱" min-width="220">
                <template #default="{ row }">{{ emailOf(row) }}</template>
              </el-table-column>
              <el-table-column label="本地凭据" min-width="210">
                <template #default="{ row }">
                  <el-space :size="4" wrap>
                    <el-tag v-if="credentialPresent(row, 'password')" size="small" type="info">密码</el-tag>
                    <el-tag v-if="credentialPresent(row, 'totp_secret')" size="small" type="warning">2FA</el-tag>
                    <el-tag v-if="credentialPresent(row, 'access_token')" size="small" type="success">AT</el-tag>
                    <el-tag v-if="credentialPresent(row, 'refresh_token')" size="small" type="primary">RT</el-tag>
                  </el-space>
                </template>
              </el-table-column>
              <el-table-column prop="status" label="状态" width="120" />
              <el-table-column label="5h 用量" width="90">
                <template #default="{ row }">{{ usageValue(row, 'pct_5h') }}</template>
              </el-table-column>
              <el-table-column label="7d 用量" width="90">
                <template #default="{ row }">{{ usageValue(row, 'pct_7d') }}</template>
              </el-table-column>
              <el-table-column label="用量状态" width="140">
                <template #default="{ row }">{{ valueOf(row.usage?.status, row.usage_status) }}</template>
              </el-table-column>
              <el-table-column label="操作" width="260" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" text @click="openCredentials(row)">编辑凭据</el-button>
                  <el-button
                    size="small" text type="primary" :loading="busy[`oauth:${emailOf(row)}`]"
                    @click="runOauth(row)"
                  >OAuth</el-button>
                  <el-button
                    size="small" text :loading="busy[`usage:${emailOf(row)}`]"
                    @click="probeUsage(row)"
                  >用量</el-button>
                </template>
              </el-table-column>
              <template #empty><el-empty description="暂无本地注册账号" :image-size="60" /></template>
            </el-table>
            <p class="hint usage-note">
              5h 与 7d 窗口原样展示；前端不把 5h 满自行判定为终态。
            </p>
          </el-card>

          <el-card shadow="never">
            <template #header>
              <div class="card-header">
                <span class="section-title">当前工作空间 · 独立 Sub2API 配置</span>
                <el-button :loading="busy.sub2apiReuse" @click="reuseExportSub2api">
                  复用自动导出 BaseURL / API Key
                </el-button>
              </div>
            </template>
            <el-form label-position="top" class="sub2api-form">
              <el-form-item label="Sub2API URL">
                <el-input v-model="sub2apiForm.url" placeholder="https://sub2api.example.com/api/v1" />
              </el-form-item>
              <el-form-item label="API Key">
                <el-input
                  v-model="sub2apiForm.apiKey" type="password"
                  :placeholder="sub2apiKeySaved ? '已设置（留空或 *** 不修改）' : '输入 API Key'"
                />
              </el-form-item>
              <el-form-item label="分组（从 Sub2API 已有分组读取）">
                <el-select
                  v-model="sub2apiForm.groupIds" multiple filterable clearable
                  collapse-tags collapse-tags-tooltip
                  placeholder="请选择一个或多个分组" style="width: 100%"
                >
                  <el-option
                    v-for="group in sub2apiGroups" :key="group.id"
                    :label="`${group.name || group.id} (${group.id})`"
                    :value="String(group.id)"
                  />
                </el-select>
                <div class="hint">
                  {{ sub2apiGroups.length ? '分组列表来自 GET /api/v1/admin/groups。' : '请先保存或复用 BaseURL/API Key，再读取分组。' }}
                </div>
              </el-form-item>
              <el-form-item label="超时（秒）">
                <el-input-number v-model="sub2apiForm.timeout" :min="5" :max="600" />
              </el-form-item>
              <el-space>
                <el-button type="primary" :loading="busy.sub2apiSave" @click="saveSub2api">保存独立配置</el-button>
                <el-button :loading="busy.sub2apiTest" @click="testSub2api">测试已保存配置</el-button>
              </el-space>
            </el-form>
          </el-card>

          <el-card v-if="operationResults.length" shadow="never">
            <template #header><span class="section-title">最近逐账号结果</span></template>
            <el-collapse>
              <el-collapse-item
                v-for="operation in operationResults" :key="operation.id"
                :title="`${operation.title} · ${operation.results.length} 项`" :name="operation.id"
              >
                <el-table :data="operation.results" size="small" stripe>
                  <el-table-column label="账号" min-width="220">
                    <template #default="{ row }">{{ emailOf(row) || row.user_id || '—' }}</template>
                  </el-table-column>
                  <el-table-column label="结果" width="100">
                    <template #default="{ row }">
                      <StatusDot :type="resultOk(row) ? 'success' : 'danger'" :text="resultOk(row) ? '成功' : '失败'" />
                    </template>
                  </el-table-column>
                  <el-table-column prop="status" label="状态" width="150" />
                  <el-table-column label="说明" min-width="260" show-overflow-tooltip>
                    <template #default="{ row }">{{ resultMessage(row) }}</template>
                  </el-table-column>
                </el-table>
              </el-collapse-item>
            </el-collapse>
          </el-card>
        </div>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="seatDialog" :title="`调整远端席位 · ${seatForm.email}`" width="520px">
      <el-alert
        title="将直接修改远端成员席位。平台不核对已购买容量，升级可能产生费用；请先在 OpenAI 工作空间确认可用席位。"
        type="warning" :closable="false" show-icon style="margin-bottom: 16px"
      />
      <el-form label-position="top">
        <el-form-item label="当前席位">{{ seatLabel(seatForm.expected) }}</el-form-item>
        <el-form-item label="调整为" required>
          <el-select v-model="seatForm.target" style="width: 100%">
            <el-option
              v-for="option in seatOptions" :key="option.value"
              :label="option.label" :value="option.value"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="seatDialog = false">取消</el-button>
        <el-button type="primary" :loading="busy.seatChange" :disabled="seatForm.target === seatForm.expected" @click="saveMemberSeat">应用到远端</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="workspaceDialog" :title="workspaceForm.id ? '编辑 Team 工作空间' : '新增 Team 工作空间'"
      width="760px" top="5vh"
    >
      <el-form label-position="top" class="workspace-form">
        <p class="form-required-note"><span>*</span> 为必填项；其余字段均标注为选填。</p>
        <el-row :gutter="16">
          <el-col :span="12"><el-form-item label="名称" required><el-input v-model="workspaceForm.name" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="Workspace ID" required><el-input v-model="workspaceForm.workspace_id" class="mono" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="Owner 邮箱" required><el-input v-model="workspaceForm.owner_email" /></el-form-item></el-col>
          <el-col :span="12">
            <el-form-item label="导入母号（选填）">
              <el-checkbox v-model="workspaceForm.import_owner_credentials">
                从本地注册结果按 Owner 邮箱导入凭据
              </el-checkbox>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="代理（选填）">
              <el-select
                v-model="workspaceForm.proxy" filterable clearable
                :placeholder="savedSecrets.proxy ? '已设置；选择新代理可替换' : '从平台代理池选择'"
                style="width: 100%"
              >
                <el-option v-for="proxy in proxyList" :key="proxy" :label="proxy" :value="proxy" />
              </el-select>
              <div class="hint">代理来源于当前平台「代理池」；请先在代理池中维护。</div>
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item
              label="Owner Access Token"
              :required="!workspaceForm.import_owner_credentials && !savedSecrets.owner_access_token"
            >
              <el-input v-model="workspaceForm.owner_access_token" type="password" :placeholder="savedSecrets.owner_access_token ? '已设置（留空或 *** 不修改）' : '输入 access token'" />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="Owner Session Token（选填）">
              <el-input v-model="workspaceForm.owner_session_token" type="password" :placeholder="savedSecrets.owner_session_token ? '已设置（留空或 *** 不修改）' : '输入 session token'" />
            </el-form-item>
          </el-col>
          <el-col :span="12"><el-form-item label="Owner Device ID（选填）"><el-input v-model="workspaceForm.owner_device_id" class="mono" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="Client Build Number（选填）"><el-input v-model="workspaceForm.client_build_number" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="Client Version（选填）"><el-input v-model="workspaceForm.client_version" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="Sub2API 超时（秒，选填）"><el-input-number v-model="workspaceForm.sub2api_timeout" :min="5" :max="600" style="width: 100%" /></el-form-item></el-col>
          <el-col :span="24"><el-form-item label="Sub2API URL（选填）"><el-input v-model="workspaceForm.sub2api_url" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="Sub2API API Key（选填）"><el-input v-model="workspaceForm.sub2api_api_key" type="password" :placeholder="savedSecrets.sub2api_api_key ? '已设置（留空或 *** 不修改）' : '输入 API Key'" /></el-form-item></el-col>
          <el-col :span="12">
            <el-form-item label="Sub2API 分组（选填）">
              <span class="hint">保存工作空间后，从 Sub2API 已有分组中选择。</span>
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button @click="workspaceDialog = false">取消</el-button>
        <el-button type="primary" :loading="workspaceSaving" @click="saveWorkspace">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="credentialDialog" :title="`编辑本地凭据 · ${credentialForm.email}`" width="560px">
      <el-alert
        title="这里只修改本地记录，不会同步 OpenAI，也不会显示已保存的完整凭据。"
        type="warning" :closable="false" show-icon style="margin-bottom: 16px"
      />
      <el-form label-position="top">
        <el-form-item>
          <el-checkbox v-model="credentialForm.updatePassword">修改密码</el-checkbox>
          <el-input
            v-model="credentialForm.password" :disabled="!credentialForm.updatePassword"
            type="password" placeholder="勾选后留空表示清空密码"
          />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="credentialForm.updateTotp">修改 TOTP Secret</el-checkbox>
          <el-input
            v-model="credentialForm.totp_secret" :disabled="!credentialForm.updateTotp"
            type="password" placeholder="勾选后留空表示清空 TOTP"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="credentialDialog = false">取消</el-button>
        <el-button type="primary" :loading="credentialSaving" @click="saveCredentials">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="remoteOauthDialog" :title="`远端成员 OAuth · ${remoteOauthForm.email}`"
      width="640px" @closed="clearRemoteOauthForm"
    >
      <el-alert
        title="该成员不在本地账号库。远端成员接口不提供 OAuth 密钥，请补充该成员自己的凭据；凭据仅用于本次验证和推送，不会保存到本地账号库。"
        type="warning" :closable="false" show-icon style="margin-bottom: 16px"
      />
      <el-form label-position="top">
        <el-form-item label="工作区 Access Token" required>
          <el-input v-model="remoteOauthForm.access_token" type="password" placeholder="该成员在当前工作空间的 Access Token" />
        </el-form-item>
        <el-form-item label="Refresh Token" required>
          <el-input v-model="remoteOauthForm.refresh_token" type="password" />
        </el-form-item>
        <el-form-item label="ID Token" required>
          <el-input v-model="remoteOauthForm.id_token" type="password" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="remoteOauthDialog = false">取消</el-button>
        <el-button type="primary" :loading="busy.remoteOauthPush" @click="pushRemoteMemberWithOauth">
          验证并推送
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="remoteLoginDialog" :title="`补充登录凭据 · ${remoteLoginForm.email}`"
      width="640px" @closed="clearRemoteLoginForm"
    >
      <el-alert
        title="提交后保存凭据并自动 OAuth、上车。已有凭据的字段可留空；首次补充需填写 Outlook Client ID 和邮箱 Refresh Token。"
        type="info" :closable="false" show-icon style="margin-bottom: 16px"
      />
      <el-form label-position="top">
        <el-form-item label="OpenAI 密码（按账号需要填写）">
          <el-input v-model="remoteLoginForm.openai_password" type="password" autocomplete="off" />
        </el-form-item>
        <el-form-item label="TOTP Secret（按账号需要填写）">
          <el-input v-model="remoteLoginForm.totp_secret" type="password" autocomplete="off" />
        </el-form-item>
        <el-form-item label="Outlook 邮箱密码（选填）">
          <el-input v-model="remoteLoginForm.mailbox_password" type="password" autocomplete="off" />
        </el-form-item>
        <el-form-item label="Outlook Client ID">
          <el-input v-model="remoteLoginForm.mail_client_id" autocomplete="off" />
        </el-form-item>
        <el-form-item label="邮箱 Refresh Token">
          <el-input v-model="remoteLoginForm.mail_refresh_token" type="password" autocomplete="off" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="remoteLoginDialog = false">取消</el-button>
        <el-button type="primary" :loading="busy.remoteLogin" @click="submitRemoteLogin">保存并自动上车</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.team-page { max-width: 1500px; margin: 0 auto; }
.page-toolbar, .card-header, .workspace-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.page-toolbar { margin-bottom: 14px; }
.workspace-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.workspace-card {
  padding: 14px;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  background: var(--app-header-bg);
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.workspace-card:hover, .workspace-card:focus, .workspace-card.active {
  border-color: var(--brand);
  box-shadow: 0 0 0 1px var(--brand);
  outline: none;
}
.workspace-title { font-size: 15px; font-weight: 600; margin-bottom: 6px; }
.workspace-id { color: var(--el-text-color-secondary); font-size: 12px; margin-bottom: 12px; word-break: break-all; }
.workspace-meta { font-size: 12px; color: var(--el-text-color-secondary); }
.detail-stack { display: grid; gap: 16px; }
.detail-stack .el-card { margin-bottom: 0; }
.section-title { margin: 0 10px 0 0; }
.usage-note { margin: 10px 0 0; }
.sub2api-form { max-width: 760px; }
.form-required-note { margin: 0 0 14px; color: var(--el-text-color-secondary); font-size: 13px; }
.form-required-note span { color: var(--el-color-danger); }
@media (max-width: 768px) {
  .page-toolbar, .card-header, .workspace-meta { align-items: flex-start; flex-direction: column; }
  .page-toolbar .el-select { width: 100% !important; }
}
</style>
