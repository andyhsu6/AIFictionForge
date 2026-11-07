import { describe, expect, it } from 'vitest';
import { summarizeFieldChange } from './profileFieldDiff';

describe('summarizeFieldChange', () => {
  it('reports only the removed clause for list-like edits', () => {
    const summary = summarizeFieldChange(
      '对母亲既崇拜、畏惧又渴望、心思缜密、善于谋划',
      '对母亲既崇拜、畏惧又渴望、心思缜密',
    );
    expect(summary.mode).toBe('delta');
    expect(summary.removed).toEqual(['善于谋划']);
    expect(summary.added).toEqual([]);
    expect(summary.similarity).toBeGreaterThan(0.6);
  });

  it('reports only the added clause for list-like edits', () => {
    const summary = summarizeFieldChange(
      '性格温和、喜欢读书',
      '性格温和、喜欢读书、擅长剑术',
    );
    expect(summary.mode).toBe('delta');
    expect(summary.removed).toEqual([]);
    expect(summary.added).toEqual(['擅长剑术']);
  });

  it('falls back to rewrite mode when the whole value is rewritten', () => {
    const summary = summarizeFieldChange(
      '她自幼在温暖的家庭里长大，性格温和，喜欢读书，也很在意别人的评价。',
      '北境佣兵出身，沉默寡言，擅长追踪与潜行，几乎不信任任何人。',
    );
    expect(summary.mode).toBe('rewrite');
    expect(summary.removed).toEqual([]);
    expect(summary.added).toEqual([]);
    expect(summary.oldLength).toBeGreaterThan(0);
    expect(summary.newLength).toBeGreaterThan(0);
  });

  it('treats an empty side as a rewrite instead of dumping the whole value', () => {
    const summary = summarizeFieldChange('', '新的性格描述');
    expect(summary.mode).toBe('rewrite');
    expect(summary.oldLength).toBe(0);
  });

  it('skips character diff for very long values to keep the dialog responsive', () => {
    const summary = summarizeFieldChange('旧'.repeat(1500), '新'.repeat(1500));
    expect(summary.mode).toBe('rewrite');
  });
});
