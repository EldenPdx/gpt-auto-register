<script setup>
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { login } from '@/api/auth'
import { useThemeStore } from '@/stores/theme'

const route = useRoute()
const router = useRouter()
useThemeStore().apply()
const form = reactive({ username: 'admin', password: '' })
const busy = ref(false)

async function submit() {
  if (!form.username || !form.password || busy.value) return
  busy.value = true
  try {
    await login(form)
    form.password = ''
    await router.replace(typeof route.query.redirect === 'string' ? route.query.redirect : '/')
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <el-card class="login-card" shadow="never">
      <div class="login-brand">
        <span class="login-logo"><el-icon :size="22"><Platform /></el-icon></span>
        <div>
          <h1>Outlook Register</h1>
          <p>管理员登录</p>
        </div>
      </div>
      <el-form :model="form" label-position="top" @submit.prevent="submit">
        <el-form-item label="账号">
          <el-input v-model="form.username" autocomplete="username" placeholder="管理员账号" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="form.password" type="password" show-password autocomplete="current-password" placeholder="请输入密码" />
        </el-form-item>
        <el-button type="primary" native-type="submit" :loading="busy" class="login-button">登录</el-button>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.login-page { min-height: 100vh; display: grid; place-items: center; padding: 24px; }
.login-card { width: min(100%, 400px); border-radius: 10px; }
.login-brand { display: flex; align-items: center; gap: 14px; margin: 8px 0 28px; }
.login-logo { width: 42px; height: 42px; display: grid; place-items: center; border-radius: 9px; background: var(--brand); color: #fff; }
h1 { font-size: 20px; margin: 0 0 2px; color: var(--app-title); }
p { margin: 0; color: var(--el-text-color-secondary); font-size: 13px; }
.login-button { width: 100%; margin-top: 8px; }
</style>
