/**
 * Issue #55 step 5 (legacy users): the hard gate fires when a config is *saved*, so a
 * user who was already sitting on a 128K model was never stopped. Their first AI
 * request is refused by the dispatch gate, and the sticky guidance links to this page —
 * which must then be able to say *why* even when the gateway cannot be reached again.
 *
 * So this drives the real page with a canned `GET /settings` that carries the cached
 * conclusion (`context_window_gate`) and a probe that never resolves successfully, and
 * asserts the three numbers appear on screen from the cache alone. DOM text
 * assertions, per AGENTS.md' visual-verification rule (grep alone is not evidence).
 *
 * Neutral placeholder values only — no imported book text, names or excerpts.
 */
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { App as AntApp } from 'antd';
import Settings from '../pages/Settings';
import i18n from '../i18n';
import enSettings from '../locales/en/settings.json';

vi.setConfig({ testTimeout: 20_000 });

/**
 * Zero-network policy, same seam as `settings-language-tab.test.tsx`: mounting Settings
 * fires a real GET /settings, and an unsettled request makes vitest exit 1 even when the
 * assertions went green. Keys carry the method; an `Error` payload rejects the call.
 */
const CANNED: Array<[RegExp, unknown]> = [];

const mockRoute = (key: RegExp, payload: unknown) => CANNED.unshift([key, payload]);

vi.mock('axios', async (importOriginal) => {
  const actual = await importOriginal<typeof import('axios')>();
  const adapter = async (config: { url?: string; method?: string }) => {
    const url = (config.url ?? '').replace(/^\/api/, '');
    const key = `${(config.method ?? 'get').toLowerCase()} ${url}`;
    const hit = CANNED.find(([re]) => re.test(key));
    const payload = hit ? hit[1] : {};
    if (payload instanceof Error) throw payload;
    return {
      data: structuredClone(payload),
      status: 200,
      statusText: 'OK',
      headers: {},
      config,
    };
  };
  actual.default.defaults.adapter = adapter;
  const instance = actual.default.create();
  instance.defaults.adapter = adapter;
  return {
    ...actual,
    default: Object.assign(instance, {
      create: actual.default.create,
      isAxiosError: actual.default.isAxiosError,
      AxiosError: actual.default.AxiosError,
      AxiosHeaders: actual.default.AxiosHeaders,
      CanceledError: actual.default.CanceledError,
      all: actual.default.all,
      spread: actual.default.spread,
    }),
  };
});

beforeAll(async () => {
  window.localStorage.setItem('lng', 'en');
  await i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
  CANNED.length = 0;
});

/** The conclusion the server already holds for gpt-4o-mini: measured 128K, rejected. */
const CACHED_BELOW_MINIMUM = {
  model: 'gpt-4o-mini',
  min_window: 1_000_000,
  verdict: 'unqualified',
  source: 'probe',
  measured_context_window_tokens: 128_000,
  requires_explicit_declaration: false,
  detail: 'probed before the upgrade',
  checked_at: '2026-01-01T00:00:00+00:00',
  due_for_recheck: false,
};

const LEGACY_CONFIG = {
  id: 'u-legacy-1',
  user_id: 'u-legacy-1',
  api_provider: 'openai',
  api_key: 'sk-stub-not-a-real-key',
  api_base_url: 'https://gw.test/v1',
  llm_model: 'gpt-4o-mini',
  temperature: 0.7,
  max_tokens: 2000,
  preferences: null,
  created_at: '2026-01-01T00:00:00',
  updated_at: '2026-01-01T00:00:00',
  context_window_gate: CACHED_BELOW_MINIMUM,
};

const ROUTES_OK = [
  [/^get \/settings$/, LEGACY_CONFIG],
  [/^get \/presets$/, []],
  [/^get \/mcp/, { plugins: [] }],
];

describe('legacy cached verdict reaches the settings gate form (issue #55 step 5)', () => {
  it('shows the probed 128,000 and the rejection copy with the probe unreachable', async () => {
    // The probe cannot help here: answering it with a network failure is exactly the
    // case where only the cached conclusion can tell the user what to do.
    for (const [re, payload] of ROUTES_OK) mockRoute(re, payload);
    mockRoute(/^post \/settings\/check-context-window$/, new Error('gateway unreachable'));

    render(
      <AntApp>
        <Settings />
      </AntApp>
    );

    expect(await screen.findByText('128,000')).toBeTruthy();
    expect(screen.getByText(enSettings.gate.status['below-minimum'])).toBeTruthy();
    expect(screen.getByText(enSettings.gate.noBypass)).toBeTruthy();
    // The third number is the one that must stay empty: a measured 128K model is never
    // adopted as a budget, so showing a number there would be the silent failure again.
    expect(screen.getByText(enSettings.gate.adoptedLabel)).toBeTruthy();
  });

  it('still runs the live probe, which is what overwrites the cached view when it works', async () => {
    for (const [re, payload] of ROUTES_OK) mockRoute(re, payload);
    mockRoute(/^post \/settings\/check-context-window$/, {
      success: true,
      supported: true,
      message: 'ok',
      response_time_ms: 1,
      provider: 'openai',
      model: 'gpt-4o-mini',
      details: {
        verdict: 'qualified',
        source: 'probe',
        min_window: 1_000_000,
        context_window_tokens: 1_048_576,
        window_display: {
          probed_context_window_tokens: 1_048_576,
          declared_context_window_tokens: null,
          minimum_required_context_window_tokens: 1_000_000,
          adopted_context_window_tokens: 1_048_576,
        },
      },
    });

    render(
      <AntApp>
        <Settings />
      </AntApp>
    );

    // Fresher evidence wins: once the gateway answers, the form shows the new number
    // and the cached rejection is gone.
    await waitFor(() => expect(screen.getAllByText('1,048,576').length).toBeGreaterThan(0));
    expect(screen.queryByText(enSettings.gate.status['below-minimum'])).toBeNull();
    expect(screen.getByText(enSettings.gate.status.qualified)).toBeTruthy();
  });
});
