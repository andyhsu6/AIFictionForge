/**
 * todo 26 (e): language-switch / preference contract.
 *
 * Contract (utils/languageSync.ts, i18n plan priority chain):
 *  - a valid server preference (`preferences.language` = zh|en) is the single
 *    source of truth AFTER login: it overrides any stale localStorage language
 *    (two accounts on one browser never pollute each other);
 *  - no server value: mirror the current local language to the server
 *    (first-login seeding) and keep the local language;
 *  - any failure: silent degradation — keep the localStorage-driven language,
 *    never throw (login must not break because of preference sync).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import i18n from '../i18n';
import { parseServerLanguage, syncLanguageWithServer } from '../utils/languageSync';

const updatePreferences = vi.fn(async () => ({}));
const getSettings = vi.fn(async () => ({ preferences: null as string | null }));

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>();
  return {
    ...actual,
    settingsApi: {
      ...actual.settingsApi,
      getSettings: (...args: unknown[]) => getSettings(...(args as [])),
      updatePreferences: (...args: unknown[]) => updatePreferences(...(args as [])),
    },
  };
});

const setLocalLanguage = (lng: 'zh' | 'en') => window.localStorage.setItem('lng', lng);

describe('parseServerLanguage', () => {
  it.each([
    ['{"language":"en"}', 'en'],
    ['{"language":"zh"}', 'zh'],
    ['{"other":1}', null],
    ['{"language":"fr"}', null],
    ['not json', null],
    [null, null],
    [undefined, null],
  ])('preferences %j -> %j', (raw, expected) => {
    expect(parseServerLanguage(raw)).toBe(expected);
  });
});

describe('syncLanguageWithServer', () => {
  beforeEach(() => {
    getSettings.mockClear();
    updatePreferences.mockClear();
    setLocalLanguage('zh');
  });

  afterEach(async () => {
    await i18n.changeLanguage('zh');
    vi.clearAllMocks();
  });

  it('server preference wins on login (en server, zh local -> en UI)', async () => {
    await i18n.changeLanguage('zh');
    getSettings.mockResolvedValueOnce({ preferences: '{"language":"en"}' });
    await syncLanguageWithServer();
    expect(i18n.language).toBe('en');
    expect(updatePreferences).not.toHaveBeenCalled();
  });

  it('server preference wins the other way (zh server, en local -> zh UI)', async () => {
    setLocalLanguage('en');
    await i18n.changeLanguage('en');
    getSettings.mockResolvedValueOnce({ preferences: '{"language":"zh"}' });
    await syncLanguageWithServer();
    expect(i18n.language).toBe('zh');
  });

  it('no server value: mirrors the local language to the server and keeps it', async () => {
    setLocalLanguage('en');
    await i18n.changeLanguage('en');
    getSettings.mockResolvedValueOnce({ preferences: null });
    await syncLanguageWithServer();
    expect(updatePreferences).toHaveBeenCalledWith({ language: 'en' });
    expect(i18n.language).toBe('en');
  });

  it('two accounts scenario: each login adopts that account server preference', async () => {
    // Account A prefers en; account B prefers zh. Logging in back-to-back on
    // the same browser must switch the UI each time (no localStorage stickiness).
    setLocalLanguage('zh');
    await i18n.changeLanguage('zh');
    getSettings.mockResolvedValueOnce({ preferences: '{"language":"en"}' });
    await syncLanguageWithServer();
    expect(i18n.language).toBe('en');

    getSettings.mockResolvedValueOnce({ preferences: '{"language":"zh"}' });
    await syncLanguageWithServer();
    expect(i18n.language).toBe('zh');
  });

  it('sync failure degrades silently: no throw, local language kept', async () => {
    setLocalLanguage('en');
    await i18n.changeLanguage('en');
    getSettings.mockRejectedValueOnce(new Error('network down'));
    await expect(syncLanguageWithServer()).resolves.toBeUndefined();
    expect(i18n.language).toBe('en');
    expect(updatePreferences).not.toHaveBeenCalled();
  });
});
