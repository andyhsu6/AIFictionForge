/**
 * Issue #39: SystemSettings renders only inside the ProjectList shell, whose
 * top bar already shows viewTitle.systemSettings -- the page hero repeated the
 * byte-identical title right under it. Same mechanism as #37's Settings fix:
 * the hero yields when embedded, keeps its title for a bare route.
 */
import { beforeAll, afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { App as AntApp } from 'antd';
import SystemSettingsPage from '../pages/SystemSettings';
import i18n from '../i18n';
import enSystemSettings from '../locales/en/systemSettings.json';

const CANNED: Array<[RegExp, unknown]> = [
  [/^get \/auth\/user$/, { id: 1, username: 'admin', display_name: 'Admin', email: null, is_admin: true }],
  [/^get \/settings\/system\/smtp$/, {}],
];

vi.mock('axios', async (importOriginal) => {
  const actual = await importOriginal<typeof import('axios')>();
  const adapter = async (config: { url?: string; method?: string }) => {
    const url = (config.url ?? '').replace(/^\/api/, '').split('?')[0];
    const key = `${(config.method ?? 'get').toLowerCase()} ${url}`;
    const hit = CANNED.find(([re]) => re.test(key));
    return {
      data: hit ? structuredClone(hit[1]) : {},
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

describe('system settings hero yields to the shell top bar (issue #39)', () => {
  it('shows no hero title when embedded (the shell top bar already says it)', async () => {
    render(
      <AntApp>
        <SystemSettingsPage embedded />
      </AntApp>
    );
    await waitFor(() => expect(screen.queryByText(enSystemSettings.page.subtitle)).toBeTruthy());
    expect(screen.queryByText(enSystemSettings.page.title)).toBeNull();
  });

  it('keeps the hero title on a bare route', async () => {
    render(
      <AntApp>
        <SystemSettingsPage />
      </AntApp>
    );
    await waitFor(() => expect(screen.queryByText(enSystemSettings.page.title)).toBeTruthy());
  });
});
