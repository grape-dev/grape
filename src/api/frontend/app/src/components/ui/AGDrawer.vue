<template>
  <Teleport to="body">
    <Transition name="a-drawer">
      <div v-if="open" class="a-drawer__mask" @click.self="closeOnMask && $emit('close')">
        <div
          :class="['a-drawer', wide && 'a-drawer--wide']"
          role="dialog"
          aria-modal="true"
        >
          <div class="a-drawer__head">
            <div class="a-drawer__headmain">
              <div v-if="title" class="a-drawer__title">{{ title }}</div>
              <div v-if="subtitle" class="a-drawer__sub">{{ subtitle }}</div>
            </div>
            <button class="a-drawer__x" type="button" @click="$emit('close')">✕</button>
          </div>
          <div class="a-drawer__body">
            <slot />
          </div>
          <div v-if="$slots.footer" class="a-drawer__foot">
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
  // 标题下的小字（scope · 仓库 · 提示），编辑类抽屉的归属上下文
  subtitle: { type: String, default: '' },
  // 宽抽屉：560px；默认 480px。长表单用宽档
  wide: { type: Boolean, default: false },
  // 点遮罩是否关闭：表单类抽屉默认关掉（防误点丢已填内容），只允许 ✕/取消关闭
  closeOnMask: { type: Boolean, default: false },
})
defineEmits(['close'])
</script>

<style scoped>
.a-drawer__mask {
  position: fixed;
  inset: 0;
  background: rgba(16, 23, 36, 0.24);
  z-index: 1000;
  display: flex;
  justify-content: flex-end;
}
.a-drawer {
  width: 480px;
  max-width: 92vw;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--glass-bg-strong, #fff);
  -webkit-backdrop-filter: blur(var(--glass-blur, 6px));
  backdrop-filter: blur(var(--glass-blur, 6px));
  box-shadow: -16px 0 44px rgba(16, 23, 36, 0.14);
  color: var(--c-text, var(--t, #101724));
}
.a-drawer--wide {
  width: 560px;
}
.a-drawer__head {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 18px 22px 14px;
  border-bottom: 1px solid var(--b, #e3e6ee);
  flex-shrink: 0;
}
.a-drawer__headmain {
  flex: 1;
  min-width: 0;
}
.a-drawer__title {
  font-size: 15px;
  font-weight: 700;
  color: var(--t, #101724);
}
.a-drawer__sub {
  margin-top: 4px;
  font-size: 11px;
  color: var(--t3, #8d97a8);
}
.a-drawer__x {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  border: 1px solid var(--b, #e3e6ee);
  background: transparent;
  color: var(--t3, #8d97a8);
  font-size: 13px;
  cursor: pointer;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}
.a-drawer__x:hover {
  color: var(--t, #101724);
  border-color: var(--a, #3168e0);
}
.a-drawer__body {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
  padding: 18px 22px;
}
.a-drawer__foot {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 22px;
  border-top: 1px solid var(--b, #e3e6ee);
  justify-content: flex-end;
  flex-shrink: 0;
  background: var(--glass-bg-strong, #fff);
}
/* 滑入/滑出：mask 渐隐 + 抽屉右滑 */
.a-drawer-enter-active,
.a-drawer-leave-active {
  transition: opacity 0.18s ease;
}
.a-drawer-enter-active .a-drawer,
.a-drawer-leave-active .a-drawer {
  transition: transform 0.22s ease;
}
.a-drawer-enter-from,
.a-drawer-leave-to {
  opacity: 0;
}
.a-drawer-enter-from .a-drawer,
.a-drawer-leave-to .a-drawer {
  transform: translateX(30px);
}
</style>
