import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';
import { resources } from './resources';
import { normalizeLanguage } from './normalize';
import { setErrorTranslator } from '../services/errorMapper';

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    fallbackLng: 'zh',
    supportedLngs: ['zh', 'en'],
    ns: ['common', 'errors'],
    defaultNS: 'common',
    resources,
    detection: {
      order: ['localStorage', 'navigator', 'htmlTag'],
      caches: ['localStorage'],
      lookupLocalStorage: 'lng',
      // Detector may return zh-CN / en-US etc. — normalize to short codes
      // (zh / en) before i18next resolves the language.
      convertDetectedLanguage: normalizeLanguage,
    },
    interpolation: {
      // React already escapes rendered output
      escapeValue: false,
    },
  });

setErrorTranslator(i18n);

export { i18n, normalizeLanguage };
export default i18n;
