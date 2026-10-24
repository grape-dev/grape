<template>
  <main class="home">
    <AGCard>
      <h1>Grape Vue (phase 1)</h1>
      <p>新前端工程地基已就绪。组件示例页：<RouterLink to="/ui">/ui</RouterLink></p>
    </AGCard>
  </main>
</template>

<script setup>
import { RouterLink } from 'vue-router'
import AGCard from '@/components/ui/AGCard.vue'
</script>

<style scoped>
.home {
  min-height: 100vh;
  background: var(--c-bg);
  color: var(--c-text);
  display: grid;
  place-items: center;
}
h1 {
  font-size: var(--f-head-2);
  margin-bottom: var(--s-2);
}
a {
  color: var(--c-accent);
}
</style>
