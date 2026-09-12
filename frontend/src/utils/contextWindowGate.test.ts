/**
 * Issue #55 step 3b: the settings gate form must show three numbers that agree with
 * what the backend will actually do, because the gate is a hard save rejection with
 * no override checkbox. This pins the derivation itself (React-free) so the semantic
 * contract holds even though the visual check runs in a separate browser dispatch.
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  deriveGateNumbers,
  formatWindowTokens,
  gateEvidenceAfterProbe,
  gateRejectionFromCachedState,
  MIN_CONTEXT_WINDOW_TOKENS,
  NO_GATE_EVIDENCE,
  type ContextWindowProbe,
  type GateEvidence,
  type GateRejection,
} from './contextWindowGate';

const FRONTEND_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const MIN = MIN_CONTEXT_WINDOW_TOKENS;

function probe(
  verdict: string,
  tokens: number | null,
  supported: boolean,
  echoedDeclared: number | null = null,
): ContextWindowProbe {
  return {
    supported,
    details: {
      verdict,
      min_window: MIN,
      context_window_tokens: tokens,
      window_display: {
        probed_context_window_tokens: tokens,
        declared_context_window_tokens: echoedDeclared,
        minimum_required_context_window_tokens: MIN,
        adopted_context_window_tokens: supported ? MIN : null,
      },
    },
  };
}

describe('context window gate derivation', () => {
  it('pins the floor against the backend constant so the two cannot drift', () => {
    const source = readFileSync(
      join(FRONTEND_ROOT, '..', 'backend', 'app', 'services', 'model_capability_probe.py'),
      'utf8',
    );
    expect(source).toMatch(/MIN_CONTEXT_WINDOW_TOKENS\s*=\s*1_000_000\b/);
    expect(MIN).toBe(1_000_000);
  });

  it('a measured >=1M model adopts the measured window', () => {
    const gate = deriveGateNumbers(probe('qualified', MIN, true), null, null);
    expect(gate.status).toBe('qualified');
    expect(gate.probed).toBe(MIN);
    expect(gate.adopted).toBe(MIN);
    expect(gate.requiresDeclaration).toBe(false);
  });

  it('a measured sub-1M model stays rejected even when the user declares >=1M', () => {
    // This is the "no checkbox override" rule: `ensure_model_allowed` only consults a
    // declaration after the probe came back inconclusive, so the form must not pretend
    // a big enough number rescues a measured 128K model.
    const gate = deriveGateNumbers(probe('unqualified', 128_000, false), MIN, null);
    expect(gate.status).toBe('below-minimum');
    expect(gate.probed).toBe(128_000);
    expect(gate.declared).toBe(MIN);
    expect(gate.adopted).toBeNull();
    expect(gate.requiresDeclaration).toBe(false);
  });

  it('an inconclusive probe adopts an explicit declaration at or above the floor', () => {
    const accepted = deriveGateNumbers(probe('inconclusive', null, false), MIN, null);
    expect(accepted.status).toBe('needs-declaration');
    expect(accepted.probed).toBeNull();
    expect(accepted.adopted).toBe(MIN);
    expect(accepted.requiresDeclaration).toBe(true);

    const refused = deriveGateNumbers(probe('inconclusive', null, false), 200_000, null);
    expect(refused.status).toBe('needs-declaration');
    expect(refused.adopted).toBeNull();
  });

  it('never probed shows nothing adopted', () => {
    const gate = deriveGateNumbers(null, null, null);
    expect(gate.status).toBe('unprobed');
    expect(gate.probed).toBeNull();
    expect(gate.declared).toBeNull();
    expect(gate.adopted).toBeNull();
    expect(gate.minimum).toBe(MIN);
  });

  it('a rejected save fills in the three numbers when no probe ran yet', () => {
    // The user may hit 保存 before ever pressing 重新检测; the rejection envelope
    // carries the same three numbers so the panel is still on-screen.
    const rejection: GateRejection = {
      verdict: 'unqualified',
      min_window: MIN,
      measured_context_window_tokens: 128_000,
      declared_context_window_tokens: 128_000,
      requires_explicit_declaration: false,
    };
    const gate = deriveGateNumbers(null, 128_000, rejection);
    expect(gate.status).toBe('below-minimum');
    expect(gate.probed).toBe(128_000);
    expect(gate.adopted).toBeNull();
  });
});

describe('the third number is the server\'s, not a local re-derivation (issue #59 item 5)', () => {
  function rawProbe(
    windowDisplay: Record<string, unknown>,
    extra: Record<string, unknown> = {},
  ) {
    return {
      supported: (extra.supported as boolean) ?? false,
      details: {
        verdict: (extra.verdict as string) ?? 'inconclusive',
        min_window: MIN,
        window_display: windowDisplay,
      },
    } as ContextWindowProbe;
  }

  const display = (over: Record<string, unknown> = {}) => ({
    probed_context_window_tokens: null,
    declared_context_window_tokens: null,
    minimum_required_context_window_tokens: MIN,
    adopted_context_window_tokens: null,
    ...over,
  });

  it('shows the budget the server adopted even when it is not the measured window', () => {
    // 本地规则只会回「实测值」；后端哪天改成留安全边际，屏幕必须跟着改。
    // 这条用例在旧实现上必红：旧实现根本不看这个键。
    const gate = deriveGateNumbers(
      rawProbe(display({
        probed_context_window_tokens: 1_048_576,
        adopted_context_window_tokens: 1_000_000,
      }), { supported: true, verdict: 'qualified' }),
      null,
      null,
    );
    expect(gate.status).toBe('qualified');
    expect(gate.probed).toBe(1_048_576);
    expect(gate.adopted).toBe(1_000_000);
    expect(formatWindowTokens(gate.adopted)).toBe('1,000,000');
  });

  it('shows a budget the local rule would never have produced', () => {
    const gate = deriveGateNumbers(
      rawProbe(display({
        probed_context_window_tokens: 4_000_000,
        adopted_context_window_tokens: 2_500_000,
      }), { supported: true, verdict: 'qualified' }),
      null,
      null,
    );
    expect(gate.adopted).toBe(2_500_000);
  });

  it('shows nothing adopted when the server says nothing is adopted', () => {
    // 反向：服务器回答「什么都不采纳」也必须被照实渲染，不能被本地规则盖成一个数。
    const gate = deriveGateNumbers(
      rawProbe(display({
        probed_context_window_tokens: 128_000,
        declared_context_window_tokens: 2_000_000,
        adopted_context_window_tokens: null,
      }), { verdict: 'unqualified' }),
      2_000_000,
      null,
    );
    expect(gate.status).toBe('below-minimum');
    expect(gate.adopted).toBeNull();
    expect(formatWindowTokens(gate.adopted)).toBe('—');
  });

  it('does not let a cached verdict inherit the budget of a probe it outranked', () => {
    // 缓存「实测 128K」压过一枪判不出的探测；那一枪的预算说的不是这个结论。
    const cached = gateRejectionFromCachedState({
      model: 'gpt-4o-mini',
      verdict: 'unqualified',
      source: 'probe',
      min_window: MIN,
      measured_context_window_tokens: 128_000,
      requires_explicit_declaration: false,
    });
    const gate = deriveGateNumbers(
      rawProbe(display({ adopted_context_window_tokens: 1_000_000 })),
      null,
      cached,
    );
    expect(gate.verdict).toBe('unqualified');
    expect(gate.status).toBe('below-minimum');
    expect(gate.adopted).toBeNull();
  });

  it('falls back to the live form field when the declaration changed after the probe', () => {
    // 探测回答的是它收到的那笔声明。用户改过之后，那个快照描述的已经不是眼前这次保存。
    const gate = deriveGateNumbers(
      rawProbe(display({
        declared_context_window_tokens: 2_000_000,
        adopted_context_window_tokens: 2_000_000,
      })),
      300_000,
      null,
    );
    expect(gate.verdict).toBe('inconclusive');
    expect(gate.declared).toBe(300_000);
    expect(gate.adopted).toBeNull();
  });

  it('still derives locally when the response carries no adopted key at all', () => {
    // 旧响应/别的端点：缺键不等于「服务器说了没有预算」，必须回退而不是塌成 0。
    const gate = deriveGateNumbers(
      rawProbe({
        probed_context_window_tokens: MIN,
        declared_context_window_tokens: null,
        minimum_required_context_window_tokens: MIN,
      }, { supported: true, verdict: 'qualified' }),
      null,
      null,
    );
    expect(gate.status).toBe('qualified');
    expect(gate.adopted).toBe(MIN);
    expect(formatWindowTokens(gate.adopted)).toBe('1,000,000');
  });
});

describe('formatWindowTokens', () => {
  it('renders a missing measurement as a dash, never as 0', () => {
    // 0 already means "whole-book injection disabled" in this codebase, so a bare 0
    // would read as a decision the probe never made.
    expect(formatWindowTokens(null)).toBe('—');
    expect(formatWindowTokens(MIN)).toBe('1,000,000');
  });
});

describe('settings page wiring (issue #55 step 3b)', () => {
  const source = readFileSync(
    join(FRONTEND_ROOT, 'src', 'pages', 'Settings.tsx'),
    'utf8',
  );

  it('no longer prefills gpt-4 as the model', () => {
    // Step 2 removed server-side model guessing; prefilling an 8K-window model in the
    // 404 fallback or in "reset" would write a guaranteed-rejected config back on the
    // very first save of a fresh install.
    expect(source).not.toMatch(/llm_model:\s*['"]gpt-4['"]/);
    expect(source).toMatch(/llm_model:\s*''/);
  });

  it('renders the three numbers and the declaration input', () => {
    expect(source).toContain('gate.probedLabel');
    expect(source).toContain('gate.declaredLabel');
    expect(source).toContain('gate.adoptedLabel');
    expect(source).toContain('name="context_window_tokens"');
    expect(source).toContain('gate.recheck');
  });

  it('has no override checkbox on the save path', () => {
    // Hard gate: nothing in the gate card may acknowledge-and-continue.
    const gateCard = source.slice(source.indexOf('gate.title'), source.indexOf('gate.stale'));
    expect(gateCard).not.toMatch(/Checkbox|allowBelow|override/i);
  });
});

describe('legacy closure from the cached verdict (issue #55 step 5)', () => {
  it('renders the below-minimum three numbers straight from a cached unqualified verdict', () => {
    // The user was never stopped at save time (the gate only exists since this
    // release), and their gateway may be unreachable right now — the conclusion the
    // server already holds must still reach the screen.
    const rejection = gateRejectionFromCachedState({
      model: 'gpt-4o-mini',
      verdict: 'unqualified',
      min_window: MIN,
      measured_context_window_tokens: 128_000,
      requires_explicit_declaration: false,
    });
    const gate = deriveGateNumbers(null, null, rejection);
    expect(gate.status).toBe('below-minimum');
    expect(gate.probed).toBe(128_000);
    expect(gate.adopted).toBeNull();
    expect(gate.requiresDeclaration).toBe(false);
  });

  it('says nothing when the triple was never concluded', () => {
    // A page render must never reject anybody: "no verdict" is resolved by the
    // synchronous probe of tiers ①② on the request path, not by the form.
    expect(gateRejectionFromCachedState(null)).toBeNull();
    expect(gateRejectionFromCachedState(undefined)).toBeNull();
    expect(deriveGateNumbers(null, null, gateRejectionFromCachedState(null)).status).toBe('unprobed');
  });

  it('carries a cached qualified verdict so the form agrees with the server', () => {
    // Since #59 an unreachable probe leaves the durable verdict alone, so hiding the
    // qualified case would demand a declaration for a model the save gate accepts —
    // the same silent lie, opposite direction.
    const carried = gateRejectionFromCachedState({
      verdict: 'qualified',
      source: 'user_declared',
      measured_context_window_tokens: MIN,
    });
    expect(carried?.verdict).toBe('qualified');
    const gate = deriveGateNumbers(null, null, carried);
    expect(gate.status).toBe('qualified');
    expect(gate.probed).toBe(MIN);
    expect(gate.adopted).toBe(MIN);
    expect(gate.requiresDeclaration).toBe(false);
  });

  it('settings page seeds the rejection from the cached state and never posts it back', () => {
    const source = readFileSync(join(FRONTEND_ROOT, 'src', 'pages', 'Settings.tsx'), 'utf8');
    expect(source).toContain('gateRejectionFromCachedState(cachedGate)');
    expect(source).toContain('const { context_window_gate: cachedGate, ...configFields } = settings');
    expect(source).toContain('...configFields');
    // The read-only conclusion must not stay inside the values written to the form.
    expect(source).not.toMatch(/\.\.\.settings,\s*\n\s*cover_api_provider/);
  });
});

describe('evidence ordering: a probe that could not decide is not new evidence (issue #59)', () => {
  const cachedUnqualified = gateRejectionFromCachedState({
    model: 'gpt-4o-mini',
    verdict: 'unqualified',
    source: 'probe',
    min_window: MIN,
    measured_context_window_tokens: 128_000,
    requires_explicit_declaration: false,
  });

  it('keeps the cached below-minimum numbers when the live probe could not decide', () => {
    // This is the exact browser-found regression: the settings page-load probe hits an
    // unreachable gateway, the backend answers HTTP 200 with `inconclusive`, and the
    // form used to blank the measured 128,000 into —/—/— plus "declare a window".
    const gate = deriveGateNumbers(probe('inconclusive', null, false), null, cachedUnqualified);
    expect(gate.verdict).toBe('unqualified');
    expect(gate.status).toBe('below-minimum');
    expect(gate.probed).toBe(128_000);
    expect(gate.adopted).toBeNull();
    expect(gate.requiresDeclaration).toBe(false);
  });

  it('still lets a fresh measurement replace carried evidence, both directions', () => {
    // 实测之间只看新旧：网关真升配了必须重新接纳，真降配了必须重新拒绝。
    const cacheQualified = gateRejectionFromCachedState({
      model: 'gpt-4o-mini',
      verdict: 'qualified',
      min_window: MIN,
      measured_context_window_tokens: 1_048_576,
    });
    const upgraded = deriveGateNumbers(probe('inconclusive', null, false), null, cacheQualified);
    expect(upgraded.status).toBe('qualified');
    expect(upgraded.probed).toBe(1_048_576);

    const demoted = deriveGateNumbers(probe('unqualified', 128_000, false), MIN, cacheQualified);
    expect(demoted.status).toBe('below-minimum');
    expect(demoted.adopted).toBeNull();
  });

  it('demands a declaration when nothing stronger than "could not decide" is known', () => {
    // 只有「从未定论」或「判不出覆盖判不出」才该走到要求声明这一态。
    const cacheUnknown = gateRejectionFromCachedState({
      model: 'mystery-model',
      verdict: 'inconclusive',
      min_window: MIN,
      measured_context_window_tokens: null,
      requires_explicit_declaration: true,
    });
    const gate = deriveGateNumbers(probe('inconclusive', null, false), null, cacheUnknown);
    expect(gate.status).toBe('needs-declaration');
    expect(gate.probed).toBeNull();
  });

  it('drops carried evidence that a probe of the same or greater strength replaced', () => {
    const carried: GateEvidence = { evidence: cachedUnqualified, model: 'gpt-4o-mini' };
    expect(gateEvidenceAfterProbe(carried, probe('inconclusive', null, false), 'gpt-4o-mini'))
      .toBe(carried);
    expect(gateEvidenceAfterProbe(carried, probe('qualified', MIN, true), 'gpt-4o-mini'))
      .toEqual(NO_GATE_EVIDENCE);
    // A measurement of this very model replaces the cache too.
    expect(gateEvidenceAfterProbe(carried, probe('unqualified', 128_000, false), 'gpt-4o-mini'))
      .toEqual(NO_GATE_EVIDENCE);
  });

  it('never carries evidence across models', () => {
    // 数字只对测量它的那台模型成立：换了模型还摆着旧数字＝凭空造一条没测过的结论。
    const carried: GateEvidence = { evidence: cachedUnqualified, model: 'gpt-4o-mini' };
    expect(gateEvidenceAfterProbe(carried, probe('inconclusive', null, false), 'other-model'))
      .toEqual(NO_GATE_EVIDENCE);
    expect(gateEvidenceAfterProbe(NO_GATE_EVIDENCE, null, 'other-model')).toEqual(NO_GATE_EVIDENCE);
  });
});
