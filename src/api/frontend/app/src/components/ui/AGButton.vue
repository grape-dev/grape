<template>
  <button
    :class="['a-btn', `a-btn--${variant}`, `a-btn--${size}`, { 'a-btn--block': block }]"
    :type="type"
    :disabled="disabled"
  >
    <slot />
  </button>
</template>

<script setup>
defineProps({
  variant: { type: String, default: 'primary' }, // primary | plain (ghost, iOS style) | danger
  size: { type: String, default: 'md' }, // sm | md | lg
  type: { type: String, default: 'button' },
  block: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
})
</script>

<style scoped>
.a-btn {
  font-family: var(--f-family);
  font-size: var(--f-body);
  border-radius: var(--r-pill);
  border: 1px solid transparent;
  cursor: pointer;
  transition: all 0.18s ease;
  white-space: nowrap;
}
.a-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.a-btn--primary {
  background: var(--c-accent);
  color: #fff;
  padding: var(--s-2) var(--s-5);
}
.a-btn--primary:not(:disabled):hover {
  filter: brightness(1.08);
}
/* iOS ghost button: transparent bg + light blue outline + blue text; hover = solid */
.a-btn--plain {
  background: transparent;
  color: var(--c-accent);
  border-color: var(--btn-outline);
  padding: var(--s-2) var(--s-5);
}
.a-btn--plain:not(:disabled):hover {
  background: var(--c-accent);
  color: #fff;
}
.a-btn--danger {
  background: transparent;
  color: var(--c-danger);
  border-color: var(--c-danger);
  padding: var(--s-2) var(--s-5);
}
.a-btn--danger:not(:disabled):hover {
  background: var(--c-danger);
  color: #fff;
}
.a-btn--sm {
  font-size: var(--f-caption);
  padding: var(--s-1) var(--s-4);
}
.a-btn--lg {
  font-size: var(--f-head-3);
  padding: var(--s-3) var(--s-6);
}
.a-btn--block {
  display: block;
  width: 100%;
}
</style>
