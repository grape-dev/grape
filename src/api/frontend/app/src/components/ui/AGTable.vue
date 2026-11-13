<template>
  <div class="a-table-wrap">
    <table class="a-table">
      <thead>
        <tr>
          <th v-for="col in columns" :key="col.key" :style="col.width ? { width: col.width } : {}">
            {{ col.title }}
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, i) in rows" :key="i">
          <td v-for="col in columns" :key="col.key">
            <slot :name="`cell-${col.key}`" :row="row" :index="i">{{ row[col.key] }}</slot>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length" class="a-table__empty">
            <slot name="empty">{{ emptyText }}</slot>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
defineProps({
  columns: { type: Array, required: true }, // [{ key, title, width? }]
  rows: { type: Array, default: () => [] },
  emptyText: { type: String, default: '—' },
})
</script>

<style scoped>
.a-table-wrap {
  overflow-x: auto;
  border-radius: var(--r-card);
}
.a-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--f-body);
  color: var(--c-text);
}
th {
  text-align: left;
  font-size: var(--f-caption);
  color: var(--c-text-2);
  font-weight: 500;
  padding: var(--s-2) var(--s-3);
  border-bottom: 1px solid var(--glass-border);
}
td {
  padding: var(--s-3);
  border-bottom: 1px solid var(--glass-border);
}
tbody tr:last-child td {
  border-bottom: none;
}
tbody tr:hover {
  background: var(--glass-bg);
}
.a-table__empty {
  text-align: center;
  color: var(--c-text-3);
  padding: var(--s-6) var(--s-3);
}
</style>
