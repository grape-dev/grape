<template>
  <div class="a-select">
    <select
      :value="modelValue"
      :disabled="disabled"
      @change="$emit('update:modelValue', $event.target.value)"
    >
      <option v-if="placeholder" value="" :disabled="!allowPlaceholder">{{ placeholder }}</option>
      <option v-for="opt in options" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
    </select>
  </div>
</template>

<script setup>
defineProps({
  modelValue: { type: String, default: '' },
  options: { type: Array, default: () => [] }, // [{ value, label }]
  placeholder: { type: String, default: '' },
  allowPlaceholder: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
})
defineEmits(['update:modelValue'])
</script>

<style scoped>
.a-select select {
  font-family: var(--f-family);
  font-size: var(--f-body);
  color: var(--c-text);
  background: var(--glass-bg-strong);
  border-radius: var(--r-control);
  border: 1px solid var(--glass-border);
  padding: var(--s-2) var(--s-3);
  outline: none;
}
.a-select select:focus {
  border-color: var(--c-accent);
}
</style>
