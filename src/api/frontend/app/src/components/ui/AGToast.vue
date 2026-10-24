<template>
  <Transition name="a-toast">
    <Teleport to="body">
      <div v-if="open" :class="['a-toast', `a-toast--${tone}`]" role="status">
        {{ message }}
      </div>
    </Teleport>
  </Transition>
</template>

<script setup>
defineProps({
  open: { type: Boolean, default: false },
  message: { type: String, default: '' },
  tone: { type: String, default: 'info' }, // info | success | danger
})
</script>

<style scoped>
.a-toast {
  position: fixed;
  top: var(--s-5);
  left: 50%;
  transform: translateX(-50%);
  background: var(--glass-bg-strong);
  -webkit-backdrop-filter: blur(var(--glass-blur));
  backdrop-filter: blur(var(--glass-blur));
  border-radius: var(--r-pill);
  box-shadow: var(--shadow-2);
  color: var(--c-text);
  font-size: var(--f-body);
  padding: var(--s-2) var(--s-5);
  z-index: 1100;
}
.a-toast--success {
  color: var(--c-success);
}
.a-toast--danger {
  color: var(--c-danger);
}
.a-toast-enter-active,
.a-toast-leave-active {
  transition:
    opacity 0.22s ease,
    transform 0.22s ease;
}
.a-toast-enter-from,
.a-toast-leave-to {
  opacity: 0;
  transform: translateX(-50%) translateY(-8px);
}
</style>
