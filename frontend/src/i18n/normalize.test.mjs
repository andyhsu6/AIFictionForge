// Standalone normalization test — runnable TODAY without vitest:
//   cd frontend && node src/i18n/normalize.test.mjs
// (Node >= 23 strips types from the imported .ts file natively.)
import assert from 'node:assert/strict';
import { normalizeLanguage } from './normalize.ts';

const cases = [
  ['zh-CN', 'zh'],
  ['en-US', 'en'],
  ['zh', 'zh'],
  ['en', 'en'],
  ['zh-TW', 'zh'],
];

for (const [input, expected] of cases) {
  assert.equal(normalizeLanguage(input), expected, `normalizeLanguage(${input}) should be ${expected}`);
}

console.log(`normalize.test.mjs: ${cases.length}/${cases.length} assertions passed`);
