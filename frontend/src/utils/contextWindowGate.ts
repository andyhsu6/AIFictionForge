/**
 * Context-window gate math for the settings form (issue #55 step 3b).
 *
 * The product premise is a >=1M token context window; below that the failure is
 * silent, so the gate is hard. The plan requires the form to show **three numbers
 * on one screen** — the probed window / the window you declared / the budget the
 * system will actually adopt — rather than only an error code, because a user who
 * cannot see *why* a save was rejected cannot fix it.
 *
 * Deliberately React-free so the derivation is unit-testable and so the two
 * surfaces that need it (the live probe result, and a rejected save whose error
 * envelope carries the same numbers in `params`) share one source of truth.
 *
 * Field names mirror `POST /api/settings/check-context-window` at commit
 * `b09a047` (`details.window_display`) and the `validation.ai_model_below_minimum`
 * params emitted by `app/services/model_capability_probe.py`; nothing here invents
 * a shape the backend does not send.
 */

/**
 * Floor the gate enforces. The backend owns the authoritative number and returns it
 * as `details.min_window` / `params.min_window`; this constant only covers the
 * pre-probe frame before any response exists. `tests/test_context_window_probe_gate.py`
 * pins it against `MIN_CONTEXT_WINDOW_TOKENS` so the two cannot drift.
 */
export const MIN_CONTEXT_WINDOW_TOKENS = 1_000_000;

/** Verdicts from `app/services/model_capability_probe.py` (`VERDICT_*`). */
export type GateVerdict = 'qualified' | 'unqualified' | 'inconclusive' | 'unknown';

/** What the form shows next to the three numbers. */
export type GateStatus =
  /** No model yet: nothing to probe, and the backend refuses to guess one. */
  | 'model-missing'
  /** Model filled but never probed from this form: 「重新检测」 is the next step. */
  | 'unprobed'
  /** >= 1M measured or declared-and-accepted: saving is allowed. */
  | 'qualified'
  /** Measured < 1M: hard reject, a declaration cannot override it. */
  | 'below-minimum'
  /** Probe could not decide: an explicit declaration >= 1M is required to save. */
  | 'needs-declaration';

export interface ContextWindowProbe {
  supported: boolean;
  details?: {
    verdict?: string;
    source?: string;
    min_window?: number;
    context_window_tokens?: number | null;
    window_display?: {
      probed_context_window_tokens?: number | null;
      declared_context_window_tokens?: number | null;
      minimum_required_context_window_tokens?: number;
      adopted_context_window_tokens?: number | null;
    };
  };
}

/** Normalized `params` of a rejected save (`validation.ai_model_below_minimum`). */
export interface GateRejection {
  verdict?: string | null;
  min_window?: number | null;
  measured_context_window_tokens?: number | null;
  declared_context_window_tokens?: number | null;
  requires_explicit_declaration?: boolean | null;
}

export interface GateNumbers {
  /** Number 1: what the probe measured (null = the probe could not tell). */
  probed: number | null;
  /** Number 2: what the user typed into the form. */
  declared: number | null;
  /** Number 3: the budget the system will actually adopt (null = it adopts none; save is rejected). */
  adopted: number | null;
  /** The floor the gate enforces, taken from the server when a response exists. */
  minimum: number;
  verdict: GateVerdict;
  status: GateStatus;
  /** True when the form must demand an explicit declaration before saving. */
  requiresDeclaration: boolean;
}

const VERDICT_BY_STATUS: Record<GateVerdict, GateStatus | null> = {
  qualified: 'qualified',
  unqualified: 'below-minimum',
  inconclusive: 'needs-declaration',
  unknown: null,
};

