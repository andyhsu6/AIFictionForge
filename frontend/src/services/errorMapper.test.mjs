// errorMapper unit test — standalone, no vitest:
//   cd frontend && node --test src/services/errorMapper.test.mjs
// Uses the setErrorTranslator seam with a real i18next instance over the
// actual locale JSONs, so it validates templates + fallback behavior.
//
// Task 14a display policy: for ANY payload with a code (registered or not,
// including the dynamic_detail marker) the display string is localized —
// template for known codes, generic text otherwise; the backend's raw text
// never leaks (it lives in the payload `raw`/`detail`, surfaced only via
// getErrorDiagnostic in debug surfaces). Payloads WITHOUT a code are legacy
// rows and still show detail verbatim — the only detail escape.
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
  getErrorDiagnostic,
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
  // unregistered code → localized generic text, backend detail NEVER leaks to display
  () => assert.equal(mapErrorPayload({ code: 'weird.some_code', detail: '原始后端文案', status: 400 }), '请求参数错误'),
  () => {
    const shown = mapErrorPayload({ code: 'weird.some_code', detail: '原始后端文案' });
    assert.ok(!shown.includes('原始后端文案'), `unregistered code leaked detail: ${shown}`);
    assert.equal(shown, '未知错误');
  },
  // http_error (unregistered HTTPException site) → generic message, no detail interpolation
  () => assert.equal(mapErrorPayload({ code: 'http_error', detail: '需要登录', status: 401 }), '请求处理失败'),
  // no code at all (legacy row) → detail verbatim
  () => assert.equal(mapErrorPayload({ detail: '旧版中文文案' }), '旧版中文文案'),
  // no code no detail → per-status fallback
  () => assert.equal(mapErrorPayload({ status: 503 }), '服务暂时不可用，请稍后重试'),
  () => assert.equal(mapErrorPayload({ status: 418 }), '请求失败 (418)'),
  () => assert.equal(mapErrorPayload({}), '未知错误'),
  // dynamic_detail is a KNOWN code → localized generic text only; the
  // runtime-composed backend text never reaches the display string.
  () => {
    const shown = mapErrorPayload({ code: 'dynamic_detail', detail: '运行时拼接文案' });
    assert.equal(shown, zhErrors.dynamic_detail);
    assert.ok(!shown.includes('运行时拼接文案'), `dynamic_detail leaked detail: ${shown}`);
  },
  // raw channel: display stays generic, original text only via getErrorDiagnostic
  () => {
    const payload = { code: 'dynamic_detail', detail: '运行时拼接文案', raw: '运行时拼接文案', status: 500 };
    assert.equal(mapErrorPayload(payload), zhErrors.dynamic_detail);
    assert.equal(getErrorDiagnostic(payload), '运行时拼接文案');
  },
  () => assert.equal(mapErrorPayload({ code: 'dynamic_detail' }), zhErrors.dynamic_detail),
  // 422 generated code flattening: errors.validation.string is unregistered →
  // status-based fallback (422 → validation.failed), still localized
  () => assert.equal(mapErrorPayload({ code: 'errors.validation.string', detail: '请求参数验证失败', status: 422 }), '请求参数验证失败'),
  () => assert.equal(normalizeErrorCode('errors.validation.string'), 'validation.errors.validation.string'),
  () => assert.equal(normalizeErrorCode('auth.unauthorized'), 'auth.unauthorized'),
  // SSE structured vs legacy
  () => assert.equal(mapSSEError({ error: '章节不存在', error_code: 'not_found.chapter', error_params: {} }), '章节不存在'),
  () => assert.equal(mapSSEError({ error: '旧版中文错误' }), '旧版中文错误'),
  () => assert.equal(mapSSEError({}), '未知错误'),
  // SSE unregistered error_code → generic, raw error text never leaks
  () => {
    const shown = mapSSEError({ error: '原始中文错误', error_code: 'weird.x' });
    assert.ok(!shown.includes('原始中文错误'), `SSE unregistered code leaked raw error: ${shown}`);
  },
  // SSE progress: registered code → template; unregistered → generic (no leak); no code → raw
  () => assert.equal(mapSSEProgressMessage({ message: '开始生成...', message_code: 'unknown' }), '未知错误'),
  () => {
    const shown = mapSSEProgressMessage({ message: '开始生成...', message_code: 'nope.missing' });
    assert.ok(!shown.includes('开始生成'), `progress unregistered code leaked raw message: ${shown}`);
  },
  () => assert.equal(mapSSEProgressMessage({ message: '开始生成...' }), '开始生成...'),
  // background task: NULL status_code (old rows) → raw status_message untouched
  () => assert.equal(mapTaskStatusMessage({ status_message: '旧任务原始进度文案' }), '旧任务原始进度文案'),
  () => assert.equal(mapTaskStatusMessage({ status_message: '章节不存在', status_code: 'not_found.chapter' }), '章节不存在'),
  // 401 redirect judgment
  () => assert.equal(isUnauthenticatedError('auth.unauthorized', null), true),
  () => assert.equal(isUnauthenticatedError('http_error', 401), true),
  () => assert.equal(isUnauthenticatedError(null, null), false),
  // getErrorDiagnostic: raw ?? detail ?? '' (debug surfaces only)
  () => assert.equal(getErrorDiagnostic({ raw: 'raw diag', detail: 'detail text' }), 'raw diag'),
  () => assert.equal(getErrorDiagnostic({ detail: 'detail text' }), 'detail text'),
  () => assert.equal(getErrorDiagnostic({ raw: null, detail: null }), ''),
  () => assert.equal(getErrorDiagnostic({}), ''),
  // English locale switch
  async () => {
    await i18next.changeLanguage('en');
    assert.equal(mapErrorPayload({ code: 'auth.unauthorized', status: 401 }), 'Not logged in');
    // unregistered code → en generic, never the backend raw text
    const shown = mapErrorPayload({ code: 'weird.x', detail: 'raw chinese kept', status: 400 });
    assert.ok(!shown.includes('raw chinese kept'), `en unregistered code leaked detail: ${shown}`);
    assert.equal(shown, 'Bad request parameters');
    // http_error → en generic
    assert.equal(mapErrorPayload({ code: 'http_error', detail: '需要登录', status: 401 }), 'Request failed');
    // dynamic_detail → en generic (known code, never the raw detail)
    assert.equal(mapErrorPayload({ code: 'dynamic_detail', detail: 'runtime composed' }), enErrors.dynamic_detail);
    // legacy no-code row keeps detail verbatim even in en
    assert.equal(mapErrorPayload({ detail: 'legacy raw' }), 'legacy raw');
    await i18next.changeLanguage('zh');
  },
];

let passed = 0;
for (const c of cases) {
  await c();
  passed += 1;
}
console.log(`errorMapper.test.mjs: ${passed}/${cases.length} assertions passed`);
