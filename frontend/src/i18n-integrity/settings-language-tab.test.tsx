/**
 * Issue #37: language settings must live in their own tab, not loose cards
 * above the API tabs. antd v5 Tabs mount inactive panes lazily, so "not in the
 * DOM before the tab is clicked" is the proof the cards actually moved.
 */
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { App as AntApp } from 'antd';
import Settings from '../pages/Settings';
import i18n from '../i18n';
import enSettings from '../locales/en/settings.json';
import zhSettings from '../locales/zh/settings.json';

/**
 * Zero-network policy, same seam as full-route.test.tsx. Mounting Settings fires
 * a real GET /settings; when that jsdom XHR rejects after the environment is
 * torn down, React's queued commit throws `ReferenceError: window is not
 * defined` and vitest exits 1 despite green assertions. Canned adapter
 * responses keep every request settled while its test is still alive, and the
 * component itself stays unmocked.
 *
 * Keys carry the method (`"<method> <path>"`) because a failing preference
 * write is a different conversation from the GET that answers with the stale
 * server value -- tests need to answer the two differently. A payload that is an
 * `Error` makes the adapter reject, which is what drives the component's catch
 * branch. Lookup is first-match-wins, so `mockRoute` prepends an override.
 */
const DEFAULT_ROUTES: Array<[RegExp, unknown]> = [
  [/^get \/presets$/, []],
  [/^get \/settings$/, { preferences: null }],
  [/^get \/mcp/, { plugins: [] }],
];

const CANNED: Array<[RegExp, unknown]> = [...DEFAULT_ROUTES];

/** Every request the component fires, as `"<method> <path>"`, in order. */
const REQUESTS: string[] = [];

const mockRoute = (key: RegExp, payload: unknown) => {
  CANNED.unshift([key, payload]);
};

afterEach(() => {
  CANNED.length = 0;
  CANNED.push(...DEFAULT_ROUTES);
});

