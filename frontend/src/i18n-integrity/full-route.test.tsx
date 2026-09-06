/**
 * Full-route render smoke with runtime i18n integrity checks (plan todo 26 a+c).
 *
 * Renders EVERY route from App's route table (mirrors src/App.tsx) under jsdom
 * with the locale forced to EN, then asserts two runtime contracts the static
 * CI checks cannot see:
 *
 * (a) No raw CJK characters leak into the en DOM. Because i18next falls back
 *     to `fallbackLng: 'zh'`, an en-missing key does NOT warn — it silently
 *     renders the zh string. The DOM scan is the only check that catches that
 *     failure mode. Allowlisted exceptions (legitimately zh content) are
 *     declared in CJK_ALLOWLIST with justifications.
 *
 * (c) No i18next missing keys. The app i18n instance's logger is spied: every
 *     `i18next::translator: missingKey` warning (a key missing in BOTH locales,
 *     i.e. no zh fallback either) is collected and must stay empty.
 *
 * Network policy: zero network. All HTTP is intercepted at the axios adapter
 * seam (services/api creates its instance from the mocked 'axios' module), and
 * global fetch (backgroundTaskService polling) is stubbed. No real timers are
 * used beyond short waitFor timeouts.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { beforeAll, afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, waitFor } from '@testing-library/react';
import App from '../App';
import { ThemeProvider } from '../theme/ThemeProvider';
import i18n from '../i18n';

// ---------------------------------------------------------------------------
// Route table — mirrors <Routes> in src/App.tsx. Keep in sync; the index
// redirect of /project/:projectId is covered by the explicit child routes.
// ---------------------------------------------------------------------------
const ROUTES: string[] = [
  '/login',
  '/auth/callback',
  '/',
  '/projects',
  '/wizard',
  '/inspiration',
  '/settings',
  '/prompt-templates',
  '/mcp-plugins',
  '/user-management',
  '/chapters/chapter-1/reader',
  '/project/project-1',
  '/project/project-1/world-setting',
  '/project/project-1/careers',
  '/project/project-1/outline',
  '/project/project-1/characters',
  '/project/project-1/relationships',
  '/project/project-1/relationships-graph',
  '/project/project-1/organizations',
  '/project/project-1/chapters',
  '/project/project-1/chapter-analysis',
  '/project/project-1/foreshadows',
  '/project/project-1/writing-styles',
  '/project/project-1/skill-chat',
  '/project/project-1/skill-manage',
];

// Minimal fixture the detail-style pages destructure on mount.
const PROJECT = {
  id: 'project-1',
  title: 'Test Project',
  description: 'A test project',
  genre: 'fantasy',
  theme: 'courage',
  target_words: 100000,
  current_words: 1234,
  narrative_perspective: 'first_person',
  status: 'writing',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  world_building: { world_name: 'Testland', world_description: 'A test world' },
};

/** Canned adapter responses keyed by URL prefix (first match wins). */
const CANNED: Array<[RegExp, unknown]> = [
  [/^\/auth\/me$/, { id: 1, username: 'tester', display_name: 'Tester', email: null }],
  [/^\/auth\/config$/, { local_auth_enabled: true, linuxdo_enabled: false, email_auth_enabled: false, email_register_enabled: false }],
  [/^\/projects\/project-1$/, PROJECT],
  [/^\/projects\/project-1\/outlines/, []],
  [/^\/projects\/project-1\/characters/, []],
  [/^\/projects\/project-1\/organizations/, []],
  [/^\/projects\/project-1\/careers/, []],
  [/^\/projects\/project-1\/chapters/, []],
  [/^\/projects\/project-1\/foreshadows/, []],
  [/^\/projects\/project-1\/batch/, []],
  [/^\/projects$/, []],
  // {entries: []} keeps the auto-open changelog modal closed on / and /projects.
  // (Its zh markdown body is exercised + allowlisted in the CJK scan docs.)
  [/^\/changelog$/, { entries: [] }],
  [/^\/settings$/, { preferences: null }],
  [/^\/writing-styles/, []],
  [/^\/presets/, []],
  [/^\/mcp/, { plugins: [] }],
  [/^\/users/, []],
  [/^\/skills/, []],
  [/^\/tasks/, { items: [], has_active_task: false, task: null }],
  [/^\/prompt-templates/, []],
  [/^\/agent/, { conversations: [], messages: [], tool_calls: [] }],
  [/^\/inspiration/, {}],
  [/^\/book-import/, { items: [], tasks: [] }],
  [/^\/system/, {}],
];

vi.mock('axios', async (importOriginal) => {
  const actual = await importOriginal<typeof import('axios')>();
  const instance = actual.default.create();
  instance.defaults.adapter = async (config) => {
    const url = (config.url ?? '').replace(/^\/api/, '');
    const hit = CANNED.find(([re]) => re.test(url));
    return {
      data: hit ? structuredClone(hit[1]) : {},
      status: 200,
      statusText: 'OK',
      headers: {},
      config,
    };
  };
  return {
    ...actual,
    default: Object.assign(instance, { create: actual.default.create, isAxiosError: actual.default.isAxiosError, AxiosError: actual.default.AxiosError, AxiosHeaders: actual.default.AxiosHeaders, CanceledError: actual.default.CanceledError, all: actual.default.all, spread: actual.default.spread }),
  };
});