function asVerdict(value: string | null | undefined): GateVerdict {
  return value === 'qualified' || value === 'unqualified' || value === 'inconclusive'
    ? value
    : 'unknown';
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/**
 * Derive the three numbers plus the status the form renders.
 *
 * Precedence: a live probe wins; a rejection envelope fills in what the probe has
 * not measured yet (a user may hit 保存 before ever pressing 重新检测). `declared`
 * always comes from the form field, so the second number tracks typing live.
 *
 * `adopted` reproduces the backend rule, not a UI opinion: a measured <1M model is
 * rejected even when the user declares 1M+ (`ensure_model_allowed` only consults the
 * declaration after tiers ①② came back inconclusive) — that is why there is no
 * "acknowledge and continue" checkbox to add here either.
 */
export function deriveGateNumbers(
  probe: ContextWindowProbe | null,
  declared: number | null | undefined,
  rejection: GateRejection | null,
): GateNumbers {
  const display = probe?.details?.window_display;
  const minimum =
    asNumber(display?.minimum_required_context_window_tokens)
    ?? asNumber(probe?.details?.min_window)
    ?? asNumber(rejection?.min_window)
    ?? MIN_CONTEXT_WINDOW_TOKENS;

  const probed =
    asNumber(display?.probed_context_window_tokens)
    ?? asNumber(probe?.details?.context_window_tokens)
    ?? asNumber(rejection?.measured_context_window_tokens);

  const effectiveDeclared =
    asNumber(declared)
    ?? asNumber(display?.declared_context_window_tokens)
    ?? asNumber(rejection?.declared_context_window_tokens);

  let verdict: GateVerdict;
  if (probe) {
    verdict = probe.supported ? 'qualified' : asVerdict(probe.details?.verdict);
    // `supported: false` without a verdict string still means "not qualified";
    // treat the unknown case as undecidable, which demands a declaration.
    if (verdict === 'unknown') verdict = 'inconclusive';
  } else if (rejection) {
    verdict = asVerdict(rejection.verdict);
    if (verdict === 'unknown') {
      verdict = rejection.requires_explicit_declaration === false ? 'unqualified' : 'inconclusive';
    }
  } else {
    verdict = 'unknown';
  }

  const qualified = VERDICT_BY_STATUS[verdict] === 'qualified';
  // 第三个数必须是「保存真的会被采纳」的那个值。实测低于下限时后端直接拒保存，
  // 声明再大也不会进采纳分支（`ensure_model_allowed` 只在 ①② 判不出时才看声明），
  // 所以这里必须回 null：否则表单会在门禁拒绝的同时显示一个 1,000,000 的预算，
  // 等于告诉用户「会用这个窗口」——正是本需求要根除的静默失败形态。
  const adopted = qualified
    ? (probed ?? effectiveDeclared ?? minimum)
    : (verdict === 'inconclusive'
        && effectiveDeclared !== null
        && effectiveDeclared >= minimum
        ? effectiveDeclared
        : null);

  let status: GateStatus;
  if (verdict === 'qualified' && adopted !== null) {
    status = 'qualified';
  } else if (verdict === 'unqualified') {
    status = 'below-minimum';
  } else if (verdict === 'inconclusive') {
    status = 'needs-declaration';
  } else {
    status = 'unprobed';
  }

  return {
    probed,
    declared: effectiveDeclared,
    adopted,
    minimum,
    verdict,
    status,
    requiresDeclaration: status === 'needs-declaration',
  };
}

/**
 * Display string for one of the three numbers: a missing value is meaningful
 * ("the probe could not tell"), so it must never render as a bare `0` — `0`
 * already means "whole-book injection disabled" in this codebase.
 */
export function formatWindowTokens(value: number | null): string {
  return value === null ? '—' : value.toLocaleString('en-US');
}

/**
 * `GET /settings` → `context_window_gate`: the verdict already cached for the user's
 * configured (provider, base_url, model) triple. Same keys as the rejection params
 * (both sides are produced by `model_capability_probe.gate_state_payload`), plus the
 * bookkeeping fields the server knows and the form does not have to guess.
 */
export interface CachedGateState {
  model?: string;
  verdict?: string;
  source?: string;
  min_window?: number;
  measured_context_window_tokens?: number | null;
  requires_explicit_declaration?: boolean;
  checked_at?: string;
  due_for_recheck?: boolean;
}

/**
 * Seed the form from the cached conclusion (issue #55 step 5: legacy users).
 *
 * Why this exists: the hard gate fires when a config is *saved*, so a user who was
 * already sitting on a 128K model was never stopped. The dispatch gate does refuse
 * their first AI request, and the sticky guidance links here — but if their gateway
 * happens to be unreachable, the live probe on this page can only say "unprobed",
 * and the one thing they need to see ("your model measures 128,000, pick another")
 * is invisible even though the server already knows it.
 *
 * `null` means "render nothing extra", and that covers two different inputs on
 * purpose: no cached verdict at all (a page render must never reject anybody — the
 * synchronous probe of tiers ①② belongs to the request path) and a qualified verdict
 * (nothing to warn about).
 */
export function gateRejectionFromCachedState(
  state: CachedGateState | null | undefined,
): GateRejection | null {
  if (!state || state.verdict === 'qualified') return null;
  return {
    verdict: typeof state.verdict === 'string' ? state.verdict : null,
    min_window: asNumber(state.min_window),
    measured_context_window_tokens: asNumber(state.measured_context_window_tokens),
    // A cached verdict never carries a declaration: the declaration is what the user
    // is about to type into this form, and `deriveGateNumbers` reads it live.
    declared_context_window_tokens: null,
    requires_explicit_declaration:
      typeof state.requires_explicit_declaration === 'boolean'
        ? state.requires_explicit_declaration
        : null,
  };
}
