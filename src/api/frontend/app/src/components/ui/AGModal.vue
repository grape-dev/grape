<template>
  <Teleport to="body">
    <Transition name="a-modal">
      <div v-if="open" class="a-modal__mask" @click.self="closeOnMask && $emit('close')">
        <div class="a-modal" role="dialog" aria-modal="true">
          <div v-if="title" class="a-modal__title">{{ title }}</div>
          <div class="a-modal__body">
            <slot />
          </div>
          <div v-if="$slots.footer" class="a-modal__footer">
            <slot name="footer" />
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, default: '' },
  // 点遮罩是否关闭：表单型弹窗（如创建组织）关掉它，避免误点丢失已填内容，
  // 只允许显式「取消 / X」关闭。默认保持既有行为。
  closeOnMask: { type: Boolean, default: true },
})
defineEmits(['close'])
</script>

<style scoped>
.a-modal__mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.28);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 16px;
}
.a-modal {
  min-width: 320px;
  max-width: min(92vw, 520px);
  max-height: calc(100vh - 48px);
  display: flex;
  flex-direction: column;
  background: var(--glass-bg-strong);
  -webkit-backdrop-filter: blur(var(--glass-blur));
  backdrop-filter: blur(var(--glass-blur));
  border-radius: var(--r-card);
  box-shadow: var(--shadow-2);
  color: var(--c-text);
  padding: var(--s-5);
}
.a-modal__title {
  font-size: var(--f-head-2);
  font-weight: 600;
  margin-bottom: var(--s-3);
  flex-shrink: 0;
}
.a-modal__body {
  font-size: var(--f-body);
  overflow-y: auto;
  min-height: 0;
}
.a-modal__footer {
  margin-top: var(--s-4);
  display: flex;
  justify-content: flex-end;
  gap: var(--s-2);
  flex-shrink: 0;
}
.a-modal-enter-active,
.a-modal-leave-active {
  transition: opacity 0.18s ease;
}
.a-modal-enter-from,
.a-modal-leave-to {
  opacity: 0;
}
</style>