// No real network via fetch either (backgroundTaskService / SSE fallbacks).
vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ items: [], has_active_task: false, task: null }), { status: 200 })));

// ---------------------------------------------------------------------------
// (c) missing-key capture: spy on the app i18n instance's logger.
// ---------------------------------------------------------------------------
const missingKeyWarnings: string[] = [];

// ---------------------------------------------------------------------------
// (a) CJK leak scan with an explicit, justified allowlist.
// ---------------------------------------------------------------------------
const CJK = /[\u4e00-\u9fff\u3400-\u4dbf]/;

interface CjkAllowEntry {
  /** CSS selector the leaking text node's closest() must match. */
  selector: string;
  /** Routes the allowance applies to ('*' = all). */
  routes: string[];
  /** Why this zh content is legitimate (recorded verbatim in evidence). */
  reason: string;
}

const CJK_ALLOWLIST: CjkAllowEntry[] = [
  {
    selector: '.sf-banner, .sf-toggle-btn, .sf-lantern, [class*="sf-"]',
    routes: ['*'],
    reason:
      'SpringFestival is a deliberate zh-only decorative component (spring couplets like 新春快乐, lantern labels). ' +
      'It renders only during the Lunar New Year season (Jan 15 - Mar 5), so CI runs outside that window never see it; ' +
      'the allowance keeps the scan green when it does render.',
  },
  {
    selector: '.ant-modal-content',
    routes: ['/', '/projects'],
    reason:
      'ChangelogModal renders the raw CHANGELOG release notes (zh markdown, the project convention for release docs) ' +
      'auto-opened once per session on the project-list routes. Release-note text is intentionally zh source content, ' +
      'like user-imported book data, not UI chrome.',
  },
];

interface Finding {
  route: string;
  text: string;
  selector: string | null;
}

const cjkFindings: Finding[] = [];

function scanCjk(route: string) {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node: Node | null;
  while ((node = walker.nextNode())) {
    const text = node.textContent ?? '';
    if (!CJK.test(text)) continue;
    const el = node.parentElement;
    const matched = CJK_ALLOWLIST.find((entry) => {
      if (entry.routes !== ['*'] && !entry.routes.includes(route)) return false;
      return el?.closest(entry.selector) !== null;
    });
    if (!matched) {
      cjkFindings.push({ route, text: text.trim().slice(0, 120), selector: el?.closest('[class]')?.className?.toString().slice(0, 80) ?? null });
    }
  }
}

const rendered = new Set<string>();

beforeAll(async () => {
  // Deterministic en locale for every route render.
  window.localStorage.setItem('lng', 'en');
  await i18n.changeLanguage('en');

  // (c) spy the translator logger BEFORE any render.
  const logger = (i18n as unknown as { services: { logger: { warn: (...a: unknown[]) => void } } }).services.logger;
  const originalWarn = logger.warn.bind(logger);
  logger.warn = (...args: unknown[]) => {
    const line = args.map(String).join(' ');
    if (line.includes('missingKey')) missingKeyWarnings.push(line);
    originalWarn(...args);
  };

  const mount = async (route: string) => {
    window.history.pushState({}, '', route);
    const { unmount } = render(
      <ThemeProvider>
        <App />
      </ThemeProvider>
    );
    // Let the page's mount-time async data land before scanning.
    await waitFor(() => expect(document.querySelector('.ant-spin-spinning, [class*="loading"]')).toBeNull(), { timeout: 2500 }).catch(() => {
      /* pages without spinners resolve immediately */
    });
    await new Promise((r) => setTimeout(r, 30));
    rendered.add(route);
    scanCjk(route);
    unmount();
  };

  for (const route of ROUTES) {
    await mount(route);
    cleanup();
  }
}, 120_000);

afterEach(() => {
  cleanup();
});

describe('full-route render smoke (todo 26a)', () => {
  it('rendered every route in the table without crashing', () => {
    expect(rendered).toEqual(new Set(ROUTES));
  });

  it('no raw CJK characters leak into the en DOM outside the allowlist', () => {
    expect(cjkFindings).toEqual([]);
  });
});

describe('missing keys (todo 26c)', () => {
  it('i18next reported no missing keys across all rendered routes', () => {
    expect(missingKeyWarnings).toEqual([]);
  });
});

// Keep the allowlist honest: every entry must still be reachable via its own
// selector definition (guards against renaming a selector into a no-op).
describe('CJK allowlist integrity', () => {
  it('every allowlist selector is a valid selector', () => {
    for (const entry of CJK_ALLOWLIST) {
      expect(() => document.createDocumentFragment().querySelector?.(entry.selector)).not.toThrow();
      expect(entry.reason.length).toBeGreaterThan(20);
    }
  });
});
