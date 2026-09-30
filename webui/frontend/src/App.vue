<script setup>
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import AdminLayout from '@/layouts/AdminLayout.vue'
</script>

<template>
  <el-config-provider :locale="zhCn">
    <router-view v-if="$route.name === 'login'" />
    <AdminLayout v-else-if="$route.name" />
  </el-config-provider>
</template>
