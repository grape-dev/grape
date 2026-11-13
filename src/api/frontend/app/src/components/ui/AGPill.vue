<template>
  <div class="a-pill">
    <slot />
  </div>
</template>

<style scoped>
/* pill tag (skill chain / branch labels) — 999px radius */
.a-pill {
  display: inline-flex;
  align-items: center;
  border-radius: var(--r-pill);
  background: var(--c-accent-soft);
  color: var(--c-accent);
  font-size: var(--f-caption);
  padding: 2px var(--s-3);
  white-space: nowrap;
}
</style>
