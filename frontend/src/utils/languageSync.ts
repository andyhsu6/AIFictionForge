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
 * 登录后语言同步（优先级契约：手动切换 > 系统语言 > 浏览器语言）：
 * - 登录页/任意入口的手动切换会在 sessionStorage 打 `lng_manual` 标记；
 *   登录同步时若标记存在 → 手动选择优先级最高：推送到账号偏好并清除标记
 *   （注意：这会有意覆盖账号旧偏好——产品决策，手动 > 账号）。
 * - 无手动标记且服务端 preferences.language 存在 → 拉取服务端值（设置页的手动
 *   切换已实时写服务端，等价于"最近一次手动"）。
 * - 两者皆无 → 当前 detector 语言（localStorage → navigator；navigator 默认镜像
 *   操作系统语言，即"系统其次浏览器"）镜像到服务端完成首次种子化。
 * - 任何失败 → 静默降级：保持当前语言；手动标记保留，下次登录重试。
 *
 * changeLanguage 会触发 LanguageDetector 自动写入 localStorage 'lng'，无需手动 setItem。
 */
export function markManualLanguageChoice(): void {
  try {
    sessionStorage.setItem('lng_manual', '1');
    console.info('[i18n] manual flag set (sessionStorage ok)');
  } catch (e) {
    console.warn('[i18n] manual flag SET FAILED — sessionStorage unavailable:', e);
  }
}

export async function syncLanguageWithServer(): Promise<void> {
  try {
    let manualPick = false;
    try {
      manualPick = sessionStorage.getItem('lng_manual') === '1';
    } catch (e) {
      console.warn('[i18n] manual flag READ FAILED:', e);
    }
    const settings = await settingsApi.getSettings();
    const serverLang = parseServerLanguage(settings.preferences);
    console.info(`[i18n] sync start: i18n=${i18n.language} server=${serverLang ?? '(none)'} manualFlag=${manualPick}`);

    if (manualPick) {
      // 手动切换优先级最高：本地选择写回账号（覆盖旧偏好），并清除标记
      const local: 'zh' | 'en' = normalizeLanguage(i18n.language) === 'en' ? 'en' : 'zh';
      await settingsApi.updatePreferences({ language: local });
      console.info(`[i18n] sync branch: MANUAL-PUSH ${local} (account preference overwritten)`);
      try {
        sessionStorage.removeItem('lng_manual');
      } catch {
        /* ignore */
      }
      return;
    }

    if (serverLang) {
      if (normalizeLanguage(i18n.language) !== serverLang) {
        console.info(`[i18n] sync branch: SERVER-PULL ${serverLang} (no manual flag this login)`);
        await i18n.changeLanguage(serverLang);
      } else {
        console.info(`[i18n] sync branch: server already matches (${serverLang})`);
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
