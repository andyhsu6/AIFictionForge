/**
 * Normalize a detected locale (e.g. `zh-CN`, `en-US`, `zh_Hant`, `zh-TW`)
 * to the short resource codes used by this app (`zh`, `en`).
 *
 * The LanguageDetector can return region-qualified tags from localStorage,
 * navigator or the html lang attribute, while our resources are keyed by
 * short codes only. Normalizing before init keeps i18next from seeing an
 * unsupported language and falling back unnecessarily.
 */
export function normalizeLanguage(lang: string | undefined | null): string {
  if (!lang) {
    return '';
  }
  const lower = lang.toLowerCase();
  if (lower.startsWith('zh')) {
    return 'zh';
  }
  if (lower.startsWith('en')) {
    return 'en';
  }
  return lower;
}
