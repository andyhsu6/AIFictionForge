/**
 * 拆书失败步骤标签守卫（issue #33 Category 1）。
 *
 * 防的回归：BookImport 失败步骤列表曾直接渲染后端 `step_label`（中文），en 界面
 * 泄漏中文。后端 `step_name` 是稳定英文键，本仓用 `utils/importStepLabels.ts` 把
 * 它映射到 bookImport ns 的 `progress.stepLabel.*`。这里读源码 + locale JSON 断言
 * 每个 step_name 都有分支、zh 与既有文案逐字节一致、en 无中文；未知 step_name
 * 必须回退 step_label 而不是渲染空白。
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { beforeAll, describe, expect, it } from 'vitest';
import i18n from '../i18n';
import { resolveStepLabel } from '../utils/importStepLabels';

function readSource(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf-8');
}

/** 后端 book_import_service.py 的稳定 step_name 值域（字面量，两侧各存一份）。 */
const EXPECTED_STEP_NAMES = [
  'world_building',
  'career_system',
  'characters',
  'relationship_extraction',
] as const;

/** zh 文案必须与迁移前逐字节一致（原后端 step_label）。 */
const EXPECTED_ZH_LABELS: Record<string, string> = {
  world_building: '世界观生成',
  career_system: '职业体系生成',
  characters: '角色与组织生成',
  relationship_extraction: '原文关系抽取',
};

const CJK = /[\u4e00-\u9fff\u3400-\u4dbf]/;

function stepLabelKeys(source: string): Map<string, string> {
  const body = source.match(/const STEP_LABEL_KEYS: Record<string, string> = \{[\s\S]*?\n\};/);
  if (!body) throw new Error('importStepLabels.ts 里找不到 STEP_LABEL_KEYS');
  const map = new Map<string, string>();
  for (const match of body[0].matchAll(/(\w+): '([^']+)'/g)) {
    map.set(match[1], match[2]);
  }
  return map;
}

function localeValue(lang: string, dottedKey: string): unknown {
  const json = JSON.parse(readSource(`../locales/${lang}/bookImport.json`));
  return dottedKey.split('.').reduce<unknown>(
    (node, seg) => (node && typeof node === 'object' ? (node as Record<string, unknown>)[seg] : undefined),
    json,
  );
}

describe('book-import step labels (issue #33 Category 1)', () => {
  const source = readSource('../utils/importStepLabels.ts');
  const keys = stepLabelKeys(source);

  beforeAll(async () => {
    await i18n.changeLanguage('en');
  });

  it('pins the stable step_name value domain literal-for-literal', () => {
    expect(EXPECTED_STEP_NAMES.length).toBe(4);
    expect(new Set(EXPECTED_STEP_NAMES).size).toBe(EXPECTED_STEP_NAMES.length);
  });

  it('has a label branch for every stable step_name', () => {
    const missing = EXPECTED_STEP_NAMES.filter((name) => !keys.has(name));
    expect(missing, `这些 step_name 会回退渲染后端中文 step_label: ${missing.join(', ')}`).toEqual([]);
  });

  it('preserves the exact zh label bytes', () => {
    for (const name of EXPECTED_STEP_NAMES) {
      const key = keys.get(name) as string;
      expect(localeValue('zh', key), `${name} 的 zh 文案变了`).toBe(EXPECTED_ZH_LABELS[name]);
    }
  });

  it('localizes every label in en with real, non-Chinese copy', () => {
    for (const [name, key] of keys) {
      const en = localeValue('en', key);
      expect(typeof en, `${name} 的 en 值缺失`).toBe('string');
      expect(en as string).not.toBe('');
      expect(en as string).not.toMatch(CJK);
      expect(en as string).not.toContain(key);
    }
  });

  it('renders the localized label at runtime and falls back to step_label for unknown keys', () => {
    expect(resolveStepLabel(i18n.t, { step_name: 'world_building', step_label: '世界观生成' })).toBe(
      'Worldview Generation',
    );
    expect(resolveStepLabel(i18n.t, { step_name: 'nope_unknown', step_label: '未知步骤' })).toBe('未知步骤');
  });
});
