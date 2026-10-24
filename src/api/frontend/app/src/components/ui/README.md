# @apple-ui Component Library

Grape 新前端的唯一视觉组件库 —— 苹果毛玻璃（Liquid Glass），明暗双主题。

## 原则

1. **只引用 tokens**：所有颜色/间距/圆角/字号/阴影必须来自 `src/styles/tokens.css` 的 CSS variables，
   **禁止手写魔法值**。
2. **一种风格**：只做苹果毛玻璃；不存在"双风格组件"。明/暗是同一语言的两个变体，由
   `:root[data-theme='light'|'dark']` 变量组切换，组件本身不感知主题。
3. **命名**：组件文件 `AG` 前缀 + PascalCase（AGButton.vue）；class 用 `a-` 前缀。
4. **交互约定**：透明底 + 浅蓝描边 + 蓝字，hover 实心蓝底白字；删除类 = 红框红字 hover 实心红底白字；
   药丸 999px；卡片 18px 圆角、极轻阴影、无描边毛玻璃。
5. **JS inline style 压制问题**：需要动态样式时用 `data-*` 属性 + CSS 选择器，不用 `!important`。

## 组件清单

| 组件 | 说明 |
|---|---|
| AGButton | primary / plain(iOS ghost) / danger，sm/md/lg，block |
| AGCard | 毛玻璃卡片容器 |
| AGInput | v-model 文本输入 + label/hint/invalid |
| AGSelect | v-model 下拉 |
| AGModal | Teleport 毛玻璃弹窗，close 事件，footer 插槽 |
| AGToast | 顶部药丸 toast，info/success/danger |
| AGPill | 药丸标签 |
| AGTable | columns/rows 声明式表格，cell 插槽 `#cell-<key>` |
| AGNav | 毛玻璃导航条，内放 router-link |

## 用法示例

```vue
<script setup>
import AGButton from '@/components/ui/AGButton.vue'
import AGCard from '@/components/ui/AGCard.vue'
</script>

<template>
  <AGCard>
    <AGButton variant="plain" @click="save">保存</AGButton>
    <AGButton variant="danger">删除</AGButton>
  </AGCard>
</template>
```

## 主题切换

沿用 legacy 的 localStorage 键 `grapeTheme` / `grapeThemeMode`；FOUC 防闪屏逻辑等价迁移到
`index.html` head 内联脚本。开发期用示例页右上角切换按钮手动验证双主题。
