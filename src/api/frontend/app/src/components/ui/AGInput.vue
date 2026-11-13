<template>
  <label class="a-field">
    <span v-if="label" class="a-field__label">{{ label }}</span>
    <input
      :class="['a-input', { 'a-input--invalid': invalid }]"
      :type="type"
      :value="modelValue"
      :placeholder="placeholder"
      :disabled="disabled"
      @input="$emit('update:modelValue', $event.target.value)"
    />
    <span v-if="hint" class="a-field__hint">{{ hint }}</span>
  </label>
</template>

<script setup>
defineProps({
  modelValue: { type: String, default: '' },
  label: { type: String, default: '' },
  hint: { type: String, default: '' },
  type: { type: String, default: 'text' },
  placeholder: { type: String, default: '' },
  disabled: { type: Boolean, default: false },
  invalid: { type: Boolean, default: false },
})
defineEmits(['update:modelValue'])
</script>

<style scoped>
.a-field {
  display: flex;
  flex-direction: column;
  gap: var(--s-1);
}
.a-field__label {
  font-size: var(--f-caption);
  color: var(--c-text-2);
}
.a-field__hint {
  font-size: var(--f-caption);
  color: var(--c-text-3);
}
.a-input {
  font-family: var(--f-family);
  font-size: var(--f-body);
  color: var(--c-text);
  background: var(--glass-bg-strong);
  border-radius: var(--r-control);
  border: 1px solid var(--glass-border);
  padding: var(--s-2) var(--s-3);
  outline: none;
  transition: border-color 0.18s ease;
}
.a-input:focus {
  border-color: var(--c-accent);
}
.a-input--invalid {
  border-color: var(--c-danger);
}
.a-input:disabled {
  opacity: 0.4;
}
</style>
