import { defineConfig } from 'i18next-cli';

export default defineConfig({
  locales: ['zh', 'en'],
  extract: {
    input: ['src/**/*.{ts,tsx}'],
    output: 'src/locales/{{language}}/{{namespace}}.json',
    defaultNS: 'common',
    // zh is the product default language; seed keys live there
    primaryLanguage: 'zh',
    // Seed keys not yet referenced in code must survive extraction runs
    // (real keys arrive in todos 6/15). extract removes keys only when
    // removeUnusedKeys is enabled, so leave it off.
    removeUnusedKeys: false,
  },
  types: {
    input: 'src/locales/zh/*.json',
    basePath: 'src/locales/zh',
    output: 'src/types/i18next.d.ts',
  },
});
