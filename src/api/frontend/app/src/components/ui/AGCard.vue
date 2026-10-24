<template>
  <div :class="['a-card', padded && 'a-card--padded']">
    <slot />
  </div>
</template>

<script setup>
defineProps({ padded: { type: Boolean, default: true } })
</script>

<style scoped>
/* Apple liquid glass card: translucent surface + backdrop blur, no border ring, hairline shadow */
.a-card {
  background: var(--glass-bg);
  -webkit-backdrop-filter: blur(var(--glass-blur));
  backdrop-filter: blur(var(--glass-blur));
  border-radius: var(--r-card);
  box-shadow: var(--shadow-1);
  color: var(--c-text);
}
.a-card--padded {
  padding: var(--s-4);
}
</style>
