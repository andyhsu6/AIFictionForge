/**
 * Backend error envelope → localized display string.
 *
 * Contract (i18n plan todo 4, backend `app/core/errors.py`; task 14a raw field):
 * - HTTP error body: `{ detail, code, params, raw? }`
 * - SSE error event: `{ type: 'error', error, code, error_code?, error_params?, error_raw? }`
 * - SSE progress event: `{ type: 'progress', message, message_code?, message_params?, message_raw? }`
 * - Background task rows: `status_message` + `status_code`/`status_params`
 *   (+ optional `raw`, which `_background_task_data` does not emit today —
 *   the row's diagnostic lives in `status_message`).
 *   NULL code = legacy raw text, show as-is.
 *
 * Display policy (task 14a — supersedes the old "never drop raw text" rule):
 * the raw backend text is preserved in the PAYLOAD (`raw` field / detail /
 * status_message) but NEVER picks the display string when a code is present:
 * any code (registered or not) renders localized text only — the `errors:<code>`
 * template for known codes, a generic message for unknown ones
 * (`dynamic_detail` is just another known code whose locale text is generic).
 * The ONLY remaining detail escape is a payload with NO code at all (legacy
 * rows / legacy SSE events, which have no translation to show). Original text
 * reaches users only via getErrorDiagnostic() in debug/detail views.
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
const HTTP_ERROR_CODE = 'http_error';

/**
 * Legacy compat export — NO LONGER on the display path. The backend registry
 * owns the group list (12 groups), so a hardcoded whitelist both rots and
 * flattens valid codes; the display path below resolves by locale lookup
 * (resolveErrorKey) instead. Behavior kept verbatim for existing imports.
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

const ERRORS_VALIDATION_PREFIX = 'errors.validation.';

/**
 * Existence-based code resolution: a code renders its own `errors:` entry when
 * one exists (the backend registry owns the group list — no frontend
 * whitelist), else — for the 422 handler's generated
 * `errors.validation.<pydantic_type>` shape, whose `errors.` head is envelope
 * noise rather than a registry group — its real `validation.*` entry. Null →
 * the caller falls through to the generic ladder.
 */
function resolveErrorKey(code: string): string | null {
  if (hasErrorsKey(code)) return code;
  if (code.startsWith(ERRORS_VALIDATION_PREFIX)) {
    const stripped = `validation.${code.slice(ERRORS_VALIDATION_PREFIX.length)}`;
    if (hasErrorsKey(stripped)) return stripped;
  }
  return null;
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
 * "unknown code → raw detail" and "dynamic_detail → raw detail" rules):
 * 1. Bare `http_error` (unregistered HTTPException) → localized generic message.
 * 2. Code with an `errors:` locale entry — existence-checked via
 *    resolveErrorKey, incl. every registry group and `dynamic_detail` —
 *    `errors:<code>` template (dots nest; params interpolated). Localized text
 *    only — `dynamic_detail` sites send runtime-composed text, so their locale
 *    entry is a generic fallback string. A generated
 *    `errors.validation.<type>` code retries without the redundant `errors.`
 *    head.
 * 3. Code with NO locale entry → localized GENERIC text — the backend detail
 *    must NOT leak to the UI; diagnostics read it via getErrorDiagnostic().
 * 4. No code at all → legacy row (old task rows, legacy SSE): detail verbatim
 *    (the ONLY remaining detail escape).
 * 5. No usable result → per-status fallback key, then `errors:unknown`.
 */
export function mapErrorPayload(payload: ErrorPayload): string {
  const { code, params, detail, status } = payload;

  if (code) {
    // Bare 'http_error' = existing HTTPException not in registry: generic
    // message only; the backend detail is a diagnostic, not display text.
    // (Checked on the RAW code before generic resolution; it has its own
    // locale entry, but the explicit branch pins this legacy site's contract.)
    if (code === HTTP_ERROR_CODE) {
      // `status` is passed for symmetry with the known-code path so a future
      // `http_error` template can interpolate {{status}}; the current locale
      // string has no placeholder, so output is unchanged.
      return tErrors(HTTP_ERROR_CODE, { status });
    }
    const key = resolveErrorKey(code);
    if (key) {
      // `detail` is deliberately NOT interpolated: for a coded payload the
      // backend text is diagnostic-only (getErrorDiagnostic), never display.
      const translated = tErrors(key, { ...params, status });
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
 * Uses `||` (not `??`) on purpose: an empty-string `raw` from any source must
 * fall through to `detail` rather than yield an empty diagnostic.
 */
export function getErrorDiagnostic(payload: ErrorPayload): string {
  return payload.raw || payload.detail || '';
}

/**
 * SSE error-event payload → display string.
 * Prefers structured `{ error_code, error_params }`; legacy `{ error }`
 * shows as-is unless it is a registered code key (e.g. 'unknown').
 * `error_raw` (task 14a) is accepted and forwarded for diagnostic consumers
 * (`getErrorDiagnostic`) — it never influences the returned display string.
 */
export function mapSSEError(payload: {
  error?: string | null;
  error_code?: string | null;
  error_params?: Record<string, unknown> | null;
  error_raw?: string | null;
}): string {
  if (payload.error_code) {
    return mapErrorPayload({
      code: payload.error_code,
      params: payload.error_params,
      detail: payload.error,
      raw: payload.error_raw,
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
 * `message_raw` is accepted for diagnostic consumers only.
 */
export function mapSSEProgressMessage(payload: {
  message?: string | null;
  message_code?: string | null;
  message_params?: Record<string, unknown> | null;
  message_raw?: string | null;
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
 * `raw` is accepted for diagnostic consumers only.
 */
export function mapTaskStatusMessage(task: {
  status_message?: string | null;
  status_code?: string | null;
  status_params?: Record<string, unknown> | null;
  raw?: string | null;
}): string {
  return mapErrorPayload({
    code: task.status_code,
    params: task.status_params,
    detail: task.status_message,
    raw: task.raw,
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
