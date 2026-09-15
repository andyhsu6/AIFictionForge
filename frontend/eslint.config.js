import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs['recommended-latest'],
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      'no-restricted-imports': [
        'error',
        {
          paths: [
            {
              name: 'antd',
              importNames: ['message', 'notification'],
              message:
                'Static antd `message`/`notification` cannot consume context (theme/locale). In components use `const { message } = App.useApp()`; in non-component code use the `antdMessage` / `antdNotification` proxies from `src/utils/antdApp.ts`.',
            },
          ],
        },
      ],
      'no-restricted-syntax': [
        'error',
        {
          // `destroyAll` is intentionally excluded: antd's context `modal` (App.useApp())
          // has no destroyAll, and the static `Modal.destroyAll()` never emits the
          // static-context warning (it only drains the shared destroyFns registry).
          selector:
            "CallExpression[callee.object.name='Modal'][callee.property.name=/^(confirm|info|success|error|warning)$/]",
          message:
            'Static `Modal.confirm/info/success/error/warning` cannot consume context (theme/locale). Use the `modal` instance from `App.useApp()` (const { modal } = App.useApp()) instead.',
        },
      ],
    },
  },
])
