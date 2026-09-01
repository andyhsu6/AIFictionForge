// errorMapper unit test — standalone, no vitest:
//   cd frontend && node src/services/errorMapper.test.mjs
// Uses the setErrorTranslator seam with a real i18next instance over the
// actual locale JSONs, so it validates templates + fallback behavior.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import i18next from 'i18next';

const require = createRequire(import.meta.url);
const zhErrors = require('../locales/zh/errors.json');
const enErrors = require('../locales/en/errors.json');

const {
  mapErrorPayload,
  mapSSEError,
  mapSSEProgressMessage,
  mapTaskStatusMessage,
  isUnauthenticatedError,
  normalizeErrorCode,
  setErrorTranslator,
} = await import('./errorMapper.ts');

await i18next.init({
  lng: 'zh',
  resources: { zh: { errors: zhErrors }, en: { errors: enErrors } },
  ns: ['errors'],
  defaultNS: 'errors',
  interpolation: { escapeValue: false },
});
setErrorTranslator(i18next);

const cases = [
  // known code → translated template
  () => assert.equal(mapErrorPayload({ code: 'auth.unauthorized', detail: '未登录', status: 401 }), '未登录'),
  () => assert.equal(mapErrorPayload({ code: 'not_found.chapter', detail: '章节不存在', status: 404 }), '章节不存在'),
  // unknown code → raw detail preserved (hard backward-compat rule)
  () => assert.equal(mapErrorPayload({ code: 'weird.some_code', detail: '原始后端文案', status: 400 }), '原始后端文案'),
  // http_error shell interpolates raw detail
  () => assert.equal(mapErrorPayload({ code: 'http_error', detail: '需要登录', status: 401 }), '需要登录'),
  // no code no detail → per-status fallback
  () => assert.equal(mapErrorPayload({ status: 503 }), '服务暂时不可用，请稍后重试'),
  () => assert.equal(mapErrorPayload({ status: 418 }), '请求失败 (418)'),
  () => assert.equal(mapErrorPayload({}), '未知错误'),
  // dynamic_detail marker → raw detail wins
  () => assert.equal(mapErrorPayload({ code: 'dynamic_detail', detail: '运行时拼接文案' }), '运行时拼接文案'),
  // 422 generated code flattening: errors.validation.string → validation.* fallback (unknown → raw detail)
  () => assert.equal(mapErrorPayload({ code: 'errors.validation.string', detail: '请求参数验证失败', status: 422 }), '请求参数验证失败'),
  () => assert.equal(normalizeErrorCode('errors.validation.string'), 'validation.errors.validation.string'),
  () => assert.equal(normalizeErrorCode('auth.unauthorized'), 'auth.unauthorized'),
  // SSE structured vs legacy
  () => assert.equal(mapSSEError({ error: '章节不存在', error_code: 'not_found.chapter', error_params: {} }), '章节不存在'),
  () => assert.equal(mapSSEError({ error: '旧版中文错误' }), '旧版中文错误'),
  () => assert.equal(mapSSEError({}), '未知错误'),
  // SSE progress: code hit → template; miss → raw message
  () => assert.equal(mapSSEProgressMessage({ message: '开始生成...', message_code: 'unknown' }), '未知错误'),
  () => assert.equal(mapSSEProgressMessage({ message: '开始生成...', message_code: 'nope.missing' }), '开始生成...'),
  () => assert.equal(mapSSEProgressMessage({ message: '开始生成...' }), '开始生成...'),
  // background task: NULL status_code (old rows) → raw status_message untouched
  () => assert.equal(mapTaskStatusMessage({ status_message: '旧任务原始进度文案' }), '旧任务原始进度文案'),
  () => assert.equal(mapTaskStatusMessage({ status_message: '章节不存在', status_code: 'not_found.chapter' }), '章节不存在'),
  // 401 redirect judgment
  () => assert.equal(isUnauthenticatedError('auth.unauthorized', null), true),
  () => assert.equal(isUnauthenticatedError('http_error', 401), true),
  () => assert.equal(isUnauthenticatedError(null, null), false),
  // English locale switch
  async () => {
    await i18next.changeLanguage('en');
    assert.equal(mapErrorPayload({ code: 'auth.unauthorized', status: 401 }), 'Not logged in');
    assert.equal(mapErrorPayload({ code: 'weird.x', detail: 'raw chinese kept', status: 400 }), 'raw chinese kept');
    await i18next.changeLanguage('zh');
  },
];

let passed = 0;
for (const c of cases) {
  await c();
  passed += 1;
}
console.log(`errorMapper.test.mjs: ${passed}/${cases.length} assertions passed`);
