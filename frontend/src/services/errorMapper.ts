/**
 * Backend error envelope → localized display string.
 *
 * Contract (i18n plan todo 4, backend `app/core/errors.py`; task 14a raw field):
 * - HTTP error body: `{ detail, code, params, raw? }`
 * - SSE error event: `{ type: 'error', error, code, error_code?, error_params?, error_raw? }`
 * - SSE progress event: `{ type: 'progress', message, message_code?, message_params?, message_raw? }`
 * - Background task rows: `status_message` + `status_code`/`status_params`
 *   (NULL code = legacy raw text, show as-is).
 *
 * Display policy (task 14a — supersedes the old "never drop raw text" rule):
 * the raw backend text is preserved in the PAYLOAD (`raw` field / detail /
 * status_message) but for a payload WITH a code, an unknown/unregistered code
 * displays a localized GENERIC message — the backend text never leaks to the
 * UI. Legacy rows with NO code still show detail verbatim (they have no
 * translation). Debug surfaces read the original text via getErrorDiagnostic().
 *
 * Unit-testable without React: pure string in/out (needs i18n initialized).
 */
// `never[]` params make any i18next TFunction structurally assignable;
// dynamic keys are cast at the single call site below (tErrors).
interface Translator {
  t: (...args: never[]) => unknown;
}

let translator: Translator | null = null;

/**
 * Injection point: src/i18n/index.ts registers the app instance after init.
 * Tests inject their own i18next without DOM/browser dependencies.
 */
export function setErrorTranslator(t: Translator): void {
  translator = t;
}

function requireTranslator(): Translator {
  if (!translator) {
    throw new Error('errorMapper: i18n not initialized (import ../i18n before use)');
  }
  return translator;
}

export interface ErrorPayload {
  /** Raw backend text (HTTP envelope `detail`, SSE `error`, task `status_message`). */
  detail?: string | null;
  /** Registry error code (`code` / `error_code` / `status_code`). */
  code?: string | null;
  /** Template params for the code (`params` / `error_params` / `status_params`). */
  params?: Record<string, unknown> | null;
  /** HTTP status, used only for status-specific fallbacks. */
  status?: number | null;
  /** Original diagnostic string (task 14a `raw` channel); debug surfaces only. */
  raw?: string | null;
}

const AUTH_UNAUTHORIZED_CODE = 'auth.unauthorized';
const DYNAMIC_DETAIL_CODE = 'dynamic_detail';
const HTTP_ERROR_CODE = 'http_error';

/**
 * Normalize a backend code to an `errors`-namespace key path.
 * Dots are i18next nesting separators. Registry codes (auth.*, not_found.*, ...)
 * pass through; generated codes whose head isn't a real group (e.g. the 422
 * handler's `errors.validation.string`) are flattened under `validation:`
 * `errors.validation.string` → `validation.errors.validation.string`.
 */
export function normalizeErrorCode(code: string): string {
  const groups = ['auth', 'not_found', 'validation', 'internal', 'conflict', 'http', 'network', 'task', 'dynamic_detail'];
  const head = code.split('.')[0];
  return groups.includes(head) ? code : `validation.${code}`;
}

function hasErrorsKey(key: string): boolean {
  // i18next returns the key itself when lookup misses (no defaultValue passed).
  const t = requireTranslator().t as (k: string, o: { ns: 'errors' }) => unknown;
  return t(key, { ns: 'errors' }) !== key;
}

function tErrors(key: string, options: Record<string, unknown>): string {
  const t = requireTranslator().t as (k: string, o: Record<string, unknown>) => unknown;
  return String(t(key, { ns: 'errors', ...options }));
}

/** Per-status fallback keys (used when code is unknown AND detail is empty). */
const STATUS_FALLBACK_KEYS: Record<number, string> = {
  400: 'http.badRequest',
  401: 'http.unauthorized',
  403: 'http.forbidden',
  404: 'http.notFound',
  422: 'validation.failed',
  500: 'internal.error',
  503: 'http.serviceUnavailable',
};

