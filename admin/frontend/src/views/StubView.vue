<template>
  <div class="stub">
    <h3>{{ route.meta.title }}</h3>
    <p class="tip">页面建设中（对应实施计划 {{ route.name }}）</p>
  </div>
</template>

<script setup>
import { useRoute } from 'vue-router'
const route = useRoute()
</script>

<style scoped>
.stub { background: #fff; border-radius: 6px; padding: 40px; text-align: center; }
.tip { color: #999; margin-top: 8px; }
</style>
