/**
 * 服务端时间戳解析（issue #283）。
 *
 * 后端把时间存成不带时区的字符串（SQLite 的 `func.now()` 是 UTC，序列化也不带偏移），
 * 浏览器对**不带时区**的 datetime 字符串按**本地时间**解析（只有纯日期如 `2026-11-10`
 * 才按 UTC）。于是 UTC 的钟点被当成当地时间显示，跨午夜时连日期都差一天。
 *
 * 这里的约定：凡是服务端下发的时间戳，没有显式时区标记时一律按 UTC 解析。
 * 若某列历史上写的是本地时间，不要用这个函数——先按 #283 统一写库口径。
 */

const ZONE_SUFFIX = /(?:Z|[+-]\d{2}:?\d{2})$/i;

export const parseServerDateTime = (value?: string | null): Date | null => {
  const raw = (value ?? '').trim();
  if (!raw) return null;
  const normalized = raw.includes('T') ? raw : raw.replace(' ', 'T');
  const withZone = ZONE_SUFFIX.test(normalized) ? normalized : `${normalized}Z`;
  const date = new Date(withZone);
  return Number.isNaN(date.getTime()) ? null : date;
};

export const formatServerDateTime = (value?: string | null, fallback = '-'): string => {
  const date = parseServerDateTime(value);
  return date ? date.toLocaleString() : fallback;
};
