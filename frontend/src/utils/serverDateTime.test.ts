import { describe, expect, it } from 'vitest';

import { formatServerDateTime, parseServerDateTime } from './serverDateTime';

describe('parseServerDateTime', () => {
  it('把不带时区的服务端时间戳按 UTC 解析', () => {
    const date = parseServerDateTime('2026-11-10T16:14:09');
    expect(date?.toISOString()).toBe('2026-11-10T16:14:09.000Z');
  });

  it('接受空格分隔的写法（SQLite 默认输出）', () => {
    const date = parseServerDateTime('2026-11-10 16:14:09');
    expect(date?.toISOString()).toBe('2026-11-10T16:14:09.000Z');
  });

  it('已经带时区时不重复追加', () => {
    expect(parseServerDateTime('2026-11-10T16:14:09Z')?.toISOString()).toBe(
      '2026-11-10T16:14:09.000Z',
    );
    expect(parseServerDateTime('2026-11-11T00:14:09+08:00')?.toISOString()).toBe(
      '2026-11-10T16:14:09.000Z',
    );
  });

  it('空值/非法值返回 null，不抛错', () => {
    expect(parseServerDateTime(null)).toBeNull();
    expect(parseServerDateTime('')).toBeNull();
    expect(parseServerDateTime('not-a-date')).toBeNull();
  });
});

describe('formatServerDateTime', () => {
  it('非法值回退占位符', () => {
    expect(formatServerDateTime(null)).toBe('-');
    expect(formatServerDateTime('nope', '—')).toBe('—');
  });

  it('UTC 与本地时区相差 8 小时时，日期可以跨到次日', () => {
    const date = parseServerDateTime('2026-11-10T16:14:09');
    expect(date).not.toBeNull();
    // 断言解析结果本身（与运行环境的 TZ 无关）：同一瞬间在 UTC+8 是 11-11 00:14
    const shifted = new Date(date!.getTime() + 8 * 3600 * 1000);
    expect(shifted.toISOString().slice(0, 10)).toBe('2026-11-11');
  });
});