/**
 * Main mapping: `{ code, params, detail }` → display string.
 *
 * Resolution order (task 14a display policy — supersedes the old
 * "unknown code → raw detail" rule):
 * 1. `dynamic_detail` marker → raw detail (content is runtime-generated display text).
 * 2. Known code → `errors:<code>` template (dots nest; params interpolated).
 * 3. Unknown/unregistered code (incl. bare `http_error`) → localized GENERIC
 *    text — the backend detail must NOT leak to the UI; diagnostics read it
 *    via getErrorDiagnostic().
 * 4. No code at all → legacy row (old task rows, legacy SSE): detail verbatim
 *    (the only remaining detail escape).
 * 5. No usable result → per-status fallback key, then `errors:unknown`.
 */
export function mapErrorPayload(payload: ErrorPayload): string {
  const { code, params, detail, status } = payload;

  if (code === DYNAMIC_DETAIL_CODE) {
    return detail || tErrors(DYNAMIC_DETAIL_CODE, {});
  }

  if (code) {
    // Bare 'http_error' = existing HTTPException not in registry: generic
    // message only; the backend detail is a diagnostic, not display text.
    // (Checked on the RAW code — normalizeErrorCode would nest it under
    // `validation.` since 'http_error' is not a registry group head.)
    if (code === HTTP_ERROR_CODE) {
      return tErrors(HTTP_ERROR_CODE, {});
    }
    const key = normalizeErrorCode(code);
    if (hasErrorsKey(key)) {
      const translated = tErrors(key, { ...params, detail: detail ?? '', status });
      if (translated.trim()) return translated;
    }
  } else if (detail) {
    // No code at all → legacy row (old task rows / legacy SSE): verbatim.
    return detail;
  }

  if (status) {
    const fallbackKey = STATUS_FALLBACK_KEYS[status];
    if (fallbackKey) return tErrors(fallbackKey, {});
    return tErrors('http.errorWithStatus', { status });
  }
  return tErrors('unknown', {});
}

/**
 * Original backend diagnostic text (task 14a `raw` channel, falling back to
 * the legacy `detail`): for debug/detail views only — never a display string.
 */
export function getErrorDiagnostic(payload: ErrorPayload): string {
  return payload.raw ?? payload.detail ?? '';
}

/**
 * SSE error-event payload → display string.
 * Prefers structured `{ error_code, error_params }`; legacy `{ error }`
 * shows as-is unless it is a registered code key (e.g. 'unknown').
 */
export function mapSSEError(payload: {
  error?: string | null;
  error_code?: string | null;
  error_params?: Record<string, unknown> | null;
}): string {
  if (payload.error_code) {
    return mapErrorPayload({
      code: payload.error_code,
      params: payload.error_params,
      detail: payload.error,
    });
  }
  const raw = payload.error || '';
  if (raw && hasErrorsKey(raw)) return tErrors(raw, {});
  return raw || tErrors('unknown', {});
}

/**
 * SSE progress-event message: `message_code`/`message_params` → localized
 * template. Task 14a policy: missing code → raw `message` (legacy event);
 * unregistered code → localized generic text (raw message never leaks).
 */
export function mapSSEProgressMessage(payload: {
  message?: string | null;
  message_code?: string | null;
  message_params?: Record<string, unknown> | null;
}): string {
  const raw = payload.message || '';
  if (!payload.message_code) return raw;
  if (!hasErrorsKey(payload.message_code)) return tErrors('unknown', {});
  return tErrors(payload.message_code, { ...(payload.message_params || {}) });
}

/**
 * Background-task status: structured `status_code`/`status_params` when set
 * (known → template, unregistered → generic); NULL code (old rows) → raw
 * `status_message` untouched. Delegates to mapErrorPayload's task-14a policy.
 */
export function mapTaskStatusMessage(task: {
  status_message?: string | null;
  status_code?: string | null;
  status_params?: Record<string, unknown> | null;
}): string {
  return mapErrorPayload({
    code: task.status_code,
    params: task.status_params,
    detail: task.status_message,
  });
}

/**
 * 401 redirect判定. With the todo-4 global handler every HTTPException gets an
 * envelope, but unregistered 401 variants (需要登录 etc.) come back as code
 * `http_error`, so status===401 remains the robust signal (supersedes the old
 * hardcoded Chinese-string array). `code === 'auth.unauthorized'` covers the
 * registered variant independent of status.
 */
export function isUnauthenticatedError(code?: string | null, status?: number | null): boolean {
  return code === AUTH_UNAUTHORIZED_CODE || status === 401;
}
