import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import vueTsEslintParser from 'vue-eslint-parser'
import prettierConfig from '@vue/eslint-config-prettier'

export default [
  { ignores: ['dist/', 'node_modules/', '../legacy/'] },
  js.configs.recommended,
  ...pluginVue.configs['flat/recommended'],
  {
    files: ['**/*.vue'],
    languageOptions: { parser: vueTsEslitParserStub() },
  },
  { languageOptions: { globals: { document: 'readonly', window: 'readonly', localStorage: 'readonly', sessionStorage: 'readonly', setTimeout: 'readonly', clearTimeout: 'readonly', setInterval: 'readonly', clearInterval: 'readonly', URLSearchParams: 'readonly', fetch: 'readonly', navigator: 'readonly', location: 'readonly', console: 'readonly', FormData: 'readonly', URL: 'readonly', history: 'readonly', FileReader: 'readonly', indexedDB: 'readonly', AbortController: 'readonly', fetch: 'readonly', TextDecoder: 'readonly', Blob: 'readonly' } } },
  { rules: { 'no-unused-vars': ['error', { caughtErrors: 'none' }] } },
  prettierConfig,
  {
    rules: {
      'vue/multi-word-component-names': 'off',
    },
  },
]

// keep parser default from plugin-vue; this stub only avoids double-import issues
function vueTsEslitParserStub() {
  return vueTsEslintParser
}
