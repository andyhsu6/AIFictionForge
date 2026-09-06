import i18n, { normalizeLanguage } from '../i18n';
import { settingsApi } from '../services/api';

/**
 * AI 生成内容语言词表（i18n plan todo 17 前端统一出口）。
 * 与后端 app/schemas/common.py 的 ContentLanguageLiteral 对齐：
 * null/undefined 与 'auto' 均表示跟随界面语言（优先级链由后端 language_resolver 解析）。
 */
export type ContentLanguage = 'auto' | 'zh' | 'en';

/**
 * 从 settings.preferences JSON 中解析合法语言值。
 * 仅接受标准化短码 zh / en；JSON 损坏或无该键返回 null。
 */
export function parseServerLanguage(rawPreferences?: string | null): 'zh' | 'en' | null {
  try {
    const prefs = JSON.parse(rawPreferences || '{}');
    if (prefs.language === 'zh' || prefs.language === 'en') {
      return prefs.language;
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * 从 settings.preferences JSON 中解析 AI 生成内容语言（i18n plan todo 16）。
 * 仅接受 auto / zh / en；JSON 损坏或无该键返回 null（null 与 'auto' 均表示跟随界面语言）。
 */
export function parseServerContentLanguage(rawPreferences?: string | null): ContentLanguage | null {
  try {
    const prefs = JSON.parse(rawPreferences || '{}');
    if (prefs.content_language === 'auto' || prefs.content_language === 'zh' || prefs.content_language === 'en') {
      return prefs.content_language;
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * 登录后语言同步（i18n 优先级契约）：
 * - 服务端 preferences.language 存在 → 它是登录后唯一事实来源，覆盖本地 localStorage 残留
 *   （同浏览器多账号互不污染：每次登录都以该账号自己的服务端偏好为准）。
 * - 服务端无值 → 把当前前端语言（detector 由 localStorage/navigator 得出）镜像到后端，完成首次种子化。
 * - 任何失败（401/网络/后端写失败）→ 静默降级：保持 localStorage 引导的当前语言，绝不抛出。
 *
 * changeLanguage 会触发 LanguageDetector 自动写入 localStorage 'lng'，无需手动 setItem。
 */
export async function syncLanguageWithServer(): Promise<void> {
  try {
    const settings = await settingsApi.getSettings();
    const serverLang = parseServerLanguage(settings.preferences);

    if (serverLang) {
      if (normalizeLanguage(i18n.language) !== serverLang) {
        await i18n.changeLanguage(serverLang);
      }
      return;
    }

    // 服务端还没有语言偏好：镜像当前本地语言（非 zh 即 en，与 fallbackLng 'zh' 契约一致）
    const local: 'zh' | 'en' = normalizeLanguage(i18n.language) === 'en' ? 'en' : 'zh';
    await settingsApi.updatePreferences({ language: local });
  } catch (error) {
    console.warn('语言偏好同步失败（不影响使用，localStorage 仍生效）:', error);
  }
}