vi.mock('axios', async (importOriginal) => {
  const actual = await importOriginal<typeof import('axios')>();
  const adapter = async (config: { url?: string; method?: string }) => {
    const url = (config.url ?? '').replace(/^\/api/, '');
    const key = `${(config.method ?? 'get').toLowerCase()} ${url}`;
    REQUESTS.push(key);
    const hit = CANNED.find(([re]) => re.test(key));
    const payload = hit ? hit[1] : {};
    if (payload instanceof Error) {
      throw payload;
    }
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
});

describe('language settings live in their own tab (issue #37)', () => {
  it('renders no language card before the tab is opened', () => {
    render(
      <AntApp>
        <Settings />
      </AntApp>
    );
    expect(screen.queryByText('Interface language')).toBeNull();
    expect(screen.queryByText('Content language')).toBeNull();
  });

  it('reveals both language selects after clicking the Language tab', async () => {
    render(
      <AntApp>
        <Settings />
      </AntApp>
    );
    fireEvent.click(screen.getByRole('tab', { name: /Language/ }));
    expect(screen.getByText('Interface language')).toBeTruthy();
    expect(screen.getByText('Content language')).toBeTruthy();
    // Scope to the pane holding the language label: a document-wide count is
    // satisfied by the default pane's own two selects and would pass even if the
    // language pane never mounted.
    const pane = screen.getByText('Interface language').closest('.ant-tabs-tabpane-active');
    expect(pane).not.toBeNull();
    expect(pane!.querySelectorAll('.ant-select').length).toBe(2);
  });

  it('keeps four tabs with the API tab still the default view', () => {
    render(
      <AntApp>
        <Settings />
      </AntApp>
    );
    const tabs = screen.getAllByRole('tab').map((el) => el.textContent);
    expect(tabs).toEqual(['Text Model Config', 'Image Model Config', 'Config Presets', 'Language']);
    expect(document.querySelector('.ant-tabs-tab-active')?.textContent).toBe('Text Model Config');
  });
});

/**
 * Issue #37 made the shell top bar and the page hero read the same word, so
 * opening 设置 from the sidebar stacked two identical titles. The shell keeps
 * its own (it serves every view), so the hero must yield when embedded.
 * Assertions match by text/role, never by h2 vs h3: the hero's level follows
 * the mobile breakpoint.
 */
describe('settings hero yields to the shell top bar (issue #37 follow-up)', () => {
  it('keeps the hero title only on the bare route', () => {
    const shell = render(
      <AntApp>
        <Settings embedded />
      </AntApp>
    );
    // Inside the shell the top bar already says "Settings" -- a hero there put
    // the same word twice on screen, which is the regression.
    expect(screen.queryByText('Settings')).toBeNull();
    expect(screen.getByText(enSettings.subtitle)).toBeTruthy();
    // The module-level cleanup only runs between tests, so the first tree has to
    // go explicitly or its nodes would still answer the queries below.
    shell.unmount();

    // The bare /settings route has no top bar at all: drop the hero there and
    // the page loses its title entirely.
    render(
      <AntApp>
        <Settings />
      </AntApp>
    );
    expect(screen.getByRole('heading', { name: 'Settings' })).toBeTruthy();
  });
});

/**
 * Sentinels for the refresh proof below: they are never real model ids, just
 * values that show up in the API-config form so a test can tell a mount load
 * apart from a tab-switch reload.
 */
const MODEL_AT_MOUNT = 'model-at-mount';
const MODEL_AFTER_REFRESH = 'model-after-refresh';

/**
 * Issue #37 made the language cards reachable only through a tab click, so
 * "click back to the API tab" became the natural next action -- and that click
 * re-ran loadSettings(), which re-applied the account preference and quietly
 * undid the choice whose PUT had just failed. `language.syncFailed` promises the
 * opposite ("Saved locally, but syncing to server failed. It will apply on this
 * browser only."), so a refresh must never revert the local language.
 */
describe('a failed preference write survives the tab-switch refresh (issue #37 follow-up)', () => {
  it('keeps the language the user picked while the server still reports the old one', async () => {
    // The account keeps answering with the stale zh preference: the fix may not
    // stop the refresh itself, only let that answer overwrite local state.
    mockRoute(/^get \/settings$/, {
      id: 'settings-1',
      preferences: '{"language":"zh"}',
      llm_model: MODEL_AT_MOUNT,
    });
    // And the write that would have made the account agree fails.
    mockRoute(/^put \/settings\/preferences$/, new Error('network down'));
    REQUESTS.length = 0;

    render(
      <AntApp>
        <Settings />
      </AntApp>
    );

    // Mount still adopts the account preference: the "server wins after login"
    // contract lives in utils/languageSync.ts and this fix must not touch it.
    await waitFor(() => expect(i18n.language).toBe('zh'));
    expect(screen.getByText(MODEL_AT_MOUNT)).toBeTruthy();

    // The tab button's accessible name is prefixed by its icon label
    // ("global 语言"), hence the substring match.
    fireEvent.click(screen.getByRole('tab', { name: new RegExp(zhSettings.tabs.language) }));
    const pane = screen.getByText(zhSettings.language.label).closest('.ant-tabs-tabpane-active');
    fireEvent.mouseDown(pane!.querySelector('.ant-select .ant-select-selector')!);
    const dropdown = await waitFor(() => {
      const el = document.querySelector('.ant-select-dropdown:not(.ant-select-dropdown-hidden)');
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    const englishOption = await waitFor(() => {
      const el = Array.from(dropdown.querySelectorAll('.ant-select-item-option')).find(
        (item) => item.textContent === enSettings.language.enLabel,
      );
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    fireEvent.click(englishOption);

    // The local switch itself lands.
    await waitFor(() => expect(i18n.language).toBe('en'));
    // The premise: the write failed and the app told the user the choice applies
    // on this browser only. The warning is rendered with the `t` captured by the
    // pre-switch render, so its copy is one commit behind -- accept either locale.
    await waitFor(() =>
      expect(
        [enSettings.language.syncFailed, zhSettings.language.syncFailed].some(
          (copy) => screen.queryByText(copy) !== null,
        ),
      ).toBe(true),
    );
    expect(REQUESTS).toContain('put /settings/preferences');

    // The refresh must still refresh the API config, so the canned answer now
    // carries a different model -- but still the stale zh preference. Since
    // loadSettings applies the server language *before* it fills the form, the
    // new model reaching the screen is a hard happens-before for the whole
    // refresh: any revert the old code would have performed has already landed
    // by then, so the language assertion below cannot pass by accident.
    mockRoute(/^get \/settings$/, {
      id: 'settings-1',
      preferences: '{"language":"zh"}',
      llm_model: MODEL_AFTER_REFRESH,
    });
    const getsBefore = REQUESTS.filter((key) => key === 'get /settings').length;
    fireEvent.click(screen.getByRole('tab', { name: new RegExp(enSettings.tabs.current) }));

    expect(await screen.findByText(MODEL_AFTER_REFRESH)).toBeTruthy();
    expect(REQUESTS.filter((key) => key === 'get /settings').length).toBe(getsBefore + 1);
    // The promise the toast made: the local choice still stands.
    expect(i18n.language).toBe('en');
    expect(screen.getByText(enSettings.language.label)).toBeTruthy();
    // 20s: the interaction chains several waitFor rounds over antd mount +
    // dropdown motion on real timers; under CI contention the default 5s
    // budget timed out even though the behavior was correct.
  }, 20000);
});
