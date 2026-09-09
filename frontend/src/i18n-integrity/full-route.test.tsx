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
 * Network policy: zero network. All axios HTTP is intercepted at the axios
 * global-defaults adapter seam (see the vi.mock factory below for why the
 * seam must be `axios.defaults.adapter`, not a replaced module export), and
 * global fetch (backgroundTaskService polling, fetch-based pages) is stubbed.
 * No real timers are used beyond short waitFor timeouts.
 */
import { afterAll, beforeAll, afterEach, describe, expect, it, vi } from 'vitest';
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

/**
 * Canned adapter responses keyed by URL prefix (first match wins). Shapes must
 * mirror what each page's consumer actually destructures — a wrong shape (e.g.
 * `{plugins: []}` where the page maps a bare array) crashes the render tree,
 * and a crashed tree makes the CJK/missing-key scans vacuous for that route.
 * Envelope notes per entry: `{total, items}` for project-scoped lists
 * (api.ts unwraps `.items`), bare arrays for raw-axios consumers
 * (Relationships/Organizations map `res.data` directly).
 */
const CANNED: Array<[RegExp, unknown]> = [
  [/^\/auth\/user$/, { id: 1, username: 'tester', display_name: 'Tester', email: null, is_admin: true }],
  [/^\/auth\/config$/, { local_auth_enabled: true, linuxdo_enabled: false, email_auth_enabled: false, email_register_enabled: false }],
  [/^\/projects\/project-1$/, PROJECT],
  [/^\/projects\/project-1\/agent\/conversations$/, []],
  [/^\/projects$/, []],
  // {total, items} list envelopes (api.ts unwraps `.items`)
  [/^\/outlines\/project\/project-1$/, { total: 0, items: [] }],
  [/^\/characters\/project\/project-1$/, { total: 0, items: [] }],
  [/^\/chapters\/project\/project-1$/, { total: 0, items: [] }],
  [/^\/foreshadows\/projects\/project-1$/, { total: 0, items: [] }],
  // bare-array / raw consumers (Relationships & Organizations map res.data directly)
  [/^\/relationships\/project\/project-1$/, []],
  [/^\/relationships\/types/, []],
  [/^\/organizations\/project\/project-1$/, []],
  [/^\/organizations\/[^/]+\/members$/, []],
  [/^\/characters$/, { items: [] }],
  [/^\/careers/, { main_careers: [], sub_careers: [] }],
  [/^\/writing-styles/, { styles: [], total: 0 }],
  // Chapter reader (ChapterReader.tsx validates content on mount)
  [/^\/chapters\/chapter-1$/, { id: 'chapter-1', chapter_number: 1, title: 'Chapter One', content: 'A test chapter body.', word_count: 22 }],
  [/^\/chapters\/chapter-1\/annotations$/, { chapter_id: 'chapter-1', chapter_number: 1, title: 'Chapter One', word_count: 22, annotations: [], has_analysis: false, summary: { total_annotations: 0, hooks: 0, foreshadows: 0, plot_points: 0, character_events: 0 } }],
  [/^\/chapters\/chapter-1\/navigation$/, { current: { id: 'chapter-1', chapter_number: 1, title: 'Chapter One' }, previous: null, next: null }],
  // {entries: []} keeps the auto-open changelog modal closed on / and /projects.
  // (Its zh markdown body is exercised + allowlisted in the CJK scan docs.)
  [/^\/changelog$/, { entries: [] }],
  [/^\/settings$/, { preferences: null }],
  [/^\/presets/, []],
  [/^\/mcp/, []],
  [/^\/admin\/users/, { total: 0, users: [] }],
  [/^\/users/, []],
  [/^\/skills/, []],
  [/^\/tasks/, { items: [], has_active_task: false, task: null }],
  [/^\/prompt-templates/, []],
  [/^\/agent/, []],
  [/^\/inspiration/, {}],
  [/^\/book-import/, { items: [], tasks: [] }],
  [/^\/system/, {}],
];

vi.mock('axios', async (importOriginal) => {
  const actual = await importOriginal<typeof import('axios')>();
  const adapter = async (config) => {
    // Query strings are stripped: raw-axios consumers embed params in the URL
    // (`/characters?project_id=...`), and fixtures key on path only.
    const url = (config.url ?? '').replace(/^\/api/, '').split('?')[0];
    const hit = CANNED.find(([re]) => re.test(url));
    return {
      data: hit ? structuredClone(hit[1]) : {},
      status: 200,
      statusText: 'OK',
      headers: {},
      config,
    };
  };
  // services/api builds its instance via axios.create() at module load, and
  // mergeConfig snapshots `defaults.adapter` at create time. Mutating the real
  // global defaults HERE (before any instance exists) is the only seam that
  // reaches it; replacing the module's default export with a pre-built
  // instance is not — its `create` is the real one, so services/api still got
  // a live XHR adapter and every protected route silently rendered /login.
  actual.default.defaults.adapter = adapter;
  const instance = actual.default.create();
  instance.defaults.adapter = adapter;
  return {
    ...actual,
    default: Object.assign(instance, { create: actual.default.create, isAxiosError: actual.default.isAxiosError, AxiosError: actual.default.AxiosError, AxiosHeaders: actual.default.AxiosHeaders, CanceledError: actual.default.CanceledError, all: actual.default.all, spread: actual.default.spread }),
  };
});

