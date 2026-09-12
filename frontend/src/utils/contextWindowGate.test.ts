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
  MIN_CONTEXT_WINDOW_TOKENS,
  type ContextWindowProbe,
  type GateRejection,
} from './contextWindowGate';

const FRONTEND_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const MIN = MIN_CONTEXT_WINDOW_TOKENS;

function probe(verdict: string, tokens: number | null, supported: boolean): ContextWindowProbe {
  return {
    supported,
    details: {
      verdict,
      min_window: MIN,
      context_window_tokens: tokens,
      window_display: {
        probed_context_window_tokens: tokens,
        declared_context_window_tokens: null,
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
