/**
 * Issue #37 guard: the settings page is no longer API-only, and the interface
 * language label must not leak Chinese into the en locale. Static JSON checks —
 * the DOM-based full-route CJK scan cannot see this page today (issue #38).
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const FRONTEND_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const load = (loc: string, ns: string) =>
  JSON.parse(readFileSync(join(FRONTEND_ROOT, 'src/locales', loc, `${ns}.json`), 'utf8'));

const CJK = /[一-鿿㐀-䶿]/;

describe('settings page copy is not API-only (issue #37)', () => {
  it('renames the sidebar/view-title key apiSettings -> settings', () => {
    for (const loc of ['zh', 'en']) {
      const pl = load(loc, 'projectList');
      expect(pl.sidebar.settings).toBeTruthy();
      expect(pl.viewTitle.settings).toBeTruthy();
      expect(pl.sidebar).not.toHaveProperty('apiSettings');
      expect(pl.viewTitle).not.toHaveProperty('apiSettings');
    }
    expect(load('zh', 'projectList').sidebar.settings).toBe('设置');
    expect(load('en', 'projectList').sidebar.settings).toBe('Settings');
  });

  it('retitles the page banner away from "AI API"', () => {
    expect(load('zh', 'settings').title).toBe('设置');
    expect(load('en', 'settings').title).toBe('Settings');
    expect(load('zh', 'settings').subtitle).toContain('偏好');
    expect(load('en', 'settings').subtitle).toMatch(/preferences/i);
  });

  it('adds a language tab label in both locales', () => {
    for (const loc of ['zh', 'en']) expect(load(loc, 'settings').tabs.language).toBeTruthy();
    expect(load('zh', 'settings').tabs.language).toBe('语言');
    expect(load('en', 'settings').tabs.language).toBe('Language');
  });

  it('keeps the interface-language label monolingual', () => {
    expect(CJK.test(load('en', 'settings').language.label)).toBe(false);
    expect(load('en', 'settings').language.label).toBe('Interface language');
    expect(load('zh', 'settings').language.label).toBe('界面语言');
    expect(load('zh', 'settings').contentLanguage.label).toBe('生成内容语言');
    expect(CJK.test(load('en', 'settings').contentLanguage.label)).toBe(false);
  });
});