// No real network via fetch either (backgroundTaskService / SSE fallbacks).
// URL-aware so fetch-based pages (e.g. SkillManage's /skills/list) get a shape
// their consumer can map; everything else gets the task-polling envelope.
vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
  const raw = typeof input === 'string' ? input : input instanceof URL ? input.pathname : input.url;
  const url = raw.replace(/^https?:\/\/[^/]+/, '').replace(/^\/api/, '');
  const payload = /^\/skills\/list/.test(url) ? [] : { items: [], has_active_task: false, task: null };
  return new Response(JSON.stringify(payload), { status: 200 });
}));

// jsdom implements neither element scroll API; pages call both in mount
// effects (Inspiration.tsx scrollTo, ProjectAgentPanel/SkillChat
// scrollIntoView). An uncaught effect error unmounts the whole tree and
// turns every later scan on that route vacuous.
Element.prototype.scrollIntoView = vi.fn();
Element.prototype.scrollTo = vi.fn();

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
      if (!entry.routes.includes('*') && !entry.routes.includes(route)) return false;
      return el?.closest(entry.selector) !== null;
    });
    if (!matched) {
      cjkFindings.push({ route, text: text.trim().slice(0, 120), selector: el?.closest('[class]')?.className?.toString().slice(0, 80) ?? null });
    }
  }
}

const rendered = new Set<string>();

/**
 * Regression guard for the axios adapter seam (#38). ProtectedRoute decides
 * auth purely by whether `authApi.getCurrentUser()` resolves, so when the
 * canned adapter does not reach services/api's instance every protected route
 * silently degrades to <Navigate to="/login"> — the suite stays green while
 * scanning login pages instead of the pages it claims to cover. Recording the
 * landed URL per route makes that failure loud.
 */
const landedPaths = new Map<string, string>();
const PROTECTED_ROUTES = ROUTES.filter((r) => r !== '/login' && r !== '/auth/callback');

/** /project/:projectId legitimately lands on its world-setting child (App.tsx index redirect). */
const EXPECTED_LANDING: Record<string, string> = {
  '/project/project-1': '/project/project-1/world-setting',
};
const landingFor = (route: string) => EXPECTED_LANDING[route] ?? route;

/**
 * React logs "The above error occurred in ..." via console.error when a
 * component crash unmounts the tree — including crashes inside async
 * continuations that render() itself cannot observe. Together with the
 * per-route non-empty-DOM check below, this closes the last vacuous path:
 * a crashed tree would otherwise pass the redirect guard (it never reaches
 * /login) while scanning nothing.
 */
const crashFindings: string[] = [];
const emptyRenderFindings: string[] = [];
const currentRoute = { value: '' };
const originalConsoleError = console.error.bind(console);
afterAll(() => {
  console.error = originalConsoleError;
});

/** True when canned PROJECT data actually reaches the DOM through services/api. */
const sawCannedProjectTitle = { value: false };

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

  // Capture React tree-unmount crash reports ("The above error occurred in...")
  // that render() cannot surface synchronously.
  console.error = (...args: unknown[]) => {
    const line = args.map(String).join(' ');
    if (line.includes('The above error occurred in')) {
      crashFindings.push(`${currentRoute.value}: ${line.slice(0, 200)}`);
    }
    originalConsoleError(...args);
  };

  const mount = async (route: string) => {
    currentRoute.value = route;
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
    landedPaths.set(route, window.location.pathname);
    const bodyText = (document.body.textContent ?? '').trim();
    if (bodyText.length < 20) {
      emptyRenderFindings.push(`${route} (${bodyText.length} chars: "${bodyText.slice(0, 60)}")`);
    }
    if (route === '/project/project-1' && bodyText.includes(PROJECT.title)) {
      sawCannedProjectTitle.value = true;
    }
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

  it('protected routes render their own page, never a redirect (#38)', () => {
    const redirected = PROTECTED_ROUTES.filter((r) => landedPaths.get(r) !== landingFor(r));
    expect(redirected).toEqual([]);
  });

  it('canned project data reaches the DOM through the real api instance (#38)', () => {
    expect(sawCannedProjectTitle.value).toBe(true);
  });

  it('every route renders a non-crashed, non-empty tree (#38)', () => {
    expect(crashFindings).toEqual([]);
    expect(emptyRenderFindings).toEqual([]);
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
