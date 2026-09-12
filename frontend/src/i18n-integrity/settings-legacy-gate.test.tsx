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
 * Since issue #59 the same cases also pin the **steady state**: a probe that could not
 * decide is a non-measurement and must leave the cached conclusion on screen. The
 * undecidable answer is therefore canned in the shape the backend really sends — HTTP 200
 * with `details.verdict: "inconclusive"` — not as a transport error, which is the mock
 * that let the state loss through.
 *
 * Neutral placeholder values only — no imported book text, names or excerpts.
 */
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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

/**
 * Every probe the page actually posted, in order. Item #59's second half is a claim
 * about *absence* ("rendering this page must not call the user's gateway"), and an
 * assertion on rendered text cannot express absence — a stale cached number looks
 * identical whether or not a probe ran. So the transport seam records the requests.
 */
const PROBE_CALLS: string[] = [];

const mockRoute = (key: RegExp, payload: unknown) => CANNED.unshift([key, payload]);

vi.mock('axios', async (importOriginal) => {
  const actual = await importOriginal<typeof import('axios')>();
  const adapter = async (config: { url?: string; method?: string }) => {
    const url = (config.url ?? '').replace(/^\/api/, '');
    const key = `${(config.method ?? 'get').toLowerCase()} ${url}`;
    if (/^post \/settings\/check-context-window$/.test(key)) PROBE_CALLS.push(key);
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
  PROBE_CALLS.length = 0;
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

/**
 * What `POST /settings/check-context-window` really answers when the user's gateway is
 * down: HTTP 200, `success: true` (the endpoint only measures, it never rejects) and a
 * verdict of `inconclusive`. Mocking an axios transport failure cannot reach this state,
 * which is precisely why the issue #59 state loss survived the jsdom suite.
 */
const undecidableProbeResponse = {
  success: true,
  supported: false,
  message: 'probe could not decide',
  response_time_ms: 1,
  provider: 'openai',
  model: 'gpt-4o-mini',
  details: {
    verdict: 'inconclusive',
    source: 'probe',
    tier: 'max_tokens_bound',
    min_window: 1_000_000,
    context_window_tokens: null,
    detail: 'max_tokens tier request failed: ConnectError: connection refused',
    window_display: {
      probed_context_window_tokens: null,
      declared_context_window_tokens: null,
      minimum_required_context_window_tokens: 1_000_000,
      adopted_context_window_tokens: null,
    },
  },
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

/**
 * The same user one release later: their conclusion is `qualified` (declared, because the
 * probe could not decide back then). Since #59 the server keeps that verdict across an
 * unreachable probe, so the form must not ask them to declare a window it is about to
 * accept — a demand whose number would not be the number actually adopted.
 */
const DECLARED_CONFIG = {
  ...LEGACY_CONFIG,
  id: 'u-legacy-2',
  user_id: 'u-legacy-2',
  context_window_gate: {
    model: 'gpt-4o-mini',
    min_window: 1_000_000,
    verdict: 'qualified',
    source: 'user_declared',
    measured_context_window_tokens: 1_000_000,
    requires_explicit_declaration: false,
    detail: 'user declared 1000000 tokens',
    checked_at: '2026-01-01T00:00:00+00:00',
    due_for_recheck: false,
  },
};

/**
 * The gate card's 「Re-check」 handle.
 *
 * Located by label text rather than `getByRole`: jsdom's `getComputedStyle` is only
 * partly implemented here (antd's own layout code throws from it in this suite), which
 * leaves the accessibility tree empty, so role queries can never match.
 */
const recheckButton = (): HTMLElement => {
  const label = screen.getByText(enSettings.gate.recheck);
  const button = label.closest('button');
  if (!button) throw new Error('Re-check label rendered outside a button element');
  return button;
};

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

  it('keeps the cached 128,000 when the probe answers 200 with an undecidable verdict', async () => {
    // The shape the transport-failure case above could never catch, and the regression
    // issue #59 is about: a dead gateway is not an axios error, it is HTTP 200 with
    // `details.verdict: "inconclusive"` (and, before the fix, a cache the backend had
    // already overwritten). A non-measurement must not blank the measured number into
    // —/—/— plus "declare a context window at or above the minimum" — that demand is the
    // exact exit a measured sub-1M verdict exists to close.
    for (const [re, payload] of ROUTES_OK) mockRoute(re, payload);
    mockRoute(/^post \/settings\/check-context-window$/, undecidableProbeResponse);

    render(
      <AntApp>
        <Settings />
      </AntApp>
    );

    // The cached conclusion is on screen first; the probe is what used to flip it, so
    // press the button that measures and assert the **steady state** afterwards, not the
    // first frame.
    expect(await screen.findByText('128,000')).toBeTruthy();
    fireEvent.click(recheckButton());
    await waitFor(() => expect(PROBE_CALLS.length).toBe(1));
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(screen.getAllByText('128,000').length).toBeGreaterThan(0);
    expect(screen.getByText(enSettings.gate.status['below-minimum'])).toBeTruthy();
    expect(screen.queryByText(enSettings.gate.status['needs-declaration'])).toBeNull();
    expect(screen.queryByText('—/—/—')).toBeNull();
  });

  it('does not demand a declaration for a model the server already accepts', async () => {
    mockRoute(/^get \/settings$/, DECLARED_CONFIG);
    mockRoute(/^get \/presets$/, []);
    mockRoute(/^get \/mcp/, { plugins: [] });
    mockRoute(/^post \/settings\/check-context-window$/, undecidableProbeResponse);

    render(
      <AntApp>
        <Settings />
      </AntApp>
    );

    expect(await screen.findByText(enSettings.gate.status.qualified)).toBeTruthy();
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(screen.getAllByText('1,000,000').length).toBeGreaterThan(0);
    expect(screen.queryByText(enSettings.gate.status['needs-declaration'])).toBeNull();
  });

  it('replaces the cached view when the user explicitly presses Re-check', async () => {
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

    expect(await screen.findByText('128,000')).toBeTruthy();
    // The button is what measures. This used to be a side effect of mounting, and
    // mounting is exactly what must stop calling the user's gateway (#59).
    fireEvent.click(recheckButton());

    // Fresher evidence wins: once the gateway answers, the form shows the new number
    // and the cached rejection is gone.
    await waitFor(() => expect(screen.getAllByText('1,048,576').length).toBeGreaterThan(0));
    expect(screen.queryByText(enSettings.gate.status['below-minimum'])).toBeNull();
    expect(screen.getByText(enSettings.gate.status.qualified)).toBeTruthy();
  });

  it('posts no probe at all just to render a page whose verdict the server already holds', async () => {
    // #59 的第二个洞：`e4d3f6c` 让每次打开设置页都打一发静默探测。渲染不是 AI 功能，
    // 而这一枪在网关不可达时把实测结论抹成了「判不出」。这里断言的是** absence **：
    // 屏幕上残留 128,000 并不足以证明没发请求，必须数请求本身。
    for (const [re, payload] of ROUTES_OK) mockRoute(re, payload);
    mockRoute(/^post \/settings\/check-context-window$/, undecidableProbeResponse);

    render(
      <AntApp>
        <Settings />
      </AntApp>
    );

    expect(await screen.findByText('128,000')).toBeTruthy();
    // Past the microtask/macrotask window the mount effect used to need to fire.
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(PROBE_CALLS).toEqual([]);
    expect(screen.getAllByText('128,000').length).toBeGreaterThan(0);
  });

  it('still probes on first-time configuration, where the server holds nothing to show', async () => {
    // The one case the mount probe must survive: a triple with no verdict at all would
    // otherwise render 「未检测」 with no numbers, and the user would have no idea the
    // field is the thing to fix. #59 narrows the probe to this case; it does not remove it.
    for (const [re, payload] of ROUTES_OK) mockRoute(re, payload);
    mockRoute(/^get \/settings$/, { ...LEGACY_CONFIG, context_window_gate: null });
    mockRoute(/^post \/settings\/check-context-window$/, {
      success: true,
      supported: false,
      message: 'probed',
      response_time_ms: 1,
      provider: 'openai',
      model: 'gpt-4o-mini',
      details: {
        verdict: 'unqualified',
        source: 'probe',
        min_window: 1_000_000,
        context_window_tokens: 128_000,
        window_display: {
          probed_context_window_tokens: 128_000,
          declared_context_window_tokens: null,
          minimum_required_context_window_tokens: 1_000_000,
          adopted_context_window_tokens: null,
        },
      },
    });

    render(
      <AntApp>
        <Settings />
      </AntApp>
    );

    // At least one: this page runs `loadSettings()` from two mount effects (the initial
    // one and the `activeTab` one), so a first-time triple is probed twice. That
    // duplication predates #59 and is not what this case pins — here it only matters that
    // a triple the server knows nothing about still gets measured on mount.
    await waitFor(() => expect(PROBE_CALLS.length).toBeGreaterThan(0));
    expect(await screen.findByText(enSettings.gate.status['below-minimum'])).toBeTruthy();
  });
});
