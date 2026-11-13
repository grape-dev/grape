<template>
  <nav class="a-nav">
    <slot />
  </nav>
</template>

<style scoped>
/* top nav bar on liquid glass */
.a-nav {
  display: flex;
  align-items: center;
  gap: var(--s-4);
  background: var(--glass-bg);
  -webkit-backdrop-filter: blur(var(--glass-blur));
  backdrop-filter: blur(var(--glass-blur));
  border-radius: var(--r-card);
  box-shadow: var(--shadow-1);
  padding: var(--s-2) var(--s-4);
  color: var(--c-text);
}
</style>

<style>
/* nav link item (unscoped so router-link can be used directly inside) */
.a-nav a {
  color: var(--c-text-2);
  text-decoration: none;
  font-size: var(--f-body);
  padding: var(--s-1) var(--s-3);
  border-radius: var(--r-pill);
  transition: all 0.18s ease;
}
.a-nav a:hover {
  color: var(--c-text);
  background: var(--glass-bg-strong);
}
.a-nav a.router-link-active {
  color: var(--c-accent);
  background: var(--c-accent-soft);
}
</style>
